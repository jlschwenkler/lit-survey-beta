"""
triage_no_abstract.py  —  Surface RELEVANT papers that the crawl buried because
they had no abstract at discovery time, so the user can decide whether to
hand-pull the ones we can't grab automatically.

WHY THIS STEP EXISTS
--------------------
The crawl's keyword pre-filter (`crawl_citation_graph.keyword_prefilter`) runs on
`title + abstract` at DISCOVERY time. A miss is parked at {score:1, deferred:True}
and NEVER sent to Claude. The only thing that revisits a deferred miss is
`rescue_by_citedness.py`, which requires accumulated in-corpus citedness — so a
paper that is central to the literature but (a) has an uninformative title AND
(b) has no API abstract AND (c) is thinly cited *within our corpus* falls through
every net. (Discovered 2026-06-02 from a grad-student's list of "obvious"
omissions — Hurd "What in the World is Wrong?", "The Deontology of Negligence",
H. Smith "Non-Tracing Cases of Culpable Ignorance", Rosen "Skepticism about
Moral Responsibility", etc. — every one present in the graph but abstract-less.)

There are THREE failure modes this tool repairs:
  1. STALE-CACHE miss   — the prefilter was improved later (the strong
     phil-action term list grew) but old cache verdicts weren't re-run; the
     node PASSES the current gate. (e.g. "Negligence and Moral Responsibility",
     "The Deontology of Negligence".)
  2. GRABBABLE abstract — an abstract is available from OpenAlex/Crossref/S2
     right now; the crawl just never pulled it. (e.g. Zimmerman 1986, Vargas
     2005, Rosen 2003.) -> auto-grab + re-score; flows into the matrix if it
     clears threshold.
  3. UNGRABBABLE        — uninformative title, no API abstract anywhere.
     (e.g. both Hurd papers.) -> can't be fixed automatically; goes on the
     hand-pull WORKLIST for the user to retrieve manually.

RELEVANCE MODEL (per the user, 2026-06-02): a buried no-abstract node is
"relevant enough to surface" if EITHER signal fires (OR, the widest net):
  - TITLE is relevant: a title-only Claude relevance score >= --title-min
    (default 3), OR the title passes the current keyword pre-filter; AND/OR
  - CITEDNESS: in-corpus in-degree (raw, from graph edges) >= --cite-min
    (default 3). This is the keyword-independent "the corpus leans on it" signal.
Relevance is title-words + network citedness, exactly as specified.

WHAT IT DOES
------------
For every candidate node (abstract-less and/or a deferred/stale prefilter miss,
not already in the matrix at score>=4):
  * compute in-corpus in-degree (raw) from graph["edges"];
  * try to FETCH an abstract (OpenAlex -> Crossref -> S2 batch), reusing
    backfill_abstracts' resolvers;
  * score relevance with Claude — from the fetched abstract if we got one, else
    title-only;
  * decide surfacing by the OR rule above.
Then it splits the surfaced set into:
  A. RECOVERED-AUTOMATICALLY — abstract grabbed; node["abstract"]+source written,
     graph["scores"][key] overwritten with the new Claude verdict (+ provenance
     triaged_by/triage_indegree). If it now scores >= --threshold it will enter
     the matrix on the next `score_engagement.py` run.
  B. HAND-PULL WORKLIST — relevant but no abstract anywhere; left UNTOUCHED in
     the graph (still deferred), written to the report + a keys file so the user
     can retrieve abstracts by hand and re-feed them.

HARD RULES (project conventions):
  - NEVER fabricate text about a work. Every abstract written is API-returned for
    THAT item (its own DOI / OA id / S2 id). < MIN_ABS_CHARS is treated a stub.
  - Snapshot the graph before writing (this mutates graph["scores"] + abstracts).
  - requests sessions verify=False + urllib3 warnings disabled (project default).
  - API mailto from $CROSSREF_MAILTO (OpenAlex/Crossref polite pool).

OUTPUTS
  - triage_no_abstract_report.md   — human-readable: recovered list + ranked
                                      hand-pull worklist (title, year, venue,
                                      in-corpus in-degree, title-score, link).
  - triage_handpull_keys.txt       — node keys on the worklist (one per line).
  - triage_recovered_keys.txt       — node keys that gained an abstract + a fresh
                                      score >= threshold (feed to score_engagement).
  - triage_score_cache.json         — per-key {abstract, score, reason} cache.

COST NOTE — the default run is a PREVIEW, not "free":
  The default (no --write) still fetches an abstract and Claude-scores EVERY
  surfaced candidate — that is the paid step. It just doesn't mutate the graph.
  The verdicts are cached to triage_score_cache.json, so the follow-up --write
  REUSES them (no re-scoring, no second charge) and the preview's counts exactly
  match what --write persists. Pass --refresh to discard the cache and re-score.
  (Writes into citation_graph.json only with --write.)

USAGE
  python3 triage_no_abstract.py            # PREVIEW: score + cache, no graph write
  python3 triage_no_abstract.py --write    # apply grabs+rescores (reuses cache, free)
  python3 triage_no_abstract.py --refresh  # ignore cache, re-score (paid)
  python3 triage_no_abstract.py --limit 40                              # smoke test
  # after a --write run, admit the recovered papers into the matrix:
  python3 score_engagement.py --keys-file triage_recovered_keys.txt
  python3 enrich_work_type.py && python3 enrich_book_kind.py && \\
    python3 enrich_citedness.py && python3 enrich_ref_provenance.py && \\
    python3 enrich_links.py && python3 build_lit_table.py
"""

import ssl
import os as _os  # TLS verification on by default; opt out with INSECURE_TLS=1
if _os.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    ssl._create_default_https_context = ssl._create_unverified_context

import argparse, json, os, re, time
from collections import Counter
from datetime import datetime

import requests, urllib3
import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
import crawl_citation_graph as C            # claude_score, keyword_prefilter, node_key
import backfill_abstracts as B              # resolve_abstract, s2_batch_abstracts, norm_doi, good

# ── Network resilience ─────────────────────────────────────────────────────────
# A single hung fetch (connection opens, server then stalls without sending data)
# can wedge the whole loop well past `timeout=` on some endpoints. Harden the
# shared session backfill_abstracts uses: a capped retry adapter + a (connect,
# read) timeout tuple. We also default every print to flush so progress is
# visible in a piped/background run (Python fully-buffers a pipe otherwise — that
# made an earlier run look "hung" when it was just buffering).
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
    _retry = Retry(total=2, connect=2, read=1, backoff_factor=0.5,
                   status_forcelist=(429, 500, 502, 503, 504))
    _adapter = HTTPAdapter(max_retries=_retry)
    B.S.mount("https://", _adapter)
    B.S.mount("http://", _adapter)
except Exception:
    pass

# Force a (connect, read) timeout on every backfill_abstracts request, even the
# ones whose call sites pass a scalar timeout — a tuple caps connect AND read.
_orig_get, _orig_post = B.S.get, B.S.post
def _capped(fn):
    def w(*a, **kw):
        kw["timeout"] = (8, 20)          # 8s to connect, 20s for the body
        return fn(*a, **kw)
    return w
B.S.get  = _capped(_orig_get)
B.S.post = _capped(_orig_post)

import builtins
def print(*a, **kw):                      # flush by default so piped runs show progress
    kw.setdefault("flush", True)
    builtins.print(*a, **kw)

FOLDER         = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH     = os.path.join(FOLDER, "citation_graph.json")
MATRIX_PATH    = os.path.join(FOLDER, "engagement_matrix.json")
REPORT_PATH    = os.path.join(FOLDER, "triage_no_abstract_report.md")
HANDPULL_PATH  = os.path.join(FOLDER, "triage_handpull_keys.txt")
RECOVERED_PATH = os.path.join(FOLDER, "triage_recovered_keys.txt")
# Per-key cache of {abstract, abs_src, score, reason} from the most recent run.
# This is what makes the dry-run a real preview AND stops --write from re-paying:
# the expensive half (fetch abstract + Claude-score every candidate) runs ONCE,
# is cached here, and a later --write reuses the cached verdicts verbatim instead
# of re-fetching and re-scoring. Mirrors crawl_citation_graph.py's --resume
# checkpoint discipline. Delete this file to force a fresh (paid) re-score.
CACHE_PATH     = os.path.join(FOLDER, "triage_score_cache.json")
ARCHIVE        = os.path.join(FOLDER, "_archive")

EMAIL = os.environ.get("CROSSREF_MAILTO", "you@example.com")


def has_abs(n):
    return bool((n.get("abstract") or "").strip())


def link_for(key, n):
    """Best human link for the worklist (DOI -> OA landing -> S2 record)."""
    doi = B.norm_doi(n.get("doi"))
    if doi:
        return f"https://doi.org/{doi}"
    if n.get("oa_id"):
        oid = n["oa_id"].replace("https://openalex.org/", "")
        return f"https://openalex.org/{oid}"
    if n.get("s2_id"):
        return f"https://www.semanticscholar.org/paper/{n['s2_id']}"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="apply abstract grabs + re-scores to citation_graph.json. "
                         "Default is PREVIEW: it still fetches + Claude-scores every "
                         "candidate (this is the paid step) and caches the result, "
                         "but does not mutate the graph. A subsequent --write reuses "
                         "the cache for free.")
    ap.add_argument("--threshold", type=int, default=4,
                    help="min relevance score to enter the matrix (default 4, "
                         "matches the crawl)")
    ap.add_argument("--title-min", type=int, default=3,
                    help="min title-only Claude score for the TITLE signal (default 3)")
    ap.add_argument("--cite-min", type=int, default=3,
                    help="min in-corpus in-degree for the CITEDNESS signal (default 3)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap candidates scored (smoke test)")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore the score cache and re-fetch + re-score every "
                         "candidate (paid). Default reuses triage_score_cache.json "
                         "so the dry-run preview and the --write commit agree and "
                         "you don't pay twice.")
    args = ap.parse_args()

    # Load the per-key score/abstract cache from a prior run (see CACHE_PATH note).
    # Reusing it makes --write free (no re-score) and makes the dry-run an exact
    # preview of what --write will persist. --refresh discards it.
    cache = {}
    if os.path.exists(CACHE_PATH) and not args.refresh:
        try:
            cache = json.load(open(CACHE_PATH))
            print(f"Loaded {len(cache)} cached scores from "
                  f"{os.path.basename(CACHE_PATH)} (use --refresh to re-score).")
        except Exception:
            cache = {}

    graph  = json.load(open(GRAPH_PATH))
    nodes  = graph["nodes"]
    scores = graph["scores"]
    edges  = graph["edges"]
    # The matrix is OPTIONAL: it only exists after scoring (Stage 4), but triage is
    # documented to run EARLY (Stage 2.6). If absent, no rows are matrix-excluded.
    if os.path.exists(MATRIX_PATH):
        mat = json.load(open(MATRIX_PATH))
        in_matrix = {r["key"] for r in mat["rows"]}
    else:
        in_matrix = set()

    # in-corpus in-degree (raw) — the keyword-independent citedness signal
    indeg = Counter(e["to"] for e in edges)

    # ── candidate set ─────────────────────────────────────────────────────────
    # A node is a candidate if it is NOT already a matrix row AND it was either
    # (a) buried with no abstract, or (b) parked as a prefilter miss / deferred.
    # We exclude nodes that already have a genuine Claude verdict >= threshold
    # (those are handled by the normal pipeline).
    candidates = []
    for k, n in nodes.items():
        if k in in_matrix:
            continue
        sc = scores.get(k) or {}
        reason = sc.get("reason", "")
        deferred = bool(sc.get("deferred"))
        prefilter_miss = (reason == "failed keyword pre-filter")
        # surface if buried-no-abstract, or a deferred/prefilter park
        if not has_abs(n) or deferred or prefilter_miss:
            # but skip nodes that already earned a real score >= threshold
            if (not deferred and not prefilter_miss
                    and sc.get("score", 0) >= args.threshold):
                continue
            candidates.append((k, n))

    print(f"Candidates (buried / deferred / prefilter-miss, not in matrix): "
          f"{len(candidates)}")

    # ── cheap pre-screen (no API spend) ────────────────────────────────────────
    # Scoring EVERY abstract-less node title-only through Claude is ~7k calls,
    # almost all on obvious noise. The OR surfacing rule can only fire if the
    # TITLE passes the keyword gate OR the in-degree clears the floor — so a node
    # that fails BOTH cheap signals can never surface. Pre-screen on those two
    # free signals and only spend a Claude call on survivors. (Limitation: a node
    # with an uninformative title AND in-degree < cite-min that Claude would
    # nonetheless score >= title-min is skipped — but that is the weakest possible
    # relevance evidence, and the user's stated preference is to over-exclude here
    # rather than burn thousands of calls. Lower --cite-min to widen the net.)
    before = len(candidates)
    candidates = [(k, n) for k, n in candidates
                  if C.keyword_prefilter(n.get("title") or "", "")
                  or indeg.get(k, 0) >= args.cite_min]
    print(f"  cheap pre-screen (title keyword-pass OR in-degree >= {args.cite_min}): "
          f"{before} -> {len(candidates)} to Claude-score")

    if args.limit:
        # rank candidates by in-degree first so a smoke test sees the juicy ones
        candidates.sort(key=lambda kn: -indeg.get(kn[0], 0))
        candidates = candidates[:args.limit]
        print(f"  (limited to top-{args.limit} by in-corpus in-degree)")

    # ── S2 batch pre-pass for candidate DOIs (cheap bulk abstract source) ──────
    cand_dois = [B.norm_doi(n.get("doi")) for _, n in candidates]
    B.S2_LOOKUP = {d.lower(): a for d, a in
                   B.s2_batch_abstracts([d for d in cand_dois if d]).items()}
    print(f"  S2 batch returned {len(B.S2_LOOKUP)} abstracts")

    recovered, handpull, errors = [], [], 0
    n_cached_hits, n_scored = 0, 0
    for i, (k, n) in enumerate(candidates, 1):
        title = n.get("title") or ""
        if not title:
            continue
        deg = indeg.get(k, 0)

        cached = cache.get(k)
        if cached is not None:
            # reuse the prior run's fetch + score — no network, no paid call
            abs = cached.get("abstract") or None
            src = cached.get("abs_src")
            score = cached.get("score", 1)
            reason = cached.get("reason", "")
            got_abs = bool(cached.get("got_abs"))
            n_cached_hits += 1
        else:
            try:
                # 1. try to grab an abstract (network; isolated so one stall/error
                #    can't wedge or crash the whole run)
                abs, src = B.resolve_abstract(n)
            except Exception as e:
                abs, src, errors = None, None, errors + 1
                if errors <= 10:
                    print(f"    [fetch error #{errors}] {title[:50]}: "
                          f"{type(e).__name__}")
            got_abs = bool(abs and B.good(abs))

            # 2. score relevance — from the fetched abstract if any, else title-only
            try:
                score, reason = C.claude_score(title, abs if got_abs else "")
            except Exception as e:
                score, reason = 1, f"score error: {type(e).__name__}"
            n_scored += 1
            # cache this verdict so --write reuses it and we never re-pay
            cache[k] = {"abstract": abs if got_abs else "", "abs_src": src,
                        "score": score, "reason": reason, "got_abs": got_abs}

        title_pass_kw = C.keyword_prefilter(title, "")

        # 3. OR-rule: surface if title-signal OR citedness-signal fires.
        #    title_signal: a relevant title (Claude title score >= title-min) OR a
        #      keyword-gate pass.
        #    cite_signal: enough in-corpus in-degree — BUT vetoed when the relevance
        #      score is 1 ("not relevant"). A high in-degree on a clearly-off-topic
        #      item (front matter "List of Charts and Figures", a tangential paper
        #      cited only because it shares a volume) is corpus-structure noise, not
        #      a relevance signal — so citedness alone can resurrect a thinly-TITLED
        #      paper, but cannot override an explicit "not relevant" read.
        title_signal = (score >= args.title_min) or title_pass_kw
        cite_signal  = (deg >= args.cite_min) and (score >= 2)
        surface = title_signal or cite_signal

        rec = {
            "key": k, "title": title, "year": n.get("year"),
            "venue": n.get("venue") or "", "indegree": deg,
            "score": score, "reason": reason, "got_abs": got_abs,
            "abs_src": src, "abs_text": abs if got_abs else "",
            "link": link_for(k, n),
            "title_signal": title_signal, "cite_signal": cite_signal,
        }

        if not surface:
            pass  # genuinely off-topic; leave buried, don't report
        elif got_abs:
            recovered.append(rec)
        else:
            handpull.append(rec)

        if i % 25 == 0 or i == len(candidates):
            print(f"  [{i}/{len(candidates)}] surfaced: "
                  f"{len(recovered)} recovered, {len(handpull)} hand-pull")
        time.sleep(0.05)

    # persist the cache so a follow-up --write (or re-run) reuses these verdicts
    # instead of re-fetching + re-paying. This is the fix for the double-spend and
    # the non-deterministic dry-run/commit mismatch.
    try:
        json.dump(cache, open(CACHE_PATH, "w"), ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  (warning: could not write score cache: {type(e).__name__})")
    print(f"\nScoring: {n_scored} paid Claude call(s) this run, "
          f"{n_cached_hits} reused from cache. "
          f"Cache -> {os.path.basename(CACHE_PATH)} ({len(cache)} keys).")

    # rank both lists: citedness first, then score, then title
    keyf = lambda r: (-r["indegree"], -r["score"], r["title"].lower())
    recovered.sort(key=keyf)
    handpull.sort(key=keyf)

    # ── write graph mutations (only with --write) ──────────────────────────────
    wrote_recovered = []
    if args.write and recovered:
        os.makedirs(ARCHIVE, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap = os.path.join(ARCHIVE, f"citation_graph.backup_pre_triage_{stamp}.json")
        json.dump(graph, open(snap, "w"), ensure_ascii=False, indent=2)
        print(f"\nSnapshot -> {os.path.relpath(snap, FOLDER)}")
        for r in recovered:
            k = r["key"]; n = nodes[k]
            # reuse the abstract from the scored run (cache), not a fresh fetch —
            # re-fetching could return different/empty text and would waste calls.
            cached = cache.get(k) or {}
            abs_text, src = cached.get("abstract") or r.get("abs_text"), r.get("abs_src")
            if not (abs_text and B.good(abs_text)):
                # fall back to a fetch only if the cache somehow lacks the text
                abs_text, src = B.resolve_abstract(n)
            if not (abs_text and B.good(abs_text)):
                continue
            n["abstract"] = abs_text
            n["abstract_source"] = src
            scores[k] = {"score": r["score"], "reason": r["reason"],
                         "triaged_by": "no_abstract_triage",
                         "triage_indegree": r["indegree"]}
            if r["score"] >= args.threshold:
                wrote_recovered.append(k)
        json.dump(graph, open(GRAPH_PATH, "w"), ensure_ascii=False, indent=2)
        print(f"Wrote {len(recovered)} recovered abstracts + scores to graph; "
              f"{len(wrote_recovered)} now score >= {args.threshold}.")
    elif recovered:
        # dry-run: which WOULD enter the matrix
        wrote_recovered = [r["key"] for r in recovered if r["score"] >= args.threshold]

    # ── reports ─────────────────────────────────────────────────────────────────
    with open(HANDPULL_PATH, "w") as f:
        f.write("\n".join(r["key"] for r in handpull) + ("\n" if handpull else ""))
    with open(RECOVERED_PATH, "w") as f:
        f.write("\n".join(wrote_recovered) + ("\n" if wrote_recovered else ""))

    def fmt_rows(rows):
        out = []
        for r in rows:
            sig = "+".join([s for s, on in
                            [("title", r["title_signal"]), ("cited", r["cite_signal"])] if on])
            out.append(
                f"| {r['indegree']} | {r['score']} | {sig} | {r['year'] or '—'} | "
                f"{r['title'][:70]} | {r['venue'][:28]} | "
                f"[link]({r['link']}) |" if r["link"] else
                f"| {r['indegree']} | {r['score']} | {sig} | {r['year'] or '—'} | "
                f"{r['title'][:70]} | {r['venue'][:28]} | — |")
        return out

    mode = "WRITE (graph mutated)" if args.write else "DRY-RUN (no changes)"
    lines = [
        f"# No-abstract triage report — {datetime.now():%Y-%m-%d %H:%M} — {mode}",
        "",
        "Papers the crawl buried for lack of an abstract, surfaced by the OR rule "
        f"(title-only Claude score ≥ {args.title_min} OR keyword-pass) OR "
        f"(in-corpus in-degree ≥ {args.cite_min}).",
        "",
        f"- Candidates examined: **{len(candidates)}**",
        f"- **Recovered automatically** (abstract grabbed, re-scored): "
        f"**{len(recovered)}** — of which **{len(wrote_recovered)}** score ≥ "
        f"{args.threshold} and {'now enter' if args.write else 'would enter'} the matrix.",
        f"- **Hand-pull worklist** (relevant, no API abstract anywhere): "
        f"**{len(handpull)}**.",
        "",
        "## A. Recovered automatically (abstract fetched + re-scored)",
        "",
        "Columns: in-corpus in-degree · relevance score · which signal fired · year · title · venue · link.",
        "",
        "| In-corp | Score | Signal | Year | Title | Venue | Link |",
        "|--:|--:|:--|:--|:--|:--|:--|",
        *fmt_rows(recovered),
        "",
        "## B. Hand-pull worklist — no API abstract; retrieve manually",
        "",
        "These are relevant by title and/or in-corpus citedness but have no "
        "abstract in OpenAlex / Crossref / Semantic Scholar. Pull the abstract "
        "by hand (publisher page / PhilPapers / PDF), paste it into the node, "
        "then re-score. Keys are in `triage_handpull_keys.txt`.",
        "",
        "| In-corp | Score | Signal | Year | Title | Venue | Link |",
        "|--:|--:|:--|:--|:--|:--|:--|",
        *fmt_rows(handpull),
        "",
    ]
    open(REPORT_PATH, "w", encoding="utf-8").write("\n".join(lines))
    print(f"\nReport  -> {os.path.basename(REPORT_PATH)}")
    print(f"Worklist-> {os.path.basename(HANDPULL_PATH)} ({len(handpull)} keys)")
    print(f"Recovered keys-> {os.path.basename(RECOVERED_PATH)} ({len(wrote_recovered)} keys)")
    if not args.write:
        print("\nPREVIEW: scores computed + cached, but no graph changes written. "
              "Re-run with --write to apply — it REUSES the cached scores above "
              "(no re-scoring, no extra cost) and persists exactly the "
              f"{len(wrote_recovered)} matrix entries previewed here.")


if __name__ == "__main__":
    main()

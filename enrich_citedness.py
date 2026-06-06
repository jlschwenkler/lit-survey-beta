"""
enrich_citedness.py  —  Add citedness signals to the engagement matrix, with
CROSS-SOURCE RECONCILIATION so a single index can't quietly inject a wrong count.

(1) GLOBAL citedness — reconciled across up to three sources:
      - OpenAlex   node["citations"]            (cited_by_count, on the graph node)
      - Crossref   is-referenced-by-count        (queried here by DOI, cached)
      - Scopus     row["scopus_cites"]           (if enrich_citedness_scopus.py ran)
    WHY: OpenAlex systematically OVER-counts citations for EDITED-VOLUME /
    HANDBOOK CHAPTERS — it pools volume-level or sibling-chapter citations onto a
    single chapter record. Measured here: chapter DOIs run 3x-6x higher in
    OpenAlex than Crossref (e.g. Robichaud "Epistemic Condition…" OA=107 but
    Crossref=0 and Google Scholar~2). The reliability gate used to catch only
    work_type "book"/"book-chapter", but OpenAlex labels many chapters "article"
    or "other", so the inflated counts slipped through and displayed as trusted.

    Reconciliation (per row):
      - Gather the available counts.
      - If the OpenAlex count is an OUTLIER HIGH vs the others (max > DIVERGENCE
        x the min of the agreeing sources, and the gap is material), the count is
        deemed untrustworthy: pick the CONSERVATIVE (lower, agreeing) value for
        display and set cite_reliable=False.
      - Structural gate (independent of counts): chapters — detected by ISBN /
        handbook DOI patterns OR work_type in {book, book-chapter, other} — and
        very recent work (< RECENCY_YEARS) are always cite_reliable=False.
    Emits, per row:
      citedness          chosen (conservative) global count, or None
      citedness_sources  {"openalex":n, "crossref":n, "scopus":n} actually seen
      citedness_source   which source the chosen value came from
      cite_reliable      False for chapters / very recent / divergent counts
      cite_diverged      True when sources disagreed enough to distrust OA
      cites_per_year     chosen count / max(1, age)  — partial recency correction

(2) WITHIN-CORPUS citedness — in_corpus_cites: how many OTHER papers in this
    graph cite this paper (in-degree on the edge list), aggregated across a
    paper's duplicate node keys. No API, no coverage gap for crawled relations.

Graph is read-only here. Re-runnable: Crossref lookups are cached in
citedness_crossref_cache.json; pass --refresh to re-query.

Usage:
  python3 enrich_citedness.py
  python3 enrich_citedness.py --refresh        # re-query Crossref counts
"""

import os, json, re, time, argparse
from collections import defaultdict

import ssl
import os as _os  # TLS verification on by default; opt out with INSECURE_TLS=1
if _os.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    ssl._create_default_https_context = ssl._create_unverified_context
import requests, urllib3
import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
FOLDER      = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH  = os.path.join(FOLDER, "citation_graph.json")
MATRIX_PATH = os.path.join(FOLDER, "engagement_matrix.json")
CACHE_PATH  = os.path.join(FOLDER, "citedness_crossref_cache.json")
ISSUES_PATH = os.path.join(FOLDER, "issues_final.json")  # per-issue leverage weights

# ── leverage-weighted in-corpus citedness ─────────────────────────────────────
# in_corpus_cites_weighted weights each in-corpus citation by the CITING paper's
# engagement tier, so a citation from a thesis-central (starred) paper counts more
# than one from a below-fold paper. This is a STRONGER relevance signal than the
# raw count (which treats every citer equally) and it doubles as the triage score
# for the keyword-pre-filter rescue (rescue_by_citedness.py). It is SEPARATE from
# the gated cite BOOST that build_lit_table.py adds to composite — that's a global
# citedness bonus; this is in-network only. (The interaction of this signal with
# the composite cite boost is a deliberately deferred design question.)
#
# CORE_ISSUES (which issue ids count toward a citer's leverage) is DERIVED at runtime
# from issues_final.json — an issue counts unless it sets "core": false — exactly as
# build_lit_table.py derives its CORE. (A hardcoded A1..A6 list silently summed to 0
# on any non-negligence topic, breaking the citedness weighting + the rescue triage.)
# See main(). The tier thresholds below are heuristic multipliers on the CITING
# paper's leverage (chosen 2026-06-01 vs additive 1+0.15*lev — same ordering, cleaner
# integers); they bias citer WEIGHTING only, they don't gate inclusion.
STAR_LEV    = 15.0   # citer leverage >= this  -> 3x
VISIBLE_LEV = 9.0    # citer leverage >= this  -> 2x   (else / off-matrix -> 1x)

def cite_tier_multiplier(citer_leverage):
    if citer_leverage >= STAR_LEV:    return 3.0
    if citer_leverage >= VISIBLE_LEV: return 2.0
    return 1.0


def core_issue_ids(issues_final, matrix=None):
    """The issue ids that count toward leverage, DERIVED from the project's own
    issues_final.json (an issue counts unless it sets "core": false) — the same rule
    build_lit_table.py uses, so all three stay topic-agnostic. Falls back to every id
    present in the matrix scores when issues_final is absent/empty. Shared so
    enrich_citedness and rescue_by_citedness can't drift apart.
        issues_final : the parsed issues_final.json dict (or None).
        matrix       : the parsed engagement_matrix.json dict (or None), used only
                       for the fallback id set."""
    ids = [i["id"] for i in (issues_final or {}).get("issues", [])
           if bool(i.get("core", True))]
    if ids:
        return ids
    rows = (matrix or {}).get("rows", []) if matrix else []
    return sorted({k for r in rows for k in (r.get("scores") or {})})

EMAIL         = os.environ.get("CROSSREF_MAILTO", "you@example.com")
CURRENT_YEAR  = 2026
RECENCY_YEARS = 4      # items newer than this get cite_reliable=False

# OpenAlex is judged an outlier-high (untrustworthy) when it exceeds the smallest
# corroborating source by at least this factor AND by at least this absolute gap.
DIVERGENCE    = 3.0
MIN_ABS_GAP   = 8

# DOI substrings that mark an edited-volume / handbook / book chapter. OpenAlex
# mislabels many of these as "article", so we detect them structurally too.
CHAPTER_DOI_PATTERNS = [
    "9780", "9781",            # ISBN-13 prefixes embedded in chapter DOIs
    "acprof:oso", "acprof:osobl", "oso/97", "oxfordhb", "med/97",
    "wbiee",                   # Wiley-Blackwell encyclopedia of ethics chapters
    "cbo97",                   # Cambridge Books Online
    "/9781", "/9780",
]
CHAPTER_WORK_TYPES = {"book", "book-chapter", "chapter", "other"}

S = requests.Session()
_VERIFY_TLS = os.environ.get("INSECURE_TLS", "") not in ("1", "true", "True")  # verify TLS unless user opts out
S.verify = _VERIFY_TLS
S.headers["User-Agent"] = f"mailto:{EMAIL}"


def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def norm_doi(d):
    if not d:
        return None
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", str(d).strip(), flags=re.I)
    return d[4:] if d.lower().startswith("doi:") else d


def doi_for(key, node):
    """The DOI for a row, from its key (doi:…) or the graph node."""
    if key.startswith("doi:"):
        return key[4:]
    return norm_doi((node or {}).get("doi"))


def looks_like_chapter(doi, work_type):
    if work_type in CHAPTER_WORK_TYPES:
        return True
    d = (doi or "").lower()
    return any(p in d for p in CHAPTER_DOI_PATTERNS)


def crossref_cites(doi, cache, refresh):
    """Crossref is-referenced-by-count for a DOI. Cached. None on miss/error."""
    if not doi:
        return None
    if not refresh and doi in cache:
        return cache[doi]
    val = None
    try:
        r = S.get(f"https://api.crossref.org/works/{doi}",
                  params={"mailto": EMAIL}, timeout=15)
        if r.ok:
            val = as_int((r.json().get("message") or {}).get("is-referenced-by-count"))
    except Exception:
        val = None
    cache[doi] = val
    time.sleep(0.12)
    return val


def reconcile(oa, cref, scopus, is_chapter):
    """Choose a trustworthy global count and decide reliability.

    Returns (chosen_count, chosen_source, diverged_bool).
    Strategy: collect the available non-None counts. If OpenAlex is the clear
    outlier-high vs a corroborating source, distrust it and take the conservative
    (lower, agreeing) value. Otherwise prefer Scopus > Crossref > OpenAlex
    (Scopus/Crossref are conservative & chapter-aware; OA is the inflater).
    """
    counts = {"openalex": oa, "crossref": cref, "scopus": scopus}
    present = {k: v for k, v in counts.items() if v is not None}
    if not present:
        return None, None, False

    corroborators = [v for k, v in present.items() if k != "openalex"]
    diverged = False
    if oa is not None and corroborators:
        lo = min(corroborators)
        if oa >= max(DIVERGENCE * max(lo, 1), lo + MIN_ABS_GAP):
            diverged = True

    if diverged:
        # trust the corroborating sources; take the lower (most conservative)
        if scopus is not None and cref is not None:
            chosen = min(scopus, cref)
            src = "scopus" if chosen == scopus else "crossref"
        elif scopus is not None:
            chosen, src = scopus, "scopus"
        else:
            chosen, src = cref, "crossref"
        return chosen, src, True

    # no divergence: prefer the conservative, chapter-aware sources
    for src in ("scopus", "crossref", "openalex"):
        if present.get(src) is not None:
            return present[src], src, False
    return None, None, False


def dedup_id(node):
    """Stable identity for a paper across its DOI/OA/S2 node keys."""
    doi = (node.get("doi") or "").strip().lower()
    if doi and doi != "none":
        return "doi:" + re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    oa = (node.get("oa_id") or "").strip()
    if oa and oa != "None":
        return "oa:" + oa
    s2 = (node.get("s2_id") or "").strip()
    if s2 and s2 != "None":
        return "s2:" + s2
    return "title:" + (node.get("title") or "").strip().lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="re-query Crossref counts, ignoring the cache")
    args = ap.parse_args()

    g = json.load(open(GRAPH_PATH))
    nodes, edges = g["nodes"], g["edges"]
    mat = json.load(open(MATRIX_PATH))

    # ── (2) within-corpus in-degree, aggregated by paper identity ──
    key_to_id = {k: dedup_id(n) for k, n in nodes.items()}

    # leverage of each matrix paper-IDENTITY (so a citing node's weight is its
    # work's engagement tier). Weights come from issues_final.json, the same
    # authoritative source build_lit_table.py reads.
    fin = json.load(open(ISSUES_PATH)) if os.path.exists(ISSUES_PATH) else {}
    issue_w = {i["id"]: float(i.get("weight", 1.0)) for i in fin.get("issues", [])}
    wt_of = lambda iid: issue_w.get(iid, 1.0)
    # Issue ids that count toward leverage — DERIVED from the project's own issues
    # (core unless explicitly core:false), NOT a hardcoded list (see core_issue_ids).
    core_issues = core_issue_ids(fin, mat)
    lev_by_id = {}
    for r in mat["rows"]:
        pid = key_to_id.get(r["key"])
        if pid is None:
            continue
        sc = r.get("scores", {}) or {}
        lev = sum(wt_of(i) * int(sc.get(i, 0)) for i in core_issues)
        # if several rows map to one identity, keep the max leverage
        lev_by_id[pid] = max(lev_by_id.get(pid, 0.0), lev)

    cited_by = defaultdict(set)          # raw: distinct citing identities
    weighted = defaultdict(float)        # tiered-weighted by citer leverage
    counted_pairs = set()                # so a citer-identity counts once per cited
    for e in edges:
        f, t = e.get("from"), e.get("to")
        if f in key_to_id and t in key_to_id:
            fid, tid = key_to_id[f], key_to_id[t]
            if fid != tid:
                cited_by[tid].add(fid)
                if (fid, tid) not in counted_pairs:
                    counted_pairs.add((fid, tid))
                    weighted[tid] += cite_tier_multiplier(lev_by_id.get(fid, 0.0))
    in_corpus          = {pid: len(citers) for pid, citers in cited_by.items()}
    in_corpus_weighted = {pid: round(v, 1) for pid, v in weighted.items()}

    # ── (1) global citedness, reconciled across sources ──
    cache = json.load(open(CACHE_PATH)) if os.path.exists(CACHE_PATH) else {}

    n_diverged = n_chapter = 0
    for r in mat["rows"]:
        n = nodes.get(r["key"], {})
        wt = n.get("work_type", "article")
        year = as_int(r.get("year")) or as_int(n.get("year"))
        doi = doi_for(r["key"], n)

        oa     = as_int(n.get("citations"))
        cref   = crossref_cites(doi, cache, args.refresh)
        scopus = as_int(r.get("scopus_cites"))  # set by enrich_citedness_scopus.py

        chosen, src, diverged = reconcile(oa, cref, scopus, looks_like_chapter(doi, wt))
        is_chapter = looks_like_chapter(doi, wt)
        too_new = year is not None and (CURRENT_YEAR - year) < RECENCY_YEARS

        r["citedness"]         = chosen
        r["citedness_sources"] = {k: v for k, v in
                                  {"openalex": oa, "crossref": cref, "scopus": scopus}.items()
                                  if v is not None}
        r["citedness_source"]  = src
        r["cite_diverged"]     = diverged
        # reliable only if: we have a count, it's not a chapter, not too new,
        # and the sources did not diverge (i.e. we didn't have to distrust OA).
        r["cite_reliable"] = bool(chosen is not None and not is_chapter
                                  and not too_new and not diverged)
        if chosen is not None and year:
            r["cites_per_year"] = round(chosen / max(1, CURRENT_YEAR - year), 1)
        else:
            r["cites_per_year"] = None

        pid = key_to_id.get(r["key"], "")
        r["in_corpus_cites"]          = in_corpus.get(pid, 0)
        # leverage-weighted in-network citedness (tiered by citer engagement).
        # Stored alongside the raw count; the table DISPLAYS this and keeps the
        # raw count on hover. Falls back to the raw count if no weighted value
        # (no in-corpus citers).
        r["in_corpus_cites_weighted"] = in_corpus_weighted.get(pid, 0.0)
        n_diverged += int(diverged)
        n_chapter  += int(is_chapter)

    json.dump(cache, open(CACHE_PATH, "w"), ensure_ascii=False, indent=2)
    json.dump(mat, open(MATRIX_PATH, "w"), indent=2, ensure_ascii=False)

    # ── report ──
    SRC = {"full": 2, "abstract": 1, "title": 0}
    best = {}
    for r in mat["rows"]:
        t = r["title"].lower().strip()
        if t not in best or SRC[r["text_source"]] > SRC[best[t]["text_source"]]:
            best[t] = r
    rows = list(best.values())

    print(f"Enriched {len(mat['rows'])} rows ({len(rows)} unique papers).")
    print(f"Edges: {len(edges)} | distinct paper identities cited in-corpus: "
          f"{len(in_corpus)}")
    print(f"Cross-source: {n_diverged} rows where OpenAlex diverged high "
          f"(distrusted, conservative count used); {n_chapter} chapter-like rows "
          f"flagged unreliable structurally.")

    print("\n=== Rows where sources DISAGREED (OA distrusted) ===")
    dv = sorted([r for r in rows if r.get("cite_diverged")],
                key=lambda r: -(max((r.get("citedness_sources") or {0:0}).values())))
    for r in dv[:20]:
        s = r.get("citedness_sources", {})
        print(f"  chosen={str(r['citedness']):>4} from {r.get('citedness_source'):<8} "
              f"| sources={s} | {r['title'][:46]}")

    print("\n=== Top 15 by LEVERAGE-WEIGHTED within-corpus citedness ===")
    print("    (weighted = Σ citer-tier-multiplier; raw = distinct citers)")
    for r in sorted(rows, key=lambda r: -r.get("in_corpus_cites_weighted", 0))[:15]:
        flag = "" if r["cite_reliable"] else "  (global unreliable)"
        print(f"  wtd={r.get('in_corpus_cites_weighted',0):5.1f} raw={r['in_corpus_cites']:3d} "
              f"| global={str(r['citedness']):>5} "
              f"[{r.get('work_type','?')[:4]:4}] {r['title'][:42]}{flag}")

    rel = sum(1 for r in rows if r["cite_reliable"])
    print(f"\nReliable global counts: {rel}/{len(rows)} unique "
          f"(rest: chapters, very recent, or divergent).")


if __name__ == "__main__":
    main()

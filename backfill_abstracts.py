"""
backfill_abstracts.py  —  Recover missing abstracts for papers already in the
graph, then re-score them so the engagement matrix isn't biased against them.

WHY: relevance scoring (crawl) and depth scoring (matrix) both lean on the
abstract. A paper with no abstract is scored from its title alone, which
systematically *under*-rates it (measured: no-abstract articles average ~1.4
lower leverage and are ~12pts less likely to reach the visible tier). This
script closes part of that gap by fetching abstracts that exist in open sources
but were never captured during the crawl.

SOURCES (per paper, first real hit wins; measured hit rates on this corpus):
  1. OpenAlex        abstract_inverted_index → reconstructed text   (~38%)
  2. Crossref        message.abstract (JATS), tags stripped         (~13%, little
                                                                    unique over OA)
  3. Semantic Scholar /paper/batch (DOIs, 500/call, x-api-key)      — different
                     coverage (S2 scrapes publisher pages); run as a batch pre-
                     pass, then consulted per-paper after OA/Crossref. Needs
                     SEMANTIC_SCHOLAR_API_KEY in env (set your env vars first); the old
                     KEYLESS probe was rate-limited to ~0% and is not the same.
  Scopus (no institutional token) still not queried — add when a token lands.

HARD RULE (project convention): never fabricate text about a work. Every
abstract written here is returned by an API for *that* item (matched by its own
OA id / DOI / S2 id). Anything < MIN_ABS_CHARS is treated as a stub and ignored.

WHAT IT WRITES:
  - citation_graph.json : node["abstract"] + node["abstract_source"]="openalex"/
    "crossref" for each recovered paper. (Snapshot the graph first!)
  - abstract_backfill_keys.txt : the node keys that gained an abstract, to feed
    score_engagement.py --keys-file.
  - abstract_backfill_report.md : what was found / still missing.

RE-SCORING (not automatic — run after, see printed instructions):
  score_engagement.py reuses an existing matrix row unless it errored, so a
  backfilled row would NOT be re-scored. This script can strip the changed keys
  out of engagement_matrix.json (with --prune-matrix) so the re-score treats
  them as new. Then:
      \\
        python score_engagement.py --keys-file abstract_backfill_keys.txt
      python build_lit_table.py

Usage:
  python backfill_abstracts.py            # scope: matrix rows
  python backfill_abstracts.py --all      # whole graph
  python backfill_abstracts.py --prune-matrix                          # drop changed rows
"""

import ssl
import os as _os  # TLS verification on by default; opt out with INSECURE_TLS=1
if _os.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    ssl._create_default_https_context = ssl._create_unverified_context

import argparse, json, os, re, time
import requests, urllib3

import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
FOLDER      = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH  = os.path.join(FOLDER, "citation_graph.json")
MATRIX_PATH = os.path.join(FOLDER, "engagement_matrix.json")
KEYS_PATH   = os.path.join(FOLDER, "abstract_backfill_keys.txt")
REPORT_PATH = os.path.join(FOLDER, "abstract_backfill_report.md")
CACHE_PATH  = os.path.join(FOLDER, "abstract_backfill_cache.json")

EMAIL = os.environ.get("CROSSREF_MAILTO", "you@example.com")
MIN_ABS_CHARS = 60        # shorter than this is treated as a stub, not an abstract
S2_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()

S = requests.Session()
_VERIFY_TLS = os.environ.get("INSECURE_TLS", "") not in ("1", "true", "True")  # verify TLS unless user opts out
S.verify = _VERIFY_TLS
S.headers["User-Agent"] = f"mailto:{EMAIL}"


def has_abs(n):
    return bool((n.get("abstract") or "").strip())


def norm_doi(d):
    if not d:
        return None
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(d).strip(), flags=re.I)


def good(s):
    return bool(s and isinstance(s, str) and len(s.strip()) >= MIN_ABS_CHARS)


# ── sources ──────────────────────────────────────────────────────────────────
def openalex_abstract(oa_id, doi):
    if oa_id:
        url = f"https://api.openalex.org/works/{oa_id.replace('https://openalex.org/', '')}"
    elif doi:
        url = f"https://api.openalex.org/works/doi:{doi}"
    else:
        return ""
    try:
        r = S.get(url, params={"select": "abstract_inverted_index", "mailto": EMAIL},
                  timeout=20)
        inv = (r.json() or {}).get("abstract_inverted_index")
        if not inv:
            return ""
        pos = {}
        for w, places in inv.items():
            for p in places:
                pos[p] = w
        return " ".join(pos[i] for i in sorted(pos)).strip()
    except Exception:
        return ""


def crossref_abstract(doi):
    if not doi:
        return ""
    try:
        r = S.get(f"https://api.crossref.org/works/{doi}",
                  params={"mailto": EMAIL}, timeout=20)
        a = (r.json().get("message") or {}).get("abstract", "")
        # JATS markup: drop a leading "Abstract" label, strip tags, squeeze space
        a = re.sub(r"<[^>]+>", " ", a)
        a = re.sub(r"\s+", " ", a).strip()
        return re.sub(r"^Abstract[:\s]*", "", a, flags=re.I).strip()
    except Exception:
        return ""


def s2_batch_abstracts(dois):
    """One POST to Semantic Scholar's /paper/batch for up to 500 DOIs. Returns
    {normalized_doi: abstract}. S2 scrapes publisher pages, so its abstract
    coverage genuinely differs from OpenAlex/Crossref — especially philosophy &
    older work. The keyless probe in the original backfill was rate-limited to
    ~0%; the batch endpoint + API key is a different proposition.

    S2 needs IDs as 'DOI:<doi>'. Order of returned list mirrors the request, and
    a miss comes back as null — so we zip request ids to responses."""
    out = {}
    dois = [d for d in dois if d]
    if not dois:
        return out
    hdr = {"x-api-key": S2_KEY} if S2_KEY else {}
    for i in range(0, len(dois), 500):
        chunk = dois[i:i + 500]
        ids = [f"DOI:{d}" for d in chunk]
        try:
            r = S.post("https://api.semanticscholar.org/graph/v1/paper/batch",
                       params={"fields": "abstract,externalIds"},
                       json={"ids": ids}, headers=hdr, timeout=40)
            if r.status_code != 200:
                print(f"    [S2 batch] HTTP {r.status_code} on chunk "
                      f"{i//500 + 1}; skipping S2 for it.")
                continue
            data = r.json()
            for req_doi, item in zip(chunk, data):
                if isinstance(item, dict):
                    ab = item.get("abstract")
                    if good(ab):
                        out[req_doi] = ab.strip()
        except Exception as e:
            print(f"    [S2 batch] error on chunk {i//500 + 1}: {e}")
        time.sleep(1.0)         # be polite between chunks
    return out


# Filled once per run by main() before the per-paper loop (DOI -> abstract).
S2_LOOKUP = {}


def resolve_abstract(node):
    doi = norm_doi(node.get("doi"))
    oa = node.get("oa_id")
    a = openalex_abstract(oa, doi)
    if good(a):
        return a, "openalex"
    time.sleep(0.1)
    a = crossref_abstract(doi)
    if good(a):
        return a, "crossref"
    # Semantic Scholar (from the batch pre-pass; keyed by normalized DOI)
    if doi and doi.lower() in S2_LOOKUP:
        return S2_LOOKUP[doi.lower()], "semantic_scholar"
    return None, None


RELEVANCE_FLOOR = 3   # default scope (no matrix): nodes with crawl relevance >= this


def select_targets(nodes, mat, scope_all, scores=None):
    """Pick no-abstract nodes to backfill.
       scope_all      -> the WHOLE graph (every no-abstract node; rare/expensive).
       matrix present -> the matrix rows (the papers that will appear in the table).
       no matrix yet  -> relevance-filtered graph nodes (score >= RELEVANCE_FLOOR),
                         so this runs at the documented EARLY stage (before scoring)
                         without scanning thousands of already-rejected papers."""
    if scope_all:
        return [(k, n) for k, n in nodes.items()
                if not has_abs(n) and not n.get("is_seed")]
    if mat is not None:
        mkeys = {r["key"] for r in mat["rows"]}
        return [(k, nodes[k]) for k in mkeys
                if nodes.get(k) and not has_abs(nodes[k])]
    # No matrix yet: scope by crawl relevance (exists right after the crawl).
    scores = scores or {}
    return [(k, n) for k, n in nodes.items()
            if not has_abs(n) and not n.get("is_seed")
            and (scores.get(k) or {}).get("score", 0) >= RELEVANCE_FLOOR]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="scope = every no-abstract node (default = matrix rows only)")
    ap.add_argument("--prune-matrix", action="store_true",
                    help="drop the backfilled keys' rows from engagement_matrix.json "
                         "so score_engagement.py re-scores them (uses the keys file "
                         "from a prior run; does NOT fetch)")
    args = ap.parse_args()

    graph = json.load(open(GRAPH_PATH))
    nodes = graph["nodes"]
    scores = graph.get("scores", {})
    # The matrix is OPTIONAL: it only exists after scoring (Stage 4), but this
    # script is documented to run EARLY (Stage 2.6). If it's absent, fall back to
    # relevance-filtered graph nodes instead of crashing.
    mat = json.load(open(MATRIX_PATH)) if os.path.exists(MATRIX_PATH) else None

    # ── prune mode: remove changed rows from the matrix, then exit ──
    if args.prune_matrix:
        if mat is None:
            print(f"No {os.path.basename(MATRIX_PATH)} yet — prune mode needs the "
                  f"matrix (run score_engagement.py first).")
            return
        if not os.path.exists(KEYS_PATH):
            print(f"No {os.path.basename(KEYS_PATH)} — run the backfill first.")
            return
        changed = {ln.strip() for ln in open(KEYS_PATH) if ln.strip()}
        before = len(mat["rows"])
        mat["rows"] = [r for r in mat["rows"] if r["key"] not in changed]
        json.dump(mat, open(MATRIX_PATH, "w"), indent=2, ensure_ascii=False)
        print(f"Pruned {before - len(mat['rows'])} rows ({len(changed)} keys) from "
              f"the matrix. Now re-score:")
        print("  python score_engagement.py "
              "--keys-file abstract_backfill_keys.txt")
        return

    cache = json.load(open(CACHE_PATH)) if os.path.exists(CACHE_PATH) else {}
    targets = select_targets(nodes, mat, args.all, scores)
    if args.all:
        scope_desc = "whole graph"
    elif mat is not None:
        scope_desc = "matrix rows"
    else:
        scope_desc = f"relevance>={RELEVANCE_FLOOR} (no matrix yet)"
    print(f"No-abstract targets: {len(targets)} ({scope_desc})")

    # ── Semantic Scholar batch pre-pass ──────────────────────────────────────
    # One (chunked) POST for every uncached target DOI. resolve_abstract() then
    # consults S2_LOOKUP after OpenAlex/Crossref. S2 has different coverage, so
    # this can recover papers the other two miss. Skipped entirely with no key.
    global S2_LOOKUP
    if not S2_KEY:
        print("  [S2] no SEMANTIC_SCHOLAR_API_KEY in env — skipping S2 "
              "(run with ``).")
    else:
        # Query DOIs that S2 hasn't been tried on yet: brand-new targets AND
        # cached MISSES (the original cache predates the S2 source, so a cached
        # null just means OA+Crossref failed — S2 may still have it).
        pending = [norm_doi(n.get("doi")) for k, n in targets
                   if norm_doi(n.get("doi"))
                   and not good((cache.get(k) or {}).get("abstract"))]
        pending = sorted({d.lower() for d in pending})
        if pending:
            print(f"  [S2] batch-querying {len(pending)} uncached DOIs "
                  f"({(len(pending) - 1)//500 + 1} chunk(s))…")
            S2_LOOKUP = s2_batch_abstracts(pending)
            print(f"  [S2] returned abstracts for {len(S2_LOOKUP)} of {len(pending)}.")

    recovered, by_src, found_keys, still_missing = 0, {}, [], []
    for i, (k, n) in enumerate(targets, 1):
        doi_l = (norm_doi(n.get("doi")) or "").lower()
        cached = cache.get(k)
        if cached and good(cached.get("abstract")):
            ab, src = cached["abstract"], cached["source"]        # prior real hit
        elif doi_l and doi_l in S2_LOOKUP:
            ab, src = S2_LOOKUP[doi_l], "semantic_scholar"        # S2 fills a miss
            cache[k] = {"abstract": ab, "source": src}
        elif cached is not None:
            ab, src = None, None                                  # confirmed miss, skip refetch
        else:
            ab, src = resolve_abstract(n)                          # brand-new target
            cache[k] = {"abstract": ab, "source": src}
            time.sleep(0.2)

        if good(ab):
            n["abstract"] = ab.strip()
            n["abstract_source"] = src
            found_keys.append(k)
            by_src[src] = by_src.get(src, 0) + 1
            recovered += 1
            tag = src
        else:
            still_missing.append((k, n))
            tag = "—"
        print(f"  [{i}/{len(targets)}] {tag:<9} {(n.get('title') or '')[:60]}")

    # ── persist ──
    json.dump(cache, open(CACHE_PATH, "w"), ensure_ascii=False, indent=2)
    if recovered:
        json.dump(graph, open(GRAPH_PATH, "w"), ensure_ascii=False)
    with open(KEYS_PATH, "w") as f:
        f.write("\n".join(found_keys) + ("\n" if found_keys else ""))

    # ── report ──
    lines = [
        "# Abstract backfill report\n",
        f"Scope: {'whole graph' if args.all else 'matrix rows'}  \n",
        f"No-abstract targets: {len(targets)}  \n",
        f"Recovered: **{recovered}** "
        f"({', '.join(f'{k}={v}' for k, v in sorted(by_src.items())) or 'none'})  \n",
        f"Still missing: {len(still_missing)}  \n",
        "\nRecovered abstracts are written into `citation_graph.json` "
        "(`abstract` + `abstract_source`). Re-score the changed rows:\n\n"
        "```\npython backfill_abstracts.py --prune-matrix\n"
        "python score_engagement.py "
        "--keys-file abstract_backfill_keys.txt\n"
        "python build_lit_table.py\n```\n",
        "\n## Still missing (no open-source abstract found)\n",
        "*(title-only scoring; not in OpenAlex/Crossref. Many are older works, "
        "book chapters, or pre-abstract-era articles.)*\n\n",
    ]
    for k, n in still_missing:
        au = ", ".join((n.get("authors") or [])[:2])
        lines.append(f"- {n.get('title')} — {au} ({n.get('year')})\n")
    open(REPORT_PATH, "w", encoding="utf-8").write("".join(lines))

    print("\n" + "=" * 56)
    print(f"Recovered {recovered}/{len(targets)} "
          f"({', '.join(f'{k}={v}' for k, v in sorted(by_src.items())) or 'none'})")
    print(f"keys file : {KEYS_PATH}")
    print(f"report    : {REPORT_PATH}")
    if recovered:
        print("\nNext — prune the changed rows, then re-score + rebuild:")
        print("  python backfill_abstracts.py --prune-matrix")
        print("  python score_engagement.py "
              "--keys-file abstract_backfill_keys.txt")
        print("  python build_lit_table.py")


if __name__ == "__main__":
    main()

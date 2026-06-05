"""
expand_wave4.py
Wave-4 expansion: a curated set of IN-FIELD books/chapters surfaced by the
Wave-2 BOOK bibliographies (Rodríguez-Blanco, Zimmerman) that were ABSENT from
the citation graph (see overlooked_texts.md). Unlike expand_wave3, these seeds
are NOT yet in the graph, so we:

  1. resolve_seeds() each to at least one ID (OA/S2/DOI),
  2. create the seed node itself (hop=4) if absent,
  3. fetch_neighbors(direction="both") and merge neighbors (hop=4),
  4. score + regenerate literature_candidates.md.

Rationale (per the Wave-2 finding): book reference lists surface book/chapter
gaps the API crawl misses. Even where a book's OUTGOING refs are thin in
OA/S2, adding it as a seed pulls in its CITING works (forward edges have decent
API coverage) — and those citing works are themselves on-thesis literature.

Report-only on the graph until run; re-running is safe (skips nodes already
present, but always (re)adds the seed->neighbor edges).

Usage:  python expand_wave4.py            # full run (writes graph)
        python expand_wave4.py --dry-run  # resolve seeds only, no fetch/write
"""
import ssl
import os as _os  # TLS verification on by default; opt out with INSECURE_TLS=1
if _os.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    ssl._create_default_https_context = ssl._create_unverified_context

import json, os, time, argparse
import urllib3
import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
FOLDER     = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH = os.path.join(FOLDER, "citation_graph.json")
REPORT_PATH= os.path.join(FOLDER, "literature_candidates.md")

# Import helpers from main crawler
import importlib.util
spec = importlib.util.spec_from_file_location(
    "crawl", os.path.join(FOLDER, "crawl_citation_graph.py"))
crawl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crawl)

HOP       = 4
THRESHOLD = 3

# Curated in-field seeds (title/authors/year + any ids already resolved from
# parsed_references.json). resolve_seeds() fills gaps via S2 by doi/title.
WAVE4_SEEDS = [
    {"title": "Criminally Ignorant", "authors": ["Sarch, Alexander"], "year": 2019,
     "doi": "10.1093/oso/9780190056575.001.0001", "oa_id": "W4255965827", "type": "book"},
    {"title": "Varieties of Negligence and Complications for Moral Blameworthiness",
     "authors": ["Fitzpatrick, W"], "year": 2021,
     "doi": "10.1017/9781108628228.006", "oa_id": "W3208731674", "type": "chapter"},
    {"title": "Non-Tracing Cases of Culpable Ignorance", "authors": ["Smith, Holly M."],
     "year": 2011, "doi": "10.1007/s11572-011-9113-1", "oa_id": "W2052671436", "type": "article"},
    {"title": "Tracing the Epistemic Condition", "authors": ["King, Matt"], "year": 2017,
     "doi": "10.1093/oso/9780198779667.003.0015", "oa_id": "W4249993424", "type": "chapter"},
    {"title": "Ignorance as a Legal Excuse", "authors": ["Alexander, Larry"], "year": 2017,
     "doi": "10.2139/ssrn.2985701", "oa_id": "W2625004189", "type": "chapter"},
    {"title": "Responsibility and Fault", "authors": ["Honoré, T"], "year": 1999,
     "doi": "10.2307/3505053", "oa_id": "W4300603869",
     "s2_id": "ea7baf97ef5f9c687beaffe487a4027846323d69", "type": "book"},
    {"title": "Philosophy of Criminal Law", "authors": ["Husak, Douglas N."], "year": 1987,
     "doi": "10.1093/oxfordhb/9780195314854.001.0001", "oa_id": "W4214498967", "type": "book"},
    {"title": "Ways to be Blameworthy: Rightness, Wrongness, and Responsibility",
     "authors": ["Mason, Elinor"], "year": 2019, "oa_id": "W2790284904", "type": "book"},
    {"title": "Unwitting Wrongdoers and the Role of Moral Disagreement in Blame",
     "authors": ["Talbert, Matthew"], "year": 2013, "type": "chapter"},
    {"title": "Moral Blame and Moral Protest", "authors": ["Smith, Angela M."], "year": 2013,
     "doi": "10.1093/acprof:oso/9780199860821.003.0002", "oa_id": "W2489310742", "type": "chapter"},
    {"title": "Interpreting Blame", "authors": ["Scanlon, Thomas M."], "year": 2013,
     "doi": "10.1093/acprof:oso/9780199860821.003.0005", "oa_id": "W4256425369", "type": "chapter"},
    {"title": "The Contours of Blame", "authors": ["Coates, D. Justin", "Tognazzini, Neal A."],
     "year": 2013, "doi": "10.1093/acprof:oso/9780199860821.003.0001",
     "oa_id": "W947820824", "type": "chapter"},
    {"title": "Forms and Conditions of Responsibility", "authors": ["Scanlon, Thomas M."],
     "year": 2015, "doi": "10.1093/acprof:oso/9780199998074.003.0005",
     "oa_id": "W2501795049", "type": "chapter"},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve seeds only; no neighbor fetch, no graph write")
    args = ap.parse_args()

    with open(GRAPH_PATH) as f:
        g = json.load(f)
    nodes, scores, edges = g["nodes"], g["scores"], g["edges"]
    n0, e0 = len(nodes), len(edges)

    print(f"Graph: {n0} nodes, {e0} edges")
    print(f"\nResolving {len(WAVE4_SEEDS)} Wave-4 seeds…")
    seeds = crawl.resolve_seeds(WAVE4_SEEDS)

    if args.dry_run:
        present = sum(1 for s in seeds if crawl.node_key(s) in nodes)
        print(f"\n[dry-run] {present}/{len(seeds)} seeds already in graph; "
              f"{len(seeds)-present} would be added. No fetch performed.")
        return

    new_total = pass_total = seeds_added = 0
    for s in seeds:
        key = crawl.node_key(s)
        label = (s.get("title") or "")[:55]
        # add the seed node itself if absent
        if key not in nodes:
            s["hop"] = HOP
            nodes[key] = s
            scores[key] = {"score": THRESHOLD,
                           "reason": "Wave-4 curated in-field seed (book bibliography gap)"}
            seeds_added += 1
        print(f"\n{'='*60}\n{label}  [{key}]")

        if not (s.get("s2_id") or s.get("oa_id") or s.get("doi")):
            print("  SKIP expand (no resolvable id)")
            continue

        candidates = crawl.fetch_neighbors(s, direction="both")
        print(f"  -> {len(candidates)} neighbors fetched")

        new_this = pass_this = 0
        for cand in candidates:
            ckey = crawl.node_key(cand)
            edges.append({"from": key, "to": ckey, "hop": HOP})
            if ckey in nodes:
                continue
            passes, score, reason = crawl.score_candidate(cand, THRESHOLD, scores)
            cand["hop"] = HOP
            nodes[ckey] = cand
            scores[ckey] = {"score": score, "reason": reason}
            new_this += 1
            if passes:
                pass_this += 1
                print(f"  [score={score}] {cand.get('title','')[:65]}")
        print(f"  {new_this} new nodes, {pass_this} above threshold")
        new_total += new_this
        pass_total += pass_this
        time.sleep(0.3)

    print(f"\n{'='*60}")
    print(f"Wave-4: {seeds_added} seeds added, {new_total} new neighbor nodes "
          f"({pass_total} above threshold). Edges +{len(edges)-e0}.")

    with open(GRAPH_PATH, "w") as f:
        json.dump(g, f, indent=2, ensure_ascii=False)
    print(f"Graph saved: {len(nodes)} nodes (+{len(nodes)-n0}), {len(edges)} edges.")


if __name__ == "__main__":
    main()

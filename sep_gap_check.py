#!/usr/bin/env python3
"""sep_gap_check.py — audit corpus coverage against an authoritative external
bibliography (a SEP entry, a handbook, a key survey article).

This is the REPEATABLE form of the 2026-06-01 SEP sanity-check. It was originally
a throwaway /tmp script; making it permanent is itself one of the workflow
lessons — an external bibliography is the cheapest high-yield completeness audit
we have, and we should be able to re-run it each time the corpus changes or a new
authoritative source appears.

For each work in the chosen bibliography it resolves the BEST corpus node via
corpus_match.canonical_node() (highest crawl-relevance, reviews demoted) and
sorts the work into three buckets:

  IN MATRIX     — matched, relevance >= --matrix-min (default 4): displayed, fine.
  UNDER-SCORED  — matched but relevance < threshold: ALREADY IN THE CRAWL, just
                  scored too low to enter the matrix. These are the cheap wins —
                  a relevance bump (+ abstract backfill) pulls them in. THIS is
                  the bucket the first SEP pass got wrong by matching review/
                  fragment siblings; canonical_node() now reports the real node.
  ABSENT        — no plausible node at all: a genuine acquisition gap (seed it
                  only if on-thesis).

USAGE:
  python3 sep_gap_check.py                       # all bibliographies in the file
  python3 sep_gap_check.py --bib tort-theories   # just one
  python3 sep_gap_check.py --matrix-min 4        # relevance threshold (default 4)
  python3 sep_gap_check.py --bib-file other.json # a different bibliography file

Does NOT modify the graph. Acting on the findings (bumping relevance, seeding a
missing work) is a separate, user-gated step — see README Stage 6.5.
"""
import json, os, argparse
from corpus_match import canonical_node, CONF_CLEAN

FOLDER   = os.path.dirname(os.path.abspath(__file__))
GRAPH    = os.path.join(FOLDER, "citation_graph.json")
BIB_FILE = os.path.join(FOLDER, "sep_bibliographies.json")


def audit(works, N, scores, matrix_min):
    in_matrix, under, absent, lowconf = [], [], [], []
    for surname, year, title in works:
        m = canonical_node(title, N, scores, author_surname=surname)
        if m is None:
            absent.append((surname, year, title))
            continue
        k, node, sim, rel, conf = m
        rec = (surname, year, title, k, sim, rel, conf)
        if conf < CONF_CLEAN:
            # a match was found but it's shaky (title-form divergence, an author
            # mismatch, or a same-subfield collision) — flag for human eyes
            # rather than silently trust or silently drop it.
            lowconf.append(rec)
        elif rel >= matrix_min:
            in_matrix.append(rec)
        else:
            under.append(rec)
    return in_matrix, under, absent, lowconf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bib", default=None, help="audit only this bibliography key")
    ap.add_argument("--matrix-min", type=int, default=4,
                    help="crawl-relevance threshold for matrix inclusion (default 4)")
    ap.add_argument("--bib-file", default=BIB_FILE)
    args = ap.parse_args()

    g = json.load(open(GRAPH))
    N, scores = g["nodes"], g["scores"]
    bibs = json.load(open(args.bib_file))

    keys = [k for k in bibs if not k.startswith("_")]
    if args.bib:
        if args.bib not in keys:
            raise SystemExit(f"--bib {args.bib!r} not in {keys}")
        keys = [args.bib]

    grand_absent, grand_under, grand_low = 0, 0, 0
    for bkey in keys:
        b = bibs[bkey]
        works = b["works"]
        in_matrix, under, absent, lowconf = audit(works, N, scores, args.matrix_min)
        print(f"\n{'='*72}\n{bkey}   ({b.get('source','?')})")
        print(f"{len(works)} works  |  in-matrix {len(in_matrix)}  "
              f"under-scored {len(under)}  ABSENT {len(absent)}  "
              f"hand-sort {len(lowconf)}\n{'='*72}")

        if under:
            print(f"\n  -- UNDER-SCORED (in crawl, rel < {args.matrix_min}; "
                  f"bump candidates) --")
            for s, y, t, k, sim, rel, conf in sorted(under, key=lambda r: r[5]):
                print(f"    rel{rel} [conf{conf:.2f}] {s} {y}: {t[:50]}")
                print(f"            node: {k}")
        if lowconf:
            print(f"\n  -- LOW-CONFIDENCE (a node matched but title/author is "
                  f"shaky — HAND-SORT: confirm or treat as absent) --")
            for s, y, t, k, sim, rel, conf in sorted(lowconf, key=lambda r: r[6]):
                node_title = (N[k].get("title") or "")[:48]
                print(f"    conf{conf:.2f} rel{rel}  {s} {y}: {t[:46]}")
                print(f"            matched node {k}")
                print(f"            node title : {node_title!r}  authors={N[k].get('authors')}")
        if absent:
            print(f"\n  -- ABSENT (no node; acquisition gaps) --")
            for s, y, t in absent:
                print(f"    {s} {y}: {t}")
        print(f"\n  -- IN MATRIX (rel >= {args.matrix_min}) : {len(in_matrix)} "
              f"works, no action --")
        grand_absent += len(absent); grand_under += len(under); grand_low += len(lowconf)

    print(f"\n{'='*72}\nTOTAL across {len(keys)} bibliographies: "
          f"{grand_under} under-scored, {grand_absent} absent, "
          f"{grand_low} hand-sort.")
    print("Next: HAND-SORT the low-confidence few (confirm match or mark absent); "
          "bump under-scored on-thesis nodes' relevance (a separate, user-gated "
          "edit), backfill abstracts, re-score; seed any on-thesis ABSENT works. "
          "See README Stage 6.5.")


if __name__ == "__main__":
    main()

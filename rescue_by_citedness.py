#!/usr/bin/env python3
"""rescue_by_citedness.py — recover on-thesis works the keyword pre-filter buried.

WHY THIS EXISTS (the in-network-citedness lesson, 2026-06-01):
The keyword pre-filter in crawl_citation_graph.score_candidate() decides on TITLE
TEXT, at DISCOVERY time — the moment a candidate first appears as one paper's
reference. At that instant the node has an in-network in-degree of (at most) 1,
so the pre-filter cannot see how heavily the rest of the corpus ends up citing it.
Title-only works with no subfield keyword in the title (Smith's *Culpable
Ignorance*, Montmarquet, Fischer & Tognazzini, Murray's *Responsibility &
Vigilance*) were frozen at relevance 1 and went invisible to every downstream
stage — yet all four sit in the TOP 2% of the corpus by in-network in-degree.
The network was pointing at them the whole time.

In-network in-degree (how many in-corpus papers cite a node) is an ENDOGENOUS
relevance signal, independent of the node's own title keywords. So a pre-filter
miss with high in-degree is a strong "go pull the abstract and re-score" flag.

WHAT THIS DOES (a post-pass sweep — the right home for the signal, since the
in-degree only exists AFTER the crawl has accumulated edges):
  1. recompute in-network in-degree from g["edges"]  (count of edges TO each key)
  2. take every node the pre-filter DEFERRED (scores[k].reason == "failed keyword
     pre-filter") whose in-degree >= --min-indeg
  3. fold book-chapter / review siblings via corpus_match.canonical_node so we
     don't waste a pull re-scoring "List of Charts and Figures"
  4. backfill an abstract (OpenAlex inverted-index, then Crossref, then S2) if the
     node lacks one
  5. re-score via crawl_citation_graph.claude_score and OVERWRITE the deferred
     rel-1 verdict (clearing the `deferred` flag), recording abstract provenance

It MODIFIES citation_graph.json (scores + node abstracts). Snapshot first. The
matrix is NOT rebuilt here — re-run the matrix build afterward to surface anything
that crossed rel>=4. This is Stage 6.6 in the README.

USAGE:
  python3 rescue_by_citedness.py --dry-run            # report candidates, no calls
  python3 rescue_by_citedness.py --min-indeg 4        # default threshold = 4
  python3 rescue_by_citedness.py                      # run it (writes the graph)
"""
import json, os, re, time, argparse, collections, shutil, datetime
import crawl_citation_graph as C
from corpus_match import canonical_node, surname as node_surname
from enrich_citedness import cite_tier_multiplier, CORE_ISSUES

FOLDER  = os.path.dirname(os.path.abspath(__file__))
GRAPH   = os.path.join(FOLDER, "citation_graph.json")
MATRIX  = os.path.join(FOLDER, "engagement_matrix.json")
ISSUES  = os.path.join(FOLDER, "issues_final.json")
ARCHIVE = os.path.join(FOLDER, "_archive")


def weighted_in_degree(edges):
    """Leverage-WEIGHTED in-network in-degree, the SAME signal the matrix column
    uses (enrich_citedness.cite_tier_multiplier): each incoming edge is weighted
    by the CITING paper's engagement tier (3x starred / 2x visible / 1x rest or
    off-matrix). This is the triage score — a buried node cited by serious work
    ranks above one cited only by fringe nodes. Returns (weighted, raw) dicts
    keyed by node key (NOT dedup-identity — the rescue operates on raw nodes)."""
    # leverage of each CITING node, via its matrix row if it has one
    lev_by_key = {}
    if os.path.exists(MATRIX) and os.path.exists(ISSUES):
        mat = json.load(open(MATRIX))
        fin = json.load(open(ISSUES))
        w = {i["id"]: float(i.get("weight", 1.0)) for i in fin.get("issues", [])}
        for r in mat.get("rows", []):
            sc = r.get("scores", {}) or {}
            lev_by_key[r["key"]] = sum(w.get(i, 1.0) * int(sc.get(i, 0))
                                       for i in CORE_ISSUES)
    wtd = collections.defaultdict(float)
    raw = collections.Counter()
    for e in edges:
        f, t = e["from"], e["to"]
        raw[t] += 1
        wtd[t] += cite_tier_multiplier(lev_by_key.get(f, 0.0))
    return wtd, raw


def pull_abstract(node):
    """Try to backfill an abstract for a node, returning (abstract, source) or
    ("", None). Reuses the crawler's configured sessions and parsers; never
    fabricates — only returns text an upstream API actually served."""
    oa_id = node.get("oa_id")
    doi   = node.get("doi")
    s2_id = node.get("s2_id")
    # 1. OpenAlex by id (inverted-index reconstruction)
    if oa_id:
        try:
            url = (f"{C.OA_BASE}/works/{oa_id}"
                   f"?select=abstract_inverted_index&mailto={C.EMAIL}")
            r = C.OA.get(url, timeout=15)
            inv = (r.json() or {}).get("abstract_inverted_index")
            ab = C.oa_reconstruct_abstract(inv)
            if ab:
                return ab, "openalex"
        except Exception:
            pass
    # 2. OpenAlex by DOI
    if doi:
        try:
            url = (f"{C.OA_BASE}/works/doi:{doi}"
                   f"?select=abstract_inverted_index&mailto={C.EMAIL}")
            r = C.OA.get(url, timeout=15)
            inv = (r.json() or {}).get("abstract_inverted_index")
            ab = C.oa_reconstruct_abstract(inv)
            if ab:
                return ab, "openalex"
        except Exception:
            pass
    # 3. Semantic Scholar (by DOI then s2 id)
    p = C.s2_get_paper(doi=doi) if doi else None
    if not p and s2_id:
        p = C.s2_get_paper(s2_id=s2_id)
    if p and p.get("abstract"):
        return p["abstract"], "semantic_scholar"
    return "", None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-weighted", type=float, default=8.0,
                    help="leverage-WEIGHTED in-network in-degree threshold to "
                         "rescue (default 8.0; tiered 3/2/1 by citer engagement)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report rescue candidates without any API calls or writes")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap the number of candidates processed (testing)")
    args = ap.parse_args()

    g = json.load(open(GRAPH))
    N, scores, edges = g["nodes"], g["scores"], g["edges"]
    wdeg, indeg = weighted_in_degree(edges)   # weighted is the triage signal

    # candidates: deferred-by-pre-filter nodes with enough WEIGHTED in-degree
    deferred = [k for k in N
                if (scores.get(k) or {}).get("reason") == "failed keyword pre-filter"
                and wdeg[k] >= args.min_weighted]
    deferred.sort(key=lambda k: wdeg[k], reverse=True)

    # Fold siblings: keep only nodes that ARE their own canonical node, so we don't
    # re-score a chapter/review twin of a work whose real node is elsewhere.
    folded, seen_canon = [], set()
    for k in deferred:
        n = N[k]
        cn = canonical_node(n.get("title", ""), N, scores,
                            author_surname=node_surname(n))
        canon_key = cn[0] if cn else k
        # if the canonical node for this title is a DIFFERENT, already-good node,
        # this k is a stale twin — skip it (its work is already represented)
        if canon_key != k:
            canon_rel = (scores.get(canon_key) or {}).get("score", 0)
            if canon_rel >= 4:
                continue  # work already in matrix under its real node
            if canon_key in seen_canon:
                continue  # another twin of the same work already queued
        seen_canon.add(canon_key)
        folded.append(k)

    if args.limit:
        folded = folded[:args.limit]

    print(f"weighted in-degree >= {args.min_weighted}: {len(deferred)} deferred "
          f"candidates, {len(folded)} after folding siblings.\n")

    if args.dry_run:
        print("DRY RUN — candidates (weighted/raw in-degree, has-abstract, title):")
        for k in folded:
            n = N[k]
            au = (n.get("authors") or ["?"])[0]
            print(f"  wtd={wdeg[k]:5.1f} raw={indeg[k]:3d} "
                  f"abs={'Y' if n.get('abstract') else 'n'} "
                  f"{au[:16]:16s} {(n.get('title') or '')[:50]!r}")
        return

    # snapshot before writing
    os.makedirs(ARCHIVE, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    snap = os.path.join(ARCHIVE, f"citation_graph.backup_pre_citedness_rescue_run_{ts}.json")
    shutil.copy(GRAPH, snap)
    print(f"snapshot -> {os.path.basename(snap)}\n")

    pulled, rescored, crossed = 0, 0, []
    for i, k in enumerate(folded, 1):
        n = N[k]
        title = n.get("title", "")
        # backfill abstract if missing
        if not n.get("abstract"):
            ab, src = pull_abstract(n)
            if ab:
                n["abstract"] = ab
                n["abstract_source"] = src
                pulled += 1
            time.sleep(0.1)
        # re-score via Claude (bypasses the pre-filter — that's the whole point)
        score, reason = C.claude_score(title, n.get("abstract", ""))
        old = (scores.get(k) or {}).get("score", 1)
        scores[k] = {"score": score, "reason": reason,
                     "rescued_by": "in_network_citedness",
                     "in_network_weighted": round(wdeg[k], 1),
                     "in_network_raw": indeg[k]}
        rescored += 1
        if score >= 4 and old < 4:
            crossed.append((k, score, wdeg[k], title))
        au = (n.get("authors") or ["?"])[0]
        print(f"  [{i}/{len(folded)}] wtd={wdeg[k]:5.1f} rel{old}->rel{score}  "
              f"{au[:14]:14s} {title[:44]!r}")
        time.sleep(0.1)

    json.dump(g, open(GRAPH, "w"), indent=2, ensure_ascii=False)

    print(f"\n{'='*64}\nrescued {rescored} nodes  |  pulled {pulled} new abstracts")
    print(f"{len(crossed)} crossed into matrix scope (rel>=4):")
    for k, s, d, t in sorted(crossed, key=lambda r: -r[1]):
        print(f"   rel{s} wtd={d:.1f}  {t[:54]}")
    print("\nNext: rebuild the engagement matrix to surface the crossings, then "
          "score_engagement on any new rel>=4 nodes. See README Stage 6.6.")


if __name__ == "__main__":
    main()

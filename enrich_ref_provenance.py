"""
enrich_ref_provenance.py  —  Make the in-corpus citedness asymmetry auditable.

In-corpus citedness (enrich_citedness.py) counts edges. Edges come from two
directions, with very different coverage:
  - FORWARD  ("who cites X"): pulled from OpenAlex/S2 cited-by endpoints during
    the crawl. Good coverage; doesn't depend on parsing anyone's references.
  - BACKWARD ("what X references"): an edge exists only if X's reference list
    was available. For all but a handful of nodes that list came from the OA/S2
    API; for 5 corpus papers it came from FULL-TEXT parsing (parse_references.py).

This script tags each node with `ref_provenance`:
  "fulltext" : reference list obtained by parsing the PDF (the 5 corpus papers)
  "api"      : has outgoing reference edges supplied by OA/S2
  "none"     : no outgoing edges at all (its references never entered the graph)

So a node's in-corpus IN-degree is trustworthy regardless (forward edges), but a
node's OUT-degree / its contribution to *others'* citedness depends on this flag.
A reader can see that a 0-out node didn't fail to cite anyone — its bibliography
was simply never ingested.

Writes node["ref_provenance"] onto citation_graph.json; mirrors onto matrix rows.

Usage:  python enrich_ref_provenance.py
"""

import os, json, re

FOLDER     = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH = os.path.join(FOLDER, "citation_graph.json")
MATRIX_PATH= os.path.join(FOLDER, "engagement_matrix.json")
PARSED_PATH= os.path.join(FOLDER, "parsed_references.json")


def norm(s):
    return re.sub(r"[^a-z0-9]", " ", (s or "").lower())


# The full-text-parsed source documents, identified by EXACT node key.
# (parse_references.py ran over 6 source files; the two Crime-and-Culpability
# chapters share one book node, so this is 5 distinct nodes. Title-cue matching
# is too loose — e.g. "justification and excuse" hits several unrelated works —
# so we pin the exact keys, verified against parsed_references.json source_paper
# stems.)
FULLTEXT_KEYS = {
    "oa:W3121936627",                               # Hurd, The Innocence of Negligence
    "oa:W2484803952",                               # Alexander/Ferzan, Against Negligence Liability
    "doi:10.1093/oxfordhb/9780195314854.003.0010",  # Ferzan, Justification and Excuse (handbook ch.)
    "oa:W606236685",                                # Alexander/Ferzan, Crime and Culpability (book; both chapters)
    "doi:10.1007/s11572-019-09504-w",               # Duff, Two Models of Criminal Fault
}


def main():
    g = json.load(open(GRAPH_PATH))
    nodes, edges = g["nodes"], g["edges"]

    # nodes that supplied any outgoing (reference) edge
    has_out = {e["from"] for e in edges}

    # full-text-parsed nodes: exact keys that actually contributed out-edges
    ft_keys = {k for k in FULLTEXT_KEYS if k in nodes and k in has_out}

    from collections import Counter
    counts = Counter()
    for k, n in nodes.items():
        if k in ft_keys:
            prov = "fulltext"
        elif k in has_out:
            prov = "api"
        else:
            prov = "none"
        n["ref_provenance"] = prov
        counts[prov] += 1

    json.dump(g, open(GRAPH_PATH, "w"), indent=2, ensure_ascii=False)

    # mirror onto matrix rows
    mat = json.load(open(MATRIX_PATH))
    for r in mat["rows"]:
        r["ref_provenance"] = (nodes.get(r["key"]) or {}).get("ref_provenance", "none")
    json.dump(mat, open(MATRIX_PATH, "w"), indent=2, ensure_ascii=False)

    print("ref_provenance across all nodes:", dict(counts))
    print(f"\nFull-text-parsed nodes ({len(ft_keys)}):")
    for k in sorted(ft_keys):
        print("   •", nodes[k].get("title", "?")[:60])

    # matrix view
    mc = Counter(r["ref_provenance"] for r in mat["rows"])
    print("\nref_provenance across matrix rows:", dict(mc))
    print("\nInterpretation: 'none' nodes have IN-degree (others may cite them) "
          "but contribute 0 to anyone else's in-corpus count — their own "
          "bibliography never entered the graph. This is why a LOW in_corpus_cites "
          "is uninformative: much of the corpus could cite a paper without the "
          "edge existing, since <3% of nodes supplied outgoing references.")
    print(f"\nNodes supplying outgoing references: {len(has_out)}/{len(nodes)} "
          f"({len(has_out)*100//len(nodes)}%).")


if __name__ == "__main__":
    main()

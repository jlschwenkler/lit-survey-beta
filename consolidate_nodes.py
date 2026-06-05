#!/usr/bin/env python3
"""Consolidate fragmented duplicate nodes in citation_graph.json.

The same work routinely enters the graph under several keys (a DOI, an OpenAlex
id, an S2 id, a title-stub). dedup() in build_lit_table.py only hides this at
RENDER time and only for exact-title twins; the underlying graph still carries
the fragments, so a recovered abstract is stranded on one node while its siblings
score 0 and inflate the matrix.

This stage MERGES fragments in the graph itself, BEFORE scoring. It is
deliberately conservative — a bad merge silently corrupts the graph — so a group
is merged ONLY when ALL hold:
  • identical NORMALIZED title (lowercased, punctuation/space-stripped), AND
  • identical first-author SURNAME across every node (missing-author nodes are
    allowed to ride along ONLY if some sibling has the surname and none disagree), AND
  • years within YEAR_WINDOW of each other, AND
  • no node is a recognized REVIEW/contaminant record (Choice / Project-MUSE DOIs).

Different-author same-title works ("Negligence" by Amaya vs Smith vs Alexander),
and review records (10.5860/choice…), are left untouched by these guards.

Canonical node selection (the survivor):
  prefer (has abstract) > (richer work_type: book/chapter > article) >
  (DOI-keyed > oa: > s2: > title:) > (longest abstract). Metadata is unioned
  onto the survivor (fill any missing field from a sibling; never overwrite a
  present field except to upgrade an empty/None). Edges are repointed, scores
  folded (max relevance kept), aliased nodes removed, and an `aliases` list +
  a JSON log are written.

USAGE:
  python3 consolidate_nodes.py                 # DRY RUN — prints groups, writes nothing
  python3 consolidate_nodes.py --commit        # snapshot + merge + write graph + log
  python3 consolidate_nodes.py --year-window N # override (default 10)

After --commit, re-score the SURVIVOR keys (written to
consolidate_changed_keys.txt) with score_engagement.py --keys-file --upgrade,
then re-run the Stage-5 enrichers + enrich_links.py, then build_lit_table.py.
"""
import json, os, re, sys, time
from collections import defaultdict

GRAPH = "citation_graph.json"
LOG   = "consolidate_log.json"
CHANGED = "consolidate_changed_keys.txt"
ARCHIVE = "_archive"
YEAR_WINDOW = 10

REVIEW_DOI = re.compile(r"10\.5860/choice|10\.1353/")  # Choice + Project MUSE reviews
# A node can carry the WORK's title but actually be a REVIEW of it (a contaminant
# that must never be merged into the work). The reliable tell is the abstract
# text. e.g. Stark's "Culpable Carelessness" has a sibling whose abstract opens
# "This book review sketches the main arguments of Findlay Stark's book...".
REVIEW_TEXT = re.compile(
    r"\bthis (?:book )?review\b|\bbook review\b|\breviewed by\b|"
    r"\bis a review of\b|\breview essay\b|\bin this review\b", re.I)

def norm_title(t):
    t = re.sub(r"[^a-z0-9 ]", " ", (t or "").lower())
    return re.sub(r"\s+", " ", t).strip()

def surname(node):
    """First author's surname, normalized across the messy formats the APIs
    return: 'Gardner, John' / 'John B. Gardner' / 'J. Gardner' all -> 'gardner'.

    Rule: if the name has a comma it's 'Surname, Given' -> take the part BEFORE
    the comma. Otherwise it's 'Given ... Surname' -> take the LAST token. Then
    strip non-letters. (This is what makes Moore/Gardner/Stark variants agree
    while still keeping genuinely different authors apart.)
    """
    a = node.get("authors") or []
    if not a:
        return None
    name = a[0]
    part = name.split(",")[0] if "," in name else name.split()[-1]
    s = re.sub(r"[^a-z]", "", part.lower())
    return s or None

def key_rank(k):
    # DOI is the most authoritative key, then OpenAlex, then S2, then a title stub
    if k.startswith("doi:"):   return 3
    if k.startswith("oa:"):    return 2
    if k.startswith("s2:"):    return 1
    return 0                   # title:

WT_RANK = {"book": 3, "book-chapter": 2, "dissertation": 2, "article": 1}

def canonical(keys, N):
    def score(k):
        n = N[k]
        return (
            1 if n.get("abstract") else 0,
            WT_RANK.get(n.get("work_type"), 0),
            key_rank(k),
            len(n.get("abstract") or ""),
        )
    return max(keys, key=score)

def is_review(node):
    if node.get("is_review") is True:
        return True
    if node.get("doi") and REVIEW_DOI.search(node.get("doi") or ""):
        return True
    # text tell: the abstract describes itself as a review of the work
    if REVIEW_TEXT.search((node.get("abstract") or "")[:400]):
        return True
    return False

def find_groups(N, scores, year_window):
    """Return list of (norm_title, [keys]) groups that are safe to merge."""
    inscope = [k for k, n in N.items()
               if (scores.get(k) or {}).get("score", 0) >= 4 and (n.get("title") or "").strip()]
    byt = defaultdict(list)
    for k in inscope:
        byt[norm_title(N[k]["title"])].append(k)

    groups, skipped = [], []
    for t, ks in byt.items():
        if len(ks) < 2:
            continue
        # surname guard: all present surnames must agree
        surs = set(filter(None, (surname(N[k]) for k in ks)))
        if len(surs) != 1:
            skipped.append((t, ks, "author-surname disagreement or all-missing"))
            continue
        # review/contaminant guard: drop review nodes from the group, don't merge them in
        review_ks = [k for k in ks if is_review(N[k])]
        merge_ks = [k for k in ks if not is_review(N[k])]
        if len(merge_ks) < 2:
            skipped.append((t, ks, "only one non-review node remains"))
            continue
        # year guard
        yrs = [N[k].get("year") for k in merge_ks if N[k].get("year")]
        if yrs and (max(yrs) - min(yrs)) > year_window:
            skipped.append((t, merge_ks, f"year span {max(yrs)-min(yrs)} > {year_window}"))
            continue
        groups.append((t, merge_ks, review_ks))
    return groups, skipped

def merge_group(N, scores, edges_index, canon, aliases):
    cn = N[canon]
    for ak in aliases:
        an = N[ak]
        # union metadata: fill any field the canonical lacks
        for f, v in an.items():
            if f in ("title",):
                continue
            if v in (None, "", []) :
                continue
            cur = cn.get(f)
            if cur in (None, "", []):
                cn[f] = v
        # prefer the longest abstract among the group
        if (an.get("abstract") or "") and len(an["abstract"]) > len(cn.get("abstract") or ""):
            cn["abstract"] = an["abstract"]
            if an.get("abstract_source"):
                cn["abstract_source"] = an["abstract_source"]
    # record provenance of the merge on the survivor
    cn.setdefault("aliases", [])
    cn["aliases"] = sorted(set(cn["aliases"]) | set(aliases))

def main():
    commit = "--commit" in sys.argv
    yw = YEAR_WINDOW
    if "--year-window" in sys.argv:
        yw = int(sys.argv[sys.argv.index("--year-window") + 1])

    g = json.load(open(GRAPH))
    N, scores, edges = g["nodes"], g["scores"], g["edges"]
    groups, skipped = find_groups(N, scores, yw)

    print(f"Matrix-scope nodes: {sum(1 for k,n in N.items() if (scores.get(k) or {}).get('score',0)>=4 and (n.get('title') or '').strip())}")
    print(f"Mergeable groups: {len(groups)} | skipped (guarded): {len(skipped)}\n")

    log = {"year_window": yw, "merges": [], "skipped": []}
    survivors = []
    for t, merge_ks, review_ks in groups:
        canon = canonical(merge_ks, N)
        aliases = [k for k in merge_ks if k != canon]
        survivors.append(canon)
        entry = {
            "title": t, "canonical": canon, "aliases": aliases,
            "dropped_reviews": review_ks,
            "canonical_year": N[canon].get("year"),
            "canonical_has_abstract": bool(N[canon].get("abstract")),
            "abstract_came_from": None,
        }
        # note where the winning abstract lives (for the log)
        best = max(merge_ks, key=lambda k: len(N[k].get("abstract") or ""))
        if N[best].get("abstract"):
            entry["abstract_came_from"] = best
        log["merges"].append(entry)
        print(f"MERGE {t[:48]!r}")
        print(f"   survivor: {canon}  (yr {N[canon].get('year')}, abs={bool(N[canon].get('abstract'))})")
        for a in aliases:
            print(f"   alias   : {a}  (yr {N[a].get('year')}, abs={bool(N[a].get('abstract'))})")
        if review_ks:
            print(f"   note: {len(review_ks)} review/contaminant node(s) left in graph, NOT merged: {review_ks}")
        print()

    for t, ks, why in skipped:
        log["skipped"].append({"title": t, "keys": ks, "reason": why})

    if not commit:
        print("DRY RUN — nothing written. Re-run with --commit to apply.")
        print(f"(skipped groups logged only on --commit; {len(skipped)} were guarded out.)")
        return

    # snapshot
    os.makedirs(ARCHIVE, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")  # include time so same-day re-runs don't clobber a snapshot
    snap = os.path.join(ARCHIVE, f"citation_graph.backup_pre_consolidate_{stamp}.json")
    json.dump(g, open(snap, "w"), ensure_ascii=False, indent=2)
    print(f"snapshot -> {snap}")

    # build alias->canonical map for edge/score rewrite
    alias_to_canon = {}
    for e in log["merges"]:
        for a in e["aliases"]:
            alias_to_canon[a] = e["canonical"]

    # merge metadata
    for e in log["merges"]:
        merge_group(N, scores, None, e["canonical"], e["aliases"])

    # rewrite edges (repoint alias endpoints to canonical; drop self-loops + dedupe)
    seen = set(); new_edges = []
    for ed in edges:
        fr = alias_to_canon.get(ed["from"], ed["from"])
        to = alias_to_canon.get(ed["to"], ed["to"])
        if fr == to:
            continue
        sig = (fr, to)
        if sig in seen:
            continue
        seen.add(sig)
        new_edges.append({"from": fr, "to": to, "hop": ed.get("hop")})
    dropped_edges = len(edges) - len(new_edges)
    g["edges"] = new_edges

    # fold scores: canonical keeps max relevance across the group
    for e in log["merges"]:
        canon = e["canonical"]
        best = scores.get(canon, {}).get("score", 0)
        best_reason = scores.get(canon, {}).get("reason", "")
        for a in e["aliases"]:
            s = scores.get(a, {})
            if s.get("score", 0) > best:
                best, best_reason = s["score"], s.get("reason", "")
            scores.pop(a, None)
        scores[canon] = {"score": best, "reason": best_reason}

    # remove aliased nodes
    for a in alias_to_canon:
        N.pop(a, None)

    json.dump(g, open(GRAPH, "w"), ensure_ascii=False, indent=2)
    json.dump(log, open(LOG, "w"), ensure_ascii=False, indent=2)
    open(CHANGED, "w").write("\n".join(sorted(set(survivors))) + "\n")

    # prune now-orphaned alias rows from the existing matrix, if present. The
    # merged-away keys still have matrix rows (the scorer carries them forward
    # blindly); drop any row whose key is no longer a graph node so the table
    # doesn't render phantom duplicates. (build_lit_table also skips missing
    # nodes, but pruning keeps the matrix file honest.)
    MATRIX = "engagement_matrix.json"
    if os.path.exists(MATRIX):
        msnap = os.path.join(ARCHIVE, f"engagement_matrix.backup_pre_consolidate_{stamp}.json")
        m = json.load(open(MATRIX))
        json.dump(m, open(msnap, "w"), ensure_ascii=False, indent=2)
        before = len(m["rows"])
        m["rows"] = [r for r in m["rows"] if r["key"] in N]
        json.dump(m, open(MATRIX, "w"), ensure_ascii=False, indent=2)
        print(f"matrix snapshot -> {msnap}")
        print(f"pruned {before - len(m['rows'])} orphaned alias rows from {MATRIX}")

    print(f"\nMerged {len(log['merges'])} groups; removed {len(alias_to_canon)} alias nodes.")
    print(f"Edges: {len(edges)} -> {len(new_edges)} (dropped {dropped_edges} self/dup).")
    print(f"log -> {LOG}")
    print(f"survivor keys to re-score -> {CHANGED}")
    print("\nNEXT: "
          "python3 score_engagement.py --keys-file consolidate_changed_keys.txt --upgrade")
    print("then re-run the 5 enrichers + enrich_links.py, then build_lit_table.py")

if __name__ == "__main__":
    main()

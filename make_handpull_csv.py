"""
make_handpull_csv.py  —  Build a fill-in spreadsheet from the topically-ranked
hand-pull worklist (triage_handpull_ranked.json), organized by priority tier, with
clickable URLs, for the user to paste abstracts into as they find them.

This is the CSV round-trip pattern the project already uses (cf. the earlier
manual_abstract_worklist.csv): one row per work, an empty `abstract` column the
user fills, plus `sibling_node_keys` (|-joined) so a pasted abstract can be written
onto EVERY fragmented node of the work, not just the one the worklist named.

Columns (UTF-8-BOM so Excel shows accents + makes `url` clickable):
  tier          P5..P2 (topical pull-priority; keepers only — excludes dropped)
  pull?         empty; user marks the ones they pulled (any non-blank = "done")
  in_corpus     in-corpus in-degree (the SECOND axis; shown, not folded into tier)
  why           Claude's one-line title-fit reason (context for the decision)
  title, year, authors, venue
  doi           bare DOI if any
  url           clickable resolve link (DOI -> OA landing -> S2 record)
  sibling_node_keys   |-joined graph keys this work appears under (paste target)
  abstract      EMPTY — user pastes the real abstract here

Sibling grouping: nodes sharing a normalized (title, first-author surname) within
a ±3y window are treated as the same work (conservative — mirrors the dedup/
consolidate logic). The worklist key is always included; siblings are added so the
later ingest writes onto all of them.

NO fabrication: this only lays out what to fetch. Abstracts come from the real
source, pasted by the user. Report-only; touches no graph/matrix.

USAGE
  python3 make_handpull_csv.py            # -> handpull_fill.csv
"""
import csv, json, os, re
from collections import defaultdict

FOLDER     = os.path.dirname(os.path.abspath(__file__))
RANKED     = os.path.join(FOLDER, "triage_handpull_ranked.json")
GRAPH      = os.path.join(FOLDER, "citation_graph.json")
OUT        = os.path.join(FOLDER, "handpull_fill.csv")


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def surname(authors):
    if not authors:
        return ""
    a = authors[0]
    a = a.split(",")[0] if "," in a else a.split()[-1] if a.split() else a
    return re.sub(r"[^a-z]", "", a.lower())


def main():
    ranked = json.load(open(RANKED))["ranked"]
    graph  = json.load(open(GRAPH))
    nodes  = graph["nodes"]

    # Build a (norm_title, surname) -> [keys] index over the WHOLE graph so we can
    # attach sibling fragments to each worklist row.
    by_work = defaultdict(list)
    for k, n in nodes.items():
        nt = norm_title(n.get("title"))
        if not nt:
            continue
        by_work[(nt, surname(n.get("authors")))].append((k, n.get("year")))

    def siblings(key, n):
        nt = norm_title(n.get("title")); sn = surname(n.get("authors"))
        yr = n.get("year")
        out = {key}
        for k2, y2 in by_work.get((nt, sn), []):
            if k2 == key:
                continue
            if yr and y2 and abs(int(yr) - int(y2)) > 3:
                continue
            out.add(k2)
        return "|".join(sorted(out))

    keepers = [r for r in ranked if not r["exclude"]]
    # already sorted priority desc, in-degree desc in the json; keep that order
    rows = []
    for r in keepers:
        k = r["key"]; n = nodes.get(k) or {}
        doi = (n.get("doi") or "").strip()
        rows.append({
            "tier": f"P{r['pull_priority']}",
            "pull?": "",
            "in_corpus": r["indegree"],
            "why": r["reason"],
            "title": r["title"],
            "year": r["year"] or "",
            "authors": "; ".join(n.get("authors") or []),
            "venue": r["venue"],
            "doi": doi,
            "url": r["link"],
            "sibling_node_keys": siblings(k, n),
            "abstract": "",
        })

    cols = ["tier", "pull?", "in_corpus", "why", "title", "year", "authors",
            "venue", "doi", "url", "sibling_node_keys", "abstract"]
    # utf-8-sig = BOM, so Excel reads accents and treats url as clickable
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    from collections import Counter
    spread = Counter(r["tier"] for r in rows)
    print(f"Wrote {len(rows)} rows -> {os.path.basename(OUT)}")
    print("  tiers: " + ", ".join(f"{t}={spread[t]}" for t in ("P5","P4","P3","P2")))
    nolink = sum(1 for r in rows if not r["url"])
    multi  = sum(1 for r in rows if "|" in r["sibling_node_keys"])
    print(f"  rows with no URL: {nolink};  rows with sibling fragments: {multi}")


if __name__ == "__main__":
    main()

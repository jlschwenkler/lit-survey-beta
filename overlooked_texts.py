"""
overlooked_texts.py  —  Find texts cited by Wave-1 full-text papers that are
ABSENT from the citation graph, ranked by co-citation count.

Report-only: makes NO changes to citation_graph.json. The point is to see whether
mining full-text bibliographies surfaces important literature the OA/S2 crawl
missed. A text cited by SEVERAL Wave-1 sources but not in the graph is a strong
"overlooked" candidate; a one-off is likely noise.

Inputs:
  parsed_references.json   (records with source_paper in WAVE1_STEMS)
  citation_graph.json      (to test presence)
Output:
  overlooked_texts.md      (ranked report)

Matching a ref to the graph: by DOI, else OA id, else S2 id, else normalized
title. "Present" means it already has a node.

Usage:  python overlooked_texts.py
        python overlooked_texts.py --min-cocite 2   # only co-cited texts
"""

import os, re, json, argparse
from collections import defaultdict

FOLDER     = os.path.dirname(os.path.abspath(__file__))
READING    = os.path.join(FOLDER, "reading")   # human-facing report lives here
os.makedirs(READING, exist_ok=True)
GRAPH_PATH = os.path.join(FOLDER, "citation_graph.json")
REFS_PATH  = os.path.join(FOLDER, "parsed_references.json")
OUT_MD     = os.path.join(READING, "overlooked_texts.md")

WAVE1_STEMS = {
    "YATES blameworthiness slips",
    "SARIN one thought too few",
    "SARIN punishment in negligence multifactorial",
    "AYARS blaming for unreasonableness",
    "FINKELSTEIN responsibility for unintended consequences",
}

# Wave 2: full-length book bibliographies (the diagnostic — book reference lists
# are where the API crawl is weakest).
WAVE2_STEMS = {
    "RODRIGUEZ-BLANCO responsibility for negligence",
    "ZIMMERMAN ignorance and moral responsibility",
}

ALL_STEMS = WAVE1_STEMS | WAVE2_STEMS


def wave_of(stem):
    return "W2" if stem in WAVE2_STEMS else "W1"

# reference types that count as real literature (skip cross-refs, cases, junk)
KEEP_TYPES = {"article", "book", "chapter"}


def norm_title(s):
    s = (s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_doi(s):
    if not s:
        return None
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", s.strip().lower()) or None


def build_graph_index(nodes):
    """Sets of identifiers present in the graph, for fast membership tests."""
    dois, oas, s2s, titles = set(), set(), set(), set()
    for n in nodes.values():
        d = norm_doi(n.get("doi"))
        if d:
            dois.add(d)
        oa = (n.get("oa_id") or "").strip()
        if oa and oa != "None":
            oas.add(oa)
        s2 = (n.get("s2_id") or "").strip()
        if s2 and s2 != "None":
            s2s.add(s2)
        t = norm_title(n.get("title"))
        if len(t) > 8:
            titles.add(t)
    return dois, oas, s2s, titles


def in_graph(rec, idx):
    dois, oas, s2s, titles = idx
    d = norm_doi(rec.get("doi"))
    if d and d in dois:
        return True
    oa = (rec.get("oa_id") or "").strip()
    if oa and oa != "None" and oa in oas:
        return True
    s2 = (rec.get("s2_id") or "").strip()
    if s2 and s2 != "None" and s2 in s2s:
        return True
    t = norm_title(rec.get("title"))
    if len(t) > 8 and t in titles:
        return True
    return False


def ref_identity(rec):
    """Collapse the same cited work across sources (for co-citation counting)."""
    d = norm_doi(rec.get("doi"))
    if d:
        return "doi:" + d
    t = norm_title(rec.get("title"))
    y = str(rec.get("year") or "")
    return f"t:{t[:60]}|{y}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-cocite", type=int, default=1)
    args = ap.parse_args()

    g = json.load(open(GRAPH_PATH))
    idx = build_graph_index(g["nodes"])
    refs = json.load(open(REFS_PATH))

    wave = [r for r in refs if r.get("source_paper") in ALL_STEMS
            and r.get("type") in KEEP_TYPES
            # drop "supra/Id." back-pointers: a real citation has a title,
            # or at least a resolvable id. Skip titleless, idless cross-refs.
            and (norm_title(r.get("title"))
                 or r.get("doi") or r.get("oa_id") or r.get("s2_id"))]
    print(f"References (article/book/chapter) across all sources: {len(wave)}")

    # group absent refs by identity; track which sources cite each + best metadata
    absent = defaultdict(lambda: {"sources": set(), "waves": set(),
                                  "rec": None, "resolved": False})
    present_count = 0
    for r in wave:
        if in_graph(r, idx):
            present_count += 1
            continue
        ident = ref_identity(r)
        a = absent[ident]
        a["sources"].add(r["source_paper"])
        a["waves"].add(wave_of(r["source_paper"]))
        # prefer a record that has an ID (resolvable -> could be added later)
        has_id = bool(r.get("doi") or r.get("oa_id") or r.get("s2_id"))
        if a["rec"] is None or (has_id and not a["resolved"]):
            a["rec"] = r
            a["resolved"] = has_id

    print(f"  present in graph: {present_count}")
    print(f"  distinct ABSENT texts: {len(absent)}")

    rows = []
    for ident, a in absent.items():
        r = a["rec"]
        rows.append({
            "cocite": len(a["sources"]),
            "sources": sorted(s.split()[0] for s in a["sources"]),
            "waves": sorted(a["waves"]),
            "title": r.get("title"), "authors": r.get("authors") or [],
            "year": r.get("year"), "venue": r.get("venue"),
            "type": r.get("type"),
            "resolved": a["resolved"],
            "doi": r.get("doi"), "oa_id": r.get("oa_id"), "s2_id": r.get("s2_id"),
        })
    rows = [x for x in rows if x["cocite"] >= args.min_cocite]
    rows.sort(key=lambda x: (-x["cocite"], -(x["year"] or 0)))

    def wflag(x):
        """Compact wave tag: W1, W2, or W1+W2."""
        return "+".join(x["waves"])

    # ── report ──
    w1 = sorted(s.split()[0] for s in WAVE1_STEMS)
    w2 = sorted(s.split()[0] for s in WAVE2_STEMS)
    lines = ["# Overlooked Texts — reference mining (Waves 1 & 2)\n",
             f"**Wave 1** (articles): {', '.join(w1)}.\n"
             f"**Wave 2** (book bibliographies): {', '.join(w2)}.\n\n",
             f"Of {len(wave)} real references across these bibliographies, "
             f"**{present_count} are already in the graph** and "
             f"**{len(absent)} are absent**. Absent texts ranked by co-citation "
             "(how many sources cite them). The `W1/W2/W1+W2` tag shows which "
             "wave surfaced each — **W2-only gaps are the diagnostic payoff**: "
             "texts that book bibliographies caught but the API crawl and the "
             "article bibliographies both missed.\n",
             "\n*No graph changes made. `[id]` = resolvable to a DOI/OA/S2 id "
             "(could be added as a seed); `[—]` = unresolved.*\n"]

    # ── Diagnostic: gaps surfaced ONLY by Wave-2 books ──
    w2_only = [x for x in rows if x["waves"] == ["W2"]]
    w2_books = [x for x in w2_only if x["type"] in ("book", "chapter")]
    w2_arts  = [x for x in w2_only if x["type"] == "article"]
    lines.append(f"\n## Wave-2 (book) gaps — {len(w2_only)} texts "
                 f"({len(w2_books)} books/chapters, {len(w2_arts)} articles)\n")
    lines.append("*Cited by a mined book but absent from the graph. "
                 "Book-form gaps especially are what the API crawl misses.*\n\n")
    lines.append(f"### Books & chapters — {len(w2_books)}\n\n")
    for x in sorted(w2_books, key=lambda x: -(x["year"] or 0)):
        idflag = "id" if x["resolved"] else "—"
        au = ", ".join((x["authors"] or [])[:2])
        lines.append(f"- [{idflag}] {x['title']} — {au} ({x['year']}) · _{x['type']}_ "
                     f"· {', '.join(x['sources'])}\n")
    lines.append(f"\n### Articles — {len(w2_arts)}\n")
    lines.append("<details><summary>expand</summary>\n\n")
    for x in sorted(w2_arts, key=lambda x: -(x["year"] or 0)):
        au = ", ".join((x["authors"] or [])[:2])
        lines.append(f"- {x['title']} — {au} ({x['year']}) · {', '.join(x['sources'])}\n")
    lines.append("\n</details>\n")

    cocited = [x for x in rows if x["cocite"] >= 2]
    lines.append(f"\n## Co-cited (≥2 sources) — {len(cocited)} texts\n\n")
    if not cocited:
        lines.append("_(none — no text was cited by more than one source)_\n")
    for x in cocited:
        idflag = "id" if x["resolved"] else "—"
        au = ", ".join((x["authors"] or [])[:2])
        lines.append(f"- **[{x['cocite']}× | {idflag} | {wflag(x)}]** {x['title']} — {au} "
                     f"({x['year']}) · _{x['type']}_ · cited by {', '.join(x['sources'])}\n")

    singles = [x for x in rows if x["cocite"] == 1 and x["waves"] != ["W2"]]
    # remaining single-source W1 gaps (W2-only already shown above)
    books = [x for x in singles if x["type"] in ("book", "chapter")]
    arts  = [x for x in singles if x["type"] == "article"]
    lines.append(f"\n## Single-source (Wave-1) books & chapters — {len(books)}\n")
    lines.append("*Book-form gaps are the kind the API crawl most often misses.*\n\n")
    for x in sorted(books, key=lambda x: -(x["year"] or 0)):
        idflag = "id" if x["resolved"] else "—"
        au = ", ".join((x["authors"] or [])[:2])
        lines.append(f"- [{idflag}] {x['title']} — {au} ({x['year']}) · _{x['type']}_ "
                     f"· {x['sources'][0]}\n")
    lines.append(f"\n## Single-source (Wave-1) articles — {len(arts)}\n")
    lines.append("<details><summary>expand</summary>\n\n")
    for x in sorted(arts, key=lambda x: -(x["year"] or 0)):
        au = ", ".join((x["authors"] or [])[:2])
        lines.append(f"- {x['title']} — {au} ({x['year']})\n")
    lines.append("\n</details>\n")

    open(OUT_MD, "w", encoding="utf-8").write("".join(lines))
    print(f"\nWrote {OUT_MD}")
    print(f"  W2-only gaps: {len(w2_only)} ({len(w2_books)} books/chapters) "
          f"| co-cited (>=2): {len(cocited)} | W1 singles: {len(books)+len(arts)}")
    if w2_books:
        print("\nTop Wave-2 (book) book/chapter gaps:")
        for x in sorted(w2_books, key=lambda x: -(x["year"] or 0))[:15]:
            print(f"  {'[id]' if x['resolved'] else '[--]'} "
                  f"{(x['title'] or '')[:55]} ({x['year']}) [{','.join(x['sources'])}]")


if __name__ == "__main__":
    main()

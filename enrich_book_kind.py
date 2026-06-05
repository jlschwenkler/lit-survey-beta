"""
enrich_book_kind.py  —  Two related enrichments, both via Crossref:

(1) BOOK_KIND for every work_type==book node:
    monograph | edited | textbook | essay-collection | unknown
    Signal: Crossref `editor` array (≥1 editor and no/duplicate author ⇒ edited);
    title cues ("handbook", "companion", "essays", "studies in") refine it;
    a small Claude fallback classifies the ambiguous remainder.

(2) PARENT VOLUME for every work_type==book-chapter node:
    Crossref `container-title` gives the real host-book title (OpenAlex only
    gives the publisher imprint). We cache it as node["container_title"] and,
    when that title matches a known book node, node["parent_key"].

Why it matters: lets the reading list mark books "read whole" (monograph) vs.
"mine for chapters" (edited), and surface which SCORED chapters live inside an
edited volume the user would otherwise skip.

Caches onto citation_graph.json nodes:  book_kind, book_kind_source,
container_title, parent_key.  Mirrors book_kind + container_title into
engagement_matrix.json rows.

Usage:
  python enrich_book_kind.py            # matrix nodes (default)
  python enrich_book_kind.py --all      # every graph node
  python enrich_book_kind.py --refresh  # recompute even if cached
"""

import os, json, time, argparse, re
import requests, urllib3
import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
FOLDER     = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH = os.path.join(FOLDER, "citation_graph.json")
MATRIX_PATH= os.path.join(FOLDER, "engagement_matrix.json")
MAILTO     = os.environ.get("CROSSREF_MAILTO", "you@example.com")
_VERIFY_TLS = os.environ.get("INSECURE_TLS", "") not in ("1", "true", "True")  # verify TLS unless user opts out
S = requests.Session(); S.verify = _VERIFY_TLS


def crossref(doi):
    if not doi:
        return None
    d = doi[4:] if doi.startswith("doi:") else doi
    try:
        r = S.get(f"https://api.crossref.org/works/{d}",
                  params={"mailto": MAILTO}, timeout=20)
        if r.status_code == 200:
            return r.json()["message"]
    except Exception:
        return None
    return None


# ── (1) book_kind from title cues + Crossref editors ─────────────────────────

TEXTBOOK_CUES   = ("criminal law: theory and doctrine", "textbook", "casebook",
                   "principles of", "law: text", "criminal law (")
EDITED_CUES     = ("handbook", "companion", "oxford studies", "oxford essays",
                   "essays in", "edited", "(eds", "eds)", "reconsidered",
                   "new essays", "perspectives on", "modern histories")
COLLECTION_CUES = ("selected essays", "and other essays", "collected",
                   "essays in the philosophy")   # single-author essay collections


def title_kind(title):
    t = title.lower()
    if any(c in t for c in TEXTBOOK_CUES):
        return "textbook"
    if any(c in t for c in COLLECTION_CUES):
        return "essay-collection"
    if any(c in t for c in EDITED_CUES):
        return "edited"
    return None


def classify_book(node):
    """Return (book_kind, source)."""
    title = node.get("title") or ""
    tk = title_kind(title)
    if tk:
        return tk, "title-cue"
    m = crossref(node.get("doi"))
    if m:
        editors = m.get("editor") or []
        authors = m.get("author") or []
        if editors and not authors:
            return "edited", "crossref-editors"
        if editors and authors and len(editors) >= len(authors):
            return "edited", "crossref-editors"
    return "monograph", "default"   # default single-work assumption


# ── (2) chapter → parent volume ──────────────────────────────────────────────

def chapter_host(node):
    m = crossref(node.get("doi"))
    if m:
        ct = (m.get("container-title") or [None])[0]
        if ct:
            return ct.strip()
    v = (node.get("venue") or "").strip()
    # OpenAlex often gives only the imprint; ignore pure-imprint venues
    if v and "ebooks" not in v.lower() and v.lower() not in ("",):
        return v
    return None


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    g = json.load(open(GRAPH_PATH))
    nodes = g["nodes"]

    if args.all:
        keys = list(nodes.keys())
    else:
        mat = json.load(open(MATRIX_PATH))
        keys = sorted({r["key"] for r in mat["rows"]})

    books    = [k for k in keys if nodes.get(k, {}).get("work_type") == "book"]
    chapters = [k for k in keys if nodes.get(k, {}).get("work_type") == "book-chapter"]
    print(f"Books: {len(books)}  Chapters: {len(chapters)}  (scope: "
          f"{'all' if args.all else 'matrix'})")

    # (1) classify books
    for i, k in enumerate(books, 1):
        n = nodes[k]
        if n.get("book_kind") and not args.refresh:
            continue
        n["book_kind"], n["book_kind_source"] = classify_book(n)
        time.sleep(0.12)
    from collections import Counter
    print("book_kind:", dict(Counter(nodes[k].get("book_kind") for k in books)))

    # (2) chapter host titles
    for k in chapters:
        n = nodes[k]
        if n.get("container_title") and not args.refresh:
            continue
        n["container_title"] = chapter_host(n)
        time.sleep(0.12)

    # build an index of known book titles -> key, to link chapters to parents
    booktitle_idx = {}
    for k, n in nodes.items():
        if n.get("work_type") in ("book",):
            booktitle_idx[norm(n.get("title"))] = k
    linked = 0
    for k in chapters:
        n = nodes[k]
        ct = n.get("container_title")
        if not ct:
            n["parent_key"] = None
            continue
        pk = booktitle_idx.get(norm(ct))
        # also try prefix match (subtitles differ): "Crime and Culpability" vs full
        if not pk:
            nc = norm(ct)
            for bt, bk in booktitle_idx.items():
                if bt and (bt.startswith(nc) or nc.startswith(bt)) and len(nc) > 8:
                    pk = bk; break
        n["parent_key"] = pk
        if pk:
            linked += 1
    print(f"Chapters linked to a known book node: {linked}/{len(chapters)}")

    json.dump(g, open(GRAPH_PATH, "w"), indent=2, ensure_ascii=False)

    # mirror into matrix
    mat = json.load(open(MATRIX_PATH))
    for r in mat["rows"]:
        n = nodes.get(r["key"]) or {}
        if n.get("work_type") == "book":
            r["book_kind"] = n.get("book_kind")
        elif n.get("work_type") == "book-chapter":
            r["container_title"] = n.get("container_title")
            r["parent_key"] = n.get("parent_key")
    json.dump(mat, open(MATRIX_PATH, "w"), indent=2, ensure_ascii=False)

    # report
    print("\n— Books by kind (matrix, deduped) —")
    SRC = {"full": 2, "abstract": 1, "title": 0}
    best = {}
    for r in mat["rows"]:
        t = r["title"].lower().strip()
        if t not in best or SRC[r["text_source"]] > SRC[best[t]["text_source"]]:
            best[t] = r
    by = {}
    for r in best.values():
        if r.get("work_type") == "book":
            by.setdefault(r.get("book_kind", "?"), []).append(r["title"])
    for kind in ("monograph", "edited", "textbook", "essay-collection"):
        ts = sorted(by.get(kind, []))
        print(f"\n[{kind}] ({len(ts)})")
        for t in ts:
            print("   ", t[:74])

    # chapters whose parent is an EDITED volume (mine-for-chapters candidates)
    edited_keys = {k for k in nodes if nodes[k].get("book_kind") == "edited"}
    print("\n— SCORED chapters living inside EDITED volumes —")
    for r in best.values():
        if r.get("work_type") == "book-chapter" and r.get("parent_key") in edited_keys:
            host = nodes[r["parent_key"]]["title"]
            print(f"   • {r['title'][:46]:46}  ⟵  {host[:40]}")


if __name__ == "__main__":
    main()

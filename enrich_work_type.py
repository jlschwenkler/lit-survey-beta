"""
enrich_work_type.py  —  Tag every node with a WORK TYPE (article / book /
book-chapter / other), so books and monographs are tracked systematically
rather than guessed from titles.

Source of truth: OpenAlex `type` (falls back to `type_crossref`). We resolve via
the node's oa_id, else its DOI. Result is cached onto each graph node as
`work_type` and a heuristic-flag `work_type_source` ("openalex" | "heuristic" |
"unknown"). For nodes OpenAlex can't resolve, a DOI/venue heuristic guesses
book vs. chapter vs. article.

Writes the cached type back into:
  - citation_graph.json   (node["work_type"])         — persistent
  - engagement_matrix.json(row["work_type"])           — for reports

Re-run anytime; it only fetches nodes lacking a cached work_type unless --refresh.

Usage:
  python enrich_work_type.py              # enrich matrix nodes (default)
  python enrich_work_type.py --all        # enrich every graph node
  python enrich_work_type.py --refresh    # re-fetch even if already cached
"""

import os, json, time, argparse, re
import requests, urllib3
import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
FOLDER     = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH = os.path.join(FOLDER, "citation_graph.json")
MATRIX_PATH= os.path.join(FOLDER, "engagement_matrix.json")
PAGES_CACHE= os.path.join(FOLDER, "page_count_cache.json")
MAILTO     = os.environ.get("CROSSREF_MAILTO", "you@example.com")

# Reviews the pattern rules can't catch (ambiguous JSTOR 10.2307/ prefix etc.),
# confirmed by hand. Clarke 1997 is a review of Wallace's "Responsibility and
# the Moral Sentiments" — its DOI carries the BOOK's title + cite count.
MANUAL_REVIEW_DOIS = {"10.2307/2953793"}

_VERIFY_TLS = os.environ.get("INSECURE_TLS", "") not in ("1", "true", "True")  # verify TLS unless user opts out
S = requests.Session(); S.verify = _VERIFY_TLS

# Serially-published "Studies"-type venues that OpenAlex/Crossref catalogue as
# book-chapters (because each volume has an ISBN) but that philosophers cite
# like a journal: by venue name + volume number. We reclassify their items as
# articles, hoist the series name into `venue`, and parse the volume. Add new
# series here as they turn up (e.g. Oxford Studies in Normative Ethics, in
# Metaethics, in Political Philosophy — all the same publishing model).
SERIAL_VENUES = [
    "Oxford Studies in Agency and Responsibility",
    "Oxford Studies in Normative Ethics",
    "Oxford Studies in Metaethics",
    "Oxford Studies in Political Philosophy",
]
_SERIAL_RE = re.compile(
    r"(" + "|".join(re.escape(v) for v in SERIAL_VENUES) + r")"
    r"(?:[,\s]*(?:Volume|Vol\.?)\s*(\d+))?", re.I)


def apply_serial_venues(nodes):
    """Reclassify serial-venue book-chapters as journal-style articles."""
    changed = 0
    for n in nodes.values():
        blob = " ".join(str(n.get(f) or "") for f in ("venue", "container_title"))
        m = _SERIAL_RE.search(blob)
        if not m:
            continue
        series, vol = m.group(1), m.group(2)
        n["work_type"] = "article"
        n["work_type_source"] = "serial-venue"
        n["venue"] = series
        if vol and not n.get("volume"):
            n["volume"] = vol
        n["book_kind"] = None          # no longer a chapter in an edited book
        changed += 1
    return changed


# ── Book-review detection ─────────────────────────────────────────────────────
# Book reviews pollute the table: a review's DOI often inherits the REVIEWED
# book's title and citation count (e.g. Choice/NDPR reviews of Wallace's
# "Responsibility and the Moral Sentiments" carry the book's title and OpenAlex's
# book-level cite count). We TAG them with is_review=True (not drop) so the table
# can hide-and-grey them without ranking on the bad numbers. work_type is left
# alone. Detection is conservative: explicit "review of…" titles OR a DOI prefix
# from a known review venue. Ambiguous JSTOR (10.2307/) reviews are NOT matched
# here — they can't be told from real articles by pattern, so flag those by hand.
# STRONG cues — tag on their own:
_REVIEW_TITLE_RE = re.compile(
    r"\breview of\b|\bbook review\b|:\s*a review\b|^review[:\s]", re.I)
# ABSTRACT-text cue — a review whose TITLE is just the reviewed book's title (no
# review wording) and whose DOI is an ordinary article prefix slips past the title
# + DOI guards (e.g. Stark, "Culpable Carelessness", Crim Law & Phil 2017, whose
# abstract opens "This book review sketches the main arguments of …"). Catch it
# from the opening of the abstract. Anchored to the first ~400 chars so a passing
# mention of "a review" mid-abstract doesn't false-positive. Mirrors the text
# guard in consolidate_nodes.py.
# Tight on purpose: only UNAMBIGUOUS book-review openers. Broader cues like
# "in this review" / "review essay" false-positive on survey/literature-review
# articles (Zimmerman, Cialdini, parasomnia surveys all say "review" innocuously).
# Verified corpus-wide: this set catches the Stark review and 0 false positives.
_REVIEW_ABSTRACT_RE = re.compile(
    r"\bthis book review\b|\bin this book review\b|\bbooks? under review\b"
    r"|\breviewed by\b", re.I)
# DOI prefixes / substrings that mark a review-venue record:
#   10.5860/choice.  → Choice Reviews (ALA)
#   ndpr             → Notre Dame Philosophical Reviews
REVIEW_DOI_SIGNATURES = ["10.5860/choice.", "/choice.", "ndpr"]

# WEAK cue — a review's title is often just the reviewed book's title, so it
# ENDS in a period and carries no review wording. On its own this is far too
# loose (lots of articles end in a period), so it only counts when CORROBORATED
# by a short page count. Never tags alone.
_BARE_BOOKTITLE_RE = re.compile(r"\.\s*$")
SHORT_PAGES = 2   # ≤ this many pages corroborates "review", never triggers alone


def _parse_span(raw):
    """Page count from a 'first-last' / single-page string, or None.
    Rejects garbage like Choice's '32-5588-32-5588' (too many parts)."""
    if not raw:
        return None
    raw = str(raw).strip()
    parts = re.split(r"[-–]", raw)
    if len(parts) > 2:
        return None
    nums = [p for p in parts if re.fullmatch(r"\d{1,5}", p)]
    if len(nums) == 1:
        return 1
    if len(nums) == 2:
        n = int(nums[1]) - int(nums[0]) + 1
        return n if 1 <= n <= 2000 else None
    return None


def page_count(doi, cache):
    """Crossref + OpenAlex page count for a DOI (genuine span preferred).
    Cached. Returns the smaller clean candidate, or None."""
    if not doi:
        return None
    if doi in cache:
        return cache[doi]
    cands = []
    try:
        r = S.get("https://api.crossref.org/works/" + doi,
                  params={"mailto": MAILTO}, timeout=15)
        if r.ok:
            p = _parse_span((r.json().get("message") or {}).get("page"))
            if p is not None:
                cands.append(p)
    except Exception:
        pass
    try:
        r = S.get("https://api.openalex.org/works/doi:" + doi,
                  params={"select": "biblio", "mailto": MAILTO}, timeout=15)
        if r.ok:
            b = (r.json().get("biblio") or {})
            fp, lp = b.get("first_page"), b.get("last_page")
            if fp and lp and re.fullmatch(r"\d{1,5}", str(fp)) \
                    and re.fullmatch(r"\d{1,5}", str(lp)):
                n = int(lp) - int(fp) + 1
                if 1 <= n <= 2000:
                    cands.append(n)
    except Exception:
        pass
    val = min(cands) if cands else None
    cache[doi] = val
    time.sleep(0.12)
    return val


def detect_reviews(nodes, keys, pages_cache):
    """Tag nodes that are book reviews. Returns the count newly tagged.

    Strong cues (review wording in title, review-venue DOI, manual list) tag on
    their own. Page count is a CORROBORATOR only: a bare book-title + a short
    page span tags, but neither signal triggers alone (avoids flagging the many
    articles whose page metadata is just a start page with no last_page)."""
    tagged = 0
    for k in keys:
        n = nodes.get(k)
        if not n:
            continue
        title = n.get("title") or ""
        doi = (n.get("doi") or "").lower()
        if doi.startswith("doi:"):
            doi = doi[4:]

        abstract = n.get("abstract") or ""
        strong = bool(_REVIEW_TITLE_RE.search(title)) \
            or any(s in doi for s in REVIEW_DOI_SIGNATURES) \
            or doi in MANUAL_REVIEW_DOIS \
            or bool(_REVIEW_ABSTRACT_RE.search(abstract[:400]))
        # weak cue must be corroborated by a genuinely short page count
        corroborated = False
        if not strong and _BARE_BOOKTITLE_RE.search(title) and doi:
            pg = page_count(doi, pages_cache)
            corroborated = pg is not None and pg <= SHORT_PAGES

        is_rev = strong or corroborated
        if is_rev and not n.get("is_review"):
            n["is_review"] = True
            n["is_review_signal"] = "strong" if strong else "page-corroborated"
            tagged += 1
        elif is_rev:
            n["is_review"] = True
    return tagged


def oa_type(node):
    """Return OpenAlex 'type' for a node, or None if unresolvable."""
    oid, doi = node.get("oa_id"), node.get("doi")
    url = None
    if oid:
        url = f"https://api.openalex.org/works/{oid}"
    elif doi:
        d = doi[4:] if doi.startswith("doi:") else doi
        url = f"https://api.openalex.org/works/doi:{d}"
    if not url:
        return None
    try:
        r = S.get(url, params={"mailto": MAILTO}, timeout=20)
        if r.status_code == 200:
            j = r.json()
            return j.get("type") or j.get("type_crossref")
    except Exception:
        return None
    return None


def heuristic_type(node):
    """Guess work type from DOI/venue shape when OpenAlex can't resolve."""
    doi   = (node.get("doi") or "").lower()
    venue = (node.get("venue") or "").lower()
    # OUP/CUP monograph DOIs: '.001.0001' book-level suffix
    if re.search(r"\.001\.0001$", doi):
        return "book"
    # acprof/oso chapter DOIs with a .003. segment are chapters
    if "acprof:oso" in doi or "/oso/" in doi:
        return "book-chapter" if ".003." in doi else "book"
    if any(w in venue for w in ("handbook", "companion", "(ed", "eds)", "edited")):
        return "book-chapter"
    return None


def normalize(t):
    if not t:
        return None
    t = t.lower()
    if t in ("book", "monograph"):
        return "book"
    if t in ("book-chapter", "book-part", "reference-entry"):
        return "book-chapter"
    if t in ("article", "journal-article", "posted-content", "preprint"):
        return "article"
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="enrich every graph node, not just matrix nodes")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch even nodes that already have a cached type")
    args = ap.parse_args()

    g = json.load(open(GRAPH_PATH))
    nodes = g["nodes"]

    if args.all:
        target_keys = list(nodes.keys())
    else:
        mat = json.load(open(MATRIX_PATH))
        target_keys = sorted({r["key"] for r in mat["rows"]})

    print(f"Enriching work_type for {len(target_keys)} nodes "
          f"({'all graph' if args.all else 'matrix'} scope).")

    fetched = cached = heur = unknown = 0
    for i, k in enumerate(target_keys, 1):
        n = nodes.get(k)
        if not n:
            continue
        if n.get("work_type") and not args.refresh:
            cached += 1
            continue
        t = normalize(oa_type(n))
        if t:
            n["work_type"], n["work_type_source"] = t, "openalex"
            fetched += 1
        else:
            h = heuristic_type(n)
            if h:
                n["work_type"], n["work_type_source"] = h, "heuristic"
                heur += 1
            else:
                n["work_type"], n["work_type_source"] = "article", "unknown"
                unknown += 1
        if i % 25 == 0:
            print(f"  {i}/{len(target_keys)} …")
        time.sleep(0.1)

    # Post-pass: serial "Studies" venues are journals, not edited books.
    serial = apply_serial_venues(nodes)

    # Post-pass: tag book reviews so the table can hide/grey them (is_review).
    # Page count is a corroborator only (see detect_reviews); cache it.
    pages_cache = json.load(open(PAGES_CACHE)) if os.path.exists(PAGES_CACHE) else {}
    reviews = detect_reviews(nodes, target_keys, pages_cache)
    json.dump(pages_cache, open(PAGES_CACHE, "w"), ensure_ascii=False, indent=2)

    json.dump(g, open(GRAPH_PATH, "w"), indent=2, ensure_ascii=False)
    print(f"OpenAlex: {fetched}, cached: {cached}, heuristic: {heur}, "
          f"defaulted-to-article: {unknown}, serial-venue reclassified: {serial}, "
          f"book reviews tagged: {reviews}")

    # Write work_type back into the matrix rows.
    if os.path.exists(MATRIX_PATH):
        mat = json.load(open(MATRIX_PATH))
        for r in mat["rows"]:
            n = nodes.get(r["key"]) or {}
            r["work_type"] = n.get("work_type", "article")
            r["is_review"] = bool(n.get("is_review"))
            # keep serial-venue reclassification (venue/volume) in sync
            if n.get("work_type_source") == "serial-venue":
                r["venue"] = n.get("venue")
                if n.get("volume"):
                    r["volume"] = n.get("volume")
                r["book_kind"] = None
        json.dump(mat, open(MATRIX_PATH, "w"), indent=2, ensure_ascii=False)
        # quick summary of books/chapters in the matrix
        from collections import Counter
        c = Counter(r["work_type"] for r in mat["rows"])
        print("Matrix work types:", dict(c))
        books = sorted({r["title"] for r in mat["rows"]
                        if r["work_type"] in ("book", "book-chapter")})
        print(f"\n{len(books)} distinct book/chapter titles in matrix:")
        for t in books:
            print("  •", t[:78])


if __name__ == "__main__":
    main()

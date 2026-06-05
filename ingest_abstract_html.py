"""
ingest_abstract_html.py  —  Harvest abstracts from SAVED HTML record pages.

WHY this exists: 54 matrix papers have no open-source abstract (older works, law
reviews, books — see abstract_worklist.tsv). The PhilPapers API does NOT return
abstracts even when it works, and OpenAlex/Crossref were already exhausted in the
2026-05-31 backfill. So the human runs targeted BOOLEAN searches on PhilPapers
(see abstract_worklist.tsv header for the cluster queries), SAVES each RESULTS-
LISTING page as .html into ./abstract_html/, and this script scrapes those local
files. NOTHING is fetched live here — zero hallucination risk, every abstract is
lifted verbatim from a page the user actually saved.

TWO page shapes are handled:
  (A) PhilPapers results-listing  — PREFERRED. One saved page = MANY entries
      (<ol class='entryList'> with <li class='entry'> rows). Each row carries the
      title, author, year, an inline <div class="abstract">…</div>, and a DOI
      embedded in a go.pl?…u=<urlencoded dx.doi.org/…> redirect link. ~20 abstracts
      per saved page in practice. parse_philpapers_listing() splits these out.
  (B) Single record/article page — fallback for HeinOnline/publisher pages that
      aren't PhilPapers listings (JSON-LD → citation_abstract/dc.description meta →
      Springer/generic abstract block). extract_abstract()/find_doi()/find_title().

Matching: every extracted entry is matched to a worklist row by DOI first (most
reliable), else by fuzzy title overlap (≥0.72). Unmatched/low-confidence entries
are reported, never force-written. Already-abstracted and review rows are skipped.

Writes (only on --commit):
  citation_graph.json   node["abstract"], node["abstract_source"]="html_scrape",
                        node["abstract_html_file"]=<filename>
  engagement_matrix.json row["text_source"] left ALONE here — re-score separately so
                        the depth scores reflect the new text deliberately.

Usage:
  python3 ingest_abstract_html.py            # dry run: report matches + extracted text
  python3 ingest_abstract_html.py --commit   # write abstracts into the graph
"""

import os, re, json, html, argparse, glob
from difflib import SequenceMatcher

FOLDER     = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH = os.path.join(FOLDER, "citation_graph.json")
MATRIX_PATH= os.path.join(FOLDER, "engagement_matrix.json")
HTML_DIR   = os.path.join(FOLDER, "abstract_html")

MIN_ABSTRACT_CHARS = 80     # shorter than this is probably a stray snippet
TITLE_MATCH_MIN    = 0.72   # fuzzy-title confidence floor when no DOI match


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def strip_tags(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def find_doi(raw):
    """Pull a DOI from the saved page's own metadata (meta tags / JSON-LD / links)."""
    for pat in (
        r'name=["\'](?:citation_doi|dc\.identifier|prism\.doi)["\']\s+content=["\']([^"\']+)["\']',
        r'content=["\']([^"\']+)["\']\s+name=["\'](?:citation_doi|dc\.identifier)["\']',
        r'"doi"\s*:\s*"([^"]+)"',
        r'doi\.org/(10\.\d{4,9}/[^\s"\'<>]+)',
    ):
        m = re.search(pat, raw, re.I)
        if m:
            d = m.group(1).strip()
            d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.I)
            d = re.sub(r"^doi:", "", d, flags=re.I)
            if d.lower().startswith("10."):
                return d.lower()
    return None


def find_title(raw):
    for pat in (
        r'name=["\']citation_title["\']\s+content=["\']([^"\']+)["\']',
        r'property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
        r"(?is)<title>(.*?)</title>",
    ):
        m = re.search(pat, raw, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    return None


def extract_abstract(raw):
    """Return (abstract_text, method) or (None, None)."""
    # 1. JSON-LD
    for m in re.finditer(r'(?is)<script[^>]+ld\+json[^>]*>(.*?)</script>', raw):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if isinstance(obj, dict):
                ab = obj.get("abstract") or obj.get("description")
                if isinstance(ab, str) and len(ab.strip()) >= MIN_ABSTRACT_CHARS:
                    return strip_tags(ab), "json-ld"

    # 2. meta tags (citation_abstract is the gold one; description is last resort)
    for name in ("citation_abstract", "dc.description", "dcterms.abstract",
                 "description", "og:description"):
        m = re.search(
            rf'(?is)<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]+content=["\'](.*?)["\']\s*/?>',
            raw)
        if not m:
            m = re.search(
                rf'(?is)<meta[^>]+content=["\'](.*?)["\'][^>]+(?:name|property)=["\']{re.escape(name)}["\']',
                raw)
        if m:
            txt = strip_tags(m.group(1))
            if len(txt) >= MIN_ABSTRACT_CHARS:
                return txt, f"meta:{name}"

    # 3. PhilPapers record page
    m = re.search(r'(?is)<div[^>]+class=["\'][^"\']*abstract[^"\']*["\'][^>]*>(.*?)</div>', raw)
    if m:
        txt = strip_tags(m.group(1))
        if len(txt) >= MIN_ABSTRACT_CHARS:
            return txt, "philpapers.div.abstract"

    # 4. Springer
    m = re.search(r'(?is)<section[^>]+id=["\']Abs1[^"\']*["\'][^>]*>(.*?)</section>', raw)
    if m:
        txt = strip_tags(m.group(1))
        # drop a leading "Abstract" heading
        txt = re.sub(r"^abstract[\s:]*", "", txt, flags=re.I)
        if len(txt) >= MIN_ABSTRACT_CHARS:
            return txt, "springer.Abs1"

    # 5. generic: a heading literally "Abstract" followed by text
    m = re.search(r'(?is)>\s*abstract\s*<[^>]*>(?:</[^>]+>)?\s*(.*?)(?:<h\d|<section|<div\s+class=["\'][^"\']*(?:keyword|footer|reference))',
                  raw)
    if m:
        txt = strip_tags(m.group(1))
        if len(txt) >= MIN_ABSTRACT_CHARS:
            return txt, "generic.AbstractHeading"

    return None, None


def parse_philpapers_listing(raw):
    """Split a PhilPapers results-listing page into per-entry dicts.

    Returns a list of {title, author, year, doi, abstract} (abstract/doi may be
    None). Returns [] if the page isn't a PhilPapers listing. Each <li class=
    'entry'> row holds: <span class='articleTitle recTitle'>TITLE</span>,
    <span class='name'>AUTHOR</span>, <span class="pubYear">YEAR</span>, an
    optional <div class="abstract">…</div>, and an optional DOI inside a
    go.pl?…u=<urlencoded dx.doi.org/DOI> redirect link."""
    if "entryList" not in raw and "class='entry'" not in raw:
        return []
    from urllib.parse import unquote
    entries = []
    # split on the entry boundary; the marker is class='entry'
    chunks = re.split(r"class=['\"]entry['\"]", raw)[1:]
    for ch in chunks:
        m = re.search(r"class=['\"]articleTitle recTitle['\"]\s*>(.*?)</span>", ch, re.S)
        if not m:
            continue
        title = strip_tags(m.group(1))
        ma = re.search(r"class=['\"]name['\"]\s*>(.*?)</span>", ch, re.S)
        author = strip_tags(ma.group(1)) if ma else None
        my = re.search(r"class=['\"]pubYear['\"]\s*>(\d{4})", ch)
        year = int(my.group(1)) if my else None
        # DOI from the go.pl redirect (url-encoded), else any bare doi.org link
        doi = None
        md = re.search(r"u=([^\"'&]+)", ch)
        if md:
            dec = unquote(md.group(1))
            dm = re.search(r"(10\.\d{4,9}/[^\s\"'<>&]+)", dec)
            if dm:
                doi = dm.group(1).lower()
        if not doi:
            dm = re.search(r"doi\.org/(10\.\d{4,9}/[^\s\"'<>&]+)", ch, re.I)
            if dm:
                doi = dm.group(1).lower()
        # abstract: <div class="abstract">…</div> (greedy-safe to the next </div>)
        mab = re.search(r"class=['\"]abstract['\"]\s*>(.*?)</div>", ch, re.S)
        abstract = None
        if mab:
            txt = strip_tags(mab.group(1))
            if len(txt) >= MIN_ABSTRACT_CHARS:
                abstract = txt
        entries.append({"title": title, "author": author, "year": year,
                        "doi": doi, "abstract": abstract})
    return entries


def parse_heinonline(raw):
    """Split a HeinOnline 'Law Journal Library' SEARCH-RESULTS page into per-entry
    dicts {title, author, year, doi, abstract}. Returns [] if not a HeinOnline page.

    Each result carries a COinS <span class="Z3988" title="…rft.atitle=…&rft.au=…">
    block (reliable title + authors), and an AI-generated summary lives in
    <span data-id="hein.journals/…" class=" d-none full_summary_text">…</span>.
    The summary's <p class="summary_sub_headers"> labels (Central Thesis,
    Legal/Academic Issues, Methodologies, Findings, Recommendations) are kept as
    readable section headers. HeinOnline has NO author abstract — this is a
    vendor AI summary, so the caller tags abstract_source='hein_ai_summary'.

    doi is None (HeinOnline scans carry no DOI); matching is by title+author."""
    if "full_summary_text" not in raw or "Z3988" not in raw:
        return []
    from urllib.parse import unquote, parse_qs
    entries = []
    # Pair each COinS title block with the summary sharing the same data-id.
    # We split the page on the result-row container so a summary stays with its title.
    # The data-id appears in both the row's COinS handle and the summary span; we
    # key on the handle (hein.journals/<journal>.<div>) to join them.
    # 1) collect summaries by data-id
    summaries = {}
    for m in re.finditer(
            r'data-id="(hein\.journals/[^"]+)"\s+class="[^"]*full_summary_text"\s*>(.*?)</span>',
            raw, re.S):
        did = m.group(1)
        body = m.group(1 + 1)
        txt = _clean_hein_summary(body)
        if txt and len(txt) >= MIN_ABSTRACT_CHARS:
            summaries[did] = txt
    if not summaries:
        return []
    # 2) collect COinS metadata, keyed by the same handle.div
    for m in re.finditer(r'class="Z3988"\s+title="([^"]+)"', raw):
        title_attr = html.unescape(m.group(1))
        q = parse_qs(title_attr.replace("&amp;", "&"))
        atitle = (q.get("rft.atitle") or [None])[0]
        if not atitle:
            continue
        aus = q.get("rft.au") or []
        author = "; ".join(aus) if aus else None
        year = None
        for k in ("rft.date",):
            if q.get(k):
                ym = re.search(r"(\d{4})", q[k][0])
                if ym:
                    year = int(ym.group(1))
        # derive the handle.div key from rft_id (…handle=hein.journals/clqv92…div=42…)
        rid = (q.get("rft_id") or [""])[0]
        rid = unquote(rid)
        hm = re.search(r"handle=(hein\.journals/[^&]+)", rid)
        dm = re.search(r"[?&]div=(\d+)", rid)
        did = f"{hm.group(1)}.{dm.group(1)}" if (hm and dm) else None
        ab = summaries.get(did)
        if not ab:
            continue
        entries.append({"title": atitle.strip(), "author": author,
                        "year": year, "doi": None, "abstract": ab})
    return entries


def _clean_hein_summary(body):
    """Turn a HeinOnline full_summary_text HTML body into readable plain text,
    preserving the section sub-headers (Central Thesis: etc.) as labels."""
    # mark sub-header <p> blocks so they survive tag-stripping as headers
    body = re.sub(
        r"<p[^>]*class=['\"]summary_sub_headers['\"][^>]*>\s*<strong>(.*?)</strong>\s*</p>",
        r"\n\n[[[\1]]]\n", body, flags=re.S | re.I)
    # bullets / paragraph breaks
    body = re.sub(r"</p>", "\n", body, flags=re.I)
    txt = strip_tags(body)
    txt = txt.replace("[[[", "").replace("]]]", "")
    # collapse runaway whitespace but keep paragraph/bullet line breaks
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def _fix_hein_ocr(s):
    """HeinOnline's PDF export mangles ligatures: 'fi'->'b', 'ffi'->'k', etc.
    (e.g. 'specibc'=specific, 'bnancial'=financial, 'akrmative'=affirmative,
    'debnition'=definition, 'fawed'=flawed). These are systematic. We only fix
    the highest-confidence, unambiguous cases to avoid corrupting real words."""
    fixes = {
        "specibc": "specific", "bnancial": "financial", "debn": "defin",
        "akrm": "affirm", "sukce": "suffice", "fawed": "flawed",
        "infuence": "influence", "benebt": "benefit", "frst": "first",
        "brst": "first", "artibcial": "artificial", "refect": "reflect",
        "signibcant": "significant", "deb ": "defi", "conf ict": "conflict",
    }
    for bad, good in fixes.items():
        s = s.replace(bad, good)
    return s


def parse_heinonline_pdf(path):
    """Parse a HeinOnline 'Law Journal Library' results page SAVED AS PDF.

    The PDF (unlike Save-As-HTML) flattens HeinOnline's lazy-loaded list into one
    document with ALL results, their DOIs, and the AI summaries inline — the HTML
    save only captures the first ~10 rows. Returns [{title, author, year, doi,
    abstract}] where abstract is the '✨ AI Summary … Read More' block (None if a
    given entry has no summary). Matching is by DOI (clean in the PDF) first, else
    title+author. Caller tags abstract_source='hein_ai_summary'.

    Entry structure (per pdfplumber line extraction):
      N. <title> [article|comments|notes]        (numbered marker = entry boundary)
      <journal citation line(s)>
      <Surname, F.> (Cited NN times)
      DOI: 10.x/...                               (optional)
      <volume citation> / PathFinder Subjects: ...
      ✨ AI Summary
      <summary paragraph…> Read More              (optional)
    """
    try:
        import pdfplumber
    except ImportError:
        return []
    with pdfplumber.open(path) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    if "Law Journal Library" not in text and "AI Summary" not in text:
        return []
    text = _fix_hein_ocr(text)
    lines = text.split("\n")

    # Split into entries on a numbered marker at line start: "N." possibly with a
    # title following on the same line. Tolerate the first entry whose number sits
    # on its own line (e.g. "1." then title on the next).
    ENTRY_RE = re.compile(r"^\s*(\d{1,3})\.\s*(.*)$")
    entries_raw, cur = [], None
    for ln in lines:
        m = ENTRY_RE.match(ln)
        # a real entry marker is a SMALL number followed by titleish text OR empty
        if m and int(m.group(1)) <= 400:
            # guard against in-text "1." footnotes: only treat as a new entry when
            # the remainder looks like a title (has letters) or is blank (number-only line)
            rest = m.group(2).strip()
            if rest == "" or re.search(r"[A-Za-z]", rest):
                if cur is not None:
                    entries_raw.append(cur)
                cur = [rest] if rest else []
                continue
        if cur is not None:
            cur.append(ln)
    if cur is not None:
        entries_raw.append(cur)

    out = []
    TITLE_TAG = re.compile(r"\[(article|comments|notes|chapter|review)\]")
    for blk in entries_raw:
        block = "\n".join(blk)
        # ---- title: text up to and including the [type] tag (may span lines) ----
        mt = TITLE_TAG.search(block)
        if not mt:
            continue
        title = block[:mt.start()].strip()
        # collapse wrapped title lines + drop trailing UI glyphs
        title = re.sub(r"\s+", " ", title)
        title = re.sub(r"[✉🔖👍🔍✨⎙✏�]", "", title).strip()
        if not title:
            continue
        # ---- author: first "Surname, ... (Cited" or just before DOI/citation ----
        author = None
        ma = re.search(r"^([A-Z][^\n]*?)\s*\(Cited\s+[\d,]+\s+times?\)", block, re.M)
        if ma:
            author = re.sub(r"\s+", " ", ma.group(1)).strip()
        # ---- year: from the "(YYYY)" in the reporter citation ----
        year = None
        my = re.search(r"\((\d{4})(?:-\d{2,4})?\)", block)
        if my:
            year = int(my.group(1))
        # ---- DOI ----
        doi = None
        md = re.search(r"DOI:\s*(?:https?://\S*?)?(10\.\d{4,9}/[^\s]+)", block)
        if md:
            doi = md.group(1).rstrip(".,;").lower()
        # ---- summary: between "AI Summary" and "Read More" ----
        abstract = None
        msum = re.search(r"AI Summary\s*(.*?)\s*Read More", block, re.S)
        if msum:
            ab = re.sub(r"\s+", " ", msum.group(1)).strip()
            if len(ab) >= MIN_ABSTRACT_CHARS:
                abstract = ab
        out.append({"title": title, "author": author, "year": year,
                    "doi": doi, "abstract": abstract})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="write extracted abstracts into the graph (default: dry run)")
    ap.add_argument("--targets-json", default=None,
                    help="JSON list of {key,title,doi,authors} to match against "
                         "(graph nodes, not just matrix rows). Use for admitting "
                         "buried/deferred nodes that aren't in the matrix yet. "
                         "Default: the no-abstract matrix rows.")
    args = ap.parse_args()

    g = json.load(open(GRAPH_PATH))
    nodes = g["nodes"]
    mat = json.load(open(MATRIX_PATH))

    # Source of targets: either an explicit JSON list of graph nodes (--targets-json,
    # for nodes not yet in the matrix) or the no-abstract matrix rows (default).
    if args.targets_json:
        src_rows = json.load(open(args.targets_json))
        print(f"Targets from {os.path.basename(args.targets_json)}: {len(src_rows)} node(s).")
    else:
        src_rows = mat["rows"]

    # Build lookup of the target rows: by DOI and by normalized title.
    by_doi, by_title = {}, {}
    targets = []
    for r in src_rows:
        n = nodes.get(r["key"], {}) or {}
        # skip rows that already have an abstract on their graph node, or are reviews
        if n.get("abstract"):
            continue
        if not args.targets_json and r.get("is_review"):
            continue
        doi = r["key"][4:] if r["key"].startswith("doi:") else (n.get("doi") or r.get("doi") or "")
        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I).lower()
        doi = re.sub(r"^doi:", "", doi)
        nt = norm_title(r.get("title") or n.get("title") or "")
        if nt in by_title:        # dedup by title, matching the worklist count
            continue
        # author surnames for the agreement guard on fuzzy title matches
        auths = r.get("authors") or n.get("authors") or []
        surnames = {a.split(",")[0].strip().split()[-1].lower()
                    for a in auths if a and a.split()} if auths else set()
        rec = {"key": r["key"], "title": r.get("title") or n.get("title") or "",
               "doi": doi, "surnames": surnames}
        targets.append(rec)
        if doi:
            by_doi[doi] = rec
        by_title[nt] = rec

    files = sorted(glob.glob(os.path.join(HTML_DIR, "*.htm*"))
                   + glob.glob(os.path.join(HTML_DIR, "*.pdf")))
    if not files:
        print(f"No .html/.pdf files in {HTML_DIR}/ — save your search-result pages there first.")
        print(f"({len(targets)} papers still need abstracts; see abstract_worklist.tsv)")
        return

    def author_ok(rec, author):
        """True if the entry's author surname agrees with the target row's
        (guards fuzzy/exact-title matches against misattribution). If we have no
        author string for the entry, we can't confirm → treat as not-ok for
        non-DOI matches (caller decides)."""
        if not rec.get("surnames"):
            return True              # worklist row has no authors to check against
        if not author:
            return False
        a = author.lower()
        return any(sn in a for sn in rec["surnames"] if sn)

    def match_row(doi, title, author=None):
        """Match an extracted entry to a target row. Returns (rec, how) or (None,None).
        DOI match is trusted outright. Title matches (exact OR fuzzy) additionally
        require author-surname agreement — a title alone misattributes (e.g.
        'Culpability for Moral Ignorance' ~0.82 'Culpability and Ignorance')."""
        doi = (doi or "").lower()
        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi).replace("doi:", "")
        if doi and doi in by_doi:
            return by_doi[doi], f"DOI {doi}"
        if title:
            nt = norm_title(title)
            if nt in by_title and author_ok(by_title[nt], author):
                return by_title[nt], "title(exact)+author"
            best, score = None, 0.0
            for t, r in by_title.items():
                s = SequenceMatcher(None, nt, t).ratio()
                if s > score:
                    best, score = r, s
            if best and score >= TITLE_MATCH_MIN and author_ok(best, author):
                return best, f"title~{score:.2f}+author"
        return None, None

    print(f"{len(files)} saved page(s) | {len(targets)} papers awaiting an abstract.\n")
    matched = wrote = 0
    # collect (rec, abstract, fn, how) to write; one row may be hit once
    to_write = {}
    seen_files_summary = []
    for fp in files:
        fn = os.path.basename(fp)

        # (A3) HeinOnline results page SAVED AS PDF — flattens the full lazy-loaded
        # list (all rows + DOIs + AI summaries) that Save-As-HTML truncates to ~10.
        if fp.lower().endswith(".pdf"):
            pents = parse_heinonline_pdf(fp)
            page_hits = page_unmatched = page_nosum = 0
            print(f"• {fn}  [HeinOnline PDF: {len(pents)} entries]")
            for e in pents:
                if not e["abstract"]:
                    page_nosum += 1
                    continue
                rec, how = match_row(e["doi"], e["title"], e["author"])
                if not rec:
                    page_unmatched += 1
                    continue
                if rec["key"] not in to_write:
                    to_write[rec["key"]] = (rec, e["abstract"], fn, how, "hein_ai_summary")
                    matched += 1
                    page_hits += 1
                    print(f"    ✓ {rec['title'][:50]!r} via {how} "
                          f"(AI summary, {len(e['abstract'])} chars)")
            print(f"    → {page_hits} matched · {page_unmatched} summary-but-not-on-worklist "
                  f"· {page_nosum} no-summary\n")
            seen_files_summary.append((fn, page_hits))
            continue

        raw = open(fp, encoding="utf-8", errors="replace").read()

        listing = parse_philpapers_listing(raw)
        if listing:
            # (A) PhilPapers results-listing — many entries per page
            page_hits = page_absent = page_unmatched = 0
            print(f"• {fn}  [PhilPapers listing: {len(listing)} entries]")
            for e in listing:
                if not e["abstract"]:
                    page_absent += 1
                    continue
                rec, how = match_row(e["doi"], e["title"], e["author"])
                if not rec:
                    page_unmatched += 1
                    continue
                if rec["key"] not in to_write:   # first match wins
                    to_write[rec["key"]] = (rec, e["abstract"], fn, how, "html_scrape")
                    matched += 1
                    page_hits += 1
                    print(f"    ✓ {rec['title'][:50]!r} via {how} "
                          f"({len(e['abstract'])} chars)")
            print(f"    → {page_hits} matched · {page_unmatched} abstract-but-not-on-worklist "
                  f"· {page_absent} no-abstract\n")
            seen_files_summary.append((fn, page_hits))
            continue

        hein = parse_heinonline(raw)
        if hein:
            # (A2) HeinOnline search-results — AI-generated summaries (tagged distinctly)
            page_hits = page_unmatched = 0
            print(f"• {fn}  [HeinOnline AI summaries: {len(hein)} entries]")
            for e in hein:
                rec, how = match_row(e["doi"], e["title"], e["author"])
                if not rec:
                    page_unmatched += 1
                    continue
                if rec["key"] not in to_write:
                    to_write[rec["key"]] = (rec, e["abstract"], fn, how, "hein_ai_summary")
                    matched += 1
                    page_hits += 1
                    print(f"    ✓ {rec['title'][:50]!r} via {how} "
                          f"(AI summary, {len(e['abstract'])} chars)")
            print(f"    → {page_hits} matched · {page_unmatched} not-on-worklist\n")
            seen_files_summary.append((fn, page_hits))
            continue

        # (B) single record/article page fallback
        ab, method = extract_abstract(raw)
        pa = re.search(r'(?is)<meta[^>]+name=["\']citation_author["\'][^>]+content=["\'](.*?)["\']', raw)
        page_author = html.unescape(pa.group(1)) if pa else None
        rec, how = match_row(find_doi(raw), find_title(raw), page_author)
        print(f"• {fn}  [single page]")
        if not ab:
            print("    NO ABSTRACT FOUND in page\n")
        elif not rec:
            print(f"    abstract found but UNMATCHED to worklist [{method}]\n")
        else:
            if rec["key"] not in to_write:
                to_write[rec["key"]] = (rec, ab, fn, f"{how} [{method}]", "html_scrape")
                matched += 1
            print(f"    ✓ {rec['title'][:50]!r} via {how} [{method}] "
                  f"({len(ab)} chars)\n")
        seen_files_summary.append((fn, 1 if (ab and rec) else 0))

    print(f"Matched {matched} distinct worklist papers across {len(files)} page(s).")
    if args.commit:
        changed_keys = []
        for key, (rec, ab, fn, how, src) in to_write.items():
            n = nodes.get(key)
            if n is not None:
                n["abstract"] = ab
                n["abstract_source"] = src
                n["abstract_html_file"] = fn
                wrote += 1
                changed_keys.append(key)
        json.dump(g, open(GRAPH_PATH, "w"), indent=2, ensure_ascii=False)
        # leave a re-score list so the next step is mechanical
        if changed_keys:
            open(os.path.join(FOLDER, "abstract_html_changed_keys.txt"), "w") \
                .write("\n".join(changed_keys) + "\n")
        print(f"COMMITTED {wrote} abstracts into the graph (abstract_source='html_scrape').")
        print("Wrote changed keys to abstract_html_changed_keys.txt")
        print("NEXT: re-score those rows so depth reflects the new text:")
        print("  (1) snapshot first: cp engagement_matrix.json engagement_matrix.bak.json")
        print("  python3 score_engagement.py --keys-file abstract_html_changed_keys.txt")
        print("  (2) re-run Stage-5 enrichers + enrich_links.py, then build_lit_table.py")
    else:
        print("DRY RUN — nothing written. Re-run with --commit when the matches look right.")


if __name__ == "__main__":
    main()

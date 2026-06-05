"""
build_citation_report.py
Hybrid citation analysis for the negligence corpus.

For each paper:
  1. Extract footnote citation blocks from the .txt file
  2. Extract case law references
  3. Pull citing-works from OpenAlex (where the paper is indexed)
  4. Write a unified Markdown report: citation_report.md
"""

import re, os, time, requests, urllib3

import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
FOLDER = os.path.dirname(os.path.abspath(__file__))
TXT_DIR = os.path.join(FOLDER, "txt")
REPORT_PATH = os.path.join(FOLDER, "citation_report.md")
EMAIL = os.environ.get("CROSSREF_MAILTO", "you@example.com")

SESSION = requests.Session()
_VERIFY_TLS = os.environ.get("INSECURE_TLS", "") not in ("1", "true", "True")  # verify TLS unless user opts out
SESSION.verify = _VERIFY_TLS
SESSION.headers["User-Agent"] = f"mailto:{EMAIL}"


# ── OpenAlex helpers ──────────────────────────────────────────────────────────

def oa_get(url: str) -> dict:
    try:
        r = SESSION.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e), "results": []}


def get_citing_works(oa_id: str, max_results: int = 40) -> list:
    url = (
        f"https://api.openalex.org/works"
        f"?filter=cites:{oa_id}&per-page={max_results}"
        f"&sort=cited_by_count:desc&mailto={EMAIL}"
    )
    return oa_get(url).get("results", [])


def format_oa_work(w: dict, indent: str = "  ") -> str:
    title = w.get("title") or "(no title)"
    year = w.get("publication_year") or "?"
    authors = ", ".join(
        a.get("author", {}).get("display_name") or "?"
        for a in w.get("authorships", [])[:4]
    )
    venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    doi = w.get("doi") or ""
    cited_by = w.get("cited_by_count", 0)
    line = f"{indent}- **{title}** — {authors} ({year})"
    if venue:
        line += f", *{venue}*"
    if cited_by:
        line += f" [cited by {cited_by}]"
    if doi:
        line += f"  \n{indent}  DOI: {doi}"
    return line


# ── Text extraction helpers ───────────────────────────────────────────────────

def load_txt(stem: str) -> str:
    path = os.path.join(TXT_DIR, stem + ".txt")
    with open(path, encoding="utf-8") as f:
        return f.read()


def extract_footnote_blocks(text: str) -> list[str]:
    """
    These papers use Chicago-style footnotes embedded in the page text.
    PyMuPDF renders them as long lines or multi-line blocks that begin with
    a superscript number (which appears as a plain number at the start of a
    paragraph) and contain author names, titles, journals, and years.

    Heuristic: find runs of text that follow a pattern like
        <number> <Author>, <Title>, <venue/year>
    appearing after the main body paragraph (distinguished by indentation or
    a line that is purely numeric).
    """
    # Split into pages for cleaner processing
    page_texts = re.split(r"--- Page \d+ ---", text)
    footnotes = []

    # Pattern: a block starting with a number followed by an author-style citation
    # Footnotes in these PDFs appear as lines beginning with superscript numbers
    # that PyMuPDF renders inline. We look for patterns like:
    #   "2 See, e.g., Heidi M. Hurd..." or "15 Larry Alexander..."
    fn_block_re = re.compile(
        r"(?<!\d)(\d{1,3})\s+"          # footnote number (not preceded by digit)
        r"((?:See|Id\.|Ibid|[A-Z][a-z]+).{20,})"  # starts with author or See/Id.
    )

    for page in page_texts:
        for m in fn_block_re.finditer(page):
            num = m.group(1)
            body = m.group(2).strip()
            # Filter out false positives (page numbers, section numbers, etc.)
            if len(body) > 30 and re.search(r"\d{4}", body):
                footnotes.append(f"[{num}] {body}")

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for fn in footnotes:
        key = fn[:80]
        if key not in seen:
            seen.add(key)
            unique.append(fn)
    return unique


def extract_case_law(text: str) -> list[str]:
    """Extract legal case citations from the text."""
    cases = set()

    # US style: "Carroll Towing Co., 159 F.2d 169" or "Palsgraf v. Long Island R.R."
    us_case = re.compile(
        r"[A-Z][A-Za-z\s&\.,']+\s+v\.?\s+[A-Z][A-Za-z\s&\.,']+"
        r"(?:,\s*\d+\s+[A-Z][A-Za-z\.]+(?:\s+\d+[a-z]*)?(?:\s+\d+)?)?"
    )
    for m in us_case.finditer(text):
        cand = m.group().strip().rstrip(",. ")
        # Must contain 'v.' to be a case
        if " v." in cand or " v " in cand:
            if 10 < len(cand) < 120:
                cases.add(cand)

    # UK/Commonwealth style: "[1932] AC 562"
    uk_case = re.compile(r"\[\d{4}\]\s+\d*\s*[A-Z]{1,5}\s+\d+")
    for m in uk_case.finditer(text):
        cases.add(m.group().strip())

    # Named US reporters: "169 F.2d 173" or "375 U.S. 85"
    us_reporter = re.compile(r"\d+\s+(?:U\.S\.|F\.\d[a-z]*|S\.\s*Ct\.|L\.Ed\.|Cal\.|N\.Y\.|Ill\.)\s+\d+")
    for m in us_reporter.finditer(text):
        cases.add(m.group().strip())

    return sorted(cases)


# ── Corpus definition ─────────────────────────────────────────────────────────

# OpenAlex IDs confirmed by manual lookup
CORPUS = [
    {
        "file": "HURD the innocence of negligence",
        "label": "Hurd — The Innocence of Negligence (2016)",
        "oa_id": "W3121936627",
        "oa_note": "Note: OpenAlex citing-works list for this paper contains citation-farm artifacts; results below are filtered to plausibly relevant entries only.",
    },
    {
        "file": "ALEXANDER FERZAN against negligence liability",
        "label": "Alexander & Ferzan — Against Negligence Liability (2011)",
        "oa_id": "W2484803952",
        "oa_note": None,
    },
    {
        "file": "FERZAN justification and excuse",
        "label": "Ferzan — Justification and Excuse",
        "oa_id": None,  # Not separately indexed in OpenAlex
        "oa_note": "Not found as a standalone entry in OpenAlex.",
    },
    {
        "file": "ALEXANDER FERZAN crime and culpability 2 the-essence-of-culpability",
        "label": "Alexander & Ferzan — The Essence of Culpability (Crime & Culpability Ch. 2)",
        "oa_id": "W606236685",  # Using the book record
        "oa_note": "OpenAlex indexes *Crime and Culpability* as a book (not individual chapters); citing-works below are for the whole volume.",
    },
    {
        "file": "ALEXANDER FERZAN crime and culpability 3 negligence",
        "label": "Alexander & Ferzan — Negligence (Crime & Culpability Ch. 3)",
        "oa_id": "W606236685",  # Same book record
        "oa_note": "Same OpenAlex record as Ch. 2 (whole book); citing-works list is shared.",
    },
]

PHILOSOPHY_LAW_KEYWORDS = {
    "negligence", "culpability", "criminal law", "tort", "liability", "moral responsibility",
    "recklessness", "blameworthiness", "mens rea", "excuse", "justification", "intentional",
    "wrongdoing", "harm", "punishment", "legal", "philosophy", "ethics", "fault",
    "corrective justice", "strict liability",
}

def is_relevant(w: dict) -> bool:
    """Filter out obvious citation-farm / off-topic works."""
    title = (w.get("title") or "").lower()
    concepts = [c.get("display_name", "").lower() for c in w.get("concepts", [])]
    all_text = title + " " + " ".join(concepts)
    return any(kw in all_text for kw in PHILOSOPHY_LAW_KEYWORDS)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    lines = [
        "# Citation & Reference Report — Negligence Corpus\n",
        "**Method:** Footnote/citation extraction from corpus texts + OpenAlex API for citing-works.\n",
        "**Date generated:** see file timestamp.\n",
        "\n---\n",
    ]

    all_cases = set()
    seen_oa_ids = set()  # avoid repeating Crime & Culpability twice

    for paper in CORPUS:
        label = paper["label"]
        file_stem = paper["file"]
        oa_id = paper["oa_id"]
        oa_note = paper["oa_note"]

        print(f"\n{'='*60}\n{label}")
        lines.append(f"\n## {label}\n")

        text = load_txt(file_stem)

        # ── Footnote citations ────────────────────────────────────
        footnotes = extract_footnote_blocks(text)
        lines.append(f"### Citations found in footnotes ({len(footnotes)} entries)\n")
        if footnotes:
            for fn in footnotes:
                lines.append(f"- {fn}\n")
        else:
            lines.append("_No footnote citations extracted — citations may be in running text._\n")

        # ── Case law ──────────────────────────────────────────────
        cases = extract_case_law(text)
        all_cases.update(cases)
        lines.append(f"\n### Case law references ({len(cases)} found)\n")
        if cases:
            for c in cases:
                lines.append(f"- `{c}`\n")
        else:
            lines.append("_No case citations detected._\n")

        # ── OpenAlex citing-works ─────────────────────────────────
        lines.append("\n### Papers that cite this work (via OpenAlex)\n")
        if oa_note:
            lines.append(f"_{oa_note}_\n")

        if oa_id and oa_id not in seen_oa_ids:
            seen_oa_ids.add(oa_id)
            print(f"  Fetching citing works for OA:{oa_id}")
            citers = get_citing_works(oa_id)
            # Filter to plausibly relevant works
            relevant = [w for w in citers if is_relevant(w)]
            all_citers = citers  # keep for count
            print(f"  → {len(citers)} total, {len(relevant)} filtered as relevant")
            if relevant:
                for w in relevant:
                    lines.append(format_oa_work(w) + "\n")
            elif citers:
                lines.append(f"_{len(citers)} citing works found but none match philosophy/law keyword filter. Raw results omitted._\n")
            else:
                lines.append("_No citing works found._\n")
            time.sleep(0.3)
        elif oa_id in seen_oa_ids:
            lines.append("_[Same OpenAlex record as previous chapter — see above.]_\n")
        else:
            lines.append("_Paper not indexed in OpenAlex._\n")

        lines.append("\n---\n")

    # ── Master case law list ──────────────────────────────────────
    lines.append("\n## Master List: Case Law References Across Corpus\n")
    lines.append(
        "_All legal case citations detected across the five corpus papers. "
        "These are candidates for further legal research._\n\n"
    )
    for c in sorted(all_cases):
        lines.append(f"- `{c}`\n")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n\nReport written to:\n{REPORT_PATH}")


if __name__ == "__main__":
    main()

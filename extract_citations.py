"""
extract_citations.py
Extract scholarly citations and case law from each corpus .txt file.
Handles three footnote formats found in this corpus:
  A. Hurd (SSRN preprint): inline footnotes at page bottom, introduced by
     a superscript number rendered as "N  Author..." after a long body para.
  B. Ferzan OUP chapter: numbered endnotes section at end of document.
  C. Alexander/Ferzan CUP chapters: footnotes at page bottom, introduced by
     "N  text..." where N is the footnote number flush-left.

Outputs one Markdown file per paper + a combined report.
"""

import re, os

FOLDER = os.path.dirname(os.path.abspath(__file__))
TXT_DIR = os.path.join(FOLDER, "txt")
REPORT_PATH = os.path.join(FOLDER, "citation_report.md")

# ── Noise patterns to strip before processing ────────────────────────────────

STRIP_PATTERNS = [
    re.compile(r"Downloaded from https?://\S+ by [^\n]+"),
    re.compile(r"Electronic copy available at: https?://\S+"),
    re.compile(r"https://doi\.org/\S+\s+Published online by Cambridge University Press"),
    re.compile(r"Online ISBN:[^\n]+"),
    re.compile(r"Print ISBN:[^\n]+"),
    re.compile(r"Published online:[^\n]+"),
    re.compile(r"Published in print:[^\n]+"),
]

def clean_noise(text: str) -> str:
    for pat in STRIP_PATTERNS:
        text = pat.sub("", text)
    return text


# ── Footnote extraction ───────────────────────────────────────────────────────

def extract_endnotes_section(text: str) -> list[str]:
    """
    For papers with a Notes/Endnotes section (Ferzan OUP chapter).
    Finds the 'Notes' heading then collects numbered entries.
    Entry format:  N.\n  Author, "Title," Journal vol (year): pages.
    """
    # Find Notes section
    m = re.search(r"(?im)^\s*Notes\s*$", text)
    if not m:
        return []

    notes_text = text[m.end():]
    # Split on note numbers: lines that are just "N." optionally followed by text
    entries = []
    # Pattern: number followed by period at start of a logical block
    note_re = re.compile(r"(?m)^(\d{1,3})\.\s*\n(.*?)(?=^\d{1,3}\.\s*\n|\Z)", re.DOTALL)
    for nm in note_re.finditer(notes_text):
        num = nm.group(1)
        body = nm.group(2).strip()
        body = re.sub(r"\s+", " ", body)  # collapse whitespace
        if body:
            entries.append(f"[{num}] {body}")
    return entries


def extract_inline_footnotes(text: str) -> list[str]:
    """
    For papers where footnotes appear inline at page bottom (Hurd, C&C chapters).
    Pattern per page: body text ends, then one or more blocks of:
        N  Author/See...<citation text ending at next number or page marker>
    The footnote number in these PDFs appears as a run of digits followed by
    two or more spaces (from the tab stop) then the footnote text.
    """
    # Work page by page
    pages = re.split(r"--- Page \d+ ---", text)
    footnotes = {}  # num -> text (deduplicate)

    # Pattern: footnote number (1-3 digits) + whitespace (spaces, en-space U+2002,
    # bell chars \x07 from OCR noise) + text that looks like a citation.
    # Hurd format:  "\n2 See, e.g., ..."  (number + space at line start)
    # C&C format:   "8 \x07See, e.g., ..." (number + en-space + bell + text)
    fn_re = re.compile(
        r"(?m)(?:^|\n)\s{0,6}(\d{1,3})[\s \x07]{1,6}"  # footnote number + any whitespace
        r"((?:See|Id\.|Ibid\.|[A-Z][a-zA-Z\"]).{15,})"          # citation-like start
    )

    for page in pages:
        page = clean_noise(page)
        for m in fn_re.finditer(page):
            num = int(m.group(1))
            body = m.group(2).strip()
            # Skip if body looks like a page number or short noise
            if len(body) < 20:
                continue
            # Grab continuation lines until a blank line or next footnote number
            start = m.end()
            continuation_re = re.compile(r"(?m)^(?!\d{1,3}[ \t]{2,})(?!\s*$)(.+)")
            cont_lines = []
            for cm in continuation_re.finditer(page, start):
                line_start = cm.start()
                # Stop if we hit a new footnote
                if fn_re.match(page[line_start:]):
                    break
                cont_lines.append(cm.group(1).strip())
                if len(cont_lines) > 8:
                    break
            full_body = body
            if cont_lines:
                full_body = body + " " + " ".join(cont_lines)
            full_body = re.sub(r"\s+", " ", full_body).strip()
            if num not in footnotes or len(full_body) > len(footnotes[num]):
                footnotes[num] = full_body

    return [f"[{n}] {t}" for n, t in sorted(footnotes.items())]


def extract_footnotes(text: str, stem: str) -> list[str]:
    """Dispatch to the right extractor based on which paper this is."""
    text = clean_noise(text)

    # Ferzan OUP chapter has an endnotes section
    if "justification and excuse" in stem.lower():
        result = extract_endnotes_section(text)
        if result:
            return result

    # Otherwise try inline footnotes (Hurd, C&C chapters)
    return extract_inline_footnotes(text)


# ── Separate scholarly citations from case law ───────────────────────────────

# Patterns that indicate a case citation within a footnote body
CASE_PATTERNS = [
    re.compile(r"\bv\.\s+[A-Z]"),                          # "v. Something"
    re.compile(r"\d+\s+(?:U\.S\.|F\.\d[a-z]*|S\.\s*Ct\.|L\.Ed\.|Eng\.\s*Rep\.)"),
    re.compile(r"\[\d{4}\]\s+\d*\s*[A-Z]{1,5}\s+\d+"),    # UK reporters
    re.compile(r"\d+\s+[A-Z]\.E\.\d[a-z]*\s+\d+"),        # N.E.2d etc.
    re.compile(r"\d+\s+[A-Z]\.[A-Z]\.\d*\s+\d+"),         # N.Y., Cal. etc.
]

def is_case_citation(text: str) -> bool:
    return any(p.search(text) for p in CASE_PATTERNS)


def classify_footnotes(footnotes: list[str]) -> tuple[list[str], list[str]]:
    """Split footnotes into (scholarly, case_law) lists."""
    scholarly = []
    cases = []
    for fn in footnotes:
        if is_case_citation(fn):
            cases.append(fn)
        else:
            scholarly.append(fn)
    return scholarly, cases


# ── Standalone case citation extraction from full text ────────────────────────

def extract_standalone_cases(text: str) -> list[str]:
    """
    Find case citations that appear in the body text (not just footnotes),
    returning well-formed citations only.
    """
    text = clean_noise(text)
    cases = set()

    # Full US case citation: Name v. Name, volume Reporter page (year)
    full_us = re.compile(
        r"([A-Z][A-Za-z\s&\.,]+\bv\.\s+[A-Z][A-Za-z\s&\.,]+)"
        r"(?:,\s*(\d+)\s+([A-Z][A-Za-z\.]+(?:\s+\d+[a-z]*)?)\s+(\d+)"
        r"(?:\s*\(([^)]+)\))?)?"
    )
    for m in full_us.finditer(text):
        full = m.group(0).strip().rstrip(",. ")
        # Must have at least "v." and be reasonably long
        if " v." in full and 15 < len(full) < 150:
            # Normalize whitespace
            full = re.sub(r"\s+", " ", full)
            cases.add(full)

    # UK style: [year] Reporter page
    uk = re.compile(r"\[\d{4}\]\s+\d*\s*[A-Z]{1,5}\s+\d+")
    for m in uk.finditer(text):
        cases.add(m.group().strip())

    # Filter out noise: entries that are too generic or just a partial match
    cleaned = set()
    for c in cases:
        # Must contain a v. with something on both sides
        if re.search(r"\w{3,}\s+v\.\s+\w{3,}", c):
            cleaned.add(c)
        elif re.search(r"\[\d{4}\]", c):
            cleaned.add(c)

    return sorted(cleaned)


# ── Corpus definition ─────────────────────────────────────────────────────────

CORPUS = [
    {
        "file": "HURD the innocence of negligence",
        "label": "Hurd — The Innocence of Negligence (2016)",
    },
    {
        "file": "ALEXANDER FERZAN against negligence liability",
        "label": "Alexander & Ferzan — Against Negligence Liability (2011)",
    },
    {
        "file": "FERZAN justification and excuse",
        "label": "Ferzan — Justification and Excuse",
    },
    {
        "file": "ALEXANDER FERZAN crime and culpability 2 the-essence-of-culpability",
        "label": "Alexander & Ferzan — The Essence of Culpability (Crime & Culpability Ch. 2)",
    },
    {
        "file": "ALEXANDER FERZAN crime and culpability 3 negligence",
        "label": "Alexander & Ferzan — Negligence (Crime & Culpability Ch. 3)",
    },
]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    lines = [
        "# Citation Report — Negligence Corpus\n",
        "Extracted from corpus text files. "
        "Scholarly and case-law citations are listed separately.\n",
        "\n---\n",
    ]

    all_cases = set()

    for paper in CORPUS:
        stem = paper["file"]
        label = paper["label"]
        print(f"\n{label}")

        txt_path = os.path.join(TXT_DIR, stem + ".txt")
        text = open(txt_path, encoding="utf-8").read()

        footnotes = extract_footnotes(text, stem)
        scholarly, fn_cases = classify_footnotes(footnotes)
        body_cases = extract_standalone_cases(text)

        # Merge case citations
        all_paper_cases = sorted(set(fn_cases) | set(body_cases))
        all_cases.update(all_paper_cases)

        print(f"  Scholarly citations: {len(scholarly)}")
        print(f"  Case citations: {len(all_paper_cases)}")

        lines.append(f"\n## {label}\n")

        lines.append(f"### Scholarly citations ({len(scholarly)})\n")
        if scholarly:
            for s in scholarly:
                lines.append(f"- {s}\n")
        else:
            lines.append("_None extracted._\n")

        lines.append(f"\n### Case law citations ({len(all_paper_cases)})\n")
        if all_paper_cases:
            for c in all_paper_cases:
                lines.append(f"- {c}\n")
        else:
            lines.append("_None detected._\n")

        lines.append("\n---\n")

    lines.append("\n## Master Case Law List\n")
    lines.append(
        "_All case citations across the corpus. Candidates for further legal research._\n\n"
    )
    for c in sorted(all_cases):
        lines.append(f"- {c}\n")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nReport written to: {REPORT_PATH}")


if __name__ == "__main__":
    main()

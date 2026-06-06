"""
detect_formats.py
For each paper in the corpus, detect the footnote/reference format and
write a metadata record to paper_metadata.json.

Format types:
  endnotes      - numbered list in a Notes/References section at end
  footnote_ssrn - inline page-bottom footnotes, Chicago style (Hurd/SSRN)
  footnote_cup  - inline page-bottom footnotes, CUP book style (en-space separator)
  intext        - author-date in-text citations with bibliography
  bluebook      - law review Bluebook footnotes
  unknown       - could not determine

Also records: endnote_start_page, has_bibliography, noise_level (low/medium/high),
and a `parser` field naming WHICH mining script to run. That last field matters:
a format LABEL is ambiguous about which parser handles it. In particular,
"endnotes" (a numbered References/Bibliography section at the END of a paper —
the BMJ / JAMA / J Med Ethics / J Med Philos norm) is parsed by
`parse_bibliography.py` (the References-section path), NOT by `parse_references.py`
(the page-bottom footnote/endnote parser). Routing such papers to
parse_references.py finds zero blocks and silently mines nothing. The `parser`
field removes that guesswork — follow it, not the bare format name.
"""

import re, os, json

FOLDER = os.path.dirname(os.path.abspath(__file__))
TXT_DIR = os.path.join(FOLDER, "txt")
META_PATH = os.path.join(FOLDER, "paper_metadata.json")

# ── Scoring helpers ───────────────────────────────────────────────────────────

def score_endnotes(text: str) -> tuple[int, int | None]:
    """Returns (score, start_page_index) for endnotes format."""
    score = 0
    start_page = None

    # Clear signal: a Notes or References heading on its own line
    m = re.search(r"(?im)^[ \t]*(Notes|References|Bibliography|Endnotes)[ \t]*$", text)
    if m:
        score += 40
        # Which page is this on?
        preceding = text[:m.start()]
        start_page = preceding.count("--- Page ")

    # Numbered list entries with period: "1.\n  Author..." or "1. Author..."
    if re.search(r"(?m)^\d{1,3}\.\s*\n\s+\S", text):
        score += 30
    elif re.search(r"(?m)^\d{1,3}\.\s+[A-Z]", text):
        score += 20

    # DOI patterns in reference list area
    if re.search(r"10\.\d{4,}/\S+", text):
        score += 10

    return score, start_page


def score_footnote_ssrn(text: str) -> int:
    """Inline footnotes, Chicago style — number + single space + Author/See."""
    score = 0
    # Lines matching: \nN Author... or \nN See...
    hits = len(re.findall(r"(?m)\n\d{1,3} (?:See|Id\.|[A-Z][a-z])", text))
    score += min(hits * 8, 40)
    # SSRN watermark
    if "ssrn.com" in text:
        score += 20
    # "Electronic copy available at"
    if "Electronic copy available at" in text:
        score += 15
    return score


def score_footnote_cup(text: str) -> int:
    """CUP book footnotes — number + en-space ( ) + optional bell (\x07)."""
    score = 0
    hits = len(re.findall(r"\d{1,3} [\x07]?[A-Z\"]", text))
    score += min(hits * 15, 60)
    if "Cambridge University Press" in text or "doi.org/10.1017/CBO" in text:
        score += 20
    return score


def score_intext(text: str) -> int:
    """Author-date in-text citations: (Author Year) or Author (Year)."""
    score = 0
    hits = len(re.findall(r"\([A-Z][a-z]+(?:\s+(?:and|&)\s+[A-Z][a-z]+)?\s+\d{4}\)", text))
    score += min(hits * 3, 30)
    # Bibliography heading
    if re.search(r"(?im)^[ \t]*(Bibliography|Works Cited|References)[ \t]*$", text):
        score += 20
    return score


def score_bluebook(text: str) -> int:
    """Bluebook law review style — volume Reporter page."""
    score = 0
    # Reporter patterns: "159 F.2d 169", "375 U.S. 85", "84 Colum. L. Rev. 1"
    hits = len(re.findall(
        r"\d+\s+(?:U\.S\.|F\.\d[a-z]*|S\.\s*Ct\.|[A-Z][a-z]+\.?\s+L\.\s*Rev\.)\s+\d+",
        text
    ))
    score += min(hits * 5, 30)
    # Law review volume/page in footnote area
    if re.search(r"\d+\s+[A-Z][a-z]+\.?\s+L\.\s*Rev\.\s+\d+", text):
        score += 15
    return score


def detect_noise_level(text: str) -> str:
    """Estimate OCR noise based on frequency of stray characters."""
    total_chars = max(len(text), 1)
    noise_chars = len(re.findall(r"[•·\x07 ]{1}", text))
    ratio = noise_chars / total_chars
    if ratio > 0.005:
        return "high"
    elif ratio > 0.001:
        return "medium"
    return "low"


def detect_format(stem: str, text: str) -> dict:
    s_endnotes, endnote_page = score_endnotes(text)
    s_ssrn = score_footnote_ssrn(text)
    s_cup = score_footnote_cup(text)
    s_intext = score_intext(text)
    s_bluebook = score_bluebook(text)

    scores = {
        "endnotes": s_endnotes,
        "footnote_ssrn": s_ssrn,
        "footnote_cup": s_cup,
        "intext": s_intext,
        "bluebook": s_bluebook,
    }

    best_format = max(scores, key=scores.get)
    best_score = scores[best_format]

    if best_score < 20:
        best_format = "unknown"

    # Check for mixed (e.g., CUP chapter with both footnote types)
    sorted_scores = sorted(scores.values(), reverse=True)
    is_mixed = (sorted_scores[0] > 0 and sorted_scores[1] > 0
                and sorted_scores[1] / sorted_scores[0] > 0.5)

    page_count = text.count("--- Page ")

    # Which mining script actually handles this format? The format label alone is
    # ambiguous (see module docstring): both "endnotes" (end-of-paper numbered
    # References section) and "intext" (author-date with a bibliography) are mined
    # by parse_bibliography.py, while the page-bottom footnote styles go through
    # parse_references.py. A standalone numbered References/Bibliography heading is
    # the strongest tell for the bibliography path even when footnote scores are
    # high, so check for it explicitly.
    has_ref_section = bool(re.search(
        r"(?im)^[ \t]*(References|Bibliography|Works Cited)[ \t]*$", text))
    if best_format in ("endnotes", "intext") or has_ref_section:
        parser = "parse_bibliography.py"
    elif best_format in ("footnote_ssrn", "footnote_cup", "bluebook"):
        parser = "parse_references.py"
    else:
        parser = "unknown — try parse_bibliography.py first, then parse_references.py"

    return {
        "stem": stem,
        "format": best_format,
        "parser": parser,
        "confidence": "high" if best_score >= 40 else "medium" if best_score >= 20 else "low",
        "mixed_signals": is_mixed,
        "scores": scores,
        "endnote_start_page": endnote_page,
        "page_count": page_count,
        "noise_level": detect_noise_level(text),
        "char_count": len(text),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    results = {}
    stems = sorted(f[:-4] for f in os.listdir(TXT_DIR) if f.endswith(".txt"))

    print(f"Detecting reference formats for {len(stems)} papers...\n")
    for stem in stems:
        path = os.path.join(TXT_DIR, stem + ".txt")
        text = open(path, encoding="utf-8").read()
        meta = detect_format(stem, text)
        results[stem] = meta
        print(f"  {stem[:48]:<48}  →  {meta['format']:<14} "
              f"({meta['confidence']})  run: {meta['parser']}")

    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"\nMetadata written to: {META_PATH}")
    print("NOTE: run the script named in each row's `parser` field — the format "
          "label alone does not tell you which mining script to use.")


if __name__ == "__main__":
    main()

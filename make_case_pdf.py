"""
make_case_pdf.py
Given a case name (and optionally a plain-text file of the full opinion),
generates a formatted PDF containing:
  - Abstract (150-200 words)
  - Executive summary (~500 words)
  - Major questions raised
  - Key quotes from the decision relevant to each question
  - Full text of the opinion (if provided)

Uses Claude to generate the analytical sections, then assembles a PDF
with reportlab.

Usage:
  python make_case_pdf.py "US v. Carroll Towing Co." --text carroll_towing.txt
  python make_case_pdf.py "Vaughan v. Menlove" --year 1837 --jurisdiction uk
  python make_case_pdf.py --batch cases.json   # process multiple cases

If --text is omitted, Claude draws on training knowledge and flags uncertainty.
Output goes to reading/cases/ subfolder.
"""

import argparse, json, os, re, sys, textwrap
from llm_client import call_model

FOLDER   = os.path.dirname(os.path.abspath(__file__))
OUT_DIR  = os.path.join(FOLDER, "reading", "cases")   # case PDFs you open to read
os.makedirs(OUT_DIR, exist_ok=True)

# ── Prompt ────────────────────────────────────────────────────────────────────

SYSTEM = """You are a legal research assistant helping a philosopher working on
moral and legal responsibility for negligent acts and omissions. The project
focuses on: the nature of negligence as a form of culpability; objective vs.
subjective standards of fault; voluntariness and involuntary action;
justification and excuse; recklessness vs. negligence; and the reasonable
person standard.

When analysing a case, be precise about what the court actually held vs. what
is dicta. Flag any uncertainty about facts or quotes if working from memory
rather than provided text. When quoting, use exact wording where confident;
mark approximate quotes with [approx.].

For the quotes under each question: pull EXTENDED passages from the decision —
at minimum 3-6 full sentences, enough to give the full context of the court's
reasoning, not just the headline phrase. Each question should have at least
2 substantial quotes where the text supports it. If a passage runs long, include
it in full rather than truncating — the reader wants to see the actual reasoning.

Respond with a JSON object with exactly these fields:
{
  "abstract": "150-200 word abstract",
  "summary": "~500 word executive summary",
  "questions": [
    {
      "question": "A major question raised by the case",
      "discussion": "2-3 sentence elaboration of why this question matters",
      "quotes": [
        {"text": "Extended exact or approximate quote from the decision — minimum 3-6 sentences", "attribution": "Judge name, court, year", "note": "optional explanatory note"}
      ]
    }
  ],
  "significance": "2-3 sentences on why this case is cited in the philosophical literature on negligence",
  "uncertainty_flags": ["any factual or textual uncertainties to flag"]
}"""


def build_user_msg(case_name, year, jurisdiction, citation, full_text):
    parts = [f"Case: {case_name}"]
    if citation:
        parts.append(f"Citation: {citation}")
    if year:
        parts.append(f"Year: {year}")
    if jurisdiction:
        parts.append(f"Jurisdiction: {jurisdiction}")

    if full_text:
        parts.append(f"\nFull opinion text (use this as the authoritative source):\n\n{full_text[:15000]}")
    else:
        parts.append("\nNo opinion text provided — draw on training knowledge. Flag any uncertainties.")

    parts.append("\nPlease generate the structured analysis as specified.")
    return "\n".join(parts)


def claude_analyse(case_name, year=None, jurisdiction=None, citation=None, full_text=None):
    user_msg = build_user_msg(case_name, year, jurisdiction, citation, full_text)
    text = call_model(
        system=SYSTEM,
        user=user_msg,
        model="smart",
        max_tokens=5000,
    )
    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


FORMAT_SYSTEM = """You are a careful legal text editor. You will receive the
raw plain text of a court opinion that has been extracted from HTML and has
lost its paragraph formatting — it may appear as one long block or have
inconsistent whitespace.

Your task: restore readable paragraph structure. Insert paragraph breaks at
natural boundaries — shifts in topic or reasoning, new numbered points,
transitions between procedural history / facts / analysis / holding, and
changes in speaker or judge. Do NOT add, remove, or alter any words. Do NOT
add headings or other markup. Return only the reformatted plain text with
blank lines between paragraphs. Preserve any numbered or lettered items as
separate paragraphs."""


def format_full_text(raw_text):
    """Use Claude to restore paragraph structure to raw opinion text."""
    # Work in chunks if very long — Claude can handle ~12k chars cleanly
    chunk_size = 12000
    if len(raw_text) <= chunk_size:
        chunks = [raw_text]
    else:
        # Split on sentence boundaries near the chunk limit
        chunks = []
        remaining = raw_text
        while len(remaining) > chunk_size:
            split_at = remaining.rfind(". ", 0, chunk_size)
            if split_at == -1:
                split_at = chunk_size
            chunks.append(remaining[:split_at + 1])
            remaining = remaining[split_at + 1:].lstrip()
        if remaining:
            chunks.append(remaining)

    formatted_parts = []
    for i, chunk in enumerate(chunks):
        print(f"    formatting text chunk {i+1}/{len(chunks)}...", end=" ", flush=True)
        formatted_parts.append(call_model(
            system=FORMAT_SYSTEM,
            user=chunk,
            model="fast",
            max_tokens=4000,
        ))
        print("ok")

    return "\n\n".join(formatted_parts)


# ── PDF generation ────────────────────────────────────────────────────────────

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable,
    KeepTogether, PageBreak
)
from reportlab.lib.enums import TA_LEFT, TA_JUSTIFY


def make_styles():
    base = getSampleStyleSheet()

    styles = {}
    styles["title"] = ParagraphStyle(
        "CaseTitle",
        fontName="Times-Bold",
        fontSize=18,
        leading=22,
        spaceAfter=4,
        textColor=colors.HexColor("#1a1a2e"),
    )
    styles["subtitle"] = ParagraphStyle(
        "CaseSubtitle",
        fontName="Times-Italic",
        fontSize=11,
        leading=14,
        spaceAfter=16,
        textColor=colors.HexColor("#444444"),
    )
    styles["section"] = ParagraphStyle(
        "SectionHead",
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        spaceBefore=18,
        spaceAfter=6,
        textColor=colors.HexColor("#1a1a2e"),
        borderPad=2,
    )
    styles["question"] = ParagraphStyle(
        "QuestionHead",
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        spaceBefore=12,
        spaceAfter=4,
        textColor=colors.HexColor("#333333"),
    )
    styles["body"] = ParagraphStyle(
        "BodyText",
        fontName="Times-Roman",
        fontSize=10.5,
        leading=15,
        spaceAfter=6,
        alignment=TA_JUSTIFY,
    )
    styles["quote"] = ParagraphStyle(
        "BlockQuote",
        fontName="Times-Italic",
        fontSize=10,
        leading=14,
        leftIndent=24,
        rightIndent=12,
        spaceAfter=4,
        spaceBefore=4,
        textColor=colors.HexColor("#222222"),
    )
    styles["quote_attr"] = ParagraphStyle(
        "QuoteAttr",
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        leftIndent=24,
        spaceAfter=8,
        textColor=colors.HexColor("#666666"),
    )
    styles["flag"] = ParagraphStyle(
        "Flag",
        fontName="Helvetica-Oblique",
        fontSize=9,
        leading=12,
        leftIndent=12,
        spaceAfter=4,
        textColor=colors.HexColor("#884400"),
    )
    styles["fulltext"] = ParagraphStyle(
        "FullText",
        fontName="Courier",
        fontSize=8.5,
        leading=12,
        spaceAfter=3,
        alignment=TA_LEFT,
    )
    return styles


def safe_para(text, style):
    """Escape XML special chars and return a Paragraph."""
    text = str(text or "")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Paragraph(text, style)


def build_pdf(case_name, year, jurisdiction, citation, analysis, full_text, out_path):
    doc = SimpleDocTemplate(
        out_path,
        pagesize=LETTER,
        leftMargin=1.1*inch,
        rightMargin=1.1*inch,
        topMargin=1*inch,
        bottomMargin=1*inch,
    )
    S = make_styles()
    story = []

    # ── Title block ──────────────────────────────────────────────────────────
    story.append(safe_para(case_name, S["title"]))
    subtitle_parts = []
    if citation:
        subtitle_parts.append(citation)
    if year:
        subtitle_parts.append(str(year))
    if jurisdiction:
        jmap = {"uk": "England & Wales", "us-federal": "U.S. Federal", "us-state": "U.S. State"}
        subtitle_parts.append(jmap.get(jurisdiction, jurisdiction))
    if subtitle_parts:
        story.append(safe_para(" · ".join(subtitle_parts), S["subtitle"]))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#1a1a2e"), spaceAfter=12))

    # ── Abstract ─────────────────────────────────────────────────────────────
    story.append(safe_para("Abstract", S["section"]))
    story.append(safe_para(analysis.get("abstract", ""), S["body"]))

    # ── Executive Summary ─────────────────────────────────────────────────────
    story.append(safe_para("Executive Summary", S["section"]))
    story.append(safe_para(analysis.get("summary", ""), S["body"]))

    # ── Significance ──────────────────────────────────────────────────────────
    story.append(safe_para("Philosophical Significance", S["section"]))
    story.append(safe_para(analysis.get("significance", ""), S["body"]))

    # ── Uncertainty flags ─────────────────────────────────────────────────────
    flags = analysis.get("uncertainty_flags", [])
    if flags:
        story.append(safe_para("Notes on Uncertainty", S["section"]))
        for flag in flags:
            story.append(safe_para(f"⚠ {flag}", S["flag"]))

    # ── Questions & Quotes ───────────────────────────────────────────────────
    story.append(safe_para("Major Questions Raised", S["section"]))
    for i, q in enumerate(analysis.get("questions", []), 1):
        block = []
        block.append(safe_para(f"{i}. {q.get('question','')}", S["question"]))
        if q.get("discussion"):
            block.append(safe_para(q["discussion"], S["body"]))
        for quote in q.get("quotes", []):
            qt = quote.get("text","")
            attr = quote.get("attribution","")
            note = quote.get("note","")
            block.append(safe_para(f"“{qt}”", S["quote"]))
            attr_str = f"— {attr}"
            if note:
                attr_str += f"  [{note}]"
            block.append(safe_para(attr_str, S["quote_attr"]))
        story.append(KeepTogether(block))
        story.append(Spacer(1, 6))

    # ── Full text ─────────────────────────────────────────────────────────────
    if full_text and full_text.strip():
        story.append(PageBreak())
        story.append(safe_para("Full Opinion Text", S["section"]))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                color=colors.HexColor("#aaaaaa"), spaceAfter=8))
        # Split on blank lines (paragraph boundaries restored by formatter)
        for para in re.split(r"\n{2,}", full_text):
            para = para.strip()
            if para:
                story.append(safe_para(para, S["body"]))
                story.append(Spacer(1, 4))

    doc.build(story)


# ── Main ──────────────────────────────────────────────────────────────────────

def process_case(case_name, year=None, jurisdiction=None, citation=None,
                 text_path=None, out_dir=OUT_DIR):
    print(f"\n{'='*60}\n{case_name}")

    # Load full text if provided
    full_text = None
    if text_path and os.path.exists(text_path):
        with open(text_path, encoding="utf-8", errors="replace") as f:
            full_text = f.read()
        print(f"  Full text loaded: {len(full_text)} chars")
        print("  Formatting full text...")
        full_text = format_full_text(full_text)
    else:
        print("  No full text — using training knowledge")

    # Generate analysis
    print("  Analysing...", end=" ", flush=True)
    analysis = claude_analyse(case_name, year, jurisdiction, citation, full_text)
    print("done")

    # Build output filename
    safe_name = re.sub(r"[^a-zA-Z0-9]+", "_", case_name).strip("_")
    out_path = os.path.join(out_dir, f"{safe_name}.pdf")

    # Build PDF
    print(f"  Building PDF...", end=" ", flush=True)
    build_pdf(case_name, year, jurisdiction, citation, analysis, full_text, out_path)
    print(f"done → {os.path.basename(out_path)}")

    return out_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("case_name", nargs="?", help="Case name")
    parser.add_argument("--year", type=int)
    parser.add_argument("--jurisdiction", default=None,
                        help="uk, us-federal, us-state")
    parser.add_argument("--citation", default=None)
    parser.add_argument("--text", default=None,
                        help="Path to plain-text file of the full opinion")
    parser.add_argument("--batch", default=None,
                        help="JSON file with list of case dicts")
    args = parser.parse_args()

    if args.batch:
        with open(args.batch) as f:
            cases = json.load(f)
        for c in cases:
            process_case(
                c["name"],
                year=c.get("year"),
                jurisdiction=c.get("jurisdiction"),
                citation=c.get("citation"),
                text_path=c.get("text_path"),
            )
    elif args.case_name:
        process_case(
            args.case_name,
            year=args.year,
            jurisdiction=args.jurisdiction,
            citation=args.citation,
            text_path=args.text,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

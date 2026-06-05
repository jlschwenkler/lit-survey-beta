"""
convert_pdfs.py
Convert PDFs in this folder to plain-text files.
Preserves: page boundaries (--- Page N ---), footnotes (best-effort),
and bibliography sections.
"""

import fitz  # PyMuPDF
import os
import re

FOLDER = os.path.dirname(os.path.abspath(__file__))
# PDFs you read live under reading/pdfs (score-5) and reading/pdfs/score4.
PDF_DIRS = [
    os.path.join(FOLDER, "reading", "pdfs"),
    os.path.join(FOLDER, "reading", "pdfs", "score4"),
]
OUTPUT_SUBDIR = os.path.join(FOLDER, "txt")   # extracted text (local artifact)
os.makedirs(OUTPUT_SUBDIR, exist_ok=True)


def extract_text(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    pages = []
    for i, page in enumerate(doc, start=1):
        # Use layout-preserving text extraction
        text = page.get_text("text", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        pages.append(f"\n\n--- Page {i} ---\n\n{text.strip()}")
    doc.close()
    return "\n".join(pages)


def clean_text(text: str) -> str:
    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of 3+ blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main():
    # Collect (stem, full_path) for every PDF across the reading dirs.
    pdfs = []
    for d in PDF_DIRS:
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(".pdf"):
                pdfs.append((f, os.path.join(d, f)))
    if not pdfs:
        print("No PDF files found under", " or ".join(PDF_DIRS))
        return

    for pdf_name, pdf_path in pdfs:
        stem = os.path.splitext(pdf_name)[0]
        out_path = os.path.join(OUTPUT_SUBDIR, stem + ".txt")

        print(f"Converting: {pdf_name} ...", end=" ", flush=True)
        try:
            raw = extract_text(pdf_path)
            clean = clean_text(raw)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(clean)
            page_count = clean.count("--- Page ")
            print(f"done ({page_count} pages → {os.path.basename(out_path)})")
        except Exception as e:
            print(f"ERROR: {e}")

    print(f"\nAll done. Text files written to: {OUTPUT_SUBDIR}")


if __name__ == "__main__":
    main()

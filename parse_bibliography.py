"""
parse_bibliography.py  —  Parse author-date REFERENCE LISTS from full-text papers.

Companion to parse_references.py (which handles footnote/endnote styles). Wave-1
reference-mining papers (Yates, Sarin x2) use an author-date "References" section
rather than footnotes, so we extract that section, split it into entries, and
structure each with Claude — then resolve IDs and append to the SAME
parsed_references.json so the overlooked-texts step is uniform.

Reuses s2_lookup / oa_lookup / dedup_key from parse_references.py.

Records carry source_paper = the stem, and footnote_num = null (bibliography,
not a footnote). Re-running is safe: skips stems already present.

Usage:
  python parse_bibliography.py                          # all configured stems
  python parse_bibliography.py --stem "YATES ..." ...   # specific stems
  python parse_bibliography.py --limit 5                # smoke test (first N entries)
"""

import os, re, json, time, argparse
import requests, urllib3
from llm_client import call_model
import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
import parse_references as PR   # reuse helpers + sessions

FOLDER   = os.path.dirname(os.path.abspath(__file__))
TXT_DIR  = os.path.join(FOLDER, "txt")
OUT_PATH = os.path.join(FOLDER, "parsed_references.json")

# Wave-1 bibliography-style source texts (stem -> txt filename).
# (Author-date reference lists. Footnote-style sources like Finkelstein go
# through parse_references.py instead.)
# Which source texts to parse, as {stem: filename}. By DEFAULT this is
# auto-discovered from every txt/*.txt file present — drop your extracted
# author-date bibliography texts in txt/ and run, nothing to edit here. (The old
# shipped hardcoded dict silently no-op'd for new users.) Use --stem to restrict to
# specific files. Footnote-style sources go through parse_references.py instead.
import glob as _glob
WAVE1 = {
    os.path.splitext(os.path.basename(p))[0]: os.path.basename(p)
    for p in sorted(_glob.glob(os.path.join(TXT_DIR, "*.txt")))
}

REF_HEADER = re.compile(r"\n\s*(References|Bibliography|REFERENCES|Works Cited)\b[^\n]*\n")

# OUP Scholarship Online bibliography PDFs interleave per-entry link labels and
# running page markers that must be stripped before entry-splitting.
OUP_BOILERPLATE = re.compile(
    r"(?im)^\s*(Google Scholar|Google Preview|WorldCat|COPAC|PubMed|Crossref"
    r"|Find at .*|p\.\s*\d+)\s*$"
)


def strip_oup_boilerplate(text):
    """Remove OUP per-entry link labels, page markers, and PUA glyphs."""
    text = re.sub("[\uE000-\uF8FF]", " ", text)  # strip Unicode private-use glyphs
    text = OUP_BOILERPLATE.sub("", text)
    return text


def extract_refs_section(text):
    """Return the text from the LAST References header to end (or to an Appendix)."""
    matches = list(REF_HEADER.finditer(text))
    if not matches:
        return None
    start = matches[-1].end()
    tail = text[start:]
    # cut off a trailing appendix/figure dump if present
    cut = re.search(r"\n\s*(Appendix|Supplementary|Author Note|Figure \d)\b", tail)
    if cut:
        tail = tail[:cut.start()]
    return tail.strip()


def split_entries(refs):
    """Split an author-date reference list into individual entries.

    Heuristic: a new entry starts at a line beginning with a capitalized surname
    followed by a comma/initial, OR an unindented line after a hanging indent.
    We rejoin wrapped lines, then split on the author-year start pattern.
    """
    # collapse hard-wrapped lines into spaces, but keep paragraph-ish breaks
    refs = re.sub(r"-\n", "", refs)            # de-hyphenate line breaks
    refs = re.sub(r"\n+", "\n", refs)
    lines = [l.strip() for l in refs.split("\n") if l.strip()]
    # An entry start looks like one of:
    #   "Surname, X."          author-date (APA-ish: Sarin, Yates, Zimmerman)
    #   "Surname  XY,"         British legal style (Rodríguez-Blanco: initials,
    #                          no comma after surname, then a comma)
    #   "—— ..." / "— ..."     repeat-author marker (same author as prior entry)
    START = re.compile(
        r"^(?:[A-ZÀ-Þ][\w’'-]+,\s+[A-ZÀ-Þ]"        # Surname, X
        r"|[A-ZÀ-Þ][\w’'-]+\s{1,4}[A-ZÀ-Þ]{1,3},"  # Surname  XY,
        r"|[—–-]{2,}\s)"                            # —— repeat author
    )
    REPEAT = re.compile(r"^[—–-]{2,}\s*")
    last_author = ""   # surname carried into "——" repeat-author entries
    entries, cur = [], ""
    for ln in lines:
        if START.match(ln) and cur:
            entries.append(cur.strip()); cur = ln
        else:
            cur = f"{cur} {ln}".strip() if cur else ln
        # remember the surname that opens a fresh non-repeat entry
        if START.match(ln) and not REPEAT.match(ln):
            last_author = ln.split(",")[0].split("  ")[0].strip()
        # expand a "——" marker so the per-entry parser keeps the author
        if REPEAT.match(cur) and last_author:
            cur = REPEAT.sub(last_author + ", ", cur, count=1)
    if cur:
        entries.append(cur.strip())
    # keep only plausible citations (contain a 4-digit year)
    return [e for e in entries if re.search(r"\b(19|20)\d{2}\b", e)]


BIB_SYSTEM = """You are a citation parser. You will receive a batch of raw
entries from an author-date REFERENCE LIST in a philosophy/psychology/law paper,
one per line, numbered. Return ONLY valid JSON: a list with one object per input
entry, in the same order. Each object:
{
  "authors": ["Last, First", ...],
  "title": "...",
  "venue": "...",          // journal, book, or publisher
  "year": 1999,            // integer or null
  "volume": "...",         // or null
  "pages": "...",          // or null
  "type": "article"|"book"|"chapter"|"other",
  "note": "..."            // e.g. "edited by ..."; else null
}
Rules: classify book chapters (in an edited collection) as "chapter"; whole
books/monographs as "book"; journal articles as "article". If an entry is
garbled or not a real citation, set type "other" and copy what you can."""


def parse_batch(entries):
    numbered = "\n".join(f"{i+1}. {e}" for i, e in enumerate(entries))
    try:
        txt = call_model(
            system=BIB_SYSTEM, user=numbered, model="fast", max_tokens=4000,
        )
        txt = re.sub(r"^```(?:json)?\n?", "", txt); txt = re.sub(r"\n?```$", "", txt)
        return json.loads(txt)
    except Exception as e:
        return [{"type": "parse-error", "note": str(e)[:80]} for _ in entries]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stem", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    existing = json.load(open(OUT_PATH)) if os.path.exists(OUT_PATH) else []
    done_stems = {r.get("source_paper") for r in existing}

    stems = args.stem if args.stem else list(WAVE1.keys())
    new_records = []

    for stem in stems:
        if stem in done_stems:
            print(f"skip (already parsed): {stem}"); continue
        path = os.path.join(TXT_DIR, WAVE1.get(stem, stem + ".txt"))
        if not os.path.exists(path):
            print(f"MISSING txt: {path}"); continue
        text = PR.clean_noise(open(path, encoding="utf-8").read())
        text = strip_oup_boilerplate(text)
        refs = extract_refs_section(text)
        if not refs:
            print(f"no References section found in {stem}"); continue
        entries = split_entries(refs)
        if args.limit:
            entries = entries[:args.limit]
        print(f"\n{stem}: {len(entries)} reference entries")

        # parse in batches of 25 to bound tokens
        parsed = []
        for i in range(0, len(entries), 25):
            parsed += parse_batch(entries[i:i+25])
            time.sleep(0.2)

        # resolve IDs + attach provenance
        for raw, rec in zip(entries, parsed):
            if not isinstance(rec, dict) or rec.get("type") in ("parse-error",):
                continue
            title, authors = rec.get("title"), rec.get("authors") or []
            s2_id = oa_id = doi = None
            if rec.get("type") != "other" and title:
                s2_id, d1 = PR.s2_lookup(title, authors)
                oa_id, d2 = PR.oa_lookup(title, authors)
                doi = d1 or d2
                time.sleep(0.1)
            rec.update({
                "doi": doi, "oa_id": oa_id, "s2_id": s2_id,
                "source_paper": stem, "footnote_num": None,
                "raw_text": raw[:300],
            })
            new_records.append(rec)
        print(f"   parsed {len([r for r in new_records if r['source_paper']==stem])} records")

    all_records = existing + new_records
    json.dump(all_records, open(OUT_PATH, "w"), indent=2, ensure_ascii=False)
    resolved = sum(1 for r in new_records if r.get("oa_id") or r.get("s2_id") or r.get("doi"))
    print(f"\nAdded {len(new_records)} records ({resolved} ID-resolved). "
          f"Total now {len(all_records)}.")


if __name__ == "__main__":
    main()

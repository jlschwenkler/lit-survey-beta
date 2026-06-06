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

# Running page headers/footers in two-column journal PDFs (BMC, BMJ, Springer,
# \u2026) get interleaved into the references and can be swallowed into an entry,
# corrupting it (and, before the parse_batch fix, sinking the whole batch). Strip
# the common shapes: "Page 10 of 10", "Page 10 -- ...", and journal running
# footers like "Benzinger et al. BMC Medical Ethics (2024) 25:78".
RUNNING_FURNITURE = re.compile(
    r"(?im)^\s*(?:"
    r"Page\s+\d+(?:\s+of\s+\d+)?\b.*"                       # "Page 10 of 10 ..."
    r"|.{0,40}\bet al\.?\s+.{0,60}\(\d{4}\)\s*\d+\s*:\s*\d+\s*"  # journal footer
    r")$"
)


def strip_oup_boilerplate(text):
    """Remove OUP per-entry link labels, page markers, running headers/footers,
    and PUA glyphs \u2014 anything page furniture that would otherwise be swallowed
    into a reference entry."""
    text = re.sub("[\uE000-\uF8FF]", " ", text)  # strip Unicode private-use glyphs
    text = OUP_BOILERPLATE.sub("", text)
    text = RUNNING_FURNITURE.sub("", text)
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


def _split_numbered_entries(refs):
    """Split a NUMBERED (Vancouver-style) reference list into entries.

    Format common in medical / bioethics journals (BMJ, JAMA, J Med Ethics,
    J Med Philos, AJOB): each entry opens with a reference number, optionally
    tab/space-indented, then an author surname + initials —
        "1  Orentlicher D. Advance Medical Directives. JAMA 1990;263:2365-7."
    The author-date split_entries() can't see these (no "Surname, X." start), so
    we split on the leading number that begins a new entry and rejoin wrapped
    lines in between. Returns [] if the list doesn't look numbered, so the caller
    can fall back to the author-date heuristic.
    """
    refs = re.sub(r"-\n", "", refs)            # de-hyphenate line breaks
    # A new entry starts at: line start, optional leading whitespace, a 1-3 digit
    # number, then whitespace, then a capitalized author surname. Using a split on
    # this boundary keeps the number-less continuation lines attached.
    START = re.compile(
        r"(?m)^[ \t\x07]*(\d{1,3})[ \t\x07]+(?=[A-ZÀ-Þ])"
    )
    marks = list(START.finditer(refs))
    # Require a real numbered list: several entries, and numbers that mostly run
    # consecutively from 1 (guards against stray "2024" lines tripping the regex).
    nums = [int(m.group(1)) for m in marks]
    if len(nums) < 3 or nums[0] > 2 or sum(
        1 for a, b in zip(nums, nums[1:]) if b == a + 1
    ) < len(nums) * 0.6:
        return []
    entries = []
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(refs)
        body = re.sub(r"\s+", " ", refs[start:end]).strip()
        if body:
            entries.append(body)
    return [e for e in entries if re.search(r"\b(19|20)\d{2}\b", e)]


def split_entries(refs):
    """Split a reference list into individual entries.

    Tries the NUMBERED (Vancouver) format first — common in medical/bioethics
    journals — then falls back to the author-date heuristic below.

    Author-date heuristic: a new entry starts at a line beginning with a
    capitalized surname followed by a comma/initial, OR an unindented line after a
    hanging indent. We rejoin wrapped lines, then split on the author-year start.
    """
    numbered = _split_numbered_entries(refs)
    if numbered:
        return numbered
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
    """Parse a batch of reference strings into structured records via the LLM.

    Resilient to a malformed JSON response: rather than marking the WHOLE batch
    parse-error (which silently zeroed an entire paper's references when one
    layout artifact derailed the model's JSON), we split the batch in half and
    retry recursively, down to per-entry. That way a single bad entry loses only
    itself; its well-formed neighbours are salvaged. Always returns a list the
    same length as `entries`, so the caller's zip() stays aligned.
    """
    if not entries:
        return []
    numbered = "\n".join(f"{i+1}. {e}" for i, e in enumerate(entries))
    try:
        txt = call_model(
            system=BIB_SYSTEM, user=numbered, model="fast", max_tokens=4000,
        )
        txt = re.sub(r"^```(?:json)?\n?", "", txt); txt = re.sub(r"\n?```$", "", txt)
        parsed = json.loads(txt)
        if isinstance(parsed, list) and len(parsed) == len(entries):
            return parsed
        # length mismatch — treat like a parse failure so we fall through to salvage
        raise ValueError(f"expected {len(entries)} records, got "
                         f"{len(parsed) if isinstance(parsed, list) else type(parsed).__name__}")
    except Exception as e:
        if len(entries) == 1:
            # can't subdivide further — this single entry is the bad one
            print(f"    ⚠ entry failed to parse, skipping: {str(e)[:60]}")
            return [{"type": "parse-error", "note": str(e)[:80]}]
        # subdivide and retry: a bad entry should not sink its batch-mates
        print(f"    ⚠ batch of {len(entries)} failed JSON ({str(e)[:40]}); "
              f"retrying in halves to salvage the good entries")
        mid = len(entries) // 2
        time.sleep(0.2)
        left = parse_batch(entries[:mid])
        time.sleep(0.2)
        right = parse_batch(entries[mid:])
        return left + right


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

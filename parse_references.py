"""
parse_references.py
Uses Claude to parse raw footnote blocks extracted from corpus PDFs into
structured citation records, then looks each one up in Semantic Scholar
and OpenAlex to obtain a DOI / paper ID for use in the graph crawler.

Input:  txt/*.txt  (corpus text files)
Output: parsed_references.json  — one record per unique citation, with
        fields: author, title, venue, year, type, doi, oa_id, s2_id,
                source_paper, footnote_num, raw_text

Run once; re-running is safe (skips already-parsed papers).
"""

import ssl
import os as _os  # TLS verification on by default; opt out with INSECURE_TLS=1
if _os.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    ssl._create_default_https_context = ssl._create_unverified_context

import re, os, json, time
from llm_client import call_model
import requests
import urllib3

import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
FOLDER   = os.path.dirname(os.path.abspath(__file__))
TXT_DIR  = os.path.join(FOLDER, "txt")
OUT_PATH = os.path.join(FOLDER, "parsed_references.json")
EMAIL    = os.environ.get("CROSSREF_MAILTO", "you@example.com")

S2      = requests.Session()
_VERIFY_TLS = os.environ.get("INSECURE_TLS", "") not in ("1", "true", "True")  # verify TLS unless user opts out
S2.verify = _VERIFY_TLS
S2.headers["User-Agent"] = f"mailto:{EMAIL}"
OA      = requests.Session()
OA.verify = _VERIFY_TLS
OA.headers["User-Agent"] = f"mailto:{EMAIL}"

# ── Noise stripping (reused from extract_citations.py) ───────────────────────

STRIP_PATTERNS = [
    re.compile(r"Downloaded from https?://\S+ by [^\n]+"),
    re.compile(r"Electronic copy available at: https?://\S+"),
    re.compile(r"https://doi\.org/\S+\s+Published online by Cambridge University Press"),
    re.compile(r"Online ISBN:[^\n]+"),
    re.compile(r"Print ISBN:[^\n]+"),
    re.compile(r"Published online:[^\n]+"),
    re.compile(r"Published in print:[^\n]+"),
]

def clean_noise(text):
    for pat in STRIP_PATTERNS:
        text = pat.sub("", text)
    return text


# ── Raw footnote block extraction ─────────────────────────────────────────────

def extract_raw_blocks(text, stem):
    """
    Return list of (footnote_num, raw_text) pairs.
    Handles all three formats found in this corpus.
    """
    text = clean_noise(text)
    blocks = []

    # ── Format A: endnotes section (Ferzan OUP) ───────────────────────────
    m = re.search(r"(?im)^\s*Notes\s*$", text)
    if m and "justification" in stem.lower():
        notes_text = text[m.end():]
        note_re = re.compile(
            r"(?m)^(\d{1,3})\.\s*\n(.*?)(?=^\d{1,3}\.\s*\n|\Z)", re.DOTALL
        )
        for nm in note_re.finditer(notes_text):
            num  = nm.group(1)
            body = re.sub(r"\s+", " ", nm.group(2)).strip()
            if len(body) > 20:
                blocks.append((num, body))
        return blocks

    # ── Format B/C: inline page-bottom footnotes ──────────────────────────
    pages = re.split(r"--- Page \d+ ---", text)
    seen  = {}   # num -> best body seen so far

    fn_re = re.compile(
        r"(?m)(?:^|\n)\s{0,6}(\d{1,3})[\s \x07]{1,6}"
        r"((?:See|Id\.|Ibid\.|[A-Z][a-zA-Z\"]).{15,})"
    )

    for page in pages:
        for m2 in fn_re.finditer(page):
            num  = m2.group(1)
            body = m2.group(2).strip()
            # Grab continuation lines
            start = m2.end()
            cont  = []
            for cm in re.finditer(r"(?m)^(?!\d{1,3}[\s \x07])(?!\s*$)(.+)", page[start:]):
                if fn_re.match(page[start + cm.start():]):
                    break
                cont.append(cm.group(1).strip())
                if len(cont) > 6:
                    break
            full = re.sub(r"\s+", " ", body + " " + " ".join(cont)).strip()
            if len(full) > 25 and (num not in seen or len(full) > len(seen[num])):
                seen[num] = full

    for num, body in sorted(seen.items(), key=lambda x: int(x[0])):
        blocks.append((num, body))

    # ── Per-paper artifact filtering ──────────────────────────────────────
    # Duff (Springer two-column layout) interleaves footnotes with body text,
    # so the inline extractor also grabs page running-heads (mis-numbered as
    # large footnote ids) and section headings (whose anchor fell at a section
    # start). Drop those; keep genuine footnotes.
    if "duff" in stem.lower():
        PAGEHDR = "Criminal Law and Philosophy (2019)"
        SECTION_HEADS = (
            "Two Problems About Recklessness",
            "Vorsatz, Fahrlässigkeit",
            "Dolus Eventualis and Recklessness",
            "The Significance of Inadvertence",
        )
        def _artifact(num, body):
            if PAGEHDR in body:
                return True
            if int(num) > 200:            # page-number mis-parsed as footnote
                return True
            if any(body.lstrip().startswith(h) for h in SECTION_HEADS):
                return True
            return False
        blocks = [(n, b) for n, b in blocks if not _artifact(n, b)]

    # Finkelstein (law-review, no page markers): body-text sentences whose
    # paragraph began with a number-like token get mis-parsed as huge footnote
    # ids (e.g. 584, 590). Genuine footnotes here run 1..33.
    if "finkelstein" in stem.lower():
        blocks = [(n, b) for n, b in blocks if int(n) <= 100]

    return blocks


# ── Claude parsing ────────────────────────────────────────────────────────────

PARSE_SYSTEM = """You are a citation parser for an academic research project.
You will receive a raw footnote or endnote block from a philosophy/law paper.
Your job is to split it into individual citations and return structured JSON.

Rules:
- A single footnote block often contains multiple citations separated by
  semicolons, or "See also", "cf.", etc. Split each into a separate record.
- "Id." and "Ibid." refer to the immediately preceding citation — include them
  with a note field saying "same as preceding" and copy the author if clear.
- "Above n. N" cross-references are not new citations — mark type "cross-ref".
- Case citations (containing "v." + reporter) get type "case".
- Books get type "book", journal articles get type "article",
  book chapters get type "chapter", everything else "other".
- If a field is unknown, use null.
- Return ONLY valid JSON — a list of objects, nothing else.

Each object must have these fields:
{
  "authors": ["Last, First", ...],   // list of author names
  "title": "...",                    // title of article/book/chapter
  "venue": "...",                    // journal name, book title, or court name
  "year": 1999,                      // integer or null
  "volume": "...",                   // volume number or null
  "pages": "...",                    // page range or null
  "type": "article"|"book"|"chapter"|"case"|"cross-ref"|"other",
  "note": "..."                      // any extra info, e.g. "same as preceding"
}"""


def parse_block_with_claude(num, raw_text):
    """Send a footnote block to Claude and return list of structured citation dicts."""
    prompt = f"Footnote [{num}]:\n{raw_text}"
    try:
        text = call_model(
            system=PARSE_SYSTEM,
            user=prompt,
            model="fast",   # fast + cheap for structured extraction
            max_tokens=800,
        )
        # Strip markdown code fences if present
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        return json.loads(text)
    except json.JSONDecodeError as e:
        return [{"type": "parse-error", "note": str(e), "raw": raw_text[:200]}]
    except Exception as e:
        return [{"type": "api-error", "note": str(e)}]


# ── ID lookup ─────────────────────────────────────────────────────────────────

def s2_lookup(title, authors):
    """Try to find a paper on Semantic Scholar by title+author."""
    if not title or len(title) < 10:
        return None, None
    query = title
    if authors:
        surname = authors[0].split(",")[0].split()[-1]
        query = f"{surname} {title}"
    try:
        r = S2.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={"query": query[:200], "limit": 3,
                    "fields": "title,authors,year,externalIds"},
            timeout=12,
        )
        results = r.json().get("data", [])
        if not results:
            return None, None
        # Score by title similarity (simple token overlap)
        title_tokens = set(title.lower().split())
        def score(p):
            pt = set((p.get("title") or "").lower().split())
            return len(title_tokens & pt) / max(len(title_tokens), 1)
        results.sort(key=score, reverse=True)
        best = results[0]
        if score(best) < 0.4:
            return None, None
        s2_id = best.get("paperId")
        doi   = (best.get("externalIds") or {}).get("DOI")
        return s2_id, (f"https://doi.org/{doi}" if doi else None)
    except Exception:
        return None, None


def oa_lookup(title, authors):
    """Try to find a paper on OpenAlex by title."""
    if not title or len(title) < 10:
        return None, None
    query = requests.utils.quote(title[:150])
    try:
        r = OA.get(
            f"https://api.openalex.org/works?search={query}&per-page=3&mailto={EMAIL}",
            timeout=12,
        )
        results = r.json().get("results", [])
        if not results:
            return None, None
        title_tokens = set(title.lower().split())
        def score(w):
            wt = set((w.get("title") or "").lower().split())
            return len(title_tokens & wt) / max(len(title_tokens), 1)
        results.sort(key=score, reverse=True)
        best = results[0]
        if score(best) < 0.4:
            return None, None
        oa_id = (best.get("id") or "").replace("https://openalex.org/", "")
        doi   = best.get("doi") or None
        return oa_id, doi
    except Exception:
        return None, None


# ── Deduplication ─────────────────────────────────────────────────────────────

def dedup_key(rec):
    """Canonical key for deduplication."""
    title = (rec.get("title") or "").lower().strip()
    # Normalize: remove punctuation, collapse spaces
    title = re.sub(r"[^\w\s]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    year  = str(rec.get("year") or "")
    return f"{title[:60]}|{year}"


# ── Main ──────────────────────────────────────────────────────────────────────

CORPUS_STEMS = [
    "HURD the innocence of negligence",
    "ALEXANDER FERZAN against negligence liability",
    "FERZAN justification and excuse",
    "ALEXANDER FERZAN crime and culpability 2 the-essence-of-culpability",
    "ALEXANDER FERZAN crime and culpability 3 negligence",
    "DUFF two models of criminal fault",
    "FINKELSTEIN responsibility for unintended consequences",
]

def main():
    # Load existing results
    if os.path.exists(OUT_PATH):
        all_refs = json.load(open(OUT_PATH))
        done_stems = {r["source_paper"] for r in all_refs}
        print(f"Loaded {len(all_refs)} existing records. Done: {done_stems}")
    else:
        all_refs   = []
        done_stems = set()

    seen_keys = {dedup_key(r): True for r in all_refs}

    for stem in CORPUS_STEMS:
        if stem in done_stems:
            print(f"\n[skip] {stem}")
            continue

        txt_path = os.path.join(TXT_DIR, stem + ".txt")
        if not os.path.exists(txt_path):
            print(f"\n[missing] {txt_path}")
            continue

        text   = open(txt_path, encoding="utf-8").read()
        blocks = extract_raw_blocks(text, stem)
        print(f"\n── {stem}")
        print(f"   {len(blocks)} footnote blocks to parse")

        paper_refs = []
        for num, raw in blocks:
            parsed = parse_block_with_claude(num, raw)
            time.sleep(0.1)   # small pause between API calls

            for rec in parsed:
                rec["source_paper"]  = stem
                rec["footnote_num"]  = num
                rec["raw_text"]      = raw[:300]
                rec["doi"]           = None
                rec["oa_id"]         = None
                rec["s2_id"]         = None

                # Skip types we can't look up
                if rec.get("type") in ("cross-ref", "other", "parse-error",
                                       "api-error", "case"):
                    paper_refs.append(rec)
                    continue

                key = dedup_key(rec)
                if key in seen_keys:
                    rec["note"] = (rec.get("note") or "") + " [duplicate]"
                    paper_refs.append(rec)
                    continue
                seen_keys[key] = True

                # ID lookup
                title   = rec.get("title") or ""
                authors = rec.get("authors") or []
                if title:
                    s2_id, doi = s2_lookup(title, authors)
                    if s2_id:
                        rec["s2_id"] = s2_id
                        rec["doi"]   = doi
                    if not doi:
                        oa_id, oa_doi = oa_lookup(title, authors)
                        rec["oa_id"] = oa_id
                        if oa_doi and not rec["doi"]:
                            rec["doi"] = oa_doi
                    time.sleep(0.15)

                paper_refs.append(rec)

        print(f"   → {len(paper_refs)} citation records")
        found_ids = sum(1 for r in paper_refs if r.get("s2_id") or r.get("oa_id"))
        print(f"   → {found_ids} matched in S2/OA")

        all_refs.extend(paper_refs)
        # Save after each paper
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(all_refs, f, indent=2, ensure_ascii=False)

    # Final summary
    print(f"\n{'='*55}")
    print(f"Total citation records: {len(all_refs)}")
    by_type = {}
    for r in all_refs:
        t = r.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
    for t, n in sorted(by_type.items()):
        print(f"  {t:<12} {n}")
    found = sum(1 for r in all_refs if r.get("s2_id") or r.get("oa_id"))
    print(f"  Matched in S2/OA: {found}")
    print(f"\nOutput: {OUT_PATH}")


if __name__ == "__main__":
    main()

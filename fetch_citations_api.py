"""
fetch_citations_api.py
For each paper, query OpenAlex then fall back to Semantic Scholar
for citing and cited-by works. Writes results to citation_api_data.json.

Run after extract_citations.py (uses paper_metadata.json for paper list).
"""

import requests, urllib3, json, time, os, re

import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
FOLDER = os.path.dirname(os.path.abspath(__file__))
META_PATH = os.path.join(FOLDER, "paper_metadata.json")
OUTPUT_PATH = os.path.join(FOLDER, "citation_api_data.json")
EMAIL = os.environ.get("CROSSREF_MAILTO", "you@example.com")
S2_API_KEY = None  # set if you have one; works without for low volume

OA = requests.Session()
_VERIFY_TLS = os.environ.get("INSECURE_TLS", "") not in ("1", "true", "True")  # verify TLS unless user opts out
OA.verify = _VERIFY_TLS
OA.headers["User-Agent"] = f"mailto:{EMAIL}"

S2 = requests.Session()
S2.verify = _VERIFY_TLS
S2.headers["User-Agent"] = f"mailto:{EMAIL}"
if S2_API_KEY:
    S2.headers["x-api-key"] = S2_API_KEY

# ── Paper definitions — extend this list as corpus grows ─────────────────────
# Each entry: title, authors (for disambiguation), known DOI or OA ID if available

CORPUS = [
    {
        "stem": "HURD the innocence of negligence",
        "title": "The Innocence of Negligence",
        "authors": ["Heidi Hurd"],
        "oa_id": "W3121936627",
        "doi": None,
    },
    {
        "stem": "ALEXANDER FERZAN against negligence liability",
        "title": "Against Negligence Liability",
        "authors": ["Larry Alexander", "Kimberly Ferzan"],
        "oa_id": "W2484803952",
        "doi": None,
    },
    {
        "stem": "FERZAN justification and excuse",
        "title": "Justification and Excuse",
        "authors": ["Kimberly Ferzan"],
        "oa_id": None,
        "doi": "10.1093/oxfordhb/9780195314854.003.0010",
    },
    {
        "stem": "ALEXANDER FERZAN crime and culpability 2 the-essence-of-culpability",
        "title": "Crime and Culpability: A Theory of Criminal Law",
        "authors": ["Larry Alexander", "Kimberly Ferzan"],
        "oa_id": "W606236685",   # whole book record
        "doi": None,
        "note": "Ch. 2 — OpenAlex indexes the whole volume only",
    },
    {
        "stem": "ALEXANDER FERZAN crime and culpability 3 negligence",
        "title": "Crime and Culpability: A Theory of Criminal Law",
        "authors": ["Larry Alexander", "Kimberly Ferzan"],
        "oa_id": "W606236685",   # same book record
        "doi": None,
        "note": "Ch. 3 — same OA record as Ch. 2; citing-works not re-fetched",
    },
    {
        "stem": "DUFF two models of criminal fault",
        "title": "Two Models of Criminal Fault",
        "authors": ["R. A. Duff"],
        "oa_id": "W2969294810",
        "doi": "10.1007/s11572-019-09504-w",
        "note": "OA/S2 carry no reference list; backward citations via parsed footnotes",
    },
]

# ── OpenAlex helpers ──────────────────────────────────────────────────────────

def oa_get(url: str) -> dict:
    try:
        r = OA.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e), "results": []}


def oa_search_by_title(title: str, authors: list[str], max_results=5) -> list[dict]:
    q = requests.utils.quote(title)
    url = f"https://api.openalex.org/works?search={q}&per-page={max_results}&mailto={EMAIL}"
    results = oa_get(url).get("results", [])
    if not results:
        return []
    # Score by author match
    def author_score(w):
        names = " ".join(
            a.get("author", {}).get("display_name", "").lower()
            for a in w.get("authorships", [])
        )
        return sum(1 for a in authors if a.split()[-1].lower() in names)
    results.sort(key=author_score, reverse=True)
    return results


def oa_citing_works(oa_id: str, max_results=50) -> list[dict]:
    short = oa_id.split("/")[-1]
    url = (f"https://api.openalex.org/works?filter=cites:{short}"
           f"&per-page={max_results}&sort=cited_by_count:desc&mailto={EMAIL}")
    return oa_get(url).get("results", [])


def oa_referenced_works(oa_id: str) -> list[dict]:
    """Fetch the reference list for a work (often empty for older articles)."""
    url = f"https://api.openalex.org/works/{oa_id}?mailto={EMAIL}"
    work = oa_get(url)
    ref_ids = work.get("referenced_works", [])
    refs = []
    for rid in ref_ids[:60]:
        short = rid.split("/")[-1]
        ref_url = (f"https://api.openalex.org/works/{short}"
                   f"?select=id,title,authorships,publication_year,"
                   f"primary_location,doi,cited_by_count&mailto={EMAIL}")
        try:
            r = OA.get(ref_url, timeout=10)
            data = r.json()
            if data.get("title"):
                refs.append(data)
        except Exception:
            pass
        time.sleep(0.05)
    return refs


def format_work(w: dict) -> dict:
    """Flatten an OA work record to a simple dict."""
    authors = [a.get("author", {}).get("display_name") or ""
               for a in w.get("authorships", [])[:6]]
    venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    return {
        "title": w.get("title") or "",
        "authors": authors,
        "year": w.get("publication_year"),
        "venue": venue,
        "doi": w.get("doi") or "",
        "cited_by_count": w.get("cited_by_count", 0),
        "oa_id": w.get("id") or "",
    }


# ── Semantic Scholar helpers ──────────────────────────────────────────────────

S2_BASE = "https://api.semanticscholar.org/graph/v1"
S2_FIELDS = "title,authors,year,venue,externalIds,citationCount"


def s2_search(title: str, authors: list[str], limit=5) -> list[dict]:
    params = {"query": title, "limit": limit, "fields": S2_FIELDS}
    try:
        r = S2.get(f"{S2_BASE}/paper/search", params=params, timeout=15)
        r.raise_for_status()
        results = r.json().get("data", [])
    except Exception:
        return []
    # Score by author match
    def author_score(p):
        names = " ".join(a.get("name", "").lower() for a in p.get("authors", []))
        return sum(1 for a in authors if a.split()[-1].lower() in names)
    results.sort(key=author_score, reverse=True)
    return results


def s2_by_doi(doi: str) -> dict | None:
    if not doi:
        return None
    doi_clean = doi.replace("https://doi.org/", "")
    try:
        r = S2.get(f"{S2_BASE}/paper/DOI:{doi_clean}",
                   params={"fields": S2_FIELDS}, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def s2_citing(s2_id: str, limit=50) -> list[dict]:
    fields = "title,authors,year,venue,externalIds,citationCount"
    try:
        r = S2.get(f"{S2_BASE}/paper/{s2_id}/citations",
                   params={"fields": fields, "limit": limit}, timeout=15)
        r.raise_for_status()
        return [e.get("citingPaper", {}) for e in r.json().get("data", [])]
    except Exception:
        return []


def s2_references(s2_id: str, limit=100) -> list[dict]:
    fields = "title,authors,year,venue,externalIds,citationCount"
    try:
        r = S2.get(f"{S2_BASE}/paper/{s2_id}/references",
                   params={"fields": fields, "limit": limit}, timeout=15)
        r.raise_for_status()
        return [e.get("citedPaper", {}) for e in r.json().get("data", [])]
    except Exception:
        return []


def format_s2_work(p: dict) -> dict:
    authors = [a.get("name") or "" for a in p.get("authors", [])[:6]]
    ext = p.get("externalIds") or {}
    doi = ext.get("DOI") or ""
    return {
        "title": p.get("title") or "",
        "authors": authors,
        "year": p.get("year"),
        "venue": p.get("venue") or "",
        "doi": f"https://doi.org/{doi}" if doi else "",
        "cited_by_count": p.get("citationCount") or 0,
        "s2_id": p.get("paperId") or "",
    }


# ── Main ──────────────────────────────────────────────────────────────────────

PHILOSOPHY_LAW_KW = {
    "negligence", "culpability", "criminal law", "tort", "liability",
    "moral responsibility", "recklessness", "blameworthiness", "mens rea",
    "excuse", "justification", "intentional", "wrongdoing", "harm",
    "punishment", "legal", "philosophy", "ethics", "fault",
    "corrective justice", "strict liability", "criminal", "penal",
    "wrongful", "omission", "causation", "volition",
}

def is_relevant(title: str, venue: str) -> bool:
    combined = (title + " " + venue).lower()
    return any(kw in combined for kw in PHILOSOPHY_LAW_KW)


def main():
    all_data = {}
    seen_oa_ids = set()

    for paper in CORPUS:
        stem = paper["stem"]
        title = paper["title"]
        authors = paper["authors"]
        oa_id = paper.get("oa_id")
        doi = paper.get("doi")
        note = paper.get("note")

        print(f"\n{'='*60}")
        print(f"{stem}")
        if note:
            print(f"  Note: {note}")

        entry = {
            "stem": stem,
            "title": title,
            "note": note,
            "oa": {},
            "s2": {},
        }

        # ── OpenAlex ──────────────────────────────────────────────
        skip_oa_fetch = oa_id and oa_id in seen_oa_ids

        if oa_id and not skip_oa_fetch:
            seen_oa_ids.add(oa_id)
            print(f"  OpenAlex ID: {oa_id}")

            # Citing works
            print("  Fetching OA citing works...", end=" ", flush=True)
            citers = oa_citing_works(oa_id)
            relevant_citers = [c for c in citers
                               if is_relevant(c.get("title",""),
                                              ((c.get("primary_location") or {}).get("source") or {}).get("display_name",""))]
            print(f"{len(citers)} total, {len(relevant_citers)} relevant")
            entry["oa"]["citing"] = [format_work(c) for c in relevant_citers]

            # Referenced works
            print("  Fetching OA references...", end=" ", flush=True)
            refs = oa_referenced_works(oa_id)
            print(f"{len(refs)} found")
            entry["oa"]["references"] = [format_work(r) for r in refs]

        elif skip_oa_fetch:
            print("  [OA] Skipping — same record as previous paper")
            entry["oa"]["note"] = "Same OA record as previous chapter; data not re-fetched"
        else:
            print("  [OA] No ID available — will try title search")
            results = oa_search_by_title(title, authors)
            if results:
                best = results[0]
                entry["oa"]["matched_title"] = best.get("title")
                entry["oa"]["matched_id"] = best.get("id")
                print(f"  OA title match: {best.get('title')} ({best.get('publication_year')})")
            else:
                print("  OA: no match found")

        time.sleep(0.5)

        # ── Semantic Scholar ──────────────────────────────────────
        s2_paper = None
        if doi:
            s2_paper = s2_by_doi(doi)
            if s2_paper:
                print(f"  S2 DOI match: {s2_paper.get('title')}")
        if not s2_paper:
            s2_results = s2_search(title, authors)
            if s2_results:
                s2_paper = s2_results[0]
                print(f"  S2 title match: {s2_paper.get('title')} ({s2_paper.get('year')})")

        if s2_paper:
            s2_id = s2_paper.get("paperId")
            entry["s2"]["paper_id"] = s2_id
            entry["s2"]["matched_title"] = s2_paper.get("title")

            print("  Fetching S2 citing works...", end=" ", flush=True)
            s2_citers = s2_citing(s2_id)
            relevant_s2 = [p for p in s2_citers
                           if is_relevant(p.get("title",""), p.get("venue",""))]
            print(f"{len(s2_citers)} total, {len(relevant_s2)} relevant")
            entry["s2"]["citing"] = [format_s2_work(p) for p in relevant_s2]

            print("  Fetching S2 references...", end=" ", flush=True)
            s2_refs = s2_references(s2_id)
            print(f"{len(s2_refs)} found")
            entry["s2"]["references"] = [format_s2_work(p) for p in s2_refs]
        else:
            print("  S2: no match found")
            entry["s2"]["note"] = "Not found in Semantic Scholar"

        time.sleep(1.0)
        all_data[stem] = entry

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)

    print(f"\n\nData written to: {OUTPUT_PATH}")
    print("Run render_citation_report.py to produce the Markdown report.")


if __name__ == "__main__":
    main()

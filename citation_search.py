"""
citation_search.py
For each paper in our corpus, query OpenAlex to find:
  - the paper's own metadata (DOI, year, venue)
  - works it references (outgoing citations)
  - works that cite it (incoming citations)
Writes a Markdown report: citation_report.md
"""

import requests
import json
import time
import os

FOLDER = os.path.dirname(os.path.abspath(__file__))
REPORT_PATH = os.path.join(FOLDER, "citation_report.md")
EMAIL = os.environ.get("CROSSREF_MAILTO", "you@example.com")

PAPERS = [
    "The Innocence of Negligence",
    "Against Negligence Liability",
    "Justification and Excuse",
    # Book chapters — search by title fragment
    "The Essence of Culpability",
    "Negligence",  # Alexander & Ferzan Ch.3 — will disambiguate by author
]

# Author filters to help disambiguation
AUTHOR_HINTS = {
    "Negligence": "Alexander",  # Ch.3 is by Alexander & Ferzan
}


SESSION = requests.Session()
_VERIFY_TLS = os.environ.get("INSECURE_TLS", "") not in ("1", "true", "True")  # verify TLS unless user opts out
SESSION.verify = _VERIFY_TLS  # macOS Python 3.14 SSL cert issue workaround
SESSION.headers.update({"User-Agent": f"mailto:{EMAIL}"})
import urllib3
import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
def _get(url: str, timeout: int = 15) -> dict:
    r = SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def openalex_search(title: str, author_hint: str = None, max_results: int = 5):
    url = (
        f"https://api.openalex.org/works"
        f"?search={requests.utils.quote(title)}"
        f"&per-page={max_results}"
        f"&mailto={EMAIL}"
    )
    data = _get(url)
    results = data.get("results", [])
    if author_hint:
        filtered = [
            w for w in results
            if any(
                author_hint.lower() in (a.get("author", {}).get("display_name") or "").lower()
                for a in w.get("authorships", [])
            )
        ]
        if filtered:
            results = filtered
    return results


def get_work_details(openalex_id: str):
    url = f"https://api.openalex.org/works/{openalex_id}?mailto={EMAIL}"
    return _get(url)


def get_referenced_works(openalex_id: str):
    work = get_work_details(openalex_id)
    return work.get("referenced_works", [])


def get_citing_works(openalex_id: str, max_results: int = 30):
    short_id = openalex_id.split("/")[-1]
    url = (
        f"https://api.openalex.org/works"
        f"?filter=cites:{short_id}"
        f"&per-page={max_results}"
        f"&sort=cited_by_count:desc"
        f"&mailto={EMAIL}"
    )
    data = _get(url)
    return data.get("results", [])


def format_work(w: dict, indent: str = "  ") -> str:
    title = w.get("title") or "(no title)"
    year = w.get("publication_year") or "?"
    authors = ", ".join(
        a.get("author", {}).get("display_name") or "?"
        for a in w.get("authorships", [])[:4]
    )
    venue = (w.get("primary_location") or {}).get("source") or {}
    venue_name = venue.get("display_name") or ""
    doi = w.get("doi") or ""
    cited_by = w.get("cited_by_count", 0)
    line = f"{indent}- **{title}** — {authors} ({year})"
    if venue_name:
        line += f", *{venue_name}*"
    if cited_by:
        line += f" [cited by {cited_by}]"
    if doi:
        line += f"\n{indent}  {doi}"
    return line


def fetch_work_stub(oa_id: str) -> dict:
    """Fetch minimal metadata for a referenced work ID."""
    url = f"https://api.openalex.org/works/{oa_id}?select=id,title,authorships,publication_year,primary_location,doi,cited_by_count&mailto={EMAIL}"
    try:
        return _get(url, timeout=10)
    except Exception:
        return {}


def main():
    lines = ["# Citation Report — Negligence Corpus\n",
             f"Generated via OpenAlex. Papers queried: {len(PAPERS)}\n",
             "---\n"]

    for paper_title in PAPERS:
        author_hint = AUTHOR_HINTS.get(paper_title)
        print(f"\nSearching: '{paper_title}' ...", flush=True)
        lines.append(f"\n## {paper_title}\n")

        results = openalex_search(paper_title, author_hint=author_hint, max_results=5)
        if not results:
            lines.append("_No results found in OpenAlex._\n")
            continue

        # Take top match
        match = results[0]
        oa_id = match["id"]
        print(f"  Matched: {match.get('title')} ({match.get('publication_year')})")
        lines.append("### Matched paper\n")
        lines.append(format_work(match, indent="") + "\n")

        # --- Works cited by this paper ---
        lines.append("\n### References (works this paper cites)\n")
        ref_ids = get_referenced_works(oa_id)
        print(f"  References: {len(ref_ids)} found", flush=True)
        if ref_ids:
            # Fetch details for up to 40 refs
            for rid in ref_ids[:40]:
                time.sleep(0.05)
                stub = fetch_work_stub(rid)
                if stub.get("title"):
                    lines.append(format_work(stub) + "\n")
        else:
            lines.append("_Reference list not available in OpenAlex for this work._\n")

        # --- Works citing this paper ---
        lines.append("\n### Cited by (top papers that cite this work)\n")
        citers = get_citing_works(oa_id, max_results=30)
        print(f"  Cited by: {len(citers)} found", flush=True)
        if citers:
            for w in citers:
                lines.append(format_work(w) + "\n")
        else:
            lines.append("_No citing works found._\n")

        lines.append("\n---\n")
        time.sleep(0.5)

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nReport written to: {REPORT_PATH}")


if __name__ == "__main__":
    main()

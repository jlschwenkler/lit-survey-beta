"""
fetch_pdfs.py
For every score-4 and score-5 paper in citation_graph.json, attempts to
find and download an open-access PDF using:
  1. OpenAlex best_oa_location
  2. Unpaywall API (by DOI)
  3. Semantic Scholar open-access PDF URL

Downloads to pdfs/  (score-5) and pdfs/score4/  (score-4).
Writes fetch_report.md summarising what was found, what needs manual retrieval.

Usage:
  python fetch_pdfs.py              # all score 4+5
  python fetch_pdfs.py --score 5   # score 5 only
"""

import ssl
import os as _os  # TLS verification on by default; opt out with INSECURE_TLS=1
if _os.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    ssl._create_default_https_context = ssl._create_unverified_context

import argparse, json, os, re, time, hashlib
import requests, urllib3

import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
FOLDER      = os.path.dirname(os.path.abspath(__file__))
READING     = os.path.join(FOLDER, "reading")   # PDFs you open to read live here
GRAPH_PATH  = os.path.join(FOLDER, "citation_graph.json")
PDF_DIR5    = os.path.join(READING, "pdfs")             # score-5 PDFs
PDF_DIR4    = os.path.join(READING, "pdfs", "score4")   # score-4 PDFs
REPORT_PATH = os.path.join(FOLDER, "fetch_report.md")   # diagnostic, local artifact
CACHE_PATH  = os.path.join(FOLDER, "fetch_cache.json")

EMAIL = os.environ.get("CROSSREF_MAILTO", "you@example.com")

os.makedirs(PDF_DIR5, exist_ok=True)
os.makedirs(PDF_DIR4, exist_ok=True)

S = requests.Session()
_VERIFY_TLS = os.environ.get("INSECURE_TLS", "") not in ("1", "true", "True")  # verify TLS unless user opts out
S.verify = _VERIFY_TLS
S.headers["User-Agent"] = f"mailto:{EMAIL}"

def norm_doi(d):
    if not d: return ""
    return str(d).lower().strip().lstrip("https://doi.org/")

def safe_filename(title, authors, year, maxlen=80):
    """Make a filesystem-safe filename stem."""
    author_part = ""
    if authors:
        last = (authors[0] or "").split()[-1] if authors else ""
        author_part = re.sub(r"[^a-zA-Z]", "", last).upper()
    title_part = re.sub(r"[^a-zA-Z0-9 ]", "", title or "")
    title_part = "_".join(title_part.split()[:8])
    stem = f"{author_part}_{year}_{title_part}" if author_part else f"{year}_{title_part}"
    return stem[:maxlen]

def oa_pdf_url(oa_id):
    """Query OpenAlex for best OA location PDF URL."""
    if not oa_id:
        return None
    wid = oa_id.replace("https://openalex.org/", "")
    try:
        r = S.get(f"https://api.openalex.org/works/{wid}",
                  params={"select": "best_oa_location,open_access"},
                  timeout=15)
        if r.ok:
            data = r.json()
            loc = data.get("best_oa_location") or {}
            url = loc.get("pdf_url") or loc.get("landing_page_url")
            # Only return if it's a direct PDF
            if url and url.lower().endswith(".pdf"):
                return url
            # Also check open_access
            oa = data.get("open_access") or {}
            oaurl = oa.get("oa_url") or ""
            if oaurl.lower().endswith(".pdf"):
                return oaurl
    except Exception:
        pass
    return None

def unpaywall_pdf_url(doi):
    """Query Unpaywall for best OA PDF URL."""
    if not doi:
        return None
    doi = norm_doi(doi)
    try:
        r = S.get(f"https://api.unpaywall.org/v2/{doi}",
                  params={"email": EMAIL}, timeout=15)
        if r.ok:
            data = r.json()
            loc = data.get("best_oa_location") or {}
            url = loc.get("url_for_pdf") or loc.get("url")
            if url and url.lower().endswith(".pdf"):
                return url
            # Try all locations
            for loc in (data.get("oa_locations") or []):
                u = loc.get("url_for_pdf") or ""
                if u.lower().endswith(".pdf"):
                    return u
    except Exception:
        pass
    return None

def s2_pdf_url(s2_id):
    """Query Semantic Scholar for open access PDF URL."""
    if not s2_id:
        return None
    try:
        r = S.get(f"https://api.semanticscholar.org/graph/v1/paper/{s2_id}",
                  params={"fields": "openAccessPdf"}, timeout=15)
        if r.ok:
            data = r.json()
            oapdf = data.get("openAccessPdf") or {}
            url = oapdf.get("url") or ""
            if url.lower().endswith(".pdf"):
                return url
    except Exception:
        pass
    return None

def download_pdf(url, dest_path):
    """Download a PDF to dest_path. Returns (success, bytes_written)."""
    try:
        r = S.get(url, timeout=30, stream=True, allow_redirects=True)
        if r.status_code != 200:
            return False, 0
        content_type = r.headers.get("content-type", "")
        if "pdf" not in content_type and not url.lower().endswith(".pdf"):
            # Peek at first bytes
            first = next(r.iter_content(512), b"")
            if not first.startswith(b"%PDF"):
                return False, 0
            with open(dest_path, "wb") as f:
                f.write(first)
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            return True, os.path.getsize(dest_path)
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        size = os.path.getsize(dest_path)
        if size < 5000:  # suspiciously small — probably not a real PDF
            os.remove(dest_path)
            return False, 0
        return True, size
    except Exception:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False, 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--score", type=int, default=4,
                        help="Minimum score to fetch (default 4)")
    parser.add_argument("--only-missing", action="store_true",
                        help="Skip papers already downloaded")
    args = parser.parse_args()

    # Load graph
    with open(GRAPH_PATH) as f:
        g = json.load(f)
    nodes  = g["nodes"]
    scores = g["scores"]

    # Load cache
    cache = {}
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            cache = json.load(f)

    # Collect candidates
    candidates = []
    for nid, node in nodes.items():
        sc = (scores.get(nid) or {}).get("score", 0)
        if sc >= args.score:
            candidates.append((sc, node.get("citations") or 0, nid, node))
    candidates.sort(key=lambda x: (-x[0], -x[1]))

    print(f"Candidates (score >= {args.score}): {len(candidates)}")

    results = []   # (score, title, authors, year, status, path, url)
    downloaded = 0
    already_have = 0
    not_found = 0

    for sc, cit, nid, node in candidates:
        title   = node.get("title") or ""
        authors = node.get("authors") or []
        year    = str(node.get("year") or "")
        oa_id   = node.get("oa_id") or ""
        s2_id   = node.get("s2_id") or ""
        doi     = norm_doi(node.get("doi") or "")

        stem     = safe_filename(title, authors, year)
        pdf_dir  = PDF_DIR5 if sc == 5 else PDF_DIR4
        dest     = os.path.join(pdf_dir, stem + ".pdf")

        print(f"\n[{sc}] {title[:60]}")

        # Already downloaded?
        if os.path.exists(dest) and os.path.getsize(dest) > 5000:
            print(f"  already have: {os.path.basename(dest)}")
            already_have += 1
            results.append((sc, title, authors, year, "have", dest, ""))
            continue

        # Check cache for known-unfindable
        cache_key = doi or oa_id or nid
        if cache_key in cache and cache[cache_key] == "not_found":
            print(f"  cached: not found")
            not_found += 1
            results.append((sc, title, authors, year, "not_found", "", ""))
            continue

        # Try to find PDF URL
        pdf_url = None

        # 1. OpenAlex
        if oa_id and not pdf_url:
            pdf_url = oa_pdf_url(oa_id)
            if pdf_url:
                print(f"  OA url: {pdf_url[:70]}")
            time.sleep(0.1)

        # 2. Unpaywall
        if doi and not pdf_url:
            pdf_url = unpaywall_pdf_url(doi)
            if pdf_url:
                print(f"  Unpaywall url: {pdf_url[:70]}")
            time.sleep(0.1)

        # 3. Semantic Scholar
        if s2_id and not pdf_url:
            pdf_url = s2_pdf_url(s2_id)
            if pdf_url:
                print(f"  S2 url: {pdf_url[:70]}")
            time.sleep(0.3)

        if not pdf_url:
            print(f"  not found (no OA PDF)")
            cache[cache_key] = "not_found"
            not_found += 1
            results.append((sc, title, authors, year, "not_found", "", ""))
            continue

        # Download
        print(f"  downloading...", end=" ", flush=True)
        ok, size = download_pdf(pdf_url, dest)
        if ok:
            print(f"ok ({size//1024} KB) -> {os.path.basename(dest)}")
            downloaded += 1
            cache[cache_key] = dest
            results.append((sc, title, authors, year, "downloaded", dest, pdf_url))
        else:
            print(f"failed (bad response)")
            cache[cache_key] = "not_found"
            not_found += 1
            results.append((sc, title, authors, year, "not_found", "", pdf_url))

        time.sleep(0.3)

    # Save cache
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)

    # Write report
    have_list     = [r for r in results if r[4] in ("have", "downloaded")]
    missing_list  = [r for r in results if r[4] == "not_found"]

    lines = [
        "# PDF Fetch Report\n",
        f"Candidates (score >= {args.score}): {len(candidates)}  \n",
        f"Already had / downloaded: {already_have + downloaded}  \n",
        f"  — Downloaded this run: {downloaded}  \n",
        f"  — Already on disk: {already_have}  \n",
        f"Not found (need manual retrieval): {not_found}  \n",
        "\n---\n",
        "## Downloaded / Available\n",
    ]
    for sc, title, authors, year, status, path, url in sorted(have_list, key=lambda x: (-x[0], x[1])):
        auth_str = ", ".join(authors[:2])
        flag = "✓" if status == "downloaded" else "•"
        lines.append(f"{flag} [{sc}★] **{title}** — {auth_str} ({year})  \n")
        lines.append(f"  `{os.path.basename(path)}`  \n\n")

    lines.append("\n## Needs Manual Retrieval\n")
    lines.append("*(Check UT Austin library, PhilPapers, or author websites)*\n\n")
    for sc, title, authors, year, status, path, url in sorted(missing_list, key=lambda x: (-x[0], x[1])):
        auth_str = ", ".join(authors[:2])
        lines.append(f"- [{sc}★] **{title}** — {auth_str} ({year})  \n")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("".join(lines))

    print(f"\n{'='*55}")
    print(f"Downloaded this run : {downloaded}")
    print(f"Already on disk     : {already_have}")
    print(f"Need manual retrieval: {not_found}")
    print(f"Report: {REPORT_PATH}")

if __name__ == "__main__":
    main()

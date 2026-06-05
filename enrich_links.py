"""
enrich_links.py  —  Find a real, API-verified link for every paper that has no
DOI in the citation graph, so the literature table can hyperlink its title.

Why: ~1/6 of the deduped corpus has no DOI captured during the crawl. Many of
those *do* have a DOI that OpenAlex/S2 know about (just never stored in our
graph), and the rest usually have a genuine publisher / institution / record
landing page. This script recovers those.

HARD RULE (project convention): never fabricate a URL from model memory. Every
link written here is returned by an API call for *this* item, or it is a
PhilPapers *search* URL (which is honest — it lands on a results page for the
title, not a guessed permalink). Items with nothing verifiable are left blank.

Resolution order per no-DOI paper (first hit wins):
  1. Recover a DOI            OpenAlex .doi  ->  S2 externalIds.DOI
                             (also written back into citation_graph.json so other
                              reports benefit; flagged doi_source="recovered")
  2. Publisher landing page   OpenAlex primary_location.landing_page_url
  3. Open-access full text     OpenAlex best_oa_location (landing, then pdf)
                             ->  S2 openAccessPdf.url
  4. Record page              semanticscholar.org/paper/<id>
  5. PhilPapers search         (philosophy work types only; search URL, labelled)
  6. (nothing) -> left blank

Output:
  links.json   { row_key : {url, kind, doi_recovered} }   <- read by build_lit_table.py
  Also backfills recovered DOIs into citation_graph.json (doi + doi_source).

Re-runnable: skips keys already in links.json unless --refresh.

Usage:
  python enrich_links.py
  python enrich_links.py --refresh   # ignore cache
"""

import ssl
import os as _os  # TLS verification on by default; opt out with INSECURE_TLS=1
if _os.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    ssl._create_default_https_context = ssl._create_unverified_context

import argparse, json, os, re, time
from urllib.parse import quote_plus
import requests, urllib3

import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
FOLDER      = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH  = os.path.join(FOLDER, "citation_graph.json")
MATRIX_PATH = os.path.join(FOLDER, "engagement_matrix.json")
LINKS_PATH  = os.path.join(FOLDER, "links.json")     # row_key -> link record

EMAIL = os.environ.get("CROSSREF_MAILTO", "you@example.com")

S = requests.Session()
_VERIFY_TLS = os.environ.get("INSECURE_TLS", "") not in ("1", "true", "True")  # verify TLS unless user opts out
S.verify = _VERIFY_TLS
S.headers["User-Agent"] = f"mailto:{EMAIL}"

SRC_RANK = {"full": 2, "abstract": 1, "title": 0}
# work types we treat as "philosophy" for the PhilPapers search fallback
PHIL_TYPES = {"article", "book", "book-chapter", "chapter", "dissertation"}


# ----- helpers mirrored from build_lit_table.py so the key set matches -----
def dedup(rows):
    best = {}
    for r in rows:
        t = r["title"].lower().strip()
        cur = best.get(t)
        cand = (SRC_RANK.get(r.get("text_source"), 0), r.get("score", 0))
        if cur is None or cand > (SRC_RANK.get(cur.get("text_source"), 0),
                                  cur.get("score", 0)):
            best[t] = r
    return list(best.values())


def norm_doi(d):
    if not d:
        return None
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", str(d).strip(), flags=re.I)
    return d[4:] if d.lower().startswith("doi:") else d


def existing_doi(key, nodes):
    n = nodes.get(key) or {}
    if n.get("doi"):
        return norm_doi(n["doi"])
    if key.startswith("doi:"):
        return key[4:]
    return None


# ----- API lookups (each returns data only for the queried item) -----
def openalex_record(oa_id):
    if not oa_id:
        return None
    wid = oa_id.replace("https://openalex.org/", "")
    try:
        r = S.get(f"https://api.openalex.org/works/{wid}",
                  params={"select": "id,doi,title,type,primary_location,"
                                    "best_oa_location,open_access",
                          "mailto": EMAIL}, timeout=20)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


def s2_record(s2_id):
    if not s2_id:
        return None
    try:
        r = S.get(f"https://api.semanticscholar.org/graph/v1/paper/{s2_id}",
                  params={"fields": "title,externalIds,openAccessPdf,url"},
                  timeout=20)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


def philpapers_search_url(title):
    """An honest fallback: a PhilPapers *search* URL for the title. This is a
    constructed query string, not a fabricated record permalink — it lands on a
    results page that lists candidate matches for the human to confirm."""
    if not title:
        return None
    return "https://philpapers.org/s/" + quote_plus(title)


def resolve(key, node, row):
    """Return (link_record_or_None). link_record = {url, kind, doi_recovered}."""
    oa_id = (node or {}).get("oa_id")
    s2_id = (node or {}).get("s2_id")
    oa = openalex_record(oa_id) if oa_id else None
    time.sleep(0.1)

    # 1. recover a DOI -------------------------------------------------------
    doi = norm_doi((oa or {}).get("doi"))
    s2 = None
    if not doi and s2_id:
        s2 = s2_record(s2_id)
        time.sleep(0.3)
        doi = norm_doi(((s2 or {}).get("externalIds") or {}).get("DOI"))
    if doi:
        return {"url": "https://doi.org/" + doi, "kind": "doi",
                "doi_recovered": doi}

    # 2. publisher / institution landing page (primary_location) -------------
    pl = (oa or {}).get("primary_location") or {}
    landing = pl.get("landing_page_url")
    if landing and "doi.org" not in landing:
        return {"url": landing, "kind": "landing", "doi_recovered": None}

    # 3. open-access full text ----------------------------------------------
    boa = (oa or {}).get("best_oa_location") or {}
    oa_landing = boa.get("landing_page_url")
    oa_pdf = boa.get("pdf_url")
    oa_url = ((oa or {}).get("open_access") or {}).get("oa_url")
    for u in (oa_landing, oa_pdf, oa_url):
        if u and "doi.org" not in u:
            return {"url": u, "kind": "oa", "doi_recovered": None}

    if s2 is None and s2_id:
        s2 = s2_record(s2_id)
        time.sleep(0.3)
    s2_pdf = ((s2 or {}).get("openAccessPdf") or {}).get("url")
    if s2_pdf:
        return {"url": s2_pdf, "kind": "oa", "doi_recovered": None}

    # 4. semantic scholar record page ---------------------------------------
    s2_page = (s2 or {}).get("url")
    if s2_page:
        return {"url": s2_page, "kind": "record", "doi_recovered": None}

    # 5. PhilPapers search (philosophy work types only) ----------------------
    wt = (row or {}).get("work_type") or (node or {}).get("work_type")
    if wt in PHIL_TYPES:
        u = philpapers_search_url(row.get("title") or (node or {}).get("title"))
        if u:
            return {"url": u, "kind": "philpapers_search", "doi_recovered": None}

    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="re-resolve all no-DOI items, ignoring the cache")
    args = ap.parse_args()

    graph = json.load(open(GRAPH_PATH))
    nodes = graph["nodes"]
    mat = json.load(open(MATRIX_PATH))
    rows = dedup(mat["rows"])

    links = {}
    if os.path.exists(LINKS_PATH) and not args.refresh:
        links = json.load(open(LINKS_PATH))

    todo = [r for r in rows
            if not existing_doi(r["key"], nodes)
            and (args.refresh or r["key"] not in links)]
    print(f"{len(rows)} deduped papers | "
          f"{sum(1 for r in rows if not existing_doi(r['key'], nodes))} lack a DOI | "
          f"{len(todo)} to resolve this run")

    kinds = {}
    recovered_dois = 0
    for i, r in enumerate(todo, 1):
        key = r["key"]
        node = nodes.get(key)
        rec = resolve(key, node, r)
        if rec:
            links[key] = rec
            kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
            # write a recovered DOI back into the graph node so other reports gain it
            if rec.get("doi_recovered") and node is not None:
                node["doi"] = rec["doi_recovered"]
                node["doi_source"] = "recovered"
                recovered_dois += 1
            tag = rec["kind"]
        else:
            links[key] = None       # remember we tried; nothing found
            kinds["none"] = kinds.get("none", 0) + 1
            tag = "—"
        print(f"  [{i}/{len(todo)}] {tag:<16} {(r['title'] or '')[:62]}")

    # save
    json.dump(links, open(LINKS_PATH, "w"), ensure_ascii=False, indent=2)
    if recovered_dois:
        json.dump(graph, open(GRAPH_PATH, "w"), ensure_ascii=False)

    print("\n" + "=" * 56)
    print("link kinds this run:", ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    print(f"DOIs recovered into graph: {recovered_dois}")
    print(f"links.json: {LINKS_PATH}")
    print("Now re-run:  python build_lit_table.py")


if __name__ == "__main__":
    main()

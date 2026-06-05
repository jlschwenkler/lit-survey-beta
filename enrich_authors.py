#!/usr/bin/env python3
"""
Stage 2.7 — Recover authors for author-less nodes.

WHY: a node with no `authors` is a quiet liability that defeats THREE later guards:
  • Stage 3.5 `consolidate_nodes.py` surname guard (can't merge / can't tell apart),
  • `ingest_abstract_html.py` fuzzy-title match guard (an author-less target waved a
    0.76 fuzzy title through → "The Form of Practical Knowledge" matched the wrong
    paper, 2026-06-04), and
  • PhilPapers `@authors` recovery queries (the paper can't be found by author).
So author-less nodes silently mis-merge, mis-abstract, and evade recovery. This
stage fills authors from the same APIs the abstract backfill uses, and FLAGS the
residue (junk footnote-fragments vs. genuinely-thin-metadata) for a human.

CONTRACT:
  • Scope default = author-less nodes that are matrix-eligible (relevance ≥ 4);
    `--all` = every author-less node in the graph.
  • Sources, in order: OpenAlex (oa_id or DOI) → Crossref (DOI) → S2 batch (DOI).
    First non-empty author list wins; tag `authors_source`.
  • Writes nothing without `--commit` (dry run prints + writes the report only).
    `--commit` snapshots the graph first.
  • Flags likely-JUNK titles (footnote fragments captured as a title: leading
    digits / "if T is", "105-127)", lone punctuation, etc.) — these have no author
    to find and should be cleaned up, not chased.

NOTE: authors-only enrichment never changes scores; no re-score needed. But run it
BEFORE Stage 3.5 consolidation and before any PhilPapers `@authors` recovery, so
those stages see the recovered surnames. (Hence Stage 2.7, early.)

USAGE:
  python3 enrich_authors.py            # dry run, matrix-eligible scope
  python3 enrich_authors.py --all      # whole graph
  python3 enrich_authors.py --commit   # write (snapshots first)
"""
import ssl
import os as _os  # TLS verification on by default; opt out with INSECURE_TLS=1
if _os.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    ssl._create_default_https_context = ssl._create_unverified_context
import argparse, json, os, re, time
import requests, urllib3
import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
FOLDER     = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH = os.path.join(FOLDER, "citation_graph.json")
MATRIX_PATH= os.path.join(FOLDER, "engagement_matrix.json")
REPORT     = os.path.join(FOLDER, "enrich_authors_report.md")
ARCHIVE    = os.path.join(FOLDER, "_archive")
EMAIL = os.environ.get("CROSSREF_MAILTO", "you@example.com")
S2_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()

_VERIFY_TLS = os.environ.get("INSECURE_TLS", "") not in ("1", "true", "True")  # verify TLS unless user opts out
S = requests.Session(); S.verify = _VERIFY_TLS
S.headers["User-Agent"] = f"mailto:{EMAIL}"

def norm_doi(d):
    if not d: return None
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(d).strip(), flags=re.I).replace("doi:", "")

def authorless(n):
    a = n.get("authors") or []
    return (not a) or all(not str(x).strip() for x in a)

# A title that is really a footnote/citation fragment, not a work title — no author
# exists to find. Heuristic, used only to FLAG (never to delete).
_JUNK = [
    re.compile(r"^\s*\d"),                       # starts with a digit (page range, "105-127)")
    re.compile(r"^\s*if\s+[a-z]\b", re.I),       # "If T is S's own..."
    re.compile(r"^\s*a mental action that is", re.I),
    re.compile(r"\b(pp?\.|vol\.)\s*\d", re.I),   # embedded page/volume citation
    re.compile(r"^[\W_]+$"),                      # all punctuation
    re.compile(r";\s*and\b", re.I),              # compound title mashing two works ("…); and Contro…")
]
def looks_junk(node_or_title):
    """Accepts a node dict (preferred) or a bare title. A node with a hand-set
    junk_title flag is always junk (respects manual cleanup decisions)."""
    if isinstance(node_or_title, dict):
        if node_or_title.get("junk_title"):
            return True
        title = node_or_title.get("title")
    else:
        title = node_or_title
    t = (title or "").strip()
    if len(t) < 6: return True
    return any(rx.search(t) for rx in _JUNK)

# ── sources ───────────────────────────────────────────────────────────────────
def _names_from_openalex(authorships):
    out = []
    for a in authorships or []:
        nm = ((a.get("author") or {}).get("display_name") or "").strip()
        if nm: out.append(nm)
    return out

def openalex_authors(oa_id, doi):
    if oa_id:
        url = f"https://api.openalex.org/works/{oa_id.replace('https://openalex.org/','')}"
    elif doi:
        url = f"https://api.openalex.org/works/doi:{doi}"
    else:
        return []
    try:
        r = S.get(url, params={"select": "authorships", "mailto": EMAIL}, timeout=20)
        return _names_from_openalex((r.json() or {}).get("authorships"))
    except Exception:
        return []

def crossref_authors(doi):
    if not doi: return []
    try:
        r = S.get(f"https://api.crossref.org/works/{doi}", params={"mailto": EMAIL}, timeout=20)
        msg = (r.json() or {}).get("message") or {}
        out = []
        for a in msg.get("author") or []:
            nm = " ".join(x for x in (a.get("given"), a.get("family")) if x).strip()
            if nm: out.append(nm)
        return out
    except Exception:
        return []

S2_LOOKUP = {}
def s2_batch_authors(dois):
    out = {}
    dois = [d for d in dois if d]
    if not dois: return out
    hdr = {"x-api-key": S2_KEY} if S2_KEY else {}
    for i in range(0, len(dois), 500):
        chunk = dois[i:i+500]
        ids = [f"DOI:{d}" for d in chunk]
        try:
            r = S.post("https://api.semanticscholar.org/graph/v1/paper/batch",
                       params={"fields": "authors,externalIds"},
                       json={"ids": ids}, headers=hdr, timeout=40)
            if r.status_code != 200:
                print(f"    [S2] HTTP {r.status_code} chunk {i//500+1}; skip"); continue
            for req_doi, item in zip(chunk, r.json()):
                if isinstance(item, dict):
                    nms = [a.get("name","").strip() for a in (item.get("authors") or []) if a.get("name")]
                    if nms: out[req_doi] = nms
        except Exception as e:
            print(f"    [S2] error chunk {i//500+1}: {e}")
        time.sleep(1.0)
    return out

def resolve_authors(node):
    doi = norm_doi(node.get("doi"))
    a = openalex_authors(node.get("oa_id"), doi)
    if a: return a, "openalex"
    a = crossref_authors(doi)
    if a: return a, "crossref"
    if doi and doi in S2_LOOKUP:
        return S2_LOOKUP[doi], "semantic_scholar"
    return [], None

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="scope = every author-less node (default: relevance>=4)")
    ap.add_argument("--commit", action="store_true", help="write authors into the graph (snapshots first)")
    args = ap.parse_args()

    g = json.load(open(GRAPH_PATH))
    N, scores = g["nodes"], g.get("scores", {})
    mkeys = {r["key"] for r in json.load(open(MATRIX_PATH))["rows"]} if os.path.exists(MATRIX_PATH) else set()

    targets = []
    for k, n in N.items():
        if not authorless(n): continue
        if args.all or (scores.get(k) or {}).get("score", 0) >= 4 or k in mkeys:
            targets.append(k)
    print(f"Author-less nodes in scope: {len(targets)}  ({'whole graph' if args.all else 'relevance>=4 / in-matrix'})")

    # S2 batch pre-pass for all DOIs in scope
    global S2_LOOKUP
    S2_LOOKUP = s2_batch_authors([norm_doi(N[k].get("doi")) for k in targets if N[k].get("doi")])

    recovered, junk, stuck = [], [], []
    for k in targets:
        n = N[k]
        if looks_junk(n):
            junk.append(k); continue
        auths, src = resolve_authors(n)
        if auths:
            recovered.append((k, auths, src))
            if args.commit:
                n["authors"] = auths
                n["authors_source"] = src
        else:
            stuck.append(k)
        time.sleep(0.1)

    print(f"  recovered: {len(recovered)} | still-missing: {len(stuck)} | likely-junk titles (flagged, not chased): {len(junk)}")

    # report
    L = ["# Stage 2.7 — author recovery", "",
         f"_scope: {'whole graph' if args.all else 'relevance>=4 / in-matrix'} · "
         f"{len(targets)} author-less node(s)_", "",
         f"recovered **{len(recovered)}** · still-missing **{len(stuck)}** · "
         f"likely-junk titles **{len(junk)}**", ""]
    L.append("## Recovered" + ("  ✅ written" if args.commit else "  (dry run)"))
    for k, a, src in sorted(recovered, key=lambda x: x[0]):
        L.append(f"- `{k}` ← **{'; '.join(a[:4])}**  _( {src} )_  · {N[k].get('title','')[:60]}")
    L.append("\n## Still missing (have title/DOI but no API authors — hand-pull or accept)")
    for k in sorted(stuck):
        L.append(f"- `{k}`  {N[k].get('year')}  doi={norm_doi(N[k].get('doi')) or '—'}  · {N[k].get('title','')[:60]}")
    L.append("\n## Likely-junk titles (footnote/citation fragments — cleanup candidates, NOT chased)")
    for k in sorted(junk):
        L.append(f"- `{k}`  · {N[k].get('title','')[:70]}")
    open(REPORT, "w").write("\n".join(L))
    print(f"report -> {os.path.basename(REPORT)}")

    if not args.commit:
        print("DRY RUN — graph unchanged. Re-run with --commit to write recovered authors.")
        return
    os.makedirs(ARCHIVE, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json.dump(g, open(os.path.join(ARCHIVE, f"citation_graph.backup_pre_enrichauthors_{stamp}.json"), "w"),
              ensure_ascii=False, indent=1)
    json.dump(g, open(GRAPH_PATH, "w"), ensure_ascii=False, indent=1)
    print(f"snapshot {stamp} · wrote {len(recovered)} author lists into the graph.")
    print("Authors-only — no re-score needed. (Run BEFORE consolidate / PhilPapers @authors recovery.)")

if __name__ == "__main__":
    main()

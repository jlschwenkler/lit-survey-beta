"""
crawl_citation_graph.py
Snowball citation-graph crawler for the negligence/responsibility literature.

Starting from seed papers (our corpus + any IDs recovered by parse_references.py),
this script expands the citation graph outward in hops, scoring and filtering
at each stage using Claude to assess abstract relevance.

Architecture (adapted from EAIM harvest_citation_network.py):
  1. Resolve seeds → OA IDs + S2 IDs
  2. Hop N: for each seed, fetch references (backward) + citing papers (forward)
             via S2 primary, OA fallback
  3. Score each new paper: (a) keyword pre-filter to skip obvious misses cheaply,
             then (b) Claude abstract scoring (1-5) for papers that pass
  4. Keep papers scoring ≥ threshold; use as seeds for next hop
  5. Save checkpoint after each hop; safe to resume

Outputs:
  citation_graph.json     — full graph data (all nodes + edges, all hops)
  literature_candidates.md — ranked reading list of discovered papers

Usage:
  python crawl_citation_graph.py              # 2 hops, score threshold 3
  python crawl_citation_graph.py --hops 3 --threshold 4
  python crawl_citation_graph.py --resume     # continue from last checkpoint
"""

import ssl
import os as _os  # TLS verification on by default; opt out with INSECURE_TLS=1
if _os.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    ssl._create_default_https_context = ssl._create_unverified_context

import argparse, json, os, re, time
from collections import defaultdict
import requests, urllib3
from llm_client import call_model

import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
FOLDER     = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH = os.path.join(FOLDER, "citation_graph.json")
REPORT_PATH= os.path.join(FOLDER, "literature_candidates.md")
REF_PATH   = os.path.join(FOLDER, "parsed_references.json")
EMAIL      = os.environ.get("CROSSREF_MAILTO", "you@example.com")

S2     = requests.Session()
_VERIFY_TLS = os.environ.get("INSECURE_TLS", "") not in ("1", "true", "True")  # verify TLS unless user opts out
S2.verify = _VERIFY_TLS
S2.headers["User-Agent"] = f"mailto:{EMAIL}"
OA     = requests.Session()
OA.verify = _VERIFY_TLS
OA.headers["User-Agent"] = f"mailto:{EMAIL}"

# ── Project description for Claude relevance scoring ─────────────────────────
# Edit this if/when the project focus shifts.

PROJECT_DESCRIPTION = """
The project concerns moral and legal responsibility for negligent acts and
omissions. Core themes include:

Legal/criminal theory: the nature of negligence and whether it is a genuine
form of culpability; the distinction between negligence and recklessness;
objective vs. subjective standards of fault; justification and excuse in
criminal and tort law; corrective justice and strict liability; the
reasonable person standard; mens rea; tracing.

Philosophy of action and moral responsibility: attributability vs.
accountability; reasons-responsiveness theories; reactive attitudes and
Strawsonian frameworks; the epistemic condition on responsibility (moral
ignorance, culpable ignorance); voluntariness and control; omissions and
failures to act; free will and its relevance to responsibility; the
quality-of-will approach (Watson, Smith, Arpaly); negligence as a failure
of attention or care.

The literature spans moral philosophy, philosophy of action/mind, criminal
law theory, and tort theory.
"""

# ── Seed paper definitions ────────────────────────────────────────────────────
# These are the papers we already have in the corpus.
# Add OA IDs and S2 IDs as known. The script will attempt to resolve any
# that are missing.

SEED_PAPERS = [
    {
        "stem":   "HURD the innocence of negligence",
        "title":  "The Innocence of Negligence",
        "authors":["Heidi Hurd"],
        "oa_id":  "W3121936627",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "ALEXANDER FERZAN against negligence liability",
        "title":  "Against Negligence Liability",
        "authors":["Larry Alexander", "Kimberly Ferzan"],
        "oa_id":  "W2484803952",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "FERZAN justification and excuse",
        "title":  "Justification and Excuse",
        "authors":["Kimberly Ferzan"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.1093/oxfordhb/9780195314854.003.0010",
    },
    {
        "stem":   "ALEXANDER FERZAN crime and culpability",
        "title":  "Crime and Culpability: A Theory of Criminal Law",
        "authors":["Larry Alexander", "Kimberly Ferzan"],
        "oa_id":  "W606236685",
        "s2_id":  None,
        "doi":    None,
    },
    # Moore & Hurd "Punishing the Awkward..." — closely related, high citation count
    {
        "stem":   "MOORE HURD punishing the awkward",
        "title":  "Punishing the Awkward, the Stupid, the Weak, and the Selfish",
        "authors":["Michael Moore", "Heidi Hurd"],
        "oa_id":  "W2011573935",
        "s2_id":  None,
        "doi":    "10.1007/s11572-011-9114-0",
    },
    # ── Wave 4 seed: Duff "Two Models of Criminal Fault" (2019) ───────────────
    # Central comparative-fault paper (advertence vs. practical-reasoning models);
    # OA/S2 carry no reference list for it, so backward citations come from the
    # parsed footnotes in parsed_references.json.
    {
        "stem":   "DUFF two models of criminal fault",
        "title":  "Two Models of Criminal Fault",
        "authors":["R. A. Duff"],
        "oa_id":  "W2969294810",
        "s2_id":  "504ba6bef9096ac15a2e6395f5c6d80a38a94c39",
        "doi":    "10.1007/s11572-019-09504-w",
    },
    # ── Wave 2 seeds: ethics / philosophy of action ───────────────────────────
    # Added to expand coverage beyond legal theory into moral philosophy,
    # reasons-responsiveness, attributability, and epistemic conditions.
    {
        "stem":   "SMITH responsibility for attitudes",
        "title":  "Responsibility for Attitudes: Activity and Passivity in Mental Life",
        "authors":["Angela M. Smith"],
        "oa_id":  "W2044818626",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "WATSON two faces of responsibility",
        "title":  "Two Faces of Responsibility",
        "authors":["Gary Watson"],
        "oa_id":  "W2137209613",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "ROSEN culpability and ignorance",
        "title":  "Culpability and Ignorance",
        "authors":["Gideon Rosen"],
        "oa_id":  "W1968502652",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "CLARKE omissions agency metaphysics",
        "title":  "Omissions: Agency, Metaphysics, and Responsibility",
        "authors":["Randolph Clarke"],
        "oa_id":  "W646780389",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "SCANLON moral dimensions",
        "title":  "Moral Dimensions: Permissibility, Meaning, Blame",
        "authors":["T.M. Scanlon"],
        "oa_id":  "W1508831339",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "FISCHER RAVIZZA responsibility and control",
        "title":  "Responsibility and Control: A Theory of Moral Responsibility",
        "authors":["John Martin Fischer", "Mark Ravizza"],
        "oa_id":  "W2044908625",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "STRAWSON freedom and resentment",
        "title":  "Freedom and Resentment",
        "authors":["P.F. Strawson"],
        "oa_id":  "W2270772451",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "ZIMMERMAN moral responsibility and ignorance",
        "title":  "Moral Responsibility and Ignorance",
        "authors":["Michael J. Zimmerman"],
        "oa_id":  "W2023270709",
        "s2_id":  None,
        "doi":    None,
    },
    # ── Wave 3 seeds: quality-of-will / reasons-responsiveness ───────────────
    # Manually bumped after noticing these were underscored (score 1-2) due to
    # titles not mentioning negligence/law. Central to the quality-of-will and
    # reasons-responsiveness literature directly relevant to the project.
    {
        "stem":   "ARPALY unprincipled virtue",
        "title":  "Unprincipled Virtue",
        "authors":["Nomy Arpaly"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.1093/0195152042.001.0001",
    },
    {
        "stem":   "ARPALY in praise of desire",
        "title":  "In Praise of Desire",
        "authors":["Nomy Arpaly", "Timothy Schroeder"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.1093/acprof:oso/9780199348169.001.0001",
    },
    {
        "stem":   "WOLF sanity and the metaphysics of responsibility",
        "title":  "Sanity and the Metaphysics of Responsibility",
        "authors":["Susan Wolf"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.1017/cbo9780511625411.003",
    },
    {
        "stem":   "WOLF freedom within reason",
        "title":  "Freedom Within Reason",
        "authors":["Susan Wolf"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.1017/cbo9780511614194.012",
    },
    {
        "stem":   "WATSON agency and answerability",
        "title":  "Agency and Answerability",
        "authors":["Gary Watson"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.1093/acprof:oso/9780199272273.001.0001",
    },
    {
        "stem":   "SCANLON what we owe to each other",
        "title":  "What We Owe to Each Other",
        "authors":["T.M. Scanlon"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.2307/j.ctv134vmrn",
    },
    {
        "stem":   "ROSEN kleinbart the oblivious",
        "title":  "Kleinbart the Oblivious and Other Tales of Ignorance and Responsibility",
        "authors":["Gideon Rosen"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.5840/jphil20081051023",
    },
    {
        "stem":   "LEVY hard luck",
        "title":  "Hard Luck: How Luck Undermines Free Will and Moral Responsibility",
        "authors":["Neil Levy"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.1093/acprof:oso/9780199601387.001.0001",
    },
]


# ── Pre-filter keywords (cheap first pass before Claude) ─────────────────────
# A paper must match at least one term from each group to proceed to Claude scoring.

PHIL_TERMS = re.compile(
    r"\b(moral|ethics|ethical|culpab|blame|blameworthi|responsib|intention|"
    r"reckless|negligent|negligence|excuse|justif|volunt|involunt|mens.rea|"
    r"fault|wrongdo|harm|corrective.justice|punishment|retribut|"
    r"philosophy|philosophical|virtue|character|capacit|omission|"
    r"attributab|answerab|accountab|reactive.attitude|resentment|"
    r"reasons.responsiv|free.will|ignorance|epistemic|agent|agency)\b",
    re.IGNORECASE,
)
LAW_TERMS = re.compile(
    r"\b(law|legal|tort|criminal|liability|defendant|plaintiff|court|"
    r"statute|doctrine|standard|reasonable.person|strict.liability|"
    r"common.law|penal|civil|jury|judge|verdict|objectively)\b",
    re.IGNORECASE,
)
# High-signal philosophy-of-action terms that pass without LAW_TERMS match
PHIL_ACTION_TERMS = re.compile(
    r"\b(attributab|answerab|accountab|reactive.attitude|resentment|"
    r"reasons.responsiv|free.will|blameworthi|culpab|negligence|"
    r"ignorance.*moral|moral.*ignorance|moral.responsib)\b",
    re.IGNORECASE,
)

def keyword_prefilter(title, abstract=""):
    text = f"{title} {abstract}"
    # Pass if: (phil + law) OR (strong phil-action signal alone)
    return (
        (bool(PHIL_TERMS.search(text)) and bool(LAW_TERMS.search(text)))
        or bool(PHIL_ACTION_TERMS.search(text))
    )


# ── Claude relevance scoring ──────────────────────────────────────────────────

SCORE_SYSTEM = f"""You are a relevance filter for an academic literature review.

Project description:
{PROJECT_DESCRIPTION.strip()}

You will receive a paper title and abstract (or just a title if no abstract
is available). Score its relevance to this project on a scale of 1–5:

5 = Central: directly addresses negligence, culpability, fault standards,
    justification/excuse, or voluntariness in criminal/tort law contexts.
4 = Highly relevant: addresses related themes (moral responsibility,
    recklessness, objective standards, corrective justice, strict liability)
    in ways clearly applicable to the project.
3 = Relevant: touches on the project's themes but not as a primary focus,
    or addresses adjacent topics (e.g., causation, harm, omissions) that
    are likely to contain useful material.
2 = Marginally relevant: general moral responsibility or criminal law
    literature without specific connection to negligence or the project's
    themes.
1 = Not relevant: unrelated to the project.

Respond with ONLY a JSON object: {{"score": N, "reason": "one sentence"}}"""


def claude_score(title, abstract=""):
    """Score a paper 1-5 for relevance. Returns (score, reason)."""
    content = f"Title: {title}"
    if abstract:
        content += f"\n\nAbstract: {abstract[:800]}"
    try:
        text = call_model(
            system=SCORE_SYSTEM,
            user=content,
            model="fast",
            max_tokens=80,
        )
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        data = json.loads(text)
        return int(data.get("score", 1)), data.get("reason", "")
    except Exception as e:
        return 1, f"scoring error: {e}"


# ── API helpers ───────────────────────────────────────────────────────────────

S2_BASE    = "https://api.semanticscholar.org/graph/v1"
S2_FIELDS  = "title,abstract,authors,year,venue,externalIds,citationCount,referenceCount"
OA_BASE    = "https://api.openalex.org"
OA_SLEEP   = 0.12
S2_SLEEP   = 0.5

def norm_doi(s):
    if not s:
        return ""
    return str(s).lower().strip().lstrip("https://doi.org/")

def oa_reconstruct_abstract(inv):
    """Reconstruct abstract from OpenAlex inverted index."""
    if not inv:
        return ""
    try:
        length = max(max(v) for v in inv.values()) + 1
        words  = [""] * length
        for word, positions in inv.items():
            for pos in positions:
                if pos < length:
                    words[pos] = word
        return " ".join(words).strip()
    except Exception:
        return ""

def oa_parse_work(w):
    doi     = norm_doi(w.get("doi") or "")
    abstract= oa_reconstruct_abstract(w.get("abstract_inverted_index") or {})
    src     = (w.get("primary_location") or {}).get("source") or {}
    oa_id   = (w.get("id") or "").replace("https://openalex.org/", "")
    authors = [a.get("author",{}).get("display_name","")
               for a in w.get("authorships",[])[:6]]
    return {
        "oa_id":    oa_id,
        "doi":      doi,
        "title":    (w.get("title") or "").strip(),
        "abstract": abstract,
        "year":     w.get("publication_year"),
        "venue":    src.get("display_name",""),
        "authors":  authors,
        "citations":w.get("cited_by_count", 0) or 0,
        "s2_id":    None,
    }

def s2_parse_paper(p):
    ext  = p.get("externalIds") or {}
    doi  = norm_doi(ext.get("DOI",""))
    authors = [a.get("name","") for a in p.get("authors",[])[:6]]
    return {
        "s2_id":    p.get("paperId",""),
        "doi":      doi,
        "title":    (p.get("title") or "").strip(),
        "abstract": (p.get("abstract") or "").strip(),
        "year":     p.get("year"),
        "venue":    p.get("venue",""),
        "authors":  authors,
        "citations":p.get("citationCount") or 0,
        "oa_id":    None,
    }

# ── OA fetchers ───────────────────────────────────────────────────────────────

def oa_get_references(oa_id, max_refs=100):
    url = (f"{OA_BASE}/works/{oa_id}"
           f"?select=referenced_works&mailto={EMAIL}")
    try:
        r = OA.get(url, timeout=15)
        ref_ids = r.json().get("referenced_works",[])
    except Exception:
        return []
    results = []
    for i in range(0, min(len(ref_ids), max_refs), 50):
        batch = ref_ids[i:i+50]
        ids   = "|".join(x.replace("https://openalex.org/","") for x in batch)
        url2  = (f"{OA_BASE}/works?filter=openalex_id:{ids}"
                 f"&select=id,doi,title,publication_year,primary_location,"
                 f"abstract_inverted_index,cited_by_count,authorships"
                 f"&per-page=50&mailto={EMAIL}")
        try:
            r2 = OA.get(url2, timeout=15)
            results.extend(r2.json().get("results",[]))
        except Exception:
            pass
        time.sleep(OA_SLEEP)
    return [oa_parse_work(w) for w in results]

def oa_get_citing(oa_id, max_results=200):
    results = []
    cursor  = "*"
    while len(results) < max_results:
        url = (f"{OA_BASE}/works?filter=cites:{oa_id}"
               f"&select=id,doi,title,publication_year,primary_location,"
               f"abstract_inverted_index,cited_by_count,authorships"
               f"&per-page=200&cursor={cursor}&mailto={EMAIL}")
        try:
            r = OA.get(url, timeout=15)
            data = r.json()
            batch = data.get("results",[])
            if not batch:
                break
            results.extend(batch)
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
        except Exception:
            break
        time.sleep(OA_SLEEP)
    return [oa_parse_work(w) for w in results]

# ── S2 fetchers ───────────────────────────────────────────────────────────────

def s2_get_paper(s2_id=None, doi=None):
    if doi:
        url = f"{S2_BASE}/paper/DOI:{norm_doi(doi)}"
    elif s2_id:
        url = f"{S2_BASE}/paper/{s2_id}"
    else:
        return None
    try:
        r = S2.get(url, params={"fields": S2_FIELDS}, timeout=15)
        if r.status_code == 200:
            return s2_parse_paper(r.json())
    except Exception:
        pass
    return None

def s2_get_references(s2_id, limit=100):
    try:
        r = S2.get(f"{S2_BASE}/paper/{s2_id}/references",
                   params={"fields": S2_FIELDS, "limit": limit}, timeout=15)
        r.raise_for_status()
        return [s2_parse_paper(e.get("citedPaper",{}))
                for e in r.json().get("data",[])]
    except Exception:
        return []

def s2_get_citing(s2_id, limit=200):
    try:
        r = S2.get(f"{S2_BASE}/paper/{s2_id}/citations",
                   params={"fields": S2_FIELDS, "limit": limit}, timeout=15)
        r.raise_for_status()
        return [s2_parse_paper(e.get("citingPaper",{}))
                for e in r.json().get("data",[])]
    except Exception:
        return []

def s2_search(title, authors, limit=3):
    query = title
    if authors:
        surname = authors[0].split(",")[0].split()[-1]
        query   = f"{surname} {title}"
    try:
        r = S2.get(f"{S2_BASE}/paper/search",
                   params={"query": query[:200], "limit": limit,
                           "fields": S2_FIELDS}, timeout=15)
        results = r.json().get("data",[])
        if not results:
            return None
        title_tokens = set(title.lower().split())
        def sc(p):
            pt = set((p.get("title") or "").lower().split())
            return len(title_tokens & pt) / max(len(title_tokens),1)
        results.sort(key=sc, reverse=True)
        if sc(results[0]) < 0.4:
            return None
        return s2_parse_paper(results[0])
    except Exception:
        return None


# ── Graph node canonical key ──────────────────────────────────────────────────

def node_key(rec):
    """Prefer DOI, then OA ID, then S2 ID, then normalized title."""
    if rec.get("doi"):
        return f"doi:{rec['doi']}"
    if rec.get("oa_id"):
        return f"oa:{rec['oa_id']}"
    if rec.get("s2_id"):
        return f"s2:{rec['s2_id']}"
    t = re.sub(r"[^\w\s]","", (rec.get("title") or "").lower())
    t = re.sub(r"\s+"," ",t).strip()[:60]
    return f"title:{t}"


# ── Core crawl logic ──────────────────────────────────────────────────────────

def fetch_neighbors(rec, direction="both"):
    """
    Fetch citing and/or cited papers for a node.
    Uses S2 if available (better abstracts), OA as fallback.
    Returns list of candidate records.
    """
    candidates = []
    s2_id = rec.get("s2_id")
    oa_id = rec.get("oa_id")
    doi   = rec.get("doi")

    # Resolve S2 ID if missing
    if not s2_id and (doi or oa_id):
        paper = s2_get_paper(doi=doi)
        if paper:
            s2_id = paper.get("s2_id")
            rec["s2_id"] = s2_id
        time.sleep(S2_SLEEP)

    if direction in ("both", "backward"):
        # References (what this paper cites)
        if s2_id:
            refs = s2_get_references(s2_id)
            candidates.extend(refs)
            time.sleep(S2_SLEEP)
        elif oa_id:
            refs = oa_get_references(oa_id)
            candidates.extend(refs)

    if direction in ("both", "forward"):
        # Citing papers (what cites this paper)
        if s2_id:
            citing = s2_get_citing(s2_id)
            candidates.extend(citing)
            time.sleep(S2_SLEEP)
        elif oa_id:
            citing = oa_get_citing(oa_id)
            candidates.extend(citing)

    return candidates


def score_candidate(rec, threshold, scored_cache):
    """Score a candidate record for relevance. Returns (passes, score, reason)."""
    key = node_key(rec)
    if key in scored_cache:
        cached = scored_cache[key]
        return cached["score"] >= threshold, cached["score"], cached["reason"]

    title    = rec.get("title","")
    abstract = rec.get("abstract","")

    if not title:
        return False, 0, "no title"

    # Keyword pre-filter (free). NOTE: a miss here is NOT final. The pre-filter
    # decides on TITLE TEXT at DISCOVERY time, when the node has been cited by at
    # most one in-corpus paper, so it cannot see in-network citedness — the
    # endogenous, keyword-independent relevance signal. A pre-filter miss is
    # therefore parked as DEFERRED (re-examinable), not entombed at rel-1. The
    # citedness-rescue sweep (rescue_by_citedness.py / --rescue) revisits these
    # once in-degree has accumulated and re-scores the well-cited ones via Claude.
    # The `deferred` flag is what lets the sweep find and override these without
    # clobbering genuine Claude verdicts. (See README Stage 6.6.)
    if not keyword_prefilter(title, abstract):
        scored_cache[key] = {"score": 1, "reason": "failed keyword pre-filter",
                             "deferred": True}
        return False, 1, "failed keyword pre-filter"

    # Claude scoring
    score, reason = claude_score(title, abstract)
    scored_cache[key] = {"score": score, "reason": reason}
    time.sleep(0.1)
    return score >= threshold, score, reason


# ── Main ──────────────────────────────────────────────────────────────────────

def resolve_seeds(seeds):
    """Ensure each seed has at least one resolvable ID."""
    resolved = []
    for s in seeds:
        rec = dict(s)
        if not rec.get("s2_id") and (rec.get("doi") or rec.get("title")):
            paper = s2_get_paper(doi=rec.get("doi"))
            if not paper and rec.get("title"):
                paper = s2_search(rec["title"], rec.get("authors",[]))
            if paper:
                rec["s2_id"] = paper.get("s2_id")
                if not rec.get("doi"):
                    rec["doi"] = paper.get("doi")
                if not rec.get("abstract"):
                    rec["abstract"] = paper.get("abstract","")
            time.sleep(S2_SLEEP)
        resolved.append(rec)
        key = node_key(rec)
        ids = [f"oa:{rec.get('oa_id')}", f"s2:{rec.get('s2_id')}",
               f"doi:{rec.get('doi')}"]
        print(f"  {rec['title'][:55]:<55}  {' | '.join(i for i in ids if not i.endswith('None'))}")
    return resolved


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hops",      type=int, default=2,
                        help="Number of expansion hops (default 2)")
    parser.add_argument("--threshold", type=int, default=3,
                        help="Min Claude relevance score to expand a node (default 3)")
    parser.add_argument("--resume",    action="store_true",
                        help="Resume from existing citation_graph.json")
    args = parser.parse_args()

    # ── Load or initialise graph ───────────────────────────────────────────
    if args.resume and os.path.exists(GRAPH_PATH):
        print("Resuming from existing graph...")
        graph = json.load(open(GRAPH_PATH))
        nodes         = graph["nodes"]          # key → record
        scores        = graph["scores"]         # key → {score, reason}
        edges         = graph["edges"]          # list of {from, to, direction}
        completed_hop = graph.get("completed_hop", 0)
        print(f"  {len(nodes)} nodes, {len(edges)} edges, completed hop {completed_hop}")
    else:
        nodes         = {}
        scores        = {}
        edges         = []
        completed_hop = 0

    # ── Add seed papers to graph ──────────────────────────────────────────
    # Also load any high-confidence references from parse_references.py
    seeds_this_hop = []

    if completed_hop == 0:
        print("\nResolving seed papers...")
        resolved_seeds = resolve_seeds(SEED_PAPERS)
        for rec in resolved_seeds:
            key = node_key(rec)
            rec["hop"] = 0
            rec["is_seed"] = True
            nodes[key] = rec
            scores[key] = {"score": 5, "reason": "seed paper"}
            seeds_this_hop.append(key)

        # Pull in parsed references as additional seeds
        if os.path.exists(REF_PATH):
            parsed_refs = json.load(open(REF_PATH))
            ref_seeds = [r for r in parsed_refs
                         if (r.get("s2_id") or r.get("oa_id") or r.get("doi"))
                         and r.get("type") in ("article","book","chapter")
                         and not (r.get("note") or "").endswith("[duplicate]")]
            print(f"\nLoading {len(ref_seeds)} parsed references as hop-0 candidates...")
            for rec in ref_seeds:
                # Convert parsed ref format to graph node format
                node = {
                    "title":    rec.get("title",""),
                    "authors":  rec.get("authors",[]),
                    "year":     rec.get("year"),
                    "venue":    rec.get("venue",""),
                    "doi":      norm_doi(rec.get("doi") or ""),
                    "oa_id":    rec.get("oa_id"),
                    "s2_id":    rec.get("s2_id"),
                    "abstract": "",
                    "citations":0,
                    "hop":      0,
                    "source_paper": rec.get("source_paper"),
                }
                key = node_key(node)
                if key in nodes:
                    continue
                passes, score, reason = score_candidate(node, args.threshold, scores)
                node["hop"] = 0
                nodes[key] = node
                if passes:
                    seeds_this_hop.append(key)
                    print(f"  [seed ref, score={score}] {node['title'][:60]}")

    else:
        # Resuming: seeds for next hop are nodes from last completed hop
        # that scored above threshold
        last_hop = completed_hop
        seeds_this_hop = [
            k for k, n in nodes.items()
            if n.get("hop") == last_hop
            and scores.get(k, {}).get("score", 0) >= args.threshold
        ]
        print(f"Resuming: {len(seeds_this_hop)} seeds for hop {last_hop+1}")

        # Also inject any new SEED_PAPERS not yet in the graph
        print("\nChecking for new seed papers not yet in graph...")
        new_resolved = resolve_seeds(SEED_PAPERS)
        new_seeds_added = []
        new_seed_keys = []
        for rec in new_resolved:
            key = node_key(rec)
            if key not in nodes:
                rec["hop"] = 0
                rec["is_seed"] = True
                nodes[key] = rec
                scores[key] = {"score": 5, "reason": "seed paper (wave 2)"}
                new_seeds_added.append(rec.get("title",""))
                new_seed_keys.append(key)
                print(f"  + New seed: {rec.get('title','')[:60]}")
        if not new_seeds_added:
            print("  No new seeds found.")

        # Also inject newly-parsed references (from parse_references.py) that are
        # not yet in the graph, so a newly-added seed paper's bibliography gets
        # expanded too. On a fresh build these enter via the completed_hop==0
        # branch; on resume we replicate that here so resuming after adding a
        # seed + parsing its footnotes traces those refs the same way.
        if os.path.exists(REF_PATH):
            parsed_refs = json.load(open(REF_PATH))
            ref_seeds = [r for r in parsed_refs
                         if (r.get("s2_id") or r.get("oa_id") or r.get("doi"))
                         and r.get("type") in ("article","book","chapter")
                         and not (r.get("note") or "").endswith("[duplicate]")]
            added_refs = 0
            for rec in ref_seeds:
                node = {
                    "title":    rec.get("title",""),
                    "authors":  rec.get("authors",[]),
                    "year":     rec.get("year"),
                    "venue":    rec.get("venue",""),
                    "doi":      norm_doi(rec.get("doi") or ""),
                    "oa_id":    rec.get("oa_id"),
                    "s2_id":    rec.get("s2_id"),
                    "abstract": "",
                    "citations":0,
                    "hop":      0,
                    "source_paper": rec.get("source_paper"),
                }
                key = node_key(node)
                if key in nodes:
                    continue
                passes, score, reason = score_candidate(node, args.threshold, scores)
                node["hop"] = 0
                nodes[key] = node
                if passes:
                    new_seed_keys.append(key)
                    new_seeds_added.append(node["title"])
                    added_refs += 1
                    print(f"  [new seed ref, score={score}] {node['title'][:55]}")
            if added_refs:
                print(f"  + {added_refs} new parsed-reference seeds injected")

        # If there are new seeds, reset hop counter so they get fully expanded.
        # seeds_this_hop is replaced with ONLY the new seeds so we don't
        # re-expand the entire existing graph.
        if new_seeds_added:
            completed_hop = 0
            seeds_this_hop = new_seed_keys
            print(f"  Resetting hop counter; will expand {len(new_seeds_added)} new seeds over {args.hops} hops")

    # ── Snowball hops ─────────────────────────────────────────────────────
    for hop in range(completed_hop + 1, args.hops + 1):
        print(f"\n{'='*60}")
        print(f"HOP {hop}  —  {len(seeds_this_hop)} seed nodes to expand")
        print(f"{'='*60}")

        next_hop_seeds = []
        new_nodes_this_hop = 0

        for i, seed_key in enumerate(seeds_this_hop, 1):
            seed_rec = nodes[seed_key]
            title    = seed_rec.get("title","")[:50]
            print(f"\n  [{i}/{len(seeds_this_hop)}] {title}")

            candidates = fetch_neighbors(seed_rec, direction="both")
            print(f"    → {len(candidates)} neighbors fetched")

            new_this_seed = 0
            for cand in candidates:
                ckey = node_key(cand)
                if ckey in nodes:
                    # Add edge even if node already known
                    edges.append({"from": seed_key, "to": ckey, "hop": hop})
                    continue

                passes, score, reason = score_candidate(cand, args.threshold, scores)
                cand["hop"] = hop
                nodes[ckey] = cand
                edges.append({"from": seed_key, "to": ckey, "hop": hop})
                new_nodes_this_hop += 1
                new_this_seed += 1

                if passes:
                    next_hop_seeds.append(ckey)
                    print(f"    ✓ [score={score}] {cand.get('title','')[:55]}")

            print(f"    {new_this_seed} new nodes, "
                  f"{sum(1 for e in edges if e.get('hop')==hop and e['from']==seed_key)} edges")

            # Save checkpoint after each seed
            graph_data = {
                "nodes": nodes,
                "scores": scores,
                "edges": edges,
                "completed_hop": hop - 1,  # mark as in-progress
            }
            with open(GRAPH_PATH, "w") as f:
                json.dump(graph_data, f, indent=2, ensure_ascii=False)

        print(f"\nHop {hop} complete: {new_nodes_this_hop} new nodes, "
              f"{len(next_hop_seeds)} pass threshold for next hop")

        # Mark hop as complete
        graph_data = {
            "nodes": nodes,
            "scores": scores,
            "edges": edges,
            "completed_hop": hop,
        }
        with open(GRAPH_PATH, "w") as f:
            json.dump(graph_data, f, indent=2, ensure_ascii=False)

        seeds_this_hop = next_hop_seeds
        if not seeds_this_hop:
            print("No seeds for next hop — stopping early.")
            break

    # ── Generate candidate report ─────────────────────────────────────────
    print(f"\nGenerating report...")

    # Exclude seed papers themselves
    seed_keys = {k for k, n in nodes.items() if n.get("is_seed")}
    candidates = [
        {"key": k, **n, "score": scores.get(k,{}).get("score",0),
         "reason": scores.get(k,{}).get("reason","")}
        for k, n in nodes.items()
        if k not in seed_keys
        and scores.get(k,{}).get("score",0) >= args.threshold
    ]
    candidates.sort(key=lambda x: (-x["score"], -(x.get("citations") or 0)))

    lines = [
        "# Literature Candidates — Negligence & Responsibility\n",
        f"Generated by crawl_citation_graph.py | {len(candidates)} papers "
        f"scoring ≥ {args.threshold}/5 for relevance.\n\n",
        f"Score = Claude relevance (1–5) | Cit = citation count\n",
        "\n---\n",
    ]

    by_score = defaultdict(list)
    for c in candidates:
        by_score[c["score"]].append(c)

    for score in sorted(by_score.keys(), reverse=True):
        group = sorted(by_score[score],
                       key=lambda x: -(x.get("citations") or 0))
        lines.append(f"\n## Score {score}/5 ({len(group)} papers)\n")
        for c in group:
            authors = ", ".join((c.get("authors") or [])[:3])
            if len(c.get("authors") or []) > 3:
                authors += " et al."
            year   = c.get("year") or "?"
            venue  = c.get("venue") or ""
            cit    = c.get("citations") or 0
            doi    = c.get("doi") or ""
            reason = c.get("reason","")
            hop    = c.get("hop","?")
            lines.append(
                f"### {c.get('title','(no title)')}\n"
                f"**{authors}** ({year})"
                + (f", *{venue}*" if venue else "")
                + f" | Citations: {cit} | Hop: {hop}\n"
            )
            if doi:
                lines.append(f"DOI: {doi}  \n")
            if reason:
                lines.append(f"*Relevance: {reason}*\n")
            abstract = c.get("abstract","")
            if abstract:
                lines.append(f"\n> {abstract[:400]}{'...' if len(abstract)>400 else ''}\n")
            lines.append("\n")

    # Score distribution
    lines.append("\n---\n\n## Score Distribution\n\n")
    all_scored = [v["score"] for v in scores.values() if "score" in v]
    dist = defaultdict(int)
    for s in all_scored:
        dist[s] += 1
    for s in sorted(dist.keys(), reverse=True):
        lines.append(f"- Score {s}: {dist[s]} papers\n")
    lines.append(f"\nTotal nodes in graph: {len(nodes)}\n")
    lines.append(f"Total edges: {len(edges)}\n")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Report: {REPORT_PATH}")
    print(f"Graph:  {GRAPH_PATH}")
    print(f"\nTop 10 candidates:")
    for c in candidates[:10]:
        print(f"  [{c['score']}] {c.get('citations',0):4d} cit  "
              f"{c.get('title','')[:60]}")


if __name__ == "__main__":
    main()

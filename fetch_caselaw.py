"""
fetch_caselaw.py
Takes case citations extracted from the corpus (via extract_citations.py),
queries CourtListener for matching decisions, and generates executive
summaries using Claude. Writes caselaw_report.md.

CourtListener API: https://www.courtlistener.com/api/
Free, no API key required for basic use.
"""

import re, os, json, time, requests, urllib3
from llm_client import call_model

import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
FOLDER = os.path.dirname(os.path.abspath(__file__))
READING = os.path.join(FOLDER, "reading")   # human-facing report lives here
os.makedirs(READING, exist_ok=True)
REPORT_PATH = os.path.join(READING, "caselaw_report.md")
CACHE_PATH  = os.path.join(FOLDER, "caselaw_cache.json")  # local cache

CL = requests.Session()
_VERIFY_TLS = os.environ.get("INSECURE_TLS", "") not in ("1", "true", "True")  # verify TLS unless user opts out
CL.verify = _VERIFY_TLS
CL.headers["User-Agent"] = f"litreview-pipeline/1.0 ({os.environ.get('CROSSREF_MAILTO', 'you@example.com')})"
_CL_TOKEN = os.environ.get("COURTLISTENER_API_KEY", "")
if _CL_TOKEN:
    CL.headers["Authorization"] = f"Token {_CL_TOKEN}"


# ── Case citations to look up ─────────────────────────────────────────────────
# Drawn from the master list in citation_report.md plus known landmark cases
# from the literature. Add more as the corpus grows.

CASES = [
    # From corpus
    {"name": "Vaughan v. Menlove",         "citation": "132 Eng. Rep. 490",  "year": 1837, "jurisdiction": "uk"},
    {"name": "Butterfield v. Forrester",   "citation": "103 Eng. Rep. 926",  "year": 1809, "jurisdiction": "uk"},
    {"name": "Baltimore & Ohio R.R. v. Goodman", "citation": "275 U.S. 66",  "year": 1927, "jurisdiction": "us-federal"},
    {"name": "United States v. Carroll Towing Co.", "citation": "159 F.2d 169", "year": 1947, "jurisdiction": "us-federal"},
    {"name": "The T.J. Hooper (New England Coal & Coke Co. v. Northern Barge Corp.)",
                                            "citation": "60 F.2d 737",        "year": 1932, "jurisdiction": "us-federal",
     "cl_search": "New England Coal Coke Northern Barge Corporation 60 F.2d 737"},
    {"name": "People v. Decina",           "citation": "138 N.E.2d 799",     "year": 1956, "jurisdiction": "us-state"},
    {"name": "Breunig v. American Family Insurance Co.", "citation": "173 N.W.2d 619", "year": 1970, "jurisdiction": "us-state"},
    # Classic negligence cases likely referenced in lit but not yet confirmed in corpus
    {"name": "Palsgraf v. Long Island R.R.", "citation": "162 N.E. 99",     "year": 1928, "jurisdiction": "us-state"},
    {"name": "Bolton v. Stone",            "citation": "[1951] AC 850",      "year": 1951, "jurisdiction": "uk"},
    {"name": "Donoghue v. Stevenson",      "citation": "[1932] AC 562",      "year": 1932, "jurisdiction": "uk"},
]

# ── CourtListener search ──────────────────────────────────────────────────────

CL_BASE = "https://www.courtlistener.com/api/rest/v4"

def cl_search(case_name: str, citation: str = "", year: int = None) -> list[dict]:
    """Search CourtListener for a case by name."""
    params = {
        "q": case_name,
        "type": "o",          # opinions
        "order_by": "score desc",
        "format": "json",
    }
    if year:
        params["filed_after"] = f"{year - 2}-01-01"
        params["filed_before"] = f"{year + 2}-12-31"

    try:
        r = CL.get(f"{CL_BASE}/search/", params=params, timeout=15)
        r.raise_for_status()
        results = r.json().get("results", [])
        return results[:5]
    except Exception as e:
        return []


def cl_extract_text(cl_result: dict) -> str:
    """
    Fetch full opinion text for a case using the authenticated REST API.
    Falls back to search result metadata/snippet if unavailable.
    """
    # Try fetching full opinion text via the opinions endpoint
    cluster_id = cl_result.get("cluster_id") or cl_result.get("id")
    if cluster_id:
        try:
            r = CL.get(
                f"{CL_BASE}/opinions/",
                params={"cluster": cluster_id, "format": "json"},
                timeout=20,
            )
            r.raise_for_status()
            for op in r.json().get("results", []):
                # Prefer plain text, then lawbox HTML, then generic HTML
                for field in ("plain_text", "html_lawbox", "html_columbia", "html"):
                    text = op.get(field) or ""
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text).strip()
                    if len(text) > 200:
                        return text[:10000]  # cap for Claude context
        except Exception:
            pass

    # Fallback: metadata fields + search snippet
    parts = []
    for field in ("posture", "procedural_history", "syllabus"):
        val = cl_result.get(field) or ""
        if val.strip():
            parts.append(f"{field.title()}: {val.strip()}")
    for op in cl_result.get("opinions", []):
        snippet = re.sub(r"<[^>]+>", " ", op.get("snippet") or "").strip()
        if snippet:
            parts.append(f"Excerpt: {snippet}")
            break
    return "\n".join(parts)


def cl_find_case(case: dict) -> dict | None:
    """Try to find a case on CourtListener. Returns best match or None."""
    name = case["name"]
    results = cl_search(name, case.get("citation",""), case.get("year"))
    if not results:
        # Try shorter name (first party only)
        short = name.split(" v.")[0].strip()
        results = cl_search(short)
    if not results:
        return None

    # Pick best match: prefer results whose case_name contains both parties
    parties = [p.strip().lower() for p in name.split(" v.")]
    def score(r):
        cn = (r.get("caseName") or "").lower()
        return sum(1 for p in parties if p[:6] in cn)
    results.sort(key=score, reverse=True)
    return results[0]


# ── Claude summary generation ─────────────────────────────────────────────────

SUMMARY_SYSTEM = """You are a legal research assistant helping a philosopher
working on moral and legal responsibility for negligent acts and omissions.
When given a case citation and any available opinion text, produce a concise
executive summary covering:
1. Facts and procedural posture (2-3 sentences)
2. Legal question decided
3. Holding
4. Key reasoning (focus on what is philosophically significant about negligence,
   fault, objective standards, or involuntary action)
5. Significance for the negligence literature (why scholars cite this case)

Be direct and precise. Flag any uncertainty about facts not confirmed by the
provided text."""


def claude_summary(case: dict, opinion_text: str, cl_result: dict | None) -> str:
    case_name = case["name"]
    citation = case.get("citation","")

    if cl_result:
        cl_name = cl_result.get("caseName","")
        court = cl_result.get("court","")
        date = cl_result.get("dateFiled","")
        cite_count = cl_result.get("citeCount", "")
        context = f"CourtListener match: {cl_name} | Court: {court} | Date filed: {date} | Cite count: {cite_count}"
    else:
        context = "Not found on CourtListener."

    user_msg = f"""Case: {case_name}, {citation} ({case.get('year','')})
{context}

{"Available metadata/excerpt:" + chr(10) + opinion_text if opinion_text else "No text available from CourtListener — please draw on your training knowledge for this case."}

Please generate the executive summary. Note: CourtListener full text requires authentication, so summaries draw on available metadata plus your training knowledge. Flag anything uncertain."""

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "[ANTHROPIC_API_KEY not set — summary not generated. Run with key in environment.]"
    try:
        return call_model(
            system=SUMMARY_SYSTEM,
            user=user_msg,
            model="smart",
            max_tokens=600,
        )
    except Exception as e:
        return f"[Summary generation failed: {e}]"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Load cache if exists
    cache = {}
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            cache = json.load(f)

    lines = [
        "# Case Law Report — Negligence Corpus\n",
        "Sources: CourtListener API + Claude summaries.\n",
        "UK cases (Vaughan, Butterfield, Bolton, Donoghue) are not on CourtListener;\n"
        "summaries for those are generated from Claude's training knowledge.\n",
        "\n---\n",
    ]

    found_count = 0
    not_found = []

    for case in CASES:
        name = case["name"]
        citation = case.get("citation","")
        jurisdiction = case.get("jurisdiction","")
        cache_key = name

        print(f"\n{'='*55}\n{name} ({citation})")

        lines.append(f"\n## {name}\n")
        lines.append(f"**Citation:** {citation}  \n")
        lines.append(f"**Year:** {case.get('year','')}  \n")

        # Check cache
        if cache_key in cache:
            print("  [cached]")
            entry = cache[cache_key]
        else:
            entry = {}
            cl_result = None
            opinion_text = ""

            if jurisdiction != "uk":
                cl_result = cl_find_case(case)
                if cl_result:
                    found_count += 1
                    print(f"  CL match: {cl_result.get('caseName')} ({cl_result.get('court')})")
                    opinion_text = cl_extract_text(cl_result)
                    print(f"  Context extracted: {len(opinion_text)} chars")
                else:
                    not_found.append(name)
                    print("  Not found on CourtListener")
            else:
                print("  UK case — skipping CourtListener, using Claude knowledge")

            print("  Generating summary...", end=" ", flush=True)
            summary = claude_summary(case, opinion_text, cl_result)
            print("done")

            entry = {
                "cl_result": cl_result,
                "opinion_chars": len(opinion_text),
                "summary": summary,
            }
            cache[cache_key] = entry
            time.sleep(0.5)

        # Write to report
        if entry.get("cl_result"):
            cr = entry["cl_result"]
            lines.append(f"**Court:** {cr.get('court','')}  \n")
            lines.append(f"**CourtListener:** https://www.courtlistener.com{cr.get('absolute_url','') or ''}  \n")
            lines.append(f"**Opinion text retrieved:** {entry.get('opinion_chars',0)} chars  \n")
        elif jurisdiction == "uk":
            lines.append("**Source:** Summary from Claude training knowledge (UK case, not on CourtListener)  \n")
        else:
            lines.append("**CourtListener:** Not found  \n")

        lines.append(f"\n### Summary\n\n{entry.get('summary','')}\n")
        lines.append("\n---\n")

    # Write cache
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    # Summary stats
    lines.append("\n## Coverage Notes\n")
    lines.append(f"- Cases found on CourtListener: {found_count}/{sum(1 for c in CASES if c.get('jurisdiction') != 'uk')}\n")
    if not_found:
        lines.append(f"- Not found on CourtListener: {', '.join(not_found)}\n")
    lines.append("- UK cases (not on CourtListener): "
                 + ", ".join(c['name'] for c in CASES if c.get('jurisdiction') == 'uk') + "\n")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n\nReport written to: {REPORT_PATH}")
    print(f"Cache written to: {CACHE_PATH}")


if __name__ == "__main__":
    main()

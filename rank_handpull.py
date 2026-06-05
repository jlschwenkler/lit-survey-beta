"""
rank_handpull.py  —  Topically rank the no-abstract hand-pull worklist so the
user can see which abstract-less papers are worth the manual effort of pulling.

WHY THIS STEP EXISTS
--------------------
`triage_no_abstract.py` produces a HAND-PULL worklist (triage_handpull_keys.txt):
papers that are relevant-enough-to-surface (by the OR rule: on-thesis TITLE or
in-corpus CITEDNESS) but have NO API abstract anywhere, so they can only be
recovered by hand. That worklist is ranked STRUCTURALLY — by in-corpus in-degree,
then by the crawl's title-only score. Structure alone doesn't answer the question
the user actually has when deciding what to pull: "given the thesis, how likely is
THIS title to be worth the effort?" Some low-in-degree titles are obviously
on-thesis; some high-in-degree ones are volume-mate / tangential noise.

This tool adds a TOPICAL judgment on top, WITHOUT collapsing it into the
structural one. For each worklist row it asks Claude (Sonnet — this is a judgment
call, not the cheap title-gate Haiku does on thousands of crawl candidates; the
worklist is small, so the economics flip) to read:
    title + year + venue + authors + the PROJECT_DESCRIPTION
and return:
    pull_priority  1-5   how worth-pulling the title looks on thesis-fit alone
    exclude        bool  clearly off-thesis; don't bother
    reason         one line

DELIBERATE DESIGN: priority is TITLE-FIT ONLY. In-corpus in-degree is shown as a
SEPARATE, visible column — NOT folded into the priority number. Averaging the two
would hide exactly the tension the user noticed (relevant-but-uncited vs.
cited-but-tangential). Two axes, both visible, sort by either.

NO model fabrication: Claude judges the TITLE's topical fit. It is NOT asked to
guess or summarize what the paper argues, and nothing it returns is written to the
graph. This produces a REPORT ONLY — it never mutates citation_graph.json or the
matrix. The abstracts themselves are still pulled by hand from the real source.

OUTPUTS
  - triage_handpull_ranked.md   — the worklist re-rendered, sorted by pull_priority
                                  (desc), then in-degree (desc); excludes in a
                                  separate trailing section. Columns: priority ·
                                  exclude · in-corpus in-degree · title-score ·
                                  year · title · venue · reason · link.
  - triage_handpull_ranked.json — machine-readable (key, priority, exclude, reason,
                                  indegree, score, …) for any follow-on tooling.

USAGE
  python3 rank_handpull.py            # rank the full worklist
  python3 rank_handpull.py --limit 25 # smoke test (top-N by in-degree)
"""

import ssl
import os as _os  # TLS verification on by default; opt out with INSECURE_TLS=1
if _os.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    ssl._create_default_https_context = ssl._create_unverified_context

import argparse, json, os, re, time
from collections import Counter
from datetime import datetime

import urllib3
import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
from llm_client import call_model

import crawl_citation_graph as C   # PROJECT_DESCRIPTION, norm_doi
import backfill_abstracts as B     # norm_doi (shared)

import builtins
def print(*a, **kw):
    kw.setdefault("flush", True)
    builtins.print(*a, **kw)

FOLDER        = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH    = os.path.join(FOLDER, "citation_graph.json")
HANDPULL_KEYS = os.path.join(FOLDER, "triage_handpull_keys.txt")
REPORT_PATH   = os.path.join(FOLDER, "triage_handpull_ranked.md")
JSON_PATH     = os.path.join(FOLDER, "triage_handpull_ranked.json")

MODEL  = "smart"   # title-fit judgment; see llm_client.py

RANK_SYSTEM = f"""You are triaging a literature-review worklist for a philosophy \
project. These are works the automated crawl could NOT obtain an abstract for, so \
the researcher must decide — title in hand — which are worth retrieving by hand.

THE PROJECT:
{C.PROJECT_DESCRIPTION.strip()}

You will be given a single work's TITLE, year, venue, and authors. Judge ONLY how \
likely its TITLE (with venue/author as context) indicates the work is worth \
pulling for THIS project — its topical fit. You are NOT asked to guess or \
summarize the work's argument; judge fit from the title alone, conservatively.

Return ONLY a JSON object, nothing else:
{{"pull_priority": <int 1-5>, "exclude": <true|false>, "reason": "<one short clause>"}}

Scale:
  5 = squarely on-thesis; a title an expert in this literature would expect to see.
  4 = clearly relevant; on a core theme (negligence, culpability, the epistemic
      condition, reasonable-person, quality of will, recklessness, excuse...).
  3 = plausibly relevant; adjacent or general moral-responsibility / criminal-law
      theory whose bearing on the thesis is uncertain from the title.
  2 = weak; touches the broad area but the title suggests a different focus.
  1 = off-thesis.
  exclude=true for clear non-fits: AI/robot responsibility gaps, free-will
      metaphysics for its own sake, neuroscience/forensic-psychiatry empirics,
      medical-malpractice practice pieces, front matter, and other works whose
      title shows they are not about the moral/legal grounds of negligence and
      responsibility. Set exclude=true AND a low priority for these.
Be willing to use the full range. Do not inflate borderline titles."""


def link_for(n):
    doi = B.norm_doi(n.get("doi"))
    if doi:
        return f"https://doi.org/{doi}"
    if n.get("oa_id"):
        return f"https://openalex.org/{n['oa_id'].replace('https://openalex.org/', '')}"
    if n.get("s2_id"):
        return f"https://www.semanticscholar.org/paper/{n['s2_id']}"
    return ""


def rank_one(n):
    """Ask Claude for a topical pull-priority on a single work. Returns dict."""
    authors = ", ".join(n.get("authors") or [])[:120]
    content = (f"Title: {n.get('title') or ''}\n"
               f"Year: {n.get('year') or '—'}\n"
               f"Venue: {n.get('venue') or '—'}\n"
               f"Authors: {authors or '—'}")
    for attempt in range(4):
        try:
            text = call_model(
                system=RANK_SYSTEM, user=content, model=MODEL, max_tokens=120,
            )
            text = re.sub(r"^```(?:json)?\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
            data = json.loads(text)
            pr = int(data.get("pull_priority", 1))
            return {"pull_priority": max(1, min(5, pr)),
                    "exclude": bool(data.get("exclude", False)),
                    "reason": (data.get("reason") or "").strip()[:160]}
        except Exception as e:
            if attempt == 3:
                return {"pull_priority": 0, "exclude": False,
                        "reason": f"rank error: {type(e).__name__}"}
            time.sleep(1.0 * (attempt + 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="cap rows ranked, taking the top-N by in-corpus in-degree "
                         "(smoke test)")
    args = ap.parse_args()

    if not os.path.exists(HANDPULL_KEYS):
        raise SystemExit(f"missing {os.path.basename(HANDPULL_KEYS)} — run "
                         f"triage_no_abstract.py first.")
    keys = [ln.strip() for ln in open(HANDPULL_KEYS) if ln.strip()]

    graph = json.load(open(GRAPH_PATH))
    nodes = graph["nodes"]
    scores = graph["scores"]
    indeg = Counter(e["to"] for e in graph["edges"])

    rows = []
    for k in keys:
        n = nodes.get(k)
        if not n:
            continue
        rows.append({"key": k, "node": n,
                     "indegree": indeg.get(k, 0),
                     "score": (scores.get(k) or {}).get("score", 0)})

    # smoke test: take the most-cited N (the ones most likely to matter)
    if args.limit:
        rows.sort(key=lambda r: -r["indegree"])
        rows = rows[:args.limit]
        print(f"(limited to top-{args.limit} by in-corpus in-degree)")

    print(f"Ranking {len(rows)} hand-pull rows with {MODEL} ...")
    out = []
    for i, r in enumerate(rows, 1):
        n = r["node"]
        verdict = rank_one(n)
        out.append({
            "key": r["key"], "title": n.get("title") or "",
            "year": n.get("year"), "venue": n.get("venue") or "",
            "authors": n.get("authors") or [],
            "indegree": r["indegree"], "score": r["score"],
            "pull_priority": verdict["pull_priority"],
            "exclude": verdict["exclude"], "reason": verdict["reason"],
            "link": link_for(n),
        })
        if i % 25 == 0 or i == len(rows):
            print(f"  [{i}/{len(rows)}] ranked")
        time.sleep(0.05)

    # Sort: keepers first (exclude last), then priority desc, then in-degree desc.
    # in-degree is a SECONDARY tiebreak only — never folded into priority.
    keepers = [r for r in out if not r["exclude"]]
    excludes = [r for r in out if r["exclude"]]
    sortf = lambda r: (-r["pull_priority"], -r["indegree"], r["title"].lower())
    keepers.sort(key=sortf)
    excludes.sort(key=sortf)

    json.dump({"ranked": keepers + excludes}, open(JSON_PATH, "w"),
              ensure_ascii=False, indent=2)

    def fmt(rows_):
        lines = []
        for r in rows_:
            link = f"[link]({r['link']})" if r["link"] else "—"
            lines.append(
                f"| {r['pull_priority']} | {r['indegree']} | {r['score']} | "
                f"{r['year'] or '—'} | {r['title'][:64]} | {r['venue'][:24]} | "
                f"{r['reason'][:80]} | {link} |")
        return lines

    pr_counts = Counter(r["pull_priority"] for r in keepers)
    lines = [
        f"# Hand-pull worklist — topically ranked — {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"Ranked with **{MODEL}** on TITLE-FIT to the project thesis. "
        "Priority is topical only; **in-corpus in-degree is shown separately, NOT "
        "folded into the priority** (two axes — sort by either). No abstracts were "
        "fetched and the graph was not modified; this is a reading aid for deciding "
        "what to hand-pull.",
        "",
        f"- Rows ranked: **{len(out)}** — keepers **{len(keepers)}**, "
        f"flagged-exclude **{len(excludes)}**.",
        f"- Keeper priority spread: "
        + ", ".join(f"P{p}={pr_counts.get(p,0)}" for p in (5, 4, 3, 2, 1)) + ".",
        "",
        "Columns: pull-priority · in-corpus in-degree · crawl title-score · year · "
        "title · venue · reason · link.",
        "",
        "## Keepers (worth considering) — sorted by priority, then in-degree",
        "",
        "| Pri | In-corp | Sc | Year | Title | Venue | Why | Link |",
        "|--:|--:|--:|:--|:--|:--|:--|:--|",
        *fmt(keepers),
        "",
        "## Flagged as off-thesis exclusions",
        "",
        "| Pri | In-corp | Sc | Year | Title | Venue | Why | Link |",
        "|--:|--:|--:|:--|:--|:--|:--|:--|",
        *fmt(excludes),
        "",
    ]
    open(REPORT_PATH, "w", encoding="utf-8").write("\n".join(lines))
    print(f"\nReport -> {os.path.basename(REPORT_PATH)}")
    print(f"JSON   -> {os.path.basename(JSON_PATH)}")
    print(f"Keepers: {len(keepers)}  ({', '.join(f'P{p}={pr_counts.get(p,0)}' for p in (5,4,3,2,1))})")
    print(f"Flagged exclude: {len(excludes)}")


if __name__ == "__main__":
    main()

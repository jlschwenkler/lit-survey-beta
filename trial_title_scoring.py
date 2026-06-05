"""
trial_title_scoring.py — REPORT-ONLY trial. Does NOT touch the graph or matrix.

Question under test (JS, 2026-06-02): for the title-only P4 keepers, does a
NON-FLOORED title judgment (allowed to assign real depth where a title genuinely
signals an issue) produce SENSIBLE leverage — or noise? Compare two scorers on
the SAME curated P4 titles:

  (1) FLOORED  — the current gate behavior: assign 0, or a cautious 1 only where
                 the title clearly names an issue. (Haiku, the production gate.)
  (2) NONFLOOR — same 9-issue list + same depth scale, but the model is told it
                 MAY assign real depth 0-3 from a strong title, with the standard
                 "a title previews, it does not prove" discipline. (Sonnet.)

Leverage uses the REAL formula from build_lit_table.py: sum(weight_i * depth_i)
over the 9 issues in issues_final.json. VISIBLE_MIN=9.0, STAR_MIN=15.0 shown for
reference. Output: trial_title_scoring.md (side-by-side) + .json (raw vectors).

Reads issues + the floored system prompt straight from score_engagement.py so the
trial uses the production instrument, not a reinvented one.
"""
import os, json, re, sys, time
from llm_client import call_model
import score_engagement as SE   # reuse load_issues + build_system (floored prompt)

FOLDER = os.path.dirname(os.path.abspath(__file__))
GRAPH  = os.path.join(FOLDER, "citation_graph.json")
OUT_MD = os.path.join(FOLDER, "trial_title_scoring.md")
OUT_JS = os.path.join(FOLDER, "trial_title_scoring.json")

FLOOR_MODEL    = "fast"    # see llm_client.py
NONFLOOR_MODEL = "smart"   # see llm_client.py

# The 24 P4 titles JS picked for the trial (in-corpus in-degree shown for context).
KEYS = [
    "doi:10.1111/j.1520-8583.2004.00030.x", "doi:10.1007/s11098-018-1053-3",
    "doi:10.1007/s10892-011-9112-4", "doi:10.1007/s11098-019-01354-5",
    "doi:10.1007/s11098-018-1208-2", "doi:10.1007/978-94-007-4707-4_120",
    "doi:10.1007/s11229-006-9089-x", "doi:10.1007/s11098-018-1132-5",
    "doi:10.1007/978-94-007-1878-4_2", "doi:10.1007/s11098-019-01399-6",
    "doi:10.1007/s11572-012-9153-1", "doi:10.1007/s11098-016-0680-9",
    "doi:10.1007/s13164-015-0287-7", "oa:W180211458",
    "doi:10.1007/s11098-015-0527-9", "doi:10.2307/3312513",
    "doi:10.2307/3312463", "doi:10.1016/j.cognition.2008.03.006",
    "doi:10.1007/s10892-019-09294-2", "doi:10.1007/s11098-021-01774-2",
    "doi:10.1007/s11229-017-1332-0", "doi:10.1007/s11572-012-9173-x",
    "doi:10.1007/s11098-024-02119-5", "doi:10.1007/s11572-010-9092-7",
]

NONFLOOR_NOTE = """
SOURCE NOTE: You are given ONLY the paper's TITLE (and year/venue), no abstract.
You MAY assign real depth (0-3) when the title clearly signals that an issue is a
core concern of the paper — do NOT auto-floor every score to 0/1. But stay
disciplined: a title PREVIEWS, it does not PROVE. Reserve depth 3 for an issue the
title names as the paper's central organizing concern; depth 2 for an issue the
title clearly puts at stake; depth 1 for an issue merely adjacent or implied;
depth 0 when the title gives no signal. When a title is vague or generic, most
issues should be 0. Judge THIS title, not the field it sits in.
"""


def strip_fences(s):
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def call(model, system, user, retries=4):
    for a in range(retries):
        try:
            txt = call_model(system=system, user=user, model=model, max_tokens=400)
            return json.loads(strip_fences(txt))
        except Exception as e:
            if a == retries - 1:
                return {"_error": str(e)}
            time.sleep(2 ** a)


def main():
    issues, depth_scale = SE.load_issues()
    ids = [i["id"] for i in issues]
    wt  = {i["id"]: float(i.get("weight", 1.0)) for i in issues}
    floored_system  = SE.build_system(issues, depth_scale)
    nonfloor_system = floored_system + "\n" + NONFLOOR_NOTE

    g = json.load(open(GRAPH)); nodes = g["nodes"]; scores = g["scores"]

    def lev(vec):
        return round(sum(wt[i] * int(vec.get(i, 0)) for i in ids), 1)

    rows = []
    for k in KEYS:
        n = nodes.get(k) or {}
        title = (n.get("title") or "").strip()
        yr = n.get("year"); ven = n.get("venue") or ""
        user = f"TITLE: {title}\nYEAR: {yr}\nVENUE: {ven}\n(No abstract available.)"
        fl = call(FLOOR_MODEL, floored_system, user)
        nf = call(NONFLOOR_MODEL, nonfloor_system, user)
        rows.append({
            "key": k, "title": title, "year": yr,
            "cur_score": (scores.get(k) or {}).get("score", 0),
            "floored": fl, "nonfloor": nf,
            "lev_floored": lev(fl) if "_error" not in fl else None,
            "lev_nonfloor": lev(nf) if "_error" not in nf else None,
        })
        print(f"  scored: {title[:48]:48}  floor_lev={rows[-1]['lev_floored']}  "
              f"nonfloor_lev={rows[-1]['lev_nonfloor']}")

    json.dump({"ids": ids, "weights": wt, "rows": rows},
              open(OUT_JS, "w"), indent=2, ensure_ascii=False)

    # markdown side-by-side
    L = []
    L.append("# Trial: floored (Haiku gate) vs non-floored (Sonnet) title scoring\n")
    L.append("Leverage = sum(weight x depth) over 9 issues. "
             "VISIBLE_MIN=9.0, STAR_MIN=15.0 (max possible 27).\n")
    L.append("| in-deg note | title (yr) | floor lev | NONFLOOR lev | nonfloor note |")
    L.append("|---|---|---:|---:|---|")
    for r in rows:
        nf_note = (r["nonfloor"].get("note", "") if "_error" not in r["nonfloor"]
                   else "ERR " + r["nonfloor"]["_error"][:40])
        L.append(f"|  | {r['title'][:50]} ({r['year']}) | "
                 f"{r['lev_floored']} | **{r['lev_nonfloor']}** | {nf_note[:80]} |")
    L.append("\n## Per-issue vectors (non-floored)\n")
    L.append("| title | " + " | ".join(ids) + " |")
    L.append("|---|" + "|".join("---" for _ in ids) + "|")
    for r in rows:
        nf = r["nonfloor"]
        if "_error" in nf:
            continue
        L.append(f"| {r['title'][:40]} | " +
                 " | ".join(str(nf.get(i, 0)) for i in ids) + " |")
    open(OUT_MD, "w").write("\n".join(L) + "\n")

    ok = [r for r in rows if r["lev_nonfloor"] is not None]
    vis = sum(1 for r in ok if r["lev_nonfloor"] >= 9.0)
    print(f"\nWrote {OUT_MD}")
    print(f"  non-floored: {vis}/{len(ok)} would clear VISIBLE_MIN(9.0); "
          f"floored clearing 9.0: {sum(1 for r in ok if (r['lev_floored'] or 0) >= 9.0)}")


if __name__ == "__main__":
    main()

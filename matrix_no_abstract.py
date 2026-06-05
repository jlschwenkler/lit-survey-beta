#!/usr/bin/env python3
"""
Stage 7.5 — In-matrix no-abstract worklist  (the INWARD mirror of triage_no_abstract.py)

WHY THIS EXISTS — the blind spot it closes:
  Two stages chase missing abstracts, and a whole class of works fell BETWEEN them:
    • `backfill_abstracts.py` targets MATRIX ROWS but only queries the APIs. When
      OpenAlex/Crossref/S2 all come up empty (older / thinly-indexed works), the row
      just stays `text_source=="title"` — backfill fails SILENTLY, with no handoff.
    • `triage_no_abstract.py` builds the hand-pull worklist, but by design it only
      looks at nodes NOT already in the matrix (its job is to pull buried works IN).
  So a work that is ALREADY a matrix row but scored title-only is invisible to both:
  backfill gives up quietly, triage never looks at it. Nothing put these in front of
  the researcher. (On the PK run, Setiya "Practical Knowledge*", Pavese "Practical
  knowledge first", Falvey "Knowledge in Intention", Paul "How We Know What We're
  Doing" all sat abstract-less as matrix rows for the whole project, surfacing only
  when JS scanned the star list by eye.)

WHAT IT DOES (report-only — never mutates graph/matrix; never fabricates an abstract):
  Lists every MATRIX ROW still scored title-only (`text_source=="title"`, not a
  review), computes its weighted leverage + weighted in-corpus citedness, and ranks
  the list by TITLE-FIT (the same claude-sonnet judgment `rank_handpull.py` uses) so
  the researcher pulls the worth-it ones first. Output: `matrix_no_abstract_report.md`
  (+ `.json`). The researcher then hand-pulls abstracts (Stage 7 routes), ingests, and
  re-scores — at which point the row gets real leverage and may become visible/starred.

PLACEMENT: late (after the table is built — Stage 7 / 6.8 neighborhood), so ranking
  can lean on leverage/citedness/star context. Re-runnable; cheap (one Sonnet call per
  title-only row, and there are few once backfill+triage have run).

USAGE:
  python3 matrix_no_abstract.py             # rank all title-only rows
  python3 matrix_no_abstract.py --no-rank   # skip the Sonnet pass (citedness-only order)
  python3 matrix_no_abstract.py --limit 15  # smoke test: top-N by citedness
"""
import os, sys, json, argparse

import rank_handpull as R          # reuse rank_one() + its Sonnet RANK_SYSTEM
import backfill_abstracts as B     # reuse norm_doi / has_abs conventions

FOLDER  = os.path.dirname(os.path.abspath(__file__))
GRAPH   = os.path.join(FOLDER, "citation_graph.json")
MATRIX  = os.path.join(FOLDER, "engagement_matrix.json")
ISSUES  = os.path.join(FOLDER, "issues_final.json")
LINKS   = os.path.join(FOLDER, "links.json")
REPORT  = os.path.join(FOLDER, "matrix_no_abstract_report.md")
REPORT_JSON = os.path.join(FOLDER, "matrix_no_abstract.json")

def load_core_weights():
    """CORE issues + their leverage weights, read from issues_final.json so the tool
    is project-agnostic. CORE = the 'A'-prefixed issues (true in both the PK scheme
    A1–A6 and the negligence scheme A1–A6 incl. A5a/A5b; related issues like C3/C4 are
    NOT core and don't contribute to leverage — matching build_lit_table.py's CORE)."""
    j = json.load(open(ISSUES))
    iss = j["issues"] if isinstance(j, dict) and "issues" in j else j
    core = [i["id"] for i in iss if str(i["id"]).startswith("A")]
    w = {i["id"]: float(i.get("weight", 1.0)) for i in iss}
    return core, {k: w.get(k, 1.0) for k in core}


def leverage(scores, core, w):
    return round(sum((scores.get(k) or 0) * w[k] for k in core), 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-rank", action="store_true",
                    help="skip the Sonnet title-fit pass; order by citedness only")
    ap.add_argument("--limit", type=int, default=0,
                    help="rank only the top-N by weighted citedness (smoke test)")
    args = ap.parse_args()

    g = json.load(open(GRAPH)); nodes = g["nodes"]
    m = json.load(open(MATRIX)); rows = m["rows"]
    links = json.load(open(LINKS)) if os.path.exists(LINKS) else {}
    core, w = load_core_weights()

    # the blind-spot set: matrix rows scored from the title alone, not reviews,
    # still genuinely abstract-less on their graph node.
    targets = []
    for r in rows:
        if r.get("text_source") != "title":
            continue
        if r.get("is_review"):
            continue
        k = r["key"]
        n = nodes.get(k, {})
        if B.has_abs(n):          # node actually HAS an abstract (stale row) — skip
            continue
        wtd = r.get("in_corpus_cites_weighted", 0) or 0
        raw = r.get("in_corpus_cites", 0) or 0
        targets.append({
            "key": k,
            "title": (n.get("title") or r.get("title") or ""),
            "year": n.get("year") or r.get("year"),
            "venue": n.get("venue") or "",
            "authors": n.get("authors") or r.get("authors") or [],
            "doi": n.get("doi"),
            "leverage_titleonly": leverage(r.get("scores", {}), core, w),
            "wtd_incorp": wtd,
            "raw_incorp": raw,
            "link": (links.get(k, {}) or {}).get("url", ""),
            "link_kind": (links.get(k, {}) or {}).get("kind", ""),
        })

    # order by endogenous citedness first (the structural signal that survives even
    # when the title is uninformative)
    targets.sort(key=lambda t: (-t["wtd_incorp"], -t["raw_incorp"], t["title"].lower()))

    print(f"In-matrix title-only (no-abstract) rows: {len(targets)}")
    if args.limit:
        targets = targets[:args.limit]
        print(f"(smoke test: ranking top {len(targets)} by weighted citedness)")

    # title-fit ranking (reuse rank_handpull's Sonnet judgment, unchanged)
    if not args.no_rank:
        print(f"Ranking {len(targets)} rows with {R.MODEL} (title-fit) ...")
        for i, t in enumerate(targets, 1):
            v = R.rank_one({"title": t["title"], "year": t["year"],
                            "venue": t["venue"], "authors": t["authors"]})
            t["pull_priority"] = v["pull_priority"]
            t["exclude"] = v["exclude"]
            t["reason"] = v["reason"]
            if i % 10 == 0 or i == len(targets):
                print(f"  {i}/{len(targets)}")
        # keepers first by priority, then citedness; excludes trail
        targets.sort(key=lambda t: (t.get("exclude", False),
                                    -(t.get("pull_priority") or 0),
                                    -t["wtd_incorp"], t["title"].lower()))

    json.dump({"rows": targets}, open(REPORT_JSON, "w"), ensure_ascii=False, indent=2)

    # ── markdown report ──────────────────────────────────────────────────────
    L = ["# Stage 7.5 — In-matrix no-abstract worklist",
         "",
         "_Matrix rows scored from the TITLE ALONE (no abstract on the node, not a "
         "review). These fell between `backfill_abstracts.py` (tries the APIs, fails "
         "silently) and `triage_no_abstract.py` (only looks OUTSIDE the matrix). Pull "
         "an abstract by hand, ingest, re-score — the row then gets real leverage._",
         "",
         f"_{len(targets)} row(s)._  Leverage shown is the **title-only** score "
         "(noise — the table renders these as `--`); rely on **wtd** (weighted "
         "in-corpus citedness) and **pull** (title-fit 1–5) to prioritize.",
         ""]
    have_rank = not args.no_rank
    if have_rank:
        keepers = [t for t in targets if not t.get("exclude")]
        excludes = [t for t in targets if t.get("exclude")]
    else:
        keepers, excludes = targets, []

    def fmt(rows_):
        out = ["| pull | wtd | raw | lev⁰ | year | title | author | link | reason |",
               "|---|---|---|---|---|---|---|---|---|"]
        for t in rows_:
            au = (t["authors"][0] if t["authors"] else "")
            pull = t.get("pull_priority", "—")
            lk = f"[{t['link_kind'] or 'link'}]({t['link']})" if t["link"] else "—"
            out.append(f"| {pull} | {t['wtd_incorp']:.0f} | {t['raw_incorp']} | "
                       f"{t['leverage_titleonly']:.1f} | {t['year'] or '—'} | "
                       f"{t['title'][:52]} | {au[:18]} | {lk} | "
                       f"{t.get('reason','')[:60]} |")
        return out

    L += ["## Keepers (pull these)" if have_rank else "## All title-only rows (by citedness)", ""]
    L += fmt(keepers)
    if excludes:
        L += ["", "## Flagged off-thesis (exclude=true) — probably skip", ""]
        L += fmt(excludes)
    open(REPORT, "w").write("\n".join(L) + "\n")

    print(f"\nReport -> {REPORT}")
    print(f"JSON   -> {REPORT_JSON}")
    if have_rank:
        print(f"Keepers: {len(keepers)} | flagged-exclude: {len(excludes)}")
    print("\nNEXT: hand-pull abstracts for the high-wtd / high-pull rows (Stage 7 routes), "
          "ingest (publisher_fetch / opening_excerpt / html_scrape), then "
          "score_engagement.py --keys-file <changed> --upgrade + enricher cascade + build.")


if __name__ == "__main__":
    main()

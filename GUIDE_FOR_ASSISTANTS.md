# Guide for AI assistants running this pipeline

**You are reading this because someone is using this literature-review pipeline
through you (Claude Code, Codex, Cursor, or similar). Read this before you start.**

Your job here is to **guide, not just execute.** This tool's whole premise is that
a human makes the judgment calls — what counts as in scope, which papers are
relevant, which issues matter, how to weigh them. You do the mechanics (editing
config, running scripts, reading output) and *help the user think* at the points
where their judgment is required. If you simply run the stages in order without
involving them, you will produce a confident-looking but wrong result — on the
wrong topic, at a surprising cost, or scored in a way they would not endorse.

---

## 1. Principles

- **You are a guide, not an executor.** Walk the user through; don't just run.
- **The user owns the judgment calls** (scope, relevance, issues, weights). You own
  the mechanics.
- **Confirm before spending money or doing anything slow or irreversible.** The
  crawl and the scoring steps make *paid* LLM calls — potentially thousands. Never
  trigger a large run without first surfacing its likely scale to the user.
- **Offer cheap tests before big runs:** `--limit`, a single hop, a small probe.
  Let the user see consequences before committing real cost.
- **Default to proactive + narrate** (see §2). Honor the user's calibration if they
  ask for less.

---

## 2. Step 0 — Calibrate with the user *first*

Before doing any work, ask the user two quick questions and remember the answers
for the rest of the session:

1. **Guidance style — proactive or responsive?**
   - *Proactive:* you stop and surface every decision point (§4), even unprompted.
   - *Responsive:* you proceed on sensible defaults and only stop for consequential
     or irreversible choices (notably anything that spends real money).

2. **Narration — explain each stage before running it, or just run and report?**
   - *Narrate:* before each stage, briefly say in plain English what it does, what
     it will produce, and (if relevant) what it will cost — even when no input is
     needed from the user.
   - *Quiet:* run the stage and report the result, without the pre-explanation.

These are **two independent dials.** A user may want responsive guidance but full
narration, or any other mix.

**If the user doesn't choose (or you can't ask), default to proactive + narrate** —
the common failure mode is too *little* guidance, not too much. Tell the user they
can change either setting at any time ("stop narrating", "just run it", "be more
hands-on"), and honor that immediately.

---

## 3. Per-stage walkthrough

For each stage: what it does · what it produces · what to tell the user (if
narrating) · the decision point(s) it raises (see §4) · the cheap test, if any.
This is the order you actually move through the work. See the README for the full
pipeline detail; this is the assistant-facing "what to say and ask here."

**Stage 0 — Configure + seeds.** Help the user write `PROJECT_DESCRIPTION` and fill
`SEED_PAPERS` in `crawl_citation_graph.py`, and drop seed PDFs in `reading/pdfs/`
(`convert_pdfs.py` extracts text). *Produces:* a configured crawler.
→ **Decision points 1 (topic scope) and 2 (seeds).** The crawler refuses to run on
the unedited example, so do this with care, not as a formality. Don't append your
seeds to the example list — replace it.

**Stage 1 — Mine seed bibliographies.** `parse_references.py` /
`parse_bibliography.py` parse the seeds' reference lists into structured citations.
*Produces:* `parsed_references.json`. Auto-discovers texts from `txt/`. Low-stakes;
narrate briefly, no real decision.

**Stage 2 — Crawl the citation graph.** `crawl_citation_graph.py` snowballs outward,
scoring each candidate 1–5 for relevance. **This is the expensive stage — one paid
LLM call per candidate that passes the keyword pre-filter.**
→ **Decision points 3 (pre-filter calibration), 4 (threshold), 5 (hops).** Always
surface the likely scale before running. **Cheap test:** start with `--hops 1`
(the default) and inspect the result before considering `--hops 2` (~10× the
papers and cost). After the crawl, check the pre-filter pass-through (aim ~20%).

**Stage 2.6 / 2.7 — Enrich the corpus.** Run `enrich_authors.py` first, then
`backfill_abstracts.py`, `rescue_by_citedness.py`, the auto-grab half of
`triage_no_abstract.py`. *Produces:* a richer graph (abstracts, authors, citedness).
These run fine before scoring. Mostly mechanical; narrate, no major decision.

**Stage 3 — Discover the issues.** `discover_issues.py` proposes candidate issues
by clustering. *Produces:* a draft issue list. → **Decision point 6 (issue
pruning):** the user must prune/edit the draft into `issues_final.json`. This is a
core judgment call — do not auto-accept the model's proposed issues.

**Before scoring — Set issue weights.** In `issues_final.json`, each issue has a
`weight` (and optionally `core: true/false`). → **Decision point 7 (weights):**
explain the leverage formula and help the user choose, *before* the scoring run.

**Stage 4 — Score engagement.** `score_engagement.py` scores every in-scope paper
0–3 per issue. **Another paid stage** (one call per paper, per run). *Produces:*
`engagement_matrix.json`. Narrate the cost; avoid needless re-runs.

**Stage 5 — Enrich the matrix + resolve links.** The `enrich_*` scripts +
`enrich_links.py`. Mechanical; run links before building the table.

**Stage 6 — Build the table.** `build_lit_table.py` →
`reading/literature_table.html`. *Produces:* the shareable deliverable.
→ **Decision point 8 (visibility/star cutoffs):** after the user sees the table,
check whether the visible set and the star tier feel right; adjust if not.

---

## 4. The decision points (where the user must judge)

These are the moments the user — not you — must decide. Surface them per the
guidance style from §2. For each: what's at stake, and how to help.

| # | Decision | When | What's at stake · how to help |
|---|---|---|---|
| 1 | **Topic scope** | before crawl | Is this a bounded subliterature or too broad? A vague scope floods the crawl with marginal papers. Help the user state the debate precisely. |
| 2 | **Seed selection** | Stage 0 | The crawl is only as good as its seeds. Are these the central works an expert would name? Suggest obvious missing seeds; flag off-topic ones. |
| 3 | **Pre-filter calibration** | Stage 2 | The cost gate. Both arms of the keyword filter must genuinely narrow; aim ~20% pass-through. Help the user pick selective, multi-word terms for *their* topic, not field-wide words. |
| 4 | **Threshold (3 vs 4)** | Stage 2 | Breadth vs. tightness/cost. 3 = broad contextual corpus; 4 = tighter, cheaper, more on-topic. Recommend 4 for a focused topic. |
| 5 | **Hops (1 vs 2)** | Stage 2 | Core vs. comprehensive — and a ~10× cost cliff. Hop 1 usually captures most top-leverage works. Start at 1; justify any move to 2. |
| 6 | **Issue pruning** | Stage 3 | Do the proposed issues carve the debate at its real joints? The user must prune/merge/rewrite them. Don't accept the draft uncritically. |
| 7 | **Issue weights** | before scoring | What the leverage ranking emphasizes. Explain that leverage = weighted sum of depth over core issues; up-weight what's central to the user's thesis. |
| 8 | **Visibility / star cutoffs** | after table | Does the top tier feel right? If 300 papers are "visible" and dozens "starred", the signal is diluted. Help the user tighten cutoffs to a usable shortlist. |

---

## 5. Cost vigilance (a standing duty)

The single most important thing to get right: **never start a large paid run without
first surfacing its likely scale to the user.** The crawl and the engagement-scoring
stage are where real money goes. Concretely:

- Prefer `--hops 1` and `--threshold 4` until the user explicitly wants more.
- Before a crawl, estimate or at least *flag* how many papers may be scored.
- After a crawl, check the pre-filter pass-through rate (count scores with reason
  `"failed keyword pre-filter"` vs. the rest); much above ~20% means the filter is
  too loose and the next run will overspend — help the user tighten it.
- Avoid needless re-runs of `score_engagement.py` (each one re-scores its scope).

When in doubt, do the cheap version first and show the user the result.

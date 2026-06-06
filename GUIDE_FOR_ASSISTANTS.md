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
*Produces:* `parsed_references.json`. Auto-discovers texts from `txt/`. Two things to
get right here: **(a) run the correct parser** — run `detect_formats.py` and follow
its printed `parser` field, not the format label; an end-of-paper numbered
*References* section ("endnotes") goes to `parse_bibliography.py`, page-bottom
footnotes to `parse_references.py`. The wrong one silently mines nothing.
**(b) This stage IS paid** — both parsers make batched LLM calls (cheap, a few cents
per paper, but not free). Narrate it; the cost-vigilance rule (§5) applies here too,
just at small scale. No *judgment* decision, but don't call it free.

**Stage 2 — Crawl the citation graph.** `crawl_citation_graph.py` snowballs outward,
scoring each candidate 1–5 for relevance. **This is the expensive stage — one paid
LLM call per candidate that passes the keyword pre-filter.**
→ **Decision points 3 (pre-filter calibration), 4 (threshold), 5 (hops).** Always
surface the likely scale before running. **Cheap test:** start with `--hops 1`
(the default). After hop 1 the crawl prints a **forecast of what hop 2 would cost**
(projected paid scoring calls) and the pre-filter pass-through rate — read both out
to the user, then `--resume --hops 2` only if the projected cost is acceptable. (Tell
the user they can set `SCORE_COST_PER_1K` to their per-1,000 rate for a $ figure.)

**Stage 2.6 / 2.7 — Enrich the corpus.** Run `enrich_authors.py` first, then
`backfill_abstracts.py`, `rescue_by_citedness.py`, the auto-grab half of
`triage_no_abstract.py`. *Produces:* a richer graph (abstracts, authors, citedness).
These run fine before scoring. Mostly mechanical; narrate, no major decision.

**Stage 3 — Discover the issues.** `discover_issues.py` proposes candidate issues
by clustering. *Produces:* a draft issue list. → **Decision point 6 (issue
pruning):** the user must prune/edit the draft into `issues_final.json`. This is a
core judgment call — do not auto-accept the model's proposed issues.

**Before scoring — Set issue weights.** In `issues_final.json`, each issue has a
`weight` (default 1.0) and optionally `core: true/false`. → **Decision point 7
(weights):** explain that leverage = Σ (issue weight × engagement depth) over the core
issues, so raising an issue's weight makes deep engagement there count for more. Help
the user up-weight what's central to their thesis, *before* the scoring run.

**Stage 4 — Score engagement.** `score_engagement.py` scores every in-scope paper
0–3 per issue. **Another paid stage** (one call per paper, per run). *Produces:*
`engagement_matrix.json`. Narrate the cost; avoid needless re-runs.

**Stage 5 — Enrich the matrix + resolve links.** The `enrich_*` scripts +
`enrich_links.py`. Mechanical; run links before building the table.

**Stage 6 — Build the table.** `build_lit_table.py` →
`reading/literature_table.html`. *Produces:* the shareable deliverable.
→ **Decision point 8 (visibility/star cutoffs):** the cutoffs AUTO-SCALE to this
corpus by default (star ≈ top 5%, visible ≈ top quartile), and the build prints the
leverage distribution + where they landed. Read that out to the user and check
whether the top tier feels right. If not, re-build (cheap, no LLM calls) shifting the
percentiles (`STAR_PCTL=…`/`VISIBLE_PCTL=…`) or pinning absolute cutoffs
(`STAR_MIN=…`/`VISIBLE_MIN=…`). Don't silently accept the first numbers.
→ **Decision point 9 (abstract embedding):** ask how many abstracts to embed via the
`ABSTRACTS` env var — `all` (the default; whole corpus searchable, largest file),
`visible` (above-the-fold rows only, smaller shareable file), or `none` (smallest).
It's a cheap re-run (no LLM calls), so the user can try one and switch.

**Stage 6.6 — Abstract recovery (don't stop at the first table).** ⚠️ The pipeline
reaches a finished-*looking* table here, but title-only papers are systematically
under-scored — a fifth of the corpus can be silently under-rated. **Do not treat the
ranking as trustworthy until you've closed the abstract-recovery loop**, and don't
skip it just because the user didn't ask (→ **Decision point 10**). The loop:
`backfill_abstracts.py` / `ingest_abstract_html.py` / `rescue_by_citedness.py`
(automated) → `triage_no_abstract.py` (⚠️ **paid** — it scores every candidate;
results cache to `triage_score_cache.json` so `--write` doesn't pay twice) →
`rank_handpull.py` (rank what's worth pulling) → `make_handpull_csv.py` (emit
`handpull_fill.csv`) → user pastes abstracts into the `abstract` column →
`ingest_handpull.py` (writes them into the graph + emits `handpull_ingest_keys.txt`)
→ `ingest_handpull.py --prune-matrix` → `score_engagement.py --keys-file
handpull_ingest_keys.txt --upgrade` → rebuild. A single hand-pulled abstract can move
a paper from invisible to top-ranked, so flag this proactively.

**Stage 6.8 — Closing audit pass.** Before declaring the table done, run the QA
scripts: `sep_gap_check.py` (coverage vs. an external bibliography),
`audit_anomalies.py` (dup/mislabel clusters), `consolidate_nodes.py` (fragmented
duplicates), `overlooked_texts.py` (cited-but-absent works). A linear stage-by-stage
run will end at Stage 6 unless you prompt for these — so prompt for them.

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
| 5 | **Hops (1 vs 2)** | Stage 2 | Core vs. comprehensive — and a ~10× cost cliff. Hop 1 usually captures most top-leverage works. Run hop 1, READ THE PRINTED FORECAST (it projects hop 2's paid scoring calls), then `--resume --hops 2` only if that cost is acceptable. Never pre-commit to 2. |
| 6 | **Issue pruning** | Stage 3 | Do the proposed issues carve the debate at its real joints? The user must prune/merge/rewrite them. Don't accept the draft uncritically. |
| 7 | **Issue weights** | before scoring | What the leverage ranking emphasizes. Leverage = Σ(weight × depth) over core issues; weights live in `issues_final.json` (default 1.0). Up-weight what's central to the user's thesis. |
| 8 | **Visibility / star cutoffs** | after table | Cutoffs AUTO-SCALE to the corpus (star ≈ top 5%, visible ≈ top quartile); the build prints the distribution + where they landed. Read it out, confirm the top tier feels right, and re-build with `STAR_PCTL`/`VISIBLE_PCTL` or pinned `STAR_MIN`/`VISIBLE_MIN` if not. |
| 9 | **Abstract embedding** | building table | `ABSTRACTS=all\|visible\|none` — a file-size vs. searchability tradeoff for the one self-contained HTML file. Put it to the user: whole-corpus-searchable (`all`, default) vs. smaller/shareable (`visible`). Cheap to re-run. |
| 10 | **Recover no-abstract papers before trusting the ranking** | after first table (Stage 6.6) | Title-only papers (often ~20% of the corpus) are systematically under-scored, so the first table under-rates them. Proactively run the abstract-recovery loop (`rank_handpull.py` → `make_handpull_csv.py` → user pastes → `ingest_handpull.py` → re-score → rebuild) and the closing audit pass — don't end at the first confident-looking table. A linear run skips this unless you raise it. |

---

## 5. Cost vigilance (a standing duty)

The single most important thing to get right: **never start a large paid run without
first surfacing its likely scale to the user.** The crawl and the engagement-scoring
stage are where real money goes. Concretely:

- Prefer `--hops 1` and `--threshold 4` until the user explicitly wants more.
- **Use the built-in forecast.** After hop 1 the crawl prints a projection of hop 2's
  paid scoring calls (and a `$` figure if `SCORE_COST_PER_1K` is set). Read it to the
  user and get explicit sign-off before `--resume --hops 2`. This is the concrete form
  of "surface the scale before spending."
- After a crawl, check the pre-filter pass-through rate (count scores with reason
  `"failed keyword pre-filter"` vs. the rest); much above ~20% means the filter is
  too loose and the next run will overspend — help the user tighten it.
- Avoid needless re-runs of `score_engagement.py` (each one re-scores its scope).

When in doubt, do the cheap version first and show the user the result.

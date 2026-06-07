# Literature-review pipeline

> ### ⚠️ BETA — rough edges, feedback wanted
> This is an early public release of a personal research workflow. It works, but
> it was built for one person's projects and *will* have sharp corners when you
> point it at a new topic. **The most useful thing you can do is tell us where it
> falls short for *your* purposes** — and you don't have to spot everything
> yourself. If you're running this inside an AI coding assistant (Claude Code,
> Codex, Cursor, …), **ask it to help you notice limitations and turn them into
> concrete suggestions** as you go. See
> [**Help us improve it — finding limitations with your assistant**](#help-us-improve-it--finding-limitations-with-your-assistant)
> below, then [open an issue](../../issues).

> ### 🤖 Running this with an AI assistant? Point it at the guide first.
> This pipeline is meant to be *driven* by an assistant (Claude Code, Codex,
> Cursor, …) but the **human makes the judgment calls** — scope, relevance,
> issues, weights, cost tradeoffs. **Tell your assistant to read
> [`GUIDE_FOR_ASSISTANTS.md`](GUIDE_FOR_ASSISTANTS.md) before starting.** It tells
> the assistant to guide you through the choices (and to confirm before spending
> money on a large crawl), not just execute. The guide's first step is to ask how
> hands-on you want it to be.

A reusable, **topic-agnostic** workflow for building a **citation-graph-driven
literature review** in a focused subliterature, then sorting it by *issue* into a
reading order. You give it a few seed papers and a description of your topic; it
snowballs outward through citations, scores everything for relevance and for how
deeply each work engages the questions you care about, and produces a searchable
HTML table you can read and share.

It was built for a philosophy project (moral and legal responsibility for
negligence) and is written so it can be **replicated for a new subliterature in a
fresh project folder** — you swap in your topic, seeds, and issues.

```
 seed papers
     │  extract their bibliographies
     ▼
 parsed references ──┐ resolve to DOIs / OpenAlex / Semantic Scholar IDs
     ▼               ▼
 citation crawl ◀────┘  snowball outward N hops: each paper's references
     │                  (backward) + citing papers (forward), LLM-scored for
     ▼                  relevance 1–5, kept if ≥ threshold
 citation_graph.json    (thousands of nodes — the "body of literature")
     │
     ├─▶ issue discovery   cluster the relevant tier; the model proposes candidate
     │                     ISSUES (as questions). You prune → issues_final.json
     ├─▶ engagement scoring every in-scope paper scored 0–3 for DEPTH on each issue
     │                     → engagement_matrix.json  (NOT a partition)
     ├─▶ enrichment        work type, citedness (reconciled), authors, links
     ├─▶ abstract backfill recover missing abstracts so title-only papers aren't
     │                     systematically under-scored
     ▼
 reading/literature_table.html   ← the shareable, searchable deliverable
```

---

## Sample outputs

Three complete pipeline runs on real subliteratures — each folder contains the
ranked HTML table and the `issues_final.json` that drove the scoring:

| Topic | Live table | Issues |
|---|---|---|
| AI / Patient Preference Predictors | [▶ open table](https://jlschwenkler.github.io/lit-survey-beta/examples/sample_output/ppp/literature_table.html) · [issues_final.json](examples/sample_output/ppp/issues_final.json) | 7 (A–G) — medical ethics, autonomy, surrogate decision-making |
| Practical & non-observational knowledge of action | [▶ open table](https://jlschwenkler.github.io/lit-survey-beta/examples/sample_output/practical-knowledge/literature_table.html) · [issues_final.json](examples/sample_output/practical-knowledge/issues_final.json) | 6 (A1–A6) — philosophy of action, Anscombe, knowledge-how |
| Negligence in ethics and law | [▶ open table](https://jlschwenkler.github.io/lit-survey-beta/examples/sample_output/negligence/literature_table.html) · [issues_final.json](examples/sample_output/negligence/issues_final.json) | 7 (A1–A5b) — culpability, reasonable-person standard, mens rea |

Each table is fully live — search box, sortable columns, inline abstracts, issue
filter chips. The `issues_final.json` alongside each one shows the single file
you supply to get there.

---

## Is this a "Claude Code" tool? (No — it's standalone Python)

A common question: **does this only work inside Claude Code, or can I use it from
Codex / Cursor / a plain terminal?**

These are **ordinary Python scripts**. They call a large language model through the
[Anthropic API](https://docs.anthropic.com/) for three jobs — scoring relevance,
proposing issues, and scoring engagement depth — using the `anthropic` Python SDK.
That has nothing to do with which coding assistant (if any) you use to run them:

- **You can run everything from a normal terminal** with `python3 script.py`.
- **You can drive it from any assistant** (Claude Code, Codex, Cursor, …) — they're
  just editing/launching the same scripts.
- All LLM calls go through **one file, [`llm_client.py`](llm_client.py)**. It
  defaults to Claude, but the provider lives in a single place: to run on OpenAI or
  another provider, you implement one backend function there — the ~10 call sites
  don't change. (An OpenAI stub is included to show exactly where.)

So: **Claude API by default, provider-swappable, assistant-agnostic.**

> **If you run the scripts *from inside a Claude Code session*** and hit an
> `Illegal header value b'Bearer '` error: that's because the Claude Code harness
> sets an empty `ANTHROPIC_AUTH_TOKEN` in its environment, which the SDK prefers
> over your `ANTHROPIC_API_KEY`. Run from a plain terminal, or
> `unset ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_CUSTOM_HEADERS` first.
> Normal end-user shells don't have this problem.

---

## Quick start

```bash
# 1. Install dependencies (a virtualenv is recommended)
pip install -r requirements.txt

# 2. Set your API key (and optionally a contact email for the API "polite pool")
export ANTHROPIC_API_KEY="sk-ant-..."
export CROSSREF_MAILTO="you@example.com"   # optional but courteous; see below

# 3. Smoke-test the scoring + table build on the included synthetic sample
cp examples/sample_citation_graph.json citation_graph.json
cp examples/issues_final.example.json  issues_final.json
python3 score_engagement.py --limit 3      # scores a few synthetic papers
python3 build_lit_table.py                 # writes reading/literature_table.html
```

Open `reading/literature_table.html` in a browser to see the shape of the output.
Then delete those two staged files and start building your real corpus (below).

---

## What you must supply

The scripts are topic-agnostic **except** for a handful of things that define *your*
review. Plan to edit/provide these:

| You supply | Where | What it is |
|---|---|---|
| **Project description** | `PROJECT_DESCRIPTION` in [`crawl_citation_graph.py`](crawl_citation_graph.py) | The prose the model uses to judge "in scope." The single most important knob. |
| **Seed papers** | `SEED_PAPERS` in [`crawl_citation_graph.py`](crawl_citation_graph.py) | Your starting works (title, authors, any known DOI / OpenAlex / S2 id). |
| **Keyword pre-filter** | the keyword regex in `crawl_citation_graph.py` | A cheap gate before paid LLM scoring. Tune it to your subfield's vocabulary. |
| **Your issues + weights** | `issues_final.json` (start from [`examples/issues_final.example.json`](examples/issues_final.example.json)) | The questions you score depth against, each with a `weight` (default 1.0) and optional `core` flag. Build from the issue-discovery pass, then hand-prune. Leverage, the CORE set, and the filter chips all derive from this file automatically — no code edits needed. |
| **Table title** | `PROJECT_NAME` env var | The table's `<title>`/`<h1>` (e.g. `export PROJECT_NAME="End-of-life AI ethics"`). |
| **API keys** | environment | See below. |

The shipped values are the **negligence example** — treat them as a worked
template to replace, not as defaults.

### API keys (environment variables, never commit them)

| Variable | Needed for | Required? |
|---|---|---|
| `ANTHROPIC_API_KEY` | all LLM scoring | **yes** |
| `CROSSREF_MAILTO` | OpenAlex/Crossref "polite pool" (a contact email, not a secret) | recommended |
| `SEMANTIC_SCHOLAR_API_KEY` | higher Semantic Scholar rate limits | optional |
| `COURTLISTENER_API_KEY` | US case-law fetch (legal projects only) | optional |
| `PROJECT_NAME` | sets the table's title/heading (e.g. `"End-of-life AI ethics"`) | optional |
| `ABSTRACTS` | how many abstracts to embed in the output table — `all` (default), `visible`, or `none`; see [The output table](#stage-6--the-outputs-you-read) | optional |
| `VISIBLE_MIN` / `STAR_MIN` | pin **absolute** leverage cutoffs for the visible set / star tier, overriding auto-scaling (e.g. `STAR_MIN=15`); see [Tuning the table](#stage-6--the-outputs-you-read) | optional |
| `VISIBLE_PCTL` / `STAR_PCTL` | shift the **auto-scale** percentiles (defaults 75 / 95 → top quartile visible, top ~5% starred) | optional |
| `SCORE_COST_PER_1K` | your $ per 1,000 LLM scoring calls — adds a rough dollar figure to the crawl's per-hop [cost forecast](#cost--tuning-read-before-your-first-crawl) | optional |

---

## Cost & tuning (read before your first crawl)

The crawl makes one **paid LLM call per candidate** that passes the keyword
pre-filter. On a real topic this can be **thousands of papers** — the single place
this tool can run up a surprising bill. Three knobs control it; tune them *before*
a big run.

**1. Hops — the biggest cost multiplier.** Each hop fans out roughly
*exponentially*: hop 2 typically scores **~10× as many papers as hop 1**. In our two
real test projects, **hop 1 alone captured ~⅔–70% of the highest-leverage ("star")
papers.** So:

> **Start with `--hops 1`.** It's the default now. It gives you the core literature
> cheaply. Add `--hops 2` only when you deliberately want broad/contextual coverage
> and accept the ~10× cost.

**2. Threshold — how strict relevance must be to keep a paper.**
`--threshold 3` (default) keeps broader contextual literature; **`--threshold 4`** is
tighter, cheaper, and more directly on-topic (a smaller final table). If your topic
is focused, prefer 4.

**3. The keyword pre-filter — your cost gate.** This cheap regex decides which
candidates are worth a paid LLM call. **Calibrating it well is the difference
between a 20% and a 50%+ pass-through rate** — i.e. between scoring 7,000 and 12,000
papers. It's a *method*, not a word list:

- The gate passes a candidate if it matches **both arms** (`PHIL_TERMS` AND
  `LAW_TERMS` in `crawl_citation_graph.py`) **or** a narrow high-signal escape term.
  Think of the two arms as **two independent dimensions** of your topic.
- **An AND-gate is only as selective as its weakest arm.** If one arm matches
  "everything in your field" (e.g. `care`, `decision`, `patient`, `model`), the AND
  buys you nothing and half your field floods through.
- **Prefer multi-word phrases over single common words.** Single broad field terms
  (`AI`, `autonomy`, `care`) are almost always too permissive alone; tight phrases
  (`patient preference predictor`, `substituted judgment`) are near-perfect.
- **Aim for ~20% pass-through.** After a crawl, count how many entries in
  `citation_graph.json`'s scores have the reason `"failed keyword pre-filter"`
  (free) versus the rest (paid). If far more than ~20% passed, an arm is too broad.

The shipped negligence example is a good *structural* template (two genuinely
independent, selective arms). Copy its shape; replace its terms for your topic.

**Budgeting — the built-in forecast.** After each hop, the crawl prints what the
*next* hop would cost, projected from the rates it just observed (fan-out per seed ×
pre-filter pass-through). So the recommended, cost-safe workflow is:

> Run **`--hops 1`** first. Read the printed forecast — it tells you roughly how many
> papers a second hop would score (e.g. *"~12,000 paid scoring calls"*). Only if that
> is acceptable, continue with **`--resume --hops 2`** (resume picks up exactly where
> hop 1 left off — no re-scoring). The forecast also warns you if the pre-filter
> pass-through is high (> 30%), the #1 sign your filter is too loose.

The forecast counts **papers to score** (the thing that actually scales). If you know
your current price, set `SCORE_COST_PER_1K` to your dollars per 1,000 scoring calls
(e.g. `export SCORE_COST_PER_1K=0.50`) and the forecast adds a rough `$` figure too.
It's an order-of-magnitude guide (a slight over-estimate, since later hops overlap
the existing graph), meant to catch surprises before you spend — not an exact quote.

> One more expensive step to know about: `rank_handpull.py` uses the stronger
> ("smart"/Sonnet) model by default. Pass `--fast` to use the cheaper model if its
> worklist is large.

---

## Folder layout

Scripts and the data they read/write live together at the root (each script
computes its own folder and joins paths against it, so they must be co-located).
Anything *you* open by hand goes under `reading/`.

```
your-project/
├── *.py                      ← the pipeline scripts
├── llm_client.py             ← the single LLM seam (provider config lives here)
├── http_util.py              ← shared HTTP session / contact email / TLS policy
├── citation_graph.json       ← the crawled graph (generated; gitignored)
├── engagement_matrix.json    ← the issue × depth matrix (generated; gitignored)
├── issues_final.json         ← YOUR issues (you build this)
├── txt/                      ← extracted full text of seed PDFs
├── examples/                 ← synthetic sample data + issue template (shipped)
└── reading/                  ← HUMAN-FACING outputs you open and read
    ├── pdfs/                 ←   seed/article PDFs you drop in
    ├── literature_table.html ←   the sortable, searchable browser table
    └── …                      ←   reading order, gap reports, case summaries
```

Move the project folder anywhere; paths follow, because they're all relative to
each script's own location.

---

## The pipeline, stage by stage

Run roughly in this order. Most scripts are **safe to re-run** (they skip work
already done). Run any script with no arguments, or check the docstring at the top
of the file, to see its options.

**Stage 0 — Seeds + their full text.** Put starting PDFs in `reading/pdfs/`;
`convert_pdfs.py` extracts each to `txt/<stem>.txt`. Edit `PROJECT_DESCRIPTION`
and `SEED_PAPERS` in `crawl_citation_graph.py`.

**Stage 1 — Mine the seeds' bibliographies.** The crawl is far richer starting
from real reference lists. `parse_references.py` (numbered/footnote bibliographies)
and `parse_bibliography.py` (author-date *References* sections) parse raw
bibliography text into structured citations and resolve each to an ID →
`parsed_references.json`. Book bibliographies are the most productive mining
instrument — they surface in-field works the API crawl misses. Helpers:
`detect_formats.py`, `extract_citations.py`.

> **Which parser?** Run `detect_formats.py` first and follow the **`parser`** field
> it prints for each paper, *not* the bare format label. A numbered *References*
> section at the end of a paper (the BMJ / JAMA / J Med Ethics / J Med Philos norm)
> is mined by `parse_bibliography.py`, even though its format reads as "endnotes";
> page-bottom footnotes go through `parse_references.py`. Routing the wrong way
> finds zero blocks and silently mines nothing.
>
> **This stage is cheap but not free.** Both parsers call the LLM (batched, fast
> model) to structure each reference block — typically a few cents per paper. It's
> small, but it *is* paid: the project's "never spend without surfacing it" rule
> applies here too, just at small scale.

**Stage 2 — Crawl the citation graph.** `crawl_citation_graph.py` is the heart of
it: snowball expansion, hop by hop, fetching references (backward) and citing
papers (forward) from Semantic Scholar (primary) and OpenAlex (fallback). Each new
paper gets a cheap keyword pre-filter, then an LLM relevance score 1–5; keep ≥
threshold. Checkpoints after each hop; `--resume` continues safely. →
`citation_graph.json`. Forward edges have good coverage; backward (reference)
edges are sparse, so a low in-corpus citation count is **uninformative**, not
evidence a work is peripheral. *Targeted expansion:* `expand_wave3.py` /
`expand_wave4.py` are copy-and-edit templates for re-crawling a specific
neighborhood; `overlooked_texts.py` reports works your seeds cite that aren't in
the graph yet.

**Stage 2.6 / 2.7 — Enrich the corpus *before* discovering issues.** Issue
discovery clusters whatever is in the corpus, so enrich it first (and re-run
whenever later steps pull in new works). Run **`enrich_authors.py` first**
(author-less nodes silently defeat consolidation, abstract-matching, and recovery
downstream), then the automatic abstract backfill (`backfill_abstracts.py`),
`rescue_by_citedness.py`, and the auto-grab half of `triage_no_abstract.py`. These
depend only on the built graph, not the issues, so they run fine at this early
stage (before any engagement matrix exists). Run plain `backfill_abstracts.py`
here — it scopes to relevant nodes automatically; `--all` (whole graph, including
already-rejected papers) is rarely needed and much slower.

**Stage 3 — Discover the issues.** `discover_issues.py` embeds and clusters the
relevant tier and asks the model to propose candidate issues (as questions) per
cluster → a draft list. **You** prune and edit this into `issues_final.json` (the
schema: a JSON object with `issues` and `depth_scale` keys — see the example).
*Optional deps:* this step uses `sentence-transformers` (embeddings) plus
`umap-learn` + `hdbscan` (clustering); skip it and write `issues_final.json` by
hand if you'd rather not install them.

> **Tuning clusters:** if `--min-cluster 6` (default) produces one or two giant
> clusters, lower it (`--min-cluster 4` or `--min-cluster 3`). A giant cluster
> (>50% of papers) means the setting is too coarse; the output is still useful
> reading material, but lower the setting and re-read before curating. The
> fine-grained output is typically more useful than the coarse one.
>
> **Issue IDs:** keep them short — single letters or two characters (`A`, `B`, `C`
> or `Q1`, `Q2`). They appear as narrow column headers in the table; longer IDs
> like `PPP1` overflow the column and look cluttered. The label and question carry
> the human-readable meaning; the ID is just a stable key.

> **`--min-cluster` is finicky — treat the output as raw material, not a verdict.**
> HDBSCAN is sensitive: on a few-hundred-paper corpus the default `--min-cluster 6`
> can dump 80% of the corpus into one giant blob (uselessly generic issues), while
> dropping to `--min-cluster 4` can over-split into dozens of tiny clusters. There's
> no single right setting — **sweep a couple of values and re-read.** 1–2 huge
> clusters mean "lower `--min-cluster` and run again," not "these are your issues."
> In practice the *finer* (more, smaller) clusters are the more useful reading aid;
> the model's proposals are candidate questions for *you* to synthesize, not a
> finished issue list.

**Stage 3.5 — Consolidate duplicate nodes.** `consolidate_nodes.py` (dry-run, then
`--commit`) merges fragmented duplicate nodes (same work under a DOI, an OpenAlex
id, an S2 id…) **before** scoring, so each work is scored once. Conservative by
design. See [NOTES.md](NOTES.md) for what it can and can't catch.

**Stage 4 — Score engagement (the matrix).** `score_engagement.py` scores every
in-scope paper 0–3 for depth on each issue → `engagement_matrix.json`. This is
**not** a partition — a paper can load on several issues.
⚠️ `--keys-file` / `--limit` rewrite the *whole* matrix file (carrying untouched
rows forward); **snapshot `engagement_matrix.json` before a scoped re-score.** See
[NOTES.md](NOTES.md).

**Stage 5 — Enrich the matrix + resolve links.** `enrich_work_type.py`,
`enrich_book_kind.py`, `enrich_citedness.py` (reconciles citation counts across
OpenAlex / Crossref — see NOTES.md), `enrich_ref_provenance.py`. Then
`enrich_links.py` gives every paper without a DOI a verified URL so table titles
click through. Run links **before** building the table.

**Stage 5.6 / 6.6 / 6.7 — Abstract recovery (don't skip this).** Title-only papers
are systematically under-scored (≈1.4 lower leverage), so a table built before this
step quietly under-rates a fifth of the corpus. **Before you trust the ranking**,
close the loop:

1. **Automated recovery first** (free / cheap): automated indexes
   (`backfill_abstracts.py` — OpenAlex / Crossref / Semantic Scholar), then
   publisher-landing scraping (`ingest_abstract_html.py`), then `rescue_by_citedness.py`
   (revisits buried-but-cited works the keyword filter froze).
2. **Triage the remainder:** `triage_no_abstract.py` auto-grabs what's left and
   produces a hand-pull worklist.
   ⚠️ **This step makes paid LLM calls** (it Claude-scores every surfaced candidate),
   despite the "dry-run" framing — the default run is the *expensive* half. Scores are
   now cached to `triage_score_cache.json`, so `--write` reuses them instead of paying
   twice; pass `--refresh` only if you want to re-score from scratch.
3. **Rank what's worth pulling:** `rank_handpull.py` ranks the worklist by title-fit
   so you pull only the high-priority papers.
4. **Pull → ingest → re-score (the hand-pull round-trip):**
   - `make_handpull_csv.py` → writes `handpull_fill.csv`, one row per work with an
     empty `abstract` column and the `sibling_node_keys` paste targets.
   - You paste the real abstracts you found into that `abstract` column.
   - `ingest_handpull.py` reads the filled CSV, writes each abstract onto every node
     of the work (snapshotting the graph into `_archive/` first), and emits
     `handpull_ingest_keys.txt`.
   - `ingest_handpull.py --prune-matrix` drops those rows from the matrix, then
     `score_engagement.py --keys-file handpull_ingest_keys.txt --upgrade` re-scores them.

Then re-run Stage-5 + links + table. (Skipping this is a real quality gap, not just
polish: in testing, one hand-pulled abstract moved a paper from invisible/title-only
to the **#1 starred** result.)

**Stage 6 — The outputs you read.** `build_lit_table.py` builds
`reading/literature_table.html`: inline abstracts, a fielded + Boolean search box,
sortable by leverage. This is the shareable deliverable — **one self-contained HTML
file** with no server or build step, so you can email it or open it offline.

That self-containedness comes from embedding the abstract text in the file, which is
also the bulk of its size: on a big crawl the abstracts can push the file past
several MB. The `ABSTRACTS` env var lets you choose the tradeoff:

| `ABSTRACTS=` | What it embeds | Use when |
|---|---|---|
| `all` *(default)* | every paper's abstract | you want the whole corpus full-text-searchable in the self-contained file (the original design) |
| `visible` | abstracts for above-the-fold (visible) rows only | you want a smaller, shareable file that still reads + searches the papers that matter; hidden rows still appear, just without an expandable abstract |
| `none` | no abstract text | smallest file; titles + metadata only |

Example: `ABSTRACTS=visible python3 build_lit_table.py`. The build prints how many
abstracts it embedded, and when you choose `visible`/`none` the table itself shows a
small note that in-page search won't match the abstracts that were left out. Re-run
the build any time to switch modes — it's cheap (no LLM calls).

#### Tuning the table — leverage, weights, and the visible/star tiers

The table ranks papers by **leverage** = the weighted sum of how deeply a paper
engages each *core* issue: `Σ (issue weight × engagement depth)` over the issues
marked `core` in your `issues_final.json`. Two things you control:

- **Issue weights.** Each issue has a `weight` in `issues_final.json` (default `1.0`).
  Raise the weight on the issues most central to your thesis so deep engagement
  *there* counts for more. (The negligence example weighted its three pivotal issues
  at 1.5 — see `examples/issues_final.example.json`.) Set these *before* scoring.
- **The visible set and the star tier.** "Visible" = the default-shown shortlist;
  "★" = the short top tier. **By default these auto-scale to your own corpus** — star
  ≈ the top 5% of scored papers, visible ≈ the top quartile — so a new topic gets a
  sensibly-sized top tier without hand-tuning (this is what stops a borrowed cutoff
  from marking 345 papers "visible" and 17 "starred" on the wrong-shaped corpus).

After each build, the script prints the **leverage distribution** (percentiles) and
exactly where the cutoffs landed, e.g.:

```
Leverage distribution over 477 scored papers (weighted depth over 9 core issues):
  p10=2.0  p25=5.0  p50=8.5  p75=12.5  p90=15.0  p95=16.5  p100=20.5
  VISIBLE_MIN = 12.5  [auto: top 25% (p75)]  → 135 visible
  STAR_MIN    = 16.5  [auto: top 5% (p95)]  → 15 starred
```

If the top tier feels too big or too small, adjust and re-build (cheap, no LLM calls):

- **Shift the percentiles:** `STAR_PCTL=97 VISIBLE_PCTL=80 python3 build_lit_table.py`.
- **Pin absolute cutoffs** (disables auto-scale for that threshold):
  `VISIBLE_MIN=10 STAR_MIN=16 python3 build_lit_table.py`.

(There is also a hand-curated `STAR_HAND_KEEP` set in `build_lit_table.py` for the
rare foundational work the citedness gate overshoots — empty by default; add your own
paper keys only if you need to.)

**Stage 6.5 / 6.8 — Audits (the closing QA pass — don't end at the first table).**
A stage-driven run reaches a finished-looking table well before these run, so it's
easy to stop too early. Before you treat the table as done, make a pass:
`sep_gap_check.py` checks coverage against an authoritative external bibliography
(catches on-thesis works the keyword pre-filter froze — see NOTES.md);
`audit_anomalies.py` (dry-run) surfaces duplicate / mislabeled clusters to
hand-resolve late; `consolidate_nodes.py` catches fragmented duplicates that slipped
through; `overlooked_texts.py` reports cited-but-absent works. None are optional
polish — they're the difference between a confident-looking table and a trustworthy
one.

**Side track — case law (legal projects only).** `fetch_caselaw.py` (CourtListener),
`make_case_pdf.py`, `fetch_pdfs.py`. Skip entirely for non-legal topics.

---

## Replicating for a new subliterature

1. Copy the `*.py` files (plus `llm_client.py`, `http_util.py`, `requirements.txt`)
   into a **new project directory**. Start clean — no old `citation_graph.json`,
   `engagement_matrix.json`, `parsed_references.json`, caches, etc.
2. Edit `PROJECT_DESCRIPTION`, `SEED_PAPERS`, and the keyword pre-filter in
   `crawl_citation_graph.py`; update the title + `CORE` list in `build_lit_table.py`.
3. Drop seed PDFs in `reading/pdfs/`, run `convert_pdfs.py`, then walk Stages 1 → 6.
   The enrichment steps run **twice**: automatically up front (so issue discovery
   sees the full literature), then again after the table reveals its shape.
4. Build `issues_final.json` fresh from your own discovery pass.

Keep each subliterature in its **own folder** — separate graph, matrix, and seeds,
no shared state between projects.

The transferable gotchas (the things that bit the original project) are collected
in **[NOTES.md](NOTES.md)** — read it before trusting a published reading list.

---

## A note on data, copyright, and politeness

This tool **fetches** abstracts and citation metadata from public scholarly APIs
into local files **for your own analysis**. It does not redistribute them, and the
generated corpus files (`citation_graph.json`, `engagement_matrix.json`, the built
HTML table, anything under `reading/` / `txt/`) are **git-ignored by default** —
they aggregate third-party copyrighted abstracts at scale and are not meant to be
committed or shared. You are responsible for respecting each source's terms of use,
especially anything behind an institutional proxy or paywall.

**TLS:** the scripts verify TLS certificates by default. If you're on a network
that breaks verification (a corporate TLS-interception proxy, or a Python build
with a stale cert bundle), you can opt out for this tool only with
`export INSECURE_TLS=1`. Don't set it on an untrusted network.

---

## Help us improve it — finding limitations with your assistant

This is a beta, and the most valuable feedback is **where the tool doesn't fit
what you're actually trying to do.** You're better placed to see that than anyone
— you know your subliterature. But you don't have to diagnose it alone: if you're
working inside an AI assistant, **make it your collaborator in spotting and
articulating limitations.** It can read the scripts, watch what they produce on
*your* corpus, and help you separate "I configured this wrong" from "the tool
genuinely can't do this yet."

**As you work, watch for limitations like these:**

- **Scope mismatch** — relevance scoring keeps the wrong things, or drops works
  you know belong. (Often the `PROJECT_DESCRIPTION` or keyword pre-filter; but
  sometimes the tool's notion of relevance is too blunt for your field.)
- **Coverage gaps** — your field's key venues, languages, book chapters, or older
  works are under-represented because the APIs (OpenAlex / Crossref / Semantic
  Scholar) don't index them well. (See [NOTES.md](NOTES.md) on humanities coverage.)
- **Issue/depth scoring that doesn't match your judgment** — the 0–3 depth scores
  disagree with how you'd rate papers you know well, or the "issues" the tool
  proposes don't carve your debate at its real joints.
- **Workflow friction** — a stage that's confusing, a step that needed manual work
  the README didn't warn you about, an output format that isn't what you needed.
- **Assumptions that don't hold for your topic** — e.g. legal-case handling,
  English-language defaults, or a "leverage" ranking that doesn't suit your goals.

**Prompts you can give your assistant** (copy/paste, adapt freely):

> "I'm setting up this literature-review pipeline for *[my topic]*. Read the
> README and `crawl_citation_graph.py`, then help me decide whether the relevance
> model and keyword pre-filter actually fit my field — and flag anything that
> looks like a limitation of the tool rather than of my setup."

> "Here's the `engagement_matrix.json` / literature table it produced on my
> corpus. I know papers X and Y well. Do the depth scores match how I'd rate them?
> Where they don't, is that a fixable config issue or a real limitation worth
> reporting?"

> "Based on what we hit while running this, draft a short, concrete beta-feedback
> note: what I was trying to do, what fell short, and a suggestion for improving
> it. Keep it specific enough that the author could act on it."

Then **[open an issue](../../issues)** with what you (and your assistant) found.
Concrete beats polite: "scoring under-rated three canonical works that had no
abstract" is far more useful than "worked great." Suggestions, missing-feature
requests, and "this assumption doesn't hold for my field" reports are all welcome.
See [CONTRIBUTING.md](CONTRIBUTING.md) for what makes a report easy to act on
(and a note on not pasting private data).

---

## Citing / acknowledgment

Released under the [MIT License](LICENSE) — use it however you like; the only
requirement is keeping the copyright notice. If you use it in published research,
an acknowledgment or citation is appreciated (see [CITATION.cff](CITATION.cff)).

---

## License

MIT © 2026 John Schwenkler. See [LICENSE](LICENSE).

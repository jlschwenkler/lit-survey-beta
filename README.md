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
| **Your issues** | `issues_final.json` (start from [`examples/issues_final.example.json`](examples/issues_final.example.json)) | The questions you score depth against. Build these from the issue-discovery pass, then hand-prune. |
| **Table title + CORE list** | `HTML_TEMPLATE` and `CORE` in [`build_lit_table.py`](build_lit_table.py) | The `<title>`/`<h1>` and which issue IDs count toward "leverage." Update both when your issue IDs change (`grep -n CORE build_lit_table.py`). |
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
downstream), then the automatic abstract backfill (`backfill_abstracts.py --all`),
`rescue_by_citedness.py`, and the auto-grab half of `triage_no_abstract.py`. These
depend only on the built graph, not the issues.

**Stage 3 — Discover the issues.** `discover_issues.py` embeds and clusters the
relevant tier and asks the model to propose candidate issues (as questions) per
cluster → a draft list. **You** prune and edit this into `issues_final.json` (the
schema: a JSON object with `issues` and `depth_scale` keys — see the example).
*Optional deps:* this step uses `sentence-transformers` (embeddings) plus
`umap-learn` + `hdbscan` (clustering); skip it and write `issues_final.json` by
hand if you'd rather not install them.

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

**Stage 5.6 / 6.6 / 6.7 — Abstract recovery.** Recover missing abstracts so
title-only papers aren't systematically under-scored: automated indexes
(OpenAlex / Crossref / Semantic Scholar) first, then publisher-landing scraping
(`ingest_abstract_html.py`), then *optional* human page-saves. `rescue_by_citedness.py`
revisits buried-but-cited works; `triage_no_abstract.py` auto-grabs what's left and
produces a hand-pull worklist; `rank_handpull.py` ranks that worklist by title-fit
so you only pull what's worth it. Re-score changed rows, then re-run Stage-5 +
links + table.

**Stage 6 — The outputs you read.** `build_lit_table.py` builds
`reading/literature_table.html`: inline abstracts, a fielded + Boolean search box,
sortable by leverage. This is the shareable deliverable.

**Stage 6.5 / 6.8 — Audits.** `sep_gap_check.py` checks coverage against an
authoritative external bibliography (catches on-thesis works the keyword
pre-filter froze — see NOTES.md). `audit_anomalies.py` (dry-run) surfaces
duplicate / mislabeled clusters to hand-resolve late.

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

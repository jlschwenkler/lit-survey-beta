# Notes, gotchas, and lessons from the field

These are the hard-won, transferable lessons from running this pipeline on a real
philosophy subliterature (moral and legal responsibility for negligence). The
specific examples are from that project; the **principles** apply to any topic.
The README has the clean how-to; this file is the "things that bit us" companion.

---

## Two score scales — don't confuse them

- **Crawl relevance, 1–5** — decides whether a paper enters the graph at all
  (threshold 3). Cheap, title+abstract based, done during the crawl.
- **Engagement depth, 0–3 per issue** — how deeply an in-scope paper engages each
  of *your* issues, in the matrix. This is the thing you actually read off.

`0` absent · `1` mentions in passing · `2` substantive (a section) · `3` central
(an organizing concern of the paper).

**Leverage** = weighted sum of depth across your *core* issues (Σ weight·depth,
weights from `issues_final.json`). That's the default reading-order ranking. A
gated citation boost forms the composite that drives the ★ tier and the sort.

---

## Footgun: `score_engagement.py --keys-file` rewrites the WHOLE matrix

The script always writes the entire `engagement_matrix.json`. With `--keys-file`
(or `--limit`) it only *builds* rows for the requested subset. The current code
carries forward untouched rows before writing, so a scoped re-score no longer
nukes the rest of the matrix — but two habits still matter:

1. **Snapshot `engagement_matrix.json` before any scoped re-score.**
   (`cp engagement_matrix.json engagement_matrix.bak.json`)
2. Rows that are *re-scored* come back with base fields only, so **re-run the
   Stage-5 enrichers + `enrich_links.py` afterward**, then `build_lit_table.py`.
   Verify with a row count + duplicate-key check before trusting the result.

A single row's API error no longer zeros that row (it carries the prior scores
forward), but snapshotting first is still the rule.

---

## Duplicates / fragmented works

The same work routinely appears under several node keys (a DOI, an OpenAlex id, a
Semantic Scholar id). Two layers handle this:

- **`consolidate_nodes.py` (Stage 3.5)** — run before scoring. Groups on
  normalized title **+ first-author surname** (not title alone, which would
  false-merge three different "Negligence" chapters), guards out reviews and
  contaminant records by DOI pattern and abstract text, picks a canonical node,
  unions metadata, keeps the longest abstract, repoints edges, folds scores.
  Conservative by design (dry-run default, full log, snapshots) — it leaves a
  group un-merged rather than risk a bad merge.
- **`dedup()` in `build_lit_table.py`** — a display-time band-aid that collapses
  exact-lowercased-title twins at render, plus a conservative subtitle-truncation
  pass (merges two non-chapter rows when one normalized title is a word-boundary
  prefix of the other AND they share first-author surname AND year).

What still needs a **manual** nudge: mid-string title *variants* — e.g. Moore's
*Placing Blame* appearing as "…A Theory of the Criminal Law" vs "…A General Theory
of the Criminal Law". Different strings, so neither layer merges them. Spot-check
by scanning for repeated rows of a canonical work, or an abstract stranded on one
node while same-title-but-slightly-different siblings stay empty.

A recovered abstract is written onto **one** node; its twins stay abstract-less
unless consolidation merged them first — another reason to consolidate before you
spend effort recovering abstracts.

---

## Citation counts are unreliable for the humanities — reconcile, don't trust one source

- **OpenAlex over-counts edited-volume / handbook chapters.** It pools
  volume-level and sibling-chapter citations onto a single chapter record, so
  chapter DOIs can read 3×–50× too high. (One chapter showed 107 citations when
  Crossref and Google Scholar said ~0–2.) Worse, OpenAlex labels many chapters
  "article"/"other", so a naive book/chapter filter misses them.
- **`enrich_citedness.py` reconciles across sources** (OpenAlex + Crossref
  `is-referenced-by-count`, plus Scopus if you wire in a key): when OpenAlex is a
  high outlier vs. a corroborator, it's distrusted, the conservative corroborated
  count is shown, and the row is flagged `cite_diverged` / `cite_reliable=False`
  (greyed, not boosted).
- **Coverage is genuinely poor for philosophy** books, chapters, and many
  journal articles not indexed via Crossref/PubMed. Most rows can come back `0`
  or `None` — *not* because the work is uncited, but because the index doesn't
  know it. A low global count is therefore weak evidence of peripheral-ness.
- **`in_corpus_cites` (within-graph in-degree)** can also be near-useless if only
  a small fraction of nodes expose outgoing reference lists via the APIs. Don't
  treat a low in-corpus count as evidence a paper is peripheral.
- **Don't fix a bad citation count as a one-off row edit** — it reappears on the
  next crawl. Fix it at the source (the reconciliation logic).
- If you add a Scopus backend: Scopus is only useful for enriching citation
  *counts* after the fact. **Do not re-run the crawl on Scopus** — its API does
  not expose reference lists or forward citations in a crawlable form. Semantic
  Scholar / OpenAlex are the right tools for graph structure.

---

## The keyword pre-filter can freeze title-only works at relevance 1 (root cause)

To save money, the crawl runs a cheap **keyword pre-filter** before paying for an
LLM relevance score (`crawl_citation_graph.py`, in `score_candidate()`): a
candidate whose title (and abstract, if any) lacks the subfield keywords is
assigned `{"score": 1, "reason": "failed keyword pre-filter"}` and never seen by
the model. That score gets cached, so it's permanent across re-crawls.

This systematically buries on-thesis works whose title is generic or oblique and
that had **no abstract at crawl time**. (In the negligence corpus, an external
bibliography check found ~18 such works frozen at relevance 1; re-scoring moved 16
up, several to relevance 5. The damage is invisible without an external list,
because a relevance-1 node never enters the matrix.)

Mitigations, in order of preference:

1. **Don't delete the pre-filter** — it saves real money on the long tail. The
   bug is only treating a miss as a *confident, permanent* score.
2. **Un-freeze the cache:** mark a pre-filter miss `deferred=True` so it's
   re-examinable, rather than an immutable relevance-1.
3. **Endogenous rescue — `rescue_by_citedness.py` (Stage 6.6):** revisit deferred
   nodes once in-network citedness has accumulated and re-score the well-cited
   ones. The network itself flags which buried works deserve an abstract pull +
   re-score, with no external bibliography needed.
4. **Reactive — `sep_gap_check.py` (Stage 6.5):** check coverage against an
   authoritative external bibliography and re-score the named gaps directly
   (calling the scorer skips the pre-filter).

---

## Things to sanity-check by hand before publishing a reading list

- **Title-only rows with high leverage** are scored on the title alone and may be
  over- or under-scored. Flag `text_source == "title"` for review.
- **Non-English titles that name the whole field** (e.g. a broad German title) can
  score very high with no abstract present, because the title looks maximally
  on-topic. Verify before trusting.
- **Author-name artifacts:** the APIs sometimes return a first name as the author
  field ("Kieran", "Richard") instead of a surname. Display-only — the key and DOI
  are correct — but worth a cleanup pass on high-leverage rows.

---

## Never fabricate facts about texts — URLs included

Relevance / issue / depth scoring asks the model to judge **only supplied text**.
Link resolution (`enrich_links.py`) writes only API-returned URLs or explicit
search URLs. A wrong link is worse than no link: leave an item unlinked rather
than guess one. Nothing the model returns about a paper's *content* is invented;
it judges what it's given.

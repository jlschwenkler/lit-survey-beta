"""
score_engagement.py  —  Build the issue x paper ENGAGEMENT MATRIX (step 3).

For each paper in scope, Claude returns a DEPTH-OF-ENGAGEMENT score (0-3) for
EVERY issue in issues_final.json, in a single call. This is NOT a partition: a
paper may score high on several issues.

Text source per paper (recorded as `text_source` on each row):
  - "full"     : a matching .txt full-text file in txt/  (preferred)
  - "abstract" : title + abstract from the graph
  - "title"    : title only (no abstract available) — least reliable, flagged

Default scope: score 4+5 papers, scored from abstracts (full text used
automatically when a txt/ file matches). Re-run later with --upgrade to
re-score, from full text, any paper whose PDF has since been converted.

Outputs:
  engagement_matrix.json   — full records: scores per issue, source, notes
  engagement_matrix.md     — human-readable; per-issue ranked paper lists

Usage:
  python score_engagement.py                 # score 4+5, abstract (+full where present)
  python score_engagement.py --min-score 5   # core only
  python score_engagement.py --limit 10      # smoke test on 10 papers
  python score_engagement.py --upgrade       # re-score papers now having full text
"""

import os, json, re, time, argparse, difflib
from llm_client import call_model

FOLDER     = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH = os.path.join(FOLDER, "citation_graph.json")
ISSUES_PATH= os.path.join(FOLDER, "issues_final.json")
TXT_DIR    = os.path.join(FOLDER, "txt")
OUT_JSON   = os.path.join(FOLDER, "engagement_matrix.json")
OUT_MD     = os.path.join(FOLDER, "engagement_matrix.md")
# Hand-pull HAND_KEEP: keys the topical ranker judged on-thesis (pull_priority>=4).
# JS policy 2026-06-02 (REVISED same day — this supersedes the original rule):
# apply the SAME standard to P4 keepers as to everything else. The P4 priority is
# only a Sonnet author+title guess, so once a real abstract exists the CONTENT score
# governs. A hand_keep key is therefore admitted for (re)scoring ONLY IF it has a
# real abstract to score on (see load_papers); a no-abstract P4 is NOT force-admitted
# at title-only — it falls back to the normal --min-score gate like any other node.
# (The earlier rule admitted every P4 regardless of content-score, which propped up
# sub-threshold P4s at leverage "--"; that was dropped. Validation: when the missing
# P4+P5 abstracts were recovered and re-scored on real text, ~75% landed below
# VISIBLE_MIN — title-fit alone systematically over-rates.) Works still lacking an
# abstract show leverage "--" + a "no abstract" flag downstream in build_lit_table.py
# (a title-derived leverage number is noise; see trial_title_scoring.py) and never
# star; a below-VISIBLE_MIN row is hidden-but-sortable, not deleted. If an abstract
# turns up later the work re-scores normally. Source list: triage_handpull_ranked.json
# (pull_priority>=4, exclude=False). Falls back to empty set if the file is absent.
RANKED_PATH = os.path.join(FOLDER, "triage_handpull_ranked.json")


def hand_keep_keys():
    try:
        r = json.load(open(RANKED_PATH))["ranked"]
    except (OSError, KeyError, ValueError):
        return set()
    return {x["key"] for x in r
            if not x.get("exclude") and (x.get("pull_priority") or 0) >= 4}

MODEL = "fast"   # abstract-based scoring; cheap, one call per paper (see llm_client.py)
MAX_FULLTEXT_CHARS = 60000   # truncate very long full texts to control tokens


def load_issues():
    d = json.load(open(ISSUES_PATH))
    return d["issues"], d["depth_scale"]


def issues_ids():
    """All issue ids (e.g. A1, A2, ... C4), for building a zeroed fallback row."""
    d = json.load(open(ISSUES_PATH))
    return [i["id"] for i in d["issues"]]


def load_papers(min_score):
    g = json.load(open(GRAPH_PATH))
    nodes, scores = g["nodes"], g["scores"]
    # JS policy 2026-06-02 (revised): apply the SAME standard to P4 keepers as to
    # everything else. The P4 priority was only a Sonnet author+title guess; once a
    # real abstract exists the CONTENT score governs. So a hand_keep key is admitted
    # for (re)scoring ONLY if it has a real abstract to score on — i.e. it competes
    # on content. A hand_keep key with NO abstract is NOT force-admitted at
    # title-only any more (that earlier behavior propped up sub-threshold P4s at
    # leverage "--"); it falls back to the normal min_score gate like any other
    # node and stays buried at "--" only if its prior content score warrants it.
    keep = {k for k in hand_keep_keys() if (nodes.get(k, {}).get("abstract") or "").strip()}
    out = []
    for k, n in nodes.items():
        sc = (scores.get(k) or {}).get("score", 0)
        if sc < min_score and k not in keep:
            continue
        title = (n.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "key": k, "title": title,
            "authors": n.get("authors") or [], "year": n.get("year"),
            "score": sc, "abstract": (n.get("abstract") or "").strip(),
            "abstract_source": n.get("abstract_source"),
            # frequency-sized Google-Books "common terms" cloud (no real abstract
            # available). Used to score the work conservatively from term presence
            # + prominence when nothing better exists. {"large":[...],"medium":[...],"small":[...]}
            "gbooks_terms_tiered": n.get("gbooks_terms_tiered"),
        })
    out.sort(key=lambda r: (-r["score"], r["title"]))
    return out


# ── match a paper to a full-text file in txt/ ────────────────────────────────

def build_txt_index():
    if not os.path.isdir(TXT_DIR):
        return {}
    idx = {}
    for f in os.listdir(TXT_DIR):
        if f.endswith(".txt"):
            idx[f[:-4]] = os.path.join(TXT_DIR, f)
    return idx

def find_fulltext(paper, txt_index):
    """Strict match of a paper to a txt/ stem.

    With only ~19 full texts on disk, false positives are worse than misses
    (a mis-assigned PDF silently scores the wrong paper). So REQUIRE the first
    author's surname to appear in the stem AND a strong title-token overlap.
    """
    if not paper["authors"]:
        return None
    auth = re.sub(r"[^a-z]", "", paper["authors"][0].split()[-1].lower())
    if len(auth) < 3:
        return None
    title_tokens = set(re.findall(r"[a-z]{4,}", paper["title"].lower()))
    best, best_score = None, 0.0
    for stem, path in txt_index.items():
        s = stem.lower()
        if auth not in re.sub(r"[^a-z]", "", s):
            continue                       # author surname must be present
        stem_tokens = set(re.findall(r"[a-z]{4,}", s))
        if not title_tokens:
            continue
        overlap = len(title_tokens & stem_tokens) / len(title_tokens)
        if overlap > best_score:
            best_score, best = overlap, path
    # require the author match PLUS at least half the title's content words
    return best if best_score >= 0.5 else None


# ── Claude scoring ───────────────────────────────────────────────────────────

def build_system(issues, depth_scale):
    issue_block = "\n".join(
        f'  {i["id"]}: {i["label"]} — {i["question"]}' for i in issues)
    scale_block = "\n".join(f'  {k} = {v}' for k, v in depth_scale.items())
    # Build the JSON examples from the ACTUAL issue ids so they can never drift
    # from issues_final.json (avoids leaking another project's A5a/C3/C4 schema).
    ids = [i["id"] for i in issues]
    _scored = {iid: 0 for iid in ids}
    for iid, v in zip(ids, (3, 1, 0, 2, 1)):   # a plausible non-uniform spread
        _scored[iid] = v
    example_scored = (json.dumps(_scored)[:-1] +
                      ', "note": "argues negligence is a genuine form of culpability"}')
    example_zero = (json.dumps({iid: 0 for iid in ids})[:-1] +
                    ', "note": "title only; depth not assessable without abstract"}')
    return f"""You are scoring how deeply an academic paper (on moral and legal
responsibility for negligence — the nature of negligence and whether it is a
genuine form of culpability, objective vs. subjective fault standards,
justification and excuse, and culpable ignorance) ENGAGES each of a
fixed list of ISSUES. You will be given the paper's title and either its abstract
or its full text.

For EACH issue below, assign a DEPTH-OF-ENGAGEMENT score on this scale:
{scale_block}

Score based on how central the issue is to THIS paper's argument — not on how
important the issue is in general. A paper can score high on several issues, or
on none. Do not inflate: most papers engage only a few issues deeply. If you are
given only an abstract, score conservatively from what the abstract supports.

If you are given an AI-GENERATED SUMMARY (not the author's own abstract), be
EXTRA conservative. Such summaries often enumerate every topic the paper
touches ("issues addressed", "methods", "findings"). A topic merely LISTED or
MENTIONED there is NOT deep engagement — reserve depth 2-3 only for issues the
summary shows the paper actually develops as part of its central argument. Do
not let the summary's length or its checklist of topics raise scores above what
a normal-length abstract of the same paper would support.

If you are given the paper's OPENING EXCERPT (its first paragraph(s), not an
abstract), be EXTRA conservative in the OTHER direction: an introduction
PREVIEWS the field and announces what the paper will do, so a topic raised there
shows the paper's SETTING, not necessarily its developed contribution. Score an
issue 2-3 only when the excerpt itself makes clear the paper's central argument
engages it; a topic merely framed, motivated, or promised in the intro is at
most a 1. Do not assume the rest of the paper delivers depth the excerpt only
gestures at.

If you are given a GOOGLE-BOOKS TERM CLOUD (an unordered keyword list sized by
frequency, NOT prose and NOT an abstract), score from term presence + prominence,
and CAP every score at 2 — never assign depth 3 from a term cloud, because a word
list cannot show that an issue is the book's CENTRAL thesis. Use the size tiers:
an issue covered by one or more MOST-FREQUENT (large) terms, ideally with related
terms also present, can reach depth 2 (the book demonstrably sustains that theme);
an issue evidenced only by LESS-FREQUENT (small) or incidental terms is at most a
1; an issue with no clearly corresponding term is 0. Do not infer engagement from
a term that merely sits in the same legal field — require terms that specifically
name THIS issue's concern.

ISSUES:
{issue_block}

Return ONLY valid JSON, an object mapping each issue id to an integer 0-3, plus
a short "note" (one phrase on the paper's main engagement). Output the JSON object
and NOTHING else — no preamble, no explanation, no prose before or after it.

IMPORTANT: If you are given only a title (no abstract or text) and cannot
reliably assess depth, do NOT refuse and do NOT reply in prose. Still return the
JSON object: assign every issue 0 (or a cautious 1 only where the title clearly
names that issue's concern) and put the caveat in the "note" field. The "note"
is the ONLY place for any commentary. Example (issue ids are exactly those listed
above):
{example_scored}
Title-only example:
{example_zero}"""


def render_gbooks(tiers):
    """Render a frequency-sized Google-Books term cloud for the scorer, grouped
    by prominence so the model can use FREQUENCY as the depth signal."""
    parts = []
    for tier, label in (("large", "MOST FREQUENT (large in the cloud)"),
                        ("medium", "MODERATELY FREQUENT (medium)"),
                        ("small", "LESS FREQUENT (small)")):
        terms = tiers.get(tier) or []
        if terms:
            parts.append(f"{label}: " + ", ".join(terms))
    return "\n".join(parts)


def score_paper(system, paper, text, source, abstract_source=None):
    head = f"TITLE: {paper['title']}\nAUTHORS: {', '.join(paper['authors'][:3])}\nYEAR: {paper['year']}\n\n"
    if source == "full":
        body = f"FULL TEXT (may be truncated):\n{text[:MAX_FULLTEXT_CHARS]}"
    elif source == "abstract":
        # A vendor AI summary is NOT an author abstract — label it so the system
        # prompt's extra-conservative rule applies and a topic checklist doesn't
        # inflate depth (the "title-only problem in reverse").
        if abstract_source in ("hein_ai_summary", "ai_summary"):
            # Any vendor/AI-generated summary or "key takeaways" list — NOT the
            # author's abstract. A bulleted topic list must not inflate depth (the
            # "title-only problem in reverse"): a topic merely LISTED is not deep
            # engagement; reserve depth 2-3 for what the summary shows the paper
            # actually develops. (hein_ai_summary = HeinOnline; ai_summary = generic,
            # e.g. a publisher/Google "Key takeaways" block.)
            body = ("AI-GENERATED SUMMARY / KEY-TAKEAWAYS LIST (machine-produced, "
                    "NOT the author's abstract):\n" + text)
        elif abstract_source == "opening_excerpt":
            # The paper's first paragraph(s), not an abstract — an intro previews
            # the field; score the setting conservatively, not as developed depth.
            body = ("OPENING EXCERPT (the paper's first paragraph(s), NOT an "
                    "abstract):\n" + text)
        elif abstract_source == "gbooks_terms":
            # A frequency-sized Google-Books keyword cloud — NOT prose. Term
            # PRESENCE shows a topic appears; term PROMINENCE (large = frequent)
            # is weak evidence the book sustains it. Capped at depth 2 (see prompt).
            body = ("GOOGLE-BOOKS TERM CLOUD (an UNORDERED keyword list extracted "
                    "from the book, sized by frequency — NOT an abstract and NOT "
                    "prose; there is no argument structure here):\n" + text)
        else:
            body = f"ABSTRACT:\n{text}"
    else:
        body = "(no abstract available — score from title only)"
    txt = ""
    for attempt in range(5):           # retry on empty/transient response
        txt = call_model(
            system=system, user=head + body, model=MODEL, max_tokens=400,
        )
        if txt:
            break
        time.sleep(1.5 + attempt)      # back off: 1.5, 2.5, 3.5, 4.5s
    txt_clean = re.sub(r"^```(?:json)?\n?", "", txt); txt_clean = re.sub(r"\n?```$", "", txt_clean)
    if not txt_clean.strip():
        # Persistent empty response after all retries. Do NOT crash the run and
        # silently drop the row — record an all-zeros profile with an error note
        # so the paper still gets a matrix row and is flagged for re-scoring.
        out = {iid: 0 for iid in issues_ids()}
        out["note"] = "scoring failed: model returned empty response after retries"
        out["score_error"] = True
        return out
    try:
        return json.loads(txt_clean)
    except json.JSONDecodeError:
        # Extract just the first balanced {...} object (handles trailing prose)
        start = txt_clean.find("{")
        if start >= 0:
            depth = 0
            for j in range(start, len(txt_clean)):
                if txt_clean[j] == "{": depth += 1
                elif txt_clean[j] == "}":
                    depth -= 1
                    if depth == 0:
                        return json.loads(txt_clean[start:j+1])
        # Unparseable non-empty text: also fall back rather than crash.
        out = {iid: 0 for iid in issues_ids()}
        out["note"] = "scoring failed: unparseable model response"
        out["score_error"] = True
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-score", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--upgrade", action="store_true",
                    help="re-score papers that now have a full-text match")
    ap.add_argument("--keys-file", default=None,
                    help="score ONLY the node keys listed in this file "
                         "(one per line); bypasses --min-score scope")
    ap.add_argument("--force", action="store_true",
                    help="re-score even when source rank is unchanged (use after "
                         "a scoring-prompt change); pair with --keys-file")
    args = ap.parse_args()

    issues, depth_scale = load_issues()
    issue_ids = [i["id"] for i in issues]
    system = build_system(issues, depth_scale)
    if args.keys_file:
        want = {ln.strip() for ln in open(args.keys_file) if ln.strip()}
        papers = [p for p in load_papers(0) if p["key"] in want]
        print(f"--keys-file: {len(papers)}/{len(want)} requested keys found.")
    else:
        papers = load_papers(args.min_score)
    txt_index = build_txt_index()

    existing = {}
    if os.path.exists(OUT_JSON):
        for r in json.load(open(OUT_JSON))["rows"]:
            existing[r["key"]] = r

    rows, done, skipped, errors = [], 0, 0, 0
    todo = papers[:args.limit] if args.limit else papers

    # ── carry forward untouched rows (FOOTGUN GUARD) ──────────────────────────
    # With --keys-file (or --limit) we only score a subset, but we still write the
    # WHOLE matrix below. Without this, every row NOT in this run would silently
    # vanish from engagement_matrix.json. Seed `rows` with the existing rows whose
    # key we are NOT about to (re)score, so the file stays complete.
    todo_keys = {p["key"] for p in todo}
    carried = 0
    if args.keys_file or args.limit:
        for k, r in existing.items():
            if k not in todo_keys:
                rows.append(r); carried += 1
        if carried:
            print(f"Carrying forward {carried} untouched matrix rows.")
    print(f"Scoring {len(todo)} papers (min-score {args.min_score}) "
          f"against {len(issue_ids)} issues.")

    for i, p in enumerate(todo, 1):
        ft = find_fulltext(p, txt_index)
        eff_abstract_source = p.get("abstract_source")
        if ft:
            source, text = "full", open(ft, encoding="utf-8").read()
        elif p["abstract"]:
            source, text = "abstract", p["abstract"]
        elif p.get("gbooks_terms_tiered"):
            # No abstract, but a frequency-sized Google-Books term cloud exists.
            # Score it as a (weak) "abstract"-rank source so it isn't dropped to
            # title-only, but flag abstract_source="gbooks_terms" so score_paper
            # renders it as a capped keyword cloud (depth <=2, never 3).
            source = "abstract"
            text = render_gbooks(p["gbooks_terms_tiered"])
            eff_abstract_source = "gbooks_terms"
        else:
            source, text = "title", ""

        prev = existing.get(p["key"])
        prev_errored = prev and (prev.get("note","").startswith("[error"))
        # Re-score when --upgrade and the available text is now BETTER than what
        # the prev row was scored from (full > abstract > title). This catches
        # title-only rows that just gained an abstract (html_scrape backfill),
        # not only abstract→full upgrades.
        SRC_RANK = {"full": 2, "abstract": 1, "title": 0}
        is_upgrade = (args.upgrade and prev
                      and SRC_RANK.get(source, 0) > SRC_RANK.get(prev.get("text_source"), 0))
        if prev and not prev_errored and not is_upgrade and not args.force:
            rows.append(prev); skipped += 1; continue

        try:
            scoremap = score_paper(system, p, text, source,
                                    abstract_source=eff_abstract_source)
            row = {
                "key": p["key"], "title": p["title"], "authors": p["authors"],
                "year": p["year"], "score": p["score"], "text_source": source,
                "scores": {iid: int(scoremap.get(iid, 0)) for iid in issue_ids},
                "note": scoremap.get("note", ""),
            }
            rows.append(row); done += 1
            tag = "FULL" if source == "full" else ("abs" if source == "abstract" else "title-only")
            hot = ",".join(f"{iid}={row['scores'][iid]}" for iid in issue_ids if row["scores"][iid] >= 2)
            print(f"  [{i}/{len(todo)}] ({tag}) {p['title'][:50]:50} {hot}")
        except Exception as e:
            errors += 1
            print(f"  [{i}/{len(todo)}] ERROR {p['title'][:50]}: {e}")
            # FOOTGUN GUARD: an API/parse error must NOT zero out a row that already
            # had real scores. If a prior matrix row exists, carry its scores forward
            # (keep its text_source too — we didn't actually re-score on the new text),
            # only attaching an error note + flag. Only fall back to all-zeros for a
            # genuinely NEW row that has no prior scores to preserve.
            prev = existing.get(p["key"])
            if prev and sum(v or 0 for v in (prev.get("scores") or {}).values()) > 0:
                kept = dict(prev)
                kept["score_error"] = True
                kept["note"] = (prev.get("note") or "") + f" [rescore error, prior kept: {e}]"
                rows.append(kept)
                print(f"        ↳ kept prior scores (text_source={prev.get('text_source')}); not zeroed")
            else:
                rows.append({"key": p["key"], "title": p["title"], "authors": p["authors"],
                             "year": p["year"], "score": p["score"], "text_source": source,
                             "scores": {iid: 0 for iid in issue_ids},
                             "score_error": True, "note": f"[error: {e}]"})
        time.sleep(0.15)

    json.dump({"issues": issues, "rows": rows}, open(OUT_JSON, "w"),
              indent=2, ensure_ascii=False)

    # ── Deduplicate by title for the report (same paper under multiple IDs) ──
    SRC_RANK = {"full": 2, "abstract": 1, "title": 0}
    best_by_title = {}
    for r in rows:
        t = r["title"].lower().strip()
        cur = best_by_title.get(t)
        if cur is None or (SRC_RANK[r["text_source"]], r["score"]) > \
                          (SRC_RANK[cur["text_source"]], cur["score"]):
            best_by_title[t] = r
    report_rows = list(best_by_title.values())

    # ── Markdown: per-issue ranked lists ──
    lines = ["# Engagement Matrix\n",
             f"{len(report_rows)} unique papers (from {len(rows)} rows; "
             f"duplicates merged) x {len(issue_ids)} issues. "
             f"Depth 0-3. Source: FULL / abs / title-only.\n",
             "\n*A paper may appear under several issues. Sorted within each "
             "issue by depth, then graph-score.*\n"]
    src_counts = {}
    for r in report_rows:
        src_counts[r["text_source"]] = src_counts.get(r["text_source"], 0) + 1
    lines.append(f"\nText sources: " + ", ".join(f"{k}={v}" for k,v in src_counts.items()) + "\n")

    for iss in issues:
        iid = iss["id"]
        engaged = [r for r in report_rows if r["scores"].get(iid, 0) >= 1]
        engaged.sort(key=lambda r: (-r["scores"][iid], -r["score"]))
        lines.append(f"\n## {iid} — {iss['label']}\n")
        lines.append(f"*{iss['question']}*\n\n")
        if not engaged:
            lines.append("_(no papers engage this issue)_\n"); continue
        for r in engaged:
            depth = r["scores"][iid]
            mark = {3:"***",2:"**",1:""}[depth]
            au = ", ".join((r["authors"] or [])[:2])
            src = "" if r["text_source"]=="full" else f" _({r['text_source']})_"
            lines.append(f"- [{depth}] {mark}{r['title']}{mark} — {au} ({r['year']}){src}\n")
    open(OUT_MD, "w", encoding="utf-8").write("".join(lines))

    print(f"\nScored {done}, reused {skipped}, errors {errors}.")
    print(f"Wrote:\n  {OUT_JSON}\n  {OUT_MD}")


if __name__ == "__main__":
    main()

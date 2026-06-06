"""
build_lit_table.py  —  Render the engagement matrix as a sortable, filterable
HTML table for reading/triage in a browser.

Self-contained: produces ONE .html file with inline CSS + JS (no network, no
build step). Open it directly in any browser.

Data sources (read-only):
  engagement_matrix.json   — one row per scored paper (issue depths + metadata)
  citation_graph.json      — node store; used only to resolve a DOI per paper
  links.json   (optional)  — enrich_links.py output: an API-verified URL for
                             papers with no DOI (publisher landing page, OA full
                             text, record page, or a PhilPapers search). Title
                             links fall back to this when there's no DOI.

Columns shown:
  ★  (priority flag)   author · year · title(→DOI) · work type
     · cit/yr overall · cit/yr in-corpus · then one column per ISSUE (depth 0-3)

Conventions baked in (match core_reading_order.md):
  - LEVERAGE = weighted summed depth across the CORE issues (those marked
    core:true in issues_final.json; defaults to ALL issues for a new project).
  - Default-visible rows: leverage >= VISIBLE_MIN. The rest are hidden behind a
    "show all" toggle (they are crawl-relevant but engage the core debate only
    lightly).
  - STAR: leverage >= STAR_MIN  (the top tier by the metric).
  - Duplicate node records of one paper (same title under DOI/OA/S2 keys) are
    merged, keeping the best text_source then graph-score — same rule the other
    reports use.
  - "cit/yr overall" is greyed when cite_reliable is False (book/chapter or very
    recent: citation indexes undercount these).

Usage:  python build_lit_table.py
Output: literature_table.html
"""

import os, json, re, html

FOLDER     = os.path.dirname(os.path.abspath(__file__))
READING    = os.path.join(FOLDER, "reading")   # human-facing outputs live here
GRAPH_PATH = os.path.join(FOLDER, "citation_graph.json")
MATRIX_PATH= os.path.join(FOLDER, "engagement_matrix.json")
ISSUES_PATH= os.path.join(FOLDER, "issues_final.json")  # per-issue WEIGHTS live here
LINKS_PATH = os.path.join(FOLDER, "links.json")   # enrich_links.py output (optional)
OUT_HTML   = os.path.join(READING, "literature_table.html")

# Project name shown in the table's <title> and <h1>. Set it for YOUR project via
# the PROJECT_NAME env var (e.g. export PROJECT_NAME="End-of-life AI ethics"), or
# edit the default here. The old hardcoded "Negligence literature" shipped on every
# user's table regardless of topic — this fixes that.
PROJECT_NAME = os.environ.get("PROJECT_NAME", "Literature")

# ── Abstract embedding scope (file-size vs. self-containedness tradeoff) ────────
# The table is ONE self-contained .html file you can email/share with no server.
# Embedding every abstract is what makes it fully offline-searchable, but on a big
# crawl it bloats the file (the negligence/PPP corpora ran ~0.5 MB of abstract text
# alone, pushing the file past 4 MB). This knob lets you choose the tradeoff:
#   ABSTRACTS=all      embed every paper's abstract (DEFAULT — largest file, fully
#                      searchable across the whole corpus; keeps the shared file
#                      self-contained and grep-able).
#   ABSTRACTS=visible  embed abstracts ONLY for above-the-fold (visible) rows
#                      (smaller shareable file that still preserves inline reading +
#                      search for the papers that matter; hidden rows still appear,
#                      just without an expandable abstract).
#   ABSTRACTS=none     embed no abstract text (smallest file; titles/metadata only).
# Set via env, e.g.  export ABSTRACTS=visible
ABSTRACTS  = os.environ.get("ABSTRACTS", "all").strip().lower()
if ABSTRACTS not in ("all", "visible", "none"):
    ABSTRACTS = "all"

SRC_RANK   = {"full": 2, "abstract": 1, "title": 0}

# ── Title display tidy: down-case SHOUTING titles ──────────────────────────────
# Some source-metadata titles arrive in ALL CAPS (publisher/aggregator quirk).
# This is a DISPLAY-ONLY fix — the stored graph title (authoritative metadata) is
# left untouched; we only relax all-caps titles to title case for the table.
# Non-all-caps titles pass through verbatim.
_SMALL_WORDS = {"a","an","and","as","at","but","by","for","from","in","into","nor",
                "of","on","onto","or","over","per","the","to","up","via","vs","vs.",
                "with","but","is","so"}
# Accented uppercase letters (so non-English ALL-CAPS titles count as caps too).
_UPPER_RE = re.compile(r"[A-ZÀ-ÖØ-Þ]")
_LOWER_RE = re.compile(r"[a-zà-öø-ÿ]")

def _is_all_caps(title):
    up = len(_UPPER_RE.findall(title))
    lo = len(_LOWER_RE.findall(title))
    return (up + lo) >= 4 and lo / (up + lo) <= 0.10

def _recase_word(w, first_or_last):
    # Keep an embedded acronym/roman-numeral-ish token (e.g. "II", "USA") if SHORT
    # and consonant-only or all-caps with no vowels pattern — conservative: only
    # 2-3 char all-letter tokens that are common acronyms stay uppercase.
    core = re.sub(r"[^\wÀ-ÿ]", "", w)
    # Treat as an acronym only if 2-4 letters, all-caps, AND vowel-less (incl. Y),
    # so real short words like WHY / WRY / DRY recase but MR / FFAA / BJC stay.
    if 2 <= len(core) <= 4 and core.isalpha() and core.upper() == core \
       and not re.search(r"[AEIOUYaeiouy]", core):
        return w  # likely an acronym (e.g. "MR", "FFAA") — leave as-is
    low = w.lower()
    base = re.sub(r"[^\wÀ-ÿ]", "", low)
    if base in _SMALL_WORDS and not first_or_last:
        return low
    # Title-case: capitalize first alphabetic char, lower the rest.
    out, capped = [], False
    for ch in low:
        if not capped and ch.isalpha():
            out.append(ch.upper()); capped = True
        else:
            out.append(ch)
    return "".join(out)

def tidy_title(title):
    """Return a display title; relax ALL-CAPS source titles to Title Case."""
    t = (title or "").strip()
    if not t or not _is_all_caps(t):
        return t
    words = t.split(" ")
    n = len(words)
    return " ".join(
        _recase_word(w, first_or_last=(i == 0 or i == n - 1))
        for i, w in enumerate(words) if True
    )

# ── Book-chapter ↔ parent-monograph clustering ────────────────────────────────
# OUP DOIs encode the relationship: a monograph is <stem>.001.0001 and each of
# its chapters is <stem>.003.000N. CUP: chapter = <stem(cbo…)>.NNN, book = bare
# <stem(cbo…)>. We detect, for the rows actually in the matrix, every chapter
# whose PARENT monograph is ALSO a row, then keep only ONE star per cluster (the
# highest-leverage member). This is a star-eligibility + display-grouping rule
# only — chapters and monographs remain SEPARATE rows with their own scores
# (no consolidation), per the explicit instruction not to collapse chapters into
# monographs as a rule. (2026-06-02)
def _doi_of(key):
    return key[4:].lower() if key.startswith("doi:") else None

def _stem_role(key):
    """Return (cluster_stem, role) where role ∈ {'book','chapter'} or (None,None)."""
    d = _doi_of(key)
    if not d:
        return (None, None)
    m = re.match(r"(.*?)\.001\.0001$", d)          # OUP monograph
    if m:
        return (m.group(1), "book")
    m = re.match(r"(.*?)\.00[13]\.\d{4}$", d)       # OUP chapter (.003.000N / .001.00NN)
    if m:
        return (m.group(1), "chapter")
    m = re.match(r"(.*cbo\d+)\.\d+$", d)            # CUP chapter
    if m:
        return (m.group(1), "chapter")
    m = re.match(r"(.*cbo\d+)$", d)                 # CUP whole book
    if m:
        return (m.group(1), "book")
    return (None, None)

def cluster_maps(rows, lev_of):
    """Given matrix rows and a leverage function, return:
       parent_of[chapter_key] = parent_book_key   (only when parent is in matrix)
       top_key[stem]          = highest-leverage member key of the cluster
       member_stem[key]       = stem (for any key that belongs to a detected cluster)
    Only clusters whose PARENT monograph is present in the matrix are returned."""
    sr = {r["key"]: _stem_role(r["key"]) for r in rows}
    books = {stem: k for k, (stem, role) in sr.items() if role == "book" and stem}
    # collect chapters whose parent monograph is also a row
    parent_of, chap_stems = {}, set()
    for k, (stem, role) in sr.items():
        if role == "chapter" and stem and stem in books:
            parent_of[k] = books[stem]
            chap_stems.add(stem)
    # a cluster exists ONLY when the parent book has ≥1 chapter in the matrix —
    # a standalone monograph (no chapters here) is NOT a cluster.
    members = {}
    for stem in chap_stems:
        members[stem] = {k for k, (s, _r) in sr.items() if s == stem}
        members[stem].add(books[stem])
    top_key, member_stem = {}, {}
    for stem, keys in members.items():
        # highest leverage wins; ties broken toward the monograph, then by key
        best = max(keys, key=lambda k: (lev_of[k], sr[k][1] == "book", k))
        top_key[stem] = best
        for k in keys:
            member_stem[k] = stem
    return parent_of, top_key, member_stem

# ── Leverage / ranking knobs ──────────────────────────────────────────────────
# LEVERAGE is WEIGHTED depth across the core issues: Σ weight·depth, with the
# per-issue weight read from issues_final.json (authoritative; edit it there). Each
# issue's weight defaults to 1.0; set higher weights on the issues most central to
# your thesis so deep engagement THERE counts for more. (The negligence example used
# A1=A4=A6=1.5, A2=A5b=1.0, A3=A5a=0.5 — see examples/issues_final.example.json.)
#
# VISIBLE / STAR cutoffs: by DEFAULT these AUTO-SCALE to each project's own leverage
# distribution — star = roughly the top STAR_PCTL of scored papers, visible = the top
# VISIBLE_PCTL — so a fresh topic gets a sensible "short top tier + readable shortlist"
# without hand-tuning. The absolute floors below are only a FALLBACK (tiny corpora,
# degenerate distributions). To PIN absolute cutoffs instead of auto-scaling, set the
# VISIBLE_MIN / STAR_MIN env vars (e.g. export STAR_MIN=15). The build prints the
# leverage distribution and what the cutoffs resolved to, so you can adjust informedly.
VISIBLE_PCTL = float(os.environ.get("VISIBLE_PCTL", "75"))  # top ~quartile visible
STAR_PCTL    = float(os.environ.get("STAR_PCTL",    "95"))  # top ~5% starred
VISIBLE_MIN_FALLBACK = 9.0    # used only if auto-scale can't (e.g. too few rows)
STAR_MIN_FALLBACK    = 15.0
# Env overrides PIN an absolute cutoff and disable auto-scale for that threshold:
VISIBLE_MIN_ENV = os.environ.get("VISIBLE_MIN")   # None unless user pins it
STAR_MIN_ENV    = os.environ.get("STAR_MIN")

# Citation boost: a flat bonus added to leverage for prominent papers, but GATED
# so off-topic blockbusters aren't promoted. A paper is boosted only if it both
# (a) already clears CITE_LEV_GATE on weighted leverage and (b) its cites/yr
# clears CITE_CPY_THRESHOLD. Set CITE_BOOST = 0 to disable (display-only cites).
CITE_BOOST         = 1.5   # flat bonus added to leverage for eligible papers
CITE_LEV_GATE      = 9.0   # min weighted leverage to be eligible for the boost
CITE_CPY_THRESHOLD = 3.0   # min cites/yr to be eligible for the boost

# ── Star gate: in-network (in-corpus) citedness as a CO-REQUIREMENT (2026-06-01) ─
# A star requires BOTH deep engagement (weighted leverage >= STAR_MIN) AND that
# the corpus itself leans on the paper (weighted in-corpus citedness >= floor).
# This is a deliberately SELECTIVE gate: the star list's failure mode is OVER-
# inclusiveness (high-leverage-alone stars unfamiliar/peripheral papers), so we
# prefer to occasionally drop a genuinely-important but thinly-tracked paper than
# keep noise. False negatives are recoverable for free — sort by Lev to see the
# top papers regardless of stars, and by year to find high-leverage newcomers
# that haven't accrued citations yet. So there is NO recency or zero-edge
# exemption (an earlier softened version had them; removed per the over-inclusion
# rationale — recency is handled by SORTING, not by exempting the star gate).
# NOTE: distinct from the global cites/yr boost above (external prominence); this
# gate uses the ENDOGENOUS in_corpus_cites_weighted (tiered 3/2/1 by citer tier).
STAR_CITE_FLOOR = 5.0   # min weighted in-corpus cites to earn a star
# HAND-KEEP override — node keys force-starred despite failing the citedness floor.
# Use this for genuine core works the floor overshoots (foundational/older books with
# thin TRACKED edges, or substantive pieces low on in-corpus uptake). Each entry is a
# deliberate, hand-curated exception — add the node key (e.g. "oa:W…", "doi:10.…")
# from your own corpus. Empty by default; populate it only for YOUR topic.
STAR_HAND_KEEP = set()

def norm_doi_url(url):
    """Strip a doi.org URL down to the bare DOI (for links.json kind=='doi')."""
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", url or "", flags=re.I)


def resolve_doi(key, nodes):
    """Best DOI for a paper, for the title hyperlink. None if unavailable."""
    n = nodes.get(key) or {}
    d = n.get("doi")
    if d:
        d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.I)
        return d[4:] if d.lower().startswith("doi:") else d
    if key.startswith("doi:"):
        return key[4:]
    return None


def dedup(rows):
    """Collapse duplicate records of ONE paper (best source, then score).

    The dedup key is the lowercased title PLUS a disambiguator so two DISTINCT
    works that happen to share a short generic title (e.g. a chapter called
    "Negligence" in Alexander & Ferzan vs. one in Zimmerman) are NOT merged.
    The disambiguator is the parent-book DOI stem for chapters (so chapters of
    different books never collide) and the bare key otherwise — but rows with
    NO DOI keep the title-only key so genuine cross-source dups (an OA copy and
    a DOI copy of the same article) still collapse."""
    best = {}
    for r in rows:
        t = r["title"].lower().strip()
        stem, role = _stem_role(r["key"])
        # chapters: disambiguate by parent-book stem so same-titled chapters of
        # different books survive as separate rows.
        disamb = stem if (role == "chapter" and stem) else ""
        kk = (t, disamb)
        cur = best.get(kk)
        cand = (SRC_RANK.get(r.get("text_source"), 0), r.get("score", 0))
        if cur is None or cand > (SRC_RANK.get(cur.get("text_source"), 0),
                                  cur.get("score", 0)):
            best[kk] = r

    # ── second pass: subtitle-truncation dups (added 2026-06-02) ─────────────
    # One work fragmented under two keys where one title is a TRUNCATION of the
    # other (e.g. OpenAlex keeps "…the Selfish: The Culpability of Negligence"
    # while the Crossref/DOI record is cut at "…the Selfish"). Exact-title dedup
    # above misses these. Merge two surviving NON-chapter rows when one normalized
    # title is a prefix of the other AND they share first-author surname AND year.
    # Conservative on purpose: prefix + author + year together won't fold genuinely
    # distinct works (a real different paper won't share all three with a prefix).
    def _norm(t):
        return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()
    def _fa_surname(r):
        au = r.get("authors") or []
        return surname(au[0]).lower() if au else ""
    survivors = list(best.values())
    survivors.sort(key=lambda r: len(r["title"]), reverse=True)  # longest title first
    merged, drop = [], set()
    for i, r in enumerate(survivors):
        if id(r) in drop:
            continue
        _, role_r = _stem_role(r["key"])
        nt, fa, yr = _norm(r["title"]), _fa_surname(r), r.get("year")
        for s in survivors[i + 1:]:
            if id(s) in drop:
                continue
            _, role_s = _stem_role(s["key"])
            if role_r == "chapter" or role_s == "chapter":
                continue  # never cross the chapter guard
            ns = _norm(s["title"])
            if not nt or not ns or nt == ns:
                continue
            # s's title must be a word-boundary prefix of r's (the longer one)
            is_prefix = nt.startswith(ns + " ") or nt == ns
            if not is_prefix:
                continue
            if _fa_surname(s) and fa and _fa_surname(s) == fa and s.get("year") == yr:
                # keep the better-source/score row; fold the other away
                keep = r if (SRC_RANK.get(r.get("text_source"), 0), r.get("score", 0)) >= \
                            (SRC_RANK.get(s.get("text_source"), 0), s.get("score", 0)) else s
                drop.add(id(s if keep is r else r))
    return [r for r in survivors if id(r) not in drop]


def surname(author):
    """Extract a display surname from a single author string.

    Handles both "Forename Surname" and the library "Surname, Forename" form,
    and strips trailing birth-year fragments OpenAlex sometimes appends
    (e.g. "Nelkin, Dana Kay 1966-" -> "Nelkin").
    """
    a = (author or "").strip()
    if not a:
        return ""
    if "," in a:                       # "Surname, Forename [year]" -> before comma
        return a.split(",")[0].strip()
    # "Forename M. Surname [year]" -> drop trailing year token, take last word
    toks = [t for t in a.split() if not re.match(r"^\d{3,4}[-–]?$", t)]
    return toks[-1] if toks else a


def author_short(authors):
    """'Surname' for 1, 'Surname & Surname' for 2, 'Surname et al.' for 3+."""
    if not authors:
        return "—"
    s = [surname(a) for a in authors if surname(a)]
    if not s:
        return "—"
    if len(s) == 1:
        return s[0]
    if len(s) == 2:
        return f"{s[0]} & {s[1]}"
    return f"{s[0]} et al."


WT_LABEL = {
    "article": "article", "book": "book", "book-chapter": "chapter",
    "dissertation": "thesis", "review": "review", "other": "other",
}


def main():
    nodes = json.load(open(GRAPH_PATH))["nodes"]
    mat   = json.load(open(MATRIX_PATH))
    links = json.load(open(LINKS_PATH)) if os.path.exists(LINKS_PATH) else {}
    issues = mat["issues"]
    issue_ids = [i["id"] for i in issues]
    rows = dedup(mat["rows"])

    # Per-issue weights are authoritative in issues_final.json (the matrix's own
    # `issues` block may predate the weights). Any id missing falls back to 1.0.
    # The same file may optionally mark each issue "core": true/false — CORE issues
    # are the ones counted toward LEVERAGE and shown as filter chips.
    issue_weights = {}
    issue_core = {}
    if os.path.exists(ISSUES_PATH):
        fin = json.load(open(ISSUES_PATH))
        issue_weights = {i["id"]: float(i.get("weight", 1.0))
                         for i in fin.get("issues", [])}
        issue_core = {i["id"]: bool(i.get("core", True))
                      for i in fin.get("issues", [])}
    wt_of = lambda iid: issue_weights.get(iid, 1.0)

    # CORE is derived from the actual issues, NOT hardcoded — an issue is core
    # unless issues_final.json explicitly sets "core": false. This is what makes
    # the table work for ANY topic (the old hardcoded A1..A6 produced leverage 0,
    # 0 visible rows, and mislabeled filter chips on every non-negligence project).
    CORE = [iid for iid in issue_ids if issue_core.get(iid, True)]

    # Precompute leverage for every row, then detect chapter↔monograph clusters so
    # the star rule can keep only the highest-leverage node per cluster.
    lev_of = {r["key"]: round(sum(wt_of(i) * int((r.get("scores", {})).get(i, 0))
                                  for i in CORE), 1) for r in rows}
    parent_of, cluster_top, member_stem = cluster_maps(rows, lev_of)
    # display title of each parent monograph (for the "ch. of …" tag on children)
    title_of = {r["key"]: tidy_title(r["title"]) for r in rows}

    # ── Resolve VISIBLE / STAR cutoffs (auto-scale by default; env pins absolute) ──
    # The distribution is taken over SCORED rows only (title-only/no-abstract rows
    # carry no real leverage and reviews are handled separately), so percentiles
    # reflect the papers that actually engage the issues.
    def _pctl(sorted_vals, p):
        if not sorted_vals:
            return None
        if p <= 0:   return sorted_vals[0]
        if p >= 100: return sorted_vals[-1]
        # linear interpolation between closest ranks
        idx = (p / 100) * (len(sorted_vals) - 1)
        lo = int(idx); hi = min(lo + 1, len(sorted_vals) - 1)
        return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)

    scored_levs = sorted(
        lev_of[r["key"]] for r in rows
        if not r.get("is_review")
        and r.get("text_source") != "title"
    )
    # Auto-scale needs enough rows to be meaningful; below this, use fallback floors.
    _AUTOSCALE_MIN_ROWS = 12
    can_autoscale = len(scored_levs) >= _AUTOSCALE_MIN_ROWS

    if VISIBLE_MIN_ENV is not None:
        VISIBLE_MIN = float(VISIBLE_MIN_ENV); vis_src = "env VISIBLE_MIN"
    elif can_autoscale:
        VISIBLE_MIN = round(_pctl(scored_levs, VISIBLE_PCTL), 1)
        vis_src = f"auto: top {round(100 - VISIBLE_PCTL)}% (p{round(VISIBLE_PCTL)})"
    else:
        VISIBLE_MIN = VISIBLE_MIN_FALLBACK; vis_src = "fallback (too few rows)"

    if STAR_MIN_ENV is not None:
        STAR_MIN = float(STAR_MIN_ENV); star_src = "env STAR_MIN"
    elif can_autoscale:
        STAR_MIN = round(_pctl(scored_levs, STAR_PCTL), 1)
        star_src = f"auto: top {round(100 - STAR_PCTL)}% (p{round(STAR_PCTL)})"
    else:
        STAR_MIN = STAR_MIN_FALLBACK; star_src = "fallback (too few rows)"

    recs = []
    for r in rows:
        sc = r.get("scores", {})
        lev = round(sum(wt_of(i) * int(sc.get(i, 0)) for i in CORE), 1)
        doi = resolve_doi(r["key"], nodes)
        # No DOI? fall back to an API-verified link from enrich_links.py.
        link_url = link_kind = None
        if not doi:
            lk = links.get(r["key"])
            if lk:
                if lk.get("kind") == "doi":     # DOI recovered after the graph was built
                    doi = lk.get("doi_recovered") or norm_doi_url(lk["url"])
                else:
                    link_url, link_kind = lk["url"], lk["kind"]
        wt = r.get("work_type", "article")
        # book_kind refines books (monograph / edited / collection / textbook)
        bk = r.get("book_kind")
        wt_disp = WT_LABEL.get(wt, wt)
        if wt == "book" and bk:
            wt_disp = bk
        cpy = r.get("cites_per_year")
        is_review = bool(r.get("is_review"))
        # Gated citation boost: a flat bonus to leverage for papers that are both
        # already on-topic (clear the leverage gate) AND prominent (clear the
        # cites/yr threshold). Only RELIABLE counts qualify — a divergent/inflated
        # or chapter-flagged count must not buy a boost (ties into the
        # cross-source citation fix). composite drives ★ + sort; lev still drives
        # the visible threshold and the displayed Lev column.
        # Book reviews (is_review) are tagged-not-ranked: never boosted, never
        # starred, forced below the visible fold — their DOI/title/cite count
        # belong to the reviewed book, not the review.
        boost_eligible = (not is_review
                          and bool(r.get("cite_reliable"))
                          and lev >= CITE_LEV_GATE
                          and (cpy or 0) >= CITE_CPY_THRESHOLD)
        composite = round(lev + (CITE_BOOST if boost_eligible else 0), 1)
        # ── No-abstract works (JS decision 2026-06-02) ──────────────────────────
        # A row scored from the TITLE ALONE (text_source=="title") has no abstract,
        # so its per-issue depths are a coin-flip (trial_title_scoring.py proved
        # title-only leverage is noise). Such works are admitted to the corpus on
        # ranker-judgment + in-corpus citedness (HAND_KEEP in score_engagement),
        # but we DO NOT show a leverage number for them: leverage renders as "--",
        # they carry a "no abstract" flag, they are NEVER starred, and they are not
        # forced visible by the leverage threshold (they still surface/sort by
        # citedness). If an abstract turns up later they re-score and get real
        # leverage. Reviews already handled separately.
        no_abstract = (r.get("text_source") == "title") and not is_review
        # Whether to EMBED this row's abstract text (file-size knob; see ABSTRACTS
        # above). "all" = every row; "visible" = only above-the-fold rows (same
        # condition as the `visible` flag below); "none" = no embedded abstracts.
        # A row with no embedded abstract simply renders no expand toggle and is not
        # matched by abstract-text search — its metadata still shows normally.
        _row_visible = (not is_review) and (not no_abstract) and lev >= VISIBLE_MIN
        emit_abstract = (ABSTRACTS == "all") or (ABSTRACTS == "visible" and _row_visible)
        # ── Star = deep engagement AND corpus uptake (selective; see knobs above) ──
        # The star FLAG requires WEIGHTED LEVERAGE >= STAR_MIN (not composite — the
        # leverage measure, consistent with the visible threshold) AND endogenous
        # in-corpus citedness >= STAR_CITE_FLOOR, with a hand-keep override for the
        # few genuine works the floor overshoots. composite still drives SORT.
        incorp_wtd = r.get("in_corpus_cites_weighted", 0.0) or 0.0
        cite_ok = (incorp_wtd >= STAR_CITE_FLOOR) or (r["key"] in STAR_HAND_KEEP)
        # Chapter↔monograph cluster rule: when a row belongs to a detected cluster
        # (a chapter whose parent monograph is also in the matrix, or that parent),
        # only the cluster's highest-leverage member is star-eligible. The others
        # keep their own scores/visibility but cannot independently earn a ★ —
        # avoids double-starring the same work as both chapter and book.
        stem = member_stem.get(r["key"])
        cluster_star_ok = (stem is None) or (cluster_top.get(stem) == r["key"])
        recs.append({
            "star": (not is_review) and (not no_abstract)
                    and lev >= STAR_MIN and cite_ok and cluster_star_ok,
            "parent_key": parent_of.get(r["key"]),     # set on chapters w/ parent in matrix
            "parent_title": (title_of.get(parent_of[r["key"]]) if r["key"] in parent_of else None),
            "cluster_stem": stem,                      # cluster id (None if standalone)
            "cluster_top": (stem is not None and cluster_top.get(stem) == r["key"]),
            # parent monograph whose chapters are also in the matrix (for a hint tag)
            "has_chapters": (stem is not None and cluster_top.get(stem) is not None
                             and r["key"] not in parent_of),
            "visible": (not is_review) and (not no_abstract) and lev >= VISIBLE_MIN,
            "no_abstract": no_abstract,    # title-only: leverage shown as "--"
            # leverage is meaningless for a title-only row → None renders as "--".
            "lev": (None if no_abstract else lev),
            "composite": (None if no_abstract else composite),
            "boosted": boost_eligible,
            "is_review": is_review,
            "author": author_short(r.get("authors")),
            "authors_full": "; ".join(r.get("authors") or []),
            "year": r.get("year") or "",
            "year_backfilled": bool(r.get("year_backfilled")),
            "title": tidy_title(r["title"]),
            "doi": doi,
            "link": link_url,          # non-DOI fallback URL (None if doi present)
            "link_kind": link_kind,    # landing | oa | record | philpapers_search
            "wt": wt_disp,
            "cpy": cpy,                                  # overall cites/yr
            "cpy_reliable": bool(r.get("cite_reliable")),
            "cpy_diverged": bool(r.get("cite_diverged")),  # sources disagreed
            "cpy_sources": r.get("citedness_sources") or {},
            "incorpus": r.get("in_corpus_cites", 0),     # within-corpus in-degree (raw)
            "incorpus_wtd": r.get("in_corpus_cites_weighted", 0.0),  # tiered 3/2/1
            "depths": {i: int(sc.get(i, 0)) for i in issue_ids},
            "note": r.get("note", ""),
            # abstract provenance — flag vendor AI summaries (HeinOnline) so they
            # are never mistaken for an author abstract or full text.
            "abstract_source": (nodes.get(r["key"], {}) or {}).get("abstract_source"),
            # full abstract text, embedded so the page is self-contained and the
            # search box can match abstract content (not just author/title). This is
            # the bulk of the file size on a big crawl, so it is GATED by the
            # ABSTRACTS knob (all / visible / none) via emit_abstract above — a
            # non-embedded row stores "" and renders no abstract toggle.
            "abstract": (((nodes.get(r["key"], {}) or {}).get("abstract") or "").strip()
                         if emit_abstract else ""),
            "venue": (nodes.get(r["key"], {}) or {}).get("venue", "") or "",
        })

    # default sort: starred first, then composite (leverage + cite boost) desc,
    # then cites/yr, then author. No-abstract rows have composite=None (leverage
    # is meaningless for them) — sort them as composite 0 so they fall below the
    # scored corpus but still order among themselves by in-corpus citedness (cpy).
    recs.sort(key=lambda x: (not x["star"], -(x["composite"] or 0.0),
                             -(x.get("cpy") or 0), x["author"].lower()))

    payload = {
        "issues": [{"id": i["id"], "label": i.get("label", i["id"]),
                    "question": i.get("question", ""), "core": i["id"] in CORE,
                    "weight": float(i.get("weight", 1.0))}
                   for i in issues],
        "rows": recs,
        "visible_min": VISIBLE_MIN, "star_min": STAR_MIN,
        "abstracts": ABSTRACTS,   # which abstracts were embedded (all/visible/none)
        "n_visible": sum(1 for r in recs if r["visible"]),
        "n_hidden":  sum(1 for r in recs if not r["visible"]),
        "n_star":    sum(1 for r in recs if r["star"]),
        "n_no_abstract": sum(1 for r in recs if r.get("no_abstract")),
        "n_abstract_embedded": sum(1 for r in recs if r.get("abstract")),
    }

    html_doc = (HTML_TEMPLATE
                .replace("/*DATA*/null", json.dumps(payload, ensure_ascii=False))
                .replace("/*PROJECT_NAME*/", PROJECT_NAME))
    os.makedirs(READING, exist_ok=True)
    open(OUT_HTML, "w", encoding="utf-8").write(html_doc)
    print(f"Wrote {OUT_HTML}")
    print(f"  {len(recs)} unique papers | "
          f"{payload['n_visible']} visible (lev>={VISIBLE_MIN}), "
          f"{payload['n_hidden']} hidden | {payload['n_star']} starred "
          f"(lev>={STAR_MIN}) | {payload['n_no_abstract']} no-abstract (lev '--')")
    _abs_note = {"all": "every paper", "visible": "visible rows only",
                 "none": "none"}[ABSTRACTS]
    print(f"  abstracts embedded: {_abs_note} "
          f"({payload['n_abstract_embedded']} rows) — set ABSTRACTS=all|visible|none "
          f"to change the file-size/searchability tradeoff")

    # ── Leverage distribution + how the cutoffs resolved (tuning aid) ─────────────
    # Shows where VISIBLE_MIN / STAR_MIN landed against the actual spread, so you can
    # judge whether the top tier / shortlist feel right and pin different cutoffs
    # (VISIBLE_MIN / STAR_MIN env) or percentiles (VISIBLE_PCTL / STAR_PCTL) if not.
    if scored_levs:
        qs = {p: round(_pctl(scored_levs, p), 1) for p in (10, 25, 50, 75, 90, 95, 100)}
        print(f"\n  Leverage distribution over {len(scored_levs)} scored papers "
              f"(weighted depth over {len(CORE)} core issues):")
        print("    " + "  ".join(f"p{p}={qs[p]}" for p in (10, 25, 50, 75, 90, 95, 100)))
        print(f"    VISIBLE_MIN = {VISIBLE_MIN}  [{vis_src}]  "
              f"→ {payload['n_visible']} visible")
        print(f"    STAR_MIN    = {STAR_MIN}  [{star_src}]  "
              f"→ {payload['n_star']} starred")
        if can_autoscale and VISIBLE_MIN_ENV is None and STAR_MIN_ENV is None:
            print("    (auto-scaled to this corpus. To pin absolute cutoffs: "
                  "VISIBLE_MIN=… STAR_MIN=…  |  to shift the percentiles: "
                  "VISIBLE_PCTL=… STAR_PCTL=…)")
        elif not can_autoscale:
            print(f"    (too few scored papers to auto-scale; using fallback floors. "
                  f"Pin cutoffs with VISIBLE_MIN=… STAR_MIN=… if needed.)")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>/*PROJECT_NAME*/ — engagement table</title>
<style>
  :root { --bg:#fbfbfa; --ink:#1d1d1b; --muted:#6b6b66; --line:#e3e2dd;
          --accent:#7a1f1f; --hi:#fff6e6; --star:#c8911a; }
  * { box-sizing: border-box; }
  body { font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         color:var(--ink); background:var(--bg); margin:0; padding:24px 28px 80px; }
  h1 { font-size:21px; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:16px; max-width:70ch; }
  .controls { display:flex; flex-wrap:wrap; gap:10px 18px; align-items:center;
              margin:14px 0 12px; padding:10px 12px; background:#fff; border:1px solid var(--line);
              border-radius:8px; position:sticky; top:0; z-index:20; }
  .controls label { font-size:13px; cursor:pointer; user-select:none; }
  .controls input[type=search]{ font:13px inherit; padding:5px 9px; border:1px solid var(--line);
              border-radius:6px; width:240px; }
  .controls .count { color:var(--muted); font-size:12px; margin-left:auto; }
  .controls .absnote { color:var(--muted); font-size:11.5px; flex-basis:100%;
                       font-style:italic; }
  .chip { font-size:12px; padding:3px 8px; border:1px solid var(--line); border-radius:20px;
          background:#fff; cursor:pointer; color:var(--muted); }
  .chip.on { background:var(--accent); color:#fff; border-color:var(--accent); }
  .issuekey { margin:0 0 16px; padding:12px 14px; background:#fff; border:1px solid var(--line);
              border-radius:8px; max-width:78ch; }
  .issuekey > summary { cursor:pointer; font-size:13px; font-weight:600; color:var(--ink);
              list-style:none; user-select:none; }
  .issuekey > summary::-webkit-details-marker { display:none; }
  .issuekey > summary::before { content:"\25B8 "; color:var(--muted); }
  .issuekey[open] > summary::before { content:"\25BE "; }
  .issuekey .grp { font-size:11px; text-transform:uppercase; letter-spacing:.04em;
              color:var(--muted); margin:12px 0 5px; }
  .venue { display:block; font-size:11px; color:var(--muted); margin-top:2px; }
  .issuekey dl { display:grid; grid-template-columns:auto 1fr; gap:5px 10px; margin:0; }
  .issuekey dt { font-weight:700; color:var(--accent); font-variant-numeric:tabular-nums;
              white-space:nowrap; }
  .issuekey dd { margin:0; }
  .issuekey dd b { font-weight:600; }
  .issuekey dd .qn { color:var(--muted); }
  /* search-syntax help dropdown */
  .qhelp { position:relative; display:inline-block; }
  .qhelp > summary { display:inline-flex; align-items:center; justify-content:center;
              width:22px; height:22px; border-radius:50%; border:1px solid var(--line);
              background:#fff; color:var(--muted); cursor:pointer; font-size:13px;
              font-weight:700; list-style:none; user-select:none; }
  .qhelp > summary::-webkit-details-marker { display:none; }
  .qhelp[open] > summary { background:var(--accent); color:#fff; border-color:var(--accent); }
  .qhelp .qhelpbody { position:absolute; z-index:20; top:28px; left:0; width:430px;
              max-width:88vw; padding:13px 15px; background:#fff; border:1px solid var(--line);
              border-radius:8px; box-shadow:0 6px 22px rgba(0,0,0,.12); font-size:12.5px;
              line-height:1.5; color:var(--ink); }
  .qhelp .qhelpbody p { margin:0 0 4px; }
  .qhelp .qhelpbody p + dl { margin-bottom:9px; }
  .qhelp .qhelpbody dl { display:grid; grid-template-columns:auto 1fr; gap:3px 10px; margin:0; }
  .qhelp .qhelpbody dt { font-weight:700; color:var(--accent); white-space:nowrap;
              font-variant-numeric:tabular-nums; }
  .qhelp .qhelpbody dd { margin:0; }
  .qhelp .qhelpbody code { background:#f3f1ec; padding:1px 4px; border-radius:3px;
              font-size:11.5px; }
  .qhelp .qhelpbody .qnote { color:var(--muted); font-size:11.5px; margin-top:4px; }
  .qbad { outline:2px solid #c98a2b; }   /* unparseable query hint (falls back to substring) */
  table { border-collapse:collapse; width:100%; background:#fff; }
  th,td { padding:7px 9px; border-bottom:1px solid var(--line); text-align:left;
          vertical-align:top; }
  thead th { position:sticky; top:64px; background:#f3f2ee; z-index:10; cursor:pointer;
             font-size:12px; text-transform:uppercase; letter-spacing:.03em; color:var(--muted);
             white-space:nowrap; user-select:none; }
  thead th.iss { text-align:center; padding:8px 4px; font-size:12px;
                 text-transform:none; letter-spacing:0; font-weight:600; }
  thead th.iss .id { color:var(--accent); font-weight:700; }
  thead th.numh { text-align:right; padding-left:5px; padding-right:7px; }
  thead th:hover { color:var(--ink); }
  th.sorted-asc::after  { content:" \25B2"; font-size:9px; }
  th.sorted-desc::after { content:" \25BC"; font-size:9px; }
  tbody tr:hover { background:var(--hi); }
  tr.starred td { background:#fffaf0; }
  tr.starred:hover td { background:var(--hi); }
  td.star { color:var(--star); text-align:center; width:22px; font-size:15px; }
  td.author { white-space:nowrap; font-weight:600; }
  td.year { color:var(--muted); width:46px; }
  td.year.backfill { font-style:italic; }
  td.title { max-width:64ch; }
  td.title a { color:var(--accent); text-decoration:none; }
  td.title a:hover { text-decoration:underline; }
  td.title .nolink { color:var(--ink); }
  td.title .note { display:block; color:var(--muted); font-size:11.5px; margin-top:2px;
                   font-style:italic; }
  td.title details.abs { margin-top:3px; }
  td.title details.abs > summary { display:inline-block; cursor:pointer; list-style:none;
                   color:var(--muted); font-size:10px; text-transform:uppercase;
                   letter-spacing:.03em; padding:0 5px; border:1px solid var(--line);
                   border-radius:4px; user-select:none; }
  td.title details.abs > summary::-webkit-details-marker { display:none; }
  td.title details.abs[open] > summary { color:var(--accent); border-color:var(--accent); }
  td.title details.abs .abstxt { margin-top:4px; font-size:12px; font-style:normal;
                   color:var(--ink); line-height:1.45; white-space:pre-wrap;
                   max-width:64ch; font-weight:400; }
  td.title .lk { display:inline-block; margin-left:6px; padding:0 5px; border-radius:4px;
                 font-size:10px; text-transform:uppercase; letter-spacing:.03em;
                 vertical-align:1px; background:#eceae3; color:#7c7c74; cursor:help;
                 white-space:nowrap; }
  td.title .lk-oa { background:#e6f0e6; color:#3c7a3c; }
  td.title .lk-philpapers_search { background:#f3e9e9; color:#9a5050; }
  td.title .lk-aisum { background:#ece4f0; color:#6f4f8a; }
  td.title .lk-gbooks { background:#e4ecf0; color:#4f6f8a; }
  /* chapter↔monograph grouping tags — keep mixed-case (they carry a book title) */
  td.title .lk-chap { background:#e7ecf2; color:#4a5d76; text-transform:none;
                      letter-spacing:0; white-space:normal; font-style:italic; }
  td.title .lk-mono { background:#f0ece2; color:#7a6a45; }
  td.wt { color:var(--muted); font-size:12px; white-space:nowrap; }
  td.wt .lk { display:inline-block; padding:0 5px; border-radius:4px; font-size:10px;
              text-transform:uppercase; letter-spacing:.03em; vertical-align:1px;
              cursor:help; white-space:nowrap; }
  td.wt .lk-review { background:#efe7d6; color:#8a6d2f; }
  tr td.isrev, tr.isrev td { opacity:.72; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; width:48px; white-space:nowrap;
           padding-left:5px; padding-right:7px; }
  td.num.unrel { color:#b9b9b3; }
  td.depth { text-align:center; width:20px; font-variant-numeric:tabular-nums;
             padding-left:3px; padding-right:3px; }
  .d0 { color:#d8d8d2; } .d1 { color:#9a9a92; } .d2 { color:#3a3a36; font-weight:600; }
  .d3 { color:var(--accent); font-weight:700; }
  .lev { text-align:right; font-variant-numeric:tabular-nums; width:32px; font-weight:600;
         padding-left:4px; padding-right:7px; }
  .legend { color:var(--muted); font-size:12px; margin-top:14px; line-height:1.7; }
  .legend code { background:#f0efe9; padding:1px 5px; border-radius:4px; }
  abbr { text-decoration:none; border-bottom:1px dotted var(--muted); cursor:help; }
</style>
</head>
<body>
<h1>/*PROJECT_NAME*/ — engagement table</h1>
<div class="sub">
  Papers turned up by the citation crawl, scored 0–3 for depth of engagement on
  each issue. <b>Leverage</b> = summed depth across the six core issues (A1–A6,
  max 18). Showing the higher-leverage set by default; lighter-engagement papers
  are hidden until you ask for them. <span class="star" style="color:var(--star)">★</span>
  marks priority papers (leverage ≥ <span id="starmin"></span>).
</div>

<details class="issuekey" id="issuekey">
  <summary>What the issue columns mean</summary>
  <div id="issuekeybody"></div>
</details>

<div class="controls">
  <input type="search" id="q" placeholder="search — try  AU:hurd AND TI:negligence …" autocomplete="off" spellcheck="false">
  <details class="qhelp" id="qhelp">
    <summary title="search syntax">?</summary>
    <div class="qhelpbody">
      <p><b>Fields</b> — prefix a term to limit it (default: all fields):</p>
      <dl>
        <dt>AU:</dt><dd>author — <code>AU:hurd</code></dd>
        <dt>TI:</dt><dd>title — <code>TI:negligence</code></dd>
        <dt>AB:</dt><dd>abstract — <code>AB:"quality of will"</code></dd>
        <dt>YR:</dt><dd>year — <code>YR:2011</code></dd>
      </dl>
      <p><b>Boolean</b> — combine terms:</p>
      <dl>
        <dt>AND</dt><dd>both (also the default between terms) — <code>hurd AND moore</code></dd>
        <dt>OR</dt><dd>either — <code>recklessness OR negligence</code></dd>
        <dt>NOT</dt><dd>exclude (or a leading <code>-</code>) — <code>luck NOT tort</code>, <code>-review</code></dd>
        <dt>"&nbsp;"</dt><dd>exact phrase — <code>"reasonable person"</code></dd>
        <dt>(&nbsp;)</dt><dd>group — <code>AU:moore AND (TI:negligence OR AB:culpability)</code></dd>
      </dl>
      <p class="qnote">Case-insensitive. Operators must be UPPER-CASE. A term with
        no field searches author + title + abstract. If a query won't parse, it
        falls back to a plain substring match.</p>
    </div>
  </details>
  <label><input type="checkbox" id="showall"> show all papers
    (<span id="hidct"></span> hidden)</label>
  <label><input type="checkbox" id="staronly"> ★ priority only</label>
  <span id="issuechips"></span>
  <span class="count" id="count"></span>
  <span class="absnote" id="absnote"></span>
</div>

<table id="tbl">
  <thead><tr id="head"></tr></thead>
  <tbody id="body"></tbody>
</table>

<div class="legend" id="legend"></div>

<script>
const DATA = /*DATA*/null;
// CORE issue ids, derived from the data (payload marks each issue core:true/false),
// NOT hardcoded — so the leverage chips/filters match THIS project's issues.
const CORE = DATA.issues.filter(i=>i.core!==false).map(i=>i.id);
// Short badge + tooltip for non-DOI fallback links (papers with no DOI).
const LINK_TAG = {landing:"publisher", oa:"full text", record:"record",
                  philpapers_search:"PhilPapers ⌕"};
const LINK_TITLE = {
  landing:"publisher / journal / institution page (no DOI on record)",
  oa:"open-access full text",
  record:"Semantic Scholar record page",
  philpapers_search:"PhilPapers search for this title — a results page, not a direct link"};
document.getElementById("starmin").textContent = DATA.star_min;
document.getElementById("hidct").textContent = DATA.n_hidden;
// Abstract-scope note: when abstracts were embedded for visible rows only (or not
// at all), text search can't match abstract content for the excluded rows. Tell
// the user so a missed search hit isn't mistaken for an absent paper.
(function(){
  var n = document.getElementById("absnote");
  if(!n) return;
  if(DATA.abstracts === "visible"){
    n.textContent = "abstracts embedded for visible rows only — search won't match hidden papers' abstracts (rebuild with ABSTRACTS=all for full-text search)";
  } else if(DATA.abstracts === "none"){
    n.textContent = "abstracts not embedded — search matches author/title/year only (rebuild with ABSTRACTS=all or ABSTRACTS=visible to search abstract text)";
  }
})();

// ---- build header ----
const baseCols = [
  {k:"star",  label:"",        cls:"star",  numeric:true, get:r=>r.star?1:0},
  {k:"author",label:"Author",  cls:"author",get:r=>r.author},
  {k:"year",  label:"Year",    cls:"year",  numeric:true, get:r=>r.year||null},
  {k:"title", label:"Title",   cls:"title", get:r=>r.title},
  {k:"wt",    label:"Type",    cls:"wt",    get:r=>r.wt},
  {k:"cpy",   label:"Cit/yr",  cls:"num",   numeric:true, get:r=>r.cpy},
  {k:"incorpus",label:"In-corp",cls:"num",numeric:true, get:r=>r.incorpus},
  {k:"lev",   label:"Lev",     cls:"lev",   numeric:true, get:r=>r.lev},
];
const issueCols = DATA.issues.map(is=>({
  k:"iss_"+is.id, label:is.id, id:is.id, cls:"depth", iss:is.id,
  header:"iss", numeric:true,
  title:is.id+" — "+is.label+" — "+is.question, get:r=>r.depths[is.id]
}));
const COLS = baseCols.concat(issueCols);

const head = document.getElementById("head");
COLS.forEach((c,i)=>{
  const th=document.createElement("th");
  if(c.header==="iss"){
    th.className="iss"; th.title=c.title;
    th.innerHTML=`<span class="id">${esc(c.id)}</span>`;
  } else {
    th.textContent = c.label;
    if(c.cls==="num"||c.cls==="lev"){ th.className="numh"; }
  }
  th.dataset.col=i;
  th.addEventListener("click",()=>sortBy(i));
  head.appendChild(th);
});

// ---- issue filter chips (core issues) ----
const chipWrap=document.getElementById("issuechips");
let issueFilter=null;
CORE.forEach(id=>{
  const c=document.createElement("span");
  c.className="chip"; c.textContent=id; c.title="show only papers central (depth≥2) on "+id;
  c.addEventListener("click",()=>{
    if(issueFilter===id){issueFilter=null;c.classList.remove("on");}
    else{ document.querySelectorAll(".chip").forEach(x=>x.classList.remove("on"));
          issueFilter=id; c.classList.add("on"); }
    render();
  });
  chipWrap.appendChild(c);
});

// ---- state ----
let sortCol=7, sortDesc=true;   // default: leverage desc
// First click on a column picks its natural direction: numeric → desc (high
// first), text → asc (A–Z). Clicking the active column again flips it.
function sortBy(i){
  if(sortCol===i){ sortDesc=!sortDesc; }
  else { sortCol=i; sortDesc = !!COLS[i].numeric; }
  render();
}

// ── search: fielded + Boolean query parser ───────────────────────────────────
// Grammar (case-insensitive values; UPPER-CASE operators):
//   expr   := orExpr
//   orExpr := andExpr ( "OR" andExpr )*
//   andExpr:= unary ( ("AND")? unary )*        // implicit AND between adjacent terms
//   unary  := ("NOT" | "-") unary | atom
//   atom   := "(" expr ")" | FIELD? term
//   FIELD  := ("AU"|"TI"|"AB"|"YR"|"ALL") ":"
//   term   := "quoted phrase" | bareword
// Compiles to a predicate (row)->bool. Field text getters:
function fieldText(r, f){
  if(f==="au") return (r.author+" "+r.authors_full);
  if(f==="ti") return r.title||"";
  if(f==="ab") return r.abstract||"";
  if(f==="yr") return ""+(r.year||"");
  return (r.author+" "+r.authors_full+" "+r.title+" "+(r.abstract||"")+" "+(r.year||"")); // all
}
function tokenizeQ(s){
  const toks=[]; const re=/\s*("[^"]*"|\(|\)|\b(?:AU|TI|AB|YR|ALL):|[^\s()]+)/g; let m;
  while((m=re.exec(s))!==null){ if(m[1]!=="") toks.push(m[1]); }
  return toks;
}
function parseQ(toks){
  let i=0;
  const peek=()=>toks[i];
  const eat =()=>toks[i++];
  function parseExpr(){ return parseOr(); }
  function parseOr(){
    let node=parseAnd();
    while(peek()==="OR"){ eat(); const rhs=parseAnd(); const l=node,r=rhs;
      node=row=>l(row)||r(row); }
    return node;
  }
  function parseAnd(){
    let node=parseUnary();
    while(peek()!==undefined && peek()!==")" && peek()!=="OR"){
      if(peek()==="AND") eat();           // explicit AND optional
      const rhs=parseUnary(); const l=node,r=rhs; node=row=>l(row)&&r(row);
    }
    return node;
  }
  function parseUnary(){
    if(peek()==="NOT"){ eat(); const x=parseUnary(); return row=>!x(row); }
    if(peek()&&peek()[0]==="-"&&peek().length>1){    // leading-dash exclude
      const t=eat().slice(1); const x=compileAtom(null,t); return row=>!x(row);
    }
    return parseAtom();
  }
  function parseAtom(){
    if(peek()==="("){ eat(); const e=parseExpr(); if(peek()===")") eat(); return e; }
    let field=null, t=eat();
    if(t===undefined) return ()=>true;
    const fm=/^(AU|TI|AB|YR|ALL):$/i.exec(t);
    if(fm){ field=fm[1].toLowerCase(); t=peek(); if(t===undefined) return ()=>true; eat(); }
    return compileAtom(field, t);
  }
  function compileAtom(field, raw){
    let term=raw; if(term[0]==='"'&&term[term.length-1]==='"') term=term.slice(1,-1);
    term=term.toLowerCase().trim();
    const f=(field==="all")?null:field;
    if(!term) return ()=>true;
    return row=>fieldText(row,f).toLowerCase().includes(term);
  }
  const pred=parseExpr();
  return pred;
}
// cache the compiled query so we parse once per keystroke, not once per row
let _qStr=null, _qPred=null, _qOk=true;
function compileQuery(s){
  if(s===_qStr) return;
  _qStr=s; _qOk=true;
  const trimmed=s.trim();
  if(!trimmed){ _qPred=null; return; }
  try{
    const toks=tokenizeQ(trimmed);
    _qPred=parseQ(toks);
    // sanity: ensure it runs without throwing
    _qPred({author:"",authors_full:"",title:"",abstract:"",year:""});
  }catch(e){
    _qOk=false;
    const sub=trimmed.toLowerCase();
    _qPred=row=>fieldText(row,null).toLowerCase().includes(sub);  // fallback: plain substring
  }
}

function passFilters(r){
  const raw=document.getElementById("q").value;
  compileQuery(raw);
  const hasQ=!!raw.trim();
  // A search hit AUTO-REVEALS an otherwise-hidden (below-fold) row, so a phrase
  // that only appears in a low-leverage paper's abstract still surfaces it. With
  // NO query, the fold rule applies.
  if(!hasQ && !document.getElementById("showall").checked && !r.visible) return false;
  if(document.getElementById("staronly").checked && !r.star) return false;
  if(issueFilter && r.depths[issueFilter] < 2) return false;
  if(hasQ && _qPred && !_qPred(r)) return false;
  return true;
}

function isMissing(v){ return v==null || v==="" || v==="—"; }

function cmp(a,b){
  const c=COLS[sortCol];
  const va=c.get(a), vb=c.get(b);
  // Missing values always sink to the bottom, regardless of sort direction,
  // so blank years / no-author rows don't crowd the top of an A–Z sort.
  const ma=isMissing(va), mb=isMissing(vb);
  if(ma && mb) return 0;
  if(ma) return 1;
  if(mb) return -1;
  if(c.numeric){
    return sortDesc ? (vb-va) : (va-vb);
  }
  const sa=(""+va).toLowerCase(), sb=(""+vb).toLowerCase();
  return sortDesc ? sb.localeCompare(sa) : sa.localeCompare(sb);
}

function render(){
  const rows=DATA.rows.filter(passFilters).slice().sort(cmp);
  const body=document.getElementById("body");
  body.innerHTML="";
  for(const r of rows){
    const tr=document.createElement("tr");
    if(r.star) tr.className="starred";
    COLS.forEach(c=>{
      const td=document.createElement("td");
      td.className=c.cls;
      if(c.k==="star"){
        td.textContent = r.star ? "★" : "";
      } else if(c.k==="title"){
        let inner;
        if(r.doi){
          inner=`<a href="https://doi.org/${encodeURI(r.doi)}" target="_blank" rel="noopener">${esc(r.title)}</a>`;
        } else if(r.link){
          // No DOI — API-verified fallback link. Tag what kind it is so the
          // student knows a "search" link isn't a direct hit.
          const tag = LINK_TAG[r.link_kind] || "link";
          inner=`<a href="${esc(r.link)}" target="_blank" rel="noopener">${esc(r.title)}</a>`+
                `<span class="lk lk-${r.link_kind}" title="${esc(LINK_TITLE[r.link_kind]||"")}">${tag}</span>`;
        } else {
          inner=`<span class="nolink">${esc(r.title)}</span>`;
        }
        // Chapter↔monograph grouping: tag a chapter with its parent book, and the
        // parent book with a "has chapters here" hint. The table is sortable, so the
        // relationship is shown inline (rows can't always sit adjacent). Only the
        // cluster's highest-leverage member is star-eligible (see build script).
        if(r.parent_key){
          inner+=`<span class="lk lk-chap" title="book chapter — its parent monograph `
               +`is also in this table; only the cluster's highest-leverage node is starred">`
               +`ch. of “${esc(r.parent_title||"")}”</span>`;
        } else if(r.has_chapters){
          inner+=`<span class="lk lk-mono" title="monograph — one or more of its chapters `
               +`are also in this table; only the cluster's highest-leverage node is starred">`
               +`has chapters here</span>`;
        }
        if(r.abstract_source==="hein_ai_summary"){
          // Scored from a vendor (HeinOnline) AI-generated summary, NOT an author
          // abstract or full text — flag so the depth scores are read with caution.
          inner+=`<span class="lk lk-aisum" title="scored from a HeinOnline AI-generated `
               +`summary (no author abstract exists) — treat depth scores as approximate">`
               +`AI summary</span>`;
        }
        if(r.abstract_source==="gbooks_terms"){
          // No abstract exists; scored from a frequency-sized Google-Books term
          // cloud, capped at depth 2. Flag so scores are read as approximate.
          inner+=`<span class="lk lk-gbooks" title="no abstract available — scored from a `
               +`Google-Books frequency-sized term cloud (depth capped at 2); treat scores as approximate">`
               +`term cloud</span>`;
        }
        if(r.no_abstract){
          // No abstract found at all; admitted to the corpus on topical-ranker
          // judgment + in-corpus citedness. Leverage is shown as "--" because a
          // title-only depth score is unreliable; the row surfaces by citedness
          // and is never starred. Will re-score if an abstract turns up later.
          inner+=`<span class="lk lk-noabs" title="no abstract available — included `
               +`on topical relevance + in-corpus citation count; leverage not scored `
               +`(shown as --); add an abstract to score it">`
               +`no abstract</span>`;
        }
        // Per-row scorer notes (Claude-generated one-line summaries) are no longer
        // displayed — the abstract is now loadable/readable inline, so the summary is
        // redundant. The note text remains in the matrix data (incl. any "[error:…]"
        // diagnostic) but is not rendered. JS decision 2026-06-02.
        // Expandable abstract: a small toggle that reveals the abstract text
        // inline. The text is embedded in the page (search matches it too). When
        // there's an active search query, auto-open so you can see what matched.
        if(r.abstract){
          const q=document.getElementById("q").value.trim();
          const open = q ? " open":"";
          inner+=`<details class="abs"${open}><summary>abstract</summary>`
               +`<div class="abstxt">${esc(r.abstract)}</div></details>`;
        }
        if(r.venue){
          inner+=`<span class="venue">${esc(r.venue)}</span>`;
        }
        td.innerHTML=inner;
      } else if(c.k==="wt"){
        td.textContent = (r.wt==null? "" : r.wt);
        if(r.is_review){
          const b=document.createElement("span");
          b.className="lk lk-review"; b.textContent="review";
          b.title="book review — its DOI/title/citation count belong to the "
                 +"reviewed book, so this row is not ranked";
          td.appendChild(document.createTextNode(" ")); td.appendChild(b);
          td.classList.add("isrev");
        }
      } else if(c.k==="cpy"){
        td.textContent = (r.cpy==null? "—" : r.cpy.toFixed(1));
        if(!r.cpy_reliable || r.is_review) td.classList.add("unrel");
        if(r.is_review){
          td.title="book review — citation count reflects the reviewed book, not the review";
        } else if(r.cpy_diverged){
          const s=r.cpy_sources||{};
          const parts=Object.keys(s).map(k=>k+"="+s[k]).join(", ");
          td.title="citation sources disagreed — OpenAlex over-counts edited-volume "
                   +"chapters; showing the conservative corroborated count ("+parts+")";
        } else if(!r.cpy_reliable){
          td.title="global citation count undercounts books/chapters & very recent work";
        }
      } else if(c.k==="year"){
        td.textContent = r.year===""||r.year==null ? "" : r.year;
        if(r.year_backfilled){ td.classList.add("backfill");
          td.title="year not in the source data — filled in manually"; }
      } else if(c.cls==="depth"){
        const d=r.depths[c.iss]||0;
        td.textContent = d? d : "·";
        td.className="depth d"+d;
      } else if(c.k==="lev"){
        // No-abstract rows have lev=null → render "--" (leverage not scored).
        if(r.no_abstract || r.lev==null){ td.textContent="--"; td.classList.add("unrel");
          td.title="no abstract — leverage not scored; row included on relevance + citedness"; }
        else td.textContent = r.lev;
      } else {
        const v=c.get(r);
        td.textContent = (v===""||v==null)? "" : v;
      }
      tr.appendChild(td);
    });
    body.appendChild(tr);
  }
  // header sort markers
  document.querySelectorAll("#head th").forEach((th,i)=>{
    th.classList.remove("sorted-asc","sorted-desc");
    if(i===sortCol) th.classList.add(sortDesc?"sorted-desc":"sorted-asc");
  });
  document.getElementById("count").textContent =
    rows.length+" shown / "+DATA.rows.length+" total";
  // hint when the query couldn't be parsed and we fell back to substring match
  const qel=document.getElementById("q");
  qel.classList.toggle("qbad", !!qel.value.trim() && !_qOk);
  qel.title = (!!qel.value.trim() && !_qOk)
    ? "couldn't parse as a Boolean/fielded query — matched as plain text instead" : "";
}
function esc(s){ return s.replace(/[&<>"]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[m])); }

["q","showall","staronly"].forEach(id=>
  document.getElementById(id).addEventListener("input",render));

// ---- top issue key (what A1–A6 etc. mean) ----
(function(){
  const coreIs = DATA.issues.filter(i=>CORE.includes(i.id));
  const otherIs= DATA.issues.filter(i=>!CORE.includes(i.id));
  function dl(list){
    return "<dl>"+list.map(i=>{
      const wt = (i.weight!=null && i.weight!==1.0)
        ? ` <span class="qn" title="leverage weight">(×${i.weight})</span>` : "";
      return `<dt>${esc(i.id)}</dt><dd><b>${esc(i.label)}</b>${wt}`+
        (i.question?` <span class="qn">— ${esc(i.question)}</span>`:"")+
        `</dd>`;
    }).join("")+"</dl>";
  }
  let h = `<div class="grp">Core issues (counted toward leverage)</div>`+dl(coreIs);
  if(otherIs.length) h += `<div class="grp">Related issues (shown, not counted)</div>`+dl(otherIs);
  document.getElementById("issuekeybody").innerHTML = h;
})();

// ---- legend ----
const lg=document.getElementById("legend");
lg.innerHTML =
  "<b>Issue columns</b> (depth 0–3): "+
  DATA.issues.map(i=>`<code>${i.id}</code> ${esc(i.label)}`).join(" · ")+
  "<br><b>Depth</b>: <span class='d1'>1</span> mentions · "+
  "<span class='d2'>2</span> substantive · <span class='d3'>3</span> central. "+
  "<b>Cit/yr</b> = global citations ÷ age (greyed = undercounted: book/chapter or recent). "+
  "<b>In-corpus</b> = how many papers <i>in this corpus</i> cite it. "+
  "<b>Lev</b> = Σ depth over A1–A6. "+
  "An <i>italic year</i> was not in the source metadata and was filled in by hand. "+
  "<br><b>Title links</b>: most go to the DOI. Papers with no DOI carry a small tag — "+
  "<span class='lk'>publisher</span> a journal/publisher/institution page, "+
  "<span class='lk lk-oa'>full text</span> open-access PDF, "+
  "<span class='lk'>record</span> a Semantic Scholar entry, "+
  "<span class='lk lk-philpapers_search'>PhilPapers ⌕</span> a PhilPapers <i>search</i> "+
  "(a results page for the title, not a direct link). "+
  "<br>A <span class='lk lk-gbooks'>term cloud</span> tag means no abstract exists and the "+
  "depth scores were derived from a Google-Books frequency-sized keyword cloud (capped at depth 2) — treat them as approximate. "+
  "<br>A <span class='lk lk-chap'>ch. of “…”</span> tag marks a book chapter whose parent "+
  "monograph is also in this table; the parent carries <span class='lk lk-mono'>has chapters here</span>. "+
  "Chapters and monographs stay as separate rows with their own scores, but only the "+
  "highest-leverage node in each chapter/book cluster is eligible for a ★ (no double-starring the same work). "+
  "Click a header to sort; click an issue chip to show only papers central on it.";

render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()

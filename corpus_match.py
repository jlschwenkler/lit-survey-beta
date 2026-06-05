#!/usr/bin/env python3
"""Shared corpus-matching helpers: title normalization, author-surname extraction,
review detection, and the CANONICAL-NODE resolver.

WHY THIS EXISTS (the SEP-sanity-check lesson, 2026-06-01):
The same work lives in citation_graph.json under several keys — its DOI, an
OpenAlex twin, an S2 stub, and (the dangerous one) a Choice/MUSE *review* of it
that carries the work's exact title. A naive "is this work in the corpus?" lookup
returns whichever sibling it hits first, which was often the lowest-scored
fragment or the review. That made central works (Sher's *Who Knew?*, Mele 2010)
look like rel-1 noise when the real, on-thesis node sat right beside them.

`canonical_node()` is the fix: given a title (and optional author surname), it
returns the BEST node for that work — highest crawl-relevance, then abstract-
bearing, then richest work_type, then most-authoritative key — and it pushes
review/contaminant nodes to the bottom so a review can never shadow the work.

Used by sep_gap_check.py (and available to any other corpus-membership lookup).
The normalization/surname/review logic is kept byte-for-byte compatible with
consolidate_nodes.py so the two stages agree on what counts as "the same work".
"""
import re, unicodedata
from difflib import SequenceMatcher

# ── normalization ────────────────────────────────────────────────────────────

def deaccent(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))

def norm_title(t):
    t = re.sub(r"[^a-z0-9 ]", " ", deaccent((t or "").lower()))
    return re.sub(r"\s+", " ", t).strip()

def title_tokens(t, minlen=4):
    return set(w for w in norm_title(t).split() if len(w) >= minlen)

# Leading function words carry no identifying signal and just push the
# distinctive words rightward; drop them before taking the title's "head".
_LEAD_STOP = {"the", "a", "an", "on", "of", "in", "to", "for", "and", "or",
              "is", "are", "some", "what", "how", "why", "does", "do", "as",
              "from", "with", "two", "no", "not", "this", "that"}

def title_seq(t, minlen=3):
    """Ordered content tokens (order preserved, light stoplist). Used for
    PREFIX comparison — titles are front-loaded, so the opening words are the
    most work-identifying part."""
    return [w for w in norm_title(t).split()
            if len(w) >= minlen and w not in _LEAD_STOP]

def head_overlap(seq_a, seq_b, head=3):
    """Fraction of the query's first `head` content tokens that appear ANYWHERE
    in the candidate's first `head`+1 content tokens. Anchored on the opening of
    the title (where the distinctive words live) but tolerant of small word-order
    and subtitle differences. Returns 0..1 over the query head."""
    ha = seq_a[:head]
    hb = set(seq_b[:head + 1])
    if not ha:
        return 0.0
    return sum(1 for w in ha if w in hb) / len(ha)

def surname(node):
    """First author's surname, normalized across the messy API formats.
    'Gardner, John' / 'John B. Gardner' / 'J. Gardner' all -> 'gardner'."""
    a = node.get("authors") or []
    if not a:
        return None
    name = deaccent(a[0])
    part = name.split(",")[0] if "," in name else (name.split()[-1] if name.split() else "")
    s = re.sub(r"[^a-z]", "", part.lower())
    return s or None

def all_surnames(node):
    out = set()
    for a in (node.get("authors") or []):
        a = deaccent(a)
        part = a.split(",")[0] if "," in a else (a.split()[-1] if a.split() else "")
        s = re.sub(r"[^a-z]", "", part.lower())
        if len(s) >= 3:
            out.add(s)
    return out

# ── review / contaminant detection (kept in sync with consolidate_nodes.py) ───

REVIEW_DOI = re.compile(r"10\.5860/choice|10\.1353/")
REVIEW_TEXT = re.compile(
    r"\bthis (?:book )?review\b|\bbook review\b|\breviewed by\b|"
    r"\bis a review of\b|\breview essay\b|\bin this review\b", re.I)

def is_review(node):
    if node.get("is_review") is True:
        return True
    if node.get("doi") and REVIEW_DOI.search(node.get("doi") or ""):
        return True
    if REVIEW_TEXT.search((node.get("abstract") or "")[:400]):
        return True
    return False

# ── canonical-node resolution ─────────────────────────────────────────────────

def key_rank(k):
    if k.startswith("doi:"):  return 3
    if k.startswith("oa:"):   return 2
    if k.startswith("s2:"):   return 1
    return 0  # title:

WT_RANK = {"book": 3, "book-chapter": 2, "dissertation": 2, "article": 1}

def _quality(k, node):
    """Tie-breaker AFTER relevance: prefer a real work over a review, then an
    abstract-bearing node, then richer work_type, then authoritative key, then
    the longest abstract."""
    return (
        0 if is_review(node) else 1,
        1 if node.get("abstract") else 0,
        WT_RANK.get(node.get("work_type"), 0),
        key_rank(k),
        len(node.get("abstract") or ""),
    )

def find_candidates(title, N, author_surname=None,
                    accept_head=0.67, accept_sim=0.80,
                    accept_with_author=0.5):
    """Return [(key, node, sim, au_ok, head, conf)] for every node whose title
    plausibly matches.

    PREFIX-LED matching. Titles are front-loaded: the distinctive, work-naming
    words come first, while the tail is a generic subtitle ('A Theory of Moral
    Responsibility', 'An Inquiry') whose words recur across the whole subfield
    and generate false matches. So the PRIMARY signal is `head` = overlap of the
    query's opening content tokens with the candidate's opening tokens
    (`head_overlap`). Full-title bag-of-words similarity (`sim`) is the FALLBACK
    for the minority of titles whose distinctive term isn't first ('A Theory of
    Negligence').

    A node qualifies if:
      • head overlap >= `accept_head` (the openings align), OR
      • full-title sim >= `accept_sim` (strong whole-title match), OR
      • author agrees AND sim >= `accept_with_author` (author + moderate title).

    `conf` (0..1) is a confidence the CALLER can threshold to split clean matches
    from a small hand-sort bucket: it rewards head overlap, author agreement, and
    whole-title sim. Author DISagreement on a non-exact title is penalized,
    because the dangerous false matches (a review or a same-subfield paper) almost
    always have the wrong first author."""
    nt = norm_title(title)
    tt = title_tokens(title)
    qseq = title_seq(title)
    if not tt:
        return []
    asn = {re.sub(r"[^a-z]", "", (author_surname or "").lower())} - {""}
    out = []
    for k, n in N.items():
        ct = n.get("title")
        if not ct:
            continue
        ctt = title_tokens(ct)
        jac = len(tt & ctt) / len(tt | ctt) if (tt | ctt) else 0
        sr = SequenceMatcher(None, nt, norm_title(ct)).ratio()
        sim = max(jac, sr)
        head = max(head_overlap(qseq, title_seq(ct)),
                   head_overlap(title_seq(ct), qseq))
        au_ok = bool(asn & all_surnames(n)) if asn else False
        qualifies = (
            head >= accept_head
            or sim >= accept_sim
            or (au_ok and sim >= accept_with_author)
        )
        if not qualifies:
            continue
        # confidence: weighted blend, author agreement bonus, disagreement penalty
        conf = 0.55 * head + 0.45 * sim
        if asn:
            conf += 0.15 if au_ok else -0.25
        conf = max(0.0, min(1.0, conf))
        out.append((k, n, sim, au_ok, head, conf))
    return out

# Below this, canonical_node treats a match as low-confidence (needs human eyes)
CONF_CLEAN = 0.55

def canonical_node(title, N, scores, author_surname=None, **kw):
    """Resolve a work title to its BEST node in the corpus.

    Returns (key, node, sim, relevance, conf) for the node that best represents
    the work, or None if nothing qualifies. Ranked by (author agreement, then
    crawl-relevance, then quality tie-breakers that demote reviews and reward
    abstracts, then prefix-head overlap, then full sim).

    `conf` lets the caller separate confident matches (>= CONF_CLEAN) from a
    small low-confidence residue worth hand-sorting — the deliberate design:
    automate the easy 90%, surface the ambiguous few rather than guess.

    This is the function every "is X in the corpus / how relevant is X" lookup
    should call, so a low-scored fragment or a review twin can never be reported
    as if it were the work itself."""
    cands = find_candidates(title, N, author_surname, **kw)
    if not cands:
        return None
    def rank(c):
        k, n, sim, au_ok, head, conf = c
        rel = (scores.get(k) or {}).get("score", 0)
        return (1 if au_ok else 0, rel) + _quality(k, n) + (head, sim)
    k, n, sim, au_ok, head, conf = max(cands, key=rank)
    rel = (scores.get(k) or {}).get("score", 0)
    return (k, n, sim, rel, conf)

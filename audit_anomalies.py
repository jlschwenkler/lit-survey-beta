#!/usr/bin/env python3
"""
Stage 6.8 — Duplicate / anomaly audit  (report-first, human-gated, with a narrow
auto-merge lane for false-positive-impossible cases).

WHY THIS EXISTS (and why it is NOT Stage 3.5):
  Stage 3.5 `consolidate_nodes.py` is the EARLY, STRICT, AUTOMATIC merge pass
  (exact normalized-title + surname + year; runs before scoring so each work is
  scored once; deliberately conservative — a false merge corrupts the graph).
  This stage is a DIFFERENT operation. It runs LATE, on the fully ENRICHED corpus,
  because most of its signals don't exist until citedness / recovered-DOIs /
  backfilled-abstracts are in place. Several anomalies it surfaces aren't merges
  at all (a review-DOI mislabeled as the reviewed book — needs a node split + edge
  repoint; a node carrying the wrong work's abstract — needs a relabel). So it
  cannot be folded into 3.5, and 3.5 must NOT be loosened to try (that just adds
  false merges).

CONTRACT (decided 2026-06-03):
  • REPORT-ONLY by default. Detects clusters, classifies, writes a report.
  • AUTO lane: with --commit, it auto-MERGES only HIGH-CONFIDENCE clusters where a
    false positive is effectively impossible:
        (A) two+ nodes sharing the SAME DOI (after normalization), or
        (B) an exact normalized-title + same-surname + same-year pair that Stage
            3.5 missed ONLY because of a key-format quirk (i.e. both are real DOI
            nodes pointing at the same DOI, or one is a bare title:/s2: twin of a
            DOI node with identical title+surname+year and an EMPTY abstract).
    Everything fuzzy or non-merge (near-dup titles, splits, relabels) is GATED:
    described in the report with a recommended resolution, never auto-applied.
  • Reuses `consolidate_nodes`' merge mechanics (canonical pick, metadata union,
    edge repoint+dedup, score fold, matrix-row prune) so an auto-merge here is
    identical to a 3.5 merge.
  • Snapshots graph+matrix before any write. After a commit, re-run the enricher
    cascade + build_lit_table.

DETECTION SIGNALS (each → a cluster in the report):
  S1  review node with high IN-degree + 0 OUT-edges
        → a book's citations mis-attached to its review (McCann/Bratman).
          Resolution: usually a SPLIT (create the real book node, repoint the
          in-edges). GATED — needs the correct book DOI/abstract from a human.
  S2  two rows, same first-author surname + year, with prefix / near-duplicate
        titles (one title is a word-boundary prefix of the other, or high ratio)
        → unconsolidated twin 3.5's exact-match missed (Schwenkler "…" vs "…: A
          Guide"). GATED unless it qualifies for the auto lane (B).
  S3  a no-abstract / title-only row whose ONLY link is a PhilPapers SEARCH page
        AND there exists a DOI-bearing node with the same surname+near-title
        → an unconsolidated twin. GATED (or auto if exact-title+empty-abstract).
  S4  an abstract whose text names a DIFFERENT title than the node
        → wrong-abstract paste (Wiseman). GATED — needs human relabel/clear.
  S5  a book-title node on a JSTOR 10.2307/ DOI
        → often a REVIEW of that book, not the book. GATED — verify + retag.
  AUTO  exact-DOI duplicates / empty-twin-of-DOI-node  → auto-merge on --commit.

USAGE:
  python3 audit_anomalies.py            # dry run: report only
  python3 audit_anomalies.py --commit   # apply AUTO merges, write report for gated
  Then (only if auto-merges happened): re-run the 5 enrichers + enrich_links + build_lit_table.
"""
import json, os, re, sys, time, difflib
from collections import defaultdict

# reuse the proven primitives from Stage 3.5
import consolidate_nodes as C

# Some `consolidate_nodes.py` variants (e.g. the negligence project's) don't define
# the OUP/CUP chapter↔monograph guard. It's a refinement, not essential to
# correctness, so degrade gracefully when it's absent.
def _chapter_monograph_conflict(keys, N):
    fn = getattr(C, "chapter_monograph_conflict", None)
    return fn(keys, N) if fn else False

GRAPH  = "citation_graph.json"
MATRIX = "engagement_matrix.json"
LINKS  = "links.json"
REPORT = "audit_anomalies_report.md"
LOG    = "audit_anomalies_log.json"
ARCHIVE = "_archive"

# ── helpers ───────────────────────────────────────────────────────────────────

def norm_doi(d):
    """Normalize a DOI string for equality (lowercase, strip the doi: key prefix
    and any resolver host)."""
    if not d:
        return None
    d = d.lower().strip()
    d = re.sub(r"^doi:", "", d)
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    return d or None

def node_doi(n):
    return norm_doi(n.get("doi"))

def in_out_degree(edges):
    indeg = defaultdict(int); outdeg = defaultdict(int)
    for e in edges:
        outdeg[e["from"]] += 1
        indeg[e["to"]] += 1
    return indeg, outdeg

def title_prefix_or_close(a, b):
    """True if normalized title a is a word-boundary prefix of b (or vice versa),
    or they are very close (ratio >= 0.86). Used for near-dup detection."""
    na, nb = C.norm_title(a), C.norm_title(b)
    if not na or not nb or na == nb:
        return na == nb and bool(na)
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    if long.startswith(short + " "):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.86

# title words that appear inside an abstract's opening — used by S4 to check the
# abstract is "about" the node's own title. We compare significant title tokens.
_STOP = set("the a an of on in to and or for is its with without by as at from "
            "an essay selected essays edition vol volume part introduction".split())

def sig_tokens(title):
    toks = [w for w in re.sub(r"[^a-z0-9 ]", " ", (title or "").lower()).split()
            if w not in _STOP and len(w) > 2]
    return set(toks)

JSTOR_RE = re.compile(r"10\.2307/")

# ── detection ───────────────────────────────────────────────────────────────

def matrix_scope(N, scores):
    return {k for k, n in N.items()
            if (scores.get(k) or {}).get("score", 0) >= 4 and (n.get("title") or "").strip()}

def detect(N, scores, edges, links, mrows):
    indeg, outdeg = in_out_degree(edges)
    scope = matrix_scope(N, scores)
    mrow = {r["key"]: r for r in mrows}

    auto, gated = [], []

    # ── AUTO + S2/S3: surname+year groupings for near-dup / exact-dup ──────────
    by_sy = defaultdict(list)   # (surname, year) -> [keys]
    for k in scope:
        n = N[k]
        s = C.surname(n); y = n.get("year")
        by_sy[(s, y)].append(k)

    seen_pairs = set()
    for (s, y), ks in by_sy.items():
        if len(ks) < 2 or s is None:
            continue
        for i in range(len(ks)):
            for j in range(i + 1, len(ks)):
                a, b = ks[i], ks[j]
                pair = tuple(sorted((a, b)))
                if pair in seen_pairs:
                    continue
                na, nb = N[a], N[b]
                ta, tb = na.get("title"), nb.get("title")
                if not title_prefix_or_close(ta, tb):
                    continue
                seen_pairs.add(pair)
                # don't fold a real review into a work
                if C.is_review(na) or C.is_review(nb):
                    continue
                # chapter/monograph guard (reuse 3.5's)
                if _chapter_monograph_conflict([a, b], N):
                    continue
                exact = C.norm_title(ta) == C.norm_title(tb)
                da, db = node_doi(na), node_doi(nb)
                empty_a = not (na.get("abstract") or "").strip()
                empty_b = not (nb.get("abstract") or "").strip()
                # AUTO lane (B): exact title + same surname/year AND
                #   (same DOI)  OR  (one side is an empty twin of the other)
                if exact and (
                    (da and db and da == db)
                    or (da and not db and empty_b)
                    or (db and not da and empty_a)
                    or (da == db and (empty_a or empty_b))
                ):
                    auto.append({"signal": "AUTO-exact-twin", "keys": [a, b],
                                 "why": "exact title + same surname+year; "
                                        "shared/empty-twin DOI — 3.5 missed on key-format only"})
                else:
                    gated.append({"signal": "S2-neardup", "keys": [a, b],
                                  "why": f"same surname '{s}' + year {y}; "
                                         f"{'exact' if exact else 'near-dup'} titles "
                                         f"({ta!r} vs {tb!r})",
                                  "rec": "merge if same work (pick DOI/abstract-bearing survivor); "
                                         "keep separate if genuinely distinct (e.g. book vs. its guide)"})

    # ── AUTO: same-DOI duplicates regardless of title ─────────────────────────
    by_doi = defaultdict(list)
    for k in scope:
        d = node_doi(N[k])
        if d:
            by_doi[d].append(k)
    for d, ks in by_doi.items():
        if len(ks) < 2:
            continue
        if any(C.is_review(N[k]) for k in ks):
            gated.append({"signal": "S-dupdoi-review", "keys": ks,
                          "why": f"multiple nodes share DOI {d} but one is flagged review",
                          "rec": "verify which is the work vs. the review before merging"})
            continue
        auto.append({"signal": "AUTO-same-doi", "keys": ks,
                     "why": f"{len(ks)} nodes share the same DOI {d}"})

    # ── S1: review node with IN-degree but 0 OUT-edges ────────────────────────
    for k in scope:
        n = N[k]
        if not C.is_review(n):
            continue
        if indeg.get(k, 0) >= 3 and outdeg.get(k, 0) == 0:
            gated.append({"signal": "S1-review-cites", "keys": [k],
                          "why": f"review node has {indeg[k]} in-citations but 0 outgoing refs "
                                 f"— citations likely belong to the reviewed WORK, not the review",
                          "rec": "create/locate the real work node (correct DOI + abstract from a "
                                 "human), repoint these in-edges to it; leave the review at 0 citers"})

    # ── S4: abstract is a foreign SYNOPSIS pasted onto the node ─────────────────
    # CALIBRATION NOTE: an earlier version fired on "node title shares no
    # significant word with its abstract" — that's 100% false-positive in
    # practice (a normal abstract describes CONTENT, not the title: Davidson's
    # "Essays on actions and events", "Actions, Reasons, and Causes", etc. all
    # tripped it). The real wrong-paste case (Wiseman) had a POSITIVE tell: the
    # abstract opened "Book synopsis: ...". So S4 now requires an explicit
    # foreign-synopsis / cross-reference marker in the abstract head AND zero
    # title-word overlap — both, so a work that merely opens "Book synopsis:"
    # about ITSELF (title words present) is not flagged.
    SYNOPSIS_MARK = re.compile(
        r"\bbook synopsis\b|\bsynopsis:\b|\bpublisher'?s? (?:description|blurb)\b|"
        r"\bfrom the (?:back )?cover\b|\babout this book\b", re.I)
    for k in scope:
        n = N[k]
        ab = (n.get("abstract") or "").strip()
        if len(ab) < 80:
            continue
        head = ab[:400]
        if not SYNOPSIS_MARK.search(head):
            continue
        ttoks = sig_tokens(n.get("title"))
        if not ttoks:
            continue
        present = sum(1 for t in ttoks if t in head.lower())
        # synopsis marker present AND none of the node's title words appear:
        # the synopsis is about a DIFFERENT work pasted onto this node.
        if len(ttoks) >= 2 and present == 0:
            gated.append({"signal": "S4-wrong-abstract", "keys": [k],
                          "why": f"abstract opens with a foreign synopsis marker but shares no "
                                 f"significant word with node title {n.get('title')!r} — likely "
                                 f"a synopsis of a DIFFERENT work pasted here",
                          "rec": "verify the abstract belongs to THIS work; if not, clear it (and "
                                 "tag/relabel the node correctly)"})

    # ── S5: book-title node on a JSTOR 10.2307/ DOI (often a review of the book) ─
    for k in scope:
        n = N[k]
        if C.is_review(n):
            continue   # already handled / known
        d = node_doi(n)
        if not d or not JSTOR_RE.match(d):
            continue
        wt = n.get("work_type")
        if wt in ("book", "book-chapter"):
            gated.append({"signal": "S5-jstor-book", "keys": [k],
                          "why": f"book-type node on a JSTOR 10.2307/ DOI ({d}) — JSTOR 10.2307 items "
                                 f"are frequently REVIEWS of the named book, not the book itself",
                          "rec": "verify: if it's a review, retag is_review + clear the book blurb; "
                                 "if cites are mis-attached, apply the S1 split"})

    return auto, gated, indeg, outdeg

# ── reporting ─────────────────────────────────────────────────────────────────

def fmt_node(N, k, indeg, outdeg, mrow):
    n = N.get(k, {})
    r = mrow.get(k, {})
    return (f"`{k}`  · {n.get('title','')[:60]!r}\n"
            f"      authors={n.get('authors')} year={n.get('year')} wt={n.get('work_type')} "
            f"review={n.get('is_review')} abs={'Y' if (n.get('abstract') or '').strip() else 'n'} "
            f"in={indeg.get(k,0)} out={outdeg.get(k,0)} "
            f"text_source={r.get('text_source')}")

def write_report(auto, gated, N, indeg, outdeg, mrow, committed):
    lines = ["# Stage 6.8 — Duplicate / anomaly audit", ""]
    lines.append(f"_generated {time.strftime('%Y-%m-%d %H:%M:%S')}_  ")
    lines.append(f"_{len(auto)} auto-merge cluster(s), {len(gated)} gated cluster(s) for hand review._")
    lines.append("")
    lines.append("AUTO clusters are false-positive-impossible (shared DOI / exact-title empty-twin) "
                 "and are merged on `--commit`. GATED clusters require your judgment — each lists a "
                 "recommended resolution; apply by hand (merge, split, or relabel).")
    lines.append("")

    lines.append("## AUTO-merge clusters" + ("  ✅ APPLIED" if committed else "  (dry run — not yet applied)"))
    if not auto:
        lines.append("\n_none_\n")
    for c in auto:
        lines.append(f"\n### {c['signal']} — {c['why']}")
        for k in c["keys"]:
            lines.append("- " + fmt_node(N, k, indeg, outdeg, mrow))
    lines.append("")

    lines.append("## GATED clusters — need hand review")
    if not gated:
        lines.append("\n_none_\n")
    by_sig = defaultdict(list)
    for c in gated:
        by_sig[c["signal"]].append(c)
    for sig in sorted(by_sig):
        lines.append(f"\n### {sig}  ({len(by_sig[sig])})")
        for c in by_sig[sig]:
            lines.append(f"\n**why:** {c['why']}  ")
            lines.append(f"**recommended:** {c.get('rec','(see signal docs)')}  ")
            for k in c["keys"]:
                lines.append("- " + fmt_node(N, k, indeg, outdeg, mrow))
    lines.append("")
    open(REPORT, "w").write("\n".join(lines))

# ── auto-merge application (reuses consolidate_nodes mechanics) ────────────────

def apply_auto_merges(g, auto):
    N, scores, edges = g["nodes"], g["scores"], g["edges"]
    log = {"merges": []}
    alias_to_canon = {}
    survivors = []
    for c in auto:
        ks = c["keys"]
        canon = C.canonical(ks, N)
        aliases = [k for k in ks if k != canon]
        if not aliases:
            continue
        survivors.append(canon)
        for a in aliases:
            alias_to_canon[a] = canon
        C.merge_group(N, scores, None, canon, aliases)
        log["merges"].append({"signal": c["signal"], "canonical": canon,
                              "aliases": aliases, "why": c["why"]})

    # rewrite edges (repoint, drop self-loops, dedup) — identical to 3.5
    seen = set(); new_edges = []
    for ed in edges:
        fr = alias_to_canon.get(ed["from"], ed["from"])
        to = alias_to_canon.get(ed["to"], ed["to"])
        if fr == to:
            continue
        sig = (fr, to)
        if sig in seen:
            continue
        seen.add(sig)
        new_edges.append({"from": fr, "to": to, "hop": ed.get("hop")})
    dropped = len(edges) - len(new_edges)
    g["edges"] = new_edges

    # fold scores: canonical keeps max relevance
    for e in log["merges"]:
        canon = e["canonical"]
        best = scores.get(canon, {}).get("score", 0)
        reason = scores.get(canon, {}).get("reason", "")
        for a in e["aliases"]:
            s = scores.get(a, {})
            if s.get("score", 0) > best:
                best, reason = s["score"], s.get("reason", "")
            scores.pop(a, None)
        scores[canon] = {"score": best, "reason": reason}

    for a in alias_to_canon:
        N.pop(a, None)

    return log, survivors, dropped, new_edges, edges

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    commit = "--commit" in sys.argv
    g = json.load(open(GRAPH))
    N, scores, edges = g["nodes"], g["scores"], g["edges"]
    links = json.load(open(LINKS)) if os.path.exists(LINKS) else {}
    m = json.load(open(MATRIX)) if os.path.exists(MATRIX) else {"rows": []}
    mrows = m["rows"]
    mrow = {r["key"]: r for r in mrows}

    auto, gated, indeg, outdeg = detect(N, scores, edges, links, mrows)

    print(f"Anomaly audit: {len(auto)} AUTO-merge cluster(s), {len(gated)} GATED cluster(s).")
    for c in auto:
        print(f"  AUTO  {c['signal']}: {c['keys']}")
    bysig = defaultdict(int)
    for c in gated:
        bysig[c["signal"]] += 1
    for s in sorted(bysig):
        print(f"  GATED {s}: {bysig[s]}")

    if not commit:
        write_report(auto, gated, N, indeg, outdeg, mrow, committed=False)
        print(f"\nDRY RUN — report written to {REPORT}. No graph/matrix changes.")
        print("Re-run with --commit to APPLY the AUTO merges (gated clusters are never auto-applied).")
        return

    if not auto:
        write_report(auto, gated, N, indeg, outdeg, mrow, committed=False)
        print(f"\nNo AUTO merges to apply. Report (gated clusters) written to {REPORT}.")
        return

    # snapshot before any mutation
    os.makedirs(ARCHIVE, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json.dump(g, open(os.path.join(ARCHIVE, f"citation_graph.backup_pre_audit_{stamp}.json"), "w"),
              ensure_ascii=False, indent=2)
    json.dump(m, open(os.path.join(ARCHIVE, f"engagement_matrix.backup_pre_audit_{stamp}.json"), "w"),
              ensure_ascii=False, indent=2)

    log, survivors, dropped, new_edges, old_edges = apply_auto_merges(g, auto)
    json.dump(g, open(GRAPH, "w"), ensure_ascii=False, indent=2)

    # prune orphaned alias rows from matrix
    before = len(mrows)
    m["rows"] = [r for r in mrows if r["key"] in g["nodes"]]
    json.dump(m, open(MATRIX, "w"), ensure_ascii=False, indent=2)

    json.dump(log, open(LOG, "w"), ensure_ascii=False, indent=2)
    open("audit_changed_keys.txt", "w").write("\n".join(sorted(set(survivors))) + "\n")
    # rebuild degree maps post-merge for an accurate report
    indeg, outdeg = in_out_degree(g["edges"])
    write_report([], gated, g["nodes"], indeg, outdeg, mrow, committed=True)

    print(f"\nsnapshot stamp {stamp}")
    print(f"Applied {len(log['merges'])} AUTO merge(s); removed alias nodes; "
          f"edges {len(old_edges)}->{len(new_edges)} (dropped {dropped}).")
    print(f"matrix rows {before}->{len(m['rows'])}.")
    print(f"log -> {LOG} · survivors -> audit_changed_keys.txt · report (gated) -> {REPORT}")
    print("\nNEXT (only because nodes changed): re-run the 5 enrichers + enrich_links.py, "
          "then build_lit_table.py. (AUTO merges fold metadata only; no re-score needed unless "
          "a survivor's text changed — none do here.)")

if __name__ == "__main__":
    main()

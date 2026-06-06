"""
ingest_handpull.py — read the filled-in handpull_fill.csv and write the abstracts
the user hand-pulled back into citation_graph.json, so the engagement scorer can
re-score those works on their real text instead of title-only.

This closes the hand-pull round-trip:

    1. build_lit_table.py / triage          → ranked worklist
    2. make_handpull_csv.py                  → handpull_fill.csv  (empty `abstract` column)
    3. <user pastes real abstracts into the CSV's `abstract` column>
    4. ingest_handpull.py                    → writes abstracts into the graph  ← THIS SCRIPT
    5. ingest_handpull.py --prune-matrix     → drops the changed rows from the matrix
    6. score_engagement.py --keys-file handpull_ingest_keys.txt   → re-scores them
    7. build_lit_table.py                    → rebuilt report

NO fabrication. Every abstract written comes verbatim from a cell the user typed.
Rows with a blank `abstract` are skipped. An abstract is written onto EVERY key in
that row's `sibling_node_keys` (a fragmented work gets the text on every fragment),
falling back to a `doi:`/key match if `sibling_node_keys` is empty.

For each touched node it sets:
  abstract           = the pasted text (whitespace-squeezed)
  abstract_source    = "handpull" (or --source VALUE to override, e.g. "publisher_fetch")
  abstract_ingested  = "handpull_<YYYYMMDD>"

A timestamped snapshot of citation_graph.json is written before any change, and the
changed keys are written to handpull_ingest_keys.txt for score_engagement.py
--keys-file. This mirrors backfill_abstracts.py exactly (same fields, same
prune-then-rescore dance) — it just takes the text from a CSV the user filled by
hand rather than from an API.

Usage:
  python3 ingest_handpull.py                    # ingest filled handpull_fill.csv
  python3 ingest_handpull.py --csv other.csv    # a different filled CSV
  python3 ingest_handpull.py --source publisher_fetch
  python3 ingest_handpull.py --dry-run          # report what WOULD change, write nothing
  python3 ingest_handpull.py --prune-matrix     # drop the changed rows from the matrix
"""
import argparse, csv, json, os, re, shutil, datetime

FOLDER      = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH  = os.path.join(FOLDER, "citation_graph.json")
MATRIX_PATH = os.path.join(FOLDER, "engagement_matrix.json")
CSV_PATH    = os.path.join(FOLDER, "handpull_fill.csv")
KEYS_PATH   = os.path.join(FOLDER, "handpull_ingest_keys.txt")
ARCHIVE     = os.path.join(FOLDER, "_archive")   # snapshots live here (gitignored)

MIN_ABS_CHARS = 60   # shorter than this is treated as a stub/note, not an abstract


def squeeze(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def norm_doi(d):
    if not d:
        return None
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(d).strip(), flags=re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=CSV_PATH,
                    help="filled CSV to ingest (default: handpull_fill.csv)")
    ap.add_argument("--source", default="handpull",
                    help="value written to abstract_source (default: handpull)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--prune-matrix", action="store_true",
                    help="drop the ingested keys' rows from engagement_matrix.json so "
                         "score_engagement.py re-scores them (uses the keys file from a "
                         "prior run; does NOT read the CSV)")
    args = ap.parse_args()

    graph = json.load(open(GRAPH_PATH))
    nodes = graph["nodes"]
    mat = json.load(open(MATRIX_PATH)) if os.path.exists(MATRIX_PATH) else None

    # ── prune mode: remove changed rows from the matrix, then exit ──
    if args.prune_matrix:
        if mat is None:
            print(f"No {os.path.basename(MATRIX_PATH)} yet — prune mode needs the "
                  f"matrix (run score_engagement.py first).")
            return
        if not os.path.exists(KEYS_PATH):
            print(f"No {os.path.basename(KEYS_PATH)} — run the ingest first.")
            return
        changed = {ln.strip() for ln in open(KEYS_PATH) if ln.strip()}
        before = len(mat["rows"])
        mat["rows"] = [r for r in mat["rows"] if r["key"] not in changed]
        json.dump(mat, open(MATRIX_PATH, "w"), indent=2, ensure_ascii=False)
        print(f"Pruned {before - len(mat['rows'])} rows ({len(changed)} keys) from "
              f"the matrix. Now re-score:")
        print("  python3 score_engagement.py "
              "--keys-file handpull_ingest_keys.txt --upgrade")
        return

    if not os.path.exists(args.csv):
        print(f"No CSV at {args.csv}. Run make_handpull_csv.py first, then fill the "
              f"`abstract` column.")
        return

    # A DOI -> key index so a row with no sibling_node_keys can still be matched.
    doi_to_key = {}
    for k, n in nodes.items():
        d = (norm_doi(n.get("doi")) or "").lower()
        if d:
            doi_to_key.setdefault(d, k)

    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    tag = "handpull_" + datetime.date.today().strftime("%Y%m%d")
    changed_keys, filled_rows, blank_rows, unmatched = [], 0, 0, []

    for r in rows:
        abs_text = squeeze(r.get("abstract"))
        if not abs_text:
            blank_rows += 1
            continue
        if len(abs_text) < MIN_ABS_CHARS:
            print(f"  ⚠ skipping short entry (<{MIN_ABS_CHARS} chars): "
                  f"{(r.get('title') or '')[:60]}")
            continue
        filled_rows += 1

        # Resolve target keys: prefer sibling_node_keys (|-joined), else DOI, else
        # any bare key columns the CSV happens to carry.
        keys = [k for k in (r.get("sibling_node_keys") or "").split("|") if k.strip()]
        if not keys:
            d = (norm_doi(r.get("doi")) or "").lower()
            if d and d in doi_to_key:
                keys = [doi_to_key[d]]
        keys = [k for k in keys if k in nodes]
        if not keys:
            unmatched.append(r.get("title") or r.get("doi") or "?")
            continue

        for k in keys:
            n = nodes[k]
            n["abstract"] = abs_text
            n["abstract_source"] = args.source
            n["abstract_ingested"] = tag
            changed_keys.append(k)

    changed_keys = sorted(set(changed_keys))

    print("=" * 56)
    print(f"CSV rows           : {len(rows)}")
    print(f"  with abstract    : {filled_rows}")
    print(f"  blank (skipped)  : {blank_rows}")
    print(f"nodes to update    : {len(changed_keys)}")
    if unmatched:
        print(f"  ⚠ unmatched rows : {len(unmatched)} (no key/DOI found in graph)")
        for t in unmatched[:10]:
            print(f"      - {str(t)[:64]}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return
    if not changed_keys:
        print("\nNothing to ingest (no filled rows matched a graph node).")
        return

    # snapshot into _archive/ (gitignored — never shipped), then write
    os.makedirs(ARCHIVE, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    snap = os.path.join(ARCHIVE, f"citation_graph.backup_pre_handpull_{stamp}.json")
    shutil.copy2(GRAPH_PATH, snap)
    json.dump(graph, open(GRAPH_PATH, "w"), ensure_ascii=False)
    with open(KEYS_PATH, "w") as f:
        f.write("\n".join(changed_keys) + "\n")

    print(f"\nsnapshot  : _archive/{os.path.basename(snap)}")
    print(f"graph     : wrote abstract + abstract_source='{args.source}' "
          f"+ abstract_ingested='{tag}' onto {len(changed_keys)} node(s)")
    print(f"keys file : {KEYS_PATH}")
    print("\nNext — prune the changed rows, then re-score + rebuild:")
    print("  python3 ingest_handpull.py --prune-matrix")
    print("  python3 score_engagement.py --keys-file handpull_ingest_keys.txt --upgrade")
    print("  python3 build_lit_table.py")


if __name__ == "__main__":
    main()

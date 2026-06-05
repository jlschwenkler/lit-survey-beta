"""
expand_wave3.py
One-shot expansion of the wave-3 manually-bumped seeds (Arpaly, Wolf,
Watson, Scanlon, Rosen/Kleinbart, Levy). These are already in the graph
but were never used as expansion seeds because they existed at the time
of the wave-2 --resume run.

Fetches neighbors for each, scores them, merges into citation_graph.json,
then regenerates literature_candidates.md.
"""
import ssl
import os as _os  # TLS verification on by default; opt out with INSECURE_TLS=1
if _os.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    ssl._create_default_https_context = ssl._create_unverified_context

import json, os, re, time
import requests, urllib3

import os as _os2
if _os2.environ.get("INSECURE_TLS", "") in ("1", "true", "True"):
    urllib3.disable_warnings()
FOLDER     = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH = os.path.join(FOLDER, "citation_graph.json")
REPORT_PATH= os.path.join(FOLDER, "literature_candidates.md")

# Import helpers from main crawler
import importlib.util
spec = importlib.util.spec_from_file_location(
    "crawl",
    os.path.join(FOLDER, "crawl_citation_graph.py")
)
crawl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crawl)

# Wave-3 seed node IDs — already in graph, just need expanding
WAVE3_KEYS = [
    ("doi:10.1093/0195152042.001.0001",                      "Arpaly - Unprincipled Virtue"),
    ("doi:10.1093/acprof:oso/9780199348169.001.0001",        "Arpaly - In Praise of Desire"),
    ("doi:10.2307/3655647",                                   "Arpaly - Moral Worth"),
    ("doi:10.1017/cbo9780511625411.003",                     "Wolf - Sanity and the Metaphysics"),
    ("doi:10.1017/cbo9780511614194.012",                     "Wolf - Freedom Within Reason"),
    ("doi:10.1093/acprof:oso/9780199272273.001.0001",        "Watson - Agency and Answerability"),
    ("doi:10.5840/philtopics199624222",                       "Watson - Two Faces of Responsibility"),
    ("doi:10.2307/j.ctv134vmrn",                              "Scanlon - What We Owe to Each Other"),
    ("doi:10.5840/jphil20081051023",                          "Rosen - Kleinbart the Oblivious"),
    ("doi:10.1093/acprof:oso/9780199601387.001.0001",        "Levy - Hard Luck"),
]

THRESHOLD = 3

with open(GRAPH_PATH) as f:
    g = json.load(f)
nodes  = g["nodes"]
scores = g["scores"]
edges  = g["edges"]

new_total  = 0
pass_total = 0

for key, label in WAVE3_KEYS:
    if key not in nodes:
        print(f"SKIP (not in graph): {label}")
        continue
    rec = nodes[key]
    print(f"\n{'='*60}\n{label}")

    candidates = crawl.fetch_neighbors(rec, direction="both")
    print(f"  -> {len(candidates)} neighbors fetched")

    new_this  = 0
    pass_this = 0
    for cand in candidates:
        ckey = crawl.node_key(cand)
        edges.append({"from": key, "to": ckey, "hop": 3})
        if ckey in nodes:
            continue
        passes, score, reason = crawl.score_candidate(cand, THRESHOLD, scores)
        cand["hop"] = 3
        nodes[ckey] = cand
        scores[ckey] = {"score": score, "reason": reason}
        new_this += 1
        if passes:
            pass_this += 1
            print(f"  [score={score}] {cand.get('title','')[:65]}")

    print(f"  {new_this} new nodes, {pass_this} above threshold")
    new_total  += new_this
    pass_total += pass_this
    time.sleep(0.3)

print(f"\n{'='*60}")
print(f"Wave-3 expansion: {new_total} new nodes, {pass_total} above threshold")

with open(GRAPH_PATH, "w") as f:
    json.dump(g, f, indent=2, ensure_ascii=False)
print("Graph saved.")

# Regenerate report
score_counts = {}
for sc_data in scores.values():
    sc = sc_data.get("score", 1)
    score_counts[sc] = score_counts.get(sc, 0) + 1

candidates = []
for nid, node in nodes.items():
    sc = (scores.get(nid) or {}).get("score", 0)
    if sc >= THRESHOLD:
        candidates.append((sc, node.get("citations") or 0, node, nid))
candidates.sort(key=lambda x: (-x[0], -x[1]))

lines = [
    "# Literature Candidates — Negligence / Responsibility Corpus\n",
    f"*Graph: {len(nodes)} nodes, {len(edges)} edges*\n",
    f"*Score distribution: " +
    ", ".join(f"{k}={v}" for k, v in sorted(score_counts.items(), reverse=True)) + "*\n",
    "\n---\n",
    f"## Score 5 — Central ({score_counts.get(5,0)} papers)\n",
]

current_score = 5
for sc, cit, node, nid in candidates:
    if sc < current_score:
        current_score = sc
        label_str = {4:"Highly Relevant", 3:"Relevant"}.get(sc, str(sc))
        lines.append(f"\n## Score {sc} -- {label_str} ({score_counts.get(sc,0)} papers)\n")
    authors = ", ".join((node.get("authors") or [])[:3])
    year    = node.get("year") or ""
    venue   = node.get("venue") or ""
    title   = node.get("title") or ""
    reason  = (scores.get(nid) or {}).get("reason","")
    lines.append(f"- **{title}**")
    if authors or year:
        lines.append(f"  {authors} ({year})")
    if venue:
        lines.append(f"  *{venue}*")
    if cit:
        lines.append(f"  Citations: {cit}")
    if reason:
        lines.append(f"  > {reason}")
    lines.append("")

with open(REPORT_PATH, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"Report written to: {REPORT_PATH}")
print(f"\nTop 15 candidates:")
for sc, cit, node, nid in candidates[:15]:
    print(f"  [{sc}] {cit:4d} cit  {(node.get('title') or '')[:60]}")

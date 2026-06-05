"""
discover_issues.py  —  Bottom-up ISSUE DISCOVERY pass (step 1 of the issue x paper
engagement matrix).

NOT a topic model / partition. The goal here is only to let the corpus PROPOSE
candidate *issues* (phrased as questions) so the human-curated issue list is
complete. Papers are NOT permanently assigned to clusters; clustering is used
purely as a discovery aid. Final structure is an issue x paper engagement matrix
built later, where a paper may load on many issues.

Pipeline:
  1. Pull score>=THRESHOLD nodes from citation_graph.json (relevant tier).
     Use title + abstract; fall back to title-only when no abstract.
  2. Embed locally with sentence-transformers (no API key, reproducible).
  3. Reduce (UMAP) + cluster (HDBSCAN). HDBSCAN leaves outliers unclustered,
     which is what we want — we don't force a partition.
  4. For each cluster, send a sample of its papers (title + short abstract) to
     Claude and ask it to propose 1-3 candidate ISSUES (questions) that the
     cluster's papers engage, plus a short label.
  5. Write discovered_issues.md (human-readable, for pruning) and
     discovered_issues.json (cluster membership + proposed issues).

Usage:
  python discover_issues.py                 # score>=3 tier
  python discover_issues.py --threshold 4   # tighter
  python discover_issues.py --min-cluster 6 # HDBSCAN min cluster size
"""

import os, json, argparse, re, time
import numpy as np
from llm_client import call_model

FOLDER     = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH = os.path.join(FOLDER, "citation_graph.json")
OUT_MD     = os.path.join(FOLDER, "discovered_issues.md")
OUT_JSON   = os.path.join(FOLDER, "discovered_issues.json")
EMB_CACHE  = os.path.join(FOLDER, "issue_embeddings.npz")

EMBED_MODEL = "all-MiniLM-L6-v2"   # small, fast, local; good enough for discovery


def load_corpus(threshold):
    g = json.load(open(GRAPH_PATH))
    nodes, scores = g["nodes"], g["scores"]
    rows = []
    for k, n in nodes.items():
        sc = (scores.get(k) or {}).get("score", 0)
        if sc < threshold:
            continue
        title = (n.get("title") or "").strip()
        abs   = (n.get("abstract") or "").strip()
        if not title:
            continue
        text = f"{title}. {abs}" if abs else title
        rows.append({
            "key": k,
            "title": title,
            "authors": n.get("authors") or [],
            "year": n.get("year"),
            "score": sc,
            "has_abstract": bool(abs),
            "text": text[:2000],
        })
    return rows


def embed(texts):
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(EMBED_MODEL)
    return np.asarray(model.encode(texts, show_progress_bar=True,
                                   batch_size=64, normalize_embeddings=True))


def cluster(emb, min_cluster):
    import umap, hdbscan
    # Reduce to a modest dimensionality before density clustering.
    reducer = umap.UMAP(n_neighbors=15, n_components=10, metric="cosine",
                        random_state=42)
    red = reducer.fit_transform(emb)
    clu = hdbscan.HDBSCAN(min_cluster_size=min_cluster, min_samples=2,
                          metric="euclidean", cluster_selection_method="eom")
    labels = clu.fit_predict(red)
    return labels, red


# ── Claude: propose issues from a cluster ────────────────────────────────────

ISSUE_SYSTEM = """You are helping a philosopher map the literature on moral and
legal responsibility for negligence (acts and omissions). You will be shown a
CLUSTER of papers (titles + abstract snippets) that an embedding model grouped
together.

Your job is NOT to summarize the cluster as a single 'topic'. Instead, identify
the underlying ISSUES — the substantive questions, debates, or problems — that
the papers in this cluster engage. A good issue is phrased as a QUESTION a
philosopher would argue about, e.g.:
  - "Can a person be culpable for a risk they never adverted to?"
  - "Does the reasonable-person standard smuggle in normative judgments?"
  - "Is the capacity to have known sufficient for blameworthiness?"

Rules:
- Propose 1-3 issues for the cluster. Fewer is better if the cluster is tight.
- Issues should CROSS-CUT: it's fine (expected) that an issue you name here also
  applies to papers in other clusters. Don't try to make issues mutually
  exclusive.
- Give each a short label (3-6 words) AND the question form.
- Also give a one-line gloss of what holds this cluster together.
- Return ONLY valid JSON:
{
  "cluster_gloss": "...",
  "issues": [
    {"label": "...", "question": "...?"},
    ...
  ]
}"""


def propose_issues(cluster_rows):
    sample = cluster_rows[:18]   # cap tokens; sample the cluster
    lines = []
    for r in sample:
        au = ", ".join((r["authors"] or [])[:2])
        snip = r["text"][:280]
        lines.append(f"- ({r['year']}) {r['title']} — {au}\n    {snip}")
    prompt = "CLUSTER ({} papers, showing {}):\n\n{}".format(
        len(cluster_rows), len(sample), "\n".join(lines))
    txt = call_model(
        system=ISSUE_SYSTEM,
        user=prompt,
        model="fast",
        max_tokens=700,
    )
    txt = re.sub(r"^```(?:json)?\n?", "", txt)
    txt = re.sub(r"\n?```$", "", txt)
    try:
        return json.loads(txt)
    except Exception as e:
        return {"cluster_gloss": f"[parse error: {e}]", "issues": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=3)
    ap.add_argument("--min-cluster", type=int, default=6)
    ap.add_argument("--no-llm", action="store_true",
                    help="cluster only; skip Claude issue proposals")
    args = ap.parse_args()

    rows = load_corpus(args.threshold)
    print(f"Corpus (score>={args.threshold}): {len(rows)} papers "
          f"({sum(r['has_abstract'] for r in rows)} with abstracts)")

    texts = [r["text"] for r in rows]

    # Embed (cache to avoid recompute)
    if os.path.exists(EMB_CACHE):
        d = np.load(EMB_CACHE, allow_pickle=True)
        if d["keys"].tolist() == [r["key"] for r in rows]:
            print("Using cached embeddings.")
            emb = d["emb"]
        else:
            print("Cache stale; re-embedding.")
            emb = embed(texts)
            np.savez(EMB_CACHE, emb=emb, keys=np.array([r["key"] for r in rows], dtype=object))
    else:
        emb = embed(texts)
        np.savez(EMB_CACHE, emb=emb, keys=np.array([r["key"] for r in rows], dtype=object))

    labels, _ = cluster(emb, args.min_cluster)
    for r, l in zip(rows, labels):
        r["cluster"] = int(l)

    n_clusters = len(set(labels) - {-1})
    n_noise = int((labels == -1).sum())
    print(f"HDBSCAN: {n_clusters} clusters, {n_noise} unclustered (noise)")

    # Group
    clusters = {}
    for r in rows:
        clusters.setdefault(r["cluster"], []).append(r)

    # Claude issue proposals
    results = {}
    if not args.no_llm:
        for cid in sorted(c for c in clusters if c != -1):
            members = sorted(clusters[cid], key=lambda x: -x["score"])
            print(f"  cluster {cid}: {len(members)} papers -> proposing issues...")
            results[cid] = propose_issues(members)
            time.sleep(0.2)

    # ── Write JSON ──
    out = {
        "threshold": args.threshold,
        "n_papers": len(rows),
        "n_clusters": n_clusters,
        "n_noise": n_noise,
        "clusters": {
            str(cid): {
                "size": len(clusters[cid]),
                "proposed": results.get(cid, {}),
                "papers": [
                    {"key": m["key"], "title": m["title"], "year": m["year"],
                     "score": m["score"], "authors": m["authors"]}
                    for m in sorted(clusters[cid], key=lambda x: -x["score"])
                ],
            } for cid in clusters
        },
    }
    json.dump(out, open(OUT_JSON, "w"), indent=2, ensure_ascii=False)

    # ── Write Markdown (for human pruning) ──
    lines = [
        "# Discovered Issues (bottom-up draft — for pruning)\n",
        f"Tier: score >= {args.threshold}  |  {len(rows)} papers  |  "
        f"{n_clusters} clusters  |  {n_noise} unclustered\n",
        "\n*These are CANDIDATE issues proposed from embedding clusters. They are "
        "meant to be pruned/merged/renamed, and they are expected to cross-cut. "
        "The final structure is an issue x paper engagement matrix, not this "
        "partition.*\n",
        "\n---\n",
    ]
    for cid in sorted(c for c in clusters if c != -1):
        info = results.get(cid, {})
        members = sorted(clusters[cid], key=lambda x: -x["score"])
        lines.append(f"\n## Cluster {cid}  ({len(members)} papers)\n")
        if info.get("cluster_gloss"):
            lines.append(f"*{info['cluster_gloss']}*\n")
        for iss in info.get("issues", []):
            lines.append(f"- **{iss.get('label','')}** — {iss.get('question','')}\n")
        lines.append("\n<details><summary>papers</summary>\n\n")
        for m in members[:25]:
            au = ", ".join((m["authors"] or [])[:2])
            lines.append(f"  - [{m['score']}] {m['title']} — {au} ({m['year']})\n")
        if len(members) > 25:
            lines.append(f"  - … +{len(members)-25} more\n")
        lines.append("\n</details>\n")
    if n_noise:
        lines.append(f"\n## Unclustered ({n_noise})\n")
        lines.append("*Papers HDBSCAN left as outliers — review separately; some "
                     "may belong to issues not captured by any cluster.*\n")
    open(OUT_MD, "w", encoding="utf-8").write("".join(lines))

    print(f"\nWrote:\n  {OUT_MD}\n  {OUT_JSON}")


if __name__ == "__main__":
    main()

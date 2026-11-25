#!/usr/bin/env python3
"""Fig. 1: architecture of the testbed. Run from the testbed root: python3 analysis/fig_architecture.py"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

fig, ax = plt.subplots(figsize=(15, 6.6), dpi=300)
ax.set_xlim(0, 152); ax.set_ylim(0, 64); ax.axis("off")
INK, MUTED, ACC, BLUE, AQUA = "#0b0b0b", "#52514e", "#eb6834", "#2a78d6", "#1baf7a"

def box(x, y, w, h, title, lines=(), fc="#ffffff", ec="#9a9a97", tc=INK, lw=1.2, fs=7.0):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=1.2", fc=fc, ec=ec, lw=lw))
    ax.text(x + w / 2, y + h - 2.6, title, ha="center", va="center", fontsize=8.6, fontweight="bold", color=tc)
    for k, l in enumerate(lines):
        ax.text(x + 1.4, y + h - 6.4 - 2.9 * k, l, ha="left", va="center", fontsize=fs, color=MUTED)

def arrow(x1, y1, x2, y2, color=INK, lw=1.4, ls="-", text=None, toff=(0, 1.0), fs=7.0, style="-|>"):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style, mutation_scale=12, color=color, lw=lw, linestyle=ls))
    if text:
        ax.text((x1 + x2) / 2 + toff[0], (y1 + y2) / 2 + toff[1], text, fontsize=fs, color=color, ha="center", va="bottom")

box(2, 34, 25, 23, "Open-loop generator", ["fixed-rate arrivals, 20 req/s", "same seeded trace per policy", "reads: product / compare /", "  category paraphrases", "writes: price, stock", "per-request outcome log"])
box(33, 4, 60, 55, "Gateway (FastAPI, async)", [])
ax.add_patch(FancyBboxPatch((35, 26), 56, 28, boxstyle="round,pad=0,rounding_size=1", fc="#fdf1ec", ec=ACC, lw=1.2))
ax.text(63, 51.3, "read   POST /api/query", ha="center", fontsize=8.0, fontweight="bold", color=ACC)
for k, l in enumerate(["1  embed q (WordLlama, 256-d); resolve entity set S_q (dictionary linker)",
                       "2  read committed versions ver_t(e), e ∈ S_q  (correctness probe)",
                       "3  graph-scoped nearest neighbour: WHERE entity_set = S_q,",
                       "    ORDER BY vec <=> v_q LIMIT 1",
                       "4  hit if cos ≥ τ: answer from Redis (stale and wrong-entity flags)",
                       "5  miss: read context rows (one snapshot, Sec. III-D); call the LLM",
                       "    without holding a database connection",
                       "6  insert under advisory locks on S_q: re-check versions, reject if",
                       "    changed, else store entry, edges (e, c) ∈ E and Redis payload"]):
    ax.text(36.4, 47.9 - 2.45 * k, l, fontsize=6.5, color=INK, ha="left", va="center")
ax.add_patch(FancyBboxPatch((35, 6), 56, 17, boxstyle="round,pad=0,rounding_size=1", fc="#e9f1fb", ec=BLUE, lw=1.2))
ax.text(63, 20.4, "write   POST /api/product/{id}", ha="center", fontsize=8.0, fontweight="bold", color=BLUE)
for k, l in enumerate(["1  advisory locks (product:id, category:c) in canonical order",
                       "2  UPDATE products ... version = version + 1",
                       "3  DELETE cache rows in Invalidate(product:id) ∪ Invalidate(category:c)",
                       "4  COMMIT, then Redis DEL of payloads and dependency sets",
                       "    (graph-cdc: steps 3–4 run asynchronously)"]):
    ax.text(36.4, 17.0 - 2.45 * k, l, fontsize=6.5, color=INK, ha="left", va="center")

box(98, 36, 25, 23, "PostgreSQL 16 + pgvector", ["products (5,000, versioned)", "semantic_cache: vec(256),", "  HNSW, entity_set, answer,", "  versions", "cache_deps (entity, cache_id)", "advisory transaction locks"], fs=6.7)
box(98, 4, 25, 23, "Redis 7", ["answer:<cache_id> payload", "entity:<key>:caches (set)", "served on hit; deleted", "on invalidation"], fs=6.7)
box(127, 36, 23, 23, "Emulated LLM", ["TTFT ~ lognormal(500 ms)", "50 tokens/s", "answer grounded in rows", "tokens priced at", r"\$0.15 / \$0.60 per 1M"], fs=6.7)
box(127, 4, 23, 23, "CDC invalidator", ["graph-cdc only", "LISTEN entity_change", "pipeline lag 0, 100", "  or 1000 ms", "DELETE Invalidate(e)"], fs=6.7)
box(2, 4, 25, 23, "200 ms monitor", ["/metrics counters", "cache size, tokens, cost", "Redis memory,", "PostgreSQL activity"])

arrow(27, 45.5, 33, 45.5); ax.text(30, 47.0, "HTTP", fontsize=6.8, ha="center", va="bottom")
arrow(27, 15.5, 33, 15.5, color=MUTED, ls=":", lw=1.0); ax.text(30, 17.0, "poll", fontsize=6.8, color=MUTED, ha="center", va="bottom")
arrow(93, 48, 98, 48); ax.text(95.5, 49.4, "SQL", fontsize=6.8, ha="center", va="bottom")
arrow(93, 15.5, 98, 15.5); ax.text(95.5, 17.0, "GET / DEL", fontsize=6.8, ha="center", va="bottom")
arrow(93, 31.5, 127, 31.5, ls="--", color=MUTED)
ax.text(110, 32.4, "miss: prompt → completion", fontsize=6.8, color=MUTED, ha="center", va="bottom")
arrow(123, 19.5, 127, 19.5, color=AQUA, ls="--"); ax.text(125, 20.8, "NOTIFY", fontsize=6.5, color=AQUA, ha="center", va="bottom")
arrow(127, 11, 123, 11, color=AQUA, ls="--"); ax.text(125, 7.4, "DELETE", fontsize=6.5, color=AQUA, ha="center", va="bottom")
ax.text(76, 62.6, "every hit is checked server-side against committed versions read before the lookup", ha="center", fontsize=7.2, color=MUTED, style="italic")
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "paper", "fig_architecture.png")
fig.savefig(out, bbox_inches="tight", facecolor="white")
print("ok", out)

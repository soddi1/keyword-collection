"""Post-hoc analysis for the Stage 1 crawl.

Reads the checkpoint (`crawl_state.json`) written by `crawl.py` and produces:

  * saturation_data.csv - the per-video series (replottable)
  * saturation.png      - two-panel saturation evidence
  * flowchart.md        - mermaid decision flowchart with real run counts
  * flowchart.png       - a matplotlib rendering of the same, for documents

Run: python3 analysis.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import config


def load_state() -> dict:
    p = config.OUTPUT_DIR / config.CHECKPOINT_JSON
    if not p.exists():
        raise SystemExit(
            f"No checkpoint at {p}. Run crawl.py first (even a smoke test)."
        )
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# Saturation
def write_saturation_csv(series: List[dict]) -> "object":
    import pandas as pd

    df = pd.DataFrame(series)
    out = config.OUTPUT_DIR / config.SATURATION_DATA_CSV
    df.to_csv(out, index=False)
    print(f"[analysis] wrote {out}")
    return df


def plot_saturation(df) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if df.empty:
        print("[analysis] no samples to plot; skipping saturation.png")
        return

    fig, (ax_top, ax_bot) = plt.subplots(2, 1, figsize=(11, 9), sharex=True)

    # --- Top: cumulative unique relevant keywords vs videos processed -----
    ax_top.plot(
        df["cumulative_videos"],
        df["relevant_total"],
        color="black",
        linewidth=2,
        label="combined",
    )
    # Per-keyword overlay so you can see which seed contributed what.
    for kw, grp in df.groupby("keyword", sort=False):
        ax_top.plot(
            grp["cumulative_videos"],
            grp["relevant_total"],
            linewidth=1,
            alpha=0.6,
            label=str(kw),
        )
    ax_top.set_ylabel("Cumulative unique relevant keywords")
    ax_top.set_title("Saturation: relevant-keyword discovery vs videos processed")
    ax_top.legend(fontsize=8, ncol=2)
    ax_top.grid(True, alpha=0.3)

    # --- Bottom: consecutive-dry-video counter with the 200 cutoff line ---
    ax_bot.plot(
        df["cumulative_videos"],
        df["dry_streak"],
        color="tab:red",
        linewidth=1.2,
        label="consecutive dry videos",
    )
    ax_bot.axhline(
        config.SATURATION_LIMIT,
        color="tab:blue",
        linestyle="--",
        label=f"cutoff = {config.SATURATION_LIMIT}",
    )
    ax_bot.set_xlabel("Cumulative videos processed")
    ax_bot.set_ylabel("Consecutive dry videos")
    ax_bot.legend(fontsize=8)
    ax_bot.grid(True, alpha=0.3)

    fig.tight_layout()
    out = config.OUTPUT_DIR / config.SATURATION_PNG
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"[analysis] wrote {out}")


# Decision flowchart with real counts
def compute_counts(state: dict) -> Dict[str, int]:
    videos = state.get("videos", {})
    keywords = state.get("keywords", {})
    edges = state.get("edges", [])
    progress = state.get("progress", {})
    stop_reasons = progress.get("stop_reasons", {})

    accepted = sum(1 for r in keywords.values() if r.get("label") == 1)
    rejected = sum(1 for r in keywords.values() if r.get("label") == 0)

    return {
        "videos_fetched": progress.get("videos_processed", len(videos)),
        "unique_videos": len(videos),
        "shorts_skipped": progress.get("shorts_skipped", 0),
        "words_prompted": len(keywords),
        "words_accepted": accepted,
        "words_rejected": rejected,
        "edges": len(edges),
        "stopped_saturation": sum(
            1 for v in stop_reasons.values() if v == "saturation"
        ),
        "stopped_pages": sum(
            1
            for v in stop_reasons.values()
            if v in ("no_next_page", "max_pages", "no_items")
        ),
    }


def write_flowchart_md(counts: Dict[str, int]) -> str:
    mermaid = f"""```mermaid
flowchart TD
    Start([Start]) --> NextKw[Take next seed keyword]
    NextKw --> Search["search.list, 50 results"]
    Search --> Enrich["videos.list + channels.list"]
    Enrich --> NextVid["Next video<br/>fetched = {counts['videos_fetched']}"]
    NextVid --> Dedup{{"video_id seen before?<br/>unique = {counts['unique_videos']}"}}
    Dedup -->|yes| NextVid
    Dedup -->|no| ShortCheck{{"duration <= 180s (Short)?<br/>skipped = {counts['shorts_skipped']}"}}
    ShortCheck -->|yes| NextVid
    ShortCheck -->|no| Save[Write row to videos_master]
    Save --> Extract["Extract words from title + tags + desc 500"]
    Extract --> NextWord["Next candidate word<br/>prompted = {counts['words_prompted']}"]
    NextWord --> Known{{"already in seed / relevant / irrelevant?"}}
    Known -->|yes| Bump[increment occurrence_count] --> NextWord
    Known -->|no| Ask{{"prompt user: 0 or 1"}}
    Ask -->|"1 (accepted = {counts['words_accepted']})"| Rel[relevant txt + csv, reset dry counter] --> NextWord
    Ask -->|"0 (rejected = {counts['words_rejected']})"| Irr[irrelevant txt + csv] --> NextWord
    NextWord -->|page done| Dry{{"200 consecutive dry videos?"}}
    Dry -->|"yes (stopped = {counts['stopped_saturation']})"| NextKw
    Dry -->|no| Page{{nextPageToken exists?}}
    Page -->|yes| Search
    Page -->|"no (stopped = {counts['stopped_pages']})"| NextKw
    NextKw -->|no keywords left| Analyze[Saturation plot + flowchart]
    Analyze --> Done([End])
```
"""
    out = config.OUTPUT_DIR / config.FLOWCHART_MD
    out.write_text(mermaid, encoding="utf-8")
    print(f"[analysis] wrote {out}")
    return mermaid


def render_flowchart_png(counts: Dict[str, int]) -> None:
    """A simple boxes-and-arrows rendering for pasting into a document.

    (matplotlib can't render mermaid, so we draw a faithful summary instead.)
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

    steps = [
        ("Take next seed keyword", ""),
        ("search.list (50 results)", ""),
        ("Enrich: videos + channels", ""),
        (f"Next video", f"fetched = {counts['videos_fetched']}"),
        (f"Dedup by video_id", f"unique = {counts['unique_videos']}"),
        (
            "Drop Shorts (duration <= 180s)",
            f"skipped = {counts['shorts_skipped']}",
        ),
        ("Extract candidate words", ""),
        (f"Prompt user 0/1", f"prompted = {counts['words_prompted']}"),
        (
            "Route by label",
            f"relevant = {counts['words_accepted']} | "
            f"irrelevant = {counts['words_rejected']}",
        ),
        (
            "Saturation check (200 dry)",
            f"stopped: saturation = {counts['stopped_saturation']} | "
            f"pages = {counts['stopped_pages']}",
        ),
        ("Saturation plot + flowchart", ""),
    ]

    fig, ax = plt.subplots(figsize=(8, 12))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, len(steps) * 2 + 1)
    ax.axis("off")

    y = len(steps) * 2
    box_h = 1.1
    centers = []
    for title, sub in steps:
        box = FancyBboxPatch(
            (1.5, y - box_h / 2),
            7,
            box_h,
            boxstyle="round,pad=0.1",
            linewidth=1.4,
            edgecolor="black",
            facecolor="none",
        )
        ax.add_patch(box)
        label = title if not sub else f"{title}\n{sub}"
        ax.text(5, y, label, ha="center", va="center", fontsize=9)
        centers.append(y)
        y -= 2

    for i in range(len(centers) - 1):
        arrow = FancyArrowPatch(
            (5, centers[i] - box_h / 2),
            (5, centers[i + 1] + box_h / 2),
            arrowstyle="-|>",
            mutation_scale=14,
            linewidth=1.2,
            color="black",
        )
        ax.add_patch(arrow)

    ax.set_title("Stage 1 crawl - decision flow (with run counts)", fontsize=12)
    fig.tight_layout()
    out = config.OUTPUT_DIR / config.FLOWCHART_PNG
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"[analysis] wrote {out}")


# Entry point
def main() -> int:
    state = load_state()
    series = state.get("progress", {}).get("series", [])

    df = write_saturation_csv(series)
    plot_saturation(df)

    counts = compute_counts(state)
    write_flowchart_md(counts)
    render_flowchart_png(counts)

    print("\n[analysis] summary:")
    for k, v in counts.items():
        print(f"  {k:>20}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

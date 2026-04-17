"""Eval-harness aggregator — baseline vs post delta.

Reads two JSONL run files and emits a markdown summary with per-prompt
and totals deltas across the key metrics defined in
audit/04-eval-harness.md §2:

    - tool_definitions_tokens
    - prompt_tokens
    - completion_tokens
    - total_response_tokens
    - grand_total_tokens
    - total_tool_calls
    - routing_match
    - wall_clock_ms
    - error_count

Usage:
    python audit/eval/aggregate.py \\
        --baseline audit/eval/runs/v0.5.0-baseline.jsonl \\
        --compare  audit/eval/runs/v0.6.0-postchange.jsonl \\
        --out      audit/eval/runs/v0.6.0-delta.md

If --out is omitted the report is written to stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


METRIC_KEYS = [
    "tool_definitions_tokens",
    "prompt_tokens",
    "completion_tokens",
    "total_response_tokens",
    "grand_total_tokens",
    "total_tool_calls",
    "wall_clock_ms",
    "error_count",
]


def load_run(path: Path) -> Dict[int, Dict[str, Any]]:
    """Load a JSONL run file into {prompt_id: record}."""
    by_id: Dict[int, Dict[str, Any]] = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            by_id[record["prompt_id"]] = record
    return by_id


def format_delta(old: Optional[int | float], new: Optional[int | float]) -> str:
    if old is None or new is None:
        return "–"
    diff = new - old
    pct = (diff / old * 100) if old else float("inf") if diff else 0.0
    sign = "+" if diff >= 0 else ""
    if isinstance(new, float) or isinstance(old, float):
        return f"{sign}{diff:.1f} ({sign}{pct:.0f}%)"
    return f"{sign}{diff} ({sign}{pct:.0f}%)"


def summarize(baseline: Dict[int, Dict[str, Any]], compare: Dict[int, Dict[str, Any]]) -> str:
    lines: List[str] = []
    baseline_id = next(iter(baseline.values()), {}).get("run_id", "baseline")
    compare_id = next(iter(compare.values()), {}).get("run_id", "compare")

    lines.append(f"# Eval delta: `{baseline_id}` → `{compare_id}`")
    lines.append("")
    lines.append(
        f"Baseline: {len(baseline)} prompts. "
        f"Compare: {len(compare)} prompts. "
        f"Shared: {len(set(baseline) & set(compare))}."
    )
    lines.append("")

    # Totals table.
    lines.append("## Totals across shared prompts")
    lines.append("")
    lines.append("| Metric | Baseline | Compare | Delta |")
    lines.append("|---|---:|---:|---:|")
    shared = sorted(set(baseline) & set(compare))
    for key in METRIC_KEYS:
        b_sum = sum(baseline[i].get(key, 0) for i in shared)
        c_sum = sum(compare[i].get(key, 0) for i in shared)
        lines.append(f"| `{key}` | {b_sum} | {c_sum} | {format_delta(b_sum, c_sum)} |")
    lines.append("")

    # Routing comparison.
    lines.append("## Routing match counts (compare run)")
    lines.append("")
    routing_counts: Dict[str, int] = {}
    for i in shared:
        r = compare[i].get("routing_match", "unknown")
        routing_counts[r] = routing_counts.get(r, 0) + 1
    for label in ("exact", "subset", "different", "unknown"):
        lines.append(f"- `{label}`: {routing_counts.get(label, 0)}")
    lines.append("")

    # Per-prompt highlights: biggest grand_total_tokens change, wins & regressions.
    lines.append("## Per-prompt deltas (by grand_total_tokens)")
    lines.append("")
    lines.append("| ID | Category | Baseline tokens | Compare tokens | Δ | Calls Δ | Routing (compare) |")
    lines.append("|---:|---|---:|---:|---:|---:|---|")
    rows = []
    for i in shared:
        b = baseline[i]
        c = compare[i]
        rows.append((
            i,
            c.get("prompt_category", ""),
            b.get("grand_total_tokens", 0),
            c.get("grand_total_tokens", 0),
            c.get("grand_total_tokens", 0) - b.get("grand_total_tokens", 0),
            c.get("total_tool_calls", 0) - b.get("total_tool_calls", 0),
            c.get("routing_match", "unknown"),
        ))
    # Sort so biggest wins (most negative delta) are at the top.
    rows.sort(key=lambda r: r[4])
    for r in rows:
        lines.append(
            f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]:+d} | {r[5]:+d} | `{r[6]}` |"
        )
    lines.append("")

    # Errors / regressions.
    regressions = [i for i in shared if compare[i].get("error_count", 0) > baseline[i].get("error_count", 0)]
    if regressions:
        lines.append("## Error-count regressions")
        lines.append("")
        for i in regressions:
            lines.append(
                f"- Prompt {i}: baseline errors = {baseline[i]['error_count']}, "
                f"compare errors = {compare[i]['error_count']}"
            )
        lines.append("")

    # Prompts present in only one side.
    only_in_baseline = sorted(set(baseline) - set(compare))
    only_in_compare = sorted(set(compare) - set(baseline))
    if only_in_baseline or only_in_compare:
        lines.append("## Coverage asymmetry")
        lines.append("")
        if only_in_baseline:
            lines.append(f"- Baseline only: {only_in_baseline}")
        if only_in_compare:
            lines.append(f"- Compare only: {only_in_compare}")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--compare", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="Write markdown report here; stdout if omitted.")
    args = parser.parse_args()

    baseline = load_run(args.baseline)
    compare = load_run(args.compare)
    report = summarize(baseline, compare)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report)
        sys.stderr.write(f"[eval] wrote {args.out}\n")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

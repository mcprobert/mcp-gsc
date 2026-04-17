"""One-shot A.4 namespace rename. Renames 24 bare @mcp.tool functions
to `gsc_` prefix using word-boundary regex. Run once; do not re-run
(rerunning would produce `gsc_gsc_*`).

Targets:
    gsc_server.py     — function defs, internal calls, tool= envelopes,
                        _instrument names, disambiguation hints
    tests/*.py        — imports, calls
    audit/eval/prompts.json — expected_tool_path entries
    README.md, CLAUDE.md — user-facing references

Leaves alone (historical snapshots):
    audit/01-discovery.md, audit/02-scorecard.md,
    audit/03-remediation-plan.md, audit/04-eval-harness.md,
    audit/_work/live_samples.json, audit/_work/tool_tokens.json
    CHANGELOG.md entries for earlier versions
"""
from __future__ import annotations

import re
from pathlib import Path

RENAMES = [
    "list_properties",
    "add_site",
    "delete_site",
    "get_search_analytics",
    "get_site_details",
    "get_sitemaps",
    "inspect_url_enhanced",
    "batch_url_inspection",
    "check_indexing_issues",
    "get_performance_overview",
    "get_advanced_search_analytics",
    "compare_search_periods",
    "get_search_by_page_query",
    "list_sitemaps_enhanced",
    "get_sitemap_details",
    "submit_sitemap",
    "delete_sitemap",
    "manage_sitemaps",
    "list_accounts",
    "get_active_account",
    "add_account",
    "switch_account",
    "remove_account",
    "get_creator_info",
]

# Sort longest-first to avoid substring clashes during replacement.
RENAMES.sort(key=len, reverse=True)

ROOT = Path(__file__).resolve().parents[2]

TARGETS = [
    ROOT / "gsc_server.py",
    ROOT / "README.md",
    ROOT / "CLAUDE.md",
    ROOT / "audit" / "eval" / "prompts.json",
    ROOT / "audit" / "eval" / "README.md",
    ROOT / "audit" / "eval" / "run.py",
    *sorted((ROOT / "tests").glob("*.py")),
]


def rename_in_text(text: str) -> tuple[str, int]:
    """Apply all renames to `text` using word-boundary regex.

    Returns (new_text, total_substitutions).
    """
    count = 0
    for old in RENAMES:
        # Word boundary on both sides. Python's \b treats underscores as
        # word chars, so `\blist_properties\b` matches the exact token.
        pattern = rf"\b{re.escape(old)}\b"
        new_text, n = re.subn(pattern, f"gsc_{old}", text)
        if n > 0:
            count += n
            text = new_text
    return text, count


def main() -> None:
    summary: list[tuple[Path, int]] = []
    for target in TARGETS:
        if not target.exists():
            print(f"  skip (missing): {target}")
            continue
        original = target.read_text(encoding="utf-8")
        updated, n = rename_in_text(original)
        if n > 0 and updated != original:
            target.write_text(updated, encoding="utf-8")
        summary.append((target, n))

    total = sum(n for _, n in summary)
    print(f"Total substitutions: {total}")
    print()
    for target, n in summary:
        rel = target.relative_to(ROOT)
        print(f"  {n:>5} {rel}")


if __name__ == "__main__":
    main()

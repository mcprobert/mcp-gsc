"""Typed records for eval-harness runs.

One JSON line per prompt is written to `audit/eval/runs/<run_id>.jsonl`.
The shape is deliberately flat so jq / csvkit / pandas can slice it
without nested navigation.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class ToolCallRecord:
    """One tool invocation within a prompt run."""

    name: str
    args: Dict[str, Any]
    response_chars: int
    response_tokens_est: int
    duration_ms: int
    error: Optional[str] = None


@dataclass
class PromptRunRecord:
    """Per-prompt log line. Flat by design."""

    run_id: str
    prompt_id: int
    prompt_category: str
    prompt_text: str
    model: str
    temperature: float
    prompt_cache: str  # "disabled" | "enabled"
    # Token accounting — split per audit/04-eval-harness.md §2.
    tool_definitions_tokens: int
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_response_tokens: int
    grand_total_tokens: int
    # Tool behaviour.
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    total_tool_calls: int = 0
    expected_tool_path: List[str] = field(default_factory=list)
    actual_tool_path: List[str] = field(default_factory=list)
    routing_match: str = "unknown"  # "exact" | "subset" | "different" | "unknown"
    # Quality / errors.
    wall_clock_ms: int = 0
    error_count: int = 0
    retry_count: int = 0
    final_answer: str = ""
    # Diff vs golden (filled by aggregate.py, not run.py).
    golden_diff: str = "pending"  # "exact_match" | "semantic_match" | "mismatch" | "pending"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def classify_routing(expected: List[str], actual: List[str]) -> str:
    """Compare expected vs actual tool-call sequence.

    "exact" — same sequence, same order.
    "subset" — every expected tool appears in actual (possibly with extra calls).
    "different" — at least one expected tool is missing.
    """
    if not expected:
        return "unknown"
    if expected == actual:
        return "exact"
    if set(expected).issubset(set(actual)):
        return "subset"
    return "different"

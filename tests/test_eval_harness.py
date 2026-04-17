"""Unit tests for the eval harness.

Guards against the regressions flagged in the post-harness code review:

- grand_total_tokens must equal prompt_tokens + completion_tokens (no
  double-counting of tool schemas or tool-response text).
- format_delta must never render "inf%" in a reader-facing table.
- classify_routing must distinguish ordered subsequences from
  out-of-order supersets.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# The eval harness lives outside the installed package, so import it by
# file path to avoid leaking it into the server's import surface.
ROOT = Path(__file__).resolve().parent.parent
_EVAL_DIR = ROOT / "audit" / "eval"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, _EVAL_DIR / filename, submodule_search_locations=None
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


aggregate = _load_module("eval_aggregate", "aggregate.py")


class TestClassifyRouting:
    """_classify_routing lives in run.py; we re-import via path."""

    @classmethod
    def setup_class(cls):
        cls.run = _load_module("eval_run", "run.py")

    def test_exact_match(self):
        assert self.run._classify_routing(["A", "B"], ["A", "B"]) == "exact"

    def test_unknown_when_no_expected(self):
        assert self.run._classify_routing([], ["A"]) == "unknown"

    def test_ordered_subset_with_extra_calls(self):
        # Agent did [A, X, B] — A and B appear in order, X is a bonus call.
        assert self.run._classify_routing(["A", "B"], ["A", "X", "B"]) == "ordered_subset"

    def test_subset_out_of_order(self):
        # [B, A] contains both A and B but not in the expected order.
        assert self.run._classify_routing(["A", "B"], ["B", "A"]) == "subset"

    def test_different_when_expected_tool_missing(self):
        assert self.run._classify_routing(["A", "B"], ["A", "C"]) == "different"

    def test_different_when_actual_empty(self):
        assert self.run._classify_routing(["A"], []) == "different"


class TestFormatDelta:
    def test_same_value_returns_zero(self):
        assert aggregate.format_delta(100, 100) == "+0 (+0%)"

    def test_positive_delta(self):
        assert aggregate.format_delta(100, 150) == "+50 (+50%)"

    def test_negative_delta(self):
        assert aggregate.format_delta(100, 50) == "-50 (-50%)"

    def test_zero_baseline_no_change(self):
        assert aggregate.format_delta(0, 0) == "no change"

    def test_zero_baseline_regression_renders_readable(self):
        # Never render raw "inf%".
        out = aggregate.format_delta(0, 5)
        assert "inf" not in out
        assert "+5" in out

    def test_zero_baseline_negative_delta_renders_readable(self):
        out = aggregate.format_delta(0, -3)
        assert "inf" not in out
        assert "-3" in out


class TestGrandTotalRecomputedOnLoad:
    """load_run() must overwrite legacy double-counted grand_total values."""

    def test_legacy_record_is_recomputed(self, tmp_path):
        legacy = tmp_path / "legacy.jsonl"
        # Old run.py emitted grand_total = schema + prompt + completion + response.
        # load_run must replace it with prompt + completion on read.
        legacy.write_text(
            '{"prompt_id": 1, "prompt_tokens": 100, "completion_tokens": 50, '
            '"tool_definitions_tokens": 6000, "total_response_tokens": 200, '
            '"grand_total_tokens": 6350}\n'
        )
        loaded = aggregate.load_run(legacy)
        assert loaded[1]["grand_total_tokens"] == 150  # 100 + 50, not 6350

    def test_missing_completion_defaults_to_zero(self, tmp_path):
        f = tmp_path / "partial.jsonl"
        f.write_text('{"prompt_id": 2, "prompt_tokens": 42}\n')
        loaded = aggregate.load_run(f)
        assert loaded[2]["grand_total_tokens"] == 42

    def test_empty_lines_are_skipped(self, tmp_path):
        f = tmp_path / "gappy.jsonl"
        f.write_text(
            '{"prompt_id": 1, "prompt_tokens": 10, "completion_tokens": 5}\n'
            "\n"
            '{"prompt_id": 2, "prompt_tokens": 20, "completion_tokens": 5}\n'
        )
        loaded = aggregate.load_run(f)
        assert set(loaded) == {1, 2}
        assert loaded[1]["grand_total_tokens"] == 15
        assert loaded[2]["grand_total_tokens"] == 25


class TestSummarizeShape:
    def test_summary_contains_expected_sections_and_no_inf(self, tmp_path):
        b = tmp_path / "b.jsonl"
        c = tmp_path / "c.jsonl"
        b.write_text(
            '{"prompt_id": 1, "prompt_category": "m", "prompt_tokens": 100, '
            '"completion_tokens": 10, "tool_definitions_tokens": 500, '
            '"total_response_tokens": 20, "total_tool_calls": 1, '
            '"wall_clock_ms": 1000, "error_count": 0, "routing_match": "exact"}\n'
        )
        c.write_text(
            '{"prompt_id": 1, "prompt_category": "m", "prompt_tokens": 80, '
            '"completion_tokens": 10, "tool_definitions_tokens": 480, '
            '"total_response_tokens": 20, "total_tool_calls": 1, '
            '"wall_clock_ms": 900, "error_count": 0, "routing_match": "exact"}\n'
        )
        report = aggregate.summarize(aggregate.load_run(b), aggregate.load_run(c))
        assert "Totals across shared prompts" in report
        assert "Per-prompt deltas" in report
        assert "Routing match counts" in report
        assert "inf%" not in report

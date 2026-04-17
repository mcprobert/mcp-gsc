"""Tests for _format_table — the Tranche B shared response-shaping helper.

This helper is the ground-work for B.1 (tool consolidation) and B.2
(response_format enum on all tabular tools). The tests lock in the
three output shapes so the subsequent refactors can't silently drift
rendering between tools.
"""
from __future__ import annotations

import json

import pytest

from gsc_server import _format_table


COLUMNS = [
    {"key": "query", "display": "Query", "type": "str"},
    {"key": "clicks", "display": "Clicks", "type": "int"},
    {"key": "ctr", "display": "CTR", "type": "pct"},
    {"key": "position", "display": "Position", "type": "float"},
]
ROWS = [
    {"query": "chaser", "clicks": 672, "ctr": 0.0216, "position": 6.3},
    {"query": "chaser login", "clicks": 58, "ctr": 0.4793, "position": 1.3},
]


class TestMarkdown:
    def test_basic_shape(self):
        out = _format_table(ROWS, COLUMNS, response_format="markdown")
        assert "Query | Clicks | CTR | Position" in out
        assert "--- | --- | --- | ---" in out
        assert "chaser | 672 | 2.16% | 6.3" in out
        assert "chaser login | 58 | 47.93% | 1.3" in out

    def test_header_lines_prepended(self):
        out = _format_table(
            ROWS, COLUMNS,
            response_format="markdown",
            header_lines=["Search analytics for sc-domain:example.com", "Last 28 days"],
        )
        assert out.startswith("Search analytics for sc-domain:example.com\nLast 28 days\n\n")

    def test_truncation_warning_at_top(self):
        out = _format_table(
            ROWS, COLUMNS,
            response_format="markdown",
            truncated=True,
            truncation_hint="more rows available; pass row_limit=1000.",
        )
        # Must appear BEFORE the table header so agents can't skim past.
        assert out.index("⚠ TRUNCATED") < out.index("Query | Clicks")

    def test_no_truncation_warning_when_not_truncated(self):
        out = _format_table(
            ROWS, COLUMNS,
            response_format="markdown",
            truncation_hint="irrelevant",  # provided but truncated=False
        )
        assert "TRUNCATED" not in out


class TestCsv:
    def test_basic_shape(self):
        out = _format_table(ROWS, COLUMNS, response_format="csv")
        lines = out.split("\r\n")
        assert lines[0] == "Query,Clicks,CTR,Position"
        assert lines[1] == "chaser,672,2.16%,6.3"
        assert lines[2] == "chaser login,58,47.93%,1.3"

    def test_csv_quoting_for_comma_cells(self):
        rows = [{"query": "a,b", "clicks": 1, "ctr": 0.1, "position": 1.0}]
        out = _format_table(rows, COLUMNS, response_format="csv")
        assert '"a,b"' in out

    def test_csv_quoting_for_embedded_quotes(self):
        rows = [{"query": 'he said "hi"', "clicks": 1, "ctr": 0.1, "position": 1.0}]
        out = _format_table(rows, COLUMNS, response_format="csv")
        assert '"he said ""hi"""' in out

    def test_header_lines_rendered_as_comments(self):
        out = _format_table(
            ROWS, COLUMNS,
            response_format="csv",
            header_lines=["Period: 28 days"],
        )
        assert out.startswith("# Period: 28 days\r\n")


class TestJson:
    def test_basic_shape(self):
        out = _format_table(ROWS, COLUMNS, response_format="json")
        assert isinstance(out, dict)
        assert out["ok"] is True
        assert out["columns"] == ["query", "clicks", "ctr", "position"]
        assert out["row_count"] == 2
        assert out["truncated"] is False
        # Rows preserve raw typed values, not stringified ones.
        assert out["rows"][0]["clicks"] == 672
        assert out["rows"][0]["ctr"] == pytest.approx(0.0216)

    def test_json_ignores_header_lines(self):
        out = _format_table(
            ROWS, COLUMNS,
            response_format="json",
            header_lines=["ignored"],
        )
        assert "header_lines" not in out
        assert "ignored" not in json.dumps(out)

    def test_json_truncation_hint_populated_only_when_truncated(self):
        out_t = _format_table(
            ROWS, COLUMNS,
            response_format="json",
            truncated=True,
            truncation_hint="raise row_limit",
        )
        assert out_t["truncated"] is True
        assert out_t["truncation_hint"] == "raise row_limit"

        out_f = _format_table(
            ROWS, COLUMNS,
            response_format="json",
            truncated=False,
            truncation_hint="irrelevant",
        )
        assert out_f["truncated"] is False
        assert out_f["truncation_hint"] == ""

    def test_meta_surfaces_on_json_only(self):
        out = _format_table(
            ROWS, COLUMNS,
            response_format="json",
            meta={"total_clicks": 730},
        )
        assert out["meta"] == {"total_clicks": 730}

        md = _format_table(ROWS, COLUMNS, response_format="markdown", meta={"x": 1})
        assert "x" not in md  # meta doesn't leak into markdown


class TestCellRendering:
    def test_pct_heuristic_ratio_vs_already_percent(self):
        # Ratio input (<=1) multiplies by 100.
        out = _format_table(
            [{"v": 0.25}], [{"key": "v", "display": "V", "type": "pct"}],
            response_format="markdown",
        )
        assert "25.00%" in out

        # Already-percent input (>1) stays as-is.
        out2 = _format_table(
            [{"v": 12.5}], [{"key": "v", "display": "V", "type": "pct"}],
            response_format="markdown",
        )
        assert "12.50%" in out2

    def test_none_renders_as_empty_string(self):
        rows = [{"query": None, "clicks": None, "ctr": None, "position": None}]
        out = _format_table(rows, COLUMNS, response_format="markdown")
        # The data row is the last line. Every cell should render empty;
        # rstrip to allow trailing whitespace from the final " | " separator.
        data_row = out.splitlines()[-1].rstrip()
        assert data_row == " |  |  |"

    def test_int_column_handles_non_numeric_gracefully(self):
        rows = [{"query": "q", "clicks": "n/a", "ctr": 0.1, "position": 1.0}]
        out = _format_table(rows, COLUMNS, response_format="markdown")
        # Falls back to str representation rather than crashing.
        assert "n/a" in out

    def test_signed_int_shows_plus_on_positive(self):
        rows = [{"d": 5}, {"d": -3}, {"d": 0}]
        cols = [{"key": "d", "display": "D", "type": "signed_int"}]
        out = _format_table(rows, cols, response_format="markdown")
        assert "+5" in out
        assert "-3" in out
        assert "+0" in out

    def test_signed_float_shows_plus_on_positive(self):
        rows = [{"d": 2.5}, {"d": -1.2}]
        cols = [{"key": "d", "display": "D", "type": "signed_float"}]
        out = _format_table(rows, cols, response_format="markdown")
        assert "+2.5" in out
        assert "-1.2" in out


class TestValidation:
    def test_unknown_format_returns_error_string(self):
        out = _format_table(ROWS, COLUMNS, response_format="xml")
        assert isinstance(out, str)
        assert out.startswith("Error: response_format must be one of")

    def test_empty_rows_still_renders_header(self):
        out = _format_table([], COLUMNS, response_format="markdown")
        assert "Query | Clicks | CTR | Position" in out
        assert "--- | ---" in out

    def test_empty_rows_json(self):
        out = _format_table([], COLUMNS, response_format="json")
        assert out["row_count"] == 0
        assert out["rows"] == []

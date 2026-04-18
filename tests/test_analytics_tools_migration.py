"""Tool-level tests for the three analytics tools migrated to
`_format_table` in B.2 (commit a9da902).

Prior coverage tested `_format_table` against hand-crafted fixtures but
never exercised the wiring in the tool functions themselves. That gap
let the truncation-ordering regression (Review issue 3) and the
`gsc_compare_search_periods` silent-truncation bug (Review issue 4) ship.
These tests cover both the behavioural contracts those fixes restore,
plus golden markdown shape + JSON shape for each migrated tool.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import gsc_server
from gsc_server import (
    gsc_compare_search_periods,
    gsc_get_advanced_search_analytics,
    gsc_get_search_analytics,
)


def _mock_analytics_service(rows):
    """Mock service whose searchanalytics().query().execute() returns rows."""
    service = MagicMock()

    def _query(*, siteUrl, body):
        req = MagicMock()
        req.execute.return_value = {"rows": rows} if rows is not None else {}
        return req

    service.searchanalytics.return_value.query.side_effect = _query
    return service


def _mock_two_period_service(period1_rows, period2_rows):
    """Mock for gsc_compare_search_periods — returns different rows per call."""
    service = MagicMock()
    calls = {"count": 0}

    def _query(*, siteUrl, body):
        req = MagicMock()
        # First call is period1, second is period2 (order matches the
        # tool's implementation).
        if calls["count"] == 0:
            req.execute.return_value = {"rows": period1_rows}
        else:
            req.execute.return_value = {"rows": period2_rows}
        calls["count"] += 1
        return req

    service.searchanalytics.return_value.query.side_effect = _query
    return service


def _patch(monkeypatch, service):
    monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)


# =============================================================================
# gsc_get_search_analytics
# =============================================================================


class TestGetSearchAnalyticsMigration:
    async def test_markdown_golden_shape(self, monkeypatch):
        rows = [
            {"keys": ["chaser"], "clicks": 672, "impressions": 31130, "ctr": 0.0216, "position": 6.3},
            {"keys": ["chaser login"], "clicks": 58, "impressions": 121, "ctr": 0.4793, "position": 1.3},
        ]
        _patch(monkeypatch, _mock_analytics_service(rows))
        out = await gsc_get_search_analytics(
            site_url="sc-domain:example.com", days=28, dimensions="query", row_limit=100
        )
        assert isinstance(out, str)
        # Header context line.
        assert "Search analytics for sc-domain:example.com (last 28 days)" in out
        # Pipe-table header row.
        assert "Query | Clicks | Impressions | CTR | Position" in out
        # Data rows — clicks/impressions render as ints (no ".0"
        # suffix), ctr as percent, position as 1-decimal float.
        assert "chaser | 672 | 31130 | 2.16% | 6.3" in out
        assert "chaser login | 58 | 121 | 47.93% | 1.3" in out

    async def test_json_shape(self, monkeypatch):
        rows = [
            {"keys": ["seo"], "clicks": 10, "impressions": 100, "ctr": 0.10, "position": 3.0},
        ]
        _patch(monkeypatch, _mock_analytics_service(rows))
        out = await gsc_get_search_analytics(
            site_url="sc-domain:example.com",
            row_limit=100,
            response_format="json",
        )
        assert isinstance(out, dict)
        assert out["ok"] is True
        assert out["columns"] == ["query", "clicks", "impressions", "ctr", "position"]
        assert out["rows"][0]["query"] == "seo"
        # Raw typed values preserved.
        assert out["rows"][0]["clicks"] == 10
        assert out["rows"][0]["ctr"] == pytest.approx(0.10)
        assert out["row_count"] == 1
        assert out["truncated"] is False
        assert out["meta"]["site_url"] == "sc-domain:example.com"

    async def test_empty_rows_returns_string_not_envelope(self, monkeypatch):
        _patch(monkeypatch, _mock_analytics_service([]))
        out = await gsc_get_search_analytics(site_url="sc-domain:example.com")
        assert isinstance(out, str)
        assert "No search analytics data found" in out

    async def test_truncation_warning_at_top_when_cap_hit(self, monkeypatch):
        rows = [
            {"keys": [f"kw {i}"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 1.0}
            for i in range(100)
        ]
        _patch(monkeypatch, _mock_analytics_service(rows))
        out = await gsc_get_search_analytics(
            site_url="sc-domain:example.com", row_limit=100
        )
        # Must be the first line — agents skim the top.
        assert out.startswith("⚠ TRUNCATED:")
        # Hint must reference the row_limit.
        assert "100" in out.splitlines()[0]

    async def test_csv_mode_returns_csv_string(self, monkeypatch):
        rows = [
            {"keys": ["kw a"], "clicks": 5, "impressions": 50, "ctr": 0.1, "position": 1.0},
        ]
        _patch(monkeypatch, _mock_analytics_service(rows))
        out = await gsc_get_search_analytics(
            site_url="sc-domain:example.com", response_format="csv"
        )
        assert isinstance(out, str)
        assert "Query,Clicks,Impressions,CTR,Position" in out
        assert "kw a,5,50,10.00%,1.0" in out


# =============================================================================
# gsc_get_advanced_search_analytics
# =============================================================================


class TestGetAdvancedSearchAnalyticsMigration:
    async def test_markdown_golden_shape(self, monkeypatch):
        rows = [
            {"keys": ["chaser"], "clicks": 672, "impressions": 31130, "ctr": 0.0216, "position": 6.3},
        ]
        _patch(monkeypatch, _mock_analytics_service(rows))
        out = await gsc_get_advanced_search_analytics(
            site_url="sc-domain:example.com",
            start_date="2026-03-20",
            end_date="2026-04-17",
            row_limit=100,
        )
        assert isinstance(out, str)
        assert "Date range: 2026-03-20 to 2026-04-17" in out
        assert "chaser | 672 | 31130 | 2.16% | 6.3" in out

    async def test_truncation_warning_at_top_takes_precedence_over_headers(self, monkeypatch):
        """A.1 invariant: when truncated, the warning must be line 1 —
        before the 'Search analytics for...' context lines."""
        rows = [
            {"keys": [f"kw {i}"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 1.0}
            for i in range(100)
        ]
        _patch(monkeypatch, _mock_analytics_service(rows))
        out = await gsc_get_advanced_search_analytics(
            site_url="sc-domain:example.com",
            row_limit=100,
        )
        first_line = out.splitlines()[0]
        assert first_line.startswith("⚠ TRUNCATED:")
        # The next_start_row hint for pagination must appear in the hint.
        assert "start_row=100" in first_line
        # Search analytics header must appear AFTER, not before.
        assert out.index("⚠ TRUNCATED") < out.index("Search analytics for")

    async def test_json_shape_with_truncation_hint_in_meta(self, monkeypatch):
        rows = [
            {"keys": [f"kw {i}"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 1.0}
            for i in range(100)
        ]
        _patch(monkeypatch, _mock_analytics_service(rows))
        out = await gsc_get_advanced_search_analytics(
            site_url="sc-domain:example.com",
            row_limit=100,
            response_format="json",
        )
        assert out["ok"] is True
        assert out["truncated"] is True
        assert "row_limit=100" in out["truncation_hint"]
        assert out["meta"]["next_start_row"] == 100

    async def test_filter_flows_through_to_request_and_header(self, monkeypatch):
        captured: dict = {}
        service = MagicMock()

        def _query(*, siteUrl, body):
            captured["body"] = body
            req = MagicMock()
            req.execute.return_value = {"rows": []}
            return req

        service.searchanalytics.return_value.query.side_effect = _query
        _patch(monkeypatch, service)

        out = await gsc_get_advanced_search_analytics(
            site_url="sc-domain:example.com",
            filter_dimension="query",
            filter_operator="contains",
            filter_expression="chaser",
        )
        # The filter should reach the API request body.
        assert captured["body"]["dimensionFilterGroups"] == [
            {"filters": [{
                "dimension": "query",
                "operator": "contains",
                "expression": "chaser",
            }]}
        ]
        # And on an empty-rows path, the "No results" message includes
        # the filter description.
        assert "chaser" in out


# =============================================================================
# gsc_compare_search_periods
# =============================================================================


class TestCompareSearchPeriodsMigration:
    async def test_markdown_golden_shape(self, monkeypatch):
        period1 = [
            {"keys": ["chaser"], "clicks": 648, "impressions": 30000, "ctr": 0.02, "position": 6.6},
        ]
        period2 = [
            {"keys": ["chaser"], "clicks": 691, "impressions": 31000, "ctr": 0.022, "position": 6.3},
        ]
        _patch(monkeypatch, _mock_two_period_service(period1, period2))
        out = await gsc_compare_search_periods(
            site_url="sc-domain:example.com",
            period1_start="2026-02-21",
            period1_end="2026-03-20",
            period2_start="2026-03-21",
            period2_end="2026-04-17",
            dimensions="query",
            limit=10,
        )
        assert isinstance(out, str)
        # Compare-specific columns.
        assert "Query | P1 Clicks | P2 Clicks | Change | % | P1 Pos | P2 Pos | Pos Δ" in out
        # signed_int on click_diff, signed_float on pos_diff.
        assert "chaser | 648 | 691 | +43 | " in out
        assert " | +0.3" in out  # pos_diff = 6.6 - 6.3 = +0.3

    async def test_truncation_fires_when_matched_gt_limit(self, monkeypatch):
        """Review issue 4 regression guard: silent truncation was
        previously hardcoded to False."""
        period1 = [
            {"keys": [f"kw {i}"], "clicks": 10, "impressions": 100, "ctr": 0.1, "position": 1.0}
            for i in range(15)
        ]
        period2 = [
            {"keys": [f"kw {i}"], "clicks": 20, "impressions": 100, "ctr": 0.2, "position": 1.0}
            for i in range(15)
        ]
        _patch(monkeypatch, _mock_two_period_service(period1, period2))
        out = await gsc_compare_search_periods(
            site_url="sc-domain:example.com",
            period1_start="2026-01-01",
            period1_end="2026-01-31",
            period2_start="2026-02-01",
            period2_end="2026-02-28",
            limit=10,
        )
        # 15 matched queries but only 10 requested → user must see a warning.
        assert out.startswith("⚠ TRUNCATED:")
        assert "15 matched" in out.splitlines()[0]

    async def test_no_truncation_when_matched_le_limit(self, monkeypatch):
        period1 = [
            {"keys": ["only"], "clicks": 10, "impressions": 100, "ctr": 0.1, "position": 1.0},
        ]
        period2 = [
            {"keys": ["only"], "clicks": 20, "impressions": 100, "ctr": 0.2, "position": 1.0},
        ]
        _patch(monkeypatch, _mock_two_period_service(period1, period2))
        out = await gsc_compare_search_periods(
            site_url="sc-domain:example.com",
            period1_start="2026-01-01",
            period1_end="2026-01-31",
            period2_start="2026-02-01",
            period2_end="2026-02-28",
            limit=10,
        )
        assert "⚠ TRUNCATED" not in out

    async def test_json_mode_exposes_total_matched_and_truncated(self, monkeypatch):
        period1 = [
            {"keys": [f"kw {i}"], "clicks": 10, "impressions": 100, "ctr": 0.1, "position": 1.0}
            for i in range(25)
        ]
        period2 = [
            {"keys": [f"kw {i}"], "clicks": 20, "impressions": 100, "ctr": 0.2, "position": 1.0}
            for i in range(25)
        ]
        _patch(monkeypatch, _mock_two_period_service(period1, period2))
        out = await gsc_compare_search_periods(
            site_url="sc-domain:example.com",
            period1_start="2026-01-01",
            period1_end="2026-01-31",
            period2_start="2026-02-01",
            period2_end="2026-02-28",
            limit=10,
            response_format="json",
        )
        assert out["truncated"] is True
        assert out["meta"]["total_matched"] == 25
        assert out["meta"]["limit"] == 10

    async def test_clicks_pct_is_float_ratio(self, monkeypatch):
        """F3 regression guard: `clicks_pct` (renamed from `click_pct`)
        is a raw float ratio (-0.5 = -50%), not a pre-formatted string.
        Consumers format for display; the "pct" column type handles
        markdown/CSV rendering."""
        # 100 → 50 clicks = -0.5 ratio (-50%)
        period1 = [
            {"keys": ["kw"], "clicks": 100, "impressions": 1000, "ctr": 0.1, "position": 3.0},
        ]
        period2 = [
            {"keys": ["kw"], "clicks": 50, "impressions": 1000, "ctr": 0.05, "position": 3.0},
        ]
        _patch(monkeypatch, _mock_two_period_service(period1, period2))
        out = await gsc_compare_search_periods(
            site_url="sc-domain:example.com",
            period1_start="2026-01-01",
            period1_end="2026-01-31",
            period2_start="2026-02-01",
            period2_end="2026-02-28",
            response_format="json",
        )
        row = out["rows"][0]
        assert "click_pct" not in row  # legacy field removed
        assert row["clicks_pct"] == pytest.approx(-0.5)
        assert isinstance(row["clicks_pct"], float)

    async def test_clicks_pct_null_when_p1_is_zero(self, monkeypatch):
        """F3 companion: clicks_pct is None (not 'N/A' string) when
        the denominator (p1 clicks) is zero."""
        period1 = [
            {"keys": ["emerging"], "clicks": 0, "impressions": 50, "ctr": 0.0, "position": 8.0},
        ]
        period2 = [
            {"keys": ["emerging"], "clicks": 5, "impressions": 60, "ctr": 0.083, "position": 7.0},
        ]
        _patch(monkeypatch, _mock_two_period_service(period1, period2))
        out = await gsc_compare_search_periods(
            site_url="sc-domain:example.com",
            period1_start="2026-01-01",
            period1_end="2026-01-31",
            period2_start="2026-02-01",
            period2_end="2026-02-28",
            response_format="json",
        )
        assert out["rows"][0]["clicks_pct"] is None

    async def test_absent_side_positions_are_null(self, monkeypatch):
        """F4 regression guard: position 0 is not a valid GSC rank.
        When a query exists in one period but not the other, the
        missing side must report null, not a sentinel 0 that naive
        consumers will read as "rank 1"."""
        period1 = [
            {"keys": ["only-in-p1"], "clicks": 5, "impressions": 100, "ctr": 0.05, "position": 4.2},
        ]
        period2 = [
            {"keys": ["only-in-p2"], "clicks": 3, "impressions": 80, "ctr": 0.0375, "position": 7.1},
        ]
        _patch(monkeypatch, _mock_two_period_service(period1, period2))
        out = await gsc_compare_search_periods(
            site_url="sc-domain:example.com",
            period1_start="2026-01-01",
            period1_end="2026-01-31",
            period2_start="2026-02-01",
            period2_end="2026-02-28",
            response_format="json",
        )
        rows_by_query = {r["query"]: r for r in out["rows"]}

        p1_only = rows_by_query["only-in-p1"]
        assert p1_only["p1_position"] == 4.2
        assert p1_only["p2_position"] is None
        assert p1_only["pos_diff"] is None
        assert p1_only["p2_clicks"] == 0

        p2_only = rows_by_query["only-in-p2"]
        assert p2_only["p1_position"] is None
        assert p2_only["p2_position"] == 7.1
        assert p2_only["pos_diff"] is None
        assert p2_only["p1_clicks"] == 0

    async def test_upstream_row_limit_flows_through(self, monkeypatch):
        """A.8 invariant — upstream_row_limit reaches the API request."""
        captured_bodies: list = []
        service = MagicMock()

        def _query(*, siteUrl, body):
            captured_bodies.append(body)
            req = MagicMock()
            req.execute.return_value = {"rows": []}
            return req

        service.searchanalytics.return_value.query.side_effect = _query
        _patch(monkeypatch, service)

        await gsc_compare_search_periods(
            site_url="sc-domain:example.com",
            period1_start="2026-01-01",
            period1_end="2026-01-31",
            period2_start="2026-02-01",
            period2_end="2026-02-28",
            upstream_row_limit=250,
        )
        assert len(captured_bodies) == 2
        assert captured_bodies[0]["rowLimit"] == 250
        assert captured_bodies[1]["rowLimit"] == 250


# =============================================================================
# Error-path behaviour through B.4 envelopes
# =============================================================================


class TestAnalyticsErrorEnvelopes:
    async def test_generic_exception_in_markdown_mode_returns_hint_string(self, monkeypatch):
        service = MagicMock()
        service.searchanalytics.side_effect = RuntimeError("network flakey")
        _patch(monkeypatch, service)
        out = await gsc_get_search_analytics(site_url="sc-domain:example.com")
        assert isinstance(out, str)
        assert out.startswith("Error: RuntimeError: network flakey")
        assert "Hint:" in out

    async def test_generic_exception_in_json_mode_returns_envelope_dict(self, monkeypatch):
        service = MagicMock()
        service.searchanalytics.side_effect = RuntimeError("bad")
        _patch(monkeypatch, service)
        out = await gsc_get_search_analytics(
            site_url="sc-domain:example.com", response_format="json"
        )
        assert isinstance(out, dict)
        assert out["ok"] is False
        assert out["tool"] == "gsc_get_search_analytics"
        assert "RuntimeError" in out["error"]


# =============================================================================
# B.5 boundary — row_limit exactly at the summary threshold
# =============================================================================


class TestPageQuerySummaryBoundary:
    """At exactly row_limit=50 the summary is omitted; at 51 it's
    included. Review flagged this as load-bearing — test the boundary."""

    async def test_row_limit_50_omits_summary_by_default(self, monkeypatch):
        from gsc_server import gsc_get_search_by_page_query
        rows = [
            {"keys": ["kw a"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 1.0},
        ]
        _patch(monkeypatch, _mock_analytics_service(rows))
        out = await gsc_get_search_by_page_query(
            site_url="sc-domain:example.com",
            page_url="https://example.com/",
            row_limit=50,
            response_format="json",
        )
        assert "summary" not in out

    async def test_row_limit_51_includes_summary_by_default(self, monkeypatch):
        from gsc_server import gsc_get_search_by_page_query
        rows = [
            {"keys": ["kw a"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 1.0},
        ]
        _patch(monkeypatch, _mock_analytics_service(rows))
        out = await gsc_get_search_by_page_query(
            site_url="sc-domain:example.com",
            page_url="https://example.com/",
            row_limit=51,
            response_format="json",
        )
        assert "summary" in out

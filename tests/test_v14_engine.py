"""v1.4.0 Search Analytics engine: validation, windows, paging, sorting,
freshness, totals honesty, and the tools built on it (gsc_query and the
migrated analytics tools)."""
from __future__ import annotations

import json
import os
from datetime import date
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

import gsc_server
from gsc_server import (
    gsc_compare_search_periods,
    gsc_get_advanced_search_analytics,
    gsc_get_search_by_page_query,
    gsc_query,
)


def _service(respond):
    """Mock whose searchanalytics().query(body=...) answers ``respond(body)``.
    Every body is recorded on ``service.bodies``."""
    service = MagicMock()
    service.bodies = []

    def _query(*, siteUrl, body):
        service.bodies.append(json.loads(json.dumps(body)))
        req = MagicMock()
        req.execute.return_value = respond(body)
        return req

    service.searchanalytics.return_value.query.side_effect = _query
    return service


def _patch(monkeypatch, service):
    monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)


def _row(key, clicks, impressions, ctr=0.0, position=5.0):
    keys = key if isinstance(key, list) else [key]
    return {"keys": keys, "clicks": clicks, "impressions": impressions, "ctr": ctr, "position": position}


def _main_bodies(service):
    return [b for b in service.bodies if b["dimensions"]]


# ---------------------------------------------------------------------------
# Sorting (acceptance test 2's property) and paging
# ---------------------------------------------------------------------------


class TestSortAndPaging:
    async def test_sort_by_impressions_is_global_not_alphabetical(self, monkeypatch):
        # The API returns clicks order; ties at 0 clicks look alphabetical.
        rows = [
            _row("alpha", 3, 10), _row("beta", 0, 900), _row("gamma", 0, 50), _row("delta", 1, 400),
        ]
        service = _service(lambda body: {"rows": rows} if body["dimensions"] else {"rows": [_row([], 4, 1360)]})
        _patch(monkeypatch, service)
        out = await gsc_query(
            "sc-domain:example.com", "2026-09-01", "2026-09-25",
            dimensions=["query"], sort_by="impressions", row_limit=2,
        )
        assert out["ok"] is True
        imps = [r["impressions"] for r in out["rows"]]
        assert imps == [900, 400]  # true top-2 by impressions, not API order
        assert out["meta"]["sort_applied"] == "client"
        assert out["truncated"] is True and out["meta"]["next_start_row"] == 2
        # Top-N by a non-API order fetches everything first.
        assert _main_bodies(service)[0]["rowLimit"] == 25000
        assert all("orderBy" not in b for b in service.bodies)

    async def test_rows_non_increasing_after_sort(self, monkeypatch):
        rows = [_row(f"q{i}", 10 - i, (i * 37) % 101) for i in range(10)]
        _patch(monkeypatch, _service(lambda b: {"rows": rows}))
        out = await gsc_query(
            "sc-domain:example.com", "2026-09-01", "2026-09-25",
            sort_by="impressions", include_totals=False,
        )
        imps = [r["impressions"] for r in out["rows"]]
        assert imps[0] == max(imps)
        assert all(a >= b for a, b in zip(imps, imps[1:]))

    async def test_fetch_all_pages_until_short_page(self, monkeypatch):
        def respond(body):
            start = body.get("startRow", 0)
            n = 25000 if start == 0 else 7
            return {"rows": [_row(f"q{start + i}", 1, 1) for i in range(n)]}
        service = _service(respond)
        _patch(monkeypatch, service)
        out = await gsc_query(
            "sc-domain:example.com", "2026-09-01", "2026-09-25",
            fetch_all=True, include_totals=False,
        )
        assert out["row_count"] == 25007
        assert out["truncated"] is False
        assert [b.get("startRow", 0) for b in service.bodies] == [0, 25000]

    async def test_fetch_all_stops_at_max_rows(self, monkeypatch):
        service = _service(lambda b: {"rows": [_row(f"q{i}", 1, 1) for i in range(b["rowLimit"])]})
        _patch(monkeypatch, service)
        out = await gsc_query(
            "sc-domain:example.com", "2026-09-01", "2026-09-25",
            fetch_all=True, max_rows=30, include_totals=False,
        )
        assert out["row_count"] == 30
        assert out["truncated"] is True
        assert out["meta"]["next_start_row"] == 30

    async def test_date_grouped_default_keeps_date_order(self, monkeypatch):
        rows = [_row("2026-09-01", 5, 50), _row("2026-09-02", 9, 90)]
        _patch(monkeypatch, _service(lambda b: {"rows": rows}))
        out = await gsc_query("sc-domain:example.com", "2026-09-01", "2026-09-02", dimensions="date")
        assert [r["date"] for r in out["rows"]] == ["2026-09-01", "2026-09-02"]
        assert out["meta"]["sort_applied"] == "api_order"


# ---------------------------------------------------------------------------
# Request body and validation
# ---------------------------------------------------------------------------


class TestRequestBody:
    async def test_regex_and_multi_filter_and_type(self, monkeypatch):
        service = _service(lambda b: {"rows": []})
        _patch(monkeypatch, service)
        await gsc_query(
            "sc-domain:example.com", "2026-09-01", "2026-09-25",
            dimensions=["query", "page"], type="googleNews",
            filter_groups=[{"group_type": "and", "filters": [
                {"dimension": "page", "operator": "equals", "expression": "https://example.com/a"},
                {"dimension": "query", "operator": "includingRegex", "expression": "\\bsage\\b"},
                {"dimension": "searchAppearance", "operator": "equals", "expression": "VIDEO"},
            ]}],
            include_totals=False,
        )
        body = service.bodies[0]
        assert body["type"] == "googleNews"
        assert "searchType" not in body
        assert body["dimensionFilterGroups"] == [{"filters": [
            {"dimension": "page", "operator": "equals", "expression": "https://example.com/a"},
            {"dimension": "query", "operator": "includingRegex", "expression": "\\bsage\\b"},
            {"dimension": "searchAppearance", "operator": "equals", "expression": "VIDEO"},
        ]}]

    async def test_flat_filter_list_is_one_and_group(self, monkeypatch):
        service = _service(lambda b: {"rows": []})
        _patch(monkeypatch, service)
        await gsc_query(
            "sc-domain:example.com", "2026-09-01", "2026-09-25",
            filter_groups=[{"dimension": "query", "operator": "notContains", "expression": "login"}],
            include_totals=False,
        )
        assert service.bodies[0]["dimensionFilterGroups"] == [
            {"filters": [{"dimension": "query", "operator": "notContains", "expression": "login"}]}
        ]

    async def test_data_state_all_is_sent(self, monkeypatch):
        service = _service(lambda b: {"rows": []})
        _patch(monkeypatch, service)
        await gsc_query("sc-domain:example.com", "2026-09-01", "2026-09-28",
                        dimensions="date", data_state="all")
        assert service.bodies[0]["dataState"] == "all"

    @pytest.mark.parametrize("kwargs,needle", [
        ({"filter_groups": [{"dimension": "query", "operator": "includingRegex", "expression": "(?!x)y"}]}, "lookaround"),
        ({"dimensions": ["hour"]}, "hourly_all"),
        ({"dimensions": ["page"], "aggregation_type": "byProperty"}, "cannot aggregate by property"),
        ({"type": "discover", "dimensions": ["page"], "aggregation_type": "byProperty"}, "property"),
        ({"aggregation_type": "byNewsShowcasePanel"}, "byNewsShowcasePanel"),
        ({"dimensions": ["nope"]}, "Unknown dimension"),
        ({"type": "shopping"}, "Unknown type"),
        ({"sort_by": "revenue"}, "sort_by"),
        ({"filter_groups": [{"dimension": "query", "operator": "fuzzy", "expression": "x"}]}, "operator"),
    ])
    async def test_documented_conflicts_rejected_before_any_call(self, monkeypatch, kwargs, needle):
        service = _service(lambda b: {"rows": []})
        _patch(monkeypatch, service)
        out = await gsc_query("sc-domain:example.com", "2026-09-01", "2026-09-25", **kwargs)
        assert out["ok"] is False
        assert out["error_code"] == "BAD_REQUEST"
        assert needle in out["error"]
        assert service.bodies == []

    async def test_start_after_end_rejected(self, monkeypatch):
        _patch(monkeypatch, _service(lambda b: {"rows": []}))
        out = await gsc_query("sc-domain:example.com", "2026-09-20", "2026-09-01")
        assert out["error_code"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# Windows and freshness (acceptance tests 3 and 4)
# ---------------------------------------------------------------------------


class TestWindows:
    async def test_days_means_last_n_final_days(self, monkeypatch):
        service = _service(lambda b: {"rows": [_row("kw", 1, 10)]})
        _patch(monkeypatch, service)
        out = await gsc_get_advanced_search_analytics(
            "sc-domain:example.com", response_format="json",
        )
        meta = out["meta"]
        assert (meta["start_date"], meta["end_date"], meta["window_days"]) == ("2026-08-29", "2026-09-25", 28)
        assert meta["latest_final_date"] == "2026-09-25"
        assert meta["data_state"] == "final"
        assert "Pacific" in meta["timezone"]
        assert meta["server_version"]

    async def test_same_window_same_totals_across_tools(self, monkeypatch):
        service = _service(lambda b: {"rows": [_row("kw", 3, 30)]} if b["dimensions"] else {"rows": [_row([], 5, 70)]})
        _patch(monkeypatch, service)
        a = await gsc_get_search_by_page_query(
            "sc-domain:example.com", "https://example.com/p", days=28, response_format="json",
        )
        b = await gsc_query(
            "sc-domain:example.com", a["meta"]["start_date"], a["meta"]["end_date"],
            filter_groups=[{"dimension": "page", "operator": "equals", "expression": "https://example.com/p"}],
        )
        assert a["meta"]["page_total"] == b["meta"]["page_total"] == {"clicks": 5, "impressions": 70}

    async def test_data_state_all_ends_today_and_lists_non_final_days(self, monkeypatch):
        _patch(monkeypatch, _service(lambda b: {"rows": [_row("kw", 1, 1)]}))
        out = await gsc_get_advanced_search_analytics(
            "sc-domain:example.com", response_format="json", data_state="all",
        )
        assert out["meta"]["end_date"] == "2026-09-28"
        assert out["meta"]["non_final_days"] == ["2026-09-26", "2026-09-27", "2026-09-28"]

    async def test_explicit_end_past_final_warns(self, monkeypatch):
        _patch(monkeypatch, _service(lambda b: {"rows": [_row("kw", 1, 1)]}))
        out = await gsc_query("sc-domain:example.com", "2026-09-20", "2026-09-28", include_totals=False)
        assert any("latest final date" in w for w in out["meta"]["warnings"])

    async def test_preliminary_rows_flagged(self, monkeypatch):
        rows = [_row("2026-09-25", 1, 1), _row("2026-09-26", 1, 1), _row("2026-09-27", 1, 1)]
        _patch(monkeypatch, _service(lambda b: {
            "rows": rows, "metadata": {"first_incomplete_date": "2026-09-26"},
        }))
        out = await gsc_query("sc-domain:example.com", "2026-09-25", "2026-09-27",
                              dimensions="date", data_state="all")
        assert out["meta"]["first_incomplete_date"] == "2026-09-26"
        assert [r["preliminary"] for r in out["rows"]] == [False, True, True]
        assert "preliminary" in out["columns"]

    async def test_comparison_days_windows_equal_and_adjacent(self, monkeypatch):
        service = _service(lambda b: {"rows": [_row("kw", 1, 1)]})
        _patch(monkeypatch, service)
        out = await gsc_compare_search_periods("sc-domain:example.com", days=7, response_format="json")
        p1, p2 = out["meta"]["period1"], out["meta"]["period2"]
        assert (p2["start"], p2["end"]) == ("2026-09-19", "2026-09-25")
        assert (p1["start"], p1["end"]) == ("2026-09-12", "2026-09-18")

    async def test_comparison_needs_dates_or_days(self, monkeypatch):
        _patch(monkeypatch, _service(lambda b: {"rows": []}))
        out = await gsc_compare_search_periods("sc-domain:example.com", response_format="json")
        assert out["error_code"] == "BAD_REQUEST"


@pytest.mark.real_freshness
class TestLatestFinalDateProbe:
    async def _latest(self, service):
        ctx = gsc_server._SaContext("sc-domain:example.com", None, "test", service)
        return await gsc_server._latest_final_date(ctx)

    async def test_from_metadata(self):
        service = _service(lambda b: {"rows": [], "metadata": {"first_incomplete_date": "2026-09-26"}})
        got = await self._latest(service)
        assert got == {"date": "2026-09-25", "source": "metadata", "warning": None}
        assert service.bodies[0]["dataState"] == "all"

    async def test_camel_case_metadata_accepted(self):
        service = _service(lambda b: {"metadata": {"firstIncompleteDate": "2026-09-26"}})
        assert (await self._latest(service))["date"] == "2026-09-25"

    async def test_populated_fallback_then_cached(self):
        service = _service(lambda b: {"rows": [] if b.get("dataState") else [_row("2026-09-20", 1, 1), _row("2026-09-24", 1, 1)]})
        got = await self._latest(service)
        assert (got["date"], got["source"]) == ("2026-09-24", "populated")
        calls = len(service.bodies)
        await self._latest(service)
        assert len(service.bodies) == calls  # cached

    async def test_empty_property_falls_back_with_warning(self):
        got = await self._latest(_service(lambda b: {}))
        assert (got["date"], got["source"]) == ("2026-09-25", "fallback")
        assert got["warning"]


# ---------------------------------------------------------------------------
# Totals honesty (§2.3, acceptance test 9)
# ---------------------------------------------------------------------------


class TestTotalsHonesty:
    async def test_unattributed_share_and_aggregation_passthrough(self, monkeypatch):
        def respond(body):
            if body["dimensions"]:
                return {"rows": [_row("a", 10, 100), _row("b", 5, 200)], "responseAggregationType": "byProperty"}
            return {"rows": [_row([], 20, 500)]}
        service = _service(respond)
        _patch(monkeypatch, service)
        out = await gsc_query("sc-domain:example.com", "2026-09-01", "2026-09-25")
        meta = out["meta"]
        assert meta["page_total"] == {"clicks": 20, "impressions": 500}
        assert meta["query_rows_sum"] == {"clicks": 15, "impressions": 300}
        assert meta["unattributed_share"]["clicks"] == pytest.approx(0.25)
        assert meta["unattributed_share"]["impressions"] == pytest.approx(0.4)
        assert meta["query_rows_sum_scope"] == "all_rows"
        assert meta["aggregation_type"] == "byProperty"
        totals_body = service.bodies[-1]
        assert totals_body["dimensions"] == [] and totals_body["aggregationType"] == "byProperty"

    async def test_zero_denominator_is_null(self, monkeypatch):
        _patch(monkeypatch, _service(lambda b: {"rows": [_row("a", 0, 0)]} if b["dimensions"] else {}))
        out = await gsc_query("sc-domain:example.com", "2026-09-01", "2026-09-25")
        assert out["meta"]["unattributed_share"] == {"clicks": None, "impressions": None}

    async def test_out_of_range_share_warns_not_clamps(self, monkeypatch):
        _patch(monkeypatch, _service(
            lambda b: {"rows": [_row("a", 30, 300)]} if b["dimensions"] else {"rows": [_row([], 10, 100)]}
        ))
        out = await gsc_query("sc-domain:example.com", "2026-09-01", "2026-09-25")
        assert out["meta"]["unattributed_share"]["clicks"] == pytest.approx(-2.0)
        assert any("outside [0, 1]" in w for w in out["meta"]["warnings"])


# ---------------------------------------------------------------------------
# Comparison completeness
# ---------------------------------------------------------------------------


class TestComparisonCompleteness:
    async def test_missing_row_from_truncated_period_is_null(self, monkeypatch):
        calls = {"n": 0}

        def respond(body):
            calls["n"] += 1
            if calls["n"] == 1:  # period 1 hits the cap of 2 rows
                return {"rows": [_row("a", 5, 50), _row("b", 4, 40)]}
            return {"rows": [_row("c", 9, 90)]}  # under the cap: complete
        _patch(monkeypatch, _service(respond))
        out = await gsc_compare_search_periods(
            "sc-domain:example.com", "2026-08-01", "2026-08-31", "2026-09-01", "2026-09-25",
            upstream_row_limit=2, response_format="json",
        )
        by_q = {r["query"]: r for r in out["rows"]}
        assert by_q["c"]["p1_clicks"] is None  # unknown, not 0: period 1 was capped
        assert by_q["c"]["click_diff"] is None
        assert by_q["b"]["p2_clicks"] == 0     # period 2 was complete
        assert out["meta"]["coverage"] == {"p1": "truncated", "p2": "complete"}


# ---------------------------------------------------------------------------
# save_to_file
# ---------------------------------------------------------------------------


class TestSaveToFile:
    async def test_writes_all_rows_returns_summary(self, monkeypatch, tmp_path):
        rows = [_row(f"q{i}", i, i * 10) for i in range(30)]
        _patch(monkeypatch, _service(lambda b: {"rows": rows}))
        path = tmp_path / "out.json"
        out = await gsc_query("sc-domain:example.com", "2026-09-01", "2026-09-25",
                              save_to_file=str(path), include_totals=False)
        assert out["row_count"] == 20
        assert out["meta"]["saved_row_count"] == 30
        assert out["meta"]["saved_totals"]["clicks"] == sum(range(30))
        saved = json.loads(path.read_text())
        assert len(saved["rows"]) == 30 and saved["meta"]["start_date"] == "2026-09-01"

    async def test_csv_file_keeps_raw_operator_strings(self, monkeypatch, tmp_path):
        _patch(monkeypatch, _service(lambda b: {"rows": [_row("+site.example.com pricing", 0, 9)]}))
        path = tmp_path / "out.csv"
        await gsc_query("sc-domain:example.com", "2026-09-01", "2026-09-25",
                        save_to_file=str(path), include_totals=False)
        lines = path.read_text().splitlines()
        assert lines[0] == "query,clicks,impressions,ctr,position"
        assert lines[1].startswith("+site.example.com pricing,0,9")

    @pytest.mark.parametrize("bad", ["relative/out.csv", "/tmp/out.txt"])
    async def test_rejects_bad_paths(self, monkeypatch, bad):
        _patch(monkeypatch, _service(lambda b: {"rows": [_row("a", 1, 1)]}))
        out = await gsc_query("sc-domain:example.com", "2026-09-01", "2026-09-25",
                              save_to_file=bad, include_totals=False)
        assert out["ok"] is False and out["error_code"] == "BAD_REQUEST"

    async def test_rejects_symlink(self, monkeypatch, tmp_path):
        target = tmp_path / "real.csv"
        target.write_text("")
        link = tmp_path / "link.csv"
        os.symlink(target, link)
        _patch(monkeypatch, _service(lambda b: {"rows": [_row("a", 1, 1)]}))
        out = await gsc_query("sc-domain:example.com", "2026-09-01", "2026-09-25",
                              save_to_file=str(link), include_totals=False)
        assert out["ok"] is False and "symlink" in out["error"]


# ---------------------------------------------------------------------------
# Review fixes (open-gem pass 1 on Release A)
# ---------------------------------------------------------------------------


class TestReviewFixes:
    async def test_save_to_file_writes_beyond_row_limit(self, monkeypatch, tmp_path):
        rows = [_row(f"q{i}", 1, 1) for i in range(40)]
        _patch(monkeypatch, _service(lambda b: {"rows": rows}))
        path = tmp_path / "all.json"
        out = await gsc_get_advanced_search_analytics(
            "sc-domain:example.com", row_limit=10, save_to_file=str(path), response_format="json",
        )
        assert out["meta"]["saved_row_count"] == 40
        assert out["meta"]["saved_truncated"] is False
        assert len(json.loads(path.read_text())["rows"]) == 40

    async def test_empty_json_is_a_table_with_meta(self, monkeypatch):
        _patch(monkeypatch, _service(lambda b: {"rows": []}))
        out = await gsc_get_advanced_search_analytics("sc-domain:example.com", response_format="json")
        assert out["ok"] is True and out["rows"] == []
        assert out["meta"]["latest_final_date"] == "2026-09-25"

    async def test_page_query_empty_markdown_shows_page_total(self, monkeypatch):
        _patch(monkeypatch, _service(
            lambda b: {"rows": []} if b["dimensions"] else {"rows": [_row([], 3, 120)]}
        ))
        out = await gsc_get_search_by_page_query("sc-domain:example.com", "https://example.com/p")
        assert "PAGE TOTAL (query-less, incl. anonymised) | 3 | 120" in out

    async def test_offset_page_sum_not_labelled_all_rows(self, monkeypatch):
        _patch(monkeypatch, _service(
            lambda b: {"rows": [_row("a", 1, 1)]} if b["dimensions"] else {"rows": [_row([], 9, 9)]}
        ))
        out = await gsc_query("sc-domain:example.com", "2026-09-01", "2026-09-25", start_row=100)
        assert out["meta"]["query_rows_sum_scope"].startswith("rows 100")

    async def test_comparison_explicit_dates_win_over_days(self, monkeypatch):
        _patch(monkeypatch, _service(lambda b: {"rows": [_row("kw", 1, 1)]}))
        out = await gsc_compare_search_periods(
            "sc-domain:example.com", "2026-08-01", "2026-08-07", "2026-08-08", "2026-08-14",
            days=7, response_format="json",
        )
        assert out["meta"]["period2"]["end"] == "2026-08-14"
        assert any("days ignored" in w for w in out["meta"]["warnings"])

    async def test_comparison_partial_dates_rejected(self, monkeypatch):
        _patch(monkeypatch, _service(lambda b: {"rows": []}))
        out = await gsc_compare_search_periods(
            "sc-domain:example.com", "2026-08-01", days=7, response_format="json",
        )
        assert out["error_code"] == "BAD_REQUEST" and "partial" in out["error"]

    async def test_comparison_reports_totals_per_period(self, monkeypatch):
        _patch(monkeypatch, _service(
            lambda b: {"rows": [_row("kw", 1, 10)]} if b["dimensions"] else {"rows": [_row([], 4, 40)]}
        ))
        out = await gsc_compare_search_periods("sc-domain:example.com", days=7, response_format="json")
        t = out["meta"]["period1"]["totals"]
        assert t["page_total"] == {"clicks": 4, "impressions": 40}
        assert t["unattributed_share"]["clicks"] == pytest.approx(0.75)

    async def test_empty_overview_and_comparison_json_are_structured(self, monkeypatch):
        _patch(monkeypatch, _service(lambda b: {"rows": []}))
        ov = await gsc_server.gsc_get_performance_overview("sc-domain:example.com", response_format="json")
        assert ov["ok"] is True and ov["rows"] == [] and ov["meta"]["window_days"] == 28
        cmp_ = await gsc_compare_search_periods("sc-domain:example.com", days=7, response_format="json")
        assert cmp_["ok"] is True and cmp_["rows"] == []

    async def test_save_to_file_written_even_when_empty(self, monkeypatch, tmp_path):
        _patch(monkeypatch, _service(lambda b: {"rows": []}))
        path = tmp_path / "empty.csv"
        await gsc_server.gsc_get_search_analytics("sc-domain:example.com", save_to_file=str(path))
        assert path.read_text().splitlines() == ["query,clicks,impressions,ctr,position"]

    async def test_preliminary_column_in_convenience_tools(self, monkeypatch):
        _patch(monkeypatch, _service(lambda b: {
            "rows": [_row("2026-09-27", 1, 1)], "metadata": {"first_incomplete_date": "2026-09-26"},
        }))
        out = await gsc_get_advanced_search_analytics(
            "sc-domain:example.com", dimensions="date", data_state="all", response_format="csv",
        )
        assert "Preliminary" in out and "2026-09-27,1,1,0.0,5.0,True" in out

"""Tests for search-analytics tools — specifically the gsc_get_search_by_page_query
row_limit enhancement and opt-in structured response mode (v0.5.0).

Covers:
- Markdown mode backward compatibility (default, byte-for-byte vs pre-0.5).
- Markdown mode row_limit passthrough and error paths.
- JSON mode row_limit passthrough, clamping (default 20, upper 25000, lower 1),
  days clamping, summary aggregates, possibly_truncated flag, empty/defensive
  cases, HttpError and generic-exception paths.
- response_format normalization (case-insensitive, whitespace-tolerant).

Mocks `gsc_server.get_gsc_service` with a MagicMock that captures the
request body passed to searchanalytics().query(...) so tests can assert
on what was sent to the API.

The golden-string test in TestMarkdownModeBackwardCompat locks the default
markdown output against accidental drift. If it fails, compare against
`git show HEAD:gsc_server.py` for the source of truth.
"""
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

import gsc_server
from gsc_server import gsc_get_search_by_page_query


def _build_mock_service(rows, captured):
    """Return a MagicMock shaped like the GSC service client.

    `rows` is the list placed into the mocked query response, or None to
    simulate a response body with no `rows` key at all.
    `captured` is a dict that will have `body` and `siteUrl` keys populated
    when the test calls `.searchanalytics().query(siteUrl=..., body=...)`.
    """
    service = MagicMock()

    def _query(*, siteUrl, body):
        captured["siteUrl"] = siteUrl
        captured["body"] = body
        request = MagicMock()
        request.execute.return_value = {"rows": rows} if rows is not None else {}
        return request

    service.searchanalytics.return_value.query.side_effect = _query
    return service


def _patch_service(monkeypatch, service):
    monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)


# =============================================================================
# Markdown mode — backward compatibility lock
# =============================================================================


class TestMarkdownModeBackwardCompat:
    async def test_default_returns_str_not_dict(self, monkeypatch):
        """With no new args, successful calls must return str, not dict."""
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service([], captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
        )

        assert isinstance(result, str)
        assert captured["body"]["rowLimit"] == 20

    async def test_markdown_default_golden_byte_for_byte(self, monkeypatch):
        """The default markdown output must match pre-0.5 byte-for-byte.

        Rows use the numeric types GSC actually returns (floats for
        clicks/impressions, because the API returns these as Number). If
        this test fails, compare to `git show HEAD:gsc_server.py` lines
        1597-1668 (or whatever the pre-0.5 function was) — the rendering
        loop must match exactly.
        """
        rows = [
            {"keys": ["seo services"], "clicks": 10.0, "impressions": 100.0, "ctr": 0.1, "position": 3.0},
            {"keys": ["seo agency"], "clicks": 20.0, "impressions": 500.0, "ctr": 0.04, "position": 5.0},
            {"keys": ["local seo"], "clicks": 12.0, "impressions": 400.0, "ctr": 0.03, "position": 10.0},
        ]
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service(rows, captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
        )

        # Hand-constructed to match pre-0.5 rendering exactly:
        # - raw float clicks/impressions in data rows ("10.0" not "10")
        # - float sums in TOTAL row ("42.0", "1000.0")
        # - avg_ctr_pct = 42.0/1000.0*100 = 4.2 → "4.20%"
        # - TOTAL position shown as "-"
        dash = "-" * 80
        expected = (
            "Search queries for page https://example.com/foo (last 28 days):"
            "\n\n" + dash + "\n"
            "\nQuery | Clicks | Impressions | CTR | Position"
            "\n" + dash +
            "\nseo services | 10.0 | 100.0 | 10.00% | 3.0"
            "\nseo agency | 20.0 | 500.0 | 4.00% | 5.0"
            "\nlocal seo | 12.0 | 400.0 | 3.00% | 10.0"
            "\n" + dash +
            "\nTOTAL | 42.0 | 1000.0 | 4.20% | -"
        )
        assert result == expected

    async def test_markdown_empty_returns_exact_no_data_message(self, monkeypatch):
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service([], captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
        )

        assert result == "No search data found for page https://example.com/foo in the last 28 days."

    async def test_markdown_unknown_default_for_missing_keys(self, monkeypatch):
        """Pre-0.5 used row.get('keys', ['Unknown'])[0]. Markdown mode must
        preserve that fallback, not switch to empty string."""
        rows = [
            {"clicks": 5, "impressions": 50, "ctr": 0.1, "position": 2.0},
        ]
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service(rows, captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
        )

        assert isinstance(result, str)
        assert "\nUnknown | 5 | 50 | 10.00% | 2.0" in result

    async def test_markdown_long_query_truncated_to_100_chars(self, monkeypatch):
        long_query = "a" * 150
        rows = [
            {"keys": [long_query], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 1.0},
        ]
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service(rows, captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
        )

        # The rendered row should contain exactly 100 'a' chars, not 150.
        assert ("a" * 100 + " | 1 |") in result
        assert ("a" * 101) not in result

    async def test_markdown_respects_row_limit_param(self, monkeypatch):
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service([], captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            row_limit=500,
        )

        assert isinstance(result, str)
        assert captured["body"]["rowLimit"] == 500
        # No appended footer/info line — markdown mode stays clean.
        assert "row_limit=" not in result
        assert "rows_returned=" not in result

    async def test_markdown_generic_exception_returns_string(self, monkeypatch):
        def _explode():
            raise RuntimeError("boom")

        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
        )

        assert result == "Error retrieving page query data: boom"

    async def test_markdown_http_error_returns_string(self, monkeypatch):
        error_body = b'{"error": {"message": "Invalid site URL", "code": 400}}'
        resp = MagicMock()
        resp.status = 400
        resp.reason = "Bad Request"
        http_error = HttpError(resp=resp, content=error_body)

        service = MagicMock()
        service.searchanalytics.return_value.query.return_value.execute.side_effect = http_error
        _patch_service(monkeypatch, service)

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
        )

        # Markdown mode preserves the pre-0.5 error format, which uses
        # str(e) on the raw HttpError. Don't lock the exact message (HttpError
        # repr is not worth pinning) — just the prefix and type.
        assert isinstance(result, str)
        assert result.startswith("Error retrieving page query data: ")

    async def test_markdown_invalid_days_type_returns_error_string(self, monkeypatch):
        """int('abc') inside try block must be caught and return the
        pre-0.5 error string format."""
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service([], captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            days="abc",  # type: ignore[arg-type]
        )

        assert isinstance(result, str)
        assert result.startswith("Error retrieving page query data: ")
        assert "invalid literal" in result


# =============================================================================
# response_format validation and normalization
# =============================================================================


class TestResponseFormatValidation:
    async def test_invalid_response_format_returns_string_error(self, monkeypatch):
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service([], captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            response_format="xml",
        )

        assert isinstance(result, str)
        assert "response_format must be 'markdown' or 'json'" in result
        assert "'xml'" in result

    async def test_response_format_normalized_whitespace_and_case(self, monkeypatch):
        """' JSON ' should be accepted as opt-in to json mode."""
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service([], captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            response_format=" JSON ",
        )

        assert isinstance(result, dict)
        assert result["ok"] is True


# =============================================================================
# JSON mode — row_limit passthrough and clamping
# =============================================================================


class TestJsonRowLimitPassthrough:
    async def test_row_limit_default_is_20(self, monkeypatch):
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service([], captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            response_format="json",
        )

        assert result["ok"] is True
        assert result["row_limit"] == 20
        assert captured["body"]["rowLimit"] == 20

    async def test_row_limit_explicit_passthrough(self, monkeypatch):
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service([], captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            row_limit=500,
            response_format="json",
        )

        assert result["ok"] is True
        assert result["row_limit"] == 500
        assert captured["body"]["rowLimit"] == 500

    async def test_row_limit_upper_cap_at_25000(self, monkeypatch):
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service([], captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            row_limit=50_000,
            response_format="json",
        )

        assert result["row_limit"] == 25_000
        assert captured["body"]["rowLimit"] == 25_000

    async def test_row_limit_lower_cap_zero_becomes_one(self, monkeypatch):
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service([], captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            row_limit=0,
            response_format="json",
        )

        assert result["row_limit"] == 1
        assert captured["body"]["rowLimit"] == 1

    async def test_row_limit_lower_cap_negative_becomes_one(self, monkeypatch):
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service([], captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            row_limit=-5,
            response_format="json",
        )

        assert result["row_limit"] == 1
        assert captured["body"]["rowLimit"] == 1


class TestJsonDaysClamping:
    async def test_days_zero_becomes_one(self, monkeypatch):
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service([], captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            days=0,
            response_format="json",
        )

        assert result["days"] == 1
        # startDate one day before endDate.
        from datetime import datetime as dt
        start = captured["body"]["startDate"]
        end = captured["body"]["endDate"]
        start_d = dt.strptime(start, "%Y-%m-%d").date()
        end_d = dt.strptime(end, "%Y-%m-%d").date()
        assert (end_d - start_d).days == 1


# =============================================================================
# JSON mode — summary aggregates and possibly_truncated
# =============================================================================


class TestJsonSummaryAggregates:
    async def test_weighted_average_position_and_totals(self, monkeypatch):
        """Hand-calculated expectation:
        clicks   = [10, 20, 12]         -> total_clicks = 42
        impr     = [100, 500, 400]      -> total_impressions = 1000
        position = [3.0, 5.0, 10.0]
        avg_ctr  = 42 / 1000 = 0.042
        avg_pos  = (3*100 + 5*500 + 10*400) / 1000 = 6.8
        """
        rows = [
            {"keys": ["kw a"], "clicks": 10, "impressions": 100, "ctr": 0.10, "position": 3.0},
            {"keys": ["kw b"], "clicks": 20, "impressions": 500, "ctr": 0.04, "position": 5.0},
            {"keys": ["kw c"], "clicks": 12, "impressions": 400, "ctr": 0.03, "position": 10.0},
        ]
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service(rows, captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            row_limit=500,
            response_format="json",
        )

        assert result["ok"] is True
        assert result["total_rows_returned"] == 3
        assert result["possibly_truncated"] is False  # 3 < 500
        assert len(result["queries"]) == 3
        assert result["queries"][0]["query"] == "kw a"
        assert result["queries"][2]["position"] == 10.0

        summary = result["summary"]
        assert summary["total_clicks"] == 42
        assert summary["total_impressions"] == 1000
        assert summary["average_ctr"] == pytest.approx(0.042)
        assert summary["average_position"] == pytest.approx(6.8)


class TestJsonPossiblyTruncated:
    async def test_possibly_truncated_true_when_rows_equal_limit(self, monkeypatch):
        rows = [
            {"keys": [f"kw {i}"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 1.0}
            for i in range(20)
        ]
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service(rows, captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            response_format="json",
        )

        assert result["ok"] is True
        assert result["total_rows_returned"] == 20
        assert result["row_limit"] == 20
        assert result["possibly_truncated"] is True

    async def test_possibly_truncated_false_when_rows_below_limit(self, monkeypatch):
        rows = [
            {"keys": ["kw a"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 1.0},
        ]
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service(rows, captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            response_format="json",
        )

        assert result["possibly_truncated"] is False


# =============================================================================
# B.5 — include_summary auto-decision (JSON mode only)
# =============================================================================


class TestSummaryAutoInclude:
    """At row_limit <= 50 the summary aggregates would be misleading
    (only across returned rows). B.5 suppresses the block by default
    at that cap and includes it at higher row counts.
    """

    async def test_summary_omitted_by_default_at_low_row_limit(self, monkeypatch):
        rows = [
            {"keys": ["kw a"], "clicks": 10, "impressions": 100, "ctr": 0.10, "position": 3.0},
        ]
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service(rows, captured))
        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            row_limit=20,  # <= 50 threshold
            response_format="json",
        )
        assert result["ok"] is True
        assert "summary" not in result

    async def test_summary_included_by_default_at_high_row_limit(self, monkeypatch):
        rows = [
            {"keys": ["kw a"], "clicks": 10, "impressions": 100, "ctr": 0.10, "position": 3.0},
        ]
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service(rows, captured))
        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            row_limit=100,  # > 50 threshold
            response_format="json",
        )
        assert result["ok"] is True
        assert "summary" in result
        assert result["summary"]["total_clicks"] == 10

    async def test_include_summary_true_overrides_low_row_limit(self, monkeypatch):
        rows = [
            {"keys": ["kw a"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 1.0},
        ]
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service(rows, captured))
        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            row_limit=20,
            response_format="json",
            include_summary=True,
        )
        assert "summary" in result

    async def test_include_summary_false_overrides_high_row_limit(self, monkeypatch):
        rows = [
            {"keys": ["kw a"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 1.0},
        ]
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service(rows, captured))
        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            row_limit=500,
            response_format="json",
            include_summary=False,
        )
        assert "summary" not in result


# =============================================================================
# JSON mode — empty and defensive behaviors
# =============================================================================


class TestJsonEmptyAndDefensive:
    async def test_empty_rows_returns_ok_with_zero_summary(self, monkeypatch):
        # include_summary=True forces the summary block even at the
        # default row_limit=20 (below the B.5 auto-include threshold).
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service([], captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            response_format="json",
            include_summary=True,
        )

        assert result["ok"] is True
        assert result["total_rows_returned"] == 0
        assert result["queries"] == []
        assert result["possibly_truncated"] is False
        assert result["summary"] == {
            "total_clicks": 0,
            "total_impressions": 0,
            "average_position": 0.0,
            "average_ctr": 0.0,
        }

    async def test_response_with_no_rows_key(self, monkeypatch):
        """GSC returns a body with no 'rows' key at all when there's no data."""
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service(None, captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            response_format="json",
        )

        assert result["ok"] is True
        assert result["total_rows_returned"] == 0

    async def test_row_missing_keys_field_becomes_empty_string(self, monkeypatch):
        """JSON mode uses empty-string default (vs markdown's 'Unknown')."""
        rows = [
            {"clicks": 5, "impressions": 50, "ctr": 0.1, "position": 2.0},
        ]
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service(rows, captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            response_format="json",
        )

        assert result["ok"] is True
        assert result["queries"][0]["query"] == ""
        assert result["queries"][0]["clicks"] == 5


# =============================================================================
# JSON mode — error paths
# =============================================================================


class TestJsonErrorPaths:
    async def test_http_error_returns_error_dict_with_parsed_message(self, monkeypatch):
        # Post-B.4 migration: the envelope now includes status-aware
        # prefix ("HTTP 400: ...") and may carry a hint/retry_after.
        # The parsed message still appears in the error field.
        error_body = b'{"error": {"message": "Invalid site URL", "code": 400}}'
        resp = MagicMock()
        resp.status = 400
        resp.reason = "Bad Request"
        resp.get = MagicMock(return_value=None)
        http_error = HttpError(resp=resp, content=error_body)

        service = MagicMock()
        service.searchanalytics.return_value.query.return_value.execute.side_effect = http_error
        _patch_service(monkeypatch, service)

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            response_format="json",
        )

        assert result["ok"] is False
        assert "Invalid site URL" in result["error"]
        assert "HTTP 400" in result["error"]
        assert result["tool"] == "gsc_get_search_by_page_query"

    async def test_generic_exception_returns_error_dict(self, monkeypatch):
        # Post-B.4: generic exceptions surface with the exception type
        # name prefix ("RuntimeError: boom") and a hint field.
        def _explode():
            raise RuntimeError("boom")

        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            response_format="json",
        )

        assert result["ok"] is False
        assert "boom" in result["error"]
        assert "RuntimeError" in result["error"]
        assert result["tool"] == "gsc_get_search_by_page_query"
        # Hint now present thanks to the envelope helpers.
        assert "hint" in result

    async def test_invalid_days_type_returns_error_dict(self, monkeypatch):
        """int('abc') inside try block must be caught and return a dict error
        in json mode (not raise, not return a string)."""
        captured: dict = {}
        _patch_service(monkeypatch, _build_mock_service([], captured))

        result = await gsc_get_search_by_page_query(
            site_url="https://example.com/",
            page_url="https://example.com/foo",
            days="abc",  # type: ignore[arg-type]
            response_format="json",
        )

        assert result["ok"] is False
        assert "invalid literal" in result["error"]
        assert result["tool"] == "gsc_get_search_by_page_query"

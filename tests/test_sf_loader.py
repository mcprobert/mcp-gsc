"""Tests for the Screaming Frog CSV bridge (Add 1).

Covers:
- Loading flat and nested SF export layouts.
- Empty (header-only) CSVs load without rows.
- Column normalization rules (snake_case, alias table, dedupe order).
- Numeric sort auto-coercion (no lex sort on word_count etc).
- Path traversal guard on the dataset param.
- UTF-16LE encoding fallback for Windows SF exports.
- Query filter/sort/projection/pagination behavior.
"""
from pathlib import Path

import pytest

import gsc_server
from gsc_server import (
    _normalize_column,
    _to_float_or_none,
    _extract_snapshot_date,
    _detect_encoding,
    _parse_gsc_date,
    _sort_landing_page_diffs,
    _filter_value_eq,
    _read_account_scopes,
    gsc_load_from_sf_export,
    gsc_query_sf_export,
    gsc_get_landing_page_summary,
    gsc_compare_periods_landing_pages,
    batch_url_inspection,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _reset_sessions():
    """Ensure each test starts with a clean session store."""
    gsc_server._sf_sessions.clear()
    yield
    gsc_server._sf_sessions.clear()


# -------- unit tests on helpers --------

class TestNormalizeColumn:
    def test_basic_lowercase(self):
        assert _normalize_column("Address", {}) == "address"

    def test_status_code_underscores(self):
        assert _normalize_column("Status Code", {}) == "status_code"

    def test_avg_position_alias(self):
        """'Avg. Position' must map to 'position' via the alias table."""
        assert _normalize_column("Avg. Position", {}) == "position"

    def test_h1_dedupe(self):
        seen: dict = {}
        assert _normalize_column("H1-1", seen) == "h1_1"
        assert _normalize_column("H1-2", seen) == "h1_2"

    def test_alias_then_dedupe_order(self):
        """Alias must happen BEFORE dedupe, so 'position' followed by 'Avg. Position'
        produces ['position', 'position_2'] — neither is silently dropped."""
        seen: dict = {}
        a = _normalize_column("Position", seen)
        b = _normalize_column("Avg. Position", seen)
        assert a == "position"
        assert b == "position_2"

    def test_avg_position_then_raw_position(self):
        """Reverse of test_alias_then_dedupe_order: 'Avg. Position' first,
        then 'Position' should also produce ['position', 'position_2']."""
        seen: dict = {}
        a = _normalize_column("Avg. Position", seen)
        b = _normalize_column("Position", seen)
        assert a == "position"
        assert b == "position_2"

    def test_strips_bom(self):
        assert _normalize_column("\ufeffAddress", {}) == "address"

    def test_strips_quotes(self):
        assert _normalize_column('"Address"', {}) == "address"

    def test_drops_non_alphanumeric(self):
        assert _normalize_column("Size (bytes)", {}) == "size_bytes"

    def test_empty_becomes_col(self):
        assert _normalize_column("", {}) == "col"


class TestToFloatOrNone:
    @pytest.mark.parametrize("raw,expected", [
        ("10", 10.0),
        ("2.5", 2.5),
        ("", None),
        ("abc", None),
        (None, None),
        ("100", 100.0),
    ])
    def test_coercion(self, raw, expected):
        assert _to_float_or_none(raw) == expected


class TestFilterValueEq:
    """Regression tests for Fix 5 — numeric-target-only eq coercion.

    The helper must resolve the {"status_code": 200.0} vs cell "200" gotcha
    while preserving string-equality semantics for string targets (so
    "200" != "200.0"), leading-zero strings, and "nan" literals.
    """

    def test_float_target_matches_string_cell(self):
        """The whole point of the fix."""
        assert _filter_value_eq("200", 200.0) is True

    def test_int_target_matches_string_cell(self):
        assert _filter_value_eq("200", 200) is True

    def test_string_target_string_cell_unchanged(self):
        """String target with the same value: exact string match."""
        assert _filter_value_eq("200", "200") is True

    def test_string_target_different_formatting_stays_unequal(self):
        """Open-gem blocker: '200' must NOT equal '200.0' when target is a string."""
        assert _filter_value_eq("200", "200.0") is False
        assert _filter_value_eq("200.0", "200") is False

    def test_leading_zero_string_preserved(self):
        """'00123' must not collapse to 123 when target is a string."""
        assert _filter_value_eq("00123", "00123") is True
        assert _filter_value_eq("00123", "123") is False

    def test_leading_zero_string_vs_numeric_target(self):
        """When caller passes numeric target 123, cell '00123' should coerce and match."""
        assert _filter_value_eq("00123", 123) is True

    def test_nan_string_stays_equal(self):
        """'nan' vs 'nan' as strings: stays equal via string compare.
        (float('nan') == float('nan') is False per IEEE 754, which would
        regress this comparison if the numeric branch were used.)"""
        assert _filter_value_eq("nan", "nan") is True

    def test_string_column_unchanged(self):
        """Pure string columns like 'indexability' still work."""
        assert _filter_value_eq("Indexable", "Indexable") is True
        assert _filter_value_eq("Indexable", "Non-Indexable") is False

    def test_bool_target_not_treated_as_numeric(self):
        """bool is a subclass of int in Python. Without the guard,
        {"col": True} would coerce cells to float and break unexpectedly."""
        # True != "1" via string compare (str(True) == 'True', not '1')
        assert _filter_value_eq("1", True) is False
        # But True == "True" via string compare
        assert _filter_value_eq("True", True) is True

    def test_non_finite_numeric_target_falls_back_to_string(self):
        """If target is float('inf'), math.isfinite(b) is False so we fall
        back to string compare. 'inf' == str(float('inf')) which is 'inf'."""
        assert _filter_value_eq("inf", float("inf")) is True


class TestExtractSnapshotDate:
    def test_timestamp_folder(self, tmp_path):
        p = tmp_path / "2026.04.08.09.04.01"
        p.mkdir()
        assert _extract_snapshot_date(p) == "2026-04-08"

    def test_nested_timestamp_parent(self, tmp_path):
        outer = tmp_path / "2026.04.08.09.04.01"
        inner = outer / "search_console"
        inner.mkdir(parents=True)
        assert _extract_snapshot_date(inner) == "2026-04-08"

    def test_no_match_returns_none(self, tmp_path):
        p = tmp_path / "plain-folder"
        p.mkdir()
        assert _extract_snapshot_date(p) is None


class TestDetectEncoding:
    def test_utf8_bom(self, tmp_path):
        p = tmp_path / "a.csv"
        p.write_bytes(b"\xef\xbb\xbfAddress\n")
        assert _detect_encoding(p) == "utf-8-sig"

    def test_utf16le_bom(self, tmp_path):
        p = tmp_path / "a.csv"
        p.write_bytes(b"\xff\xfeA\x00d\x00")
        assert _detect_encoding(p) == "utf-16"

    def test_plain_ascii_fallback(self, tmp_path):
        p = tmp_path / "a.csv"
        p.write_bytes(b"Address\n")
        assert _detect_encoding(p) == "utf-8-sig"


class TestParseGscDate:
    def test_today(self):
        from datetime import datetime
        assert _parse_gsc_date("today") == datetime.now().date().isoformat()

    def test_yesterday(self):
        from datetime import datetime, timedelta
        expected = (datetime.now().date() - timedelta(days=1)).isoformat()
        assert _parse_gsc_date("yesterday") == expected

    def test_n_days_ago_lowercase(self):
        from datetime import datetime, timedelta
        expected = (datetime.now().date() - timedelta(days=90)).isoformat()
        assert _parse_gsc_date("90daysago") == expected

    def test_n_days_ago_case_insensitive(self):
        from datetime import datetime, timedelta
        expected = (datetime.now().date() - timedelta(days=90)).isoformat()
        assert _parse_gsc_date("90daysAgo") == expected

    def test_iso_date_passthrough(self):
        assert _parse_gsc_date("2026-01-15") == "2026-01-15"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _parse_gsc_date("not a date")


# -------- integration tests via tool entry points --------

class TestLoadFromSfExport:
    async def test_flat_layout(self):
        result = await gsc_load_from_sf_export(
            sf_export_path=str(FIXTURES / "sf_export_flat"),
            site_url="https://example.com/",
        )
        assert result["ok"] is True
        assert result["site_url"] == "https://example.com/"
        loaded = {d["dataset"]: d for d in result["loaded"]}

        assert "search_console_all" in loaded
        assert loaded["search_console_all"]["row_count"] == 5
        assert loaded["search_console_all"]["columns"] == 5
        assert loaded["search_console_all"]["empty"] is False

        assert "search_console_clicks_above_0" in loaded
        assert loaded["search_console_clicks_above_0"]["row_count"] == 0
        assert loaded["search_console_clicks_above_0"]["empty"] is True

        assert "search_console_indexable_url_not_indexed" in loaded
        assert loaded["search_console_indexable_url_not_indexed"]["row_count"] == 3

        assert "internal_all" in loaded
        assert loaded["internal_all"]["row_count"] == 5
        assert loaded["internal_all"]["columns"] == 10

    async def test_nested_layout(self):
        result = await gsc_load_from_sf_export(
            sf_export_path=str(FIXTURES / "sf_export_nested_2026.04.08.09.04.01"),
            site_url="https://nested.example.com/",
        )
        assert result["ok"] is True
        assert result["snapshot_date"] == "2026-04-08"
        loaded = {d["dataset"]: d for d in result["loaded"]}
        assert "search_console_all" in loaded
        assert loaded["search_console_all"]["row_count"] == 2

    async def test_with_metrics_auto_adapts(self):
        """Loader should pick up Clicks/Impressions/CTR/Avg.Position when present."""
        result = await gsc_load_from_sf_export(
            sf_export_path=str(FIXTURES / "sf_export_with_metrics"),
            site_url="https://metrics.example.com/",
        )
        assert result["ok"] is True
        session_id = result["session_id"]
        session = gsc_server._sf_sessions[session_id]
        cols = session["datasets"]["search_console_clicks_above_0"]["columns"]
        assert "clicks" in cols
        assert "impressions" in cols
        assert "ctr" in cols
        assert "position" in cols  # aliased from "Avg. Position"

    async def test_include_internal_false(self):
        result = await gsc_load_from_sf_export(
            sf_export_path=str(FIXTURES / "sf_export_flat"),
            site_url="https://example.com/",
            include_internal=False,
        )
        datasets = {d["dataset"] for d in result["loaded"]}
        assert "internal_all" not in datasets
        assert "search_console_all" in datasets

    async def test_nonexistent_path(self, tmp_path):
        result = await gsc_load_from_sf_export(
            sf_export_path=str(tmp_path / "does-not-exist"),
            site_url="https://example.com/",
        )
        assert result["ok"] is False
        assert "not found" in result["error"]

    async def test_folder_without_sf_csvs(self, tmp_path):
        (tmp_path / "random.txt").write_text("nothing here")
        result = await gsc_load_from_sf_export(
            sf_export_path=str(tmp_path),
            site_url="https://example.com/",
        )
        assert result["ok"] is False
        assert "search_console" in result["error"].lower()

    async def test_explicit_session_id(self):
        result = await gsc_load_from_sf_export(
            sf_export_path=str(FIXTURES / "sf_export_flat"),
            site_url="https://example.com/",
            session_id="sf-test-fixed",
        )
        assert result["session_id"] == "sf-test-fixed"

    async def test_utf16le_fixture(self, tmp_path):
        """Windows SF exports ship as UTF-16LE; loader must auto-detect via BOM."""
        export_dir = tmp_path / "sf_utf16_2026.04.08.09.04.01"
        export_dir.mkdir()
        csv_path = export_dir / "search_console_all.csv"
        content = (
            '"Address","Status Code","Title 1","Indexability","Indexability Status"\r\n'
            '"https://utf16.example.com/","200","UTF16 Home","Indexable",""\r\n'
            '"https://utf16.example.com/page","200","UTF16 Page","Indexable",""\r\n'
        )
        csv_path.write_bytes(b"\xff\xfe" + content.encode("utf-16-le"))

        result = await gsc_load_from_sf_export(
            sf_export_path=str(export_dir),
            site_url="https://utf16.example.com/",
        )
        assert result["ok"] is True
        loaded = {d["dataset"]: d for d in result["loaded"]}
        assert loaded["search_console_all"]["row_count"] == 2
        assert loaded["search_console_all"]["columns"] == 5

    async def test_nested_layout_picks_up_root_internal(self):
        """Codex regression: nested layouts must still find internal_all.csv
        at the EXPORT ROOT, not inside the search_console/ subfolder."""
        result = await gsc_load_from_sf_export(
            sf_export_path=str(FIXTURES / "sf_export_nested_with_internal_2026.04.08.09.04.01"),
            site_url="https://nested.example.com/",
        )
        assert result["ok"] is True
        datasets = {d["dataset"]: d for d in result["loaded"]}
        # search_console_all lives in search_console/ subfolder
        assert "search_console_all" in datasets
        assert datasets["search_console_all"]["row_count"] == 2
        # internal_all lives at the export root — THIS is what Fix 2 enables
        assert "internal_all" in datasets
        assert datasets["internal_all"]["row_count"] == 3
        assert datasets["internal_all"]["columns"] == 2


class TestQuerySfExport:
    async def _load_flat(self):
        result = await gsc_load_from_sf_export(
            sf_export_path=str(FIXTURES / "sf_export_flat"),
            site_url="https://example.com/",
        )
        assert result["ok"] is True
        return result["session_id"]

    async def test_basic_all_rows(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
        )
        assert result["ok"] is True
        assert result["total_matched"] == 5
        assert len(result["rows"]) == 5
        assert result["truncated"] is False

    async def test_filter_eq_scalar(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            filter={"indexability": "Non-Indexable"},
        )
        assert result["ok"] is True
        assert result["total_matched"] == 3
        assert all(r["indexability"] == "Non-Indexable" for r in result["rows"])

    async def test_filter_contains(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            filter={"address": {"op": "contains", "value": "broken"}},
        )
        assert result["total_matched"] == 1
        assert "broken" in result["rows"][0]["address"]

    async def test_filter_gt_numeric(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            filter={"status_code": {"op": "gt", "value": 299}},
        )
        # 301 and 404 rows
        assert result["total_matched"] == 2

    async def test_filter_lt_numeric(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            filter={"status_code": {"op": "lt", "value": 300}},
        )
        # Three 200 rows (home, about, noindex)
        assert result["total_matched"] == 3

    async def test_filter_gte_numeric(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            filter={"status_code": {"op": "gte", "value": 301}},
        )
        # 301 and 404
        assert result["total_matched"] == 2

    async def test_filter_lte_numeric(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            filter={"status_code": {"op": "lte", "value": 200}},
        )
        assert result["total_matched"] == 3

    async def test_filter_unknown_op_surfaces_error(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            filter={"status_code": {"op": "weird", "value": 200}},
        )
        assert result["ok"] is False
        assert "unsupported filter op" in result["error"]

    async def test_filter_eq_numeric_float_vs_string_cell(self):
        """Regression test for Fix 5: {"status_code": {"op": "eq", "value": 200.0}}
        against cell '200' must match. Before the fix this silently returned zero rows."""
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            filter={"status_code": {"op": "eq", "value": 200.0}},
        )
        assert result["ok"] is True
        assert result["total_matched"] == 3

    async def test_filter_eq_scalar_int_target(self):
        """Scalar (non-dict) int target should also benefit from numeric coercion."""
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            filter={"status_code": 200},
        )
        assert result["ok"] is True
        assert result["total_matched"] == 3

    async def test_filter_eq_string_column_unchanged(self):
        """String columns like 'indexability' still work after Fix 5."""
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            filter={"indexability": "Indexable"},
        )
        assert result["ok"] is True
        assert result["total_matched"] == 2  # home + about

    async def test_numeric_sort_no_lex(self):
        """word_count column has values [1200, 800, 2500, 10, 100] — descending
        must be [2500, 1200, 800, 100, 10], NOT lex order [800, 2500, 1200, 100, 10]."""
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="internal_all",
            sort_by="word_count",
            sort_direction="desc",
        )
        assert result["ok"] is True
        ordered = [int(r["word_count"]) for r in result["rows"]]
        assert ordered == [2500, 1200, 800, 100, 10]

    async def test_numeric_sort_asc(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="internal_all",
            sort_by="word_count",
            sort_direction="asc",
        )
        ordered = [int(r["word_count"]) for r in result["rows"]]
        assert ordered == [10, 100, 800, 1200, 2500]

    async def test_pagination_offset_limit(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            sort_by="address",
            sort_direction="asc",
            limit=2,
            offset=0,
        )
        assert result["total_matched"] == 5
        assert len(result["rows"]) == 2
        assert result["truncated"] is True

        result2 = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            sort_by="address",
            sort_direction="asc",
            limit=2,
            offset=2,
        )
        assert len(result2["rows"]) == 2
        assert result["rows"][0]["address"] != result2["rows"][0]["address"]

    async def test_column_projection(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="internal_all",
            columns=["address", "word_count"],
            limit=2,
        )
        assert result["ok"] is True
        assert result["columns"] == ["address", "word_count"]
        for row in result["rows"]:
            assert set(row.keys()) == {"address", "word_count"}

    async def test_path_traversal_rejected(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="../../../etc/passwd",
        )
        assert result["ok"] is False
        assert "invalid dataset name" in result["error"].lower()

    async def test_unknown_dataset(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="nonexistent",
        )
        assert result["ok"] is False
        assert "unknown dataset" in result["error"].lower()
        assert "search_console_all" in result["available"]

    async def test_unknown_filter_column(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            filter={"nonexistent_col": "foo"},
        )
        assert result["ok"] is False
        assert "unknown filter column" in result["error"].lower()

    async def test_unknown_session(self):
        result = await gsc_query_sf_export(
            session_id="sf-nonexistent",
            dataset="search_console_all",
        )
        assert result["ok"] is False
        assert "unknown session" in result["error"].lower()


# -------- Fix 3: streaming pagination + input validation + sort edge cases --------

class TestQueryStreamingAndValidation:
    """Regression tests for the Codex-flagged streaming / pagination issues."""

    async def _load_flat(self):
        result = await gsc_load_from_sf_export(
            sf_export_path=str(FIXTURES / "sf_export_flat"),
            site_url="https://example.com/",
        )
        assert result["ok"] is True
        return result["session_id"]

    async def _load_with_mixed_word_count(self, tmp_path):
        """Create a synthetic export where word_count has one empty cell and
        one inf value, mixed with real numerics. Used for sort edge tests."""
        export_dir = tmp_path / "mixed_2026.04.08.09.04.01"
        export_dir.mkdir()
        (export_dir / "search_console_all.csv").write_text(
            '"Address","Status Code","Title 1","Indexability","Indexability Status"\n'
            '"https://a.example/","200","A","Indexable",""\n'
            '"https://b.example/","200","B","Indexable",""\n'
        )
        # internal_all.csv with mixed numeric values in word_count
        (export_dir / "internal_all.csv").write_text(
            '"Address","Word Count"\n'
            '"https://a.example/","100"\n'
            '"https://b.example/",""\n'       # empty → non-numeric sentinel
            '"https://c.example/","50"\n'
            '"https://d.example/","200"\n'
            '"https://e.example/","inf"\n'    # inf → also non-numeric (math.isfinite guard)
        )
        result = await gsc_load_from_sf_export(
            sf_export_path=str(export_dir),
            site_url="https://mixed.example/",
        )
        assert result["ok"] is True
        return result["session_id"]

    async def test_streaming_pagination_counts_total_matched(self):
        """Unsorted pagination path: slice must be correct AND total_matched
        must reflect every matching row, not just the returned window."""
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            limit=2,
            offset=1,
        )
        assert result["ok"] is True
        assert result["total_matched"] == 5
        assert len(result["rows"]) == 2
        assert result["truncated"] is True

    async def test_limit_zero_counts_only_no_rows(self):
        """limit=0 is a counts-only query. Even with a nonzero offset, it must
        NOT invoke the heap path and must return total_matched correctly."""
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            limit=0,
            offset=100,
        )
        assert result["ok"] is True
        assert result["rows"] == []
        assert result["total_matched"] == 5

    async def test_limit_zero_with_filter(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            filter={"indexability": "Non-Indexable"},
            limit=0,
        )
        assert result["ok"] is True
        assert result["rows"] == []
        assert result["total_matched"] == 3

    async def test_sort_desc_puts_empty_numeric_last(self, tmp_path):
        """Descending sort on a numeric column with one empty cell must place
        the empty row LAST, not first."""
        session_id = await self._load_with_mixed_word_count(tmp_path)
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="internal_all",
            sort_by="word_count",
            sort_direction="desc",
        )
        assert result["ok"] is True
        word_counts = [r["word_count"] for r in result["rows"]]
        # Real numerics first in descending order; non-numeric sentinels last.
        # "200", "100", "50" are finite numerics → descend as [200, 100, 50].
        # "", "inf" are non-finite/non-numeric → tail, order within group not guaranteed.
        assert word_counts[:3] == ["200", "100", "50"]
        assert set(word_counts[3:]) == {"", "inf"}

    async def test_sort_asc_puts_empty_numeric_last(self, tmp_path):
        session_id = await self._load_with_mixed_word_count(tmp_path)
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="internal_all",
            sort_by="word_count",
            sort_direction="asc",
        )
        assert result["ok"] is True
        word_counts = [r["word_count"] for r in result["rows"]]
        # Ascending: real numerics first, non-numerics last
        assert word_counts[:3] == ["50", "100", "200"]
        assert set(word_counts[3:]) == {"", "inf"}

    async def test_inf_nan_treated_as_non_numeric(self, tmp_path):
        """math.isfinite guard: inf sorts with the non-numeric sentinel group."""
        session_id = await self._load_with_mixed_word_count(tmp_path)
        # Top-3 descending should NOT include the "inf" row
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="internal_all",
            sort_by="word_count",
            sort_direction="desc",
            limit=3,
        )
        assert result["ok"] is True
        word_counts = [r["word_count"] for r in result["rows"]]
        assert word_counts == ["200", "100", "50"]

    async def test_reject_negative_offset(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            offset=-1,
        )
        assert result["ok"] is False
        assert "offset and limit must be >= 0" in result["error"]

    async def test_reject_negative_limit(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            limit=-5,
        )
        assert result["ok"] is False
        assert "offset and limit must be >= 0" in result["error"]

    async def test_reject_invalid_sort_direction(self):
        session_id = await self._load_flat()
        result = await gsc_query_sf_export(
            session_id=session_id,
            dataset="search_console_all",
            sort_by="address",
            sort_direction="sideways",
        )
        assert result["ok"] is False
        assert "sort_direction must be 'asc' or 'desc'" in result["error"]


# -------- Fix 4: landing-page diffs None sort --------

class TestSortLandingPageDiffs:
    """Regression tests for the Codex-flagged bug where None values sorted to
    the FRONT of descending period comparisons instead of the tail."""

    def test_none_last_ascending(self):
        rows = [
            {"clicks_pct": -0.2},
            {"clicks_pct": None},
            {"clicks_pct": -0.5},
            {"clicks_pct": 0.1},
        ]
        out = _sort_landing_page_diffs(rows, "clicks_pct", "asc")
        assert [r["clicks_pct"] for r in out] == [-0.5, -0.2, 0.1, None]

    def test_none_last_descending(self):
        """The Codex bug: with the old (group, value) sort key, reverse=True
        put None rows at position 0. This test locks in the fix."""
        rows = [
            {"clicks_pct": -0.2},
            {"clicks_pct": None},
            {"clicks_pct": 0.3},
        ]
        out = _sort_landing_page_diffs(rows, "clicks_pct", "desc")
        assert [r["clicks_pct"] for r in out] == [0.3, -0.2, None]

    def test_all_none(self):
        rows = [{"clicks_pct": None}, {"clicks_pct": None}]
        out = _sort_landing_page_diffs(rows, "clicks_pct", "desc")
        assert len(out) == 2
        assert all(r["clicks_pct"] is None for r in out)

    def test_empty_list(self):
        assert _sort_landing_page_diffs([], "clicks_pct", "asc") == []

    def test_preserves_other_fields(self):
        rows = [
            {"page": "a", "clicks_pct": -0.5},
            {"page": "b", "clicks_pct": None},
            {"page": "c", "clicks_pct": 0.2},
        ]
        out = _sort_landing_page_diffs(rows, "clicks_pct", "desc")
        assert [r["page"] for r in out] == ["c", "a", "b"]


# -------- Fix 3+4: batch_url_inspection validation + auth reorder --------

class _AuthWouldFire(Exception):
    """Raised by a monkeypatched get_gsc_service when validation should have
    blocked the call. Catching this in tests proves validation executed
    AFTER the auth-wall, which is the opposite of what Fix 4 wants."""


class TestBatchUrlInspectionValidation:
    """All tests here assert that validation errors surface WITHOUT calling
    get_gsc_service. The fixture replaces the auth helper with a function
    that raises _AuthWouldFire — if a test ever sees that exception, the
    validation failed to run first."""

    @pytest.fixture(autouse=True)
    def _no_auth(self, monkeypatch):
        def _explode():
            raise _AuthWouldFire("get_gsc_service called before validation")
        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)

    # --- session-backed path ---

    async def _load_flat_session(self):
        result = await gsc_load_from_sf_export(
            sf_export_path=str(FIXTURES / "sf_export_flat"),
            site_url="https://example.com/",
        )
        return result["session_id"]

    async def test_unknown_session(self):
        result = await batch_url_inspection(
            site_url="https://example.com/",
            from_session="sf-does-not-exist",
        )
        assert "Unknown SF session_id" in result

    async def test_invalid_dataset_name_path_traversal(self):
        session_id = await self._load_flat_session()
        result = await batch_url_inspection(
            site_url="https://example.com/",
            from_session=session_id,
            dataset="../../etc/passwd",
        )
        assert "Invalid dataset name" in result

    async def test_unknown_dataset_in_session(self):
        session_id = await self._load_flat_session()
        result = await batch_url_inspection(
            site_url="https://example.com/",
            from_session=session_id,
            dataset="nonexistent",
        )
        assert "Unknown dataset" in result

    async def test_reject_negative_offset(self):
        session_id = await self._load_flat_session()
        result = await batch_url_inspection(
            site_url="https://example.com/",
            from_session=session_id,
            dataset="search_console_all",
            offset=-1,
        )
        assert "Invalid offset" in result

    async def test_reject_limit_zero(self):
        session_id = await self._load_flat_session()
        result = await batch_url_inspection(
            site_url="https://example.com/",
            from_session=session_id,
            dataset="search_console_all",
            limit=0,
        )
        assert "Invalid limit" in result

    async def test_reject_negative_limit(self):
        session_id = await self._load_flat_session()
        result = await batch_url_inspection(
            site_url="https://example.com/",
            from_session=session_id,
            dataset="search_console_all",
            limit=-5,
        )
        assert "Invalid limit" in result

    # --- direct-urls path ---

    async def test_empty_urls_returns_before_auth(self):
        """Direct path: empty urls string must fail before the OAuth call."""
        result = await batch_url_inspection(
            site_url="https://example.com/",
            urls="",
        )
        assert result == "No URLs provided for inspection."

    async def test_over_ten_direct_urls_returns_before_auth(self):
        """Direct path: >10 URLs must surface the count error before auth."""
        urls = "\n".join(f"https://example.com/p{i}" for i in range(11))
        result = await batch_url_inspection(
            site_url="https://example.com/",
            urls=urls,
        )
        assert "Too many URLs provided" in result


# -------- Fix 2: gsc_compare_periods_landing_pages validation --------

class TestCompareLandingPagesValidation:
    """Validation for limit, sort_by, and sort_direction. All tests use a
    monkeypatched get_gsc_service that raises, proving validation fires
    BEFORE authentication."""

    @pytest.fixture(autouse=True)
    def _no_auth(self, monkeypatch):
        def _explode():
            raise _AuthWouldFire("get_gsc_service called before validation")
        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)

    async def test_reject_limit_zero(self):
        result = await gsc_compare_periods_landing_pages(
            site_url="https://example.com/",
            period_a_start="180daysago", period_a_end="91daysago",
            period_b_start="90daysago", period_b_end="yesterday",
            limit=0,
        )
        assert result["ok"] is False
        assert "limit must be >= 1" in result["error"]

    async def test_reject_negative_limit(self):
        result = await gsc_compare_periods_landing_pages(
            site_url="https://example.com/",
            period_a_start="180daysago", period_a_end="91daysago",
            period_b_start="90daysago", period_b_end="yesterday",
            limit=-5,
        )
        assert result["ok"] is False
        assert "limit must be >= 1" in result["error"]

    async def test_reject_invalid_sort_by(self):
        result = await gsc_compare_periods_landing_pages(
            site_url="https://example.com/",
            period_a_start="180daysago", period_a_end="91daysago",
            period_b_start="90daysago", period_b_end="yesterday",
            sort_by="bogus",
        )
        assert result["ok"] is False
        assert "invalid sort_by" in result["error"]

    async def test_reject_invalid_sort_direction(self):
        result = await gsc_compare_periods_landing_pages(
            site_url="https://example.com/",
            period_a_start="180daysago", period_a_end="91daysago",
            period_b_start="90daysago", period_b_end="yesterday",
            sort_direction="descending",  # should be 'desc' not 'descending'
        )
        assert result["ok"] is False
        assert "sort_direction must be 'asc' or 'desc'" in result["error"]

    async def test_reject_invalid_date(self):
        result = await gsc_compare_periods_landing_pages(
            site_url="https://example.com/",
            period_a_start="not a date", period_a_end="yesterday",
            period_b_start="7daysago", period_b_end="yesterday",
        )
        assert result["ok"] is False
        # _parse_gsc_date delegates to datetime.strptime which raises
        # "time data 'not a date' does not match format '%Y-%m-%d'"
        assert "does not match format" in result["error"] or "invalid date" in result["error"]

    async def test_sort_direction_uppercase_would_pass_validation(self, monkeypatch):
        """The normalize-then-validate order means 'DESC' should PASS validation
        and reach the auth wall (which explodes). This proves the
        str().strip().lower() normalization works before the whitelist check."""
        result = await gsc_compare_periods_landing_pages(
            site_url="https://example.com/",
            period_a_start="180daysago", period_a_end="91daysago",
            period_b_start="90daysago", period_b_end="yesterday",
            sort_direction="DESC",
        )
        # Either the auth-wall explodes (proving we passed validation), or
        # the response surfaces the _AuthWouldFire message through the outer
        # except-block. Either way it's NOT a sort_direction validation error.
        assert "sort_direction must be" not in str(result)


# -------- Fix 1: gsc_get_landing_page_summary striking_distance_range --------

class TestLandingPageSummaryValidation:
    """Validation paths for the restored striking_distance_range parameter.
    All tests use a monkeypatched get_gsc_service that raises, proving the
    validation error surfaces before any API call."""

    @pytest.fixture(autouse=True)
    def _no_auth(self, monkeypatch):
        def _explode():
            raise _AuthWouldFire("get_gsc_service called before validation")
        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)

    async def test_reject_reversed_range(self):
        """min > max must be rejected with the finite-and-ordered error."""
        result = await gsc_get_landing_page_summary(
            site_url="https://example.com/",
            striking_distance_range=(20.0, 10.0),
        )
        assert result["ok"] is False
        assert "min <= max" in result["error"]

    async def test_reject_wrong_length_range(self):
        """A one-item tuple must be rejected on unpack with the two-item error."""
        result = await gsc_get_landing_page_summary(
            site_url="https://example.com/",
            striking_distance_range=(5.0,),
        )
        assert result["ok"] is False
        assert "two-item" in result["error"]

    async def test_reject_non_numeric_range(self):
        """String values must fail the float() coercion."""
        result = await gsc_get_landing_page_summary(
            site_url="https://example.com/",
            striking_distance_range=("a", "b"),
        )
        assert result["ok"] is False
        assert "two-item" in result["error"]

    async def test_reject_non_finite_range(self):
        """inf values must be rejected by the math.isfinite check."""
        result = await gsc_get_landing_page_summary(
            site_url="https://example.com/",
            striking_distance_range=(float("inf"), 20.0),
        )
        assert result["ok"] is False
        assert "min <= max" in result["error"] or "finite" in result["error"]

    async def test_reject_nan_range(self):
        """NaN values must be rejected by the math.isfinite check.
        Note that NaN is excluded via isfinite BEFORE the lo > hi check
        (which would otherwise be False because all NaN comparisons are False)."""
        result = await gsc_get_landing_page_summary(
            site_url="https://example.com/",
            striking_distance_range=(float("nan"), 20.0),
        )
        assert result["ok"] is False

    async def test_list_range_accepted_as_input(self):
        """JSON clients send arrays, not tuples — validation should accept
        [5.0, 15.0]. Validation passes and execution reaches get_gsc_service,
        which the fixture replaces with a function that raises _AuthWouldFire.
        The tool's outer except-Exception handler catches that and returns an
        error dict mentioning the exception name — which is our signal that
        we got past validation."""
        result = await gsc_get_landing_page_summary(
            site_url="https://example.com/",
            striking_distance_range=[5.0, 15.0],
        )
        assert result["ok"] is False
        assert "_AuthWouldFire" in result["error"]
        # And critically, NOT a striking_distance_range validation error:
        assert "striking_distance_range" not in result["error"]

    async def test_default_range_reaches_auth(self):
        """With no striking_distance_range passed, the default (11.0, 20.0)
        is valid and execution reaches get_gsc_service (proved the same way)."""
        result = await gsc_get_landing_page_summary(
            site_url="https://example.com/",
        )
        assert result["ok"] is False
        assert "_AuthWouldFire" in result["error"]
        assert "striking_distance_range" not in result["error"]


# -------- _read_account_scopes pure-logic tests --------

class TestReadAccountScopes:
    """The helper must never leak exceptions or refresh tokens — all
    failure modes return ['<unavailable>']."""

    def test_none_path(self):
        assert _read_account_scopes(None) == ["<unavailable>"]

    def test_empty_string_path(self):
        assert _read_account_scopes("") == ["<unavailable>"]

    def test_nonexistent_relative_path(self):
        assert _read_account_scopes("accounts/definitely-not-there/token.json") == ["<unavailable>"]

    def test_nonexistent_absolute_path(self, tmp_path):
        p = tmp_path / "missing.json"
        assert _read_account_scopes(str(p)) == ["<unavailable>"]

    def test_corrupt_json_swallows_exception(self, tmp_path):
        """A malformed JSON token file must surface as <unavailable> WITHOUT
        leaking the underlying exception message (which could contain token
        material if Credentials ever stringifies its input)."""
        p = tmp_path / "bad.json"
        p.write_text("not valid json {{{")
        result = _read_account_scopes(str(p))
        assert result == ["<unavailable>"]

"""Tests for the composed gsc_* + SF bridge tools after the B.4
envelope harmonisation.

These tools already returned dict-shaped {ok, error, tool} envelopes
but lacked B.4's hint + retry_after fields. The harmonisation
migrates the outer HttpError + Exception handlers to use
_http_error_envelope / _make_error_envelope so every error envelope
carries the full B.4 shape.

Validation-layer dicts inside the tools (striking_distance_range
rejects, invalid sort_by, missing session, etc.) stay as-is — they're
already B.4-compatible-enough (missing hint/retry_after fields get
defensively read as .get('hint', '') by consumers).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

import gsc_server
from gsc_server import (
    gsc_compare_periods_landing_pages,
    gsc_get_landing_page_summary,
    gsc_health_check,
    gsc_load_from_sf_export,
    gsc_query_sf_export,
)


def _http_error(status: int, message: str = "err") -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "Error"
    resp.get = MagicMock(return_value=None)
    return HttpError(resp=resp, content=f'{{"error": {{"message": "{message}"}}}}'.encode())


def _assert_full_b4_envelope(env, *, tool: str):
    """Every B.4 envelope must carry ok/error/hint/retry_after/tool."""
    assert env["ok"] is False
    assert "error" in env and env["error"]
    assert "hint" in env  # may be empty string but key must exist
    assert "retry_after" in env  # may be None
    assert env["tool"] == tool


class TestLandingPageSummary:
    async def test_http_403_envelope_full_shape(self, monkeypatch):
        service = MagicMock()
        service.searchanalytics.return_value.query.return_value.execute.side_effect = _http_error(403)
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        env = await gsc_get_landing_page_summary(site_url="sc-domain:example.com")
        _assert_full_b4_envelope(env, tool="gsc_get_landing_page_summary")
        assert "HTTP 403" in env["error"]
        assert "get_active_account" in env["hint"]

    async def test_generic_exception_envelope_full_shape(self, monkeypatch):
        def _explode():
            raise RuntimeError("boom")
        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)
        env = await gsc_get_landing_page_summary(site_url="sc-domain:example.com")
        _assert_full_b4_envelope(env, tool="gsc_get_landing_page_summary")
        assert "RuntimeError" in env["error"]

    async def test_validation_dict_stays_lightweight(self, monkeypatch):
        """Validation errors deliberately KEEP the minimal shape —
        they're already actionable. This test locks that decision so a
        future refactor doesn't bloat them into full envelopes."""
        env = await gsc_get_landing_page_summary(
            site_url="sc-domain:example.com",
            striking_distance_range=(20.0, 10.0),  # min > max
        )
        assert env["ok"] is False
        assert env["tool"] == "gsc_get_landing_page_summary"
        assert "min <= max" in env["error"]


class TestComparePeriodsLandingPages:
    async def test_http_429_envelope_with_retry_after(self, monkeypatch):
        resp = MagicMock()
        resp.status = 429
        resp.reason = "Too Many Requests"
        resp.get = MagicMock(side_effect=lambda k, d=None: "30" if k == "retry-after" else d)
        service = MagicMock()
        service.searchanalytics.return_value.query.return_value.execute.side_effect = HttpError(
            resp=resp, content=b'{"error": {"message": "slow"}}'
        )
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        env = await gsc_compare_periods_landing_pages(
            site_url="sc-domain:example.com",
            period_a_start="2026-01-01",
            period_a_end="2026-01-31",
            period_b_start="2026-02-01",
            period_b_end="2026-02-28",
        )
        _assert_full_b4_envelope(env, tool="gsc_compare_periods_landing_pages")
        assert env["retry_after"] == 30.0

    async def test_generic_exception_envelope_mentions_sort_by(self, monkeypatch):
        def _explode():
            raise RuntimeError("unexpected")
        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)
        env = await gsc_compare_periods_landing_pages(
            site_url="sc-domain:example.com",
            period_a_start="2026-01-01",
            period_a_end="2026-01-31",
            period_b_start="2026-02-01",
            period_b_end="2026-02-28",
        )
        _assert_full_b4_envelope(env, tool="gsc_compare_periods_landing_pages")
        assert "sort_by" in env["hint"]


class TestHealthCheck:
    async def test_auth_httperror_envelope(self, monkeypatch):
        def _explode():
            raise _http_error(401)
        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)
        env = await gsc_health_check(site_url="sc-domain:example.com")
        _assert_full_b4_envelope(env, tool="gsc_health_check")
        assert "HTTP 401" in env["error"]
        assert "authenticate" in env["hint"].lower() or "re-authenticate" in env["hint"].lower()

    async def test_auth_generic_exception_envelope(self, monkeypatch):
        def _explode():
            raise RuntimeError("no creds")
        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)
        env = await gsc_health_check(site_url="sc-domain:example.com")
        _assert_full_b4_envelope(env, tool="gsc_health_check")
        assert "auth failed" in env["error"]
        assert "client_secrets" in env["hint"]


class TestSfBridge:
    async def test_load_missing_path_stays_lightweight_validation(self, tmp_path):
        """The 'path not found' return is a validation-layer dict
        that intentionally stays lightweight — no HTTP involved, the
        error IS the hint."""
        env = await gsc_load_from_sf_export(
            sf_export_path=str(tmp_path / "nonexistent"),
            site_url="sc-domain:example.com",
        )
        assert env["ok"] is False
        assert "path not found" in env["error"]
        assert env["tool"] == "gsc_load_from_sf_export"

    async def test_load_outer_exception_gets_full_envelope(self, monkeypatch, tmp_path):
        # Force an error inside the loading logic by patching the
        # private helper to explode.
        def _explode(path):
            raise RuntimeError("fs brittle")
        monkeypatch.setattr(gsc_server, "_resolve_sf_dir", _explode)
        # Make the directory so the path-check passes.
        tmp_path.mkdir(exist_ok=True)
        env = await gsc_load_from_sf_export(
            sf_export_path=str(tmp_path),
            site_url="sc-domain:example.com",
        )
        _assert_full_b4_envelope(env, tool="gsc_load_from_sf_export")
        assert "RuntimeError" in env["error"]
        assert "search_console_" in env["hint"]

    async def test_query_unknown_session_stays_lightweight(self):
        env = await gsc_query_sf_export(
            session_id="never-loaded",
            dataset="search_console_all",
        )
        assert env["ok"] is False
        assert "never-loaded" in env["error"]
        assert env["tool"] == "gsc_query_sf_export"

    async def test_query_outer_exception_gets_full_envelope(self, monkeypatch):
        # Seed a session so we get past the early validation.
        gsc_server._sf_sessions["test-session"] = {
            "session_id": "test-session",
            "datasets": {
                "search_console_all": {
                    "path": "/nonexistent.csv",
                    "columns": ["address", "clicks"],
                    "row_count": 0,
                    "empty": False,
                    "file_size": 0,
                },
            },
        }
        try:
            def _explode(*args, **kwargs):
                raise RuntimeError("stream broke")
            monkeypatch.setattr(gsc_server, "_stream_sf_csv", _explode)
            env = await gsc_query_sf_export(
                session_id="test-session",
                dataset="search_console_all",
            )
            _assert_full_b4_envelope(env, tool="gsc_query_sf_export")
            assert "RuntimeError" in env["error"]
            assert "gsc_load_from_sf_export" in env["hint"]
        finally:
            del gsc_server._sf_sessions["test-session"]

"""Tests for the URL inspection cluster after the B.4 envelope rollout.

Covers the outer exception handlers only — the per-URL inner try/except
in batch/check tools is intentionally left string-based (each iteration
collects errors into the results list; envelope-per-URL would flip the
return shape to dict and break the text-composition contract).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

import gsc_server
from gsc_server import (
    gsc_batch_url_inspection,
    gsc_check_indexing_issues,
    gsc_inspect_url_enhanced,
)


def _http_error(status: int, message: str = "boom") -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "Error"
    resp.get = MagicMock(return_value=None)
    return HttpError(resp=resp, content=f'{{"error": {{"message": "{message}"}}}}'.encode())


class TestInspectUrlEnhanced:
    async def test_http_error_renders_envelope_with_site_url_hint(self, monkeypatch):
        service = MagicMock()
        service.urlInspection.return_value.index.return_value.inspect.return_value.execute.side_effect = (
            _http_error(403, "no access")
        )
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_inspect_url_enhanced(
            "sc-domain:example.com", "https://example.com/foo"
        )
        assert out.startswith("Error: HTTP 403")
        assert "Hint:" in out
        assert "sc-domain:example.com" in out

    async def test_generic_exception_renders_envelope(self, monkeypatch):
        def _explode():
            raise RuntimeError("boom")
        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)
        out = await gsc_inspect_url_enhanced(
            "sc-domain:example.com", "https://example.com/foo"
        )
        assert "RuntimeError" in out
        assert "Hint:" in out

    async def test_happy_path_preserves_markdown_shape(self, monkeypatch):
        service = MagicMock()
        service.urlInspection.return_value.index.return_value.inspect.return_value.execute.return_value = {
            "inspectionResult": {
                "indexStatusResult": {
                    "verdict": "PASS",
                    "coverageState": "Submitted and indexed",
                    "pageFetchState": "SUCCESSFUL",
                    "robotsTxtState": "ALLOWED",
                },
            }
        }
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_inspect_url_enhanced(
            "sc-domain:example.com", "https://example.com/foo"
        )
        assert "URL Inspection for https://example.com/foo" in out
        assert "Indexing Status: PASS" in out
        assert "Error:" not in out


class TestBatchUrlInspection:
    async def test_outer_oauth_failure_renders_envelope(self, monkeypatch):
        def _explode():
            raise RuntimeError("oauth dead")
        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)
        out = await gsc_batch_url_inspection(
            site_url="sc-domain:example.com",
            urls="https://example.com/a",
        )
        # Since oauth fails BEFORE the per-URL loop, this is an outer
        # exception — envelope treatment is the right shape.
        assert "RuntimeError" in out
        assert "oauth dead" in out
        assert "Hint:" in out

    async def test_outer_http_error_before_loop(self, monkeypatch):
        # Simulate an HttpError raised by get_gsc_service itself
        # (credential validation failure, for instance).
        def _raise_http_err():
            raise _http_error(401, "token expired")
        monkeypatch.setattr(gsc_server, "get_gsc_service", _raise_http_err)
        out = await gsc_batch_url_inspection(
            site_url="sc-domain:example.com",
            urls="https://example.com/a",
        )
        assert "HTTP 401" in out
        assert "re-authenticate" in out.lower() or "authenticate" in out.lower()

    async def test_per_url_error_still_goes_into_results_list(self, monkeypatch):
        """Inner try/except is deliberately NOT migrated — per-URL
        errors get collected into the formatted result string so the
        batch tool can still return one coherent response even if N-1
        URLs succeeded."""
        service = MagicMock()
        # First URL fails, second URL succeeds.
        responses = [
            RuntimeError("per-url failure"),
            {
                "inspectionResult": {
                    "indexStatusResult": {"verdict": "PASS", "coverageState": "OK"},
                }
            },
        ]
        def _execute_side_effect(*args, **kwargs):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        service.urlInspection.return_value.index.return_value.inspect.return_value.execute.side_effect = _execute_side_effect
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)

        out = await gsc_batch_url_inspection(
            site_url="sc-domain:example.com",
            urls="https://example.com/a\nhttps://example.com/b",
        )
        # Failed URL surfaces as string inside the batch response,
        # NOT as an outer envelope that masks the successful one.
        assert "https://example.com/a: Error" in out
        assert "PASS" in out  # second URL succeeded
        assert not out.startswith("Error:")  # no outer envelope wraps it


class TestCheckIndexingIssues:
    async def test_outer_exception_renders_envelope(self, monkeypatch):
        def _explode():
            raise RuntimeError("boom")
        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)
        out = await gsc_check_indexing_issues(
            "sc-domain:example.com", "https://example.com/a"
        )
        assert "RuntimeError" in out
        assert "Hint:" in out

    async def test_outer_http_error_envelope(self, monkeypatch):
        def _explode():
            raise _http_error(429, "slow down")
        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)
        out = await gsc_check_indexing_issues(
            "sc-domain:example.com", "https://example.com/a"
        )
        assert "HTTP 429" in out
        assert "Retry-after:" in out

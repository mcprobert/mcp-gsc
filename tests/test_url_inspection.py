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


class TestInspectUrlEnhancedJson:
    """F2: JSON response_format emits nested structured payload."""

    async def test_happy_path_json_shape(self, monkeypatch):
        service = MagicMock()
        service.urlInspection.return_value.index.return_value.inspect.return_value.execute.return_value = {
            "inspectionResult": {
                "indexStatusResult": {
                    "verdict": "PASS",
                    "coverageState": "Submitted and indexed",
                    "lastCrawlTime": "2026-04-17T10:00:00Z",
                    "pageFetchState": "SUCCESSFUL",
                    "robotsTxtState": "ALLOWED",
                    "indexingState": "INDEXING_ALLOWED",
                    "googleCanonical": "https://example.com/foo",
                    "userCanonical": "https://example.com/foo",
                    "crawledAs": "MOBILE",
                    "referringUrls": ["https://example.com/a", "https://example.com/b"],
                },
                "richResultsResult": {
                    "verdict": "PASS",
                    "detectedItems": [{"richResultType": "Article", "items": []}],
                },
            }
        }
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_inspect_url_enhanced(
            "sc-domain:example.com",
            "https://example.com/foo",
            response_format="json",
        )
        assert out["ok"] is True
        assert out["tool"] == "gsc_inspect_url_enhanced"
        assert out["page_url"] == "https://example.com/foo"
        assert out["index_status"]["verdict"] == "PASS"
        assert out["index_status"]["coverage_state"] == "Submitted and indexed"
        assert out["index_status"]["referring_urls"] == [
            "https://example.com/a",
            "https://example.com/b",
        ]
        assert out["rich_results"]["verdict"] == "PASS"
        assert out["rich_results"]["detected_items"][0]["rich_result_type"] == "Article"

    async def test_missing_inspection_result_json(self, monkeypatch):
        """When the API returns an empty envelope, JSON mode returns
        ok:True with index_status=None (the URL is just unknown), not
        an error envelope."""
        service = MagicMock()
        service.urlInspection.return_value.index.return_value.inspect.return_value.execute.return_value = {}
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_inspect_url_enhanced(
            "sc-domain:example.com",
            "https://example.com/foo",
            response_format="json",
        )
        assert out["ok"] is True
        assert out["index_status"] is None
        assert out["rich_results"] is None

    async def test_invalid_response_format_returns_error_string(self, monkeypatch):
        out = await gsc_inspect_url_enhanced(
            "sc-domain:example.com",
            "https://example.com/foo",
            response_format="xml",
        )
        assert isinstance(out, str)
        assert "response_format" in out


class TestBatchUrlInspectionJson:
    async def test_happy_path_json_shape(self, monkeypatch):
        service = MagicMock()
        service.urlInspection.return_value.index.return_value.inspect.return_value.execute.return_value = {
            "inspectionResult": {
                "indexStatusResult": {
                    "verdict": "PASS",
                    "coverageState": "Submitted and indexed",
                    "lastCrawlTime": "2026-04-17T10:00:00Z",
                },
                "richResultsResult": {
                    "verdict": "PASS",
                    "detectedItems": [{"richResultType": "Article"}],
                },
            }
        }
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_batch_url_inspection(
            site_url="sc-domain:example.com",
            urls="https://example.com/a",
            response_format="json",
        )
        assert out["ok"] is True
        assert out["tool"] == "gsc_batch_url_inspection"
        assert out["row_count"] == 1
        row = out["rows"][0]
        assert row["url"] == "https://example.com/a"
        assert row["verdict"] == "PASS"
        assert row["rich_results"] == ["Article"]
        assert row["error"] is None

    async def test_validation_error_returns_envelope_in_json_mode(self, monkeypatch):
        out = await gsc_batch_url_inspection(
            site_url="sc-domain:example.com",
            urls="",
            response_format="json",
        )
        assert isinstance(out, dict)
        assert out["ok"] is False
        assert "No URLs" in out["error"]


class TestCheckIndexingIssuesJson:
    async def test_happy_path_json_shape(self, monkeypatch):
        service = MagicMock()
        responses = [
            {  # indexed
                "inspectionResult": {
                    "indexStatusResult": {"verdict": "PASS", "coverageState": "OK"},
                }
            },
            {  # canonical conflict + robots blocked
                "inspectionResult": {
                    "indexStatusResult": {
                        "verdict": "PASS",
                        "coverageState": "OK",
                        "googleCanonical": "https://example.com/canonical",
                        "userCanonical": "https://example.com/user",
                        "robotsTxtState": "BLOCKED",
                    }
                }
            },
        ]

        def _execute_side_effect(*args, **kwargs):
            return responses.pop(0)

        service.urlInspection.return_value.index.return_value.inspect.return_value.execute.side_effect = _execute_side_effect
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)

        out = await gsc_check_indexing_issues(
            "sc-domain:example.com",
            "https://example.com/a\nhttps://example.com/b",
            response_format="json",
        )
        assert out["ok"] is True
        assert out["summary"]["total"] == 2
        assert out["summary"]["canonical_conflict"] == 1
        assert out["summary"]["robots_blocked"] == 1
        assert out["buckets"]["canonical_conflict"][0]["google_canonical"] == (
            "https://example.com/canonical"
        )
        assert out["buckets"]["robots_blocked"] == ["https://example.com/b"]

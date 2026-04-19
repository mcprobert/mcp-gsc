"""Tests for gsc_submit_sitemap / gsc_delete_sitemap / gsc_manage_sitemaps after
the B.4 envelope rollout.

gsc_delete_sitemap preserves its idempotent 404 short-circuit (same
pattern as gsc_delete_site). gsc_manage_sitemaps is a dispatcher — its own
envelope fires only on validation-layer programming errors since the
delegated tools already handle their own HttpError.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

import gsc_server
from gsc_server import gsc_delete_sitemap, gsc_manage_sitemaps, gsc_submit_sitemap


def _http_error(status: int, message: str = "err") -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "Error"
    resp.get = MagicMock(return_value=None)
    return HttpError(resp=resp, content=f'{{"error": {{"message": "{message}"}}}}'.encode())


class TestSubmitSitemap:
    async def test_happy_path_with_details(self, monkeypatch):
        service = MagicMock()
        service.sitemaps.return_value.submit.return_value.execute.return_value = None
        service.sitemaps.return_value.get.return_value.execute.return_value = {
            "lastSubmitted": "2026-04-17T10:00:00Z",
            "isPending": True,
        }
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_submit_sitemap(
            "sc-domain:example.com", "https://example.com/sitemap.xml"
        )
        assert "Successfully submitted sitemap" in out
        assert "Pending processing" in out

    async def test_details_fetch_failure_falls_back_to_basic_success(self, monkeypatch):
        service = MagicMock()
        service.sitemaps.return_value.submit.return_value.execute.return_value = None
        # details() fetch raises, but submit already succeeded.
        service.sitemaps.return_value.get.return_value.execute.side_effect = RuntimeError("details hiccup")
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_submit_sitemap(
            "sc-domain:example.com", "https://example.com/sitemap.xml"
        )
        assert "Successfully submitted sitemap" in out
        assert "Google will queue it for processing" in out
        # Must NOT leak the details-fetch failure as an error envelope.
        assert "Error:" not in out

    async def test_http_error_on_submit_renders_envelope(self, monkeypatch):
        service = MagicMock()
        service.sitemaps.return_value.submit.return_value.execute.side_effect = _http_error(403)
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_submit_sitemap(
            "sc-domain:example.com", "https://example.com/sitemap.xml"
        )
        assert out.startswith("Error: HTTP 403")
        assert "sc-domain:example.com" in out

    async def test_generic_exception_envelope_has_hint(self, monkeypatch):
        def _explode():
            raise RuntimeError("network dead")
        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)
        out = await gsc_submit_sitemap(
            "sc-domain:example.com", "https://example.com/sitemap.xml"
        )
        assert "RuntimeError" in out
        assert "sitemap URL is reachable" in out


class TestDeleteSitemap:
    async def test_happy_path(self, monkeypatch):
        service = MagicMock()
        service.sitemaps.return_value.get.return_value.execute.return_value = {"path": "x"}
        service.sitemaps.return_value.delete.return_value.execute.return_value = None
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_delete_sitemap(
            "sc-domain:example.com", "https://example.com/sitemap.xml"
        )
        assert "Successfully deleted sitemap" in out

    async def test_404_idempotent_short_circuit_via_http_error(self, monkeypatch):
        """When sitemaps().get() raises a 404 HttpError, gsc_delete_sitemap
        treats it as idempotent success — matches gsc_delete_site's
        404 behaviour from the site-CRUD rollout."""
        service = MagicMock()
        service.sitemaps.return_value.get.return_value.execute.side_effect = _http_error(404)
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_delete_sitemap(
            "sc-domain:example.com", "https://example.com/missing.xml"
        )
        assert "Sitemap not found" in out
        assert "already been deleted" in out
        assert "Error:" not in out

    async def test_legacy_404_string_match_still_works(self, monkeypatch):
        """Defensive fallback — non-HttpError exceptions whose str()
        contains '404' are also treated as idempotent."""
        service = MagicMock()
        service.sitemaps.return_value.get.return_value.execute.side_effect = Exception(
            "something 404 something"
        )
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_delete_sitemap(
            "sc-domain:example.com", "https://example.com/x.xml"
        )
        assert "Sitemap not found" in out

    async def test_403_on_delete_renders_envelope(self, monkeypatch):
        service = MagicMock()
        service.sitemaps.return_value.get.return_value.execute.return_value = {"path": "x"}
        service.sitemaps.return_value.delete.return_value.execute.side_effect = _http_error(403)
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_delete_sitemap(
            "sc-domain:example.com", "https://example.com/x.xml"
        )
        assert out.startswith("Error: HTTP 403")

    async def test_generic_exception_envelope_mentions_list_tool(self, monkeypatch):
        def _explode():
            raise RuntimeError("boom")
        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)
        out = await gsc_delete_sitemap(
            "sc-domain:example.com", "https://example.com/x.xml"
        )
        assert "gsc_list_sitemaps_enhanced" in out


class TestManageSitemaps:
    async def test_unknown_action_returns_plain_validation_message(self, monkeypatch):
        out = await gsc_manage_sitemaps(
            "sc-domain:example.com", action="fetch-all"
        )
        assert "Invalid action" in out
        # Validation-level messages are NOT wrapped in envelopes.
        assert "Error:" not in out

    async def test_missing_sitemap_url_for_details(self, monkeypatch):
        out = await gsc_manage_sitemaps(
            "sc-domain:example.com", action="details"
        )
        assert "requires a sitemap_url parameter" in out
        assert "Error:" not in out

    async def test_list_action_delegates(self, monkeypatch):
        # The dispatcher must actually route — patch the delegate and
        # confirm it runs.
        called = {"n": 0}

        async def _fake_list(site_url, sitemap_index=None, response_format="markdown", *, account_alias=None):
            called["n"] += 1
            return "delegated-list-ok"

        monkeypatch.setattr(gsc_server, "gsc_list_sitemaps_enhanced", _fake_list)
        out = await gsc_manage_sitemaps(
            "sc-domain:example.com", action="list"
        )
        assert out == "delegated-list-ok"
        assert called["n"] == 1

    async def test_dispatcher_outer_envelope_fires_on_delegate_typeerror(self, monkeypatch):
        """The outer envelope is a safety net for programming errors
        that escape the delegate (e.g. a bad kwarg signature). It must
        not swallow routine HttpErrors — those are handled inside the
        delegated tools."""

        async def _fake_list_bad_signature(site_url, sitemap_index=None, response_format="markdown", *, account_alias=None):
            raise TypeError("unexpected argument xyz")

        monkeypatch.setattr(gsc_server, "gsc_list_sitemaps_enhanced", _fake_list_bad_signature)
        out = await gsc_manage_sitemaps(
            "sc-domain:example.com", action="list"
        )
        assert "TypeError" in out
        assert "list, details, submit, delete" in out

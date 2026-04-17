"""Tests for the site CRUD cluster after the B.4 envelope rollout.

Covers the idempotent-edge-case preservation (409 on add, 404 on
delete), plus the status-aware envelope rendering for the remaining
HttpError codes and generic exceptions.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

import gsc_server
from gsc_server import add_site, delete_site, get_site_details


def _mock_http_error(status: int, message: str = "msg", reason: str = "") -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = reason or "Error"
    resp.get = MagicMock(return_value=None)
    body = ('{"error": {"message": "' + message + '"}}').encode()
    return HttpError(resp=resp, content=body)


class TestAddSite:
    async def test_happy_path(self, monkeypatch):
        service = MagicMock()
        service.sites.return_value.add.return_value.execute.return_value = {
            "permissionLevel": "siteOwner"
        }
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await add_site("sc-domain:example.com")
        assert "has been added" in out
        assert "Permission level: siteOwner" in out

    async def test_409_idempotent_success_message(self, monkeypatch):
        """409 Conflict → site already added. Should surface as clean
        idempotent success, NOT a bare error envelope."""
        service = MagicMock()
        service.sites.return_value.add.return_value.execute.side_effect = _mock_http_error(409)
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await add_site("sc-domain:example.com")
        assert "already added" in out
        assert "Error:" not in out  # Must not render as a B.4 error envelope

    async def test_403_renders_envelope_with_hint(self, monkeypatch):
        service = MagicMock()
        service.sites.return_value.add.return_value.execute.side_effect = _mock_http_error(
            403, message="no access"
        )
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await add_site("sc-domain:example.com")
        assert out.startswith("Error: HTTP 403")
        assert "Hint:" in out
        assert "sc-domain:example.com" in out

    async def test_generic_exception_renders_envelope(self, monkeypatch):
        def _explode():
            raise RuntimeError("boom")
        monkeypatch.setattr(gsc_server, "get_gsc_service", _explode)
        out = await add_site("sc-domain:example.com")
        assert "RuntimeError" in out
        assert "Hint:" in out


class TestDeleteSite:
    async def test_happy_path(self, monkeypatch):
        service = MagicMock()
        service.sites.return_value.delete.return_value.execute.return_value = None
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await delete_site("sc-domain:example.com")
        assert "has been removed" in out

    async def test_404_idempotent_not_found_message(self, monkeypatch):
        """404 → nothing to delete. Should NOT render as an envelope;
        this matches the pre-B.4 "Site X was not found" semantics."""
        service = MagicMock()
        service.sites.return_value.delete.return_value.execute.side_effect = _mock_http_error(404)
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await delete_site("sc-domain:example.com")
        assert "was not found" in out
        assert "Error:" not in out

    async def test_403_renders_envelope(self, monkeypatch):
        service = MagicMock()
        service.sites.return_value.delete.return_value.execute.side_effect = _mock_http_error(
            403, message="denied"
        )
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await delete_site("sc-domain:example.com")
        assert out.startswith("Error: HTTP 403")
        assert "get_active_account" in out  # B.4's 403 hint

    async def test_429_envelope_surfaces_retry_after(self, monkeypatch):
        resp = MagicMock()
        resp.status = 429
        resp.reason = "Too Many Requests"
        resp.get = MagicMock(side_effect=lambda k, d=None: "45" if k == "retry-after" else d)
        http_err = HttpError(resp=resp, content=b'{"error": {"message": "slow"}}')
        service = MagicMock()
        service.sites.return_value.delete.return_value.execute.side_effect = http_err
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await delete_site("sc-domain:example.com")
        assert "Retry-after: 45s" in out


class TestGetSiteDetails:
    async def test_happy_path(self, monkeypatch):
        service = MagicMock()
        service.sites.return_value.get.return_value.execute.return_value = {
            "permissionLevel": "siteOwner",
            "siteVerificationInfo": {
                "verificationState": "VERIFIED",
                "verifiedUser": "user@example.com",
                "verificationMethod": "DNS",
            },
        }
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await get_site_details("sc-domain:example.com")
        assert "Permission level: siteOwner" in out
        assert "Verification state: VERIFIED" in out

    async def test_404_envelope(self, monkeypatch):
        service = MagicMock()
        service.sites.return_value.get.return_value.execute.side_effect = _mock_http_error(404)
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await get_site_details("sc-domain:example.com")
        assert "HTTP 404" in out
        assert "sc-domain:" in out  # hint names the property

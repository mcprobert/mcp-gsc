"""Tests for the site CRUD cluster after the B.4 envelope rollout.

Covers the idempotent-edge-case preservation (409 on add, 404 on
delete), plus the status-aware envelope rendering for the remaining
HttpError codes and generic exceptions.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

import gsc_server
from gsc_server import gsc_add_site, gsc_delete_site, gsc_get_site_details


def _mock_http_error(status: int, message: str = "msg", reason: str = "") -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = reason or "Error"
    resp.get = MagicMock(return_value=None)
    body = ('{"error": {"message": "' + message + '"}}').encode()
    return HttpError(resp=resp, content=body)


def _configure_one_account(monkeypatch, service=None):
    """Write a single-account manifest + patch the non-interactive auth
    helper to return ``service`` so ``gsc_add_site`` can route."""
    accounts_dir = Path(gsc_server.ACCOUNTS_DIR)
    accounts_dir.mkdir(parents=True, exist_ok=True)
    (accounts_dir / "accounts.json").write_text(json.dumps({
        "accounts": {"only": {"alias": "only", "token_file": "t"}},
    }))
    if service is None:
        service = MagicMock()
    monkeypatch.setattr(
        gsc_server, "_build_service_noninteractive",
        lambda alias: (service, None),
    )
    return service


class TestAddSite:
    async def test_happy_path(self, monkeypatch):
        service = MagicMock()
        service.sites.return_value.add.return_value.execute.return_value = {
            "permissionLevel": "siteOwner"
        }
        _configure_one_account(monkeypatch, service)
        out = await gsc_add_site("sc-domain:example.com")
        assert "has been added" in out
        assert "Permission level: siteOwner" in out

    async def test_409_idempotent_success_message(self, monkeypatch):
        """409 Conflict → site already added. Should surface as clean
        idempotent success, NOT a bare error envelope."""
        service = MagicMock()
        service.sites.return_value.add.return_value.execute.side_effect = _mock_http_error(409)
        _configure_one_account(monkeypatch, service)
        out = await gsc_add_site("sc-domain:example.com")
        assert "already added" in out
        assert "Error:" not in out  # Must not render as a B.4 error envelope

    async def test_403_renders_envelope_with_hint(self, monkeypatch):
        service = MagicMock()
        service.sites.return_value.add.return_value.execute.side_effect = _mock_http_error(
            403, message="no access"
        )
        _configure_one_account(monkeypatch, service)
        out = await gsc_add_site("sc-domain:example.com")
        assert out.startswith("Error: HTTP 403")
        assert "Hint:" in out
        assert "sc-domain:example.com" in out

    async def test_generic_exception_renders_envelope(self, monkeypatch):
        service = MagicMock()
        service.sites.return_value.add.side_effect = RuntimeError("boom")
        _configure_one_account(monkeypatch, service)
        out = await gsc_add_site("sc-domain:example.com")
        assert "RuntimeError" in out
        assert "Hint:" in out


class TestDeleteSite:
    async def test_happy_path(self, monkeypatch):
        service = MagicMock()
        service.sites.return_value.delete.return_value.execute.return_value = None
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_delete_site("sc-domain:example.com")
        assert "has been removed" in out

    async def test_404_idempotent_not_found_message(self, monkeypatch):
        """404 → nothing to delete. Should NOT render as an envelope;
        this matches the pre-B.4 "Site X was not found" semantics."""
        service = MagicMock()
        service.sites.return_value.delete.return_value.execute.side_effect = _mock_http_error(404)
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_delete_site("sc-domain:example.com")
        assert "was not found" in out
        assert "Error:" not in out

    async def test_403_renders_envelope(self, monkeypatch):
        service = MagicMock()
        service.sites.return_value.delete.return_value.execute.side_effect = _mock_http_error(
            403, message="denied"
        )
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_delete_site("sc-domain:example.com")
        assert out.startswith("Error: HTTP 403")
        # v1.2.0: 403 hint points to gsc_whoami / gsc_list_accounts
        # (replacements for the deprecated gsc_get_active_account surface).
        assert "gsc_whoami" in out or "gsc_list_accounts" in out

    async def test_429_envelope_surfaces_retry_after(self, monkeypatch):
        resp = MagicMock()
        resp.status = 429
        resp.reason = "Too Many Requests"
        resp.get = MagicMock(side_effect=lambda k, d=None: "45" if k == "retry-after" else d)
        http_err = HttpError(resp=resp, content=b'{"error": {"message": "slow"}}')
        service = MagicMock()
        service.sites.return_value.delete.return_value.execute.side_effect = http_err
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_delete_site("sc-domain:example.com")
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
        out = await gsc_get_site_details("sc-domain:example.com")
        assert "Permission level: siteOwner" in out
        assert "Verification state: VERIFIED" in out

    async def test_404_envelope(self, monkeypatch):
        service = MagicMock()
        service.sites.return_value.get.return_value.execute.side_effect = _mock_http_error(404)
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_get_site_details("sc-domain:example.com")
        assert "HTTP 404" in out
        assert "sc-domain:" in out  # hint names the property

    async def test_json_mode_with_full_metadata(self, monkeypatch):
        """F5: JSON mode emits a structured envelope with nullable
        verification / ownership sub-objects."""
        service = MagicMock()
        service.sites.return_value.get.return_value.execute.return_value = {
            "permissionLevel": "siteOwner",
            "siteVerificationInfo": {
                "verificationState": "VERIFIED",
                "verifiedUser": "user@example.com",
                "verificationMethod": "DNS",
            },
            "ownershipInfo": {
                "owner": "user@example.com",
                "verificationMethod": "DNS",
            },
        }
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_get_site_details(
            "sc-domain:example.com", response_format="json"
        )
        assert out["ok"] is True
        assert out["tool"] == "gsc_get_site_details"
        assert out["site_url"] == "sc-domain:example.com"
        assert out["permission_level"] == "siteOwner"
        assert out["verification"] == {
            "state": "VERIFIED",
            "verified_user": "user@example.com",
            "method": "DNS",
        }
        assert out["ownership"] == {"owner": "user@example.com", "method": "DNS"}

    async def test_json_mode_minimal_property(self, monkeypatch):
        """F5: many domain properties only carry permissionLevel. The
        verification / ownership blocks must be null (not absent)
        so consumers can branch on presence without KeyError."""
        service = MagicMock()
        service.sites.return_value.get.return_value.execute.return_value = {
            "permissionLevel": "siteFullUser",
        }
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_get_site_details(
            "sc-domain:example.com", response_format="json"
        )
        assert out["permission_level"] == "siteFullUser"
        assert out["verification"] is None
        assert out["ownership"] is None

    async def test_invalid_response_format_returns_error_string(self, monkeypatch):
        out = await gsc_get_site_details(
            "sc-domain:example.com", response_format="xml"
        )
        assert isinstance(out, str)
        assert "response_format" in out

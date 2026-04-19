"""F6: gsc_whoami diagnostic.

Resolves which account would serve a site_url WITHOUT making an actual
GSC call. Returns a structured envelope so agents can branch on
routing decisions before committing to an expensive analytics call.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import gsc_server
from gsc_server import ErrorCode, gsc_whoami


def _write_manifest(accounts: dict) -> None:
    dir_path = Path(gsc_server.ACCOUNTS_DIR)
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "accounts.json").write_text(json.dumps({"accounts": accounts}))


def _seed_cache(alias: str, state: str, *, properties=None):
    gsc_server._account_property_state[alias] = state
    if state == "ok":
        gsc_server._account_properties[alias] = set(properties or [])


class TestWhoami:
    async def test_unique_resolution(self, monkeypatch):
        _write_manifest({
            "a": {"alias": "a", "token_file": "t"},
            "b": {"alias": "b", "token_file": "t"},
        })
        _seed_cache("a", "ok", properties=["sc-domain:ex.com"])
        _seed_cache("b", "ok", properties=["sc-domain:other.com"])

        out = await gsc_whoami("sc-domain:ex.com")
        assert out["ok"] is True
        assert out["tool"] == "gsc_whoami"
        assert out["resolved_account"] == "a"
        assert out["alternatives"] == []

    async def test_ambiguous_resolution(self, monkeypatch):
        _write_manifest({
            "a": {"alias": "a", "token_file": "t"},
            "b": {"alias": "b", "token_file": "t"},
        })
        _seed_cache("a", "ok", properties=["sc-domain:shared.com"])
        _seed_cache("b", "ok", properties=["sc-domain:shared.com"])

        out = await gsc_whoami("sc-domain:shared.com")
        # Ambiguity is ok=True but resolved_account=None. The tool's
        # job is to SURFACE the ambiguity, not to make it an error —
        # agents use gsc_whoami specifically to discover this before
        # calling a real tool.
        assert out["ok"] is True
        assert out["resolved_account"] is None
        assert set(out["alternatives"]) == {"a", "b"}
        assert out["meta"]["ambiguous"] is True

    async def test_no_account_for_property(self, monkeypatch):
        _write_manifest({"a": {"alias": "a", "token_file": "t"}})
        _seed_cache("a", "ok", properties=["sc-domain:other.com"])

        # Prevent force-refresh from reviving cache (resolver tries that
        # on zero-candidate path).
        async def _noop(alias, *, force_refresh=False):
            return None
        monkeypatch.setattr(gsc_server, "_ensure_property_cache", _noop)

        out = await gsc_whoami("sc-domain:missing.com")
        assert out["ok"] is False
        assert out["error_code"] == ErrorCode.NO_ACCOUNT_FOR_PROPERTY

    async def test_no_accounts_configured(self, monkeypatch):
        # No manifest written.
        out = await gsc_whoami("sc-domain:ex.com")
        assert out["ok"] is False
        assert out["error_code"] == ErrorCode.NO_ACCOUNTS_CONFIGURED

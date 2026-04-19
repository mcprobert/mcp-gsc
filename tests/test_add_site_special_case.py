"""F5: gsc_add_site special-case routing.

The resolver's "property must be reachable" verification doesn't
apply here — by definition the property isn't in GSC yet. Rules:

- Explicit account_alias → use directly, no pre-verification.
- No alias + one account → use it.
- No alias + multiple accounts → AMBIGUOUS_ACCOUNT (caller must pick).
- No accounts → NO_ACCOUNTS_CONFIGURED.

Plus: on successful add, the chosen alias's property cache is
invalidated so subsequent read tools discover the new property.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import gsc_server
from gsc_server import ErrorCode, gsc_add_site


def _write_manifest(accounts: dict) -> None:
    dir_path = Path(gsc_server.ACCOUNTS_DIR)
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "accounts.json").write_text(json.dumps({"accounts": accounts}))


def _mock_service() -> MagicMock:
    service = MagicMock()
    service.sites.return_value.add.return_value.execute.return_value = {
        "permissionLevel": "siteOwner",
    }
    return service


class TestNoAccounts:
    async def test_empty_manifest_surfaces_no_accounts_configured(self, monkeypatch):
        out = await gsc_add_site("sc-domain:ex.com")
        assert out.startswith("Error: No GSC accounts configured.")
        assert "gsc_add_account" in out


class TestSingleAccount:
    async def test_single_account_no_alias_uses_it(self, monkeypatch):
        _write_manifest({"only": {"alias": "only", "token_file": "t"}})
        service = _mock_service()
        monkeypatch.setattr(
            gsc_server, "_build_service_noninteractive",
            lambda alias: (service, None),
        )
        out = await gsc_add_site("sc-domain:ex.com")
        assert "has been added" in out
        # Cache of the chosen alias is invalidated on success so
        # subsequent reads pick up the new property.
        assert gsc_server._account_property_state.get("only") == "never" or \
               "only" not in gsc_server._account_property_state


class TestMultipleAccountsNoAlias:
    async def test_multiple_no_alias_returns_ambiguous(self, monkeypatch):
        _write_manifest({
            "a": {"alias": "a", "token_file": "ta"},
            "b": {"alias": "b", "token_file": "tb"},
        })
        out = await gsc_add_site("sc-domain:ex.com")
        # AMBIGUOUS_ACCOUNT for add_site — caller must choose. Cannot
        # auto-resolve because the property isn't in GSC yet.
        assert out.startswith("Error:")
        assert "Multiple accounts" in out or "AMBIGUOUS" in out or "auto-resolve" in out
        assert "account_alias" in out


class TestExplicitAlias:
    async def test_explicit_alias_uses_it_without_property_verification(
        self, monkeypatch,
    ):
        """The KEY test: even though "a" has ZERO properties in the
        cache, gsc_add_site with account_alias="a" must still work. This
        is what makes gsc_add_site a special case — the whole point is
        to add a property that isn't there yet."""
        _write_manifest({
            "a": {"alias": "a", "token_file": "t"},
            "b": {"alias": "b", "token_file": "t"},
        })
        # Seed "a" with empty properties to prove the resolver isn't
        # running (it would reject this as ACCOUNT_SITE_MISMATCH).
        gsc_server._account_property_state["a"] = "ok"
        gsc_server._account_properties["a"] = set()

        service = _mock_service()
        monkeypatch.setattr(
            gsc_server, "_build_service_noninteractive",
            lambda alias: (service, None) if alias == "a" else (None, ErrorCode.INTERNAL_ERROR),
        )
        out = await gsc_add_site("sc-domain:ex.com", account_alias="a")
        assert "has been added" in out

    async def test_explicit_unknown_alias_errors(self, monkeypatch):
        _write_manifest({"a": {"alias": "a", "token_file": "t"}})
        out = await gsc_add_site("sc-domain:ex.com", account_alias="ghost")
        assert out.startswith("Error:")
        assert "ghost" in out

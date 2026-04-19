"""Tests for the account-mutation cluster after the B.4 envelope rollout.

The account tools are all LOCAL (manifest + filesystem ops, no GSC API)
so there's no HttpError path to cover — only generic exceptions and
the `gsc_add_account` HeadlessOAuthError special-case.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import gsc_server
from gsc_server import (
    gsc_add_account,
    gsc_list_accounts,
    gsc_remove_account,
    gsc_switch_account,
)


@pytest.fixture
def fake_accounts_home(tmp_path, monkeypatch):
    """Isolate manifest + accounts dir per test.

    The autouse conftest shim already isolates SCRIPT_DIR/ACCOUNTS_DIR
    to a tmp_path + creates ``accounts/``. This fixture just hands
    back the effective tmp root so tests can write manifests into it.
    """
    return Path(gsc_server.ACCOUNTS_DIR).parent


class TestListAccounts:
    async def test_empty_manifest_explains_next_step(self, fake_accounts_home):
        out = await gsc_list_accounts()
        assert "No accounts configured" in out
        assert "gsc_add_account" in out

    async def test_populated_manifest_renders_both_accounts(self, fake_accounts_home):
        """v1.2.0: no more active-account marker. List renders all
        accounts alphabetically with email + scopes."""
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text(json.dumps({
            "accounts": {
                "client-a": {"alias": "client-a", "email": "a@example.com", "added_at": "2026-04-17"},
                "client-b": {"alias": "client-b", "email": "b@example.com", "added_at": "2026-04-16"},
            },
        }))
        out = await gsc_list_accounts()
        assert "**client-a**" in out
        assert "**client-b**" in out
        # Active-account concept is gone; no marker should appear.
        assert "(active)" not in out

    async def test_exception_renders_envelope(self, monkeypatch):
        def _explode():
            raise RuntimeError("manifest locked")
        monkeypatch.setattr(gsc_server, "_load_manifest", _explode)
        out = await gsc_list_accounts()
        assert "RuntimeError" in out
        assert "manifest locked" in out
        assert "Hint:" in out


class TestAddAccount:
    async def test_invalid_alias_short_circuits_before_filesystem(self, fake_accounts_home):
        # `_validate_alias` lowercases first, so case alone doesn't
        # fail — use a genuinely invalid char. Underscore is rejected
        # by the regex ^[a-z0-9][a-z0-9-]*$.
        out = await gsc_add_account("has_underscore")
        assert "Invalid alias" in out
        # Alias validation error is NOT wrapped in an envelope — the
        # message is already actionable and includes the regex rule.
        assert "Error:" not in out

    async def test_alias_collision_returns_instructive_message(self, fake_accounts_home):
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text(json.dumps({
            "active_account": "existing",
            "accounts": {"existing": {"alias": "existing", "email": "e@x.com"}},
        }))
        out = await gsc_add_account("existing")
        assert "already exists" in out
        assert "gsc_remove_account" in out

    async def test_headless_error_surfaces_verbatim(self, fake_accounts_home, monkeypatch):
        """HeadlessOAuthError already carries a detailed remediation
        message from _start_oauth_flow; B.4 preserves it verbatim rather
        than wrapping it in an envelope prefix."""
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text("{}")

        # Ensure the client_secrets check passes so we reach the OAuth flow.
        monkeypatch.setattr(gsc_server, "OAUTH_CLIENT_SECRETS_FILE", str(fake_accounts_home / "client_secrets.json"))
        (fake_accounts_home / "client_secrets.json").write_text("{}")

        def _raise_headless(*args, **kwargs):
            raise gsc_server.HeadlessOAuthError(
                "OAuth required for gsc_add_account('new'), but GSC_MCP_HEADLESS=1 is set. "
                "Run `python gsc_server.py --login` from a desktop session..."
            )
        monkeypatch.setattr(gsc_server, "_start_oauth_flow", _raise_headless)
        # InstalledAppFlow.from_client_secrets_file must not fail.
        monkeypatch.setattr(
            gsc_server.InstalledAppFlow,
            "from_client_secrets_file",
            lambda *a, **kw: MagicMock(),
        )

        out = await gsc_add_account("new")
        assert "GSC_MCP_HEADLESS=1" in out
        # Envelope prefix must NOT be present — agent sees the raw
        # remediation message.
        assert not out.startswith("Error:")

    async def test_oauth_flow_failure_envelope_includes_cleanup_hint(self, fake_accounts_home, monkeypatch):
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text("{}")
        monkeypatch.setattr(gsc_server, "OAUTH_CLIENT_SECRETS_FILE", str(fake_accounts_home / "client_secrets.json"))
        (fake_accounts_home / "client_secrets.json").write_text("{}")

        def _raise_runtime(*args, **kwargs):
            raise RuntimeError("port already in use")
        monkeypatch.setattr(gsc_server, "_start_oauth_flow", _raise_runtime)
        monkeypatch.setattr(
            gsc_server.InstalledAppFlow,
            "from_client_secrets_file",
            lambda *a, **kw: MagicMock(),
        )

        out = await gsc_add_account("new")
        assert out.startswith("Error: OAuth flow failed:")
        assert "RuntimeError" in out
        assert "port already in use" in out
        assert "Hint:" in out
        # Partial account dir must have been cleaned up.
        assert not (fake_accounts_home / "accounts" / "new").exists()


class TestSwitchAccountDeprecated:
    """v1.2.0: gsc_switch_account is a behavioural no-op that returns
    ``ok:false`` with ``error_code: DEPRECATED_TOOL`` so old callers
    can't silently believe state was changed."""

    async def test_invalid_alias_still_validated(self, fake_accounts_home):
        # Underscore violates the regex. Even for a deprecated tool we
        # surface the validation failure distinctly, so a typo doesn't
        # get masked by the generic deprecation message.
        out = await gsc_switch_account("has_underscore")
        assert out["ok"] is False
        assert out["error_code"] == "DEPRECATED_TOOL"
        assert "invalid" in out["error"].lower()

    async def test_known_alias_returns_deprecation_envelope(self, fake_accounts_home):
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text(json.dumps({
            "accounts": {"a": {"alias": "a"}, "b": {"alias": "b", "email": "b@example.com"}},
        }))
        out = await gsc_switch_account("b")
        assert out["ok"] is False
        assert out["error_code"] == "DEPRECATED_TOOL"
        assert out["retryable"] is False
        assert "account_alias" in out["hint"]
        # replacement info surfaces so agents can migrate their calls.
        # F15: nested key is ``suggested_tool`` (not ``tool``) to avoid
        # collision with the envelope-level ``tool`` field.
        assert "replacement" in out
        assert "suggested_tool" in out["replacement"]
        assert "tool" not in out["replacement"]  # old name must be gone

    async def test_unknown_alias_includes_alternatives(self, fake_accounts_home):
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text(json.dumps({
            "accounts": {"a": {"alias": "a"}, "b": {"alias": "b"}},
        }))
        out = await gsc_switch_account("nonexistent")
        assert out["ok"] is False
        assert out["error_code"] == "DEPRECATED_TOOL"
        # Known aliases listed for discovery.
        assert set(out["alternatives"]) == {"a", "b"}


class TestRemoveAccount:
    async def test_unknown_alias_lists_available(self, fake_accounts_home):
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text(json.dumps({
            "active_account": "a",
            "accounts": {"a": {"alias": "a"}},
        }))
        out = await gsc_remove_account("nonexistent")
        assert "not found" in out

    async def test_happy_path_sets_new_active(self, fake_accounts_home):
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text(json.dumps({
            "active_account": "a",
            "accounts": {"a": {"alias": "a"}, "b": {"alias": "b"}},
        }))
        # Create directories so shutil.rmtree has something to remove.
        (fake_accounts_home / "accounts" / "a").mkdir()
        (fake_accounts_home / "accounts" / "b").mkdir()

        out = await gsc_remove_account("a")
        assert "Account 'a' removed" in out
        assert "Active account is now 'b'" in out

    async def test_last_account_removal_falls_back_to_legacy(self, fake_accounts_home):
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text(json.dumps({
            "active_account": "only",
            "accounts": {"only": {"alias": "only"}},
        }))
        (fake_accounts_home / "accounts" / "only").mkdir()

        out = await gsc_remove_account("only")
        assert "No accounts remaining" in out
        assert "legacy token.json" in out

    async def test_exception_renders_envelope(self, fake_accounts_home, monkeypatch):
        def _explode():
            raise PermissionError("read only fs")
        monkeypatch.setattr(gsc_server, "_load_manifest", _explode)
        out = await gsc_remove_account("a")
        assert "PermissionError" in out
        assert "Hint:" in out

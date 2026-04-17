"""Tests for the account-mutation cluster after the B.4 envelope rollout.

The account tools are all LOCAL (manifest + filesystem ops, no GSC API)
so there's no HttpError path to cover — only generic exceptions and
the `add_account` HeadlessOAuthError special-case.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import gsc_server
from gsc_server import (
    add_account,
    list_accounts,
    remove_account,
    switch_account,
)


@pytest.fixture
def fake_accounts_home(tmp_path, monkeypatch):
    """Isolate manifest + accounts dir per test."""
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    manifest = tmp_path / "accounts" / "accounts.json"
    monkeypatch.setattr(gsc_server, "ACCOUNTS_DIR", str(accounts_dir))
    monkeypatch.setattr(gsc_server, "ACCOUNTS_MANIFEST", str(manifest))
    # Reset active-account module state between tests.
    monkeypatch.setattr(gsc_server, "_active_account", None)
    return tmp_path


class TestListAccounts:
    async def test_empty_manifest_explains_next_step(self, fake_accounts_home):
        out = await list_accounts()
        assert "No accounts configured" in out
        assert "add_account" in out

    async def test_populated_manifest_renders_active_marker(self, fake_accounts_home):
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text(json.dumps({
            "active_account": "client-a",
            "accounts": {
                "client-a": {"alias": "client-a", "email": "a@example.com", "added_at": "2026-04-17"},
                "client-b": {"alias": "client-b", "email": "b@example.com", "added_at": "2026-04-16"},
            },
        }))
        out = await list_accounts()
        assert "**client-a**" in out and "**(active)**" in out
        assert "**client-b**" in out
        # Active marker only fires on the active account.
        assert out.count("**(active)**") == 1

    async def test_exception_renders_envelope(self, monkeypatch):
        def _explode():
            raise RuntimeError("manifest locked")
        monkeypatch.setattr(gsc_server, "_load_manifest", _explode)
        out = await list_accounts()
        assert "RuntimeError" in out
        assert "manifest locked" in out
        assert "Hint:" in out


class TestAddAccount:
    async def test_invalid_alias_short_circuits_before_filesystem(self, fake_accounts_home):
        # `_validate_alias` lowercases first, so case alone doesn't
        # fail — use a genuinely invalid char. Underscore is rejected
        # by the regex ^[a-z0-9][a-z0-9-]*$.
        out = await add_account("has_underscore")
        assert "Invalid alias" in out
        # Alias validation error is NOT wrapped in an envelope — the
        # message is already actionable and includes the regex rule.
        assert "Error:" not in out

    async def test_alias_collision_returns_instructive_message(self, fake_accounts_home):
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text(json.dumps({
            "active_account": "existing",
            "accounts": {"existing": {"alias": "existing", "email": "e@x.com"}},
        }))
        out = await add_account("existing")
        assert "already exists" in out
        assert "remove_account" in out

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
                "OAuth required for add_account('new'), but GSC_MCP_HEADLESS=1 is set. "
                "Run `python gsc_server.py --login` from a desktop session..."
            )
        monkeypatch.setattr(gsc_server, "_start_oauth_flow", _raise_headless)
        # InstalledAppFlow.from_client_secrets_file must not fail.
        monkeypatch.setattr(
            gsc_server.InstalledAppFlow,
            "from_client_secrets_file",
            lambda *a, **kw: MagicMock(),
        )

        out = await add_account("new")
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

        out = await add_account("new")
        assert out.startswith("Error: OAuth flow failed:")
        assert "RuntimeError" in out
        assert "port already in use" in out
        assert "Hint:" in out
        # Partial account dir must have been cleaned up.
        assert not (fake_accounts_home / "accounts" / "new").exists()


class TestSwitchAccount:
    async def test_invalid_alias(self, fake_accounts_home):
        # Underscore violates the regex; case alone is lowercased first.
        out = await switch_account("has_underscore")
        assert "Invalid alias" in out

    async def test_unknown_alias_lists_available(self, fake_accounts_home):
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text(json.dumps({
            "active_account": "a",
            "accounts": {"a": {"alias": "a"}, "b": {"alias": "b"}},
        }))
        out = await switch_account("nonexistent")
        assert "not found" in out
        assert "a, b" in out or "b, a" in out

    async def test_happy_path(self, fake_accounts_home):
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text(json.dumps({
            "active_account": "a",
            "accounts": {"a": {"alias": "a"}, "b": {"alias": "b", "email": "b@example.com"}},
        }))
        out = await switch_account("b")
        assert "Switched to account 'b'" in out
        assert "b@example.com" in out

    async def test_exception_renders_envelope(self, fake_accounts_home, monkeypatch):
        def _explode():
            raise OSError("manifest unreadable")
        monkeypatch.setattr(gsc_server, "_load_manifest", _explode)
        out = await switch_account("a")
        assert "OSError" in out
        assert "manifest unreadable" in out
        assert "list_accounts" in out  # The hint names the next tool.


class TestRemoveAccount:
    async def test_unknown_alias_lists_available(self, fake_accounts_home):
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text(json.dumps({
            "active_account": "a",
            "accounts": {"a": {"alias": "a"}},
        }))
        out = await remove_account("nonexistent")
        assert "not found" in out

    async def test_happy_path_sets_new_active(self, fake_accounts_home):
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text(json.dumps({
            "active_account": "a",
            "accounts": {"a": {"alias": "a"}, "b": {"alias": "b"}},
        }))
        # Create directories so shutil.rmtree has something to remove.
        (fake_accounts_home / "accounts" / "a").mkdir()
        (fake_accounts_home / "accounts" / "b").mkdir()

        out = await remove_account("a")
        assert "Account 'a' removed" in out
        assert "Active account is now 'b'" in out

    async def test_last_account_removal_falls_back_to_legacy(self, fake_accounts_home):
        Path(fake_accounts_home / "accounts" / "accounts.json").write_text(json.dumps({
            "active_account": "only",
            "accounts": {"only": {"alias": "only"}},
        }))
        (fake_accounts_home / "accounts" / "only").mkdir()

        out = await remove_account("only")
        assert "No accounts remaining" in out
        assert "legacy token.json" in out

    async def test_exception_renders_envelope(self, fake_accounts_home, monkeypatch):
        def _explode():
            raise PermissionError("read only fs")
        monkeypatch.setattr(gsc_server, "_load_manifest", _explode)
        out = await remove_account("a")
        assert "PermissionError" in out
        assert "Hint:" in out

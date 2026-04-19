"""F2: _build_service_noninteractive safety contract.

This helper is the auth lever for the resolver and for every routed
tool call in v1.2.0+. It MUST NOT:

- launch a browser OAuth flow
- delete a token file on refresh failure
- fall back to service-account credentials

and MUST:

- return AUTH_EXPIRED on missing/expired/unrefreshable tokens
- return a service + None on happy path
- persist a refreshed token to disk only on success

These are regression-critical — a silent fallback here would reintroduce
the confused-deputy class of bug this refactor exists to remove.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import gsc_server
from gsc_server import ErrorCode, _build_service_noninteractive


@pytest.fixture
def isolated_accounts(monkeypatch, tmp_path):
    """Point ACCOUNTS_DIR / ACCOUNTS_MANIFEST at a clean tmp_path.

    Also resets _migration_checked so the legacy-migration hook re-runs
    cleanly against an empty TOKEN_FILE (which tmp_path does not contain).
    """
    accounts_dir = tmp_path / "accounts"
    manifest_path = accounts_dir / "accounts.json"
    monkeypatch.setattr(gsc_server, "ACCOUNTS_DIR", str(accounts_dir))
    monkeypatch.setattr(gsc_server, "ACCOUNTS_MANIFEST", str(manifest_path))
    monkeypatch.setattr(gsc_server, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(gsc_server, "TOKEN_FILE", str(tmp_path / "token.json"))
    # Reset migration guard so the helper under test can hit a clean slate.
    monkeypatch.setattr(gsc_server, "_migration_checked", False)
    monkeypatch.setattr(gsc_server, "_active_account", None)
    return tmp_path


def _write_manifest(tmp_path: Path, accounts: dict) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"accounts": accounts}
    (accounts_dir / "accounts.json").write_text(json.dumps(manifest))


def _write_token_file(tmp_path: Path, rel_path: str, payload: dict) -> Path:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(payload))
    return full


class TestMissingAliasOrToken:
    def test_alias_not_in_manifest_returns_internal_error(self, isolated_accounts):
        _write_manifest(isolated_accounts, {"real": {
            "alias": "real",
            "token_file": "accounts/real/token.json",
        }})
        service, err = _build_service_noninteractive("ghost")
        assert service is None
        # INTERNAL_ERROR (not AUTH_EXPIRED): retrying would not change
        # the outcome, because the alias genuinely doesn't exist.
        assert err == ErrorCode.INTERNAL_ERROR

    def test_alias_with_empty_token_path_returns_auth_expired(self, isolated_accounts):
        _write_manifest(isolated_accounts, {"a": {"alias": "a", "token_file": ""}})
        service, err = _build_service_noninteractive("a")
        assert service is None
        assert err == ErrorCode.AUTH_EXPIRED

    def test_token_file_missing_on_disk_returns_auth_expired(self, isolated_accounts):
        _write_manifest(isolated_accounts, {"a": {
            "alias": "a",
            "token_file": "accounts/a/token.json",
        }})
        service, err = _build_service_noninteractive("a")
        assert service is None
        assert err == ErrorCode.AUTH_EXPIRED


class TestCorruptedToken:
    def test_corrupted_token_returns_auth_expired_and_does_not_delete(
        self, isolated_accounts, monkeypatch,
    ):
        _write_manifest(isolated_accounts, {"a": {
            "alias": "a",
            "token_file": "accounts/a/token.json",
        }})
        token_file = _write_token_file(isolated_accounts, "accounts/a/token.json", {
            "not": "a valid token shape",
        })
        # Credentials.from_authorized_user_file raises on schema mismatch.
        def _boom(path, scopes):
            raise ValueError("missing required fields")
        monkeypatch.setattr(gsc_server.Credentials, "from_authorized_user_file", _boom)

        service, err = _build_service_noninteractive("a")
        assert service is None
        assert err == ErrorCode.AUTH_EXPIRED
        # CRITICAL: file must still exist. Deletion would stop the user
        # from inspecting / fixing it by hand.
        assert token_file.exists()


class TestHappyPath:
    def test_valid_creds_returns_service(self, isolated_accounts, monkeypatch):
        _write_manifest(isolated_accounts, {"a": {
            "alias": "a",
            "token_file": "accounts/a/token.json",
        }})
        _write_token_file(isolated_accounts, "accounts/a/token.json", {"placeholder": True})

        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.expired = False
        monkeypatch.setattr(
            gsc_server.Credentials, "from_authorized_user_file",
            lambda path, scopes: mock_creds,
        )
        sentinel = object()
        monkeypatch.setattr(gsc_server, "build", lambda *a, **kw: sentinel)

        service, err = _build_service_noninteractive("a")
        assert service is sentinel
        assert err is None


class TestRefreshFlow:
    def test_expired_refresh_success_persists_token(self, isolated_accounts, monkeypatch):
        _write_manifest(isolated_accounts, {"a": {
            "alias": "a",
            "token_file": "accounts/a/token.json",
        }})
        token_file = _write_token_file(
            isolated_accounts, "accounts/a/token.json", {"stale": True},
        )

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "refresh-xyz"
        mock_creds.to_json = MagicMock(return_value='{"refreshed": true}')

        # refresh() flips valid to True to simulate a successful refresh.
        def _do_refresh(request):
            mock_creds.valid = True
            mock_creds.expired = False
        mock_creds.refresh = MagicMock(side_effect=_do_refresh)

        monkeypatch.setattr(
            gsc_server.Credentials, "from_authorized_user_file",
            lambda path, scopes: mock_creds,
        )
        sentinel = object()
        monkeypatch.setattr(gsc_server, "build", lambda *a, **kw: sentinel)

        service, err = _build_service_noninteractive("a")
        assert service is sentinel
        assert err is None
        mock_creds.refresh.assert_called_once()
        # Refreshed token was persisted to disk.
        assert token_file.read_text() == '{"refreshed": true}'

    def test_expired_refresh_failure_returns_auth_expired_and_keeps_token(
        self, isolated_accounts, monkeypatch,
    ):
        _write_manifest(isolated_accounts, {"a": {
            "alias": "a",
            "token_file": "accounts/a/token.json",
        }})
        original_bytes = json.dumps({"stale": True})
        token_file = _write_token_file(
            isolated_accounts, "accounts/a/token.json", {"stale": True},
        )

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "refresh-xyz"
        mock_creds.refresh = MagicMock(side_effect=RuntimeError("network dead"))

        monkeypatch.setattr(
            gsc_server.Credentials, "from_authorized_user_file",
            lambda path, scopes: mock_creds,
        )

        service, err = _build_service_noninteractive("a")
        assert service is None
        assert err == ErrorCode.AUTH_EXPIRED
        # CRITICAL: original token must be intact — a transient refresh
        # error must not force a full re-auth.
        assert token_file.exists()
        assert token_file.read_text() == original_bytes

    def test_no_refresh_token_returns_auth_expired(self, isolated_accounts, monkeypatch):
        _write_manifest(isolated_accounts, {"a": {
            "alias": "a",
            "token_file": "accounts/a/token.json",
        }})
        _write_token_file(isolated_accounts, "accounts/a/token.json", {"stale": True})

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = None  # no refresh token available

        monkeypatch.setattr(
            gsc_server.Credentials, "from_authorized_user_file",
            lambda path, scopes: mock_creds,
        )

        service, err = _build_service_noninteractive("a")
        assert service is None
        assert err == ErrorCode.AUTH_EXPIRED


class TestSafetyContract:
    """Negative assertions — the helper must NOT touch dangerous paths."""

    def test_never_calls_installed_app_flow(self, isolated_accounts, monkeypatch):
        """The whole point of non-interactive — a browser prompt would
        wedge an MCP subprocess indefinitely."""
        _write_manifest(isolated_accounts, {"a": {
            "alias": "a",
            "token_file": "accounts/a/token.json",
        }})
        _write_token_file(isolated_accounts, "accounts/a/token.json", {"stale": True})

        browser_launched = {"called": False}

        def _tripwire(*args, **kwargs):
            browser_launched["called"] = True
            raise AssertionError("InstalledAppFlow must not be invoked")

        monkeypatch.setattr(gsc_server.InstalledAppFlow, "from_client_secrets_file", _tripwire)
        monkeypatch.setattr(gsc_server, "_start_oauth_flow", _tripwire)
        # Force the "cannot refresh" branch.
        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = None
        monkeypatch.setattr(
            gsc_server.Credentials, "from_authorized_user_file",
            lambda path, scopes: mock_creds,
        )

        service, err = _build_service_noninteractive("a")
        assert service is None
        assert err == ErrorCode.AUTH_EXPIRED
        assert browser_launched["called"] is False

    def test_never_falls_back_to_service_account(self, isolated_accounts, monkeypatch):
        """Service-account fallback satisfies an alias-routed call with
        different credentials — classic confused-deputy risk."""
        _write_manifest(isolated_accounts, {"a": {
            "alias": "a",
            "token_file": "accounts/a/token.json",
        }})
        _write_token_file(isolated_accounts, "accounts/a/token.json", {"stale": True})

        sa_loaded = {"called": False}

        def _sa_tripwire(*args, **kwargs):
            sa_loaded["called"] = True
            raise AssertionError("service_account must not be loaded")

        monkeypatch.setattr(
            gsc_server.service_account.Credentials,
            "from_service_account_file",
            _sa_tripwire,
        )
        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = None
        monkeypatch.setattr(
            gsc_server.Credentials, "from_authorized_user_file",
            lambda path, scopes: mock_creds,
        )

        service, err = _build_service_noninteractive("a")
        assert service is None
        assert err == ErrorCode.AUTH_EXPIRED
        assert sa_loaded["called"] is False

    def test_build_failure_returns_service_unavailable(self, isolated_accounts, monkeypatch):
        """Errors after a successful creds load surface as transient
        (retryable) rather than AUTH_EXPIRED (which would be misleading)."""
        _write_manifest(isolated_accounts, {"a": {
            "alias": "a",
            "token_file": "accounts/a/token.json",
        }})
        _write_token_file(isolated_accounts, "accounts/a/token.json", {"ok": True})

        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.expired = False
        monkeypatch.setattr(
            gsc_server.Credentials, "from_authorized_user_file",
            lambda path, scopes: mock_creds,
        )

        def _build_explodes(*args, **kwargs):
            raise RuntimeError("discovery doc unreachable")
        monkeypatch.setattr(gsc_server, "build", _build_explodes)

        service, err = _build_service_noninteractive("a")
        assert service is None
        assert err == ErrorCode.SERVICE_UNAVAILABLE

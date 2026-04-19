"""F8: default → legacy migration.

Two upgrade paths:

1. Fresh install with legacy ``token.json`` at the repo root → copied
   into ``accounts/legacy/token.json`` with alias ``legacy`` (NOT the
   old ``default``).
2. Pre-v1.2.0 manifest with an existing ``default`` alias → alias
   renamed to ``legacy`` in-place; token file path left untouched.

Plus:

- ``gsc_add_account("default")`` is rejected.
- Migration is idempotent (second call is a no-op).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import gsc_server
from gsc_server import gsc_add_account


def _write_token(path: Path, contents: str = '{"stub": true}') -> None:
    path.write_text(contents)


def _manifest_path() -> Path:
    return Path(gsc_server.ACCOUNTS_MANIFEST)


class TestLegacyFromBareToken:
    async def test_fresh_install_registers_legacy_alias(self, monkeypatch):
        """Pre-v1.2.0 layout: a bare ``token.json`` next to the script.
        Migration must register it as alias ``legacy``, NOT ``default``.
        """
        _write_token(Path(gsc_server.TOKEN_FILE))
        # Reset the migration guard so it runs for this test.
        monkeypatch.setattr(gsc_server, "_migration_checked", False)

        gsc_server._migrate_legacy_state()

        manifest = json.loads(_manifest_path().read_text())
        assert "legacy" in manifest.get("accounts", {})
        assert "default" not in manifest.get("accounts", {})
        assert manifest["accounts"]["legacy"]["token_file"] == "accounts/legacy/token.json"
        # Token file copied to its new home.
        assert (Path(gsc_server.ACCOUNTS_DIR) / "legacy" / "token.json").exists()
        # Original token.json preserved for rollback.
        assert Path(gsc_server.TOKEN_FILE).exists()
        # v1.2.0 drops active_account.
        assert "active_account" not in manifest


class TestDefaultToLegacyRename:
    async def test_existing_default_alias_renamed(self, monkeypatch):
        """v1.1.x manifest with alias 'default' → rename to 'legacy'."""
        manifest_path = _manifest_path()
        manifest_path.parent.mkdir(exist_ok=True, parents=True)
        manifest_path.write_text(json.dumps({
            "active_account": "default",
            "accounts": {
                "default": {
                    "alias": "default",
                    "email": "x@y.com",
                    "token_file": "accounts/default/token.json",
                    "added_at": "2026-01-01",
                },
            },
        }))
        monkeypatch.setattr(gsc_server, "_migration_checked", False)

        gsc_server._migrate_legacy_state()

        after = json.loads(manifest_path.read_text())
        assert "default" not in after["accounts"]
        assert "legacy" in after["accounts"]
        # Token-file path preserved (no filesystem churn).
        assert after["accounts"]["legacy"]["token_file"] == "accounts/default/token.json"
        # active_account field is dropped.
        assert "active_account" not in after

    async def test_legacy_alias_collision_uses_timestamped_suffix(self, monkeypatch):
        """If both 'default' AND 'legacy' are configured, the rename
        must not overwrite the existing 'legacy'."""
        manifest_path = _manifest_path()
        manifest_path.parent.mkdir(exist_ok=True, parents=True)
        manifest_path.write_text(json.dumps({
            "accounts": {
                "default": {
                    "alias": "default",
                    "token_file": "accounts/default/token.json",
                },
                "legacy": {
                    "alias": "legacy",
                    "token_file": "accounts/legacy/token.json",
                },
            },
        }))
        monkeypatch.setattr(gsc_server, "_migration_checked", False)

        gsc_server._migrate_legacy_state()

        after = json.loads(manifest_path.read_text())
        assert "default" not in after["accounts"]
        assert "legacy" in after["accounts"]  # original preserved
        # And there's a timestamped rename of the old default.
        timestamped = [a for a in after["accounts"] if a.startswith("legacy_")]
        assert len(timestamped) == 1

    async def test_legacy_timestamp_collision_uses_counter_suffix(self, monkeypatch):
        """F14: double-collision case. Both 'legacy' AND
        'legacy_<ts>' already exist (the timestamp was burned by an
        earlier migration at the same second). Migration must not
        silently overwrite — counter-suffix loop closes the window."""
        from unittest.mock import patch
        import datetime as dt

        fixed_ts = 1700000000
        # Pre-populate manifest with default, legacy, AND legacy_<ts>.
        manifest_path = _manifest_path()
        manifest_path.parent.mkdir(exist_ok=True, parents=True)
        manifest_path.write_text(json.dumps({
            "accounts": {
                "default": {"alias": "default", "token_file": "accounts/default/token.json"},
                "legacy": {"alias": "legacy", "token_file": "accounts/legacy/token.json"},
                f"legacy_{fixed_ts}": {
                    "alias": f"legacy_{fixed_ts}",
                    "token_file": f"accounts/legacy_{fixed_ts}/token.json",
                },
            },
        }))
        monkeypatch.setattr(gsc_server, "_migration_checked", False)

        class _FrozenDatetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt.datetime.fromtimestamp(fixed_ts, tz=tz)
        monkeypatch.setattr(gsc_server, "datetime", _FrozenDatetime)

        gsc_server._migrate_legacy_state()

        after = json.loads(manifest_path.read_text())
        assert "default" not in after["accounts"]
        assert "legacy" in after["accounts"]
        assert f"legacy_{fixed_ts}" in after["accounts"]
        # Counter-suffixed landing spot picked up the old "default".
        assert f"legacy_{fixed_ts}_1" in after["accounts"]


class TestIdempotence:
    async def test_running_twice_is_noop(self, monkeypatch):
        manifest_path = _manifest_path()
        manifest_path.parent.mkdir(exist_ok=True, parents=True)
        manifest_path.write_text(json.dumps({
            "accounts": {
                "default": {"alias": "default", "token_file": "accounts/default/token.json"},
            },
        }))
        monkeypatch.setattr(gsc_server, "_migration_checked", False)

        gsc_server._migrate_legacy_state()
        first = json.loads(manifest_path.read_text())

        # Reset guard and re-run.
        monkeypatch.setattr(gsc_server, "_migration_checked", False)
        gsc_server._migrate_legacy_state()
        second = json.loads(manifest_path.read_text())

        # Second run finds no "default" → no further changes.
        assert first == second


class TestRejectDefault:
    async def test_gsc_add_account_rejects_default_alias(self, monkeypatch):
        """The user-facing tool must refuse to create a 'default' alias
        even though _validate_alias alone would accept it as a syntax
        match."""
        out = await gsc_add_account("default")
        assert out.startswith("Error:")
        assert "reserved" in out.lower()

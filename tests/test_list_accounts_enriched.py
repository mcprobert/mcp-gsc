"""F6: gsc_list_accounts enrichment.

v1.2.0: ``include_properties`` defaults to False for privacy + speed.
When True, the output includes the property list per account. The
JSON envelope also carries ``property_count`` (from cache when warm;
null when not) regardless of ``include_properties``.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import gsc_server
from gsc_server import gsc_list_accounts


def _write_manifest(accounts: dict) -> None:
    dir_path = Path(gsc_server.ACCOUNTS_DIR)
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "accounts.json").write_text(json.dumps({"accounts": accounts}))


def _seed_cache(alias: str, state: str, *, properties=None):
    gsc_server._account_property_state[alias] = state
    if state == "ok":
        gsc_server._account_properties[alias] = set(properties or [])


class TestJsonShape:
    async def test_empty_manifest_json(self, monkeypatch):
        out = await gsc_list_accounts(response_format="json")
        assert out == {
            "ok": True,
            "tool": "gsc_list_accounts",
            "accounts": [],
            "meta": {"account_count": 0},
        }

    async def test_include_properties_false_is_default(self, monkeypatch):
        _write_manifest({
            "a": {"alias": "a", "email": "a@x", "token_file": "t", "added_at": "2026-01-01"},
        })
        _seed_cache("a", "ok", properties=["sc-domain:one.com"])

        out = await gsc_list_accounts(response_format="json")
        assert len(out["accounts"]) == 1
        entry = out["accounts"][0]
        assert entry["alias"] == "a"
        assert entry["property_count"] == 1  # from warm cache
        # Default excludes the properties list.
        assert "properties" not in entry
        assert out["meta"]["include_properties"] is False

    async def test_include_properties_true_adds_list(self, monkeypatch):
        _write_manifest({
            "a": {"alias": "a", "email": "a@x", "token_file": "t", "added_at": "2026-01-01"},
        })
        _seed_cache("a", "ok", properties=["sc-domain:one.com", "sc-domain:two.com"])

        # Stub _ensure_property_cache (it would try to auth otherwise).
        async def _noop(alias, *, force_refresh=False):
            return None
        monkeypatch.setattr(gsc_server, "_ensure_property_cache", _noop)

        out = await gsc_list_accounts(include_properties=True, response_format="json")
        entry = out["accounts"][0]
        assert entry["property_count"] == 2
        assert entry["properties"] == ["sc-domain:one.com", "sc-domain:two.com"]
        assert entry["discovery_state"] == "ok"

    async def test_property_count_null_when_cache_cold(self, monkeypatch):
        _write_manifest({
            "a": {"alias": "a", "email": "a@x", "token_file": "t", "added_at": "2026-01-01"},
        })
        # No cache seeded — state is "never" (missing from dict).

        out = await gsc_list_accounts(response_format="json")
        entry = out["accounts"][0]
        assert entry["property_count"] is None  # not force-warmed

    async def test_include_properties_surfaces_discovery_error(self, monkeypatch):
        _write_manifest({
            "a": {"alias": "a", "email": "a@x", "token_file": "t", "added_at": "2026-01-01"},
        })
        _seed_cache("a", "error")
        gsc_server._account_property_error["a"] = "AUTH_EXPIRED"

        async def _noop(alias, *, force_refresh=False):
            return None
        monkeypatch.setattr(gsc_server, "_ensure_property_cache", _noop)

        out = await gsc_list_accounts(include_properties=True, response_format="json")
        entry = out["accounts"][0]
        assert entry["discovery_state"] == "error"
        assert entry["discovery_error"] == "AUTH_EXPIRED"
        # properties is null for error-state accounts — never conflate with empty.
        assert entry["properties"] is None


class TestMarkdown:
    async def test_markdown_has_no_active_marker_in_v12(self, monkeypatch):
        _write_manifest({
            "a": {"alias": "a", "email": "a@x", "token_file": "t", "added_at": "2026-01-01"},
            "b": {"alias": "b", "email": "b@x", "token_file": "t", "added_at": "2026-01-01"},
        })
        out = await gsc_list_accounts()
        # "active" concept removed; the marker must not appear.
        assert "(active)" not in out

    async def test_markdown_shows_property_count_when_cache_warm(self, monkeypatch):
        _write_manifest({
            "a": {"alias": "a", "email": "a@x", "token_file": "t", "added_at": "2026-01-01"},
        })
        _seed_cache("a", "ok", properties=["sc-domain:one.com", "sc-domain:two.com"])

        out = await gsc_list_accounts()
        assert "property_count: 2" in out

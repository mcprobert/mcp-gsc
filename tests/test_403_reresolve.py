"""F4: 403 stale-positive recovery (``_call_with_stale_retry``).

When an auto-resolved call gets a 403 back from Google, the cache was
wrong — the user likely revoked access to this property on the picked
account. The helper invalidates that alias's cache, re-resolves, and
retries if resolution picks a different alias.

Explicit-alias calls do NOT retry — the caller chose that credential.

Tests cover four branches:
1. Explicit alias + 403 → no retry, 403 propagates.
2. Auto + 403 + re-resolve picks SAME alias → no retry, 403 propagates.
3. Auto + 403 + re-resolve picks DIFFERENT alias → retry, success.
4. Auto + 403 + re-resolve raises → original 403 surfaces.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

import gsc_server
from gsc_server import (
    AccountResolverError,
    ErrorCode,
    _call_with_stale_retry,
    _invalidate_property_cache,
)


def _make_http_403(message: str = "access denied") -> HttpError:
    resp = MagicMock()
    resp.status = 403
    resp.get = MagicMock(return_value=None)
    return HttpError(resp=resp, content=f'{{"error": {{"message": "{message}"}}}}'.encode())


def _seed_cache(alias: str, state: str, *, properties=None):
    gsc_server._account_property_state[alias] = state
    if state == "ok":
        gsc_server._account_properties[alias] = set(properties or [])


def _write_manifest(tmp_path: Path, accounts: dict) -> None:
    manifest = {"accounts": accounts}
    (tmp_path / "accounts" / "accounts.json").write_text(json.dumps(manifest))


class TestStaleRetry:
    async def test_explicit_alias_403_does_not_retry(self, monkeypatch, tmp_path):
        _write_manifest(Path(gsc_server.ACCOUNTS_DIR).parent, {
            "a": {"alias": "a", "token_file": "t"},
        })
        _seed_cache("a", "ok", properties=["sc-domain:ex.com"])

        call_count = {"n": 0}
        sentinel_service = MagicMock()

        def _api(svc):
            call_count["n"] += 1
            raise _make_http_403()
        monkeypatch.setattr(
            gsc_server, "_build_service_noninteractive",
            lambda alias: (sentinel_service, None),
        )
        with pytest.raises(HttpError):
            await _call_with_stale_retry(
                site_url="sc-domain:ex.com",
                account_alias="a",        # explicit
                api_call=_api,
            )
        # Explicit alias: no retry. Exactly one API call.
        assert call_count["n"] == 1

    async def test_auto_403_same_alias_after_reresolve_does_not_retry(
        self, monkeypatch,
    ):
        _write_manifest(Path(gsc_server.ACCOUNTS_DIR).parent, {
            "a": {"alias": "a", "token_file": "t"},
        })
        _seed_cache("a", "ok", properties=["sc-domain:ex.com"])

        call_count = {"n": 0}
        sentinel_service = MagicMock()

        def _api(svc):
            call_count["n"] += 1
            raise _make_http_403()

        def _build(alias):
            return sentinel_service, None
        monkeypatch.setattr(gsc_server, "_build_service_noninteractive", _build)

        # Respect state: only run when cache is "never" (post-invalidate).
        # After invalidate, re-refresh rehydrates with the SAME access set,
        # so re-resolve picks "a" again.
        async def _refresh(alias, *, force_refresh=False):
            state = gsc_server._account_property_state.get(alias, "never")
            if not force_refresh and state in ("ok", "error"):
                return
            _seed_cache("a", "ok", properties=["sc-domain:ex.com"])
        monkeypatch.setattr(gsc_server, "_ensure_property_cache", _refresh)

        with pytest.raises(HttpError):
            await _call_with_stale_retry(
                site_url="sc-domain:ex.com",
                account_alias=None,
                api_call=_api,
            )
        # Re-resolution picks same alias → no retry.
        assert call_count["n"] == 1

    async def test_auto_403_different_alias_after_reresolve_retries(
        self, monkeypatch,
    ):
        _write_manifest(Path(gsc_server.ACCOUNTS_DIR).parent, {
            "a": {"alias": "a", "token_file": "ta"},
            "b": {"alias": "b", "token_file": "tb"},
        })
        # Cache says both have access — AMBIGUOUS would normally trigger.
        # We set only "a" as candidate to force unique resolution.
        _seed_cache("a", "ok", properties=["sc-domain:ex.com"])
        _seed_cache("b", "ok", properties=["sc-domain:other.com"])

        first_service = MagicMock(name="service-a")
        second_service = MagicMock(name="service-b")
        builds = {"calls": []}

        def _build(alias):
            builds["calls"].append(alias)
            if alias == "a":
                return first_service, None
            return second_service, None
        monkeypatch.setattr(gsc_server, "_build_service_noninteractive", _build)

        # Respect state so the initial seeded cache survives the first
        # resolve. After invalidate of "a", refresh shows "a" empty and
        # "b" now having the property — so re-resolve picks "b".
        async def _refresh(alias, *, force_refresh=False):
            state = gsc_server._account_property_state.get(alias, "never")
            if not force_refresh and state in ("ok", "error"):
                return
            if alias == "a":
                _seed_cache("a", "ok", properties=[])
            elif alias == "b":
                _seed_cache("b", "ok", properties=["sc-domain:ex.com"])
        monkeypatch.setattr(gsc_server, "_ensure_property_cache", _refresh)

        calls = {"n": 0, "services": []}

        def _api(svc):
            calls["n"] += 1
            calls["services"].append(svc)
            if calls["n"] == 1:
                raise _make_http_403()
            return {"result": "ok"}

        resolved_alias, service, result = await _call_with_stale_retry(
            site_url="sc-domain:ex.com",
            account_alias=None,
            api_call=_api,
        )
        assert calls["n"] == 2  # first call 403, second call success
        assert calls["services"][0] is first_service
        assert calls["services"][1] is second_service
        assert resolved_alias == "b"
        assert result == {"result": "ok"}

    async def test_auto_403_reresolve_raises_surfaces_original_403(
        self, monkeypatch,
    ):
        _write_manifest(Path(gsc_server.ACCOUNTS_DIR).parent, {
            "a": {"alias": "a", "token_file": "t"},
        })
        _seed_cache("a", "ok", properties=["sc-domain:ex.com"])

        sentinel_service = MagicMock()
        monkeypatch.setattr(
            gsc_server, "_build_service_noninteractive",
            lambda alias: (sentinel_service, None),
        )

        # After invalidate, re-resolve raises NO_ACCOUNT_FOR_PROPERTY
        # (access really gone). Respect state so the initial seeded cache
        # is still active on the first resolve.
        async def _refresh(alias, *, force_refresh=False):
            state = gsc_server._account_property_state.get(alias, "never")
            if not force_refresh and state in ("ok", "error"):
                return
            _seed_cache("a", "ok", properties=[])
        monkeypatch.setattr(gsc_server, "_ensure_property_cache", _refresh)

        def _api(svc):
            raise _make_http_403()

        # Resolver will find zero candidates + no errors → NO_ACCOUNT_FOR_PROPERTY
        # which the helper converts back to the ORIGINAL HttpError so the tool's
        # existing HttpError envelope path owns the surface.
        with pytest.raises(HttpError):
            await _call_with_stale_retry(
                site_url="sc-domain:ex.com",
                account_alias=None,
                api_call=_api,
            )

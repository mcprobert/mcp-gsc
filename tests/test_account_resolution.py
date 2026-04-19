"""F3: AccountResolver + tri-state property cache.

Verifies the three rules the review caught:

- "error" state is NOT conflated with "known empty" — a transient
  discovery 500 must surface as ACCOUNT_RESOLUTION_INCOMPLETE, not as
  NO_ACCOUNT_FOR_PROPERTY.
- Partial cache must not declare unique auto-resolution until every
  non-candidate alias is known-good negative.
- Per-alias lock: concurrent resolves for the same cold alias trigger
  exactly one discovery call.

Plus the plain happy / mismatch / ambiguous / missing paths.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import gsc_server
from gsc_server import (
    AccountResolverError,
    ErrorCode,
    _resolve_account,
    _ensure_property_cache,
    get_gsc_service_for_site,
    _invalidate_property_cache,
)


@pytest.fixture
def resolver_env(monkeypatch):
    """Reuse the autouse-isolated paths from conftest; just reset the
    per-alias lock mutex so concurrency tests see a clean slate."""
    monkeypatch.setattr(gsc_server, "_alias_locks_mutex", asyncio.Lock())
    return Path(gsc_server.ACCOUNTS_DIR).parent


def _write_manifest(tmp_path: Path, accounts: dict) -> None:
    manifest = {"accounts": accounts}
    (tmp_path / "accounts" / "accounts.json").write_text(json.dumps(manifest))


def _seed_cache(alias: str, state: str, *, properties=None, error=None):
    """Directly seed the cache — bypasses _ensure_property_cache so tests
    can exercise resolver rules without mocking auth + discovery."""
    gsc_server._account_property_state[alias] = state
    if state == "ok":
        gsc_server._account_properties[alias] = set(properties or [])
    else:
        gsc_server._account_properties.pop(alias, None)
    if state == "error":
        gsc_server._account_property_error[alias] = error or ErrorCode.AUTH_EXPIRED
    else:
        gsc_server._account_property_error.pop(alias, None)


# ---------------------------------------------------------------------------
# Empty manifest + bad input
# ---------------------------------------------------------------------------


class TestEmptyManifest:
    async def test_empty_manifest_raises_no_accounts_configured(self, resolver_env):
        _write_manifest(resolver_env, {})
        with pytest.raises(AccountResolverError) as exc:
            await _resolve_account("sc-domain:example.com", None)
        assert exc.value.code == ErrorCode.NO_ACCOUNTS_CONFIGURED


class TestExplicitAliasValidation:
    async def test_invalid_alias_format_raises_bad_request(self, resolver_env):
        _write_manifest(resolver_env, {"real": {"alias": "real", "token_file": "t"}})
        with pytest.raises(AccountResolverError) as exc:
            await _resolve_account("sc-domain:ex.com", "Not Valid!")
        assert exc.value.code == ErrorCode.BAD_REQUEST

    async def test_unknown_alias_raises_account_site_mismatch(self, resolver_env):
        _write_manifest(resolver_env, {"real": {"alias": "real", "token_file": "t"}})
        with pytest.raises(AccountResolverError) as exc:
            await _resolve_account("sc-domain:ex.com", "ghost")
        assert exc.value.code == ErrorCode.ACCOUNT_SITE_MISMATCH
        assert "real" in (exc.value.alternatives or [])


# ---------------------------------------------------------------------------
# Explicit alias path
# ---------------------------------------------------------------------------


class TestExplicitAliasHappyAndMismatch:
    async def test_explicit_alias_with_access_returns_alias(self, resolver_env):
        _write_manifest(resolver_env, {"a": {"alias": "a", "token_file": "t"}})
        _seed_cache("a", "ok", properties=["sc-domain:ex.com"])
        alias = await _resolve_account("sc-domain:ex.com", "a")
        assert alias == "a"

    async def test_explicit_alias_without_access_raises_mismatch(self, resolver_env):
        _write_manifest(resolver_env, {"a": {"alias": "a", "token_file": "t"}})
        _seed_cache("a", "ok", properties=["sc-domain:other.com"])
        with pytest.raises(AccountResolverError) as exc:
            await _resolve_account("sc-domain:ex.com", "a")
        assert exc.value.code == ErrorCode.ACCOUNT_SITE_MISMATCH

    async def test_explicit_alias_in_error_state_surfaces_auth_not_mismatch(
        self, resolver_env,
    ):
        """Critical per review: a transient discovery failure must NOT
        masquerade as ACCOUNT_SITE_MISMATCH. That would lie about the
        routing decision."""
        _write_manifest(resolver_env, {"a": {"alias": "a", "token_file": "t"}})
        _seed_cache("a", "error", error=ErrorCode.AUTH_EXPIRED)
        with pytest.raises(AccountResolverError) as exc:
            await _resolve_account("sc-domain:ex.com", "a")
        assert exc.value.code == ErrorCode.AUTH_EXPIRED


# ---------------------------------------------------------------------------
# Auto-resolve path
# ---------------------------------------------------------------------------


class TestAutoResolve:
    async def test_single_candidate_returns_it(self, resolver_env):
        _write_manifest(resolver_env, {
            "a": {"alias": "a", "token_file": "t1"},
            "b": {"alias": "b", "token_file": "t2"},
        })
        _seed_cache("a", "ok", properties=["sc-domain:ex.com"])
        _seed_cache("b", "ok", properties=["sc-domain:other.com"])
        alias = await _resolve_account("sc-domain:ex.com", None)
        assert alias == "a"

    async def test_multiple_candidates_raise_ambiguous(self, resolver_env):
        _write_manifest(resolver_env, {
            "a": {"alias": "a", "token_file": "t"},
            "b": {"alias": "b", "token_file": "t"},
        })
        _seed_cache("a", "ok", properties=["sc-domain:ex.com"])
        _seed_cache("b", "ok", properties=["sc-domain:ex.com"])
        with pytest.raises(AccountResolverError) as exc:
            await _resolve_account("sc-domain:ex.com", None)
        assert exc.value.code == ErrorCode.AMBIGUOUS_ACCOUNT
        assert set(exc.value.alternatives or []) == {"a", "b"}

    async def test_zero_candidates_all_known_good_raises_no_account(
        self, resolver_env, monkeypatch,
    ):
        _write_manifest(resolver_env, {
            "a": {"alias": "a", "token_file": "t"},
        })
        _seed_cache("a", "ok", properties=["sc-domain:other.com"])
        # Stub the force-refresh call to keep cache unchanged.
        async def _noop_refresh(alias, *, force_refresh=False):
            return None
        monkeypatch.setattr(gsc_server, "_ensure_property_cache", _noop_refresh)

        with pytest.raises(AccountResolverError) as exc:
            await _resolve_account("sc-domain:ex.com", None)
        assert exc.value.code == ErrorCode.NO_ACCOUNT_FOR_PROPERTY

    async def test_one_candidate_plus_errors_raises_incomplete(
        self, resolver_env, monkeypatch,
    ):
        """Partial-cache false-uniqueness: must not return a unique alias
        while some aliases are in error state — they might also match."""
        _write_manifest(resolver_env, {
            "a": {"alias": "a", "token_file": "t"},
            "b": {"alias": "b", "token_file": "t"},
            "c": {"alias": "c", "token_file": "t"},
        })
        _seed_cache("a", "ok", properties=["sc-domain:ex.com"])
        _seed_cache("b", "ok", properties=["sc-domain:other.com"])
        _seed_cache("c", "error", error=ErrorCode.AUTH_EXPIRED)

        async def _noop_refresh(alias, *, force_refresh=False):
            return None
        monkeypatch.setattr(gsc_server, "_ensure_property_cache", _noop_refresh)

        with pytest.raises(AccountResolverError) as exc:
            await _resolve_account("sc-domain:ex.com", None)
        assert exc.value.code == ErrorCode.ACCOUNT_RESOLUTION_INCOMPLETE
        assert "c" in (exc.value.alternatives or [])

    async def test_zero_candidates_with_errors_raises_incomplete(
        self, resolver_env, monkeypatch,
    ):
        _write_manifest(resolver_env, {
            "a": {"alias": "a", "token_file": "t"},
            "b": {"alias": "b", "token_file": "t"},
        })
        _seed_cache("a", "ok", properties=["sc-domain:other.com"])
        _seed_cache("b", "error", error=ErrorCode.SERVICE_UNAVAILABLE)

        async def _noop_refresh(alias, *, force_refresh=False):
            return None
        monkeypatch.setattr(gsc_server, "_ensure_property_cache", _noop_refresh)

        with pytest.raises(AccountResolverError) as exc:
            await _resolve_account("sc-domain:ex.com", None)
        assert exc.value.code == ErrorCode.ACCOUNT_RESOLUTION_INCOMPLETE

    async def test_force_refresh_discovers_new_property(self, resolver_env, monkeypatch):
        """Property added after first discovery: zero candidates on the
        initial scan triggers a force-refresh, which reveals the match."""
        _write_manifest(resolver_env, {
            "a": {"alias": "a", "token_file": "t"},
        })
        _seed_cache("a", "ok", properties=["sc-domain:other.com"])

        async def _force_refresh(alias, *, force_refresh=False):
            if force_refresh:
                _seed_cache(alias, "ok", properties=["sc-domain:ex.com"])
        monkeypatch.setattr(gsc_server, "_ensure_property_cache", _force_refresh)

        alias = await _resolve_account("sc-domain:ex.com", None)
        assert alias == "a"


# ---------------------------------------------------------------------------
# Per-alias lock (concurrency)
# ---------------------------------------------------------------------------


class TestPerAliasLock:
    async def test_concurrent_ensure_triggers_one_refresh_per_alias(
        self, resolver_env, monkeypatch,
    ):
        _write_manifest(resolver_env, {"a": {"alias": "a", "token_file": "t"}})

        call_count = {"n": 0}

        def _fake_build(alias):
            call_count["n"] += 1
            mock_service = MagicMock()
            mock_service.sites.return_value.list.return_value.execute.return_value = {
                "siteEntry": [{"siteUrl": "sc-domain:ex.com"}],
            }
            return mock_service, None
        monkeypatch.setattr(gsc_server, "_build_service_noninteractive", _fake_build)

        # Fire two concurrent resolves for the same cold alias.
        results = await asyncio.gather(
            _ensure_property_cache("a"),
            _ensure_property_cache("a"),
        )
        assert results == [None, None]
        # Per-alias lock + post-acquire double-check means the second
        # caller sees state=="ok" and skips the refresh.
        assert call_count["n"] == 1
        assert gsc_server._account_property_state["a"] == "ok"


# ---------------------------------------------------------------------------
# AccountResolverError.to_envelope
# ---------------------------------------------------------------------------


class TestResolverErrorEnvelope:
    def test_envelope_has_standard_fields(self):
        err = AccountResolverError(
            code=ErrorCode.AMBIGUOUS_ACCOUNT,
            error="two candidates",
            hint="pick one",
            alternatives=["a", "b"],
            site_url="sc-domain:ex.com",
        )
        env = err.to_envelope(tool="gsc_get_search_analytics")
        assert env["ok"] is False
        assert env["error_code"] == ErrorCode.AMBIGUOUS_ACCOUNT
        assert env["error"] == "two candidates"
        assert env["hint"] == "pick one"
        assert env["alternatives"] == ["a", "b"]
        assert env["site_url"] == "sc-domain:ex.com"
        assert env["tool"] == "gsc_get_search_analytics"
        # retryable derived from code (AMBIGUOUS is NOT retryable).
        assert env["retryable"] is False


# ---------------------------------------------------------------------------
# get_gsc_service_for_site
# ---------------------------------------------------------------------------


class TestServiceForSite:
    async def test_happy_returns_alias_and_service(self, resolver_env, monkeypatch):
        _write_manifest(resolver_env, {"a": {"alias": "a", "token_file": "t"}})
        _seed_cache("a", "ok", properties=["sc-domain:ex.com"])

        sentinel = object()
        monkeypatch.setattr(
            gsc_server, "_build_service_noninteractive",
            lambda alias: (sentinel, None),
        )
        alias, service = await get_gsc_service_for_site("sc-domain:ex.com", None)
        assert alias == "a"
        assert service is sentinel

    async def test_resolver_error_propagates(self, resolver_env):
        _write_manifest(resolver_env, {})
        with pytest.raises(AccountResolverError) as exc:
            await get_gsc_service_for_site("sc-domain:ex.com", None)
        assert exc.value.code == ErrorCode.NO_ACCOUNTS_CONFIGURED

    async def test_post_resolve_auth_race_surfaces_transient(
        self, resolver_env, monkeypatch,
    ):
        """Resolver said state=='ok', but by the time we build creds the
        token has expired. Caller should see a transient error, not a
        mismatch."""
        _write_manifest(resolver_env, {"a": {"alias": "a", "token_file": "t"}})
        _seed_cache("a", "ok", properties=["sc-domain:ex.com"])

        monkeypatch.setattr(
            gsc_server, "_build_service_noninteractive",
            lambda alias: (None, ErrorCode.AUTH_EXPIRED),
        )
        with pytest.raises(AccountResolverError) as exc:
            await get_gsc_service_for_site("sc-domain:ex.com", None)
        assert exc.value.code == ErrorCode.AUTH_EXPIRED

    async def test_post_resolve_auth_race_is_explicitly_retryable(
        self, resolver_env, monkeypatch,
    ):
        """F11: AUTH_EXPIRED is non-retryable by default, but the
        post-resolve race IS genuinely transient (token expired in the
        microseconds between resolver and build). ``get_gsc_service_for_site``
        opts in to retryable=True at this specific call site so agents
        retry once instead of escalating immediately to re-auth."""
        _write_manifest(resolver_env, {"a": {"alias": "a", "token_file": "t"}})
        _seed_cache("a", "ok", properties=["sc-domain:ex.com"])

        monkeypatch.setattr(
            gsc_server, "_build_service_noninteractive",
            lambda alias: (None, ErrorCode.AUTH_EXPIRED),
        )
        with pytest.raises(AccountResolverError) as exc:
            await get_gsc_service_for_site("sc-domain:ex.com", None)
        # retryable flag carries through to the envelope.
        env = exc.value.to_envelope(tool="x")
        assert env["retryable"] is True, (
            "post-resolve race must be retryable even though AUTH_EXPIRED "
            "is non-retryable by default — agents must retry the race once."
        )


# ---------------------------------------------------------------------------
# _invalidate_property_cache
# ---------------------------------------------------------------------------


class TestInvalidate:
    def test_invalidate_resets_state_to_never(self, resolver_env):
        _seed_cache("a", "ok", properties=["sc-domain:ex.com"])
        _invalidate_property_cache("a")
        assert gsc_server._account_property_state["a"] == "never"
        assert "a" not in gsc_server._account_properties

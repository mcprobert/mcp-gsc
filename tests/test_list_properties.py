"""v1.2.1 R1: gsc_list_properties JSON mode.

The CMO analyst flagged that every other tabular tool had a
``response_format='json'`` option emitting the standard
``_format_table`` envelope — `gsc_list_properties` still returned
only markdown, forcing orchestrating agents to text-parse. v1.2.1
adds JSON mode.

Contracts pinned here:

- JSON emits the standard table envelope shape.
- Rows are dicts keyed by `account`, `site_url`, `permission`.
- JSON always tags with `account` (machine-readability); markdown
  drops the tag in the single-account case (human compactness).
- `name_contains` substring filter works in both modes.
- Partial failures surface in ``meta.partial_failures``.
- Empty manifest → structured ``NO_ACCOUNTS_CONFIGURED`` envelope
  in JSON mode; plain string in markdown mode.
- Markdown remains the back-compat default.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import gsc_server
from gsc_server import ErrorCode, gsc_list_properties


def _write_manifest(accounts: dict) -> None:
    dir_path = Path(gsc_server.ACCOUNTS_DIR)
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "accounts.json").write_text(json.dumps({"accounts": accounts}))


def _mock_service(site_entries: list) -> MagicMock:
    service = MagicMock()
    service.sites.return_value.list.return_value.execute.return_value = {
        "siteEntry": site_entries,
    }
    return service


class TestJsonMode:
    async def test_json_mode_returns_table_envelope(self, monkeypatch):
        _write_manifest({"a": {"alias": "a", "token_file": "t"}})
        monkeypatch.setattr(
            gsc_server, "_build_service_noninteractive",
            lambda alias: (_mock_service([
                {"siteUrl": "sc-domain:ex.com", "permissionLevel": "siteOwner"},
            ]), None),
        )
        out = await gsc_list_properties(response_format="json")
        # Standard _format_table shape.
        assert out["ok"] is True
        assert "columns" in out and "rows" in out
        assert out["row_count"] == 1
        assert out["truncated"] is False
        assert "meta" in out
        # Columns in the order produced by _format_table.
        col_keys = [c for c in out["columns"]]
        assert col_keys == ["account", "site_url", "permission"]

    async def test_json_mode_rows_tagged_with_account(self, monkeypatch):
        _write_manifest({
            "a": {"alias": "a", "token_file": "t"},
            "b": {"alias": "b", "token_file": "t"},
        })

        def _build(alias):
            if alias == "a":
                return _mock_service([
                    {"siteUrl": "sc-domain:one.com", "permissionLevel": "siteOwner"},
                ]), None
            return _mock_service([
                {"siteUrl": "sc-domain:two.com", "permissionLevel": "siteFullUser"},
            ]), None
        monkeypatch.setattr(gsc_server, "_build_service_noninteractive", _build)

        out = await gsc_list_properties(response_format="json")
        accounts_in_rows = {r["account"] for r in out["rows"]}
        assert accounts_in_rows == {"a", "b"}
        # Each row has the full triple.
        for row in out["rows"]:
            assert set(row.keys()) == {"account", "site_url", "permission"}

    async def test_json_mode_respects_name_contains_filter(self, monkeypatch):
        _write_manifest({"a": {"alias": "a", "token_file": "t"}})
        monkeypatch.setattr(
            gsc_server, "_build_service_noninteractive",
            lambda alias: (_mock_service([
                {"siteUrl": "sc-domain:whitehat-seo.co.uk", "permissionLevel": "siteOwner"},
                {"siteUrl": "sc-domain:thrive.uk.com", "permissionLevel": "siteFullUser"},
                {"siteUrl": "sc-domain:chaserhq.com", "permissionLevel": "siteFullUser"},
            ]), None),
        )
        out = await gsc_list_properties(name_contains="whitehat", response_format="json")
        assert out["row_count"] == 1
        assert out["rows"][0]["site_url"] == "sc-domain:whitehat-seo.co.uk"
        # meta.name_contains echoes the filter for orchestration debugging.
        assert out["meta"]["name_contains"] == "whitehat"

    async def test_json_mode_single_account_still_tags_rows_for_machine_readability(
        self, monkeypatch,
    ):
        """Markdown drops the ``[alias]`` tag in the single-account case
        because humans don't need to see ``[only] sc-domain:...`` twice.
        JSON must NOT drop the tag — agents shouldn't have to infer the
        owning account from an enclosing context."""
        _write_manifest({"only": {"alias": "only", "token_file": "t"}})
        monkeypatch.setattr(
            gsc_server, "_build_service_noninteractive",
            lambda alias: (_mock_service([
                {"siteUrl": "sc-domain:ex.com", "permissionLevel": "siteOwner"},
            ]), None),
        )
        out = await gsc_list_properties(response_format="json")
        assert out["row_count"] == 1
        assert out["rows"][0]["account"] == "only"

    async def test_json_mode_partial_failures_surface_in_meta(self, monkeypatch):
        _write_manifest({
            "ok_alias": {"alias": "ok_alias", "token_file": "t"},
            "bad_alias": {"alias": "bad_alias", "token_file": "t"},
        })

        def _build(alias):
            if alias == "ok_alias":
                return _mock_service([
                    {"siteUrl": "sc-domain:ex.com", "permissionLevel": "siteOwner"},
                ]), None
            return None, ErrorCode.AUTH_EXPIRED
        monkeypatch.setattr(gsc_server, "_build_service_noninteractive", _build)

        out = await gsc_list_properties(response_format="json")
        assert out["ok"] is True  # partial failure isn't total failure
        assert out["row_count"] == 1  # one account still produced data
        fails = out["meta"]["partial_failures"]
        assert len(fails) == 1
        assert fails[0] == {"account": "bad_alias", "error_code": "AUTH_EXPIRED"}

    async def test_json_mode_empty_manifest_returns_no_accounts_configured_envelope(self):
        # No manifest written by this test; the conftest autouse fixture's
        # path isolation means ACCOUNTS_MANIFEST points at an empty tmp.
        out = await gsc_list_properties(response_format="json")
        assert out["ok"] is False
        assert out["error_code"] == ErrorCode.NO_ACCOUNTS_CONFIGURED
        assert "gsc_add_account" in out["hint"]


class TestMarkdownMode:
    async def test_markdown_mode_is_default_and_unchanged(self, monkeypatch):
        """v1.2.0 markdown behaviour must not change: cross-account
        listings tag each row with ``[alias]``."""
        _write_manifest({
            "a": {"alias": "a", "token_file": "t"},
            "b": {"alias": "b", "token_file": "t"},
        })

        def _build(alias):
            return _mock_service([
                {"siteUrl": f"sc-domain:{alias}.com", "permissionLevel": "siteOwner"},
            ]), None
        monkeypatch.setattr(gsc_server, "_build_service_noninteractive", _build)

        out = await gsc_list_properties()  # default markdown
        assert isinstance(out, str)
        assert "[a] sc-domain:a.com" in out
        assert "[b] sc-domain:b.com" in out

    async def test_markdown_single_account_drops_tag(self, monkeypatch):
        """In the single-account markdown case the ``[alias]`` tag is
        elided for human readability."""
        _write_manifest({"only": {"alias": "only", "token_file": "t"}})
        monkeypatch.setattr(
            gsc_server, "_build_service_noninteractive",
            lambda alias: (_mock_service([
                {"siteUrl": "sc-domain:ex.com", "permissionLevel": "siteOwner"},
            ]), None),
        )
        out = await gsc_list_properties()
        assert "[only]" not in out
        assert "sc-domain:ex.com" in out


class TestInvalidInput:
    async def test_invalid_response_format_returns_validation_string(self):
        out = await gsc_list_properties(response_format="xml")
        assert isinstance(out, str)
        assert out.startswith("Error listing properties:")
        assert "markdown" in out and "json" in out


class TestJsonModeErrorPaths:
    """v1.2.2 coverage top-up: JSON-mode error envelopes. Previously
    verified by inspection only. These tests pin the dict shape so a
    future refactor that threads ``response_format`` wrongly would
    surface as a loud failure rather than a silent markdown-fallback."""

    async def test_json_mode_unknown_alias_returns_mismatch_envelope(self, monkeypatch):
        _write_manifest({"a": {"alias": "a", "token_file": "t"}})
        out = await gsc_list_properties(response_format="json", account_alias="ghost")
        assert isinstance(out, dict), f"expected dict envelope, got {type(out).__name__}"
        assert out["ok"] is False
        assert out["error_code"] == ErrorCode.ACCOUNT_SITE_MISMATCH
        assert "ghost" in out["error"]
        assert out["alternatives"] == ["a"]
        # Core envelope spine present.
        assert out["tool"] == "gsc_list_properties"
        assert "retryable" in out and "hint" in out

    async def test_json_mode_invalid_alias_syntax_returns_bad_request(self):
        # Must write a manifest first so the empty-manifest short-circuit
        # doesn't intercept before alias validation runs.
        _write_manifest({"a": {"alias": "a", "token_file": "t"}})
        # Space is rejected by _validate_alias regex.
        out = await gsc_list_properties(response_format="json", account_alias="BAD ALIAS")
        assert isinstance(out, dict)
        assert out["ok"] is False
        assert out["error_code"] == ErrorCode.BAD_REQUEST
        assert "alias" in out["error"].lower()

    async def test_json_mode_truncated_rows_emit_hint(self, monkeypatch):
        """When the result set exceeds ``limit``, JSON mode surfaces
        ``truncated=True`` and a human-readable ``truncation_hint``
        naming the row counts and the knobs for getting more."""
        _write_manifest({"a": {"alias": "a", "token_file": "t"}})
        sites = [
            {"siteUrl": f"sc-domain:site{i}.com", "permissionLevel": "siteOwner"}
            for i in range(60)
        ]
        monkeypatch.setattr(
            gsc_server, "_build_service_noninteractive",
            lambda alias: (_mock_service(sites), None),
        )
        out = await gsc_list_properties(limit=10, response_format="json")
        assert out["truncated"] is True
        assert out["row_count"] == 10
        assert out["meta"]["total_available"] == 60
        assert "10" in out["truncation_hint"]
        assert out["meta"]["limit"] == 10

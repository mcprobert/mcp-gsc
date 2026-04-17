"""Tests for B.6 — opt-in structured telemetry (`_log` + `_instrument`).

Guarantees:
- No-op when telemetry is disabled (default).
- JSON lines on stderr when ``GSC_MCP_TELEMETRY=1``.
- Stdout stays clean — MCP JSON-RPC frames travel there.
- Every log line is valid JSON with `ts` and `event` fields.
"""
from __future__ import annotations

import importlib
import json

import pytest

import gsc_server


def _reload_with_env(monkeypatch, value: str | None):
    if value is None:
        monkeypatch.delenv("GSC_MCP_TELEMETRY", raising=False)
    else:
        monkeypatch.setenv("GSC_MCP_TELEMETRY", value)
    return importlib.reload(gsc_server)


class TestLogEnablement:
    def test_disabled_by_default(self, monkeypatch, capsys):
        mod = _reload_with_env(monkeypatch, None)
        mod._log("test_event", foo="bar")
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""

    def test_disabled_when_env_falsy(self, monkeypatch, capsys):
        for falsy in ("", "0", "false", "no", "FALSE"):
            mod = _reload_with_env(monkeypatch, falsy)
            mod._log("test_event", foo="bar")
            captured = capsys.readouterr()
            assert captured.err == "", f"expected silent on value {falsy!r}"

    def test_enabled_on_one(self, monkeypatch, capsys):
        mod = _reload_with_env(monkeypatch, "1")
        mod._log("test_event", foo="bar")
        captured = capsys.readouterr()
        line = captured.err.strip()
        record = json.loads(line)
        assert record["event"] == "test_event"
        assert record["foo"] == "bar"
        assert "ts" in record
        assert captured.out == ""

    def test_enabled_on_true_yes(self, monkeypatch, capsys):
        for truthy in ("true", "TRUE", "yes", "YES"):
            mod = _reload_with_env(monkeypatch, truthy)
            mod._log("test_event")
            captured = capsys.readouterr()
            assert captured.err.strip(), f"expected log on {truthy!r}"


class TestInstrumentContextManager:
    @pytest.mark.asyncio
    async def test_exit_emits_ok_true_and_dur_ms(self, monkeypatch, capsys):
        mod = _reload_with_env(monkeypatch, "1")
        async with mod._instrument("some_tool", site_url="sc-domain:example.com"):
            pass
        lines = [json.loads(line) for line in capsys.readouterr().err.strip().splitlines()]
        assert len(lines) == 2
        enter, exit_ = lines
        assert enter["event"] == "tool_enter"
        assert enter["tool"] == "some_tool"
        assert enter["site_url"] == "sc-domain:example.com"
        assert exit_["event"] == "tool_exit"
        assert exit_["tool"] == "some_tool"
        assert exit_["ok"] is True
        assert exit_["dur_ms"] >= 0

    @pytest.mark.asyncio
    async def test_error_emits_tool_error_and_reraises(self, monkeypatch, capsys):
        mod = _reload_with_env(monkeypatch, "1")
        with pytest.raises(ValueError, match="boom"):
            async with mod._instrument("failing_tool"):
                raise ValueError("boom")
        lines = [json.loads(line) for line in capsys.readouterr().err.strip().splitlines()]
        assert len(lines) == 2
        enter, err = lines
        assert enter["event"] == "tool_enter"
        assert err["event"] == "tool_error"
        assert err["ok"] is False
        assert err["error_type"] == "ValueError"
        assert "boom" in err["error"]
        assert err["dur_ms"] >= 0

    @pytest.mark.asyncio
    async def test_disabled_yields_without_logging(self, monkeypatch, capsys):
        mod = _reload_with_env(monkeypatch, None)
        async with mod._instrument("silent_tool"):
            pass
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""

    @pytest.mark.asyncio
    async def test_error_truncation_cap(self, monkeypatch, capsys):
        mod = _reload_with_env(monkeypatch, "1")
        long = "x" * 500
        with pytest.raises(RuntimeError):
            async with mod._instrument("long_error"):
                raise RuntimeError(long)
        err_record = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert err_record["error_type"] == "RuntimeError"
        assert len(err_record["error"]) <= 200


class TestLogNeverReachesStdout:
    """Stdout is the MCP JSON-RPC channel — nothing from telemetry can go there."""

    def test_log_writes_only_to_stderr(self, monkeypatch, capsys):
        mod = _reload_with_env(monkeypatch, "1")
        mod._log("sensitive", foo=1)
        captured = capsys.readouterr()
        assert captured.err.strip()
        assert captured.out == ""


class TestHotPathToolsEmitTelemetry:
    """Sanity-check that the B.6 rollout wraps actually emit events.

    We don't duplicate the full _instrument tests — we just confirm that
    when an instrumented tool runs with telemetry enabled, at least one
    tool_enter/tool_exit pair lands on stderr with the expected tool name.
    """

    async def _run_with_mocked_service(self, mod, tool_fn_name, tool_args, *, rows=None):
        """Build a mock service whose searchanalytics().query().execute()
        returns a canned rows response, then invoke the given tool."""
        from unittest.mock import MagicMock

        service = MagicMock()
        service.searchanalytics.return_value.query.return_value.execute.return_value = {
            "rows": rows if rows is not None else []
        }
        service.sites.return_value.list.return_value.execute.return_value = {
            "siteEntry": [{"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"}]
        }

        # Monkey-patch get_gsc_service on the reloaded module.
        mod.get_gsc_service = lambda: service  # type: ignore[assignment]

        fn = getattr(mod, tool_fn_name)
        return await fn(**tool_args)

    @pytest.mark.asyncio
    async def test_list_properties_emits_enter_and_exit(self, monkeypatch, capsys):
        mod = _reload_with_env(monkeypatch, "1")
        await self._run_with_mocked_service(mod, "list_properties", {})
        lines = [
            json.loads(line) for line in capsys.readouterr().err.strip().splitlines()
            if line.startswith("{")
        ]
        events_for_tool = [l for l in lines if l.get("tool") == "list_properties"]
        # Must have at least tool_enter + tool_exit.
        event_types = {e["event"] for e in events_for_tool}
        assert "tool_enter" in event_types
        assert "tool_exit" in event_types

    @pytest.mark.asyncio
    async def test_get_search_analytics_emits_and_records_args(self, monkeypatch, capsys):
        mod = _reload_with_env(monkeypatch, "1")
        await self._run_with_mocked_service(
            mod,
            "get_search_analytics",
            {"site_url": "sc-domain:example.com", "days": 14, "row_limit": 50},
        )
        lines = [
            json.loads(line) for line in capsys.readouterr().err.strip().splitlines()
            if line.startswith("{")
        ]
        enter = next(l for l in lines if l["event"] == "tool_enter" and l["tool"] == "get_search_analytics")
        # Initial fields passed to _instrument must appear on the enter event.
        assert enter["site_url"] == "sc-domain:example.com"
        assert enter["days"] == 14
        assert enter["row_limit"] == 50

    @pytest.mark.asyncio
    async def test_tool_error_fires_before_envelope_conversion(self, monkeypatch, capsys):
        """When an instrumented body raises, _instrument must log
        tool_error before the outer except converts the exception into
        a user-facing envelope string. Otherwise telemetry thinks every
        envelope-returning exception was a success."""
        mod = _reload_with_env(monkeypatch, "1")
        from unittest.mock import MagicMock

        service = MagicMock()
        service.sites.return_value.list.return_value.execute.side_effect = RuntimeError("boom")
        mod.get_gsc_service = lambda: service  # type: ignore[assignment]

        out = await mod.list_properties()
        # User saw the envelope-converted string...
        assert "RuntimeError" in out or "boom" in out or "Error" in out

        # ...but telemetry saw the raw error.
        lines = [
            json.loads(line) for line in capsys.readouterr().err.strip().splitlines()
            if line.startswith("{")
        ]
        tool_errors = [l for l in lines if l["event"] == "tool_error" and l["tool"] == "list_properties"]
        assert tool_errors, "tool_error must fire before the envelope swallows the exception"
        assert tool_errors[0]["error_type"] == "RuntimeError"
        assert tool_errors[0]["ok"] is False

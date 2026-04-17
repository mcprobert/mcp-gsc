"""Tests for A.9 — headless OAuth guard.

``_start_oauth_flow`` should:
- raise ``HeadlessOAuthError`` with an instructive message when
  ``GSC_MCP_HEADLESS=1`` is set, without attempting ``run_local_server``;
- fall through to ``flow.run_local_server`` when the env var is absent,
  after emitting a stderr warning.
"""
from unittest.mock import MagicMock

import pytest

import gsc_server
from gsc_server import HeadlessOAuthError, _start_oauth_flow


class TestHeadlessGuard:
    def test_headless_env_raises_without_run_local_server(self, monkeypatch, capsys):
        monkeypatch.setenv("GSC_MCP_HEADLESS", "1")
        flow = MagicMock()
        with pytest.raises(HeadlessOAuthError) as exc_info:
            _start_oauth_flow(flow, context="unit-test")
        msg = str(exc_info.value)
        assert "GSC_MCP_HEADLESS" in msg
        assert "desktop session" in msg
        assert "unit-test" in msg
        flow.run_local_server.assert_not_called()

    def test_headless_true_string_also_triggers(self, monkeypatch):
        monkeypatch.setenv("GSC_MCP_HEADLESS", "TRUE")
        flow = MagicMock()
        with pytest.raises(HeadlessOAuthError):
            _start_oauth_flow(flow, context="ctx")

    def test_empty_env_falls_through(self, monkeypatch, capsys):
        monkeypatch.delenv("GSC_MCP_HEADLESS", raising=False)
        flow = MagicMock()
        flow.run_local_server.return_value = "fake-creds"
        result = _start_oauth_flow(flow, context="desktop-path")
        assert result == "fake-creds"
        flow.run_local_server.assert_called_once_with(port=0)
        captured = capsys.readouterr()
        # The stderr warning is informational; we only check it's present.
        assert "Opening browser" in captured.err
        assert "desktop-path" in captured.err

    def test_env_falsy_value_falls_through(self, monkeypatch):
        monkeypatch.setenv("GSC_MCP_HEADLESS", "0")
        flow = MagicMock()
        flow.run_local_server.return_value = "creds"
        result = _start_oauth_flow(flow, context="ctx")
        assert result == "creds"


class TestHeadlessErrorPropagatesThroughGetGscService:
    """Integration test — without this, a previous broad ``except Exception``
    at the primary entry point swallowed HeadlessOAuthError and the server
    fell through to the service-account path with a less useful error.
    """

    def test_headless_error_is_re_raised_not_swallowed(self, monkeypatch):
        def fake_oauth():
            raise gsc_server.HeadlessOAuthError("unit-test sentinel")

        monkeypatch.setattr(gsc_server, "get_gsc_service_oauth", fake_oauth)
        monkeypatch.setattr(gsc_server, "SKIP_OAUTH", False, raising=False)

        with pytest.raises(gsc_server.HeadlessOAuthError) as exc_info:
            gsc_server.get_gsc_service()
        assert "unit-test sentinel" in str(exc_info.value)

    def test_non_headless_oauth_failure_still_falls_through(self, monkeypatch):
        # Regression guard: only HeadlessOAuthError should bypass the
        # service-account fallback. A plain RuntimeError from OAuth must
        # still fall through and hit the FileNotFoundError at the bottom
        # of get_gsc_service when no service-account creds exist either.
        def fake_oauth():
            raise RuntimeError("some other OAuth failure")

        monkeypatch.setattr(gsc_server, "get_gsc_service_oauth", fake_oauth)
        monkeypatch.setattr(gsc_server, "SKIP_OAUTH", False, raising=False)
        monkeypatch.setattr(gsc_server, "POSSIBLE_CREDENTIAL_PATHS", [])

        with pytest.raises(FileNotFoundError):
            gsc_server.get_gsc_service()

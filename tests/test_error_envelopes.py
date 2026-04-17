"""Tests for B.4 structured error envelopes (_make_error_envelope,
_http_error_envelope, _format_error).

Guards that HttpError status codes produce status-aware hints, the
envelope shape stays stable across tools, and the markdown/csv
rendering of an error surfaces the hint prominently.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from gsc_server import (
    _format_error,
    _http_error_envelope,
    _make_error_envelope,
)


def _make_http_error(status: int, message: str = "boom") -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.get = MagicMock(side_effect=lambda key, default=None: {"retry-after": "12"}.get(key, default))
    content = f'{{"error": {{"message": "{message}"}}}}'.encode()
    return HttpError(resp=resp, content=content)


class TestMakeErrorEnvelope:
    def test_minimal(self):
        env = _make_error_envelope(error="something failed")
        assert env == {
            "ok": False,
            "error": "something failed",
            "hint": "",
            "retry_after": None,
            "tool": None,
        }

    def test_full(self):
        env = _make_error_envelope(
            error="rate limit",
            hint="wait 30s",
            retry_after=30.0,
            tool="my_tool",
        )
        assert env["ok"] is False
        assert env["retry_after"] == 30.0
        assert env["tool"] == "my_tool"


class TestHttpErrorEnvelope:
    def test_401_unauthorised(self):
        env = _http_error_envelope(_make_http_error(401), tool="x")
        assert env["ok"] is False
        assert "401" in env["error"]
        assert "authenticate" in env["hint"].lower() or "unauthor" in env["hint"].lower()
        assert env["retry_after"] is None

    def test_403_permission_denied_with_site_url(self):
        env = _http_error_envelope(
            _make_http_error(403),
            tool="x",
            site_url="sc-domain:example.com",
        )
        assert "403" in env["error"]
        assert "sc-domain:example.com" in env["hint"]

    def test_403_permission_denied_without_site_url(self):
        env = _http_error_envelope(_make_http_error(403), tool="x")
        assert "get_active_account" in env["hint"]

    def test_404_site_hint(self):
        env = _http_error_envelope(
            _make_http_error(404),
            tool="x",
            site_url="https://example.com/",
        )
        assert "exactly" in env["hint"].lower() or "verify" in env["hint"].lower()
        assert "example.com" in env["hint"]

    def test_429_rate_limited_populates_retry_after(self):
        env = _http_error_envelope(_make_http_error(429), tool="x")
        assert env["retry_after"] == 12.0  # from our mocked retry-after header

    def test_429_without_retry_after_header_defaults_to_60(self):
        resp = MagicMock()
        resp.status = 429
        resp.get = MagicMock(return_value=None)
        content = b'{"error": {"message": "slow down"}}'
        err = HttpError(resp=resp, content=content)
        env = _http_error_envelope(err, tool="x")
        assert env["retry_after"] == 60.0

    def test_500_transient_has_retry_after(self):
        env = _http_error_envelope(_make_http_error(500), tool="x")
        assert env["retry_after"] == 30.0

    def test_503_transient_has_retry_after(self):
        env = _http_error_envelope(_make_http_error(503), tool="x")
        assert env["retry_after"] == 30.0

    def test_unknown_status_no_hint(self):
        env = _http_error_envelope(_make_http_error(418), tool="x")
        assert env["hint"] == ""
        assert env["retry_after"] is None
        assert "418" in env["error"]

    def test_malformed_content_falls_back_to_str(self):
        resp = MagicMock()
        resp.status = 500
        resp.get = MagicMock(return_value=None)
        err = HttpError(resp=resp, content=b"not json at all")
        env = _http_error_envelope(err, tool="x")
        assert env["ok"] is False
        assert "500" in env["error"]


class TestFormatError:
    def test_json_returns_envelope_verbatim(self):
        env = _make_error_envelope(error="x", hint="y", tool="t")
        out = _format_error(env, response_format="json")
        assert out is env  # exact object identity

    def test_markdown_renders_error_and_hint(self):
        env = _make_error_envelope(
            error="HTTP 403: no access",
            hint="use get_active_account",
            tool="t",
        )
        out = _format_error(env, response_format="markdown")
        assert isinstance(out, str)
        assert out.startswith("Error: HTTP 403: no access")
        assert "Hint: use get_active_account" in out

    def test_markdown_omits_empty_hint(self):
        env = _make_error_envelope(error="plain fail")
        out = _format_error(env, response_format="markdown")
        assert out == "Error: plain fail"

    def test_markdown_includes_retry_after(self):
        env = _make_error_envelope(error="429", hint="wait", retry_after=30.0)
        out = _format_error(env, response_format="markdown")
        assert "Retry-after: 30s" in out

    def test_csv_renders_same_as_markdown(self):
        env = _make_error_envelope(error="x", hint="y")
        assert _format_error(env, response_format="csv") == _format_error(
            env, response_format="markdown"
        )

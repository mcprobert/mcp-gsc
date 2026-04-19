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
    ErrorCode,
    _HTTP_STATUS_TO_CODE,
    _RETRYABLE_CODES,
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
        # v1.2.0: envelopes always include error_code + retryable.
        assert env == {
            "ok": False,
            "error": "something failed",
            "error_code": ErrorCode.INTERNAL_ERROR,
            "hint": "",
            "retryable": True,  # INTERNAL_ERROR is in _RETRYABLE_CODES
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


class TestErrorCodeTaxonomy:
    """v1.2.0 error_code + retryable invariants."""

    def test_every_code_attribute_matches_its_value(self):
        # Convention: ErrorCode.FOO == "FOO". Keeps the enum self-documenting
        # and makes grep audits possible without traversing the class body.
        for attr in dir(ErrorCode):
            if attr.startswith("_"):
                continue
            value = getattr(ErrorCode, attr)
            assert value == attr, f"ErrorCode.{attr} must equal {attr!r}, got {value!r}"

    def test_every_code_is_string(self):
        for attr in dir(ErrorCode):
            if attr.startswith("_"):
                continue
            assert isinstance(getattr(ErrorCode, attr), str)

    def test_retryable_codes_subset_of_all_codes(self):
        all_codes = {
            getattr(ErrorCode, a) for a in dir(ErrorCode) if not a.startswith("_")
        }
        assert _RETRYABLE_CODES <= all_codes

    def test_retryable_default_derived_from_code(self):
        # SERVICE_UNAVAILABLE is in _RETRYABLE_CODES (genuine transient).
        env = _make_error_envelope(error="x", error_code=ErrorCode.SERVICE_UNAVAILABLE)
        assert env["retryable"] is True

    def test_non_retryable_default_derived_from_code(self):
        env = _make_error_envelope(error="x", error_code=ErrorCode.PERMISSION_DENIED)
        assert env["retryable"] is False

    def test_auth_expired_not_retryable_by_default(self):
        """F11: AUTH_EXPIRED is non-retryable by default because most of
        its emission sites (corrupt/missing token, missing refresh
        token, expired creds without refresh) cannot succeed on retry
        — the user must re-run ``gsc_add_account``. Agent retry loops
        on those cases spin forever. The one genuinely transient site
        (post-resolve race in get_gsc_service_for_site) opts in to
        retryable=True explicitly."""
        env = _make_error_envelope(error="x", error_code=ErrorCode.AUTH_EXPIRED)
        assert env["retryable"] is False

    def test_explicit_retryable_overrides_default(self):
        # Explicit False on a code whose default would be True.
        env = _make_error_envelope(
            error="x",
            error_code=ErrorCode.INTERNAL_ERROR,
            retryable=False,
        )
        assert env["retryable"] is False
        # And the other direction.
        env = _make_error_envelope(
            error="x",
            error_code=ErrorCode.PERMISSION_DENIED,
            retryable=True,
        )
        assert env["retryable"] is True

    def test_extras_cannot_override_ok_field(self):
        """F12: ``ok`` is the spine field NOT present as an explicit
        kwarg of ``_make_error_envelope``, so without a guard a caller
        could pass ``_make_error_envelope(error="x", ok=True)`` and
        end up with an envelope claiming success. The explicit guard
        catches this before the envelope is returned."""
        with pytest.raises(TypeError) as exc:
            _make_error_envelope(
                error="original",
                error_code=ErrorCode.INTERNAL_ERROR,
                tool="t",
                ok=True,
            )
        msg = str(exc.value).lower()
        assert "core" in msg or "reserved" in msg or "override" in msg, (
            f"guard must produce a clear message about spine-field "
            f"protection, got: {exc.value}"
        )

    def test_explicit_kwarg_collision_still_typeerror(self):
        """Kwargs that ARE in the signature AND also passed explicitly
        collide at the Python-level (``got multiple values``). Pin
        this so a future signature refactor doesn't accidentally move
        e.g. ``tool`` out of the signature and into ``**extras``
        without also adding coverage for the new attack surface."""
        for bad_field in ("error", "error_code", "tool"):
            with pytest.raises(TypeError):
                _make_error_envelope(
                    error="original",
                    error_code=ErrorCode.INTERNAL_ERROR,
                    tool="t",
                    **{bad_field: "something-else"},
                )

    def test_extras_merged_into_envelope(self):
        env = _make_error_envelope(
            error="x",
            error_code=ErrorCode.AMBIGUOUS_ACCOUNT,
            tool="t",
            alternatives=["chaser", "whitehat"],
            site_url="sc-domain:example.com",
        )
        assert env["alternatives"] == ["chaser", "whitehat"]
        assert env["site_url"] == "sc-domain:example.com"
        # Core fields still present and not shadowed.
        assert env["ok"] is False
        assert env["error_code"] == ErrorCode.AMBIGUOUS_ACCOUNT

    def test_http_status_to_code_mapping(self):
        # Pin the agreed status→code mapping. Changing any of these is
        # a breaking change for agent retry logic.
        assert _HTTP_STATUS_TO_CODE[400] == ErrorCode.BAD_REQUEST
        assert _HTTP_STATUS_TO_CODE[401] == ErrorCode.AUTH_EXPIRED
        assert _HTTP_STATUS_TO_CODE[403] == ErrorCode.PERMISSION_DENIED
        assert _HTTP_STATUS_TO_CODE[404] == ErrorCode.NOT_FOUND
        assert _HTTP_STATUS_TO_CODE[429] == ErrorCode.QUOTA_EXCEEDED
        assert _HTTP_STATUS_TO_CODE[500] == ErrorCode.INTERNAL_ERROR
        assert _HTTP_STATUS_TO_CODE[503] == ErrorCode.SERVICE_UNAVAILABLE

    @pytest.mark.parametrize("status,code,retryable", [
        (400, ErrorCode.BAD_REQUEST, False),
        # 401 → AUTH_EXPIRED, non-retryable (F11). Agents that see a
        # 401 must re-auth before retrying; blind retry spins forever.
        (401, ErrorCode.AUTH_EXPIRED, False),
        (403, ErrorCode.PERMISSION_DENIED, False),
        (404, ErrorCode.NOT_FOUND, False),
        (429, ErrorCode.QUOTA_EXCEEDED, True),
        (500, ErrorCode.INTERNAL_ERROR, True),
        (503, ErrorCode.SERVICE_UNAVAILABLE, True),
    ])
    def test_http_error_envelope_carries_code_and_retryable(self, status, code, retryable):
        env = _http_error_envelope(_make_http_error(status), tool="x")
        assert env["error_code"] == code
        assert env["retryable"] is retryable

    def test_unknown_http_status_falls_back_to_internal_error(self):
        env = _http_error_envelope(_make_http_error(418), tool="x")
        assert env["error_code"] == ErrorCode.INTERNAL_ERROR
        assert env["retryable"] is True

    def test_status_zero_falls_back_to_internal_error(self):
        resp = MagicMock()
        resp.status = 0
        resp.get = MagicMock(return_value=None)
        env = _http_error_envelope(
            HttpError(resp=resp, content=b'{"error": {"message": "dead"}}'),
            tool="x",
        )
        assert env["error_code"] == ErrorCode.INTERNAL_ERROR


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
        # Hint points to v1.2.0 replacements for the old gsc_get_active_account
        # surface.
        assert "gsc_whoami" in env["hint"] or "gsc_list_accounts" in env["hint"]

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
            hint="use gsc_get_active_account",
            tool="t",
        )
        out = _format_error(env, response_format="markdown")
        assert isinstance(out, str)
        assert out.startswith("Error: HTTP 403: no access")
        assert "Hint: use gsc_get_active_account" in out

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

    def test_unknown_format_returns_validation_error(self):
        # Matches _format_table's rejection behaviour — both helpers
        # agree on what counts as a valid response_format.
        env = _make_error_envelope(error="x")
        out = _format_error(env, response_format="xml")
        assert isinstance(out, str)
        assert out.startswith("Error: response_format must be one of")


class TestRetryAfterParsing:
    """B.4 now accepts both numeric and HTTP-date Retry-After headers."""

    def test_numeric_seconds(self):
        resp = MagicMock()
        resp.status = 429
        resp.get = MagicMock(side_effect=lambda k, d=None: "45" if k == "retry-after" else d)
        env = _http_error_envelope(
            HttpError(resp=resp, content=b'{"error": {"message": "slow"}}'),
            tool="x",
        )
        assert env["retry_after"] == 45.0

    def test_http_date_form(self):
        # RFC 7231 allows an HTTP-date. Pick a date guaranteed to be
        # in the future so the parser returns a positive delta.
        from email.utils import format_datetime
        from datetime import datetime, timezone, timedelta
        future = datetime.now(tz=timezone.utc) + timedelta(seconds=20)
        date_str = format_datetime(future)
        resp = MagicMock()
        resp.status = 429
        resp.get = MagicMock(side_effect=lambda k, d=None: date_str if k == "retry-after" else d)
        env = _http_error_envelope(
            HttpError(resp=resp, content=b'{"error": {"message": "slow"}}'),
            tool="x",
        )
        # Within the ±2s tolerance of the parse vs clock skew.
        assert 15.0 <= env["retry_after"] <= 25.0

    def test_unparseable_falls_back_to_60(self):
        resp = MagicMock()
        resp.status = 429
        resp.get = MagicMock(side_effect=lambda k, d=None: "not a number or date" if k == "retry-after" else d)
        env = _http_error_envelope(
            HttpError(resp=resp, content=b'{"error": {"message": "slow"}}'),
            tool="x",
        )
        assert env["retry_after"] == 60.0


class TestUnknownStatus:
    def test_status_0_renders_as_unknown(self):
        resp = MagicMock()
        resp.status = 0
        resp.get = MagicMock(return_value=None)
        env = _http_error_envelope(
            HttpError(resp=resp, content=b'{"error": {"message": "transport dead"}}'),
            tool="x",
        )
        # Literal "HTTP 0" is misleading — HTTP has no status 0.
        assert "HTTP 0" not in env["error"]
        assert "unknown" in env["error"].lower()

    def test_status_none_renders_as_unknown(self):
        # Real googleapiclient transport failures sometimes surface with
        # a None/0 status. Either should render as "unknown" rather than
        # "HTTP 0" / "HTTP None".
        resp = MagicMock()
        resp.status = None
        resp.get = MagicMock(return_value=None)
        env = _http_error_envelope(
            HttpError(resp=resp, content=b'{"error": {"message": "weird"}}'),
            tool="x",
        )
        assert "unknown" in env["error"].lower()

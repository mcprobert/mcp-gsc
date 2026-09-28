from typing import Any, Dict, Iterator, List, Optional, Tuple, Union
import asyncio
import os
import json
import re
import shutil
import csv
import heapq
import io
import math
import random
import socket
import sqlite3
import stat
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

# B.5 (gsc_get_search_by_page_query): when `row_limit` is at or below
# this threshold, the JSON `summary` block is suppressed by default
# because its impression-weighted aggregates are misleading when the
# returned rows have been capped (the top-ranked queries dominate).
# Callers can force the summary on or off via the `include_summary`
# argument regardless of row_limit.
_PAGE_QUERY_SUMMARY_MIN_ROWS = 50

# B.6 — opt-in structured telemetry. Off by default; enable with
# ``GSC_MCP_TELEMETRY=1`` in the server's environment. When enabled, each
# instrumented tool emits one ``tool_enter`` JSON line on start and one
# ``tool_exit`` (or ``tool_error``) line on completion, to stderr. stdout
# is reserved for MCP JSON-RPC frames so telemetry MUST stay on stderr.
TELEMETRY_ENABLED = os.environ.get("GSC_MCP_TELEMETRY", "").strip().lower() in ("1", "true", "yes")


def _log(event: str, **fields: Any) -> None:
    """Emit one JSON line to stderr when telemetry is enabled.

    ``default=str`` makes the helper robust to non-JSON-native values
    (datetimes, Path, exception instances) sneaking in via ``fields``.
    No-op when telemetry is disabled so the overhead is zero on the
    hot path.
    """
    if not TELEMETRY_ENABLED:
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stderr, flush=True)


@asynccontextmanager
async def _instrument(tool: str, **initial_fields: Any):
    """Wrap a tool body with tool_enter / tool_exit / tool_error logging.

    Usage::

        async def my_tool(...):
            async with _instrument("my_tool", site_url=site_url):
                ...

    On normal exit emits ``tool_exit`` with ``dur_ms`` and ``ok=True``.
    On exception emits ``tool_error`` with ``ok=False``,
    ``error_type``, and a truncated ``error`` message, then re-raises.
    """
    start = time.perf_counter()
    _log("tool_enter", tool=tool, **initial_fields)
    try:
        yield
    except Exception as e:
        _log(
            "tool_error",
            tool=tool,
            dur_ms=int((time.perf_counter() - start) * 1000),
            ok=False,
            error_type=type(e).__name__,
            error=str(e)[:200],
        )
        raise
    else:
        _log(
            "tool_exit",
            tool=tool,
            dur_ms=int((time.perf_counter() - start) * 1000),
            ok=True,
        )


class HeadlessOAuthError(RuntimeError):
    """Raised when an interactive OAuth flow would block a headless server."""


def _start_oauth_flow(flow: "InstalledAppFlow", *, context: str):
    """Run the InstalledAppFlow local-server handshake with a headless guard.

    ``flow.run_local_server(port=0)`` opens a browser and blocks until
    the redirect URL is hit. In any headless MCP context (Claude Desktop
    subprocess without browser access, SSH, CI) that would hang the
    server indefinitely.

    If ``GSC_MCP_HEADLESS=1`` is set we raise a :class:`HeadlessOAuthError`
    with remediation instructions instead. Otherwise we print a warning
    to stderr before starting the flow so users see *why* the server
    appears to stall.
    """
    headless = os.environ.get("GSC_MCP_HEADLESS", "").strip().lower() in ("1", "true", "yes")
    if headless:
        raise HeadlessOAuthError(
            f"OAuth required for {context}, but GSC_MCP_HEADLESS=1 is set. "
            "Run `python gsc_server.py --login` from a desktop session "
            "(or any environment that can open a browser) to authorise, "
            "then re-start the MCP server with GSC_MCP_HEADLESS unset or "
            "with the cached token.json in place."
        )
    print(
        f"[gsc-mcp] Opening browser for Google OAuth ({context}). "
        "If no browser opens within ~30s, set GSC_MCP_HEADLESS=1 and "
        "complete the login flow from a desktop session instead.",
        file=sys.stderr,
        flush=True,
    )
    return flow.run_local_server(port=0)

import google.auth
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import httplib2

# MCP
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("gsc-server")

# FastMCP (mcp 1.3.0) does not forward a version to the lowlevel Server it
# builds, and that Server falls back to reporting the *MCP SDK* version in the
# handshake's serverInfo. So without this, every client -- and any handshake
# health check -- sees the SDK version where it expects ours. Set it from our
# own package metadata so there is a single source of truth (pyproject's
# version). Best-effort: running straight from a source checkout with no
# installed dist must not break startup, and a private-attribute write is the
# only route FastMCP leaves open here.
_SERVER_VERSION = "unknown"
try:
    from importlib.metadata import version as _pkg_version

    _SERVER_VERSION = _pkg_version("mcp-gsc")
    mcp._mcp_server.version = _SERVER_VERSION
except Exception:  # pragma: no cover - metadata absent or SDK internals moved
    pass

# Path to your service account JSON or user credentials JSON
# First check if GSC_CREDENTIALS_PATH environment variable is set
# Then try looking in the script directory and current working directory as fallbacks
GSC_CREDENTIALS_PATH = os.environ.get("GSC_CREDENTIALS_PATH")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
POSSIBLE_CREDENTIAL_PATHS = [
    GSC_CREDENTIALS_PATH,  # First try the environment variable if set
    os.path.join(SCRIPT_DIR, "service_account_credentials.json"),
    os.path.join(os.getcwd(), "service_account_credentials.json"),
    # Add any other potential paths here
]

# OAuth client secrets file path
OAUTH_CLIENT_SECRETS_FILE = os.environ.get("GSC_OAUTH_CLIENT_SECRETS_FILE")
if not OAUTH_CLIENT_SECRETS_FILE:
    OAUTH_CLIENT_SECRETS_FILE = os.path.join(SCRIPT_DIR, "client_secrets.json")

# State directory for OAuth tokens + the multi-account manifest.
#
# Defaults to ~/.config/gsc-mcp so that a shared-network-volume install
# (where SCRIPT_DIR is a per-user mount name like /Volumes/Whitehat vs
# /Volumes/Whitehat-1, or — after a non-editable pip install — a path
# inside the venv's site-packages) keeps its credentials in the running
# user's $HOME rather than next to the code. Override with GSC_STATE_DIR.
#
# Legacy installs stored token.json + accounts/ next to the script; those
# are migrated into GSC_STATE_DIR on first run (see _migrate_state_dir).
GSC_STATE_DIR = os.environ.get("GSC_STATE_DIR") or os.path.expanduser(
    "~/.config/gsc-mcp"
)

# Token file path for storing OAuth tokens
TOKEN_FILE = os.path.join(GSC_STATE_DIR, "token.json")

# Environment variable to skip OAuth authentication
SKIP_OAUTH = os.environ.get("GSC_SKIP_OAUTH", "").lower() in ("true", "1", "yes")

SCOPES = ["https://www.googleapis.com/auth/webmasters"]
OAUTH_SCOPES = SCOPES + [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

# Multi-account support. All module globals below (`_active_account`,
# `_sf_sessions`, `_migration_checked`) are touched without locks. This
# is safe under FastMCP's stdio transport — it dispatches one request
# at a time through the asyncio loop and none of the read-modify-write
# sequences contain an `await` that would yield between read and
# write. If this server is ever re-platformed to SSE or HTTP
# multi-tenant, these three variables become racy and need an asyncio
# Lock (or a move to per-session state).
ACCOUNTS_DIR = os.path.join(GSC_STATE_DIR, "accounts")
ACCOUNTS_MANIFEST = os.path.join(ACCOUNTS_DIR, "accounts.json")
_active_account: Optional[str] = None

_RESPONSE_FORMATS = ("markdown", "csv", "json")


class ErrorCode:
    """Stable string enum for error envelopes (v1.2.0+).

    Agents branch on these; they MUST remain stable across versions.
    When adding a new code, update ``_RETRYABLE_CODES`` if retries are
    safe, and document it in CLAUDE.md's response-envelope section.
    """

    # Account resolution (new in v1.2.0)
    ACCOUNT_SITE_MISMATCH = "ACCOUNT_SITE_MISMATCH"
    AMBIGUOUS_ACCOUNT = "AMBIGUOUS_ACCOUNT"
    NO_ACCOUNT_FOR_PROPERTY = "NO_ACCOUNT_FOR_PROPERTY"
    NO_ACCOUNTS_CONFIGURED = "NO_ACCOUNTS_CONFIGURED"
    ACCOUNT_RESOLUTION_INCOMPLETE = "ACCOUNT_RESOLUTION_INCOMPLETE"

    # Input validation
    INVALID_PROPERTY_FORMAT = "INVALID_PROPERTY_FORMAT"
    BAD_REQUEST = "BAD_REQUEST"

    # Auth
    AUTH_EXPIRED = "AUTH_EXPIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"

    # HTTP-mapped
    NOT_FOUND = "NOT_FOUND"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"

    # Lifecycle
    DEPRECATED_TOOL = "DEPRECATED_TOOL"

    # Reliability (new in v1.4.0)
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"  # daily quota spent; retry after PT midnight
    TIMEOUT = "TIMEOUT"                  # request timed out after every retry
    JOB_NOT_FOUND = "JOB_NOT_FOUND"      # unknown / evicted async job id


# Codes where retrying the identical request is likely to succeed
# (transient server-side conditions). Agents that do automated retries
# should key off ``retryable`` rather than regex-matching the error
# message.
#
# Why AUTH_EXPIRED is NOT in this set: four of its five emission sites
# (missing token file, corrupt token, missing refresh token, expired
# creds with no refresh token) are strictly non-retryable — the user
# must re-run ``gsc_add_account``. The fifth site (``creds.refresh()``
# raising) is ambiguous: a network blip is transient but a revoked
# token is not, and an agent loop on revoked is worse than one extra
# manual re-auth on a blip. The one genuinely transient case — the
# post-resolve race in ``get_gsc_service_for_site`` where a token
# expires between discovery and use — opts in by passing
# ``retryable=True`` explicitly at that call site.
_RETRYABLE_CODES: frozenset = frozenset({
    ErrorCode.ACCOUNT_RESOLUTION_INCOMPLETE,
    ErrorCode.QUOTA_EXCEEDED,
    ErrorCode.INTERNAL_ERROR,
    ErrorCode.SERVICE_UNAVAILABLE,
    ErrorCode.TIMEOUT,
})


# HTTP status → ErrorCode. Any status not in this map falls back to
# INTERNAL_ERROR on the principle that an unknown status from Google is
# more likely a transient hiccup than a deterministic caller bug.
_HTTP_STATUS_TO_CODE: Dict[int, str] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.AUTH_EXPIRED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.NOT_FOUND,
    429: ErrorCode.QUOTA_EXCEEDED,
    500: ErrorCode.INTERNAL_ERROR,
    502: ErrorCode.SERVICE_UNAVAILABLE,
    503: ErrorCode.SERVICE_UNAVAILABLE,
    504: ErrorCode.SERVICE_UNAVAILABLE,
}


# Fields that are part of the envelope spine and must not be set via
# ``_make_error_envelope(..., **extras)``. Guards against a future
# caller bug that accidentally flips ``ok`` to True on an error
# envelope via a misnamed extras kwarg.
_ENVELOPE_RESERVED_FIELDS: frozenset = frozenset({
    "ok", "error", "error_code", "hint", "retryable", "retry_after", "tool",
})


def _make_error_envelope(
    *,
    error: str,
    hint: str = "",
    error_code: str = ErrorCode.INTERNAL_ERROR,
    retryable: Optional[bool] = None,
    retry_after: Optional[float] = None,
    tool: Optional[str] = None,
    **extras: Any,
) -> Dict[str, Any]:
    """Structured error envelope (extended in v1.2.0).

    Fields:
        ok: always False (this is the error branch).
        error: short message, human-readable, suitable for agent
            reasoning. Does not include remediation — that's ``hint``.
        error_code: stable string enum from :class:`ErrorCode`. Agents
            branch on this; it is the canonical decision pivot.
        hint: one-sentence remediation suggestion. May be empty.
        retryable: "Is retrying the identical request likely to
            succeed?" When ``None`` (default), derived from
            ``error_code`` via :data:`_RETRYABLE_CODES`. Pass explicit
            True/False only when the code's default would be wrong
            for this specific call site.
        retry_after: seconds to wait before retrying, when the
            error is transient. None for non-transient errors.
        tool: name of the tool that produced the envelope.
        **extras: additional keyed fields — e.g. ``alternatives=[...]``
            for ``AMBIGUOUS_ACCOUNT``, ``site_url=...`` for routing
            errors. Merged into the returned envelope.
    """
    if retryable is None:
        retryable = error_code in _RETRYABLE_CODES
    envelope: Dict[str, Any] = {
        "ok": False,
        "error": error,
        "error_code": error_code,
        "hint": hint,
        "retryable": retryable,
        "retry_after": retry_after,
        "tool": tool,
    }
    if extras:
        # Guard spine fields. A caller that accidentally passed e.g.
        # ``ok=True`` as an extras kwarg would otherwise flip the
        # envelope's error flag silently.
        overlap = _ENVELOPE_RESERVED_FIELDS & extras.keys()
        if overlap:
            raise TypeError(
                f"_make_error_envelope extras may not override core "
                f"envelope fields: {sorted(overlap)}"
            )
        envelope.update(extras)
    return envelope


# --- Pacific Time (v1.4.0) ---
# The Search Console API reports and bounds dates in Pacific Time, so every
# "today", window end and quota reset is computed there -- never in the
# server's local zone.
_PT = ZoneInfo("America/Los_Angeles")
_PT_LABEL = "America/Los_Angeles (Pacific Time, API convention)"


def _now_pt() -> datetime:
    return datetime.now(_PT)


def _today_pt():
    """Today's date in Pacific Time. Tests stub this (see conftest)."""
    return _now_pt().date()


def _seconds_until_pt_midnight() -> float:
    now = _now_pt()
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=_PT)
    return max(1.0, (midnight - now).total_seconds())


# --- Google error classification (v1.4.0) ---
_DAILY_QUOTA_REASONS = frozenset({"dailyLimitExceeded", "dailyLimitExceededUnreg"})
_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
_DAILY_QUOTA_MESSAGE_RE = re.compile(r"per\s+day|daily", re.IGNORECASE)


class _GscTimeoutError(HttpError):
    """Raised by ``_gsc_execute_sync`` when a request timed out on every
    attempt. Subclasses ``HttpError`` (as a synthetic 504) so every tool's
    existing ``except HttpError`` branch renders it -- as ``TIMEOUT``."""

    def __init__(self, *, step: str, attempts: int, cause: BaseException) -> None:
        content = json.dumps({"error": {"message": (
            f"Request timed out at step {step!r} after {attempts} attempt(s): "
            f"{type(cause).__name__}: {cause}"
        )}}).encode()
        super().__init__(httplib2.Response({"status": 504}), content)
        self.gsc_step = step
        self.gsc_attempts = attempts


def _http_error_details(e: HttpError) -> Dict[str, Any]:
    """Pull ``status``, Google's ``message`` and ``reason`` out of an
    HttpError and classify it: ``daily_quota`` | ``rate_limit`` |
    ``server`` | ``timeout`` | ``other``."""
    message = str(e)
    reason: Optional[str] = None
    try:
        content = json.loads(e.content.decode("utf-8"))
        err = content.get("error", {}) or {}
        message = err.get("message", message)
        errors = err.get("errors") or []
        if errors and isinstance(errors[0], dict):
            reason = errors[0].get("reason")
        if not reason:
            reason = err.get("status")  # e.g. RESOURCE_EXHAUSTED
    except Exception:
        pass
    status = getattr(e.resp, "status", None)
    # getattr returns the attribute's value — if it's literally 0, treat
    # that as "unknown" (HTTP has no status 0; it usually indicates a
    # transport-layer failure surfaced without a proper code).
    try:
        status = int(status) if status else None
    except (TypeError, ValueError):
        status = None

    if isinstance(e, _GscTimeoutError):
        kind = "timeout"
    elif (
        status == 429
        or reason in _RATE_LIMIT_REASONS
        or reason in _DAILY_QUOTA_REASONS
        or reason == "quotaExceeded"
    ):
        daily = reason in _DAILY_QUOTA_REASONS or bool(_DAILY_QUOTA_MESSAGE_RE.search(message or ""))
        kind = "daily_quota" if daily else "rate_limit"
    elif status in (500, 502, 503, 504):
        kind = "server"
    else:
        kind = "other"
    return {"status": status, "message": message, "reason": reason, "kind": kind}


def _http_error_envelope(
    e: HttpError,
    *,
    tool: str,
    site_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Map a googleapiclient ``HttpError`` to an error envelope with
    status-aware hints so agents can recover without a retry storm.

    v1.4.0: also names the failing ``step`` and attempt count (when the
    call went through ``_gsc_execute``), Google's ``http_status`` and
    ``google_reason``, and distinguishes a spent daily quota
    (``QUOTA_EXHAUSTED``) and a timeout (``TIMEOUT``) from a rate limit.
    """
    info = _http_error_details(e)
    message = info["message"]
    status = info["status"]
    kind = info["kind"]

    hint = ""
    retry_after: Optional[float] = None

    if kind == "timeout":
        hint = "Google did not answer in time on any attempt. Retry shortly; for URL inspection use gsc_inspect_start."
        retry_after = 30.0
    elif kind == "daily_quota":
        hint = "Daily Google quota is spent for this property. It resets at midnight Pacific Time."
        retry_after = _seconds_until_pt_midnight()
    elif kind == "rate_limit" and status != 429:
        hint = "Rate limited by GSC. Wait then retry."
        retry_after = _parse_retry_after(e.resp)
    elif status == 400:
        hint = "Request rejected by GSC. Check the arguments you passed."
    elif status == 401:
        hint = (
            "Unauthorised. Re-authenticate with `gsc_add_account`, "
            "or set GSC_OAUTH_CLIENT_SECRETS_FILE."
        )
    elif status == 403:
        site_part = f" Verify `{site_url!r}` is shared with this account." if site_url else ""
        hint = (
            f"Permission denied. Use `gsc_whoami` / `gsc_list_accounts` "
            f"to confirm routing.{site_part}"
        )
    elif status == 404:
        if site_url:
            hint = (
                f"Not found. Verify `site_url={site_url!r}` matches GSC "
                f"exactly (domain properties need the `sc-domain:` prefix)."
            )
        else:
            hint = "Not found. Check the resource identifier was copied exactly."
    elif status == 429:
        hint = "Rate limited by GSC. Wait then retry."
        retry_after = _parse_retry_after(e.resp)
    elif status in (500, 503):
        hint = "GSC server error. Retry after a short backoff."
        retry_after = 30.0

    status_text = f"HTTP {status}" if status else "HTTP (unknown status)"
    # Unknown / missing status → INTERNAL_ERROR (transient assumption).
    error_code = _HTTP_STATUS_TO_CODE.get(status, ErrorCode.INTERNAL_ERROR) if status else ErrorCode.INTERNAL_ERROR
    if kind == "timeout":
        error_code = ErrorCode.TIMEOUT
    elif kind == "daily_quota":
        error_code = ErrorCode.QUOTA_EXHAUSTED
    elif kind == "rate_limit":
        error_code = ErrorCode.QUOTA_EXCEEDED
    extras: Dict[str, Any] = {"http_status": status, "google_reason": info["reason"]}
    step = getattr(e, "gsc_step", None)
    if step:
        extras["step"] = step
        extras["attempts"] = getattr(e, "gsc_attempts", None)
    return _make_error_envelope(
        error=f"{status_text}: {message}",
        hint=hint,
        error_code=error_code,
        retry_after=retry_after,
        tool=tool,
        **extras,
    )


def _parse_retry_after(resp: Any) -> float:
    """Parse an HTTP ``Retry-After`` header, accepting seconds or an
    HTTP-date (RFC 7231). Falls back to 60s for anything unparseable.
    """
    try:
        raw = resp.get("retry-after")
    except Exception:
        raw = None
    if raw is None:
        return 60.0
    # Number-of-seconds form first (cheap).
    try:
        return max(0.0, min(float(raw), 3600.0))
    except (TypeError, ValueError):
        pass
    # HTTP-date form. `email.utils.parsedate_to_datetime` is stdlib.
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(raw)
        if when is None:
            return 60.0
        delta = (when - datetime.now(tz=when.tzinfo)).total_seconds()
        return max(0.0, min(delta, 3600.0))
    except Exception:
        return 60.0


# --- Retrying executor (v1.4.0) ---
# Every Google API ``.execute()`` in this file goes through
# ``_gsc_execute`` (async, off the event loop) or ``_gsc_execute_sync``
# (already inside a worker thread). Retries rate limits, 5xx and
# transport timeouts with jittered exponential backoff; never retries a
# spent daily quota or a caller error. The step name and attempt count
# are attached to the raised HttpError so the envelope can name them.
_RETRY_MAX_ATTEMPTS = 4
_RETRY_BASE_DELAY = 1.0
_RETRY_MAX_DELAY = 30.0
# A Retry-After longer than this is handed back to the caller (in the
# envelope's retry_after) instead of being slept inside the tool call,
# which would eat most of a 60 s client timeout.
_RETRY_AFTER_MAX_WAIT = 10.0
_retry_sleep = time.sleep  # test seam: conftest replaces it with a no-op


def _backoff_delay(attempt: int) -> float:
    """Equal-jitter exponential backoff: attempt 1 → 0.5–1 s, 2 → 1–2 s, …"""
    cap = min(_RETRY_MAX_DELAY, _RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return cap / 2 + random.uniform(0, cap / 2)


def _retry_after_header(e: HttpError) -> Optional[float]:
    try:
        raw = e.resp.get("retry-after")
    except Exception:
        raw = None
    if raw is None:
        return None
    return _parse_retry_after(e.resp)


def _retry_delay(e: HttpError, attempt: int) -> Optional[float]:
    """Delay before retrying ``e``, or None to hand it back to the caller:
    backoff, raised to any Retry-After the server sent (rate limits and
    5xx alike); a Retry-After above _RETRY_AFTER_MAX_WAIT is not slept."""
    delay = _backoff_delay(attempt)
    wait = _retry_after_header(e)
    if wait is not None:
        if wait > _RETRY_AFTER_MAX_WAIT:
            return None
        delay = max(delay, wait)
    return delay


def _gsc_execute_sync(request_fn, *, step: str, max_attempts: int = _RETRY_MAX_ATTEMPTS):
    """Run ``request_fn()`` (a zero-arg callable ending in ``.execute()``)
    with retries. Blocking: call from a worker thread, or use
    :func:`_gsc_execute` from async code."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return request_fn()
        except HttpError as e:
            kind = _http_error_details(e)["kind"]
            # Honour Retry-After rather than retrying early; a wait longer
            # than _RETRY_AFTER_MAX_WAIT belongs to the caller, not a tool call.
            delay = _retry_delay(e, attempt) if kind in ("rate_limit", "server") else None
            if delay is None or attempt >= max_attempts:
                e.gsc_step = step
                e.gsc_attempts = attempt
                raise
        except (TimeoutError, socket.timeout) as e:
            if attempt >= max_attempts:
                raise _GscTimeoutError(step=step, attempts=attempt, cause=e) from e
            delay = _backoff_delay(attempt)
        except ConnectionError:
            if attempt >= max_attempts:
                raise
            delay = _backoff_delay(attempt)
        _log("api_retry", step=step, attempt=attempt, delay_s=round(delay, 2))
        _retry_sleep(delay)


async def _gsc_execute(request_fn, *, step: str, max_attempts: int = _RETRY_MAX_ATTEMPTS):
    """Async front for :func:`_gsc_execute_sync`: runs in a worker thread so
    a slow Google call never blocks the event loop (and other tool calls,
    e.g. ``gsc_inspect_status``, keep answering)."""
    return await asyncio.to_thread(
        _gsc_execute_sync, request_fn, step=step, max_attempts=max_attempts,
    )


def _write_token_file(path: str, text: str) -> None:
    """Atomically replace an OAuth token file (temp file + ``os.replace``)
    so a concurrent reader never sees half-written JSON.

    Keeps the existing file's mode: on the shared multi-login tree tokens
    are group/world-writable so every login can persist a refresh, and a
    umask-default replacement would lock the others out. A new file gets
    0o666 when its directory is world-writable (shared tree), else 0o600.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        dir_mode = stat.S_IMODE(os.stat(directory).st_mode)
        mode = 0o666 if dir_mode & stat.S_IWOTH else 0o600
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.{uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _format_error(
    envelope: Dict[str, Any],
    *,
    response_format: str = "markdown",
) -> Any:
    """Render an error envelope per response_format.

    json: the envelope dict verbatim.
    markdown / csv: a string starting with ``Error:`` followed by the
    hint and retry_after if present — human-readable and parseable by
    agent text routing.

    Unknown ``response_format`` values return a validation-style error
    string matching :func:`_format_table`'s behaviour so the two helpers
    stay consistent.
    """
    fmt = str(response_format or "").strip().lower()
    if fmt not in _RESPONSE_FORMATS:
        return (
            f"Error: response_format must be one of {_RESPONSE_FORMATS}; "
            f"got {response_format!r}."
        )
    if fmt == "json":
        return envelope
    error = envelope.get("error", "")
    hint = envelope.get("hint", "")
    retry_after = envelope.get("retry_after")
    parts = [f"Error: {error}"]
    if hint:
        parts.append(f"Hint: {hint}")
    if retry_after:
        parts.append(f"Retry-after: {retry_after:.0f}s")
    return "\n".join(parts)


def _coverage(checked: int, of: Optional[int], unit: str, note: Optional[str] = None) -> Dict[str, Any]:
    """How much of the whole a result looked at (v1.6.0): ``checked`` of
    ``of`` (None = unknown, more exist). ``partial`` is true whenever the
    result is a sample, a cap, a page or a skip — so "4 redirects" from 40
    of 591 URLs can never read as the whole answer."""
    partial = of is None or checked < of
    total = "an unknown total (more exist)" if of is None else str(of)
    out: Dict[str, Any] = {
        "checked": checked, "of": of, "unit": unit, "partial": partial,
        "summary": f"checked {checked} of {total} {unit}" + (" — PARTIAL" if partial else ""),
    }
    if note:
        out["note"] = note
    return out


def _coverage_rollup(parts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Several coverages under one key; partial if any part is."""
    return {"partial": any(c["partial"] for c in parts.values()),
            "summary": "; ".join(f"{k}: {c['summary']}" for k, c in parts.items()), **parts}


def _markdown_meta_footer(meta: Dict[str, Any]) -> List[str]:
    """Compact meta lines under a markdown table: the totals-honesty gap and
    any warnings (the window itself is a header line)."""
    lines: List[str] = []
    cov = meta.get("coverage")
    if isinstance(cov, dict) and cov.get("partial"):
        lines.append(f"Coverage: {cov.get('summary')}")
    share = meta.get("unattributed_share")
    total = meta.get("page_total")
    if isinstance(share, dict) and isinstance(total, dict):
        def _pct(v: Any) -> str:
            return "n/a" if v is None else f"{v * 100:.2f}%"
        lines.append(
            f"Total (query-less, incl. anonymised): {total.get('clicks')} clicks, "
            f"{total.get('impressions')} impressions; unattributed share: clicks "
            f"{_pct(share.get('clicks'))}, impressions {_pct(share.get('impressions'))}"
        )
    for warning in meta.get("warnings") or []:
        lines.append(f"Warning: {warning}")
    return [""] + lines if lines else []


def _format_table(
    rows: List[Dict[str, Any]],
    columns: List[Dict[str, str]],
    *,
    response_format: str = "markdown",
    header_lines: Optional[List[str]] = None,
    truncated: bool = False,
    truncation_hint: str = "",
    meta: Optional[Dict[str, Any]] = None,
    text_meta: bool = False,
) -> Any:
    """Render a tabular result as markdown, csv, or a json dict.

    This is the shared output-shaping primitive that Tranche B leans on
    — individual tools will migrate their per-tool string-building to
    call this so every tool gets csv + json for free and so the
    truncation-nudge phrasing stays identical.

    Args:
        rows: list of dicts. Every dict should carry the keys named in
            ``columns[*]["key"]``; missing keys render as empty.
        columns: column specs, in display order. Each is
            ``{"key": str, "display": str, "type": str}`` where type is
            one of ``int``, ``float``, ``pct`` (formatted with 2-decimal
            percent sign), or ``str`` (default).
        response_format: ``markdown`` | ``csv`` | ``json``.
        header_lines: plain-text lines prepended above the table
            (markdown / csv only; ignored for json).
        truncated: True when the caller knows ``len(rows)`` hit a cap
            and more rows exist.
        truncation_hint: agent-facing sentence explaining how to get
            more rows. Rendered prominently when ``truncated=True``.
        meta: extra key/value pairs for the json shape (e.g. totals,
            thresholds). With ``text_meta=True`` the csv shape also carries
            it, as one ``# meta: {json}`` comment line, and markdown gets a
            footer with the unattributed share and any warnings.

    CSV cells are raw values (v1.4.0): ratios stay ratios and floats are
    not rounded; the formula-injection guard applies to string columns
    only, so a negative number is still a number.

    Returns:
        str for markdown and csv, dict for json. On unknown
        ``response_format`` returns a plain error string — tools should
        treat this as a validation failure and surface to the caller.
    """
    fmt = str(response_format or "").strip().lower()
    if fmt not in _RESPONSE_FORMATS:
        return (
            f"Error: response_format must be one of {_RESPONSE_FORMATS}; "
            f"got {response_format!r}."
        )

    if fmt == "json":
        return {
            "ok": True,
            "columns": [c["key"] for c in columns],
            "rows": [{c["key"]: row.get(c["key"]) for c in columns} for row in rows],
            "row_count": len(rows),
            "truncated": bool(truncated),
            "truncation_hint": truncation_hint if truncated else "",
            "meta": meta or {},
        }

    # For markdown + csv we need string renderings of each cell.
    def _render_cell(value: Any, col_type: str) -> str:
        if value is None:
            return ""
        if col_type == "int":
            try:
                return str(int(value))
            except (TypeError, ValueError):
                return str(value)
        if col_type == "signed_int":
            try:
                return f"{int(value):+d}"
            except (TypeError, ValueError):
                return str(value)
        if col_type == "float":
            try:
                return f"{float(value):.1f}"
            except (TypeError, ValueError):
                return str(value)
        if col_type == "signed_float":
            try:
                return f"{float(value):+.1f}"
            except (TypeError, ValueError):
                return str(value)
        if col_type == "pct":
            # Caller may pass either a 0-1 ratio or an already-percent
            # number; heuristic: anything <= 1 is treated as a ratio.
            try:
                f = float(value)
            except (TypeError, ValueError):
                return str(value)
            if abs(f) <= 1.0:
                f *= 100.0
            return f"{f:.2f}%"
        return str(value)

    headers = [c.get("display", c["key"]) for c in columns]
    data_rows = [
        [_render_cell(row.get(c["key"]), c.get("type", "str")) for c in columns]
        for row in rows
    ]

    # The truncation warning MUST precede any other context lines so
    # agents can't skim past it (A.1's original invariant; the B.2
    # review caught this regression when the ordering flipped).
    effective_hint = truncation_hint
    if truncated and not effective_hint:
        effective_hint = "Result was truncated; no hint provided by the caller."

    if fmt == "markdown":
        lines: List[str] = []
        if truncated:
            lines.append(f"⚠ TRUNCATED: {effective_hint}")
            lines.append("")
        if header_lines:
            lines.extend(header_lines)
            lines.append("")
        lines.append(" | ".join(headers))
        lines.append(" | ".join("---" for _ in headers))
        for row in data_rows:
            lines.append(" | ".join(row))
        if text_meta and meta:
            lines.extend(_markdown_meta_footer(meta))
        return "\n".join(lines)

    # csv — RFC-4180-ish: comma separator, CRLF newlines, quote cells
    # that contain comma / quote / newline. Cells that begin with a
    # spreadsheet-formula trigger (=, +, -, @, tab, CR) get a leading
    # apostrophe prepended so Excel/Sheets don't treat them as
    # formulas — OWASP CSV-injection mitigation.
    _FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

    def _csv_quote_plain(cell: str) -> str:
        if any(ch in cell for ch in (",", '"', "\n", "\r")):
            return '"' + cell.replace('"', '""') + '"'
        return cell

    def _csv_quote(cell: str) -> str:
        if cell and cell[0] in _FORMULA_TRIGGERS:
            cell = "'" + cell
        return _csv_quote_plain(cell)

    def _raw_cell(value: Any, col_type: str) -> str:
        if value is None:
            return ""
        if col_type in ("int", "signed_int"):
            try:
                f = float(value)
                return str(int(f)) if f.is_integer() else repr(f)
            except (TypeError, ValueError):
                return str(value)
        if col_type in ("float", "signed_float", "pct"):
            try:
                return repr(float(value))
            except (TypeError, ValueError):
                return str(value)
        return str(value)

    lines = []
    if truncated:
        # Truncation first in CSV too — downstream parsers that skip
        # `#`-comments will still log the warning, agents reading the
        # raw stream see it before any data rows.
        lines.append(f"# TRUNCATED: {effective_hint}")
    if header_lines:
        lines.extend(f"# {line}" for line in header_lines)
    if text_meta and meta:
        lines.append("# meta: " + json.dumps(meta, default=str, separators=(",", ":")))
    lines.append(",".join(_csv_quote(h) for h in headers))
    for row in rows:
        cells = []
        for c in columns:
            col_type = c.get("type", "str")
            cell = _raw_cell(row.get(c["key"]), col_type)
            if col_type == "str":
                cells.append(_csv_quote(cell))
            else:
                cells.append(_csv_quote_plain(cell))
        lines.append(",".join(cells))
    return "\r\n".join(lines)


# --- Screaming Frog CSV bridge (Add 1) ---
# Sessions hold file paths and metadata only. Rows stream from disk at query time
# to avoid OOM on large exports (internal_all.csv can be 60MB+ and 1100+ columns).
_sf_sessions: Dict[str, Dict[str, Any]] = {}
_SF_FILE_SIZE_WARNING_BYTES = 150 * 1024 * 1024  # 150 MB informational warning
_ALLOWED_DATASET_RE = re.compile(r"^[a-z0-9_]+$")  # path traversal guard
_COLUMN_ALIAS = {
    "avg_position": "position",
    "average_position": "position",
}
_SF_TIMESTAMP_RE = re.compile(
    r"(\d{4})[.\-_](\d{2})[.\-_](\d{2})[.\-_]\d{2}[.\-_]\d{2}[.\-_]\d{2}"
)


# --- Multi-account helpers ---

def _validate_alias(alias: str) -> str:
    """Validate and normalize account alias. Returns normalized alias or raises ValueError."""
    alias = alias.strip().lower()
    if not alias or len(alias) > 30:
        raise ValueError("Alias must be 1-30 characters.")
    if not re.match(r'^[a-z0-9][a-z0-9-]*$', alias):
        raise ValueError("Alias must be lowercase alphanumeric and hyphens, starting with a letter or digit.")
    return alias


def _load_manifest() -> dict:
    """Load accounts manifest. Returns empty structure if missing or corrupted."""
    if os.path.exists(ACCOUNTS_MANIFEST):
        try:
            with open(ACCOUNTS_MANIFEST, "r") as f:
                data = json.load(f)
            if isinstance(data, dict) and "accounts" in data:
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"active_account": None, "accounts": {}}


def _save_manifest(manifest: dict) -> None:
    """Atomically write manifest to disk, creating accounts dir if needed."""
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    tmp_path = ACCOUNTS_MANIFEST + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp_path, ACCOUNTS_MANIFEST)


def _get_active_token_file() -> Optional[str]:
    """Resolve token file path for the active account.
    Returns the expected path even if the file is missing on disk,
    so that OAuth re-auth can recreate it rather than silently falling back."""
    global _active_account
    # Ensure legacy migration has run
    _maybe_migrate_legacy_token()
    # Lazy init from manifest
    if _active_account is None:
        manifest = _load_manifest()
        _active_account = manifest.get("active_account")
    if _active_account is None:
        return None
    manifest = _load_manifest()
    acct = manifest.get("accounts", {}).get(_active_account)
    if acct and acct.get("token_file"):
        token_path = acct["token_file"]
        # Resolve relative paths against GSC_STATE_DIR
        if not os.path.isabs(token_path):
            token_path = os.path.join(GSC_STATE_DIR, token_path)
        return token_path
    return None


def _detect_email(creds) -> Optional[str]:
    """Detect email from OAuth credentials using tokeninfo endpoint."""
    try:
        import urllib.request
        import urllib.parse
        if creds.token:
            url = f"https://oauth2.googleapis.com/tokeninfo?access_token={urllib.parse.quote(creds.token)}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                return data.get("email")
    except Exception:
        pass
    return None


_migration_checked = False


def _migrate_state_dir() -> None:
    """Relocate on-share state into GSC_STATE_DIR ($HOME) on first run.

    Legacy installs kept ``token.json`` and ``accounts/`` next to the
    script on a shared network volume. When GSC_STATE_DIR (default
    ``~/.config/gsc-mcp``) has no state yet but the on-share copies
    exist, copy them so the user keeps their OAuth logins without
    re-authenticating.

    Copy semantics (never move — the source may still back another
    user's bare-script install on a different mount name):

    - If ``$GSC_STATE_DIR/accounts/accounts.json`` is absent and the
      on-share ``accounts/`` tree has a manifest, copy the whole tree.
      The manifest stores *relative* token paths (``accounts/<a>/
      token.json``) which then resolve against GSC_STATE_DIR.
    - Else if ``$GSC_STATE_DIR/token.json`` is absent and the on-share
      bare ``token.json`` exists, copy just that file (fresh-install
      path handled downstream by _migrate_legacy_state).

    Idempotent: only copies when the destination is missing. A no-op
    once the state dir is populated, and a no-op after a non-editable
    install (site-packages has no on-share state to find).
    """
    # Legacy on-share locations, resolved from the *current* SCRIPT_DIR
    # (read at call time so monkeypatched tests stay isolated, and so a
    # non-editable install correctly points at site-packages — which has
    # no on-share state, making this a no-op there).
    legacy_token_file = os.path.join(SCRIPT_DIR, "token.json")
    legacy_accounts_dir = os.path.join(SCRIPT_DIR, "accounts")

    # Nothing to do if the source and destination are the same tree
    # (e.g. GSC_STATE_DIR left at its legacy SCRIPT_DIR value).
    if os.path.abspath(GSC_STATE_DIR) == os.path.abspath(SCRIPT_DIR):
        return

    os.makedirs(GSC_STATE_DIR, exist_ok=True)

    legacy_manifest = os.path.join(legacy_accounts_dir, "accounts.json")
    if not os.path.exists(ACCOUNTS_MANIFEST) and os.path.exists(legacy_manifest):
        shutil.copytree(legacy_accounts_dir, ACCOUNTS_DIR, dirs_exist_ok=True)
        print(
            f"[gsc-mcp] Migrated accounts/ from {legacy_accounts_dir} → "
            f"{ACCOUNTS_DIR}. Original preserved.",
            file=sys.stderr, flush=True,
        )
    elif not os.path.exists(TOKEN_FILE) and os.path.exists(legacy_token_file):
        shutil.copy2(legacy_token_file, TOKEN_FILE)
        print(
            f"[gsc-mcp] Migrated token.json from {legacy_token_file} → "
            f"{TOKEN_FILE}. Original preserved.",
            file=sys.stderr, flush=True,
        )


def _migrate_legacy_state() -> None:
    """One-time startup migration, covering both upgrade paths:

    1. **Fresh install with a legacy ``token.json``.** Copy it to
       ``accounts/legacy/token.json`` and register alias ``legacy`` in
       the manifest. (Pre-v1.2.0 used alias ``default`` for this; we
       now use ``legacy`` so the user-facing alias is meaningful.)

    2. **Upgrade from v1.1.x with a ``default`` alias in the manifest.**
       Rename the alias to ``legacy`` in-place. The underlying token
       file stays where it is — ``accounts/default/token.json`` is
       just an internal filesystem path at this point; only the alias
       is user-visible. If ``legacy`` is already taken, suffix with a
       timestamp.

    Both migrations are idempotent and persist ``active_account: None``
    (the field is preserved for manifest back-compat reads but is no
    longer consulted by the resolver).

    Deferred to first use: no network I/O, no blocking at import time.
    """
    global _migration_checked, _active_account
    if _migration_checked:
        return
    _migration_checked = True

    # Relocate on-share state into GSC_STATE_DIR before reading the
    # manifest, so the manifest we load below is the (possibly just
    # copied) home-dir copy.
    _migrate_state_dir()

    manifest = _load_manifest()
    mutated = False

    # Path 2: rename "default" → "legacy" in existing manifest.
    accounts = manifest.get("accounts") or {}
    if "default" in accounts:
        target = "legacy"
        if target in accounts:
            # Collision: try legacy_<ts>, then legacy_<ts>_1, _2, ...
            # until unique. The counter loop closes the sub-second
            # race window a bare timestamp would leave open.
            base = f"legacy_{int(datetime.now(timezone.utc).timestamp())}"
            target = base
            counter = 0
            while target in accounts:
                counter += 1
                target = f"{base}_{counter}"
        accounts[target] = dict(accounts["default"])
        accounts[target]["alias"] = target
        del accounts["default"]
        manifest["accounts"] = accounts
        # Drop the active_account field (v1.2.0 no longer uses it).
        manifest.pop("active_account", None)
        mutated = True
        print(
            f"[gsc-mcp] Migrated account alias 'default' → {target!r}. "
            f"The v1.2.0 resolver routes by site_url; 'default' is reserved.",
            file=sys.stderr, flush=True,
        )

    # Path 1: legacy token.json + no configured accounts → fresh install.
    if not accounts and os.path.exists(TOKEN_FILE):
        legacy_dir = os.path.join(ACCOUNTS_DIR, "legacy")
        os.makedirs(legacy_dir, exist_ok=True)
        dest = os.path.join(legacy_dir, "token.json")
        if not os.path.exists(dest):
            shutil.copy2(TOKEN_FILE, dest)
        manifest["accounts"] = {
            "legacy": {
                "alias": "legacy",
                "email": None,
                "token_file": "accounts/legacy/token.json",
                "added_at": datetime.now(timezone.utc).isoformat(),
            }
        }
        manifest.pop("active_account", None)
        mutated = True
        print(
            "[gsc-mcp] Migrated legacy token.json → accounts/legacy/token.json. "
            "Alias 'legacy' is available. Original token.json preserved.",
            file=sys.stderr, flush=True,
        )

    if mutated:
        _save_manifest(manifest)
    # _active_account stays None; the resolver is per-call in v1.2.0.
    _active_account = None


# Back-compat shim: old callers (including our own get_gsc_service_oauth)
# use the pre-v1.2.0 name. Forward to the new migration path.
def _maybe_migrate_legacy_token() -> None:
    _migrate_legacy_state()


# --- Shared date helper (used by landing-page tools) ---

def _parse_gsc_date(s: str) -> str:
    """Accepts 'today', 'yesterday', 'Ndaysago' (case-insensitive), or 'YYYY-MM-DD'.
    Returns an ISO date string (YYYY-MM-DD). Raises ValueError on unrecognised input.
    """
    if not isinstance(s, str) or not s.strip():
        raise ValueError(f"invalid date: {s!r}")
    normalized = s.strip().lower()
    today = _today_pt()  # v1.4.0: Pacific Time, the API's date convention
    if normalized == "today":
        return today.isoformat()
    if normalized == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    m = re.fullmatch(r"(\d+)daysago", normalized)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    # Assume ISO date; validate strictly
    datetime.strptime(s.strip(), "%Y-%m-%d")
    return s.strip()


def _sort_landing_page_diffs(
    diffs: List[Dict[str, Any]],
    sort_by: str,
    sort_direction: str,
) -> List[Dict[str, Any]]:
    """Sort landing-page delta rows, keeping None values for the sort column
    at the tail regardless of ascending/descending direction.

    The naive `(group, value)` sort key gets flipped by `reverse=True` and
    puts None rows at the FRONT of descending sorts. Partition-and-concatenate
    avoids that by sorting real values in-place and appending None rows as a
    tail that direction never touches.
    """
    real_rows = [r for r in diffs if r.get(sort_by) is not None]
    none_rows = [r for r in diffs if r.get(sort_by) is None]
    real_rows.sort(
        key=lambda r: float(r[sort_by]),
        reverse=(sort_direction.lower() == "desc"),
    )
    return real_rows + none_rows


# --- Screaming Frog CSV bridge helpers ---

def _to_float_or_none(v: Any) -> Optional[float]:
    """Coerce a value to float, returning None on failure. Used in sort keys and numeric filters."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _filter_value_eq(cell: Any, target: Any) -> bool:
    """Equality comparison for filter values.

    Only coerces numerically when the CALLER supplied a Python numeric
    target (int or float, but not bool — bool is a subclass of int in
    Python). String targets stay string-compared. This resolves the
    {"status_code": 200.0} vs cell "200" gotcha without introducing
    collapsing bugs for:
      - string targets like "200" vs "200.0" (stay !=)
      - leading-zero strings like "00123" (stay string)
      - non-finite values like "nan" vs "nan" (stay == via string compare
        — float('nan') == float('nan') is False per IEEE 754)
    """
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        a = _to_float_or_none(cell)
        b = float(target)
        if a is not None and math.isfinite(a) and math.isfinite(b):
            return a == b
    return str(cell) == str(target)


def _detect_encoding(path: Path) -> str:
    """Peek the first 2 bytes to detect UTF-16LE (Windows SF exports) vs UTF-8-BOM (macOS/Linux).
    Returns an encoding name suitable for open()."""
    try:
        with open(path, "rb") as f:
            head = f.read(2)
    except OSError:
        return "utf-8-sig"
    if head == b"\xff\xfe":
        return "utf-16"
    return "utf-8-sig"


def _normalize_column(raw: str, seen: Dict[str, int]) -> str:
    """Normalize a CSV header to a snake_case key.

    Order of operations is intentional:
      1. strip BOM/quotes, lowercase
      2. collapse whitespace/dots/hyphens/slashes to underscore
      3. drop non-alphanumeric
      4. apply semantic alias (Avg. Position -> position)
      5. dedupe via `seen` (position, position_2, ...)
    Alias BEFORE dedupe prevents 'Avg. Position' from stomping an existing 'position' column.
    """
    s = raw.lstrip("\ufeff").strip().strip('"').lower()
    s = re.sub(r"[ \.\-/]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        s = "col"
    # Apply alias BEFORE dedupe so aliased name participates in dedupe.
    s = _COLUMN_ALIAS.get(s, s)
    if s in seen:
        seen[s] += 1
        return f"{s}_{seen[s]}"
    seen[s] = 1
    return s


def _extract_snapshot_date(path: Path) -> Optional[str]:
    """Parse YYYY-MM-DD from a Screaming Frog timestamped folder name like 2026.04.08.09.04.01.
    Checks the given path's own name first, then its parent. None if no match."""
    for candidate in (path.name, path.parent.name if path.parent != path else ""):
        if not candidate:
            continue
        m = _SF_TIMESTAMP_RE.search(candidate)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _peek_sf_csv(path: Path) -> Dict[str, Any]:
    """Read the header, normalize columns, and count rows by streaming the file once.
    Returns metadata only — no row data is buffered."""
    encoding = _detect_encoding(path)
    try:
        with open(path, "r", encoding=encoding, newline="") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                return {
                    "columns": [],
                    "row_count": 0,
                    "empty": True,
                    "file": str(path),
                    "encoding": encoding,
                    "file_size": path.stat().st_size if path.exists() else 0,
                }
            seen: Dict[str, int] = {}
            columns = [_normalize_column(h, seen) for h in header]
            row_count = sum(1 for _ in reader)
    except UnicodeDecodeError as e:
        raise ValueError(f"could not decode {path.name} with {encoding}: {e}")
    return {
        "columns": columns,
        "row_count": row_count,
        "empty": row_count == 0,
        "file": str(path),
        "encoding": encoding,
        "file_size": path.stat().st_size,
    }


def _stream_sf_csv(
    dataset_meta: Dict[str, Any],
) -> Iterator[Dict[str, str]]:
    """Stream rows from a dataset's CSV file. Uses pre-normalized column names so
    subsequent filter/sort operations work on snake_case keys."""
    file_path = dataset_meta["file"]
    encoding = dataset_meta["encoding"]
    columns = dataset_meta["columns"]
    with open(file_path, "r", encoding=encoding, newline="") as f:
        reader = csv.reader(f)
        try:
            next(reader)  # skip header — we use our normalized columns instead
        except StopIteration:
            return
        n_cols = len(columns)
        for raw in reader:
            # Pad or truncate defensively so zip doesn't silently drop columns.
            if len(raw) < n_cols:
                raw = raw + [""] * (n_cols - len(raw))
            elif len(raw) > n_cols:
                raw = raw[:n_cols]
            yield dict(zip(columns, raw))


def _resolve_sf_dir(path: Path) -> Path:
    """Prefer path/search_console subfolder (brief forwards-compat) if it contains
    search_console_*.csv; otherwise treat path as a flat SF export root.
    Raises ValueError if neither layout yields any search_console_*.csv files."""
    sc_sub = path / "search_console"
    if sc_sub.is_dir() and any(sc_sub.glob("search_console_*.csv")):
        return sc_sub
    if any(path.glob("search_console_*.csv")):
        return path
    raise ValueError(
        f"no search_console_*.csv files found in {path} or {sc_sub}. "
        "Is this a Screaming Frog export folder?"
    )


def _apply_sf_filter(
    row: Dict[str, str],
    filter_spec: Dict[str, Any],
) -> bool:
    """Apply a filter dict to a single row. Filter values can be:
      - scalar (string/number): equality match. Numeric comparison when
        the scalar is a Python int/float; string comparison otherwise.
        See _filter_value_eq for exact semantics.
      - dict: {"op": "eq"|"contains"|"gt"|"lt"|"gte"|"lte", "value": ...}

    Equality (scalar form OR dict eq) uses _filter_value_eq, which only
    coerces numerically when the caller passed a Python numeric target.
    Ordered ops (gt/lt/gte/lte) always coerce both sides to float; rows
    where either side fails to coerce are excluded.

    Unknown ops or columns raise ValueError (caller surfaces as tool error).
    """
    for col, spec in filter_spec.items():
        if col not in row:
            raise ValueError(f"unknown column in filter: {col!r}")
        cell = row[col]
        if isinstance(spec, dict):
            op = spec.get("op", "eq")
            target = spec.get("value")
            if op == "eq":
                if not _filter_value_eq(cell, target):
                    return False
            elif op == "contains":
                if str(target).lower() not in str(cell).lower():
                    return False
            elif op in ("gt", "lt", "gte", "lte"):
                a = _to_float_or_none(cell)
                b = _to_float_or_none(target)
                if a is None or b is None:
                    return False
                if op == "gt" and not (a > b):
                    return False
                if op == "lt" and not (a < b):
                    return False
                if op == "gte" and not (a >= b):
                    return False
                if op == "lte" and not (a <= b):
                    return False
            else:
                raise ValueError(f"unsupported filter op: {op!r}")
        else:
            if not _filter_value_eq(cell, spec):
                return False
    return True


def get_gsc_service():
    """
    Returns an authorized Search Console service object.
    First tries OAuth authentication, then falls back to service account.
    """
    # Try OAuth authentication first if not skipped
    if not SKIP_OAUTH:
        try:
            return get_gsc_service_oauth()
        except HeadlessOAuthError:
            # Environment cannot complete an interactive OAuth flow; surface
            # the remediation message rather than falling through to the
            # service-account path (which will fail with a less useful error).
            raise
        except Exception as e:
            # If OAuth fails, try service account. stderr, not stdout — stdout
            # on an MCP stdio transport carries JSON-RPC frames.
            print(f"OAuth authentication failed: {str(e)}", file=sys.stderr, flush=True)
    
    # Try service account authentication
    for cred_path in POSSIBLE_CREDENTIAL_PATHS:
        if cred_path and os.path.exists(cred_path):
            try:
                creds = service_account.Credentials.from_service_account_file(
                    cred_path, scopes=SCOPES
                )
                return build("searchconsole", "v1", credentials=creds)
            except Exception as e:
                continue  # Try the next path if this one fails
    
    # If we get here, none of the authentication methods worked
    raise FileNotFoundError(
        f"Authentication failed. Please either:\n"
        f"1. Set up OAuth by placing a client_secrets.json file in the script directory, or\n"
        f"2. Set the GSC_CREDENTIALS_PATH environment variable or place a service account credentials file in one of these locations: "
        f"{', '.join([p for p in POSSIBLE_CREDENTIAL_PATHS[1:] if p])}"
    )

def get_gsc_service_oauth(token_file: Optional[str] = None):
    """
    Returns an authorized Search Console service object using OAuth.
    Resolves token file from explicit param; otherwise consults the
    manifest for the legacy fallback. Retained for the interactive
    OAuth path of ``gsc_add_account``; routed tool calls go through
    ``_build_service_noninteractive`` instead.
    """
    if token_file is None:
        token_file = _get_active_token_file()
    if token_file is None:
        token_file = TOKEN_FILE  # legacy fallback

    creds = None

    # Check if token file exists
    if os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        except Exception as e:
            # If token file is corrupted, delete it
            if os.path.exists(token_file):
                os.remove(token_file)
            creds = None

    # If credentials don't exist or are invalid, get new ones
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                # Save the refreshed credentials
                _write_token_file(token_file, creds.to_json())
            except Exception as e:
                # If refresh fails, delete the bad token and trigger new OAuth flow
                if os.path.exists(token_file):
                    os.remove(token_file)
                # Fall through to the OAuth flow below
                creds = None

        # Start new OAuth flow if we don't have valid credentials
        if not creds or not creds.valid:
            # Check if client secrets file exists
            if not os.path.exists(OAUTH_CLIENT_SECRETS_FILE):
                raise FileNotFoundError(
                    f"OAuth client secrets file not found. Please place a client_secrets.json file in the script directory "
                    f"or set the GSC_OAUTH_CLIENT_SECRETS_FILE environment variable."
                )

            # Start OAuth flow (use OAUTH_SCOPES to request email for account detection)
            flow = InstalledAppFlow.from_client_secrets_file(OAUTH_CLIENT_SECRETS_FILE, OAUTH_SCOPES)
            creds = _start_oauth_flow(flow, context="token refresh / initial login")

            # Save the credentials for future use
            _write_token_file(token_file, creds.to_json())

    # Build and return the service
    return build("searchconsole", "v1", credentials=creds)


class AccountResolverError(Exception):
    """Raised by :func:`_resolve_account` when no single account can be
    chosen for a site_url (or when an explicit alias can't serve one).

    Carries the envelope-shaping fields so every routed tool can emit a
    standard error envelope with a single ``e.to_envelope(tool=...)``
    call instead of duplicating the mapping 20 times.
    """

    def __init__(
        self,
        *,
        code: str,
        error: str,
        hint: str = "",
        retryable: Optional[bool] = None,
        alternatives: Optional[List[str]] = None,
        site_url: Optional[str] = None,
    ) -> None:
        super().__init__(error)
        self.code = code
        self.error = error
        self.hint = hint
        self.retryable = retryable
        self.alternatives = alternatives
        self.site_url = site_url

    def to_envelope(self, *, tool: str) -> Dict[str, Any]:
        extras: Dict[str, Any] = {}
        if self.alternatives is not None:
            extras["alternatives"] = self.alternatives
        if self.site_url is not None:
            extras["site_url"] = self.site_url
        return _make_error_envelope(
            error=self.error,
            hint=self.hint,
            error_code=self.code,
            retryable=self.retryable,
            tool=tool,
            **extras,
        )


# Tri-state property cache. Keys are account aliases; states:
#   "never"  — never loaded (or invalidated). Must refresh before trusting.
#   "ok"     — last refresh succeeded; ``_account_properties[alias]`` is
#              the authoritative set of site URLs for that account.
#   "error"  — last refresh failed. Alias is NEITHER a candidate NOR a
#              known-negative — resolver returns ACCOUNT_RESOLUTION_INCOMPLETE
#              rather than silently excluding it.
#
# Conflating "error" with empty-set would convert transient 500s during
# discovery into false NO_ACCOUNT_FOR_PROPERTY / ACCOUNT_SITE_MISMATCH
# verdicts — exactly the confused-deputy class of failure this refactor
# is meant to close off.
_account_property_state: Dict[str, str] = {}          # alias -> "never"|"ok"|"error"
_account_properties: Dict[str, set] = {}              # alias -> {site_url,...}, valid iff state=="ok"
_account_property_error: Dict[str, Optional[str]] = {}  # alias -> last error_code, valid iff state=="error"
_account_property_refreshed_at: Dict[str, float] = {}  # alias -> unix ts of last refresh
_alias_locks: Dict[str, asyncio.Lock] = {}            # per-alias refresh serialisation
_alias_locks_mutex = asyncio.Lock()                   # protects lock-dict mutation


async def _get_alias_lock(alias: str) -> asyncio.Lock:
    """Get-or-create the per-alias refresh lock. Avoids a single global
    lock that would serialise unrelated accounts' refreshes."""
    async with _alias_locks_mutex:
        lock = _alias_locks.get(alias)
        if lock is None:
            lock = asyncio.Lock()
            _alias_locks[alias] = lock
        return lock


def _invalidate_property_cache(alias: str) -> None:
    """Drop the alias's cached property snapshot so the next resolver
    lookup will rediscover from the Google API.

    Called from two sites where the cache is known-stale:
    ``gsc_add_site`` (we just added a new property that the cache can't
    possibly know about) and ``_call_with_stale_retry`` (403 on an
    auto-resolved call means the cached "can reach" judgment is wrong).

    Safe to call without holding the per-alias lock: dict writes are
    atomic under the stdio single-threaded asyncio loop, and the worst
    concurrent case is one wasted refresh.
    """
    _account_property_state[alias] = "never"
    _account_properties.pop(alias, None)
    _account_property_error.pop(alias, None)


async def _ensure_property_cache(
    alias: str,
    *,
    force_refresh: bool = False,
) -> None:
    """Ensure ``alias`` has a known-good or known-error property snapshot.

    - ``force_refresh=False`` (default): no-op if state is already ``ok``
      or ``error`` from a previous refresh attempt.
    - ``force_refresh=True``: always re-runs discovery.

    Uses a per-alias lock so concurrent callers see at most one
    discovery call per alias. Double-checks state after acquiring the
    lock to avoid redundant refresh when another task just finished.

    Never raises — failures land in ``state == "error"`` + error_code.
    Resolver inspects state/error_code to produce the right envelope.
    """
    lock = await _get_alias_lock(alias)
    async with lock:
        current = _account_property_state.get(alias, "never")
        if not force_refresh and current in ("ok", "error"):
            return

        service, err = await asyncio.to_thread(_build_service_noninteractive, alias)
        if service is None:
            _account_property_state[alias] = "error"
            _account_property_error[alias] = err or ErrorCode.INTERNAL_ERROR
            _account_properties.pop(alias, None)
            _account_property_refreshed_at[alias] = time.time()
            return

        try:
            site_list = await _gsc_execute(
                lambda: service.sites().list().execute(), step="sites.list",
            )
        except Exception:
            # Discovery failed (403, 500, network). Mark state="error"
            # so the resolver treats this alias as "unknown" — neither
            # a candidate nor a known-negative. ``_account_properties``
            # is left in place for inspection but the resolver's
            # ``_classify`` only counts aliases with state=="ok"; that
            # data is effectively dead while state is "error".
            #
            # We deliberately do NOT fall back to any stale snapshot:
            # doing so would reintroduce the confused-deputy risk that
            # drove this refactor. If access was revoked in GSC after
            # the cache warmed, routing to the "last-known-good" alias
            # would silently hit the wrong account.
            _account_property_state[alias] = "error"
            _account_property_error[alias] = ErrorCode.SERVICE_UNAVAILABLE
            _account_property_refreshed_at[alias] = time.time()
            return

        sites = site_list.get("siteEntry", []) or []
        props: set = set()
        for entry in sites:
            url = entry.get("siteUrl")
            if url:
                props.add(url)
        _account_properties[alias] = props
        _account_property_state[alias] = "ok"
        _account_property_error.pop(alias, None)
        _account_property_refreshed_at[alias] = time.time()


def _list_configured_aliases() -> List[str]:
    """Sorted alias list for the resolver and discovery tools.

    Runs the legacy-state migration first: without this, a pre-v1.2.0
    manifest still containing ``default`` would leak that reserved
    alias into routing decisions before the migration had a chance to
    rename it. Deferring migration to here (instead of server start)
    keeps import-time fast and lets tests that manipulate the manifest
    directly control when migration runs.
    """
    _maybe_migrate_legacy_token()
    manifest = _load_manifest()
    return sorted((manifest.get("accounts") or {}).keys())


async def _resolve_account(
    site_url: str,
    account_alias: Optional[str],
) -> str:
    """Return the alias that should serve ``site_url``.

    Explicit-alias path: verify the alias exists and has access; raise
    ``ACCOUNT_SITE_MISMATCH`` if not. Auth/discovery failure for the
    explicit alias surfaces as ``AUTH_EXPIRED`` / ``SERVICE_UNAVAILABLE``
    rather than pretending mismatch — otherwise a transient network blip
    would look like a credential mix-up.

    Auto-resolve path: refresh all ``never`` aliases, then classify:

    - >1 candidates → ``AMBIGUOUS_ACCOUNT`` with ``alternatives``.
    - 1 candidate + zero ``error`` aliases → use it.
    - 1 candidate + some ``error`` aliases → ``ACCOUNT_RESOLUTION_INCOMPLETE``
      (can't claim uniqueness; another error-state alias might also match).
    - 0 candidates + zero errors → force-refresh once (newly-added site
      case), re-check; still zero → ``NO_ACCOUNT_FOR_PROPERTY``.
    - 0 candidates + some errors → ``ACCOUNT_RESOLUTION_INCOMPLETE``.

    Never raises anything except :class:`AccountResolverError`.
    """
    aliases = _list_configured_aliases()
    if not aliases:
        raise AccountResolverError(
            code=ErrorCode.NO_ACCOUNTS_CONFIGURED,
            error="No GSC accounts configured.",
            hint="Run `gsc_add_account` with an alias to authorise an account.",
            site_url=site_url,
        )

    if account_alias is not None:
        try:
            alias = _validate_alias(account_alias)
        except ValueError as e:
            raise AccountResolverError(
                code=ErrorCode.BAD_REQUEST,
                error=f"Invalid account_alias: {e}",
                hint="Aliases are 1-30 chars, lowercase alphanumerics and hyphens.",
                site_url=site_url,
            )
        if alias not in aliases:
            raise AccountResolverError(
                code=ErrorCode.ACCOUNT_SITE_MISMATCH,
                error=f"Account alias {alias!r} is not configured.",
                hint=f"Configured aliases: {aliases}. Use gsc_list_accounts to verify.",
                alternatives=aliases,
                site_url=site_url,
            )
        await _ensure_property_cache(alias)
        state = _account_property_state.get(alias, "never")
        if state == "error":
            err_code = _account_property_error.get(alias) or ErrorCode.INTERNAL_ERROR
            # Hint bifurcates on code so agents don't loop-retry on
            # AUTH_EXPIRED (which usually needs human re-auth) but DO
            # retry on SERVICE_UNAVAILABLE (transient backend blip).
            # retryable derives from the code via _RETRYABLE_CODES.
            if err_code == ErrorCode.AUTH_EXPIRED:
                hint = (
                    f"Account {alias!r}'s token is no longer valid. "
                    f"Re-auth via `gsc_add_account {alias}`."
                )
            else:
                hint = (
                    f"Discovery for {alias!r} failed transiently. "
                    f"Retry after a short backoff."
                )
            raise AccountResolverError(
                code=err_code,
                error=f"Cannot verify account {alias!r} access right now.",
                hint=hint,
                site_url=site_url,
            )
        if site_url in _account_properties.get(alias, set()):
            return alias
        raise AccountResolverError(
            code=ErrorCode.ACCOUNT_SITE_MISMATCH,
            error=f"Account {alias!r} does not have access to {site_url!r}.",
            hint=(
                f"Use gsc_whoami(site_url={site_url!r}) to see which accounts can; "
                f"or gsc_list_properties(account_alias={alias!r}) to see what it can."
            ),
            site_url=site_url,
        )

    # Auto-resolve: refresh any never-loaded aliases.
    await asyncio.gather(
        *(_ensure_property_cache(a) for a in aliases),
        return_exceptions=False,
    )

    def _classify() -> Tuple[List[str], List[str]]:
        cands = [
            a for a in aliases
            if _account_property_state.get(a) == "ok"
            and site_url in _account_properties.get(a, set())
        ]
        errs = [a for a in aliases if _account_property_state.get(a) == "error"]
        return cands, errs

    candidates, errors = _classify()

    if len(candidates) > 1:
        raise AccountResolverError(
            code=ErrorCode.AMBIGUOUS_ACCOUNT,
            error=f"Multiple accounts have access to {site_url!r}.",
            hint=(
                f"Pass account_alias explicitly to disambiguate. "
                f"Candidates: {sorted(candidates)}."
            ),
            alternatives=sorted(candidates),
            site_url=site_url,
        )

    if len(candidates) == 1 and not errors:
        return candidates[0]

    if len(candidates) == 1 and errors:
        raise AccountResolverError(
            code=ErrorCode.ACCOUNT_RESOLUTION_INCOMPLETE,
            error=(
                f"Account resolution incomplete for {site_url!r}: "
                f"cannot confirm uniqueness while {errors} are unreachable."
            ),
            hint=(
                f"Retry after a backoff, or pass account_alias explicitly "
                f"(e.g. {candidates[0]!r}) to bypass discovery."
            ),
            alternatives=errors,
            site_url=site_url,
        )

    # Zero candidates case — two sub-paths.
    if not errors:
        # Force refresh once in case the property was added after first
        # discovery populated the cache.
        await asyncio.gather(
            *(_ensure_property_cache(a, force_refresh=True) for a in aliases),
            return_exceptions=False,
        )
        candidates, errors = _classify()
        if len(candidates) == 1 and not errors:
            return candidates[0]
        if len(candidates) > 1:
            raise AccountResolverError(
                code=ErrorCode.AMBIGUOUS_ACCOUNT,
                error=f"Multiple accounts have access to {site_url!r}.",
                hint=(
                    f"Pass account_alias explicitly to disambiguate. "
                    f"Candidates: {sorted(candidates)}."
                ),
                alternatives=sorted(candidates),
                site_url=site_url,
            )
        if errors:
            raise AccountResolverError(
                code=ErrorCode.ACCOUNT_RESOLUTION_INCOMPLETE,
                error=(
                    f"Account resolution incomplete for {site_url!r}: "
                    f"{errors} unreachable on force-refresh."
                ),
                hint="Retry after a backoff, or pass account_alias explicitly.",
                alternatives=errors,
                site_url=site_url,
            )
        # Still zero candidates after force-refresh — genuinely no access.
        raise AccountResolverError(
            code=ErrorCode.NO_ACCOUNT_FOR_PROPERTY,
            error=f"No configured account has access to {site_url!r}.",
            hint=(
                f"Use gsc_list_accounts(include_properties=True) to audit coverage, "
                f"or gsc_add_account to authorise an account with access."
            ),
            site_url=site_url,
        )

    # candidates == 0 AND errors != []
    raise AccountResolverError(
        code=ErrorCode.ACCOUNT_RESOLUTION_INCOMPLETE,
        error=(
            f"Account resolution incomplete for {site_url!r}: "
            f"{errors} unreachable during discovery."
        ),
        hint="Retry after a backoff, or pass account_alias explicitly to bypass discovery.",
        alternatives=errors,
        site_url=site_url,
    )


async def get_gsc_service_for_site(
    site_url: str,
    account_alias: Optional[str],
) -> Tuple[str, Any]:
    """Resolve the account for ``site_url`` and return an authenticated
    Google Search Console service for it.

    Returns ``(resolved_alias, service)``. Raises
    :class:`AccountResolverError` on resolution failure. Raises a wrapped
    :class:`AccountResolverError` with ``AUTH_EXPIRED`` /
    ``SERVICE_UNAVAILABLE`` on auth failure for the resolved alias —
    callers convert via ``err.to_envelope(tool=...)``.

    Safety: uses only :func:`_build_service_noninteractive`. Never
    launches browser OAuth. Never falls back to service-account creds.
    """
    resolved_alias = await _resolve_account(site_url, account_alias)
    service, err_code = await asyncio.to_thread(
        _build_service_noninteractive, resolved_alias
    )
    if service is None:
        # Resolver just verified this alias had state=="ok" — if auth
        # fails here it's a brief race (token expired between discovery
        # and use) or a token revoked in the narrow window. The race
        # case IS genuinely transient, so explicitly mark the error as
        # retryable even though AUTH_EXPIRED is non-retryable by
        # default (see _RETRYABLE_CODES). Worst case the agent retries
        # once and the second attempt surfaces the non-retryable
        # AUTH_EXPIRED from the resolver path.
        raise AccountResolverError(
            code=err_code or ErrorCode.INTERNAL_ERROR,
            error=f"Could not authenticate account {resolved_alias!r}.",
            hint=(
                "Token may have expired between discovery and use; retry once. "
                "If the retry also fails, re-auth via gsc_add_account."
            ),
            retryable=True,
            site_url=site_url,
        )
    return resolved_alias, service


async def _call_with_stale_retry(
    *,
    site_url: str,
    account_alias: Optional[str],
    api_call,
    step: str = "api_call",
) -> Tuple[str, Any, Any]:
    """Resolve → build service → invoke ``api_call(service)``.

    On HTTP 403 from ``api_call`` AND ``account_alias is None`` (auto-
    resolved path), invalidate the resolved alias's property cache,
    re-resolve once, and retry if resolution picks a different alias.
    Explicit-alias callers do not retry — they chose that credential
    and 403 should surface as PERMISSION_DENIED straight away.

    Returns ``(resolved_alias, service, result)``. The returned service
    is the one used by the retry if a retry occurred, so downstream
    calls in the tool body target the right account.

    Raises :class:`AccountResolverError` or :class:`HttpError` — the
    caller's existing error-envelope handlers deal with both.
    """
    resolved_alias, service = await get_gsc_service_for_site(site_url, account_alias)
    try:
        result = await _gsc_execute(lambda: api_call(service), step=step)
        return resolved_alias, service, result
    except HttpError as e:
        if account_alias is not None:
            raise
        if getattr(e.resp, "status", None) != 403:
            raise
        _invalidate_property_cache(resolved_alias)
        try:
            new_alias, new_service = await get_gsc_service_for_site(site_url, None)
        except AccountResolverError:
            # Re-resolve itself failed — surface the original 403 so
            # the caller sees the actual Google API response, not a
            # resolver error that masks it.
            raise e
        if new_alias == resolved_alias:
            # Resolution picked the same alias — no recovery possible.
            raise e
        result = await _gsc_execute(lambda: api_call(new_service), step=step)
        return new_alias, new_service, result


def _build_service_noninteractive(alias: str) -> Tuple[Optional[Any], Optional[str]]:
    """Build a GSC service for ``alias`` without any interactive fallback.

    Used by the account resolver (discovery) and by routed tool calls in
    v1.2.0+. Safety contract:

    * NEVER launches ``InstalledAppFlow.run_local_server`` (no browser).
    * NEVER deletes the token file on refresh failure — a transient
      refresh error should not force a full re-auth on the next call.
      Users recover via ``gsc_add_account``, which owns the interactive
      path.
    * NEVER falls back to service-account credentials — that path exists
      only for ``get_gsc_service`` and would silently satisfy an
      alias-routed request with the wrong identity.
    * Persists the refreshed token ONLY after a successful refresh.

    Returns ``(service, None)`` on success or ``(None, error_code)`` on
    failure, where ``error_code`` is one of :class:`ErrorCode` values
    ``AUTH_EXPIRED``, ``SERVICE_UNAVAILABLE``, or ``INTERNAL_ERROR``.
    Callers decide how to surface the failure (envelope, re-raise, etc).
    """
    # Ensure the default→(per-account) migration has run so the manifest
    # is internally consistent. Cheap after first call.
    _maybe_migrate_legacy_token()
    manifest = _load_manifest()
    acct = manifest.get("accounts", {}).get(alias)
    if acct is None:
        # Resolver should have caught this upstream; treat as internal
        # (not AUTH_EXPIRED, which would mislead a retrying caller).
        return None, ErrorCode.INTERNAL_ERROR

    token_path = acct.get("token_file")
    if not token_path:
        return None, ErrorCode.AUTH_EXPIRED
    if not os.path.isabs(token_path):
        token_path = os.path.join(GSC_STATE_DIR, token_path)
    if not os.path.exists(token_path):
        return None, ErrorCode.AUTH_EXPIRED

    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    except Exception:
        # Corrupted token file. Do NOT delete — user can fix via
        # gsc_add_account re-auth, which writes a fresh token.
        return None, ErrorCode.AUTH_EXPIRED

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                # Refresh failed (network, revoked token, server error).
                # Leave token file intact; next call will re-try. If the
                # token is genuinely revoked the user re-runs
                # gsc_add_account.
                return None, ErrorCode.AUTH_EXPIRED
            # Persist refreshed token on success only. Best-effort write:
            # a disk error here is not fatal because creds are valid
            # in-memory for this call.
            try:
                _write_token_file(token_path, creds.to_json())
            except OSError:
                pass
        else:
            # No refresh token, or invalid in a non-expired way. Can't
            # recover without interactive auth.
            return None, ErrorCode.AUTH_EXPIRED

    try:
        return build("searchconsole", "v1", credentials=creds), None
    except Exception:
        # discovery/build failure — treat as transient upstream.
        return None, ErrorCode.SERVICE_UNAVAILABLE


# --- Search Analytics engine (v1.4.0) ---
# One code path for every searchanalytics.query caller: request validation,
# date windows in Pacific Time, pagination, client-side sorting, freshness
# metadata and the page-total vs query-sum gap. `gsc_query` exposes it
# directly; the older analytics tools call it and keep their own output
# shapes.


class _SaValidationError(ValueError):
    """Caller error in a Search Analytics request; rendered as BAD_REQUEST."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def _norm_key(value: Any) -> str:
    return str(value).strip().replace("_", "").replace("-", "").replace(" ", "").lower()


def _norm_enum(value: Any, table: Dict[str, str], *, field: str) -> str:
    key = _norm_key(value)
    if key not in table:
        raise _SaValidationError(
            f"Unknown {field}: {value!r}.",
            f"Valid {field} values: {sorted(set(table.values()))}.",
        )
    return table[key]


_SA_DIMENSIONS = {
    "date": "date", "hour": "hour", "query": "query", "page": "page",
    "country": "country", "device": "device", "searchappearance": "searchAppearance",
}
_SA_FILTER_DIMENSIONS = {k: v for k, v in _SA_DIMENSIONS.items() if v not in ("date", "hour")}
_SA_TYPES = {
    "web": "web", "image": "image", "video": "video", "news": "news",
    "googlenews": "googleNews", "discover": "discover",
}
_SA_OPERATORS = {
    "equals": "equals", "notequals": "notEquals", "contains": "contains",
    "notcontains": "notContains", "includingregex": "includingRegex",
    "excludingregex": "excludingRegex",
}
_SA_AGGREGATIONS = {
    "auto": "auto", "bypage": "byPage", "byproperty": "byProperty",
    "bynewsshowcasepanel": "byNewsShowcasePanel",
}
_SA_DATA_STATES = {"final": "final", "all": "all", "hourlyall": "hourly_all"}
_SA_SORT_METRICS = ("clicks", "impressions", "ctr", "position")
_SA_PAGE_SIZE = 25000          # API maximum rowLimit
_SA_DEFAULT_MAX_ROWS = 100_000  # fetch_all hard cap unless the caller raises it
_SA_LOOKAROUND_RE = re.compile(r"\(\?(?:[=!]|<[=!])")

# Freshness: the latest date whose data is final, per (site, type), cached
# for an hour. See _latest_final_date.
_LATEST_FINAL_TTL_SEC = 3600
_latest_final_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}


def _parse_dimensions(dimensions: Any, *, allow_empty: bool = False) -> List[str]:
    if dimensions is None:
        items: List[Any] = []
    elif isinstance(dimensions, str):
        items = [d for d in dimensions.split(",") if d.strip()]
    else:
        items = list(dimensions)
    dims = [_norm_enum(d, _SA_DIMENSIONS, field="dimension") for d in items]
    if len(set(dims)) != len(dims):
        raise _SaValidationError(f"Duplicate dimension in {dims}.", "List each dimension once.")
    if not dims and not allow_empty:
        raise _SaValidationError("At least one dimension is required.", "e.g. dimensions=['query'].")
    return dims


def _normalize_filter_groups(filter_groups: Any) -> List[Dict[str, Any]]:
    """Accept ``[{group_type:'and', filters:[{dimension, operator, expression}]}]``,
    or a flat list of filters (one AND group). Returns API-shaped groups."""
    if not filter_groups:
        return []
    if isinstance(filter_groups, dict):
        filter_groups = [filter_groups]
    if not isinstance(filter_groups, list):
        raise _SaValidationError("filter_groups must be a list.", "See the gsc_query docstring for the shape.")
    if all(isinstance(g, dict) and "filters" not in g for g in filter_groups):
        filter_groups = [{"filters": filter_groups}]
    groups: List[Dict[str, Any]] = []
    for group in filter_groups:
        if not isinstance(group, dict) or not isinstance(group.get("filters"), list):
            raise _SaValidationError("Each filter group needs a 'filters' list.", "")
        group_type = group.get("group_type", group.get("groupType", "and"))
        if _norm_key(group_type) != "and":
            raise _SaValidationError(f"Unsupported group_type {group_type!r}.", "The API supports only 'and'.")
        filters = []
        for f in group["filters"]:
            if not isinstance(f, dict):
                raise _SaValidationError(f"Filter must be an object, got {f!r}.", "")
            dim = _norm_enum(f.get("dimension"), _SA_FILTER_DIMENSIONS, field="filter dimension")
            op = _norm_enum(f.get("operator", "equals"), _SA_OPERATORS, field="filter operator")
            expr = f.get("expression")
            if not isinstance(expr, str) or expr == "":
                raise _SaValidationError(f"Filter on {dim} needs a non-empty expression.", "")
            if op in ("includingRegex", "excludingRegex"):
                if _SA_LOOKAROUND_RE.search(expr):
                    raise _SaValidationError(
                        f"Regex {expr!r} uses lookaround, which Google's RE2 engine does not support.",
                        "Rewrite without (?=, (?!, (?<= or (?<!; e.g. use excludingRegex for negation.",
                    )
                try:
                    re.compile(expr)
                except re.error as e:
                    raise _SaValidationError(f"Invalid regex {expr!r}: {e}.", "The API uses RE2 syntax.")
            filters.append({"dimension": dim, "operator": op, "expression": expr})
        if filters:
            groups.append({"filters": filters})
    return groups


def _check_sa_compat(
    *,
    dimensions: List[str],
    filter_groups: List[Dict[str, Any]],
    search_type: str,
    aggregation_type: Optional[str],
    data_state: str,
) -> None:
    """Reject combinations the API documents as invalid (searchanalytics.query
    reference, last updated 2026-08-11). Anything else passes through, and a
    Google 400 surfaces as BAD_REQUEST naming the step."""
    filters = [f for g in filter_groups for f in g["filters"]]
    uses_page = "page" in dimensions or any(f["dimension"] == "page" for f in filters)
    if "hour" in dimensions and data_state != "hourly_all":
        raise _SaValidationError(
            "The 'hour' dimension needs data_state='hourly_all'.",
            "Pass data_state='hourly_all' (hourly data covers roughly the last 10 days).",
        )
    if aggregation_type == "byProperty" and uses_page:
        raise _SaValidationError(
            "If you group or filter by page, you cannot aggregate by property.",
            "Use aggregation_type='byPage' or 'auto'.",
        )
    if aggregation_type == "byProperty" and search_type in ("discover", "googleNews"):
        raise _SaValidationError(
            f"byProperty aggregation is not supported for type={search_type!r}.",
            "Use aggregation_type='byPage' or 'auto'.",
        )
    if aggregation_type == "byNewsShowcasePanel":
        showcase = any(
            f["dimension"] == "searchAppearance" and f["operator"] == "equals"
            and f["expression"].upper() == "NEWS_SHOWCASE"
            for f in filters
        )
        other_appearance = any(
            f["dimension"] == "searchAppearance" and f["expression"].upper() != "NEWS_SHOWCASE"
            for f in filters
        )
        if search_type not in ("discover", "googleNews") or not showcase or uses_page or other_appearance:
            raise _SaValidationError(
                "byNewsShowcasePanel needs type discover or googleNews, a searchAppearance "
                "equals NEWS_SHOWCASE filter, and no page grouping/filter or other appearance filter.",
                "See the searchanalytics.query reference for aggregationType.",
            )


def _build_sa_body(
    *,
    start_date: str,
    end_date: str,
    dimensions: List[str],
    search_type: Optional[str] = None,
    filter_groups: Optional[List[Dict[str, Any]]] = None,
    aggregation_type: Optional[str] = None,
    data_state: Optional[str] = None,
    row_limit: int = 1000,
    start_row: int = 0,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": list(dimensions),
        "rowLimit": row_limit,
    }
    if start_row:
        body["startRow"] = start_row
    if search_type:
        body["type"] = search_type
    if filter_groups:
        body["dimensionFilterGroups"] = filter_groups
    if aggregation_type:
        body["aggregationType"] = aggregation_type
    if data_state and data_state != "final":
        body["dataState"] = data_state
    return body


class _SaContext:
    """A resolved account + service for one tool call. Carries the 403
    stale-cache re-resolve so every analytics call gets it once."""

    def __init__(self, site_url: str, account_alias: Optional[str], alias: str, service: Any) -> None:
        self.site_url = site_url
        self.account_alias = account_alias
        self.alias = alias
        self.service = service
        self.re_resolved = False


async def _sa_context(site_url: str, account_alias: Optional[str]) -> _SaContext:
    alias, service = await get_gsc_service_for_site(site_url, account_alias)
    return _SaContext(site_url, account_alias, alias, service)


async def _sa_exec(ctx: _SaContext, body: Dict[str, Any], *, step: str) -> Dict[str, Any]:
    def _call(svc):
        return lambda: svc.searchanalytics().query(siteUrl=ctx.site_url, body=body).execute()

    try:
        return await _gsc_execute(_call(ctx.service), step=step) or {}
    except HttpError as e:
        info = _http_error_details(e)
        if ctx.account_alias is not None or ctx.re_resolved or info["status"] != 403 or info["kind"] != "other":
            raise
        ctx.re_resolved = True
        _invalidate_property_cache(ctx.alias)
        try:
            new_alias, new_service = await get_gsc_service_for_site(ctx.site_url, None)
        except AccountResolverError:
            raise e
        if new_alias == ctx.alias:
            raise
        ctx.alias, ctx.service = new_alias, new_service
        return await _gsc_execute(_call(new_service), step=step) or {}


def _response_metadata(resp: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Freshness markers. The reference documents snake_case names; accept
    camelCase too in case the wire form differs."""
    meta = resp.get("metadata") or {}
    return {
        "first_incomplete_date": meta.get("first_incomplete_date") or meta.get("firstIncompleteDate"),
        "first_incomplete_hour": meta.get("first_incomplete_hour") or meta.get("firstIncompleteHour"),
    }


async def _latest_final_date(ctx: _SaContext, search_type: Optional[str] = None) -> Dict[str, Any]:
    """Latest date with FINAL data for ``ctx.site_url``, cached for an hour.

    Primary: one date-grouped probe over the last 10 days with
    dataState=all; final data stops the day before ``first_incomplete_date``.
    Fallbacks: the max populated date from a final-data probe (sparse
    properties), then today PT − 3 with a warning. ``source`` says which.
    """
    stype = search_type or "web"
    key = (ctx.site_url, stype)
    hit = _latest_final_cache.get(key)
    if hit and time.time() - hit[0] < _LATEST_FINAL_TTL_SEC:
        return dict(hit[1])
    today = _today_pt()
    base = {
        "startDate": (today - timedelta(days=10)).isoformat(),
        "endDate": today.isoformat(),
        "dimensions": ["date"],
        "rowLimit": 20,
    }
    if search_type and search_type != "web":
        base["type"] = search_type
    result: Dict[str, Any] = {"date": None, "source": None, "warning": None}
    # A failing probe must not fail the caller's query: fall through to the
    # next source (a real auth/permission problem surfaces on the main call).
    try:
        resp = await _sa_exec(ctx, dict(base, dataState="all"), step="freshness_probe")
    except HttpError:
        resp = {}
    fid = _response_metadata(resp)["first_incomplete_date"]
    if fid:
        try:
            result["date"] = (datetime.strptime(fid, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
            result["source"] = "metadata"
        except ValueError:
            pass
    if result["date"] is None:
        try:
            resp = await _sa_exec(ctx, base, step="freshness_probe_final")
        except HttpError:
            resp = {}
        dates = [r["keys"][0] for r in resp.get("rows") or [] if r.get("keys") and r["keys"][0]]
        if dates:
            result["date"] = max(dates)
            result["source"] = "populated"
    if result["date"] is None:
        result["date"] = (today - timedelta(days=3)).isoformat()
        result["source"] = "fallback"
        result["warning"] = "No freshness data for this property; assumed final data ends 3 days ago (PT)."
    _latest_final_cache[key] = (time.time(), dict(result))
    return result


def _as_date(value: Any):
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value
    return datetime.strptime(_parse_gsc_date(str(value)), "%Y-%m-%d").date()


async def _resolve_window(
    ctx: _SaContext,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    days: Optional[int] = None,
    data_state: str = "final",
    search_type: Optional[str] = None,
    default_days: int = 28,
) -> Dict[str, Any]:
    """Resolve the query window and describe it for ``meta``.

    Explicit dates win. Otherwise ``days=N`` means the last N days of final
    data (inclusive, ending on ``latest_final_date``, like the GSC UI), or N
    days ending today PT when ``data_state`` is ``all``/``hourly_all``.
    """
    freshness = await _latest_final_date(ctx, search_type)
    latest_final = datetime.strptime(freshness["date"], "%Y-%m-%d").date()
    warnings: List[str] = []
    if freshness.get("warning"):
        warnings.append(freshness["warning"])
    fresh_end = _today_pt() if data_state in ("all", "hourly_all") else latest_final
    n = None
    if days is not None:
        n = max(1, int(days))
    if start_date or end_date:
        end = _as_date(end_date) if end_date else fresh_end
        if start_date:
            start = _as_date(start_date)
        else:
            start = end - timedelta(days=(n or default_days) - 1)
    else:
        n = n or default_days
        end = fresh_end
        start = end - timedelta(days=n - 1)
    if start > end:
        raise _SaValidationError(
            f"start_date {start.isoformat()} is after end_date {end.isoformat()}.", "Swap the dates.",
        )
    non_final = []
    d = max(start, latest_final + timedelta(days=1))
    while d <= end:
        non_final.append(d.isoformat())
        d += timedelta(days=1)
    if non_final and data_state == "final":
        warnings.append(
            f"end_date {end.isoformat()} is after the latest final date {latest_final.isoformat()}; "
            f"{len(non_final)} day(s) have no final data yet. Pass data_state='all' for preliminary data."
        )
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "window_days": (end - start).days + 1,
        "days_requested": n if not (start_date or end_date) else None,
        "data_state": data_state,
        "latest_final_date": latest_final.isoformat(),
        "latest_final_date_source": freshness["source"],
        "non_final_days": non_final,
        "timezone": _PT_LABEL,
        "warnings": warnings,
    }


async def _resolve_comparison_windows(
    ctx: _SaContext,
    *,
    days: int,
    data_state: str = "final",
    search_type: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Two equal-length adjacent windows for ``days=N`` comparisons:
    ``(earlier, later)``. The later one is the last N final days; the
    earlier one is the N days before it, so both end on final data."""
    later = await _resolve_window(ctx, days=days, data_state=data_state, search_type=search_type)
    later_start = datetime.strptime(later["start_date"], "%Y-%m-%d").date()
    n = later["window_days"]
    earlier_end = later_start - timedelta(days=1)
    earlier = await _resolve_window(
        ctx,
        start_date=(earlier_end - timedelta(days=n - 1)).isoformat(),
        end_date=earlier_end.isoformat(),
        data_state=data_state,
        search_type=search_type,
    )
    return earlier, later


def _comparison_mode(explicit: List[Optional[str]], days: Optional[int]) -> bool:
    """True to use ``days`` windows. Explicit dates win when all four are
    given (the caller then warns that days was ignored); a partial set is
    ambiguous and rejected."""
    if all(explicit):
        return False
    if any(explicit):
        raise _SaValidationError(
            "Pass all four period dates, or days=N alone (a partial set of dates is ambiguous).",
            "days=N compares the last N final days with the N days before them.",
        )
    if days is None:
        raise _SaValidationError(
            "Pass all four period dates, or days=N.",
            "days=N compares the last N final days with the N days before them.",
        )
    return True


def _window_line(w: Dict[str, Any]) -> str:
    extra = f", {len(w['non_final_days'])} non-final" if w.get("non_final_days") else ""
    return (
        f"Window: {w['start_date']} → {w['end_date']} ({w['window_days']} days{extra}, "
        f"data_state={w['data_state']}, latest final {w['latest_final_date']}, Pacific Time)"
    )


def _window_meta(w: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "start_date", "end_date", "window_days", "data_state", "latest_final_date",
        "latest_final_date_source", "non_final_days", "timezone",
    )
    return {k: w.get(k) for k in keys}


def _standard_meta(w: Optional[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    """§5 meta block: the window echo plus row/aggregation facts and the
    server version. Extra keys are merged in; warnings are concatenated."""
    meta: Dict[str, Any] = {}
    if w is not None:
        meta.update(_window_meta(w))
    warnings = list(w.get("warnings", [])) if w else []
    warnings.extend(extra.pop("warnings", None) or [])
    meta.update(extra)
    meta["warnings"] = warnings
    meta["server_version"] = _SERVER_VERSION
    return meta


def _sa_shape_rows(raw_rows: List[Dict[str, Any]], dims: List[str]) -> List[Dict[str, Any]]:
    rows = []
    for r in raw_rows:
        keys = r.get("keys") or []
        row: Dict[str, Any] = {dim: (keys[i] if i < len(keys) else "") for i, dim in enumerate(dims)}
        row["clicks"] = r.get("clicks", 0)
        row["impressions"] = r.get("impressions", 0)
        row["ctr"] = r.get("ctr", 0)
        row["position"] = r.get("position", 0)
        rows.append(row)
    return rows


def _sa_sort(rows: List[Dict[str, Any]], dims: List[str], sort_by: str, direction: str) -> List[Dict[str, Any]]:
    """Sort on a metric with a deterministic tie-break on the dimension keys.
    Missing values go last whichever the direction."""
    desc = _norm_key(direction) in ("desc", "descending")
    real = [r for r in rows if r.get(sort_by) is not None]
    none = [r for r in rows if r.get(sort_by) is None]
    real.sort(key=lambda r: tuple(str(r.get(d, "")) for d in dims))
    real.sort(key=lambda r: float(r[sort_by]), reverse=desc)
    return real + none


def _mark_preliminary(rows: List[Dict[str, Any]], dims: List[str], fid: Optional[str], fih: Optional[str]) -> None:
    if fid and "date" in dims:
        for r in rows:
            r["preliminary"] = str(r.get("date", "")) >= fid
    if fih and "hour" in dims:
        try:
            cutoff = datetime.fromisoformat(fih)
        except ValueError:
            cutoff = None
        for r in rows:
            try:
                r["preliminary"] = datetime.fromisoformat(str(r.get("hour"))) >= cutoff if cutoff else str(r.get("hour")) >= fih
            except (TypeError, ValueError):
                r["preliminary"] = str(r.get("hour")) >= fih


async def _sa_run(
    ctx: _SaContext,
    *,
    dimensions: Any,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    days: Optional[int] = None,
    window: Optional[Dict[str, Any]] = None,
    search_type: Optional[str] = None,
    filter_groups: Any = None,
    aggregation_type: Optional[str] = None,
    data_state: Optional[str] = None,
    row_limit: int = 1000,
    start_row: int = 0,
    fetch_all: bool = False,
    max_rows: int = _SA_DEFAULT_MAX_ROWS,
    sort_by: Optional[str] = None,
    sort_direction: str = "descending",
    include_totals: bool = False,
    default_days: int = 28,
    step: str = "searchanalytics.query",
) -> Dict[str, Any]:
    """Run one validated Search Analytics query end to end.

    Returns ``rows`` (shaped dicts, sorted and sliced as requested) plus the
    facts every caller reports: ``window``, ``truncated``,
    ``next_start_row``, ``aggregation_type`` (as returned), freshness
    markers, ``sort_applied`` and, with ``include_totals`` on a
    query-grouped request, ``totals`` (page_total / query_rows_sum /
    unattributed_share). Raises _SaValidationError, HttpError,
    AccountResolverError.
    """
    dims = _parse_dimensions(dimensions, allow_empty=True)
    stype = _norm_enum(search_type, _SA_TYPES, field="type") if search_type else None
    dstate = _norm_enum(data_state, _SA_DATA_STATES, field="data_state") if data_state else "final"
    agg = _norm_enum(aggregation_type, _SA_AGGREGATIONS, field="aggregation_type") if aggregation_type else None
    groups = _normalize_filter_groups(filter_groups)
    _check_sa_compat(
        dimensions=dims, filter_groups=groups, search_type=stype or "web",
        aggregation_type=agg, data_state=dstate,
    )
    if sort_by is not None and sort_by not in _SA_SORT_METRICS:
        raise _SaValidationError(f"Unknown sort_by {sort_by!r}.", f"Valid: {list(_SA_SORT_METRICS)}.")
    if _norm_key(sort_direction) not in ("asc", "ascending", "desc", "descending"):
        raise _SaValidationError(f"Unknown sort_direction {sort_direction!r}.", "Use ascending or descending.")
    row_limit = max(1, min(int(row_limit), _SA_PAGE_SIZE))
    start_row = int(start_row or 0)
    if start_row < 0:
        raise _SaValidationError(f"start_row must be >= 0, got {start_row}.", "")
    max_rows = max(1, int(max_rows))

    w = window or await _resolve_window(
        ctx, start_date=start_date, end_date=end_date, days=days,
        data_state=dstate, search_type=stype, default_days=default_days,
    )
    base = _build_sa_body(
        start_date=w["start_date"], end_date=w["end_date"], dimensions=dims,
        search_type=stype, filter_groups=groups, aggregation_type=agg,
        data_state=dstate, row_limit=row_limit,
    )

    # The API orders by clicks desc (by date asc when grouped by date). Any
    # other requested order is only correct over the full result set.
    desc = _norm_key(sort_direction) in ("desc", "descending")
    api_order_matches = sort_by is None or (sort_by == "clicks" and desc and "date" not in dims)
    client_sort = sort_by is not None
    effective_fetch_all = fetch_all or not api_order_matches

    raw: List[Dict[str, Any]] = []
    responses: List[Dict[str, Any]] = []
    complete = False
    full_sort = client_sort and not api_order_matches
    fetch_origin = 0 if full_sort else start_row  # first row the sums cover
    if effective_fetch_all:
        cursor = 0 if full_sort else start_row
        while True:
            page_size = min(_SA_PAGE_SIZE, max_rows - len(raw))
            if page_size <= 0:
                break
            body = dict(base, rowLimit=page_size)
            if cursor:
                body["startRow"] = cursor
            resp = await _sa_exec(ctx, body, step=step)
            responses.append(resp)
            batch = resp.get("rows") or []
            raw.extend(batch)
            cursor += len(batch)
            if len(batch) < page_size:
                complete = True
                break
    else:
        body = dict(base)
        if start_row:
            body["startRow"] = start_row
        resp = await _sa_exec(ctx, body, step=step)
        responses.append(resp)
        raw = list(resp.get("rows") or [])
        complete = len(raw) < row_limit

    first = responses[0] if responses else {}
    fresh = {"first_incomplete_date": None, "first_incomplete_hour": None}
    for resp in responses:
        for k, v in _response_metadata(resp).items():
            fresh[k] = fresh[k] or v
    all_rows = _sa_shape_rows(raw, dims)
    _mark_preliminary(all_rows, dims, fresh["first_incomplete_date"], fresh["first_incomplete_hour"])

    warnings: List[str] = []
    if client_sort:
        all_rows = _sa_sort(all_rows, dims, sort_by, sort_direction)
    if full_sort:
        # Fetched from row 0 so the order is global; now apply the page.
        window_rows = all_rows[start_row:]
        rows = window_rows if fetch_all else window_rows[:row_limit]
        more_in_set = (not fetch_all) and len(window_rows) > row_limit
        if not complete:
            warnings.append(
                f"Sorted by {sort_by} over the first {len(all_rows)} rows only (max_rows cap); "
                f"raise max_rows for an exact order."
            )
        truncated = (not complete) or more_in_set
        next_start_row = (start_row + row_limit) if more_in_set else None
    elif effective_fetch_all:
        rows = all_rows
        truncated = not complete
        next_start_row = (start_row + len(all_rows)) if not complete else None
    else:
        rows = all_rows
        truncated = not complete
        next_start_row = (start_row + row_limit) if not complete else None

    totals = None
    if include_totals and "query" in dims:
        totals_body = dict(base, dimensions=[], rowLimit=1)
        resp_agg = first.get("responseAggregationType")
        if resp_agg and resp_agg != "auto":
            totals_body["aggregationType"] = resp_agg
        t_resp = await _sa_exec(ctx, totals_body, step=f"{step}.totals")
        t_rows = t_resp.get("rows") or []
        page_total = {
            "clicks": (t_rows[0].get("clicks", 0) if t_rows else 0),
            "impressions": (t_rows[0].get("impressions", 0) if t_rows else 0),
        }
        rows_sum = {
            "clicks": sum(r.get("clicks", 0) or 0 for r in all_rows),
            "impressions": sum(r.get("impressions", 0) or 0 for r in all_rows),
        }
        share = {
            m: (1 - rows_sum[m] / page_total[m]) if page_total[m] else None
            for m in ("clicks", "impressions")
        }
        for m, v in share.items():
            if v is not None and not (0.0 <= v <= 1.0):
                warnings.append(
                    f"unattributed_share.{m}={v:.4f} is outside [0, 1]; query rows and the "
                    f"query-less total disagree (aggregation {resp_agg or 'auto'})."
                )
        totals = {
            "page_total": page_total,
            "query_rows_sum": rows_sum,
            "query_rows_sum_scope": (
                "all_rows" if complete and fetch_origin == 0
                else f"rows {fetch_origin}–{fetch_origin + len(all_rows) - 1} only"
                     + ("" if complete else "; more exist")
            ),
            "unattributed_share": share,
            "aggregation_type": resp_agg,
        }

    return {
        "rows": rows,
        "all_rows_count": len(all_rows),
        "complete": complete,
        "truncated": truncated,
        "next_start_row": next_start_row,
        "window": w,
        "dimensions": dims,
        "type": stype or "web",
        "data_state": dstate,
        "aggregation_type": first.get("responseAggregationType"),
        "first_incomplete_date": fresh["first_incomplete_date"],
        "first_incomplete_hour": fresh["first_incomplete_hour"],
        "sort_applied": "client" if client_sort else "api_order",
        # Rows returned of rows that exist: when the fetch ran to the end the
        # total is known (rows before a start_row offset included).
        "coverage": _coverage(
            len(rows), (fetch_origin + len(all_rows)) if complete else None, "rows",
            None if complete else "the API has more rows than were fetched (row_limit / max_rows)",
        ),
        "totals": totals,
        "warnings": warnings,
        "filter_groups": groups,
    }


def _sa_meta(res: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    """Standard meta for a `_sa_run` result."""
    fields: Dict[str, Any] = {
        "type": res["type"],
        "aggregation_type": res["aggregation_type"],
        "first_incomplete_date": res["first_incomplete_date"],
        "first_incomplete_hour": res["first_incomplete_hour"],
        "row_count": len(res["rows"]),
        "truncated": res["truncated"],
        "sort_applied": res["sort_applied"],
        "coverage": res["coverage"],
    }
    if res["totals"] is not None:
        fields["page_total"] = res["totals"]["page_total"]
        fields["query_rows_sum"] = res["totals"]["query_rows_sum"]
        fields["query_rows_sum_scope"] = res["totals"]["query_rows_sum_scope"]
        fields["unattributed_share"] = res["totals"]["unattributed_share"]
    fields.update(extra)
    fields["warnings"] = list(res["warnings"]) + list(extra.get("warnings") or [])
    return _standard_meta(res["window"], **fields)


def _sa_bad_request(e: "_SaValidationError", *, tool: str, response_format: str = "json") -> Any:
    return _format_error(
        _make_error_envelope(error=str(e), hint=e.hint, error_code=ErrorCode.BAD_REQUEST, tool=tool),
        response_format=response_format,
    )


# --- save_to_file (v1.4.0) ---
# Ported from hubspot-cms-mcp's _safe_write: absolute path only, no symlink
# target or symlinked parent, atomic temp file + replace. Saved files hold
# raw values (no spreadsheet-formula guard) because they are data for
# programs, and operator strings like "+site." must survive intact.
_SAVE_PREVIEW_ROWS = 20


def _validate_save_path(path: str) -> Optional[str]:
    p = Path(path)
    if not p.is_absolute():
        return f"save_to_file must be an absolute path, got: {path!r}"
    if p.suffix.lower() not in (".csv", ".json"):
        return f"save_to_file must end in .csv or .json, got: {path!r}"
    if p.is_symlink():
        return "save_to_file cannot be a symlink"
    if p.parent.exists() and p.parent.is_symlink():
        return f"save_to_file parent directory is a symlink, which is not allowed: {p.parent}"
    return None


def _save_rows(path: str, rows: List[Dict[str, Any]], columns: List[str], meta: Dict[str, Any]) -> Optional[str]:
    """Write rows (+ meta) to ``path``; returns an error string or None."""
    err = _validate_save_path(path)
    if err:
        return err
    p = Path(path)
    tmp: Optional[Path] = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f".{p.name}.{os.getpid()}.{uuid4().hex}.tmp"
        if p.suffix.lower() == ".json":
            text = json.dumps({"meta": meta, "columns": columns, "rows": rows}, default=str, indent=2)
            tmp.write_text(text, encoding="utf-8")
        else:
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(columns)
                for r in rows:
                    writer.writerow(["" if r.get(c) is None else r.get(c) for c in columns])
        tmp.replace(p)
        return None
    except Exception as e:  # noqa: BLE001 — surface any write failure as a string
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        return f"{type(e).__name__}: {e}"


async def _saved_summary(
    *,
    tool: str,
    path: str,
    rows: List[Dict[str, Any]],
    columns: List[Dict[str, str]],
    meta: Dict[str, Any],
    response_format: str,
    header_lines: Optional[List[str]] = None,
    truncated: bool = False,
) -> Any:
    """Write every row to ``path`` (in a worker thread) and return only a
    summary: row count, metric totals and the first 20 rows. Callers fetch
    the full result first (fetch_all); ``truncated`` means even that hit
    max_rows."""
    keys = [c["key"] for c in columns]
    err = await asyncio.to_thread(_save_rows, path, rows, keys, meta)
    if err:
        return _format_error(
            _make_error_envelope(error=err, hint="Pass an absolute .csv or .json path.",
                                 error_code=ErrorCode.BAD_REQUEST, tool=tool),
            response_format=response_format,
        )
    totals = {
        m: sum(r.get(m) or 0 for r in rows)
        for m in ("clicks", "impressions") if m in keys
    }
    summary_meta = dict(meta, saved_to=path, saved_row_count=len(rows), saved_totals=totals,
                        saved_truncated=truncated)
    lines = [f"Saved {len(rows)} rows to {path} (showing the first {min(len(rows), _SAVE_PREVIEW_ROWS)})."]
    if truncated:
        lines.insert(0, "WARNING: the saved file hit max_rows; raise max_rows for every row.")
    lines.extend(header_lines or [])
    return _format_table(
        rows[:_SAVE_PREVIEW_ROWS], columns, response_format=response_format,
        header_lines=lines, meta=summary_meta, text_meta=True,
    )


async def _save_and_trim(result: Dict[str, Any], path: str, list_keys: Tuple[str, ...]) -> Dict[str, Any]:
    """save_to_file for dict-shaped results: write the whole result as JSON
    (in a worker thread) and return it with each list in ``list_keys``
    trimmed to the first 20 items."""
    err = _validate_save_path(path)
    if err is None and not path.lower().endswith(".json"):
        err = f"save_to_file for this tool must be a .json path, got: {path!r}"
    if err:
        raise _SaValidationError(err, "Pass an absolute .json path.")

    def _write() -> Optional[str]:
        p = Path(path)
        tmp = p.parent / f".{p.name}.{os.getpid()}.{uuid4().hex}.tmp"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(result, default=str, indent=2), encoding="utf-8")
            tmp.replace(p)
            return None
        except Exception as e:  # noqa: BLE001
            try:
                tmp.unlink()
            except OSError:
                pass
            return f"{type(e).__name__}: {e}"
    werr = await asyncio.to_thread(_write)
    if werr:
        raise _SaValidationError(werr, "Check the directory exists and is writable.")
    trimmed = dict(result)
    counts = {}
    for k in list_keys:
        if isinstance(result.get(k), list):
            counts[k] = len(result[k])
            trimmed[k] = result[k][:_SAVE_PREVIEW_ROWS]
    trimmed["meta"] = dict(result.get("meta") or {}, saved_to=path, saved_counts=counts)
    return trimmed


@mcp.tool()
async def gsc_list_properties(
    name_contains: Optional[str] = None,
    limit: int = 50,
    response_format: str = "markdown",
    *,
    account_alias: Optional[str] = None,
) -> Any:
    """List GSC properties visible to one or all configured accounts.

    v1.2.0: when ``account_alias`` is omitted this lists properties
    across every configured account, tagging each row with its source
    account so agents can see the full coverage in one call.

    v1.2.1: ``response_format='json'`` emits the standard tabular
    envelope (``{ok, columns, rows, row_count, truncated, meta}``) so
    orchestrating agents don't have to parse markdown.

    Args:
        name_contains: Optional case-insensitive substring filter on the
            site URL (e.g. ``name_contains='whitehat'``). Use this first
            on agency accounts with many properties.
        limit: Max properties to return (default 50; clamped to [1, 1000]).
        response_format: ``markdown`` (default) | ``json``. JSON mode
            always tags rows with ``account`` for machine-readability
            even when only one account is queried; markdown mode drops
            the tag in the single-account case for human compactness.
        account_alias: Restrict to a single account. Omit for cross-
            account listing.
    """
    fmt = str(response_format or "").strip().lower()
    if fmt not in ("markdown", "json"):
        return (
            "Error listing properties: "
            f"response_format must be 'markdown' or 'json', got {response_format!r}"
        )

    try:
        async with _instrument(
            "gsc_list_properties",
            name_contains=name_contains,
            limit=limit,
            account_alias=account_alias,
            response_format=fmt,
        ):
            limit = max(1, min(int(limit), 1000))

            aliases = _list_configured_aliases()
            if not aliases:
                if fmt == "json":
                    return _make_error_envelope(
                        error="No accounts configured.",
                        error_code=ErrorCode.NO_ACCOUNTS_CONFIGURED,
                        hint="Use `gsc_add_account` to authorise a Google account.",
                        tool="gsc_list_properties",
                    )
                return (
                    "No accounts configured.\n\n"
                    "Use `gsc_add_account` to authorise a Google account."
                )

            if account_alias is not None:
                try:
                    chosen = _validate_alias(account_alias)
                except ValueError as e:
                    return _format_error(
                        _make_error_envelope(
                            error=f"Invalid account_alias: {e}",
                            error_code=ErrorCode.BAD_REQUEST,
                            tool="gsc_list_properties",
                        ),
                        response_format=fmt,
                    )
                if chosen not in aliases:
                    return _format_error(
                        _make_error_envelope(
                            error=f"Account alias {chosen!r} is not configured.",
                            error_code=ErrorCode.ACCOUNT_SITE_MISMATCH,
                            hint=f"Configured: {aliases}.",
                            tool="gsc_list_properties",
                            alternatives=aliases,
                        ),
                        response_format=fmt,
                    )
                targets = [chosen]
            else:
                targets = aliases

            # Discover per-account sites (non-interactive). Failures are
            # tolerated per-account so one bad token doesn't break the
            # whole listing — the failed alias just gets an annotation.
            rows: List[Dict[str, Any]] = []
            partial_failures: List[Dict[str, str]] = []
            for alias in targets:
                service, err_code = await asyncio.to_thread(
                    _build_service_noninteractive, alias,
                )
                if service is None:
                    partial_failures.append({
                        "account": alias,
                        "error_code": err_code or ErrorCode.INTERNAL_ERROR,
                    })
                    continue
                try:
                    site_list = await _gsc_execute(
                        lambda s=service: s.sites().list().execute(),
                        step="sites.list",
                    )
                except HttpError as e:
                    code = _HTTP_STATUS_TO_CODE.get(
                        getattr(e.resp, "status", None) or 0,
                        ErrorCode.INTERNAL_ERROR,
                    )
                    partial_failures.append({"account": alias, "error_code": code})
                    continue
                for site in site_list.get("siteEntry", []) or []:
                    site_url = site.get("siteUrl", "")
                    if name_contains and name_contains.lower() not in site_url.lower():
                        continue
                    rows.append({
                        "account": alias,
                        "site_url": site_url,
                        "permission": site.get("permissionLevel", "Unknown permission"),
                    })

            if not rows and not partial_failures:
                if fmt == "json":
                    return _format_table(
                        [],
                        [
                            {"key": "account", "display": "Account", "type": "str"},
                            {"key": "site_url", "display": "Site URL", "type": "str"},
                            {"key": "permission", "display": "Permission", "type": "str"},
                        ],
                        response_format="json",
                        meta={
                            "total_available": 0,
                            "limit": limit,
                            "name_contains": name_contains,
                            "accounts_queried": targets,
                            "partial_failures": [],
                        },
                    )
                if name_contains:
                    return f"No Search Console properties matching {name_contains!r}."
                return "No Search Console properties found."

            total_available = len(rows)
            truncated = total_available > limit
            shown = rows[:limit]

            if fmt == "json":
                return _format_table(
                    shown,
                    [
                        {"key": "account", "display": "Account", "type": "str"},
                        {"key": "site_url", "display": "Site URL", "type": "str"},
                        {"key": "permission", "display": "Permission", "type": "str"},
                    ],
                    response_format="json",
                    truncated=truncated,
                    truncation_hint=(
                        f"Showing first {limit} of {total_available}. "
                        f"Pass name_contains='…' to filter or raise limit "
                        f"(max 1000)."
                    ) if truncated else "",
                    meta={
                        "total_available": total_available,
                        "limit": limit,
                        "name_contains": name_contains,
                        "accounts_queried": targets,
                        "partial_failures": partial_failures,
                    },
                )

            # Markdown path (unchanged semantics).
            lines: List[str] = []
            # Only tag rows with the account when listing across
            # multiple accounts, to keep single-account output compact.
            tag_account = len(targets) > 1
            for row in shown:
                prefix = f"[{row['account']}] " if tag_account else ""
                lines.append(f"- {prefix}{row['site_url']} ({row['permission']})")

            if truncated:
                lines.append("")
                lines.append(
                    f"⚠ Showing first {limit} of {total_available} properties. "
                    f"Pass `name_contains='…'` to filter or raise `limit` "
                    f"(max 1000) to see more."
                )

            if partial_failures:
                lines.append("")
                lines.append("Partial failures (not queried):")
                for pf in partial_failures:
                    lines.append(f"- {pf['account']}: {pf['error_code']}")

            return "\n".join(lines)
    except Exception as e:
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint="Check that at least one account has a valid token; "
                     "see gsc_list_accounts(include_properties=True).",
                tool="gsc_list_properties",
            ),
            response_format=fmt,
        )

@mcp.tool()
async def gsc_add_site(
    site_url: str,
    *,
    account_alias: Optional[str] = None,
) -> str:
    """
    Add a site to your Search Console properties.

    The resolver's normal "property must already be reachable"
    verification doesn't apply here — by definition, the property is
    not yet in GSC. Routing rules for this tool alone:

    - Explicit ``account_alias``: use it directly. No pre-verification.
    - No alias + exactly one configured account: use it.
    - No alias + multiple configured accounts: return
      ``AMBIGUOUS_ACCOUNT`` — the caller must choose which account
      owns the new property.

    Args:
        site_url: The URL of the site to add (must be exact match e.g.
            https://example.com, or https://www.example.com, or
            https://subdomain.example.com/path/, for domain properties
            use format: sc-domain:example.com).
        account_alias: Optional explicit account. Required when more
            than one account is configured (cannot auto-resolve since
            the property isn't yet in GSC).
    """
    try:
        aliases = _list_configured_aliases()
        if not aliases:
            return _format_error(
                _make_error_envelope(
                    error="No GSC accounts configured.",
                    error_code=ErrorCode.NO_ACCOUNTS_CONFIGURED,
                    hint="Run `gsc_add_account` with an alias to authorise an account.",
                    tool="gsc_add_site",
                    site_url=site_url,
                ),
                response_format="markdown",
            )

        if account_alias is not None:
            try:
                chosen = _validate_alias(account_alias)
            except ValueError as e:
                return _format_error(
                    _make_error_envelope(
                        error=f"Invalid account_alias: {e}",
                        error_code=ErrorCode.BAD_REQUEST,
                        tool="gsc_add_site",
                    ),
                    response_format="markdown",
                )
            if chosen not in aliases:
                return _format_error(
                    _make_error_envelope(
                        error=f"Account alias {chosen!r} is not configured.",
                        error_code=ErrorCode.ACCOUNT_SITE_MISMATCH,
                        hint=f"Configured: {aliases}. Use gsc_list_accounts.",
                        tool="gsc_add_site",
                        alternatives=aliases,
                    ),
                    response_format="markdown",
                )
        elif len(aliases) == 1:
            chosen = aliases[0]
        else:
            return _format_error(
                _make_error_envelope(
                    error=(
                        f"Multiple accounts configured ({aliases}); "
                        f"cannot auto-resolve for gsc_add_site because "
                        f"the property isn't in GSC yet."
                    ),
                    error_code=ErrorCode.AMBIGUOUS_ACCOUNT,
                    hint="Pass account_alias to choose which account owns the new property.",
                    tool="gsc_add_site",
                    alternatives=aliases,
                    site_url=site_url,
                ),
                response_format="markdown",
            )

        service, err_code = await asyncio.to_thread(
            _build_service_noninteractive, chosen,
        )
        if service is None:
            return _format_error(
                _make_error_envelope(
                    error=f"Could not authenticate account {chosen!r}.",
                    error_code=err_code or ErrorCode.INTERNAL_ERROR,
                    hint="Retry, or re-auth via gsc_add_account.",
                    tool="gsc_add_site",
                ),
                response_format="markdown",
            )

        response = await _gsc_execute(
            lambda: service.sites().add(siteUrl=site_url).execute(), step="sites.add",
        )

        # On success, invalidate the chosen alias's property cache so
        # the next read-tool call picks up the newly-added property.
        _invalidate_property_cache(chosen)

        result_lines = [f"Site {site_url} has been added to Search Console."]
        if "permissionLevel" in response:
            result_lines.append(f"Permission level: {response['permissionLevel']}")
        return "\n".join(result_lines)
    except HttpError as e:
        # 409 is idempotency — the site already exists. Treat as success.
        if getattr(e.resp, "status", None) == 409:
            return f"Site {site_url} is already added to Search Console."
        return _format_error(
            _http_error_envelope(e, tool="gsc_add_site", site_url=site_url),
            response_format="markdown",
        )
    except Exception as e:
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint="Set GSC_MCP_TELEMETRY=1 for structured logs and retry.",
                tool="gsc_add_site",
            ),
            response_format="markdown",
        )

@mcp.tool()
async def gsc_delete_site(
    site_url: str,
    *,
    account_alias: Optional[str] = None,
) -> str:
    """
    Remove a site from your Search Console properties.

    Args:
        site_url: The URL of the site to remove (must be exact match e.g. https://example.com, or https://www.example.com, or https://subdomain.example.com/path/, for domain properties use format: sc-domain:example.com)
        account_alias: Optional explicit account to route this call to.
            When omitted, auto-resolves from site_url. Pass this when
            multiple configured accounts have access to the same
            property (AMBIGUOUS_ACCOUNT otherwise).
    """
    try:
        try:
            def _do(svc):
                return svc.sites().delete(siteUrl=site_url).execute()
            await _call_with_stale_retry(
                site_url=site_url,
                account_alias=account_alias,
                api_call=_do, step="sites.delete",
            )
        except AccountResolverError as e:
            return _format_error(
                e.to_envelope(tool="gsc_delete_site"),
                response_format="markdown",
            )
        return f"Site {site_url} has been removed from Search Console."
    except HttpError as e:
        # 404 is semantically "nothing to delete" — treat as a
        # clean idempotent no-op rather than a bare error envelope.
        if getattr(e.resp, "status", None) == 404:
            return f"Site {site_url} was not found in Search Console."
        return _format_error(
            _http_error_envelope(e, tool="gsc_delete_site", site_url=site_url),
            response_format="markdown",
        )
    except Exception as e:
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint="Set GSC_MCP_TELEMETRY=1 for structured logs and retry.",
                tool="gsc_delete_site",
            ),
            response_format="markdown",
        )

def _tool_error(e: BaseException, *, tool: str, site_url: Optional[str] = None,
                response_format: str = "json",
                generic_hint: str = "Set GSC_MCP_TELEMETRY=1 for structured logs and retry.") -> Any:
    """Map any exception from a v1.4.0 tool body to a formatted envelope."""
    if isinstance(e, AccountResolverError):
        env = e.to_envelope(tool=tool)
    elif isinstance(e, _SaValidationError):
        env = _make_error_envelope(error=str(e), hint=e.hint, error_code=ErrorCode.BAD_REQUEST, tool=tool)
    elif isinstance(e, HttpError):
        env = _http_error_envelope(e, tool=tool, site_url=site_url)
    elif isinstance(e, ValueError):
        env = _make_error_envelope(error=str(e), hint="Check the arguments (dates are YYYY-MM-DD).",
                                   error_code=ErrorCode.BAD_REQUEST, tool=tool)
    else:
        env = _make_error_envelope(
            error=f"{type(e).__name__}: {e}",
            hint=generic_hint,
            tool=tool,
        )
    return _format_error(env, response_format=response_format)


_METRIC_COLUMNS = [
    {"key": "clicks", "display": "Clicks", "type": "int"},
    {"key": "impressions", "display": "Impressions", "type": "int"},
    {"key": "ctr", "display": "CTR", "type": "pct"},
    {"key": "position", "display": "Position", "type": "float"},
]


def _sa_columns(dims: List[str], *, preliminary: bool = False) -> List[Dict[str, str]]:
    cols = [{"key": d, "display": d[0].upper() + d[1:], "type": "str"} for d in dims]
    cols.extend(_METRIC_COLUMNS)
    if preliminary:
        cols.append({"key": "preliminary", "display": "Preliminary", "type": "str"})
    return cols


@mcp.tool()
async def gsc_query(
    site_url: str,
    start_date: str,
    end_date: str,
    dimensions: Union[List[str], str] = "query",
    type: str = "web",
    filter_groups: Optional[List[Dict[str, Any]]] = None,
    aggregation_type: Optional[str] = None,
    data_state: str = "final",
    row_limit: int = 1000,
    start_row: int = 0,
    fetch_all: bool = False,
    max_rows: int = _SA_DEFAULT_MAX_ROWS,
    sort_by: Optional[str] = None,
    sort_direction: str = "descending",
    save_to_file: Optional[str] = None,
    response_format: str = "json",
    *,
    include_totals: bool = True,
    account_alias: Optional[str] = None,
) -> Any:
    """Raw Search Analytics passthrough: every searchanalytics.query
    parameter, validated, with explicit dates. Pick me when a convenience
    tool can't express the query (regex/multi filters, searchAppearance,
    fresh or hourly data, News/Discover).

    API limits, stated plainly: no Generative AI features (AI Overview /
    AI Mode) data — import a UI export instead; anonymised queries are
    never returned, so query rows undercount (see meta.unattributed_share);
    there is no brand dimension — filter with includingRegex/excludingRegex.

    Args:
        site_url: GSC property (`sc-domain:example.com` for domain properties).
        start_date / end_date: YYYY-MM-DD (or 'today', 'yesterday',
            'Ndaysago'), Pacific Time. Required.
        dimensions: list or comma string of date, hour, query, page,
            country, device, searchAppearance.
        type: web | image | video | news | googleNews | discover.
        filter_groups: [{group_type: 'and', filters: [{dimension, operator,
            expression}]}] (a flat list of filters is one AND group).
            Operators: equals, notEquals, contains, notContains,
            includingRegex, excludingRegex (RE2; e.g. "\\bsage\\b" matches
            the word, not "message"). Filter dimensions: query, page,
            country, device, searchAppearance.
        aggregation_type: auto | byPage | byProperty | byNewsShowcasePanel.
        data_state: final (default) | all (fresh, preliminary data) |
            hourly_all (needed for the hour dimension). Rows on or after
            meta.first_incomplete_date carry preliminary=true.
        row_limit: rows per page, 1–25000. start_row: offset.
        fetch_all: page through every row (25k per call) up to max_rows.
        sort_by: clicks | impressions | ctr | position, applied client-side
            over the full result (fetches all rows when the API's own order
            differs). sort_direction: ascending | descending.
        save_to_file: absolute .csv/.json path; writes every row and returns
            a summary (row count, totals, first 20 rows).
        response_format: json (default) | csv | markdown.
        include_totals: when grouped by query, one extra query-less call gives
            meta.page_total, query_rows_sum and unattributed_share.
        account_alias: explicit account; omit to auto-resolve.
    """
    tool = "gsc_query"
    try:
        async with _instrument(tool, site_url=site_url, account_alias=account_alias):
            ctx = await _sa_context(site_url, account_alias)
            res = await _sa_run(
                ctx, dimensions=dimensions, start_date=start_date, end_date=end_date,
                search_type=type, filter_groups=filter_groups,
                aggregation_type=aggregation_type, data_state=data_state,
                row_limit=row_limit, start_row=start_row, fetch_all=fetch_all or bool(save_to_file),
                max_rows=max_rows, sort_by=sort_by, sort_direction=sort_direction,
                include_totals=include_totals, step="gsc_query",
            )
            dims = res["dimensions"]
            columns = _sa_columns(
                dims, preliminary=bool(res["first_incomplete_date"] or res["first_incomplete_hour"]),
            )
            meta = _sa_meta(
                res,
                site_url=site_url,
                dimensions=dims,
                filter_groups=res["filter_groups"],
                sort_by=sort_by,
                sort_direction=sort_direction if sort_by else None,
                next_start_row=res["next_start_row"],
                account_alias=ctx.alias,
            )
            meta["tool"] = tool
            header = [f"gsc_query for {site_url}", _window_line(res["window"])]
            if save_to_file:
                return await _saved_summary(
                    tool=tool, path=save_to_file, rows=res["rows"], columns=columns,
                    meta=meta, response_format=response_format, header_lines=header,
                    truncated=res["truncated"],
                )
            hint = (
                f"More rows exist. Pass start_row={res['next_start_row']}, fetch_all=true, "
                f"or save_to_file." if res["next_start_row"] is not None
                else "Hit max_rows; raise max_rows or narrow the filters."
            )
            out = _format_table(
                res["rows"], columns, response_format=response_format,
                header_lines=header, truncated=res["truncated"], truncation_hint=hint,
                meta=meta, text_meta=True,
            )
            if isinstance(out, dict):
                out["tool"] = tool
            return out
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, site_url=site_url, response_format=response_format)


@mcp.tool()
async def gsc_get_search_analytics(
    site_url: str,
    days: int = 28,
    dimensions: str = "query",
    row_limit: int = 100,
    response_format: str = "markdown",
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    data_state: str = "final",
    save_to_file: Optional[str] = None,
    account_alias: Optional[str] = None,
) -> Any:
    """Overview of a GSC property's top rows. Pick me for a single-dimension
    summary; use `gsc_get_advanced_search_analytics` for sorting/filtering,
    `gsc_query` for every API parameter, or `gsc_get_search_by_page_query`
    to break queries down for one page.

    Args:
        site_url: GSC site URL (exact match; for domain properties use `sc-domain:example.com`).
        days: The last N days of final data (default 28, like the GSC UI),
            ending on meta.latest_final_date (Pacific Time). Ignored when
            start_date/end_date are given.
        dimensions: Comma-separated GSC dimensions (default `query`; options: query, page, device, country, date, searchAppearance).
        row_limit: Max rows returned (default 100; clamped to [1, 25000]).
        response_format: `markdown` (default, table) | `csv` (compact
            for downstream parsing) | `json` (dict with typed rows).
        start_date / end_date: Explicit YYYY-MM-DD window (overrides days).
        data_state: `final` (default) | `all` (includes preliminary days).
        save_to_file: Absolute .csv/.json path; writes all rows, returns a summary.
        account_alias: Optional explicit account to route this call to.
            When omitted, auto-resolves from site_url. Pass when
            multiple configured accounts share access (AMBIGUOUS_ACCOUNT).
    """
    tool = "gsc_get_search_analytics"
    try:
        async with _instrument(
            tool, site_url=site_url, days=days, row_limit=row_limit,
            account_alias=account_alias,
        ):
            days = max(int(days), 1)
            row_limit = max(1, min(int(row_limit), 25000))
            dimension_list = [d.strip() for d in dimensions.split(",")]

            ctx = await _sa_context(site_url, account_alias)
            res = await _sa_run(
                ctx, dimensions=dimension_list, days=days, start_date=start_date,
                end_date=end_date, data_state=data_state, row_limit=row_limit,
                fetch_all=bool(save_to_file), include_totals=True, step=tool,
            )
            w = res["window"]
            dims = res["dimensions"]
            span = (
                f"in the last {days} days" if w["days_requested"]
                else f"for {w['start_date']} to {w['end_date']}"
            )
            columns = _sa_columns(dims, preliminary=bool(res["first_incomplete_date"]))

            truncation_hint = (
                f"Showing {row_limit} rows of possibly-more. Pass a larger "
                f"`row_limit` (max 25000), or use `gsc_query` with `fetch_all` "
                f"or `save_to_file`."
            )
            meta = _sa_meta(
                res, site_url=site_url, days=days, dimensions=dims, row_limit=row_limit,
            )
            header = [
                f"Search analytics for {site_url} "
                + (f"(last {days} days)" if w["days_requested"] else f"({w['start_date']} to {w['end_date']})"),
                _window_line(w),
            ]
            if save_to_file:
                return await _saved_summary(
                    tool=tool, path=save_to_file, rows=res["rows"], columns=columns,
                    meta=meta, response_format=response_format, header_lines=header,
                    truncated=res["truncated"],
                )
            if not res["rows"] and str(response_format).strip().lower() != "json":
                return (
                    f"No search analytics data found for {site_url} {span} "
                    f"({w['start_date']} → {w['end_date']}, Pacific Time)."
                )

            # Truncate long dimension values for display.
            rows = []
            for r in res["rows"]:
                row_dict = dict(r)
                for dim in dims:
                    row_dict[dim] = str(r.get(dim, ""))[:100]
                rows.append(row_dict)
            return _format_table(
                rows,
                columns,
                response_format=response_format,
                header_lines=header,
                truncated=res["truncated"],
                truncation_hint=truncation_hint,
                meta=meta,
                text_meta=True,
            )
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, site_url=site_url, response_format=response_format)

@mcp.tool()
async def gsc_get_site_details(
    site_url: str,
    response_format: str = "markdown",
    *,
    account_alias: Optional[str] = None,
) -> Any:
    """Show permission level and, when available, verification and
    ownership metadata for a GSC property. Returns only fields the
    Search Console API populates — for most domain properties (sc-domain:…)
    the response is just `permission_level`.

    Args:
        site_url: GSC site URL (exact match).
        response_format: "markdown" (default) or "json".
        account_alias: Optional explicit account; omit to auto-resolve.
    """
    fmt = str(response_format or "").strip().lower()
    if fmt not in ("markdown", "json"):
        return (
            "Error retrieving site details: "
            f"response_format must be 'markdown' or 'json', got {response_format!r}"
        )

    try:
        try:
            def _do(svc):
                return svc.sites().get(siteUrl=site_url).execute()
            _resolved, _service, site_info = await _call_with_stale_retry(
                site_url=site_url, account_alias=account_alias, api_call=_do, step="sites.get",
            )
        except AccountResolverError as e:
            return _format_error(
                e.to_envelope(tool="gsc_get_site_details"), response_format=fmt,
            )

        permission_level = site_info.get("permissionLevel", "Unknown")

        verification: Optional[Dict[str, Any]] = None
        if "siteVerificationInfo" in site_info:
            v = site_info["siteVerificationInfo"]
            verification = {
                "state": v.get("verificationState"),
                "verified_user": v.get("verifiedUser"),
                "method": v.get("verificationMethod"),
            }

        ownership: Optional[Dict[str, Any]] = None
        if "ownershipInfo" in site_info:
            o = site_info["ownershipInfo"]
            ownership = {
                "owner": o.get("owner"),
                "method": o.get("verificationMethod"),
            }

        if fmt == "json":
            return {
                "ok": True,
                "tool": "gsc_get_site_details",
                "site_url": site_url,
                "permission_level": permission_level,
                "verification": verification,
                "ownership": ownership,
                "meta": {"site_url": site_url},
            }

        result_lines = [f"Site details for {site_url}:"]
        result_lines.append("-" * 50)
        result_lines.append(f"Permission level: {permission_level}")
        if verification is not None:
            result_lines.append(
                f"Verification state: {verification['state'] or 'Unknown'}"
            )
            if verification["verified_user"]:
                result_lines.append(f"Verified by: {verification['verified_user']}")
            if verification["method"]:
                result_lines.append(f"Verification method: {verification['method']}")
        if ownership is not None:
            result_lines.append("\nOwnership Information:")
            result_lines.append(f"Owner: {ownership['owner'] or 'Unknown'}")
            if ownership["method"]:
                result_lines.append(f"Ownership verification: {ownership['method']}")
        return "\n".join(result_lines)
    except HttpError as e:
        return _format_error(
            _http_error_envelope(e, tool="gsc_get_site_details", site_url=site_url),
            response_format=fmt,
        )
    except Exception as e:
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint="Set GSC_MCP_TELEMETRY=1 for structured logs and retry.",
                tool="gsc_get_site_details",
            ),
            response_format=fmt,
        )

@mcp.tool()
async def gsc_get_sitemaps(
    site_url: str,
    response_format: str = "markdown",
    *,
    account_alias: Optional[str] = None,
) -> Any:
    """List submitted sitemaps for a GSC property. Compact output with
    Valid/Has-errors status. Use `gsc_list_sitemaps_enhanced` for the richer
    table (includes submission + download timestamps + warnings).

    Args:
        site_url: GSC site URL (exact match).
        response_format: `markdown` (default) | `csv` | `json`.
        account_alias: Optional explicit account; omit to auto-resolve.
    """
    try:
        try:
            def _do(svc):
                return svc.sitemaps().list(siteUrl=site_url).execute()
            _resolved, _service, sitemaps = await _call_with_stale_retry(
                site_url=site_url, account_alias=account_alias, api_call=_do, step="sitemaps.list",
            )
        except AccountResolverError as e:
            return _format_error(
                e.to_envelope(tool="gsc_get_sitemaps"),
                response_format=response_format,
            )

        raw = sitemaps.get("sitemap") or []
        if not raw:
            return f"No sitemaps found for {site_url}."

        rows: List[Dict[str, Any]] = []
        for sitemap in raw:
            path = sitemap.get("path", "Unknown")
            last_downloaded = sitemap.get("lastDownloaded", "Never")
            if last_downloaded != "Never":
                try:
                    dt = datetime.fromisoformat(last_downloaded.replace('Z', '+00:00'))
                    last_downloaded = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass

            # GSC returns errors/warnings as strings — coerce defensively.
            try:
                errors = int(sitemap.get("errors", 0) or 0)
            except (TypeError, ValueError):
                errors = 0
            try:
                warnings = int(sitemap.get("warnings", 0) or 0)
            except (TypeError, ValueError):
                warnings = 0

            indexed_urls: Optional[int] = None
            if "contents" in sitemap:
                for content in sitemap["contents"]:
                    if content.get("type") == "web":
                        try:
                            indexed_urls = int(content.get("submitted", 0) or 0)
                        except (TypeError, ValueError):
                            indexed_urls = None
                        break

            rows.append({
                "path": path,
                "last_downloaded": last_downloaded,
                "status": "Has errors" if errors > 0 else "Valid",
                "indexed_urls": indexed_urls,
                "errors": errors,
                "warnings": warnings,
            })

        columns = [
            {"key": "path", "display": "Path", "type": "str"},
            {"key": "last_downloaded", "display": "Last Downloaded", "type": "str"},
            {"key": "status", "display": "Status", "type": "str"},
            {"key": "indexed_urls", "display": "Indexed URLs", "type": "int"},
            {"key": "errors", "display": "Errors", "type": "int"},
        ]

        return _format_table(
            rows,
            columns,
            response_format=response_format,
            header_lines=[f"Sitemaps for {site_url}"],
            meta={"site_url": site_url, "count": len(rows)},
        )
    except HttpError as e:
        return _format_error(
            _http_error_envelope(e, tool="gsc_get_sitemaps", site_url=site_url),
            response_format=response_format,
        )
    except Exception as e:
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint="Set GSC_MCP_TELEMETRY=1 for structured logs and retry.",
                tool="gsc_get_sitemaps",
            ),
            response_format=response_format,
        )

# --- URL inspection core (v1.4.0) ---
# Shared by gsc_inspect_start / gsc_inspect_status and the three older
# inspection tools:
#
# * a state store at $GSC_STATE_DIR/gsc-state.sqlite (rollback journal, not
#   WAL; mode 0666 on the shared tree) holding a 6-hour per-URL result
#   cache and a quota ledger shared by every process and login;
# * a reservation before EVERY outbound attempt, retries included, against
#   Google's published per-property limits (2,000/day, 600/minute —
#   developers.google.com/webmaster-tools/limits, checked 2026-09-28).
#   Google returns the same "quota exceeded" error for every limit, so the
#   ledger is the authority on which one was hit; it is an estimate
#   (UI use and other tools are invisible to it);
# * bounded concurrency, one service (one httplib2.Http) per worker.
_INSPECTION_DAILY_LIMIT = int(os.environ.get("GSC_INSPECTION_DAILY_LIMIT", "2000"))
_INSPECTION_MINUTE_LIMIT = int(os.environ.get("GSC_INSPECTION_MINUTE_LIMIT", "600"))
_INSPECTION_CACHE_TTL_SEC = 6 * 3600
_INSPECTION_MAX_ATTEMPTS = 4
_INSPECT_DEFAULT_CONCURRENCY = 4
_INSPECT_MAX_CONCURRENCY = 8
_INSPECT_JOB_TTL_SEC = 24 * 3600
_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS inspection_cache (
    site TEXT NOT NULL, url TEXT NOT NULL, fetched_at REAL NOT NULL,
    result_json TEXT NOT NULL, PRIMARY KEY (site, url));
CREATE TABLE IF NOT EXISTS inspection_ledger (
    site TEXT NOT NULL, pt_day TEXT NOT NULL, minute TEXT NOT NULL,
    count INTEGER NOT NULL, PRIMARY KEY (site, pt_day, minute));
"""


_async_retry_sleep = asyncio.sleep  # test seam: conftest replaces it with a no-op


def _state_db_path() -> str:
    """Test seam (conftest points it at tmp_path)."""
    return os.path.join(GSC_STATE_DIR, "gsc-state.sqlite")


def _state_db() -> sqlite3.Connection:
    path = _state_db_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    is_new = not os.path.exists(path)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    if is_new:
        # Same rule as token files: shared (world-writable) tree → 0666 so
        # every login can write; otherwise owner-only.
        try:
            dir_mode = stat.S_IMODE(os.stat(directory).st_mode)
            os.chmod(path, 0o666 if dir_mode & stat.S_IWOTH else 0o600)
        except OSError:
            pass
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_STATE_SCHEMA)
    return conn


class _LocalQuotaExhausted(HttpError):
    """The ledger refused a reservation: this property's daily inspection
    quota is spent. A synthetic 429 whose reason makes every existing
    ``except HttpError`` branch render ``QUOTA_EXHAUSTED``."""

    def __init__(self, site_url: str, used: int, limit: int) -> None:
        content = json.dumps({"error": {
            "message": (
                f"Daily URL Inspection quota for {site_url} is spent "
                f"({used}/{limit} used today, Pacific Time; local ledger estimate)."
            ),
            "errors": [{"reason": "dailyLimitExceeded"}],
        }}).encode()
        super().__init__(httplib2.Response({"status": 429}), content)
        self.gsc_step = "urlInspection.reserve"
        self.gsc_attempts = 0


def _reserve_inspection(site_url: str) -> Tuple[bool, Optional[str], Dict[str, int]]:
    """Atomically reserve one inspection for ``site_url``.

    Returns ``(ok, refused_by, counts)``; ``refused_by`` is ``"day"`` or
    ``"minute"``. ``BEGIN IMMEDIATE`` takes the write lock up front, so two
    processes racing for the last slot cannot both get it."""
    now = _now_pt()
    day = now.date().isoformat()
    minute = now.strftime("%Y-%m-%dT%H:%M")
    conn = _state_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        day_used = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM inspection_ledger WHERE site=? AND pt_day=?",
            (site_url, day),
        ).fetchone()[0]
        minute_used = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM inspection_ledger WHERE site=? AND minute=?",
            (site_url, minute),
        ).fetchone()[0]
        counts = {"day_used": day_used, "minute_used": minute_used}
        if day_used >= _INSPECTION_DAILY_LIMIT:
            conn.execute("ROLLBACK")
            return False, "day", counts
        if minute_used >= _INSPECTION_MINUTE_LIMIT:
            conn.execute("ROLLBACK")
            return False, "minute", counts
        conn.execute(
            "INSERT INTO inspection_ledger (site, pt_day, minute, count) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(site, pt_day, minute) DO UPDATE SET count = count + 1",
            (site_url, day, minute),
        )
        old = (now.date() - timedelta(days=3)).isoformat()
        conn.execute("DELETE FROM inspection_ledger WHERE pt_day < ?", (old,))
        conn.execute("COMMIT")
        counts["day_used"] += 1
        counts["minute_used"] += 1
        return True, None, counts
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _inspection_quota(site_url: str) -> Dict[str, Any]:
    now = _now_pt()
    conn = _state_db()
    try:
        day_used = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM inspection_ledger WHERE site=? AND pt_day=?",
            (site_url, now.date().isoformat()),
        ).fetchone()[0]
        minute_used = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM inspection_ledger WHERE site=? AND minute=?",
            (site_url, now.strftime("%Y-%m-%dT%H:%M")),
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "day_used": day_used,
        "day_limit": _INSPECTION_DAILY_LIMIT,
        "day_remaining": max(0, _INSPECTION_DAILY_LIMIT - day_used),
        "minute_used": minute_used,
        "minute_limit": _INSPECTION_MINUTE_LIMIT,
        "resets_in_seconds": round(_seconds_until_pt_midnight()),
        "basis": "estimate: inspections made by this server on this machine (all logins); "
                 "Search Console UI use is not visible to it",
    }


def _cache_get(site_url: str, url: str) -> Optional[Tuple[Dict[str, Any], float]]:
    conn = _state_db()
    try:
        row = conn.execute(
            "SELECT result_json, fetched_at FROM inspection_cache WHERE site=? AND url=?",
            (site_url, url),
        ).fetchone()
    finally:
        conn.close()
    if not row or time.time() - row[1] > _INSPECTION_CACHE_TTL_SEC:
        return None
    try:
        return json.loads(row[0]), row[1]
    except ValueError:
        return None


def _cache_put(site_url: str, url: str, response: Dict[str, Any]) -> float:
    fetched_at = time.time()
    conn = _state_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO inspection_cache (site, url, fetched_at, result_json) VALUES (?, ?, ?, ?)",
            (site_url, url, fetched_at, json.dumps(response)),
        )
        conn.execute(
            "DELETE FROM inspection_cache WHERE fetched_at < ?",
            (fetched_at - 2 * _INSPECTION_CACHE_TTL_SEC,),
        )
    finally:
        conn.close()
    return fetched_at


def _clone_service(service: Any) -> Any:
    """A second service over the same (already refreshed) credentials, so a
    worker thread gets its own httplib2.Http — which is not thread-safe.
    Test seam: conftest makes this the identity."""
    creds = getattr(getattr(service, "_http", None), "credentials", None)
    if creds is None:
        return service
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


async def _inspect_one(
    service: Any,
    site_url: str,
    page_url: str,
    *,
    force: bool = False,
    wait_for_minute: bool = True,
    on_throttle=None,
) -> Tuple[Dict[str, Any], Optional[float]]:
    """Inspect one URL: cache first (unless ``force``), then reserve quota
    and call Google, retrying rate limits and 5xx with backoff — each
    attempt reserves again. Returns ``(response, cached_at)``;
    ``cached_at`` is None for a live result."""
    if not force:
        hit = await asyncio.to_thread(_cache_get, site_url, page_url)
        if hit is not None:
            return hit[0], hit[1]
    body = {"inspectionUrl": page_url, "siteUrl": site_url}
    attempt = 0
    while True:
        ok, refused_by, counts = await asyncio.to_thread(_reserve_inspection, site_url)
        if not ok:
            if refused_by == "day" or not wait_for_minute:
                if refused_by == "day":
                    raise _LocalQuotaExhausted(site_url, counts["day_used"], _INSPECTION_DAILY_LIMIT)
                raise HttpError(httplib2.Response({"status": 429}), json.dumps({"error": {
                    "message": f"Per-minute URL Inspection quota for {site_url} is spent (local ledger).",
                    "errors": [{"reason": "rateLimitExceeded"}],
                }}).encode())
            if on_throttle:
                on_throttle(True)
            await _async_retry_sleep(60 - _now_pt().second + 0.5)
            if on_throttle:
                on_throttle(False)
            continue
        attempt += 1
        try:
            response = await _gsc_execute(
                lambda: service.urlInspection().index().inspect(body=body).execute(),
                step="urlInspection.inspect", max_attempts=1,
            )
        except HttpError as e:
            kind = _http_error_details(e)["kind"]
            delay = _retry_delay(e, attempt) if kind in ("rate_limit", "server", "timeout") else None
            if delay is None or attempt >= _INSPECTION_MAX_ATTEMPTS:
                e.gsc_attempts = attempt
                raise
            await _async_retry_sleep(delay)
            continue
        except ConnectionError:
            if attempt >= _INSPECTION_MAX_ATTEMPTS:
                raise
            await _async_retry_sleep(_backoff_delay(attempt))
            continue
        response = response or {}
        if "inspectionResult" in response:
            await asyncio.to_thread(_cache_put, site_url, page_url, response)
        return response, None


def _normalize_inspection(page_url: str, response: Dict[str, Any], cached_at: Optional[float] = None) -> Dict[str, Any]:
    """Every field §4.1 asks for, flat, from a raw inspect response."""
    inspection = (response or {}).get("inspectionResult")
    if not inspection:
        return {"url": page_url, "error": "No inspection data found", "cached_at": cached_at}
    idx = inspection.get("indexStatusResult", {}) or {}
    rich = inspection.get("richResultsResult")
    google_canonical = idx.get("googleCanonical")
    user_canonical = idx.get("userCanonical")
    return {
        "url": page_url,
        "verdict": idx.get("verdict"),
        "coverage_state": idx.get("coverageState"),
        "indexing_state": idx.get("indexingState"),
        "robots_txt_state": idx.get("robotsTxtState"),
        "page_fetch_state": idx.get("pageFetchState"),
        "last_crawl_time": idx.get("lastCrawlTime"),
        "crawled_as": idx.get("crawledAs"),
        "google_canonical": google_canonical,
        "user_canonical": user_canonical,
        "canonical_mismatch": bool(google_canonical and user_canonical and google_canonical != user_canonical),
        "sitemaps": list(idx.get("sitemap", []) or []),
        "referring_urls": list(idx.get("referringUrls", []) or []),
        "rich_results": None if rich is None else {
            "verdict": rich.get("verdict"),
            "types": [item.get("richResultType") for item in rich.get("detectedItems", []) or []],
            "items": rich.get("detectedItems", []) or [],
            "issues": rich.get("richResultsIssues", []) or [],
        },
        "inspection_result_link": inspection.get("inspectionResultLink"),
        "cached_at": (
            datetime.fromtimestamp(cached_at, timezone.utc).isoformat() if cached_at else None
        ),
        "error": None,
    }


async def _inspect_many(
    service: Any,
    site_url: str,
    urls: List[str],
    *,
    force: bool = False,
    concurrency: int = _INSPECT_DEFAULT_CONCURRENCY,
    wait_for_minute: bool = True,
    on_result=None,
    on_throttle=None,
    stop_on=None,
) -> Dict[str, Any]:
    """Inspect ``urls`` with at most ``concurrency`` in flight, each worker
    on its own service clone. Returns ``{url: (response, cached_at) |
    exception}``. When ``stop_on(exception)`` is true the queue stops: no
    new URL starts, in-flight ones finish, the rest are left out."""
    concurrency = max(1, min(int(concurrency), _INSPECT_MAX_CONCURRENCY, len(urls) or 1))
    queue: asyncio.Queue = asyncio.Queue()
    for u in urls:
        queue.put_nowait(u)
    results: Dict[str, Any] = {}
    stopped = {"flag": False}

    async def worker(svc: Any) -> None:
        while not stopped["flag"]:
            try:
                url = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                outcome: Any = await _inspect_one(
                    svc, site_url, url, force=force,
                    wait_for_minute=wait_for_minute, on_throttle=on_throttle,
                )
            except Exception as e:  # noqa: BLE001 — per-URL errors are data
                outcome = e
                if stop_on is not None and stop_on(e):
                    stopped["flag"] = True
            results[url] = outcome
            if on_result:
                on_result(url, outcome)

    services = [service]
    for _ in range(concurrency - 1):
        try:
            services.append(await asyncio.to_thread(_clone_service, service))
        except Exception:  # noqa: BLE001 — fall back to fewer workers
            break
    await asyncio.gather(*(worker(svc) for svc in services))
    return results


def _parse_url_list(urls: Any) -> List[str]:
    if isinstance(urls, str):
        items = urls.split("\n")
    else:
        items = list(urls or [])
    seen: Dict[str, None] = {}
    for u in items:
        u = str(u).strip()
        if u and u not in seen:
            seen[u] = None
    return list(seen)


# Async inspection jobs: in-memory, one registry per server process. A job
# outlives any single tool call, so a client's 60 s timeout never kills it;
# results also land in the on-disk cache, so after a restart re-running
# gsc_inspect_start returns cached URLs instantly.
_inspect_jobs: Dict[str, Dict[str, Any]] = {}


def _evict_inspect_jobs() -> None:
    cutoff = time.time() - _INSPECT_JOB_TTL_SEC
    for job_id in [j for j, job in _inspect_jobs.items() if job["updated_at"] < cutoff]:
        _inspect_jobs.pop(job_id, None)


def _job_error_entry(e: BaseException, site_url: str) -> Dict[str, Any]:
    if isinstance(e, HttpError):
        env = _http_error_envelope(e, tool="gsc_inspect_start", site_url=site_url)
    else:
        env = _make_error_envelope(error=f"{type(e).__name__}: {e}", tool="gsc_inspect_start")
    return {k: env.get(k) for k in ("error", "error_code", "retryable", "retry_after", "step", "http_status", "google_reason")}


def _is_daily_quota(e: BaseException) -> bool:
    """Local ledger refusal or Google's own daily-quota error."""
    return isinstance(e, HttpError) and _http_error_details(e)["kind"] == "daily_quota"


async def _run_inspect_job(job: Dict[str, Any], service: Any, concurrency: int) -> None:
    site_url = job["site_url"]
    quota_hit = {"flag": False}

    def on_result(url: str, outcome: Any) -> None:
        job["updated_at"] = time.time()
        if isinstance(outcome, BaseException):
            job["errors"][url] = _job_error_entry(outcome, site_url)
            if _is_daily_quota(outcome):
                quota_hit["flag"] = True
        else:
            response, cached_at = outcome
            job["results"][url] = _normalize_inspection(url, response, cached_at)
            job.setdefault("result_ts", {})[url] = cached_at or time.time()

    def on_throttle(active: bool) -> None:
        if job["state"] in ("running", "throttled"):
            job["state"] = "throttled" if active else "running"

    job["state"] = "running"
    pending = [u for u in job["urls"] if u not in job["results"]]
    try:
        await _inspect_many(
            service, site_url, pending, force=job["force"], concurrency=concurrency,
            on_result=on_result, on_throttle=on_throttle, stop_on=_is_daily_quota,
        )
    except Exception as e:  # noqa: BLE001 — a job never dies silently
        job["state"] = "failed"
        job["failure"] = f"{type(e).__name__}: {e}"
    else:
        # Published only after every worker has drained, so a terminal state
        # never coexists with in-flight results.
        if quota_hit["flag"]:
            job["state"] = "quota_exhausted"
        else:
            job["state"] = "failed" if job["urls"] and not job["results"] else "done"
    job["updated_at"] = time.time()
    job["finished_at"] = time.time()


def _job_rows(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for url in job["urls"]:
        if url in job["results"]:
            rows.append(dict(job["results"][url], status="done"))
        elif url in job["errors"]:
            rows.append({"url": url, "status": "error", "error": job["errors"][url]})
        else:
            status = "skipped_quota" if job["state"] == "quota_exhausted" else "pending"
            if job["state"] in ("done", "failed"):
                status = "not_run"
            rows.append({"url": url, "status": status})
    return rows


@mcp.tool()
async def gsc_inspect_start(
    site_url: str,
    urls: Union[List[str], str],
    force: bool = False,
    concurrency: int = _INSPECT_DEFAULT_CONCURRENCY,
    *,
    account_alias: Optional[str] = None,
) -> Any:
    """Start an async URL-inspection job for any number of URLs; returns a
    job_id in about a second. Poll `gsc_inspect_status(job_id)` for progress
    and partial results. Pick me over `gsc_batch_url_inspection` for more
    than a handful of URLs — no client timeout can kill the job.

    Results are cached for 6 hours per URL (cached URLs are answered at
    once and cost no quota); `force=true` re-inspects. Quota: Google allows
    2,000 inspections per property per day and 600 per minute; the job
    waits out a spent minute and stops cleanly with QUOTA_EXHAUSTED (keeping
    partial results) when the day's quota is gone.

    Args:
        site_url: GSC property (`sc-domain:example.com` for domain properties).
        urls: List of URLs (or newline-separated string). Duplicates dropped.
        force: Bypass the 6-hour cache.
        concurrency: Parallel inspections (default 4, max 8).
        account_alias: Optional explicit account; omit to auto-resolve.
    """
    tool = "gsc_inspect_start"
    try:
        url_list = _parse_url_list(urls)
        if not url_list:
            raise _SaValidationError("No URLs provided for inspection.", "Pass urls=[...].")
        concurrency = max(1, min(int(concurrency), _INSPECT_MAX_CONCURRENCY))
        resolved_alias, service = await get_gsc_service_for_site(site_url, account_alias)
        job = await _start_inspect_job(site_url, service, resolved_alias, url_list,
                                       force=force, concurrency=concurrency)
        job_id = job["job_id"]
        quota = await asyncio.to_thread(_inspection_quota, site_url)
        return {
            "ok": True,
            "tool": tool,
            "job_id": job_id,
            "state": job["state"],
            "total": len(url_list),
            "cached": job["cached"],
            "queued": len(url_list) - job["cached"],
            "poll_with": "gsc_inspect_status",
            "quota": quota,
            "meta": _standard_meta(None, site_url=site_url, account_alias=resolved_alias, concurrency=concurrency),
        }
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, site_url=site_url)


@mcp.tool()
async def gsc_inspect_status(
    job_id: str,
    offset: int = 0,
    limit: int = 50,
    response_format: str = "json",
) -> Any:
    """Progress and (partial) results of a `gsc_inspect_start` job. Safe to
    call repeatedly. `state`: queued | running | throttled (waiting out a
    spent minute) | done | quota_exhausted | failed. Each result carries
    verdict, coverage_state, indexing/robots/fetch states, last_crawl_time,
    crawled_as, google vs user canonical (+ canonical_mismatch), sitemaps,
    referring_urls and rich results.

    Args:
        job_id: From gsc_inspect_start.
        offset / limit: Page through results in the original URL order.
        response_format: json (default) | markdown.
    """
    tool = "gsc_inspect_status"
    fmt = str(response_format or "").strip().lower()
    try:
        job = _inspect_jobs.get(job_id)
        if job is None:
            return _format_error(_make_error_envelope(
                error=f"Unknown inspection job {job_id!r}.",
                hint=("Jobs live in server memory and vanish on restart (or after 24h). "
                      "Re-run gsc_inspect_start: URLs inspected in the last 6 hours come back "
                      "from the cache instantly and cost no quota."),
                error_code=ErrorCode.JOB_NOT_FOUND,
                tool=tool,
            ), response_format=fmt if fmt in _RESPONSE_FORMATS else "json")
        offset = max(0, int(offset))
        limit = max(1, int(limit))
        # Read the quota first: everything below is one synchronous snapshot
        # (no await), so state, progress and rows always agree.
        quota = await asyncio.to_thread(_inspection_quota, job["site_url"])
        state = job["state"]
        rows = _job_rows(job)
        page = rows[offset:offset + limit]
        pending = sum(1 for r in rows if r["status"] == "pending")
        progress = {
            "total": len(rows),
            "done": len(job["results"]),
            "cached": job["cached"],
            "errors": len(job["errors"]),
            "pending": pending,
            "canonical_mismatches": sum(1 for r in job["results"].values() if r.get("canonical_mismatch")),
        }
        elapsed = (job["finished_at"] or time.time()) - job["created_at"]
        next_offset = offset + limit if offset + limit < len(rows) else None
        if fmt == "markdown":
            lines = [
                f"Inspection job {job_id} for {job['site_url']}: {state}",
                f"Progress: {progress['done']}/{progress['total']} done ({progress['cached']} cached), "
                f"{progress['errors']} errors, {pending} pending; {progress['canonical_mismatches']} canonical mismatches",
                f"Quota (estimate): {quota['day_used']}/{quota['day_limit']} used today (Pacific Time)",
                "",
                "URL | Status | Verdict | Coverage | Last crawl | Canonical mismatch | Error",
                "--- | --- | --- | --- | --- | --- | ---",
            ]
            for r in page:
                err = r.get("error")
                err_text = err.get("error") if isinstance(err, dict) else (err or "")
                lines.append(
                    f"{r['url']} | {r['status']} | {r.get('verdict') or ''} | {r.get('coverage_state') or ''} | "
                    f"{r.get('last_crawl_time') or ''} | {'yes' if r.get('canonical_mismatch') else ''} | {err_text}"
                )
            if next_offset is not None:
                lines.append(f"\nMore results: offset={next_offset}")
            return "\n".join(lines)
        return {
            "ok": True,
            "tool": tool,
            "job_id": job_id,
            "site_url": job["site_url"],
            "state": state,
            "failure": job.get("failure"),
            "progress": progress,
            "results": page,
            "next_offset": next_offset,
            "elapsed_seconds": round(elapsed, 1),
            "quota": quota,
            "meta": _standard_meta(None, site_url=job["site_url"], account_alias=job["account_alias"],
                                   row_count=len(page), truncated=next_offset is not None,
                                   coverage=_coverage_rollup({
                                       "inspected": _coverage(progress["done"], progress["total"], "URLs inspected"),
                                       "shown": _coverage(len(page), len(rows), "results on this page"),
                                   })),
        }
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, response_format=fmt if fmt in _RESPONSE_FORMATS else "json")


def _raise_quota_error(outcomes: Dict[str, Any]) -> None:
    """The fixed-size batch tools surface a quota error for the whole call
    (it will hit every URL) instead of burying it in one row."""
    for outcome in outcomes.values():
        if isinstance(outcome, HttpError) and _http_error_details(outcome)["kind"] in ("daily_quota", "rate_limit"):
            raise outcome


@mcp.tool()
async def gsc_inspect_url_enhanced(
    site_url: str,
    page_url: str,
    response_format: str = "markdown",
    *,
    force: bool = False,
    account_alias: Optional[str] = None,
) -> Any:
    """Inspect a single URL's indexing status + rich results in Google.
    Pick me for one URL; use `gsc_inspect_start` for many URLs, or
    `gsc_check_indexing_issues` to bucket several URLs by problem type.

    Args:
        site_url: GSC site URL (exact match; `sc-domain:example.com`
            for domain properties).
        page_url: The URL to inspect.
        response_format: "markdown" (default) or "json".
        force: Bypass the 6-hour per-URL result cache.
        account_alias: Optional explicit account; omit to auto-resolve.
    """
    fmt = str(response_format or "").strip().lower()
    if fmt not in ("markdown", "json"):
        return (
            "Error inspecting URL: "
            f"response_format must be 'markdown' or 'json', got {response_format!r}"
        )

    try:
        async with _instrument(
            "gsc_inspect_url_enhanced",
            site_url=site_url,
            page_url=page_url,
            account_alias=account_alias,
        ):
            try:
                resolved, service = await get_gsc_service_for_site(site_url, account_alias)
                try:
                    response, cached_at = await _inspect_one(
                        service, site_url, page_url, force=force, wait_for_minute=False,
                    )
                except HttpError as e:
                    # Stale-positive 403 recovery on the auto-resolved path
                    # (same rule as _call_with_stale_retry).
                    info = _http_error_details(e)
                    if account_alias is not None or info["status"] != 403 or info["kind"] != "other":
                        raise
                    _invalidate_property_cache(resolved)
                    try:
                        new_alias, new_service = await get_gsc_service_for_site(site_url, None)
                    except AccountResolverError:
                        raise e
                    if new_alias == resolved:
                        raise
                    response, cached_at = await _inspect_one(
                        new_service, site_url, page_url, force=force, wait_for_minute=False,
                    )
            except AccountResolverError as e:
                return _format_error(
                    e.to_envelope(tool="gsc_inspect_url_enhanced"),
                    response_format=fmt,
                )

        if not response or "inspectionResult" not in response:
            if fmt == "json":
                return {
                    "ok": True,
                    "tool": "gsc_inspect_url_enhanced",
                    "site_url": site_url,
                    "page_url": page_url,
                    "index_status": None,
                    "rich_results": None,
                    "meta": {"site_url": site_url, "page_url": page_url},
                }
            return f"No inspection data found for {page_url}."

        inspection = response["inspectionResult"]
        index_status = inspection.get("indexStatusResult", {})
        rich = inspection.get("richResultsResult")

        if fmt == "json":
            rich_payload: Optional[Dict[str, Any]] = None
            if rich is not None:
                rich_payload = {
                    "verdict": rich.get("verdict"),
                    "detected_items": [
                        {
                            "rich_result_type": item.get("richResultType"),
                            "items": [
                                {k: v for k, v in sub.items()}
                                for sub in item.get("items", [])
                            ],
                        }
                        for item in rich.get("detectedItems", [])
                    ],
                    "issues": [
                        {
                            "severity": issue.get("severity"),
                            "message": issue.get("message"),
                        }
                        for issue in rich.get("richResultsIssues", [])
                    ],
                }
            return {
                "ok": True,
                "tool": "gsc_inspect_url_enhanced",
                "site_url": site_url,
                "page_url": page_url,
                "inspection_result_link": inspection.get("inspectionResultLink"),
                "index_status": {
                    "verdict": index_status.get("verdict"),
                    "coverage_state": index_status.get("coverageState"),
                    "last_crawl_time": index_status.get("lastCrawlTime"),
                    "page_fetch_state": index_status.get("pageFetchState"),
                    "robots_txt_state": index_status.get("robotsTxtState"),
                    "indexing_state": index_status.get("indexingState"),
                    "google_canonical": index_status.get("googleCanonical"),
                    "user_canonical": index_status.get("userCanonical"),
                    "crawled_as": index_status.get("crawledAs"),
                    "referring_urls": list(index_status.get("referringUrls", [])),
                    "sitemaps": list(index_status.get("sitemap", []) or []),
                    "canonical_mismatch": bool(
                        index_status.get("googleCanonical") and index_status.get("userCanonical")
                        and index_status.get("googleCanonical") != index_status.get("userCanonical")
                    ),
                },
                "rich_results": rich_payload,
                "cached_at": (
                    datetime.fromtimestamp(cached_at, timezone.utc).isoformat() if cached_at else None
                ),
                "meta": _standard_meta(None, site_url=site_url, page_url=page_url),
            }

        # --- markdown path (byte-equivalent to pre-F2) ---
        result_lines = [f"URL Inspection for {page_url}:"]
        result_lines.append("-" * 80)

        if "inspectionResultLink" in inspection:
            result_lines.append(f"Search Console Link: {inspection['inspectionResultLink']}")
            result_lines.append("-" * 80)

        verdict = index_status.get("verdict", "UNKNOWN")
        result_lines.append(f"Indexing Status: {verdict}")

        if "coverageState" in index_status:
            result_lines.append(f"Coverage: {index_status['coverageState']}")

        if "lastCrawlTime" in index_status:
            try:
                crawl_time = datetime.fromisoformat(index_status["lastCrawlTime"].replace('Z', '+00:00'))
                result_lines.append(f"Last Crawled: {crawl_time.strftime('%Y-%m-%d %H:%M')}")
            except Exception:
                result_lines.append(f"Last Crawled: {index_status['lastCrawlTime']}")

        if "pageFetchState" in index_status:
            result_lines.append(f"Page Fetch: {index_status['pageFetchState']}")

        if "robotsTxtState" in index_status:
            result_lines.append(f"Robots.txt: {index_status['robotsTxtState']}")

        if "indexingState" in index_status:
            result_lines.append(f"Indexing State: {index_status['indexingState']}")

        if "googleCanonical" in index_status:
            result_lines.append(f"Google Canonical: {index_status['googleCanonical']}")

        if "userCanonical" in index_status and index_status.get("userCanonical") != index_status.get("googleCanonical"):
            result_lines.append(f"User Canonical: {index_status['userCanonical']}")

        if "crawledAs" in index_status:
            result_lines.append(f"Crawled As: {index_status['crawledAs']}")

        if "referringUrls" in index_status and index_status["referringUrls"]:
            result_lines.append("\nReferring URLs:")
            for url in index_status["referringUrls"][:5]:
                result_lines.append(f"- {url}")
            if len(index_status["referringUrls"]) > 5:
                result_lines.append(f"... and {len(index_status['referringUrls']) - 5} more")

        if rich is not None:
            result_lines.append(f"\nRich Results: {rich.get('verdict', 'UNKNOWN')}")
            if rich.get("detectedItems"):
                result_lines.append("Detected Rich Result Types:")
                for item in rich["detectedItems"]:
                    rich_type = item.get("richResultType", "Unknown")
                    result_lines.append(f"- {rich_type}")
                    if item.get("items"):
                        for subitem in item["items"][:3]:
                            if "name" in subitem:
                                result_lines.append(f"  • {subitem['name']}")
                        if len(item["items"]) > 3:
                            result_lines.append(f"  • ... and {len(item['items']) - 3} more items")
            if rich.get("richResultsIssues"):
                result_lines.append("\nRich Results Issues:")
                for issue in rich["richResultsIssues"]:
                    severity = issue.get("severity", "Unknown")
                    message = issue.get("message", "Unknown issue")
                    result_lines.append(f"- [{severity}] {message}")

        if index_status.get("sitemap"):
            result_lines.append("\nIn sitemaps:")
            for sm in index_status["sitemap"][:5]:
                result_lines.append(f"- {sm}")
        if cached_at:
            result_lines.append(
                f"\n(Cached result from {datetime.fromtimestamp(cached_at, timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC; "
                f"pass force=true to re-inspect.)"
            )

        return "\n".join(result_lines)
    except HttpError as e:
        return _format_error(
            _http_error_envelope(e, tool="gsc_inspect_url_enhanced", site_url=site_url),
            response_format=fmt,
        )
    except Exception as e:
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint="Set GSC_MCP_TELEMETRY=1 for structured logs and retry.",
                tool="gsc_inspect_url_enhanced",
            ),
            response_format=fmt,
        )

@mcp.tool()
async def gsc_batch_url_inspection(
    site_url: str,
    urls: str = "",
    from_session: Optional[str] = None,
    dataset: str = "search_console_all",
    offset: int = 0,
    limit: int = 10,
    response_format: str = "markdown",
    *,
    force: bool = False,
    account_alias: Optional[str] = None,
) -> Any:
    """Inspect up to 10 URLs in one call (4 at a time, 6-hour cache; pass
    force=true to bypass it). For more URLs use `gsc_inspect_start`, which
    runs as a background job no client timeout can kill.
    Pick me when you have several URLs and want the same 4-field
    per-URL output; use `gsc_inspect_url_enhanced` for a single URL with
    full detail, or `gsc_check_indexing_issues` to bucket URLs by problem
    type.

    Two ways to supply URLs:
    1. Newline-separated `urls` (max 10; tool errors beyond that).
    2. `from_session` pointing at a session loaded via
       `gsc_load_from_sf_export`; the `address` column of `dataset`
       is the URL source. Use `offset`/`limit` to paginate — each
       call still processes at most 10 URLs.

    Args:
        site_url: GSC site URL (exact match; `sc-domain:example.com`
            for domain properties).
        urls: Newline-separated URLs (optional when `from_session` set).
        from_session: SF session id; URLs come from session dataset.
        dataset: Session dataset name (default 'search_console_all';
            must match ^[a-z0-9_]+$ and contain 'address' column).
        offset: Rows to skip in session dataset (>= 0). Ignored in
            direct-URL mode.
        limit: Max URLs per call. Must be 1–10; values > 10 are
            clamped to 10. Ignored in direct-URL mode.
        response_format: "markdown" (default) or "json".
    """
    fmt = str(response_format or "").strip().lower()
    if fmt not in ("markdown", "json"):
        return (
            "Error in batch URL inspection: "
            f"response_format must be 'markdown' or 'json', got {response_format!r}"
        )
    def _val_error(msg: str) -> Any:
        """Format-aware validation error. Markdown returns a string
        (byte-compatible with pre-F2); JSON returns an error envelope."""
        if fmt == "json":
            return _make_error_envelope(
                error=msg, tool="gsc_batch_url_inspection"
            )
        return msg

    try:
        # --- Phase 1: resolve URL list (no network) ---
        # Session/input validation must fail fast WITHOUT authenticating so
        # session errors don't get masked behind OAuth failures.
        clamp_note = ""
        next_offset = None
        if from_session is not None:
            if from_session not in _sf_sessions:
                return _val_error(f"Unknown SF session_id: {from_session!r}")
            session = _sf_sessions[from_session]
            if not _ALLOWED_DATASET_RE.match(dataset):
                return _val_error(f"Invalid dataset name: {dataset!r} (must match ^[a-z0-9_]+$)")
            if dataset not in session["datasets"]:
                available = sorted(session["datasets"].keys())
                return _val_error(f"Unknown dataset {dataset!r} in session {from_session!r}. Available: {available}")
            dataset_meta = session["datasets"][dataset]
            if "address" not in dataset_meta["columns"]:
                return _val_error(
                    f"Dataset {dataset!r} has no 'address' column. "
                    f"Available columns: {dataset_meta['columns']}"
                )

            # Explicit pagination validation. Reject limit<1 and negative
            # offset rather than silently clamping to 1 (the old code did
            # min(max(1, limit), 10) which hid these errors).
            if offset < 0:
                return _val_error(f"Invalid offset: {offset}. Must be >= 0.")
            if limit < 1:
                return _val_error(
                    f"Invalid limit: {limit}. Must be >= 1 for URL inspection "
                    "(each URL burns API quota)."
                )
            if limit > 10:
                clamp_note = f"Note: limit {limit} clamped to 10 for quota safety.\n"
            effective_limit = min(limit, 10)

            # Stream the dataset, pull address values, slice.
            url_list: List[str] = []
            skipped = 0
            for row in _stream_sf_csv(dataset_meta):
                addr = row.get("address", "").strip()
                if not addr:
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                url_list.append(addr)
                if len(url_list) >= effective_limit:
                    break

            next_offset = offset + len(url_list)
        else:
            # Parse URLs from the `urls` string (original behavior).
            url_list = [url.strip() for url in urls.split('\n') if url.strip()]

        if not url_list:
            return _val_error("No URLs provided for inspection.")

        if len(url_list) > 10:
            return _val_error(
                f"Too many URLs provided ({len(url_list)}). Please limit to 10 URLs per batch to avoid API quota issues."
            )

        # --- Phase 2: authenticate and inspect ---
        try:
            _resolved_alias, service = await get_gsc_service_for_site(
                site_url, account_alias,
            )
        except AccountResolverError as e:
            return _format_error(
                e.to_envelope(tool="gsc_batch_url_inspection"),
                response_format=fmt,
            )

        # Telemetry: emit tool_enter/tool_exit around the batch as a
        # whole, not per URL (per-URL would drown the batch-latency
        # signal).
        _batch_start = time.perf_counter()
        _log(
            "tool_enter",
            tool="gsc_batch_url_inspection",
            site_url=site_url,
            url_count=len(url_list),
            from_session=from_session,
        )

        structured: List[Dict[str, Any]] = []
        outcomes = await _inspect_many(
            service, site_url, url_list, force=force, wait_for_minute=False,
        )
        _raise_quota_error(outcomes)

        for page_url in url_list:
            outcome = outcomes.get(page_url)
            if isinstance(outcome, BaseException) or outcome is None:
                structured.append({
                    "url": page_url,
                    "verdict": None,
                    "coverage": None,
                    "last_crawl": None,
                    "rich_results": None,
                    "error": str(outcome) if outcome is not None else "Not inspected",
                })
                continue
            response = outcome[0]
            if not response or "inspectionResult" not in response:
                structured.append({
                    "url": page_url,
                    "verdict": None,
                    "coverage": None,
                    "last_crawl": None,
                    "rich_results": None,
                    "error": "No inspection data found",
                })
                continue

            inspection = response["inspectionResult"]
            index_status = inspection.get("indexStatusResult", {})

            verdict = index_status.get("verdict", "UNKNOWN")
            coverage = index_status.get("coverageState", "Unknown")
            last_crawl: Optional[str] = None
            if "lastCrawlTime" in index_status:
                try:
                    crawl_time = datetime.fromisoformat(index_status["lastCrawlTime"].replace('Z', '+00:00'))
                    last_crawl = crawl_time.strftime('%Y-%m-%d')
                except Exception:
                    last_crawl = index_status["lastCrawlTime"]

            rich_results: Optional[List[str]] = None
            if "richResultsResult" in inspection:
                rich = inspection["richResultsResult"]
                if rich.get("verdict") == "PASS" and rich.get("detectedItems"):
                    rich_results = [
                        item.get("richResultType", "Unknown")
                        for item in rich["detectedItems"]
                    ]

            structured.append({
                "url": page_url,
                "verdict": verdict,
                "coverage": coverage,
                "last_crawl": last_crawl,
                "rich_results": rich_results,
                "error": None,
            })

        _log(
            "tool_exit",
            tool="gsc_batch_url_inspection",
            dur_ms=int((time.perf_counter() - _batch_start) * 1000),
            ok=True,
            urls_inspected=len(structured),
        )

        if fmt == "json":
            return {
                "ok": True,
                "tool": "gsc_batch_url_inspection",
                "site_url": site_url,
                "rows": structured,
                "row_count": len(structured),
                "next_offset": next_offset,
                "clamp_note": clamp_note.strip() or None,
                "meta": {
                    "site_url": site_url,
                    "from_session": from_session,
                    "dataset": dataset if from_session else None,
                },
            }

        # --- markdown path (byte-equivalent for the happy path) ---
        results: List[str] = []
        for row in structured:
            if row["error"] is not None and row["verdict"] is None and row["coverage"] is None:
                if row["error"] == "No inspection data found":
                    results.append(f"{row['url']}: No inspection data found")
                else:
                    results.append(f"{row['url']}: Error - {row['error']}")
                continue
            rich_cell = ", ".join(row["rich_results"]) if row["rich_results"] else "None"
            last_crawl_cell = row["last_crawl"] or "Never"
            results.append(
                f"{row['url']}:\n  Status: {row['verdict']} - {row['coverage']}\n"
                f"  Last Crawl: {last_crawl_cell}\n  Rich Results: {rich_cell}\n"
            )
        header = clamp_note + f"Batch URL Inspection Results for {site_url}:\n\n"
        next_offset_note = f"\nNext offset: {next_offset}" if next_offset is not None else ""
        return header + "\n".join(results) + next_offset_note

    except HttpError as e:
        _log(
            "tool_error",
            tool="gsc_batch_url_inspection",
            ok=False,
            error_type=type(e).__name__,
            error=str(e)[:200],
        )
        return _format_error(
            _http_error_envelope(e, tool="gsc_batch_url_inspection", site_url=site_url),
            response_format=fmt,
        )
    except Exception as e:
        _log(
            "tool_error",
            tool="gsc_batch_url_inspection",
            ok=False,
            error_type=type(e).__name__,
            error=str(e)[:200],
        )
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint="Set GSC_MCP_TELEMETRY=1 for structured logs and retry.",
                tool="gsc_batch_url_inspection",
            ),
            response_format=fmt,
        )

@mcp.tool()
async def gsc_check_indexing_issues(
    site_url: str,
    urls: str,
    response_format: str = "markdown",
    *,
    force: bool = False,
    account_alias: Optional[str] = None,
) -> Any:
    """Bucket up to 10 URLs (for more, run `gsc_inspect_start`) by indexing problem (not-indexed, canonical
    conflict, robots-blocked, fetch failure, indexed). Pick me when you
    want a triage summary across several URLs; use `gsc_inspect_url_enhanced`
    for one URL in full detail, or `gsc_batch_url_inspection` for uniform
    per-URL output.

    Args:
        site_url: GSC site URL (exact match; `sc-domain:example.com`
            for domain properties).
        urls: Newline-separated URLs (max 10).
        response_format: "markdown" (default) or "json".
    """
    fmt = str(response_format or "").strip().lower()
    if fmt not in ("markdown", "json"):
        return (
            "Error checking indexing issues: "
            f"response_format must be 'markdown' or 'json', got {response_format!r}"
        )

    def _val_error(msg: str) -> Any:
        if fmt == "json":
            return _make_error_envelope(error=msg, tool="gsc_check_indexing_issues")
        return msg

    try:
        url_list = [url.strip() for url in urls.split('\n') if url.strip()]

        if not url_list:
            return _val_error("No URLs provided for inspection.")

        if len(url_list) > 10:
            return _val_error(
                f"Too many URLs provided ({len(url_list)}). Please limit to 10 URLs per batch to avoid API quota issues."
            )

        try:
            _resolved_alias, service = await get_gsc_service_for_site(
                site_url, account_alias,
            )
        except AccountResolverError as e:
            return _format_error(
                e.to_envelope(tool="gsc_check_indexing_issues"),
                response_format=fmt,
            )

        _batch_start = time.perf_counter()
        _log(
            "tool_enter",
            tool="gsc_check_indexing_issues",
            site_url=site_url,
            url_count=len(url_list),
            account_alias=account_alias,
        )

        # Structured buckets. Each entry carries enough context to
        # render the markdown line without re-parsing a concat string.
        buckets: Dict[str, List[Any]] = {
            "not_indexed": [],        # list[{url, reason}]
            "canonical_conflict": [], # list[{url, google_canonical, user_canonical}]
            "robots_blocked": [],     # list[url]
            "fetch_failure": [],      # list[{url, state}]
            "indexed": [],            # list[url]
        }

        outcomes = await _inspect_many(
            service, site_url, url_list, force=force, wait_for_minute=False,
        )
        _raise_quota_error(outcomes)

        for page_url in url_list:
            outcome = outcomes.get(page_url)
            if isinstance(outcome, BaseException) or outcome is None:
                buckets["not_indexed"].append({"url": page_url, "reason": f"Error: {outcome}"})
                continue
            response = outcome[0]
            if not response or "inspectionResult" not in response:
                buckets["not_indexed"].append(
                    {"url": page_url, "reason": "No inspection data found"}
                )
                continue

            inspection = response["inspectionResult"]
            index_status = inspection.get("indexStatusResult", {})

            verdict = index_status.get("verdict", "UNKNOWN")
            coverage = index_status.get("coverageState", "Unknown")

            if verdict != "PASS" or "not indexed" in coverage.lower() or "excluded" in coverage.lower():
                buckets["not_indexed"].append({"url": page_url, "reason": coverage})
            else:
                buckets["indexed"].append(page_url)

            google_canonical = index_status.get("googleCanonical", "")
            user_canonical = index_status.get("userCanonical", "")
            if google_canonical and user_canonical and google_canonical != user_canonical:
                buckets["canonical_conflict"].append({
                    "url": page_url,
                    "google_canonical": google_canonical,
                    "user_canonical": user_canonical,
                })

            # The API's RobotsTxtState enum is ALLOWED | DISALLOWED; "BLOCKED"
            # is kept for backward compatibility with older responses.
            if index_status.get("robotsTxtState", "") in ("DISALLOWED", "BLOCKED"):
                buckets["robots_blocked"].append(page_url)

            fetch_state = index_status.get("pageFetchState", "")
            if fetch_state and fetch_state != "SUCCESSFUL":
                buckets["fetch_failure"].append({"url": page_url, "state": fetch_state})

        summary = {
            "total": len(url_list),
            "indexed": len(buckets["indexed"]),
            "not_indexed": len(buckets["not_indexed"]),
            "canonical_conflict": len(buckets["canonical_conflict"]),
            "robots_blocked": len(buckets["robots_blocked"]),
            "fetch_failure": len(buckets["fetch_failure"]),
        }

        _log(
            "tool_exit",
            tool="gsc_check_indexing_issues",
            dur_ms=int((time.perf_counter() - _batch_start) * 1000),
            ok=True,
            urls_inspected=len(url_list),
        )

        if fmt == "json":
            return {
                "ok": True,
                "tool": "gsc_check_indexing_issues",
                "site_url": site_url,
                "summary": summary,
                "buckets": buckets,
                "meta": {"site_url": site_url, "url_count": len(url_list)},
            }

        # --- markdown path (byte-equivalent for the happy path) ---
        result_lines = [f"Indexing Issues Report for {site_url}:"]
        result_lines.append("-" * 80)
        result_lines.append(f"Total URLs checked: {summary['total']}")
        result_lines.append(f"Indexed: {summary['indexed']}")
        result_lines.append(f"Not indexed: {summary['not_indexed']}")
        result_lines.append(f"Canonical issues: {summary['canonical_conflict']}")
        result_lines.append(f"Robots.txt blocked: {summary['robots_blocked']}")
        result_lines.append(f"Fetch issues: {summary['fetch_failure']}")
        result_lines.append("-" * 80)

        if buckets["not_indexed"]:
            result_lines.append("\nNot Indexed URLs:")
            for entry in buckets["not_indexed"]:
                result_lines.append(f"- {entry['url']} - {entry['reason']}")

        if buckets["canonical_conflict"]:
            result_lines.append("\nCanonical Issues:")
            for entry in buckets["canonical_conflict"]:
                result_lines.append(
                    f"- {entry['url']} - Google chose: {entry['google_canonical']} "
                    f"instead of user-declared: {entry['user_canonical']}"
                )

        if buckets["robots_blocked"]:
            result_lines.append("\nRobots.txt Blocked URLs:")
            for url in buckets["robots_blocked"]:
                result_lines.append(f"- {url}")

        if buckets["fetch_failure"]:
            result_lines.append("\nFetch Issues:")
            for entry in buckets["fetch_failure"]:
                result_lines.append(f"- {entry['url']} - {entry['state']}")

        return "\n".join(result_lines)

    except HttpError as e:
        _log(
            "tool_error",
            tool="gsc_check_indexing_issues",
            ok=False,
            error_type=type(e).__name__,
            error=str(e)[:200],
        )
        return _format_error(
            _http_error_envelope(e, tool="gsc_check_indexing_issues", site_url=site_url),
            response_format=fmt,
        )
    except Exception as e:
        _log(
            "tool_error",
            tool="gsc_check_indexing_issues",
            ok=False,
            error_type=type(e).__name__,
            error=str(e)[:200],
        )
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint="Set GSC_MCP_TELEMETRY=1 for structured logs and retry.",
                tool="gsc_check_indexing_issues",
            ),
            response_format=fmt,
        )

@mcp.tool()
async def gsc_get_performance_overview(
    site_url: str,
    days: int = 28,
    response_format: str = "markdown",
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    data_state: str = "final",
    account_alias: Optional[str] = None,
) -> Any:
    """Totals-plus-daily-trend snapshot for a GSC property. Pick me for a
    quick "how is my site doing?" read; use `gsc_get_search_analytics`
    or `gsc_get_advanced_search_analytics` to slice by dimension.

    Args:
        site_url: GSC site URL (exact match).
        days: The last N days of final data (default 28, like the GSC UI),
            ending on meta.latest_final_date (Pacific Time).
        response_format: `markdown` (default) | `csv` | `json`.
        start_date / end_date: Explicit YYYY-MM-DD window (overrides days).
        data_state: `final` (default) | `all` (preliminary days flagged).
        account_alias: Optional explicit account; omit to auto-resolve.
    """
    tool = "gsc_get_performance_overview"
    try:
        days = max(int(days), 1)
        async with _instrument(tool, site_url=site_url, days=days):
            ctx = await _sa_context(site_url, account_alias)
            w = await _resolve_window(
                ctx, start_date=start_date, end_date=end_date, days=days, data_state=data_state,
            )
            total_res = await _sa_run(
                ctx, dimensions=[], window=w, data_state=data_state, row_limit=1,
                step=f"{tool}.totals",
            )
            date_res = await _sa_run(
                ctx, dimensions=["date"], window=w, data_state=data_state,
                row_limit=w["window_days"], step=f"{tool}.daily",
            )

        span = f"last {days} days" if w["days_requested"] else f"{w['start_date']} to {w['end_date']}"
        if not total_res["rows"] and str(response_format).strip().lower() != "json":
            return f"No performance data found for {site_url} in the {span}."

        t = total_res["rows"][0] if total_res["rows"] else {}
        totals = {
            "clicks": t.get("clicks", 0),
            "impressions": t.get("impressions", 0),
            "ctr": t.get("ctr", 0),
            "position": t.get("position", 0),
        }

        # Daily trend rows, sorted by date ascending.
        trend_rows: List[Dict[str, Any]] = []
        for row in sorted(date_res["rows"], key=lambda x: str(x.get("date", ""))):
            date_str = str(row.get("date", ""))
            try:
                date_formatted = datetime.strptime(date_str, "%Y-%m-%d").strftime("%m/%d")
            except ValueError:
                date_formatted = date_str
            trend_row = {
                "date": date_formatted,
                "clicks": row.get("clicks", 0),
                "impressions": row.get("impressions", 0),
                "ctr": row.get("ctr", 0),
                "position": row.get("position", 0),
            }
            if "preliminary" in row:
                trend_row["preliminary"] = row["preliminary"]
            trend_rows.append(trend_row)

        columns = [
            {"key": "date", "display": "Date", "type": "str"},
            *_METRIC_COLUMNS,
        ]
        if date_res["first_incomplete_date"]:
            columns.append({"key": "preliminary", "display": "Preliminary", "type": "str"})

        header_lines = [
            f"Performance Overview for {site_url} ({span})",
            _window_line(w),
            f"Total Clicks: {totals['clicks']:,}",
            f"Total Impressions: {totals['impressions']:,}",
            f"Average CTR: {totals['ctr'] * 100:.2f}%",
            f"Average Position: {totals['position']:.1f}",
            "",
            "Daily Trend:",
        ]

        return _format_table(
            trend_rows,
            columns,
            response_format=response_format,
            header_lines=header_lines,
            meta=_sa_meta(
                date_res,
                site_url=site_url,
                days=days,
                totals=totals,
                daily_count=len(trend_rows),
                row_count=len(trend_rows),
            ),
            text_meta=True,
        )
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, site_url=site_url, response_format=response_format)

@mcp.tool()
async def gsc_get_advanced_search_analytics(
    site_url: str,
    start_date: str = None,
    end_date: str = None,
    dimensions: str = "query",
    search_type: str = "WEB",
    row_limit: int = 100,
    start_row: int = 0,
    sort_by: str = "clicks",
    sort_direction: str = "descending",
    filter_dimension: str = None,
    filter_operator: str = "contains",
    filter_expression: str = None,
    response_format: str = "markdown",
    *,
    filter_groups: Optional[List[Dict[str, Any]]] = None,
    data_state: str = "final",
    aggregation_type: Optional[str] = None,
    fetch_all: bool = False,
    max_rows: int = _SA_DEFAULT_MAX_ROWS,
    save_to_file: Optional[str] = None,
    account_alias: Optional[str] = None,
) -> Any:
    """GSC search analytics with sorting, filtering, and pagination. Pick me
    when you need more than a plain top-N summary; use `gsc_get_search_analytics`
    for a quick overview, `gsc_query` for every API parameter, or
    `gsc_get_search_by_page_query` to break one page down by query.

    Args:
        site_url: GSC site URL (exact match).
        start_date: YYYY-MM-DD (default: the last 28 days of final data).
        end_date: YYYY-MM-DD (default: meta.latest_final_date, Pacific Time).
        dimensions: Comma-separated (e.g. "query,page,device").
        search_type: web, image, video, news, googleNews or discover.
        row_limit: Rows per page (default 100; clamped to [1, 25000]).
            Paginate via `start_row`, or pass fetch_all / save_to_file.
        start_row: Starting row for pagination.
        sort_by: clicks | impressions | ctr | position. Applied client-side;
            any order other than the API's (clicks desc) fetches every row
            first so the top-N is exact.
        sort_direction: ascending | descending.
        filter_dimension: query | page | country | device | searchAppearance.
        filter_operator: contains | equals | notContains | notEquals |
            includingRegex | excludingRegex (RE2; "\\bsage\\b" matches the
            word "sage" but not "message").
        filter_expression: value to filter on.
        response_format: `markdown` (default) | `csv` | `json`.
        filter_groups: Several AND filters at once (see `gsc_query`); combined
            with the single filter_* filter when both are given.
        data_state: final (default) | all.
        aggregation_type: auto | byPage | byProperty.
        fetch_all / max_rows: page through every row up to max_rows.
        save_to_file: Absolute .csv/.json path; writes all rows, returns a summary.
    """
    tool = "gsc_get_advanced_search_analytics"
    try:
        row_limit = max(1, min(int(row_limit), 25000))
        dimension_list = [d.strip() for d in dimensions.split(",")]

        groups: List[Dict[str, Any]] = list(_normalize_filter_groups(filter_groups))
        if filter_dimension and filter_expression:
            single = {"filters": [{
                "dimension": filter_dimension,
                "operator": filter_operator,
                "expression": filter_expression,
            }]}
            groups = _normalize_filter_groups([single]) + groups

        async with _instrument(
            tool, site_url=site_url, row_limit=row_limit,
            start_row=start_row, dimensions=dimensions,
        ):
            ctx = await _sa_context(site_url, account_alias)
            res = await _sa_run(
                ctx, dimensions=dimension_list, start_date=start_date, end_date=end_date,
                search_type=search_type, filter_groups=groups, data_state=data_state,
                aggregation_type=aggregation_type, row_limit=row_limit,
                start_row=start_row, fetch_all=fetch_all or bool(save_to_file), max_rows=max_rows,
                sort_by=sort_by or None, sort_direction=sort_direction,
                include_totals=True, step=tool,
            )
        w = res["window"]
        start_date, end_date = w["start_date"], w["end_date"]
        filter_note_text = (
            f"{filter_dimension} {filter_operator} '{filter_expression}'"
            if filter_dimension else None
        )
        extra_groups = len(groups) - (1 if filter_note_text else 0)
        dims = res["dimensions"]
        rows_returned = len(res["rows"])
        columns = _sa_columns(dims, preliminary=bool(res["first_incomplete_date"]))

        header_lines = [
            f"Search analytics for {site_url}",
            f"Date range: {start_date} to {end_date}",
            _window_line(w),
            f"Search type: {search_type}",
        ]
        if filter_note_text:
            header_lines.append(f"Filter: {filter_note_text}")
        if extra_groups > 0:
            header_lines.append(f"Additional filter groups: {extra_groups} (see meta.filter_groups)")
        header_lines.append(
            f"Showing rows {start_row + 1} to {start_row + rows_returned} "
            f"(sorted by {sort_by} {sort_direction})"
        )

        next_row = res["next_start_row"]
        truncation_hint = (
            f"returned {rows_returned} rows and hit `row_limit={row_limit}`. "
            f"There may be more data. Pass a larger `row_limit` (max 25000), "
            f"paginate via `start_row={next_row if next_row is not None else start_row + row_limit}`, "
            f"or pass fetch_all / save_to_file."
        )

        meta = _sa_meta(
            res,
            site_url=site_url,
            start_date=start_date,
            end_date=end_date,
            dimensions=dims,
            search_type=search_type,
            row_limit=row_limit,
            start_row=start_row,
            sort_by=sort_by,
            sort_direction=sort_direction,
            next_start_row=next_row,
            filter_groups=res["filter_groups"],
        )
        if save_to_file:
            return await _saved_summary(
                tool=tool, path=save_to_file, rows=res["rows"], columns=columns,
                meta=meta, response_format=response_format, header_lines=header_lines,
                truncated=res["truncated"],
            )
        if not res["rows"] and str(response_format).strip().lower() != "json":
            filter_note = f"- Filter: {filter_note_text}" if filter_note_text else "- No filter applied"
            return (
                f"No search analytics data found for {site_url} with the specified parameters.\n\n"
                f"Parameters used:\n"
                f"- Date range: {start_date} to {end_date} (Pacific Time)\n"
                f"- Dimensions: {dimensions}\n"
                f"- Search type: {search_type}\n"
                f"{filter_note}"
            )
        rows = []
        for r in res["rows"]:
            row_dict = dict(r)
            for dim in dims:
                row_dict[dim] = str(r.get(dim, ""))[:100]
            rows.append(row_dict)
        return _format_table(
            rows,
            columns,
            response_format=response_format,
            header_lines=header_lines,
            truncated=res["truncated"],
            truncation_hint=truncation_hint,
            meta=meta,
            text_meta=True,
        )
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, site_url=site_url, response_format=response_format)

@mcp.tool()
async def gsc_compare_search_periods(
    site_url: str,
    period1_start: Optional[str] = None,
    period1_end: Optional[str] = None,
    period2_start: Optional[str] = None,
    period2_end: Optional[str] = None,
    dimensions: str = "query",
    limit: int = 10,
    *,
    days: Optional[int] = None,
    data_state: str = "final",
    upstream_row_limit: int = 500,
    response_format: str = "markdown",
    account_alias: Optional[str] = None,
) -> Any:
    """Compare GSC analytics between two time periods (period 2 minus period 1).

    Args:
        site_url: GSC site URL (exact match).
        period1_start / period1_end: Earlier period (YYYY-MM-DD).
        period2_start / period2_end: Later period (YYYY-MM-DD).
        dimensions: Dimensions to group by (default: query).
        limit: Number of top-N results to return after the diff (default 10).
        days: Instead of dates: period 2 = the last N days of final data,
            period 1 = the N days before it (equal length, both final).
        data_state: final (default) | all.
        upstream_row_limit: Per-period rows pulled from GSC before the
            join (default 500; clamped to [1, 25000]). A row missing from a
            period that hit this cap is reported as null (unknown), not 0;
            raise this if long-tail queries aren't matching between periods.
        response_format: `markdown` (default) | `csv` | `json`.
    """
    tool = "gsc_compare_search_periods"
    try:
        upstream_row_limit = max(1, min(int(upstream_row_limit), 25000))
        dimension_list = [d.strip() for d in dimensions.split(",")]
        explicit = [period1_start, period1_end, period2_start, period2_end]
        use_days = _comparison_mode(explicit, days)

        async with _instrument(
            tool, site_url=site_url, upstream_row_limit=upstream_row_limit, dimensions=dimensions,
        ):
            ctx = await _sa_context(site_url, account_alias)
            if use_days:
                w1, w2 = await _resolve_comparison_windows(ctx, days=days, data_state=data_state)
            else:
                w1 = await _resolve_window(ctx, start_date=period1_start, end_date=period1_end, data_state=data_state)
                w2 = await _resolve_window(ctx, start_date=period2_start, end_date=period2_end, data_state=data_state)
            res1 = await _sa_run(
                ctx, dimensions=dimension_list, window=w1, data_state=data_state,
                row_limit=upstream_row_limit, include_totals=True, step=f"{tool}.period1",
            )
            res2 = await _sa_run(
                ctx, dimensions=dimension_list, window=w2, data_state=data_state,
                row_limit=upstream_row_limit, include_totals=True, step=f"{tool}.period2",
            )
        period1_start, period1_end = w1["start_date"], w1["end_date"]
        period2_start, period2_end = w2["start_date"], w2["end_date"]
        dims = res1["dimensions"]

        if not res1["rows"] and not res2["rows"] and str(response_format).strip().lower() != "json":
            return f"No data found for either period for {site_url}."

        def _key(r: Dict[str, Any]) -> tuple:
            return tuple(r.get(d, "") for d in dims)

        period1_data = {_key(row): row for row in res1["rows"]}
        period2_data = {_key(row): row for row in res2["rows"]}
        p1_complete, p2_complete = res1["complete"], res2["complete"]

        all_keys = set(period1_data.keys()) | set(period2_data.keys())
        comparison_data: List[Dict[str, Any]] = []

        for key in all_keys:
            p1 = period1_data.get(key)
            p2 = period2_data.get(key)

            # A row absent from a period is 0 only if that period's fetch was
            # complete; absent from a capped sample it is unknown (null).
            p1_clicks = p1.get("clicks", 0) if p1 is not None else (0 if p1_complete else None)
            p2_clicks = p2.get("clicks", 0) if p2 is not None else (0 if p2_complete else None)
            click_diff = (
                p2_clicks - p1_clicks
                if p1_clicks is not None and p2_clicks is not None else None
            )
            # Ratio (e.g. -0.5353 = -53.53%). `_format_table`'s "pct"
            # column type handles the display formatting for markdown.
            clicks_pct = (click_diff / p1_clicks) if (click_diff is not None and p1_clicks) else None

            # Position is 1-indexed in GSC — 0 is not a valid rank. When a
            # side is absent we emit null rather than a misleading sentinel.
            p1_position = p1.get("position") if p1 is not None else None
            p2_position = p2.get("position") if p2 is not None else None
            pos_diff = (
                p1_position - p2_position
                if p1_position is not None and p2_position is not None
                else None
            )

            row: Dict[str, Any] = {}
            for i, dim in enumerate(dims):
                row[dim] = str(key[i])[:100] if i < len(key) else ""
            row.update({
                "p1_clicks": p1_clicks,
                "p2_clicks": p2_clicks,
                "click_diff": click_diff,
                "clicks_pct": clicks_pct,
                "p1_position": p1_position,
                "p2_position": p2_position,
                "pos_diff": pos_diff,
            })
            if p1_clicks is None or p2_clicks is None:
                row["absent_reason"] = "beyond_row_limit"
            comparison_data.append(row)

        # Sort by absolute click difference descending; unknown diffs last.
        comparison_data.sort(
            key=lambda r: (r["click_diff"] is None, -abs(r["click_diff"] or 0),
                           tuple(str(r.get(d, "")) for d in dims)),
        )
        total_matched = len(comparison_data)
        rows = comparison_data[:limit]
        truncated = total_matched > limit
        truncation_hint = (
            f"{total_matched} matched queries across both periods; only the top "
            f"{limit} are shown. Raise `limit` (default 10) or narrow the date "
            f"range to see more." if truncated else ""
        )

        columns: List[Dict[str, str]] = [
            {"key": dim, "display": dim.capitalize(), "type": "str"}
            for dim in dims
        ]
        columns.extend([
            {"key": "p1_clicks", "display": "P1 Clicks", "type": "int"},
            {"key": "p2_clicks", "display": "P2 Clicks", "type": "int"},
            {"key": "click_diff", "display": "Change", "type": "signed_int"},
            {"key": "clicks_pct", "display": "%", "type": "pct"},
            {"key": "p1_position", "display": "P1 Pos", "type": "float"},
            {"key": "p2_position", "display": "P2 Pos", "type": "float"},
            {"key": "pos_diff", "display": "Pos Δ", "type": "signed_float"},
        ])

        header_lines = [
            f"Search analytics comparison for {site_url}",
            f"Period 1: {period1_start} to {period1_end}",
            f"Period 2: {period2_start} to {period2_end}",
            f"Dimension(s): {dimensions}",
            f"Top {min(limit, len(comparison_data))} results by change in clicks",
        ]
        if not (p1_complete and p2_complete):
            header_lines.append(
                "Note: a period hit upstream_row_limit; rows missing from it show as blank (unknown), not 0."
            )

        meta = _standard_meta(
            None,
            site_url=site_url,
            period1={"start": period1_start, "end": period1_end, "window": _window_meta(w1),
                     "totals": res1["totals"]},
            period2={"start": period2_start, "end": period2_end, "window": _window_meta(w2),
                     "totals": res2["totals"]},
            dimensions=dims,
            limit=limit,
            upstream_row_limit=upstream_row_limit,
            total_matched=total_matched,
            coverage={
                "p1": "complete" if p1_complete else "truncated",
                "p2": "complete" if p2_complete else "truncated",
                **_coverage_rollup({
                    "period1_rows": res1["coverage"], "period2_rows": res2["coverage"],
                    "shown": _coverage(len(rows), total_matched, "matched keys"),
                }),
            },
            data_state=data_state,
            latest_final_date=w2["latest_final_date"],
            timezone=_PT_LABEL,
            row_count=len(rows),
            truncated=truncated,
            warnings=list(w1["warnings"]) + list(w2["warnings"]) + res1["warnings"] + res2["warnings"]
            + (["days ignored: explicit period dates were given."] if days is not None and not use_days else []),
        )
        return _format_table(
            rows,
            columns,
            response_format=response_format,
            header_lines=header_lines,
            truncated=truncated,
            truncation_hint=truncation_hint,
            meta=meta,
            text_meta=True,
        )
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, site_url=site_url, response_format=response_format)

@mcp.tool()
async def gsc_get_search_by_page_query(
    site_url: str,
    page_url: str,
    days: int = 28,
    row_limit: int = 20,
    response_format: str = "markdown",
    include_summary: Optional[bool] = None,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    data_state: str = "final",
    sort_by: Optional[str] = None,
    sort_direction: str = "descending",
    fetch_all: bool = False,
    account_alias: Optional[str] = None,
):
    """Break down GSC queries for a single page. Pick me when you already
    know which URL you want to analyse; use `gsc_get_search_analytics` for
    a property-wide overview or `gsc_get_advanced_search_analytics` for
    filtered/paginated analytics.

    Args:
        site_url: GSC site URL (exact match).
        page_url: Full page URL (scheme + host + path) matching the GSC
            `page` dimension exactly.
        days: The last N days of final data (default 28, like the GSC UI),
            ending on meta.latest_final_date (Pacific Time).
        row_limit: Max query rows (default 20; clamped to [1, 25000]).
            Raise to 500–1000 on pages ranking for many queries. The page
            total (a query-less call) and the unattributed share — traffic
            no returned query row accounts for, mostly anonymised queries —
            are always reported.
        response_format: "markdown" (default, compact) or "json"
            (structured; parseable for downstream code).
        include_summary: json-only. When None (default), the
            `summary` block is included when `row_limit > 50` and
            omitted below (the summary is misleading when many rows
            are capped). Pass True to force-include or False to
            force-omit.
        start_date / end_date: Explicit YYYY-MM-DD window (overrides days).
        data_state: final (default) | all.
        sort_by: clicks | impressions | ctr | position, sorted over the
            page's full query set (default: the API's clicks order).
        sort_direction: ascending | descending.
        fetch_all: return every query row for the page, not just row_limit.

    Returns:
        markdown mode (default): str table with a window line, a TOTAL row
        over returned queries, then the page total and unattributed share.
        json mode: dict with keys `ok, tool, site_url, page_url, days,
        row_limit, total_rows_returned, possibly_truncated, queries, meta`.
        `summary` key is included per the `include_summary` rule above;
        when present it holds
        `{total_clicks, total_impressions, average_position
        (impression-weighted), average_ctr}`.
        On error: an error envelope in json mode, or a string prefixed
        `"Error retrieving page query data: ..."` in markdown mode.
        Invalid response_format always returns a string error
        (conservative default).
    """
    tool = "gsc_get_search_by_page_query"
    fmt = str(response_format).strip().lower()
    if fmt not in ("markdown", "json"):
        return (
            "Error retrieving page query data: "
            f"response_format must be 'markdown' or 'json', got {response_format!r}"
        )

    async def _run() -> Tuple[Dict[str, Any], int, int, str]:
        effective_days = max(1, int(days))
        effective_row_limit = max(1, min(int(row_limit), 25000))
        ctx = await _sa_context(site_url, account_alias)
        async with _instrument(
            tool, site_url=site_url, page_url=page_url, mode=fmt,
            row_limit=effective_row_limit, account_alias=account_alias,
        ):
            res = await _sa_run(
                ctx, dimensions=["query"], days=effective_days, start_date=start_date,
                end_date=end_date, data_state=data_state,
                filter_groups=[{"filters": [{"dimension": "page", "operator": "equals", "expression": page_url}]}],
                row_limit=effective_row_limit, fetch_all=fetch_all, sort_by=sort_by,
                sort_direction=sort_direction, include_totals=True, step=tool,
            )
        w = res["window"]
        span = f"last {effective_days} days" if w["days_requested"] else f"{w['start_date']} to {w['end_date']}"
        return res, effective_days, effective_row_limit, span

    if fmt == "markdown":
        try:
            try:
                res, effective_days, effective_row_limit, span = await _run()
            except AccountResolverError as e:
                # Markdown branch — caller expects a plain string starting "Error:".
                env = e.to_envelope(tool=tool)
                return f"Error retrieving page query data: {env['error']}"

            rows = res["rows"]
            if not rows:
                msg = f"No search data found for page {page_url} in the {span}.\n{_window_line(res['window'])}"
                pt = (res["totals"] or {}).get("page_total") or {}
                if pt.get("clicks") or pt.get("impressions"):
                    # Traffic with no attributable query rows: all anonymised.
                    msg += (
                        f"\nPAGE TOTAL (query-less, incl. anonymised) | {pt['clicks']} | "
                        f"{pt['impressions']} | - | -\nUnattributed share: 100% "
                        f"(no query rows; every query is anonymised or below the row threshold)"
                    )
                for warning in res["window"]["warnings"] + res["warnings"]:
                    msg += f"\nWarning: {warning}"
                return msg

            result_lines = [f"Search queries for page {page_url} ({span}):"]
            result_lines.append(_window_line(res["window"]))
            result_lines.append("\n" + "-" * 80 + "\n")
            result_lines.append("Query | Clicks | Impressions | CTR | Position")
            result_lines.append("-" * 80)

            # Raw row values, no int/float coercion, "Unknown" for missing keys.
            for row in rows:
                query = row.get("query") or "Unknown"
                clicks = row.get("clicks", 0)
                impressions = row.get("impressions", 0)
                ctr = row.get("ctr", 0) * 100
                position = row.get("position", 0)
                result_lines.append(f"{query[:100]} | {clicks} | {impressions} | {ctr:.2f}% | {position:.1f}")

            total_clicks = sum(row.get("clicks", 0) for row in rows)
            total_impressions = sum(row.get("impressions", 0) for row in rows)
            avg_ctr = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0

            result_lines.append("-" * 80)
            result_lines.append(f"TOTAL | {total_clicks} | {total_impressions} | {avg_ctr:.2f}% | -")

            t = res["totals"]
            if t is not None:
                pt, share = t["page_total"], t["unattributed_share"]

                def _pct(v: Optional[float]) -> str:
                    return "n/a" if v is None else f"{v * 100:.2f}%"
                result_lines.append(
                    f"PAGE TOTAL (query-less, incl. anonymised) | {pt['clicks']} | {pt['impressions']} | - | -"
                )
                result_lines.append(
                    f"Unattributed share: clicks {_pct(share['clicks'])}, "
                    f"impressions {_pct(share['impressions'])} "
                    f"(query rows: {t['query_rows_sum_scope']})"
                )
            for warning in res["window"]["warnings"] + res["warnings"]:
                result_lines.append(f"Warning: {warning}")

            return "\n".join(result_lines)
        except Exception as e:
            return f"Error retrieving page query data: {str(e)}"

    # response_format == "json" — structured output with summary aggregates
    try:
        res, effective_days, effective_row_limit, span = await _run()

        queries: List[Dict[str, Any]] = []
        for row in res["rows"]:
            q: Dict[str, Any] = {
                "query": row.get("query", ""),
                "clicks": int(row.get("clicks", 0)),
                "impressions": int(row.get("impressions", 0)),
                "ctr": float(row.get("ctr", 0.0)),
                "position": float(row.get("position", 0.0)),
            }
            if "preliminary" in row:
                q["preliminary"] = row["preliminary"]
            queries.append(q)

        possibly_truncated = res["truncated"]
        result: Dict[str, Any] = {
            "ok": True,
            "tool": tool,
            "site_url": site_url,
            "page_url": page_url,
            "days": effective_days,
            "row_limit": effective_row_limit,
            "total_rows_returned": len(queries),
            "possibly_truncated": possibly_truncated,
            "truncation_hint": (
                f"Returned {len(queries)} rows at row_limit={effective_row_limit}. "
                f"Raise row_limit (up to 25000) or pass fetch_all=true to surface "
                f"long-tail queries for this page."
            ) if possibly_truncated else "",
            "queries": queries,
            "meta": _sa_meta(res, site_url=site_url, page_url=page_url),
        }

        # B.5: summary is suppressed by default when row_limit is low
        # enough that the aggregates would be misleading (they only
        # cover returned rows; if rows are capped, the averages are
        # skewed toward the top-ranked queries). Caller can override.
        if include_summary is None:
            include_summary = effective_row_limit > _PAGE_QUERY_SUMMARY_MIN_ROWS

        if include_summary:
            total_clicks = sum(q["clicks"] for q in queries)
            total_impressions = sum(q["impressions"] for q in queries)
            if total_impressions > 0:
                average_ctr = total_clicks / total_impressions
                average_position = sum(
                    q["position"] * q["impressions"] for q in queries
                ) / total_impressions
            else:
                average_ctr = 0.0
                average_position = 0.0
            result["summary"] = {
                "total_clicks": total_clicks,
                "total_impressions": total_impressions,
                "average_position": average_position,
                "average_ctr": average_ctr,
            }

        return result
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, site_url=site_url, response_format="json")


# --- Aggregated landing-page tools (Adds 2 + 3) ---

@mcp.tool()
async def gsc_get_landing_page_summary(
    site_url: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    top_n: int = 25,
    striking_distance_range: Tuple[float, float] = (11.0, 20.0),
    high_impression_min: int = 500,
    low_ctr_ratio: float = 0.5,
    country: Optional[str] = None,
    device: Optional[str] = None,
    *,
    days: int = 90,
    data_state: str = "final",
    account_alias: Optional[str] = None,
) -> Any:
    """Compact top-N landing pages for a GSC property with striking-distance
    and high-impression/low-CTR flags. Returns a dict (~2k tokens) so
    callers can ingest many pages without blowing context. Makes 2 API
    calls (site totals + top-N rows).

    Args:
        site_url: GSC site URL (exact match; `sc-domain:example.com` for
            domain properties).
        start_date: Window start. Accepts 'today', 'yesterday',
            'Ndaysago', or YYYY-MM-DD (Pacific Time). Default: the last
            `days` days of final data.
        end_date: Window end, same format (default: latest final date).
        top_n: Number of landing pages (default 25).
        striking_distance_range: [min, max] position band for the
            striking-distance flag (default (11.0, 20.0)). Must be
            finite with min <= max.
        high_impression_min: Min impressions for the high-impression/
            low-CTR flag (default 500).
        low_ctr_ratio: CTR below site_avg_ctr * this ratio flags the
            page (default 0.5).
        country: Optional ISO-3166 country filter (e.g. 'gbr').
        device: Optional 'DESKTOP' | 'MOBILE' | 'TABLET' filter.
        days: Window length when no dates are given (default 90).
        data_state: final (default) | all.
    """
    tool = "gsc_get_landing_page_summary"
    try:
        # Validate striking_distance_range up front so the error surfaces
        # before any API call. Accept tuple or list (JSON clients send arrays).
        try:
            sd_lo_raw, sd_hi_raw = striking_distance_range
            sd_lo = float(sd_lo_raw)
            sd_hi = float(sd_hi_raw)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "error": "striking_distance_range must be a two-item array/tuple [min, max]",
                "tool": tool,
            }
        if not math.isfinite(sd_lo) or not math.isfinite(sd_hi) or sd_lo > sd_hi:
            return {
                "ok": False,
                "error": "striking_distance_range must contain finite numbers with min <= max",
                "tool": tool,
            }

        try:
            if start_date:
                _parse_gsc_date(start_date)
            if end_date:
                _parse_gsc_date(end_date)
        except ValueError as e:
            return {"ok": False, "error": str(e), "tool": tool}

        filters: List[Dict[str, Any]] = []
        if country:
            filters.append({"dimension": "country", "operator": "equals", "expression": country})
        if device:
            filters.append({"dimension": "device", "operator": "equals", "expression": device.upper()})
        groups = [{"filters": filters}] if filters else None

        ctx = await _sa_context(site_url, account_alias)
        w = await _resolve_window(
            ctx, start_date=start_date, end_date=end_date, days=days, data_state=data_state,
        )
        totals_res = await _sa_run(
            ctx, dimensions=[], window=w, filter_groups=groups, data_state=data_state,
            row_limit=1, step=f"{tool}.totals",
        )
        if totals_res["rows"]:
            t = totals_res["rows"][0]
            site_totals = {
                "clicks": int(t.get("clicks", 0)),
                "impressions": int(t.get("impressions", 0)),
                "ctr": float(t.get("ctr", 0.0)),
                "position": float(t.get("position", 0.0)),
            }
        else:
            site_totals = {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}

        site_avg_ctr = site_totals["ctr"]

        # Top-N landing pages by clicks (the API's own order).
        pages_res = await _sa_run(
            ctx, dimensions=["page"], window=w, filter_groups=groups, data_state=data_state,
            row_limit=max(1, min(top_n, 25000)), step=f"{tool}.pages",
        )

        top_pages: List[Dict[str, Any]] = []
        for row in pages_res["rows"]:
            clicks = int(row.get("clicks", 0))
            impressions = int(row.get("impressions", 0))
            ctr = float(row.get("ctr", 0.0))
            position = float(row.get("position", 0.0))
            low_ctr_threshold = site_avg_ctr * low_ctr_ratio if site_avg_ctr > 0 else 0.0
            top_pages.append({
                "page": row.get("page", ""),
                "clicks": clicks,
                "impressions": impressions,
                "ctr": ctr,
                "position": position,
                "striking_distance_flag": sd_lo <= position <= sd_hi,
                "high_impression_low_ctr_flag": (
                    impressions >= high_impression_min
                    and site_avg_ctr > 0
                    and ctr < low_ctr_threshold
                ),
            })

        return {
            "ok": True,
            "tool": tool,
            "site_url": site_url,
            "start_date": w["start_date"],
            "end_date": w["end_date"],
            "site_totals": site_totals,
            "top_pages": top_pages,
            "thresholds": {
                "striking_distance_range": [sd_lo, sd_hi],
                "high_impression_min": high_impression_min,
                "low_ctr_ratio": low_ctr_ratio,
                "site_avg_ctr": site_avg_ctr,
            },
            "filters": {"country": country, "device": device},
            "meta": _sa_meta(pages_res, site_url=site_url),
        }
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(
            e, tool=tool, site_url=site_url,
            generic_hint="Check the date range and filter args; set GSC_MCP_TELEMETRY=1 for structured logs.",
        )


@mcp.tool()
async def gsc_compare_periods_landing_pages(
    site_url: str,
    period_a_start: Optional[str] = None,
    period_a_end: Optional[str] = None,
    period_b_start: Optional[str] = None,
    period_b_end: Optional[str] = None,
    min_impressions: int = 100,
    limit: int = 50,
    decay_threshold_pct: float = -0.20,
    sort_by: str = "clicks_delta",
    sort_direction: str = "asc",
    *,
    days: Optional[int] = None,
    data_state: str = "final",
    max_rows: int = _SA_DEFAULT_MAX_ROWS,
    account_alias: Optional[str] = None,
) -> Any:
    """Landing-page period-vs-period diff with decay_flag for content-rot
    detection. Fetches every page row for each period (one call per 25k
    rows), joins on page URL, sorts by a chosen delta column (B − A). Use
    `sort_by='clicks_delta'` + `sort_direction='asc'` for decayers
    (default); `desc` for risers.

    Args:
        site_url: GSC site URL.
        period_a_start / period_a_end: Earlier window; accepts 'today',
            'yesterday', 'Ndaysago', or YYYY-MM-DD.
        period_b_start / period_b_end: Later window, same format.
        min_impressions: Keep rows where period_a OR period_b
            impressions >= this (default 100).
        limit: Max rows after sorting (default 50).
        decay_threshold_pct: clicks_pct threshold for decay_flag
            (default -0.20 = 20% click drop; flag also requires
            position to have worsened).
        sort_by: `clicks_delta`, `clicks_pct`, `impressions_delta`,
            `impressions_pct`, `position_delta`, or `ctr_delta`.
        sort_direction: 'asc' or 'desc' (case-insensitive).
        days: Instead of dates: B = the last N days of final data, A = the
            N days before it (equal length, both final).
        data_state: final (default) | all.
        max_rows: Per-period page-row cap (default 100000). A page missing
            from a capped period is reported with null metrics, not 0.
    """
    tool = "gsc_compare_periods_landing_pages"
    try:
        if limit < 1:
            return {"ok": False, "error": "limit must be >= 1", "tool": tool}

        explicit = [period_a_start, period_a_end, period_b_start, period_b_end]
        try:
            use_days = _comparison_mode(explicit, days)
        except _SaValidationError as e:
            return {"ok": False, "error": str(e), "tool": tool}
        if not use_days:
            try:
                for d in explicit:
                    _parse_gsc_date(d)
            except ValueError as e:
                return {"ok": False, "error": str(e), "tool": tool}

        valid_sort_keys = {
            "clicks_delta", "clicks_pct",
            "impressions_delta", "impressions_pct",
            "position_delta", "ctr_delta",
        }
        if sort_by not in valid_sort_keys:
            return {
                "ok": False,
                "error": f"invalid sort_by: {sort_by!r}. Valid: {sorted(valid_sort_keys)}",
                "tool": tool,
            }

        direction_normalized = str(sort_direction).strip().lower()
        if direction_normalized not in ("asc", "desc"):
            return {
                "ok": False,
                "error": "sort_direction must be 'asc' or 'desc'",
                "tool": tool,
            }

        ctx = await _sa_context(site_url, account_alias)
        if use_days:
            wa, wb = await _resolve_comparison_windows(ctx, days=days, data_state=data_state)
        else:
            wa = await _resolve_window(ctx, start_date=period_a_start, end_date=period_a_end, data_state=data_state)
            wb = await _resolve_window(ctx, start_date=period_b_start, end_date=period_b_end, data_state=data_state)
        a_start, a_end, b_start, b_end = wa["start_date"], wa["end_date"], wb["start_date"], wb["end_date"]

        async with _instrument(
            tool, site_url=site_url, period_a=f"{a_start}..{a_end}", period_b=f"{b_start}..{b_end}",
        ):
            a_res = await _sa_run(
                ctx, dimensions=["page"], window=wa, data_state=data_state, fetch_all=True,
                max_rows=max_rows, step=f"{tool}.period_a",
            )
            b_res = await _sa_run(
                ctx, dimensions=["page"], window=wb, data_state=data_state, fetch_all=True,
                max_rows=max_rows, step=f"{tool}.period_b",
            )
        a_rows, b_rows = a_res["rows"], b_res["rows"]
        a_complete, b_complete = a_res["complete"], b_res["complete"]

        a_by_page = {r.get("page", ""): r for r in a_rows}
        b_by_page = {r.get("page", ""): r for r in b_rows}
        all_pages = set(a_by_page.keys()) | set(b_by_page.keys())

        def _metric(row: Optional[Dict[str, Any]], key: str, complete: bool) -> Optional[float]:
            # Absent from a complete fetch = 0; absent from a capped one = unknown.
            if row is None:
                return 0.0 if complete else None
            return float(row.get(key, 0.0))

        # NOTE: This duplicates the inline aggregation in gsc_get_search_by_page_query.
        # If extracted to a module-level helper, add regression coverage for both call sites.
        def _period_totals(rows: List[Dict[str, Any]]) -> Dict[str, float]:
            clicks = sum(int(r.get("clicks", 0)) for r in rows)
            impressions = sum(int(r.get("impressions", 0)) for r in rows)
            ctr = (clicks / impressions) if impressions > 0 else 0.0
            # Impression-weighted average position
            if impressions > 0:
                position = sum(
                    float(r.get("position", 0.0)) * int(r.get("impressions", 0))
                    for r in rows
                ) / impressions
            else:
                position = 0.0
            return {"clicks": clicks, "impressions": impressions, "ctr": ctr, "position": position}

        def _sub(b: Optional[float], a: Optional[float]) -> Optional[float]:
            return None if a is None or b is None else b - a

        diffs: List[Dict[str, Any]] = []
        for page in all_pages:
            a_row = a_by_page.get(page)
            b_row = b_by_page.get(page)
            a_impr_f = _metric(a_row, "impressions", a_complete)
            b_impr_f = _metric(b_row, "impressions", b_complete)

            # OR semantics on min_impressions: keep rows where either period clears the bar.
            if (a_impr_f or 0) < min_impressions and (b_impr_f or 0) < min_impressions:
                continue

            a_impr = int(a_impr_f) if a_impr_f is not None else None
            b_impr = int(b_impr_f) if b_impr_f is not None else None
            a_clicks_f = _metric(a_row, "clicks", a_complete)
            b_clicks_f = _metric(b_row, "clicks", b_complete)
            a_clicks = int(a_clicks_f) if a_clicks_f is not None else None
            b_clicks = int(b_clicks_f) if b_clicks_f is not None else None
            a_ctr = _metric(a_row, "ctr", a_complete)
            b_ctr = _metric(b_row, "ctr", b_complete)
            a_pos = _metric(a_row, "position", a_complete)
            b_pos = _metric(b_row, "position", b_complete)

            clicks_delta = _sub(b_clicks, a_clicks)
            impressions_delta = _sub(b_impr, a_impr)
            ctr_delta = _sub(b_ctr, a_ctr)
            position_delta = _sub(b_pos, a_pos)  # positive = worse (further down)

            clicks_pct = (clicks_delta / a_clicks) if (clicks_delta is not None and a_clicks) else None
            impressions_pct = (impressions_delta / a_impr) if (impressions_delta is not None and a_impr) else None

            decay_flag = (
                clicks_pct is not None
                and clicks_pct < decay_threshold_pct
                and position_delta is not None
                and position_delta > 0
            )

            diff = {
                "page": page,
                "a_clicks": a_clicks,
                "b_clicks": b_clicks,
                "clicks_delta": clicks_delta,
                "clicks_pct": clicks_pct,
                "a_impressions": a_impr,
                "b_impressions": b_impr,
                "impressions_delta": impressions_delta,
                "impressions_pct": impressions_pct,
                "a_ctr": a_ctr,
                "b_ctr": b_ctr,
                "ctr_delta": ctr_delta,
                "a_position": a_pos,
                "b_position": b_pos,
                "position_delta": position_delta,
                "decay_flag": decay_flag,
            }
            if a_clicks is None or b_clicks is None:
                diff["absent_reason"] = "beyond_row_limit"
            diffs.append(diff)

        # Sort with None-safe helper: None values for the sort column always
        # appear LAST regardless of direction (the naive (group, value) key
        # gets flipped by reverse=True and puts None rows at the front).
        diffs.sort(key=lambda r: r["page"])
        diffs = _sort_landing_page_diffs(diffs, sort_by, direction_normalized)
        sliced = diffs[:limit]

        return {
            "ok": True,
            "tool": tool,
            "site_url": site_url,
            "period_a": {"start": a_start, "end": a_end, "totals": _period_totals(a_rows)},
            "period_b": {"start": b_start, "end": b_end, "totals": _period_totals(b_rows)},
            "rows": sliced,
            "thresholds": {
                "min_impressions": min_impressions,
                "decay_threshold_pct": decay_threshold_pct,
            },
            "sort": {"by": sort_by, "direction": direction_normalized},
            "total_matched": len(diffs),
            "truncated": len(diffs) > limit,
            "meta": _standard_meta(
                None,
                site_url=site_url,
                period_a=_window_meta(wa),
                period_b=_window_meta(wb),
                coverage={
                    "a": "complete" if a_complete else "truncated",
                    "b": "complete" if b_complete else "truncated",
                    **_coverage_rollup({
                        "period_a_rows": a_res["coverage"], "period_b_rows": b_res["coverage"],
                        "shown": _coverage(len(sliced), len(diffs), "matched pages"),
                    }),
                },
                data_state=data_state,
                latest_final_date=wb["latest_final_date"],
                timezone=_PT_LABEL,
                row_count=len(sliced),
                truncated=len(diffs) > limit,
                warnings=list(wa["warnings"]) + list(wb["warnings"])
                + (["days ignored: explicit period dates were given."] if days is not None and not use_days else []),
            ),
        }
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(
            e, tool=tool, site_url=site_url,
            generic_hint="Check date args and sort_by / sort_direction values; "
                         "set GSC_MCP_TELEMETRY=1 for structured logs.",
        )


@mcp.tool()
async def gsc_list_sitemaps_enhanced(
    site_url: str,
    sitemap_index: str = None,
    response_format: str = "markdown",
    *,
    account_alias: Optional[str] = None,
) -> Any:
    """List submitted sitemaps for a GSC property with submission +
    download timestamps, type, URL counts, and error/warning totals.
    Pick me for the detailed table; use `gsc_get_sitemaps` for a compact
    Valid/Has-errors summary.

    Args:
        site_url: GSC site URL (exact match).
        sitemap_index: Optional sitemap-index URL to list its children.
        response_format: `markdown` (default) | `csv` | `json`.
        account_alias: Optional explicit account; omit to auto-resolve.
    """
    try:
        try:
            _resolved_alias, service = await get_gsc_service_for_site(
                site_url, account_alias,
            )
        except AccountResolverError as e:
            return _format_error(
                e.to_envelope(tool="gsc_list_sitemaps_enhanced"),
                response_format=response_format,
            )

        if sitemap_index:
            sitemaps = await _gsc_execute(
                lambda: service.sitemaps().list(
                    siteUrl=site_url, sitemapIndex=sitemap_index
                ).execute(),
                step="sitemaps.list",
            )
            source = f"child sitemaps from index: {sitemap_index}"
        else:
            sitemaps = await _gsc_execute(
                lambda: service.sitemaps().list(siteUrl=site_url).execute(),
                step="sitemaps.list",
            )
            source = "all submitted sitemaps"

        raw = sitemaps.get("sitemap") or []
        if not raw:
            suffix = f" in index {sitemap_index}" if sitemap_index else "."
            return f"No sitemaps found for {site_url}{suffix}"

        rows: List[Dict[str, Any]] = []
        for sitemap in raw:
            path = sitemap.get("path", "Unknown")

            def _fmt_date(raw_date: str) -> str:
                if raw_date == "Never":
                    return raw_date
                try:
                    dt = datetime.fromisoformat(raw_date.replace('Z', '+00:00'))
                    return dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    return raw_date

            last_submitted = _fmt_date(sitemap.get("lastSubmitted", "Never"))
            last_downloaded = _fmt_date(sitemap.get("lastDownloaded", "Never"))
            sitemap_type = "Index" if sitemap.get("isSitemapsIndex", False) else "Sitemap"

            # GSC returns errors/warnings as strings — coerce for sort /
            # meta consistency, let int column type re-stringify.
            try:
                errors = int(sitemap.get("errors", 0) or 0)
            except (TypeError, ValueError):
                errors = 0
            try:
                warnings = int(sitemap.get("warnings", 0) or 0)
            except (TypeError, ValueError):
                warnings = 0

            url_count: Optional[int] = None
            if "contents" in sitemap:
                for content in sitemap["contents"]:
                    if content.get("type") == "web":
                        try:
                            url_count = int(content.get("submitted", 0) or 0)
                        except (TypeError, ValueError):
                            url_count = None
                        break

            rows.append({
                "path": path,
                "last_submitted": last_submitted,
                "last_downloaded": last_downloaded,
                "type": sitemap_type,
                "urls": url_count,
                "errors": errors,
                "warnings": warnings,
            })

        columns = [
            {"key": "path", "display": "Path", "type": "str"},
            {"key": "last_submitted", "display": "Last Submitted", "type": "str"},
            {"key": "last_downloaded", "display": "Last Downloaded", "type": "str"},
            {"key": "type", "display": "Type", "type": "str"},
            {"key": "urls", "display": "URLs", "type": "int"},
            {"key": "errors", "display": "Errors", "type": "int"},
            {"key": "warnings", "display": "Warnings", "type": "int"},
        ]

        pending_count = sum(1 for s in raw if s.get("isPending", False))
        header_lines = [f"Sitemaps for {site_url} ({source})"]

        meta = {
            "site_url": site_url,
            "sitemap_index": sitemap_index,
            "count": len(rows),
            "pending_count": pending_count,
        }

        rendered = _format_table(
            rows,
            columns,
            response_format=response_format,
            header_lines=header_lines,
            meta=meta,
        )

        # Pending-processing footnote is agent-useful and was part of
        # the legacy markdown — append to str modes, expose via meta in
        # json mode (already in meta above).
        if pending_count > 0 and isinstance(rendered, str):
            note = f"\nNote: {pending_count} sitemaps are still pending processing by Google."
            return rendered + note
        return rendered
    except HttpError as e:
        return _format_error(
            _http_error_envelope(e, tool="gsc_list_sitemaps_enhanced", site_url=site_url),
            response_format=response_format,
        )
    except Exception as e:
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint="Set GSC_MCP_TELEMETRY=1 for structured logs and retry.",
                tool="gsc_list_sitemaps_enhanced",
            ),
            response_format=response_format,
        )

@mcp.tool()
async def gsc_get_sitemap_details(
    site_url: str,
    sitemap_url: str,
    *,
    account_alias: Optional[str] = None,
) -> str:
    """
    Get detailed information about a specific sitemap.

    Args:
        site_url: The URL of the site in Search Console (must be exact match)
        sitemap_url: The full URL of the sitemap to inspect
        account_alias: Optional explicit account; omit to auto-resolve.
    """
    try:
        try:
            def _do(svc):
                return svc.sitemaps().get(siteUrl=site_url, feedpath=sitemap_url).execute()
            _resolved, _service, details = await _call_with_stale_retry(
                site_url=site_url, account_alias=account_alias, api_call=_do, step="sitemaps.get",
            )
        except AccountResolverError as e:
            return _format_error(
                e.to_envelope(tool="gsc_get_sitemap_details"),
                response_format="markdown",
            )
        
        if not details:
            return f"No details found for sitemap {sitemap_url}."
        
        # Format the results
        result_lines = [f"Sitemap Details for {sitemap_url}:"]
        result_lines.append("-" * 80)
        
        # Basic info
        is_index = details.get("isSitemapsIndex", False)
        result_lines.append(f"Type: {'Sitemap Index' if is_index else 'Sitemap'}")
        
        # Status
        is_pending = details.get("isPending", False)
        result_lines.append(f"Status: {'Pending processing' if is_pending else 'Processed'}")
        
        # Dates
        if "lastSubmitted" in details:
            try:
                dt = datetime.fromisoformat(details["lastSubmitted"].replace('Z', '+00:00'))
                result_lines.append(f"Last Submitted: {dt.strftime('%Y-%m-%d %H:%M')}")
            except:
                result_lines.append(f"Last Submitted: {details['lastSubmitted']}")
        
        if "lastDownloaded" in details:
            try:
                dt = datetime.fromisoformat(details["lastDownloaded"].replace('Z', '+00:00'))
                result_lines.append(f"Last Downloaded: {dt.strftime('%Y-%m-%d %H:%M')}")
            except:
                result_lines.append(f"Last Downloaded: {details['lastDownloaded']}")
        
        # Errors and warnings
        result_lines.append(f"Errors: {details.get('errors', 0)}")
        result_lines.append(f"Warnings: {details.get('warnings', 0)}")
        
        # Content breakdown
        if "contents" in details and details["contents"]:
            result_lines.append("\nContent Breakdown:")
            for content in details["contents"]:
                content_type = content.get("type", "Unknown").upper()
                submitted = content.get("submitted", 0)
                indexed = content.get("indexed", "N/A")
                
                result_lines.append(f"- {content_type}: {submitted} submitted, {indexed} indexed")
        
        # If it's an index, suggest how to list child sitemaps
        if is_index:
            result_lines.append("\nThis is a sitemap index. To list child sitemaps, use:")
            result_lines.append(f"gsc_list_sitemaps_enhanced with sitemap_index={sitemap_url}")
        
        return "\n".join(result_lines)
    except HttpError as e:
        env = _http_error_envelope(e, tool="gsc_get_sitemap_details", site_url=site_url)
        return _format_error(env, response_format="markdown")
    except Exception as e:
        env = _make_error_envelope(
            error=f"{type(e).__name__}: {e}",
            hint="Set GSC_MCP_TELEMETRY=1 for structured logs and retry.",
            tool="gsc_get_sitemap_details",
        )
        return _format_error(env, response_format="markdown")

@mcp.tool()
async def gsc_submit_sitemap(
    site_url: str,
    sitemap_url: str,
    *,
    account_alias: Optional[str] = None,
) -> str:
    """
    Submit a new sitemap or resubmit an existing one to Google.

    Args:
        site_url: The URL of the site in Search Console (must be exact match)
        sitemap_url: The full URL of the sitemap to submit
        account_alias: Optional explicit account; omit to auto-resolve.
    """
    try:
        try:
            _resolved_alias, service = await get_gsc_service_for_site(
                site_url, account_alias,
            )
        except AccountResolverError as e:
            return _format_error(
                e.to_envelope(tool="gsc_submit_sitemap"),
                response_format="markdown",
            )

        # Submit the sitemap
        await _gsc_execute(
            lambda: service.sitemaps().submit(siteUrl=site_url, feedpath=sitemap_url).execute(),
            step="sitemaps.submit",
        )
        
        # Verify submission by getting details
        try:
            details = await _gsc_execute(
                lambda: service.sitemaps().get(siteUrl=site_url, feedpath=sitemap_url).execute(),
                step="sitemaps.get",
            )
            
            # Format response
            result_lines = [f"Successfully submitted sitemap: {sitemap_url}"]
            
            # Add submission time if available
            if "lastSubmitted" in details:
                try:
                    dt = datetime.fromisoformat(details["lastSubmitted"].replace('Z', '+00:00'))
                    result_lines.append(f"Submission time: {dt.strftime('%Y-%m-%d %H:%M')}")
                except:
                    result_lines.append(f"Submission time: {details['lastSubmitted']}")
            
            # Add processing status
            is_pending = details.get("isPending", True)
            result_lines.append(f"Status: {'Pending processing' if is_pending else 'Processing started'}")
            
            # Add note about processing time
            result_lines.append("\nNote: Google may take some time to process the sitemap. Check back later for full details.")
            
            return "\n".join(result_lines)
        except Exception:
            # If we can't get details, just return basic success message.
            # We swallow the details-fetch failure — the submit itself
            # already succeeded; the agent just gets a slimmer confirmation.
            return f"Successfully submitted sitemap: {sitemap_url}\n\nGoogle will queue it for processing."

    except HttpError as e:
        return _format_error(
            _http_error_envelope(e, tool="gsc_submit_sitemap", site_url=site_url),
            response_format="markdown",
        )
    except Exception as e:
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint="Verify the sitemap URL is reachable and uses the same "
                     "scheme/host as the GSC property.",
                tool="gsc_submit_sitemap",
            ),
            response_format="markdown",
        )

@mcp.tool()
async def gsc_delete_sitemap(
    site_url: str,
    sitemap_url: str,
    *,
    account_alias: Optional[str] = None,
) -> str:
    """
    Delete (unsubmit) a sitemap from Google Search Console.

    Args:
        site_url: The URL of the site in Search Console (must be exact match)
        sitemap_url: The full URL of the sitemap to delete
        account_alias: Optional explicit account; omit to auto-resolve.
    """
    try:
        try:
            _resolved_alias, service = await get_gsc_service_for_site(
                site_url, account_alias,
            )
        except AccountResolverError as e:
            return _format_error(
                e.to_envelope(tool="gsc_delete_sitemap"),
                response_format="markdown",
            )

        # Pre-check: if the sitemap isn't registered, short-circuit to an
        # idempotent "already deleted" message rather than surfacing an
        # error envelope (matches gsc_delete_site's 404 semantics from the
        # site-CRUD B.4 rollout).
        try:
            await _gsc_execute(
                lambda: service.sitemaps().get(siteUrl=site_url, feedpath=sitemap_url).execute(),
                step="sitemaps.get",
            )
        except HttpError as e:
            if getattr(e.resp, "status", None) == 404:
                return f"Sitemap not found: {sitemap_url}. It may have already been deleted or was never submitted."
            raise
        except Exception as e:
            # Older error shapes used string match on "404"; keep the
            # fallback for defensive coverage of non-HttpError 404s.
            if "404" in str(e):
                return f"Sitemap not found: {sitemap_url}. It may have already been deleted or was never submitted."
            raise

        await _gsc_execute(
            lambda: service.sitemaps().delete(siteUrl=site_url, feedpath=sitemap_url).execute(),
            step="sitemaps.delete",
        )
        return (
            f"Successfully deleted sitemap: {sitemap_url}\n\n"
            "Note: This only removes the sitemap from Search Console. Any URLs "
            "already indexed will remain in Google's index."
        )
    except HttpError as e:
        return _format_error(
            _http_error_envelope(e, tool="gsc_delete_sitemap", site_url=site_url),
            response_format="markdown",
        )
    except Exception as e:
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint="Verify the sitemap URL was submitted in the first place; "
                     "use `gsc_list_sitemaps_enhanced` to list known sitemaps.",
                tool="gsc_delete_sitemap",
            ),
            response_format="markdown",
        )

@mcp.tool()
async def gsc_manage_sitemaps(
    site_url: str,
    action: str,
    sitemap_url: str = None,
    sitemap_index: str = None,
    *,
    account_alias: Optional[str] = None,
) -> str:
    """
    All-in-one tool to manage sitemaps (list, get details, submit, delete).

    Args:
        site_url: The URL of the site in Search Console (must be exact match)
        action: The action to perform (list, details, submit, delete)
        sitemap_url: The full URL of the sitemap (required for details, submit, delete)
        sitemap_index: Optional sitemap index URL for listing child sitemaps (only used with 'list' action)
        account_alias: Optional explicit account; omit to auto-resolve.
    """
    try:
        # Validate inputs
        action = action.lower().strip()
        valid_actions = ["list", "details", "submit", "delete"]

        if action not in valid_actions:
            return f"Invalid action: {action}. Please use one of: {', '.join(valid_actions)}"

        if action in ["details", "submit", "delete"] and not sitemap_url:
            return f"The {action} action requires a sitemap_url parameter."

        # Perform the requested action (pass account_alias through to inner tools).
        if action == "list":
            return await gsc_list_sitemaps_enhanced(site_url, sitemap_index, account_alias=account_alias)
        elif action == "details":
            return await gsc_get_sitemap_details(site_url, sitemap_url, account_alias=account_alias)
        elif action == "submit":
            return await gsc_submit_sitemap(site_url, sitemap_url, account_alias=account_alias)
        elif action == "delete":
            return await gsc_delete_sitemap(site_url, sitemap_url, account_alias=account_alias)

    except Exception as e:
        # Dispatcher-level safety net. The underlying tools handle
        # HttpError + their own exceptions, so this branch only fires
        # for a validation-layer programming error (unreachable in
        # normal flow) or an exception escaping the dispatch logic
        # itself (e.g. TypeError on bad kwargs).
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint="Check `action` is one of: list, details, submit, delete.",
                tool="gsc_manage_sitemaps",
            ),
            response_format="markdown",
        )


# --- Account Management Tools ---

def _read_account_scopes(token_file_relative: Optional[str]) -> List[str]:
    """Load an account's OAuth credentials and return its granted scope names.
    Never include exception repr in the return — Credentials objects can
    leak refresh tokens when stringified."""
    if not token_file_relative:
        return ["<unavailable>"]
    token_path = token_file_relative
    if not os.path.isabs(token_path):
        token_path = os.path.join(GSC_STATE_DIR, token_path)
    if not os.path.exists(token_path):
        return ["<unavailable>"]
    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    except Exception:
        return ["<unavailable>"]
    raw_scopes = getattr(creds, "scopes", None) or []
    # Trim the common Google prefix for readability.
    trimmed = []
    for scope in raw_scopes:
        if scope.startswith("https://www.googleapis.com/auth/"):
            trimmed.append(scope.rsplit("/", 1)[-1])
        else:
            trimmed.append(scope)
    return trimmed or ["<unavailable>"]


@mcp.tool()
async def gsc_list_accounts(
    include_properties: bool = False,
    response_format: str = "markdown",
) -> Any:
    """
    Lists all configured Google accounts with their aliases, emails, and
    granted OAuth scopes.

    In v1.2.0+ there is no "active account" concept — routing is per-call
    via site_url auto-resolution or an explicit account_alias argument on
    the tool.

    Args:
        include_properties: When True, also returns each account's
            ``properties[]`` list (site URLs it can see). Default False
            for privacy + speed — enumerating properties across every
            configured account can leak cross-client information and
            requires N discovery calls. Always returns ``property_count``
            in JSON mode (from cache if warm; null if not).
        response_format: ``markdown`` (default) | ``json``.
    """
    fmt = str(response_format or "").strip().lower()
    if fmt not in ("markdown", "json"):
        return (
            "Error listing accounts: "
            f"response_format must be 'markdown' or 'json', got {response_format!r}"
        )

    try:
        manifest = _load_manifest()
        accounts = manifest.get("accounts", {})

        if not accounts:
            if fmt == "json":
                return {
                    "ok": True,
                    "tool": "gsc_list_accounts",
                    "accounts": [],
                    "meta": {"account_count": 0},
                }
            return (
                "No accounts configured.\n\n"
                "Use `gsc_add_account` to add a Google account."
            )

        # Optionally warm the property cache (one non-interactive call
        # per account). Errors-per-alias don't abort the whole listing.
        if include_properties:
            await asyncio.gather(
                *(_ensure_property_cache(a) for a in accounts),
                return_exceptions=True,
            )

        aliases_sorted = sorted(accounts.keys())

        if fmt == "json":
            payload: List[Dict[str, Any]] = []
            for alias in aliases_sorted:
                info = accounts[alias]
                entry: Dict[str, Any] = {
                    "alias": alias,
                    "email": info.get("email"),
                    "added_at": info.get("added_at"),
                    "scopes": _read_account_scopes(info.get("token_file")),
                }
                # property_count: from cache if warm, else null.
                if _account_property_state.get(alias) == "ok":
                    entry["property_count"] = len(_account_properties.get(alias, set()))
                else:
                    entry["property_count"] = None
                if include_properties:
                    entry["properties"] = sorted(
                        _account_properties.get(alias, set())
                    ) if _account_property_state.get(alias) == "ok" else None
                    entry["discovery_state"] = _account_property_state.get(alias, "never")
                    if _account_property_state.get(alias) == "error":
                        entry["discovery_error"] = _account_property_error.get(alias)
                payload.append(entry)
            return {
                "ok": True,
                "tool": "gsc_list_accounts",
                "accounts": payload,
                "meta": {
                    "account_count": len(payload),
                    "include_properties": include_properties,
                },
            }

        # Markdown rendering.
        lines = ["# Google Search Console Accounts\n"]
        for alias in aliases_sorted:
            info = accounts[alias]
            email = info.get("email") or "unknown"
            added = info.get("added_at", "unknown")
            lines.append(f"- **{alias}**: {email} (added {added})")
            scopes = _read_account_scopes(info.get("token_file"))
            lines.append(f"  - scopes: {', '.join(scopes)}")
            if _account_property_state.get(alias) == "ok":
                count = len(_account_properties.get(alias, set()))
                lines.append(f"  - property_count: {count}")
            if include_properties and _account_property_state.get(alias) == "ok":
                for site in sorted(_account_properties.get(alias, set())):
                    lines.append(f"    - {site}")
            elif include_properties and _account_property_state.get(alias) == "error":
                lines.append(
                    f"  - discovery error: "
                    f"{_account_property_error.get(alias, 'unknown')}"
                )

        lines.append(f"\nTotal: {len(accounts)} account(s)")
        return "\n".join(lines)
    except Exception as e:
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint="Check that the accounts manifest exists and is readable.",
                tool="gsc_list_accounts",
            ),
            response_format=fmt,
        )


@mcp.tool()
async def gsc_whoami(site_url: str) -> Any:
    """
    Diagnostic: show which account would serve ``site_url`` without
    actually running a GSC API call. Useful for pre-flight checks in
    agent workflows that want to verify routing before an expensive
    analytics call.

    Returns a JSON envelope (no markdown mode — this is a structured
    diagnostic). On unique resolution, ``resolved_account`` names the
    alias and ``alternatives`` is empty. On ambiguous, ``resolved_account``
    is null and ``alternatives`` lists the candidates.

    Args:
        site_url: GSC property URL (exact match).
    """
    try:
        resolved = await _resolve_account(site_url, None)
        return {
            "ok": True,
            "tool": "gsc_whoami",
            "site_url": site_url,
            "resolved_account": resolved,
            "alternatives": [],
            "meta": {"site_url": site_url},
        }
    except AccountResolverError as e:
        if e.code == ErrorCode.AMBIGUOUS_ACCOUNT:
            return {
                "ok": True,
                "tool": "gsc_whoami",
                "site_url": site_url,
                "resolved_account": None,
                "alternatives": e.alternatives or [],
                "meta": {"site_url": site_url, "ambiguous": True},
            }
        return e.to_envelope(tool="gsc_whoami")
    except Exception as e:
        return _make_error_envelope(
            error=f"{type(e).__name__}: {e}",
            hint="Retry after a short backoff; check gsc_list_accounts if persistent.",
            tool="gsc_whoami",
        )


@mcp.tool()
async def gsc_get_active_account() -> Any:
    """
    DEPRECATED (v1.2.0): the "active account" concept has been removed.
    Routing is per-call via site_url auto-resolution or explicit
    ``account_alias`` argument on each tool.

    Returns an ``ok: false`` envelope with
    ``error_code: DEPRECATED_TOOL`` so callers that forgot to update
    notice. Use :func:`gsc_whoami` (needs a site_url) for routing
    diagnostics, or :func:`gsc_list_accounts` for the account roster.
    """
    print(
        "[gsc-mcp] DEPRECATED call: gsc_get_active_account. v1.2.0 has no "
        "active-account concept. Use gsc_whoami(site_url=...) instead.",
        file=sys.stderr, flush=True,
    )
    return _make_error_envelope(
        error="gsc_get_active_account is deprecated; v1.2.0 has no active-account concept.",
        error_code=ErrorCode.DEPRECATED_TOOL,
        retryable=False,
        hint=(
            "Use gsc_whoami(site_url=...) to see which account will serve a "
            "given property, or gsc_list_accounts to view all configured accounts."
        ),
        tool="gsc_get_active_account",
        replacement={
            # ``suggested_tool`` (not ``tool``) to avoid collision with
            # the envelope-level ``tool`` field that names the deprecated
            # caller. Agents branch on ``error_code == DEPRECATED_TOOL``
            # and then read ``replacement.suggested_tool`` to learn
            # which tool to migrate to.
            "suggested_tool": "gsc_whoami",
            "example": "gsc_whoami(site_url='sc-domain:example.com')",
        },
    )


@mcp.tool()
async def gsc_add_account(alias: str) -> str:
    """
    Adds a new Google account. Opens a browser window for Google OAuth login.

    After authentication the alias is immediately available for use as
    ``account_alias`` on any site_url-taking tool, or auto-resolves
    from site_url when that property is reachable from exactly one
    configured account. v1.2.0 removed the "active account" concept;
    routing is per-call.

    The alias ``default`` is reserved and will be rejected (it was a
    placeholder in pre-v1.2.0 releases and is not user-facing any more).

    Args:
        alias: A short name for this account (lowercase alphanumeric and hyphens, 1-30 chars).
               Examples: 'client-a', 'personal', 'agency-main'.
    """
    try:
        alias = _validate_alias(alias)
    except ValueError as e:
        return f"Invalid alias: {str(e)}"

    # v1.2.0: the 'default' alias is reserved. Pre-v1.2.0 installs used
    # it as a placeholder for the migrated legacy token; keeping it
    # creatable would let users re-introduce the exact footgun this
    # refactor removes (a reserved-sounding alias with unclear routing).
    if alias == "default":
        return _format_error(
            _make_error_envelope(
                error="Alias 'default' is reserved and cannot be created.",
                error_code=ErrorCode.BAD_REQUEST,
                hint="Pick a meaningful alias (e.g. a client name or workspace).",
                tool="gsc_add_account",
            ),
            response_format="markdown",
        )

    try:
        manifest = _load_manifest()

        # Check for alias collision
        if alias in manifest.get("accounts", {}):
            return f"Account '{alias}' already exists. Use a different alias or remove it first with `gsc_remove_account`."

        # Check client secrets
        if not os.path.exists(OAUTH_CLIENT_SECRETS_FILE):
            return (
                "OAuth client secrets file not found. Please place a client_secrets.json file "
                "in the script directory or set the GSC_OAUTH_CLIENT_SECRETS_FILE environment variable."
            )

        # Create account directory (fail if already exists as secondary guard)
        acct_dir = os.path.join(ACCOUNTS_DIR, alias)
        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        try:
            os.mkdir(acct_dir)
        except FileExistsError:
            return f"Account directory for '{alias}' already exists. Remove it first with `gsc_remove_account`."
        token_path = os.path.join(acct_dir, "token.json")

        # Run OAuth flow
        try:
            flow = InstalledAppFlow.from_client_secrets_file(OAUTH_CLIENT_SECRETS_FILE, OAUTH_SCOPES)
            creds = _start_oauth_flow(flow, context=f"gsc_add_account('{alias}')")
        except HeadlessOAuthError as e:
            # HeadlessOAuthError carries its own remediation message —
            # surface it verbatim rather than wrapping in an envelope.
            shutil.rmtree(acct_dir, ignore_errors=True)
            return str(e)
        except Exception as e:
            # Clean up partial directory on OAuth failure
            shutil.rmtree(acct_dir, ignore_errors=True)
            return _format_error(
                _make_error_envelope(
                    error=f"OAuth flow failed: {type(e).__name__}: {e}",
                    hint="Check that client_secrets.json is current and that the "
                         "browser was able to reach the local callback port.",
                    tool="gsc_add_account",
                ),
                response_format="markdown",
            )

        # Save token
        _write_token_file(token_path, creds.to_json())

        # Detect email. _detect_email does a sync urllib GET against
        # tokeninfo (10s timeout); offload to a thread so we don't
        # block the asyncio loop in the middle of gsc_add_account.
        email = await asyncio.to_thread(_detect_email, creds)

        # Update manifest
        manifest.setdefault("accounts", {})[alias] = {
            "alias": alias,
            "email": email,
            "token_file": f"accounts/{alias}/token.json",
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest["active_account"] = alias
        _save_manifest(manifest)

        # Set as active
        global _active_account
        _active_account = alias

        email_str = email or "unknown"
        return f"Account '{alias}' added and set as active. Email: {email_str}"
    except Exception as e:
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint=f"The `{alias}` account directory may be in a partial state; "
                     "try `gsc_remove_account` to clean up, then retry.",
                tool="gsc_add_account",
            ),
            response_format="markdown",
        )


@mcp.tool()
async def gsc_switch_account(alias: str) -> Any:
    """
    DEPRECATED (v1.2.0): state-changing account switches have been
    removed — the class of bug this refactor exists to close off.
    Routing is now per-call via site_url auto-resolution or explicit
    ``account_alias`` argument on each tool.

    Returns an ``ok: false`` envelope so callers that haven't updated
    notice. The alias argument is still validated (so the error can
    distinguish "unknown alias" from "stop calling this tool"), but no
    server state is mutated.

    Args:
        alias: Unused; kept for call-signature compatibility with v1.1.x.
    """
    print(
        f"[gsc-mcp] DEPRECATED call: gsc_switch_account({alias!r}). "
        f"v1.2.0 routes per-call via site_url / account_alias.",
        file=sys.stderr, flush=True,
    )
    # Still validate the alias so a typo surfaces distinctly from the
    # generic deprecation message.
    try:
        alias = _validate_alias(alias)
    except ValueError as e:
        return _make_error_envelope(
            error=f"gsc_switch_account is deprecated, AND the alias is invalid: {e}",
            error_code=ErrorCode.DEPRECATED_TOOL,
            retryable=False,
            hint="Stop calling gsc_switch_account. Pass account_alias on each tool call.",
            tool="gsc_switch_account",
        )
    manifest = _load_manifest()
    known_aliases = sorted(manifest.get("accounts", {}).keys())
    extras = {
        "replacement": {
            # ``suggested_tool`` rather than ``tool`` — see note on
            # gsc_get_active_account's deprecation envelope.
            "suggested_tool": "<any site_url-taking tool>",
            "example": "gsc_get_performance_overview(site_url=..., account_alias='chaser')",
        },
    }
    if alias not in known_aliases:
        extras["alternatives"] = known_aliases

    return _make_error_envelope(
        error=(
            f"gsc_switch_account is deprecated and no longer changes server state "
            f"(called with alias={alias!r})."
        ),
        error_code=ErrorCode.DEPRECATED_TOOL,
        retryable=False,
        hint=(
            "Pass account_alias on each site_url tool call, or omit it and "
            "let the server auto-resolve from site_url. See gsc_whoami(site_url=...) "
            "for diagnostics."
        ),
        tool="gsc_switch_account",
        **extras,
    )


@mcp.tool()
async def gsc_remove_account(alias: str) -> str:
    """
    Removes a Google account and its stored credentials.

    v1.2.2: cleaned up vestigial active-account manipulation that
    survived the v1.2.0 refactor. Removal now deletes the manifest
    entry + token directory and nothing else — the resolver picks
    per-call from site_url, so there's no "active" concept to keep
    consistent. Any leftover ``active_account`` field in the manifest
    is dropped on the write here.

    Args:
        alias: The alias of the account to remove. Use `gsc_list_accounts`
            to see available accounts.
    """
    try:
        alias = _validate_alias(alias)
    except ValueError as e:
        return f"Invalid alias: {str(e)}"

    try:
        manifest = _load_manifest()

        if alias not in manifest.get("accounts", {}):
            available = ", ".join(sorted(manifest.get("accounts", {}).keys())) or "none"
            return f"Account '{alias}' not found. Available accounts: {available}"

        # Remove account directory on disk.
        acct_dir = os.path.join(ACCOUNTS_DIR, alias)
        if os.path.isdir(acct_dir):
            shutil.rmtree(acct_dir)

        # Remove from manifest; also strip the vestigial active_account
        # field if a pre-v1.2.2 run had re-persisted it.
        del manifest["accounts"][alias]
        manifest.pop("active_account", None)
        _save_manifest(manifest)

        # Invalidate the resolver's cache entry for this alias so a
        # subsequent auto-resolve doesn't hit stale state.
        _invalidate_property_cache(alias)

        remaining_count = len(manifest.get("accounts", {}))
        if remaining_count == 0:
            return (
                f"Account '{alias}' removed. No accounts configured; "
                f"run gsc_add_account to add one."
            )
        return f"Account '{alias}' removed. {remaining_count} account(s) remaining."
    except Exception as e:
        return _format_error(
            _make_error_envelope(
                error=f"{type(e).__name__}: {e}",
                hint="The account directory or manifest may be locked; check file "
                     "permissions on accounts/ and retry.",
                tool="gsc_remove_account",
            ),
            response_format="markdown",
        )


# --- Per-site config (v1.5.0) ---
# $GSC_STATE_DIR/site-config.json, keyed by site_url. Untracked on purpose:
# brand terms and URL lists are client data, and origin is a public fork.
#
#   {"sc-domain:example.com": {
#       "brand_terms": ["example"],            # RE2 regexes, OR-ed together
#       "login_terms": ["^(example login)$"],
#       "ctr_curve": {"1": 0.28, "2": 0.15},    # optional; else the site's own
#       "exclude_urls": ["https://example.com/refreshed"]}}
_SITE_CONFIG_FILE = "site-config.json"


def _site_config_path() -> str:
    return os.path.join(GSC_STATE_DIR, _SITE_CONFIG_FILE)


def _load_site_config(site_url: str) -> Dict[str, Any]:
    try:
        with open(_site_config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}
    entry = data.get(site_url) if isinstance(data, dict) else None
    return entry if isinstance(entry, dict) else {}


def _terms_regex(terms: Any, *, field: str) -> Optional[str]:
    """OR a list of RE2 regexes into one expression (validated)."""
    if not terms:
        return None
    if isinstance(terms, str):
        terms = [terms]
    parts = []
    for t in terms:
        t = str(t)
        if _SA_LOOKAROUND_RE.search(t):
            raise _SaValidationError(f"{field} term {t!r} uses lookaround, which RE2 does not support.", "")
        try:
            re.compile(t)
        except re.error as e:
            raise _SaValidationError(f"Invalid {field} regex {t!r}: {e}.", "")
        parts.append(t)
    return parts[0] if len(parts) == 1 else "|".join(f"(?:{p})" for p in parts)


# --- Pure analysis helpers (v1.5.0) ---

_LANG_STOPWORDS: Dict[str, frozenset] = {
    "es": frozenset("el la los las del que por para con una unos como qué cómo cuentas incobrables cobrar "
                    "deudor deudores acreedor acreedores factura facturas pago pagos cobro cobranza es son".split()),
    "fr": frozenset("le les des du et pour avec une est sont comment facture factures paiement "
                    "créance créances recouvrement".split()),
    "de": frozenset("der die das und für mit ein eine ist wie rechnung rechnungen zahlung forderung "
                    "forderungen mahnung".split()),
    "pt": frozenset("do da os as com uma não como fatura faturas pagamento cobrança contas receber".split()),
    "it": frozenset("il lo gli di per con una che come fattura fatture pagamento crediti recupero".split()),
    "nl": frozenset("het een van voor met en hoe factuur facturen betaling debiteuren incasso".split()),
}
_EN_STOPWORDS = frozenset(
    "the of and to for with how what is in a an vs best free template software tool tools accounts "
    "receivable payable invoice invoices payment payments credit control debt collection".split()
)
_DIACRITIC_LANG = [
    ("es", re.compile(r"[ñ¿¡]")),
    ("pt", re.compile(r"[ãõ]")),
    ("de", re.compile(r"[äöüß]")),
    ("fr", re.compile(r"[àèùâêîôûëïœç]")),
]
_NON_LATIN_RE = re.compile(r"[Ͱ-ϿЀ-ӿ֐-ۿऀ-ॿ぀-ヿ一-鿿가-힯]")
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _guess_language(query: str) -> Tuple[str, str]:
    """A lightweight language guess for one query: ``(lang, confidence)``.
    ``lang`` is ``en``, ``es``/``fr``/``de``/``pt``/``it``/``nl``,
    ``other`` (non-Latin script) or ``unknown``; confidence is ``low`` or
    ``medium``. A flag for a human to check, never an assertion."""
    q = (query or "").lower()
    if _NON_LATIN_RE.search(q):
        return "other", "medium"
    # URL / operator tokens ("site:example.com") are not words in any language.
    q = " ".join(t for t in q.split() if not re.search(r"[.:/@]", t))
    words = _WORD_RE.findall(q)
    scores = {lang: sum(w in stops for w in words) for lang, stops in _LANG_STOPWORDS.items()}
    for lang, rx in _DIACRITIC_LANG:
        if rx.search(q):
            scores[lang] += 2
    en = sum(w in _EN_STOPWORDS for w in words)
    best, score = max(scores.items(), key=lambda kv: kv[1])
    if score > en and score >= 1:
        return best, "medium" if score >= 2 else "low"
    if en or words:
        return "en", "low" if not en else "medium"
    return "unknown", "low"


_OPERATOR_RE = re.compile(r"(site:|inurl:|intitle:|filetype:|[+%]site\.)", re.IGNORECASE)
_QUESTION_START_RE = re.compile(r"^(how|what|why|which|when|where|who|can|should|is|are|do|does|explain|write|give|list)\b")


def _machine_query_flags(
    rows: List[Dict[str, Any]],
    page_impressions: Optional[float] = None,
    daily: Optional[Dict[str, List[float]]] = None,
) -> Dict[str, List[str]]:
    """Flag queries that look machine-made or anomalous (§3.5). Returns
    ``{query: [flag, ...]}`` for flagged queries only. Flags:
    ``dominant_zero_click`` (>50% of the page's impressions at 0 clicks),
    ``operator_string`` (site:, +site., %site., inurl:, …),
    ``assistant_style`` (long prompt-like query — an AEO signal, not noise),
    ``spike`` (one day > 10× the query's typical day; needs ``daily``)."""
    total = page_impressions or sum(r.get("impressions", 0) or 0 for r in rows) or 0
    flags: Dict[str, List[str]] = {}
    for r in rows:
        q = str(r.get("query", ""))
        f: List[str] = []
        if total and (r.get("impressions") or 0) > 0.5 * total and not r.get("clicks"):
            f.append("dominant_zero_click")
        if _OPERATOR_RE.search(q):
            f.append("operator_string")
        words = q.split()
        if len(words) >= 10 or len(q) >= 80 or (len(words) >= 7 and _QUESTION_START_RE.match(q.lower())):
            f.append("assistant_style")
        series = [v for v in (daily or {}).get(q) or [] if v > 0]
        if len(series) >= 2:
            peak = max(series)
            rest = sorted(series)
            rest.remove(peak)
            typical = rest[len(rest) // 2]  # median of the other non-zero days
            if peak >= 50 and peak > 10 * typical:
                f.append("spike")
        if f:
            flags[q] = f
    return flags


_POSITION_BUCKETS = (("1-3", 1, 3.5), ("4-10", 3.5, 10.5), ("11-20", 10.5, 20.5), ("21+", 20.5, float("inf")))


def _position_buckets(rows: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """Share of impressions by average position band."""
    total = sum(r.get("impressions", 0) or 0 for r in rows)
    out: Dict[str, Optional[float]] = {}
    for name, lo, hi in _POSITION_BUCKETS:
        imp = sum(r.get("impressions", 0) or 0 for r in rows if lo <= (r.get("position") or 0) < hi)
        out[name] = (imp / total) if total else None
    return out


def _site_ctr_curve(rows: List[Dict[str, Any]], *, min_impressions: int = 10) -> Dict[int, float]:
    """The site's own expected CTR by rounded position: the median CTR of
    rows at that position (rows under ``min_impressions`` ignored)."""
    by_pos: Dict[int, List[float]] = {}
    for r in rows:
        if (r.get("impressions") or 0) < min_impressions:
            continue
        pos = max(1, int(round(r.get("position") or 0)))
        by_pos.setdefault(min(pos, 50), []).append(float(r.get("ctr") or 0))
    curve = {}
    for pos, ctrs in by_pos.items():
        ctrs.sort()
        mid = len(ctrs) // 2
        curve[pos] = ctrs[mid] if len(ctrs) % 2 else (ctrs[mid - 1] + ctrs[mid]) / 2
    return curve


def _expected_ctr(position: float, curve: Dict[int, float]) -> Optional[float]:
    """Expected CTR at ``position`` from ``curve`` (nearest known position)."""
    if not curve:
        return None
    pos = max(1, int(round(position or 0)))
    if pos in curve:
        return curve[pos]
    nearest = min(curve, key=lambda p: (abs(p - pos), p))
    return curve[nearest]


def _date_bucket(day: str, granularity: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d").date()
    if granularity == "day":
        return day
    if granularity == "week":
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return day[:7]


def _url_regex(urls: List[str]) -> str:
    return "^(" + "|".join(re.escape(u) for u in urls) + ")$"


def _chunks(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# --- Analysis tools (v1.5.0) ---


@mcp.tool()
async def gsc_page_query_profile(
    site_url: str,
    page_url: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    *,
    days: int = 28,
    data_state: str = "final",
    brand_terms: Optional[List[str]] = None,
    login_terms: Optional[List[str]] = None,
    max_rows: int = _SA_DEFAULT_MAX_ROWS,
    save_to_file: Optional[str] = None,
    account_alias: Optional[str] = None,
) -> Any:
    """Everything a page brief needs about one page's queries, in one call:
    page total, every query sorted by impressions, the unattributed
    (anonymised) share, non-English share (a per-query language guess —
    a flag, not an assertion), brand and login share, machine-query flags
    (dominant zero-click query, operator strings like site:/+site., long
    assistant-style prompts, 10× spikes) and the position distribution.

    Args:
        site_url: GSC property. page_url: the exact page URL.
        start_date / end_date: YYYY-MM-DD (default: the last `days` final days).
        days: window length when no dates are given (default 28).
        data_state: final (default) | all.
        brand_terms / login_terms: RE2 regexes; default from site config.
        max_rows: cap on query rows fetched.
        save_to_file: absolute .csv/.json path for the query rows.
    """
    tool = "gsc_page_query_profile"
    try:
        cfg = _load_site_config(site_url)
        # An explicit [] disables a classification; None means "use config".
        brand_re = _terms_regex(cfg.get("brand_terms") if brand_terms is None else brand_terms, field="brand")
        login_re = _terms_regex(cfg.get("login_terms") if login_terms is None else login_terms, field="login")
        page_filter = [{"filters": [{"dimension": "page", "operator": "equals", "expression": page_url}]}]
        async with _instrument(tool, site_url=site_url, page_url=page_url):
            ctx = await _sa_context(site_url, account_alias)
            res = await _sa_run(
                ctx, dimensions=["query"], start_date=start_date, end_date=end_date, days=days,
                data_state=data_state, filter_groups=page_filter, fetch_all=True, max_rows=max_rows,
                sort_by="impressions", include_totals=True, step=tool,
            )
            daily_res = await _sa_run(
                ctx, dimensions=["query", "date"], window=res["window"], data_state=data_state,
                filter_groups=page_filter, fetch_all=True, max_rows=max_rows, step=f"{tool}.daily",
            )
        daily: Dict[str, List[float]] = {}
        for r in daily_res["rows"]:
            daily.setdefault(r["query"], []).append(float(r.get("impressions") or 0))

        rows = res["rows"]
        page_total = (res["totals"] or {}).get("page_total") or {"clicks": 0, "impressions": 0}
        flags = _machine_query_flags(rows, page_total.get("impressions"), daily)
        brand_rx = re.compile(brand_re) if brand_re else None
        login_rx = re.compile(login_re) if login_re else None
        queries = []
        for r in rows:
            lang, conf = _guess_language(r["query"])
            queries.append(dict(
                r, lang_guess=lang, lang_confidence=conf,
                brand=bool(brand_rx and brand_rx.search(r["query"])),
                login=bool(login_rx and login_rx.search(r["query"])),
                flags=flags.get(r["query"], []),
            ))

        def _share(pred) -> Dict[str, Optional[float]]:
            out = {}
            for m in ("clicks", "impressions"):
                part = sum(q.get(m) or 0 for q in queries if pred(q))
                out[f"of_query_rows_{m}"] = part / (res["totals"]["query_rows_sum"][m] or 0) if res["totals"]["query_rows_sum"][m] else None
                out[f"of_page_total_{m}"] = part / page_total[m] if page_total.get(m) else None
            return out

        summary = {
            "page_total": page_total,
            "query_rows_sum": res["totals"]["query_rows_sum"],
            "unattributed_share": res["totals"]["unattributed_share"],
            "non_english_share": _share(lambda q: q["lang_guess"] not in ("en", "unknown")),
            "non_english_languages": sorted({q["lang_guess"] for q in queries if q["lang_guess"] not in ("en", "unknown")}),
            "brand_share": _share(lambda q: q["brand"]) if brand_rx else None,
            "login_share": _share(lambda q: q["login"]) if login_rx else None,
            "position_distribution": _position_buckets(rows),
            "flag_counts": {
                name: sum(1 for q in queries if name in q["flags"])
                for name in ("dominant_zero_click", "operator_string", "assistant_style", "spike")
            },
        }
        meta = _sa_meta(res, site_url=site_url, page_url=page_url,
                        spike_evidence="complete" if daily_res["complete"] else "partial (max_rows cap)",
                        warnings=list(daily_res["warnings"]) + ([] if daily_res["complete"] else [
                            "Daily query evidence hit max_rows; spike flags may be missing."]),
                        brand_regex=brand_re, login_regex=login_re,
                        language_note="lang_guess is a stopword/diacritic heuristic: flag, don't assert")
        if not brand_rx:
            meta["warnings"].append("No brand_terms given or configured; brand_share not computed.")
        if save_to_file:
            columns = _sa_columns(["query"]) + [
                {"key": k, "display": k, "type": "str"}
                for k in ("lang_guess", "lang_confidence", "brand", "login", "flags")
            ]
            err = await asyncio.to_thread(_save_rows, save_to_file, queries, [c["key"] for c in columns], meta)
            if err:
                raise _SaValidationError(err, "Pass an absolute .csv or .json path.")
            meta["saved_to"] = save_to_file
            meta["saved_row_count"] = len(queries)
        return {
            "ok": True, "tool": tool, "site_url": site_url, "page_url": page_url,
            "summary": summary,
            # After saving, return only the first 20 rows (the file has all).
            "queries": queries[:_SAVE_PREVIEW_ROWS] if save_to_file else queries,
            "meta": meta,
        }
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, site_url=site_url)


@mcp.tool()
async def gsc_brand_split(
    site_url: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    granularity: str = "month",
    *,
    days: int = 28,
    data_state: str = "final",
    brand_terms: Optional[List[str]] = None,
    login_terms: Optional[List[str]] = None,
    account_alias: Optional[str] = None,
) -> Any:
    """Branded vs non-branded (and login) clicks and impressions by month,
    week or day. The split runs in the API (includingRegex / excludingRegex
    on query), not by guessing client-side. Shares are of the property
    total, which also counts anonymised queries; `anonymised` is what
    neither the branded nor the non-branded pull can see.

    Args:
        site_url: GSC property.
        start_date / end_date: YYYY-MM-DD (default: the last `days` final days).
        granularity: month (default) | week (ISO, Monday start) | day.
        brand_terms / login_terms: RE2 regexes (OR-ed); default from site config.
    """
    tool = "gsc_brand_split"
    try:
        gran = str(granularity).strip().lower()
        if gran not in ("month", "week", "day"):
            raise _SaValidationError(f"Unknown granularity {granularity!r}.", "Use month, week or day.")
        cfg = _load_site_config(site_url)
        brand_re = _terms_regex(cfg.get("brand_terms") if brand_terms is None else brand_terms, field="brand")
        login_re = _terms_regex(cfg.get("login_terms") if login_terms is None else login_terms, field="login")
        if not brand_re:
            raise _SaValidationError(
                "No brand terms: pass brand_terms or add them to the site config.",
                f"Site config lives at $GSC_STATE_DIR/{_SITE_CONFIG_FILE}, keyed by site_url.",
            )
        ctx = await _sa_context(site_url, account_alias)
        w = await _resolve_window(ctx, start_date=start_date, end_date=end_date, days=days, data_state=data_state)
        n = w["window_days"] + 1

        async def _daily(expr: Optional[str], op: str, step: str) -> Dict[str, Dict[str, float]]:
            groups = [{"filters": [{"dimension": "query", "operator": op, "expression": expr}]}] if expr else None
            r = await _sa_run(ctx, dimensions=["date"], window=w, data_state=data_state,
                              filter_groups=groups, row_limit=n, step=step)
            return {row["date"]: row for row in r["rows"]}

        total = await _daily(None, "", f"{tool}.total")
        branded = await _daily(brand_re, "includingRegex", f"{tool}.branded")
        non_branded = await _daily(brand_re, "excludingRegex", f"{tool}.non_branded")
        login = await _daily(login_re, "includingRegex", f"{tool}.login") if login_re else {}

        buckets: Dict[str, Dict[str, Any]] = {}
        for day in sorted(set(total) | set(branded) | set(non_branded) | set(login)):
            b = buckets.setdefault(_date_bucket(day, gran), {
                "period": _date_bucket(day, gran), "days_of_data": 0,
                **{f"{k}_{m}": 0 for k in ("total", "branded", "non_branded", "login") for m in ("clicks", "impressions")},
            })
            if day in total:
                b["days_of_data"] += 1
            for key, src in (("total", total), ("branded", branded), ("non_branded", non_branded), ("login", login)):
                row = src.get(day)
                if row:
                    b[f"{key}_clicks"] += row.get("clicks") or 0
                    b[f"{key}_impressions"] += row.get("impressions") or 0
        rows = []
        for b in buckets.values():
            for m in ("clicks", "impressions"):
                t = b[f"total_{m}"]
                b[f"anonymised_{m}"] = t - b[f"branded_{m}"] - b[f"non_branded_{m}"]
                b[f"brand_share_{m}"] = b[f"branded_{m}"] / t if t else None
                b[f"login_share_{m}"] = b[f"login_{m}"] / t if (t and login_re) else None
            rows.append(b)
        columns = [{"key": "period", "display": "Period", "type": "str"},
                   {"key": "days_of_data", "display": "Days", "type": "int"}]
        for k in ("total", "branded", "non_branded", "login", "anonymised"):
            columns += [{"key": f"{k}_clicks", "display": f"{k} clicks", "type": "int"},
                        {"key": f"{k}_impressions", "display": f"{k} impr", "type": "int"}]
        columns += [{"key": "brand_share_clicks", "display": "brand share (clicks)", "type": "pct"},
                    {"key": "login_share_clicks", "display": "login share (clicks)", "type": "pct"}]
        out = _format_table(
            rows, columns, response_format="json",
            meta=_standard_meta(w, site_url=site_url, granularity=gran, brand_regex=brand_re,
                                login_regex=login_re, row_count=len(rows), truncated=False,
                                coverage=_coverage(len(total), len(total), "days with data (all fetched)"),
                                brand_terms_source="site_config" if brand_terms is None else "argument"),
        )
        out["tool"] = tool
        return out
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, site_url=site_url)


@mcp.tool()
async def gsc_striking_distance(
    site_url: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    min_impressions: int = 100,
    pos_from: float = 4,
    pos_to: float = 20,
    ctr_below_expected: bool = True,
    *,
    days: int = 28,
    exclude_urls: Optional[List[str]] = None,
    top_queries: int = 5,
    limit: int = 50,
    data_state: str = "final",
    save_to_file: Optional[str] = None,
    account_alias: Optional[str] = None,
) -> Any:
    """Pages ranking within reach of the top (average position pos_from–
    pos_to) with enough impressions, optionally only those whose CTR is
    below what their position should earn. Each candidate carries position,
    impressions, clicks, CTR, expected CTR, the gap, the click upside and
    its top queries (with machine-query flags). Ordered by click upside.

    Expected CTR comes from `ctr_curve` in the site config when present,
    else from the site's own median CTR at each position (meta says which).

    Args:
        exclude_urls: pages to skip (e.g. just refreshed); merged with the
            site config's exclude_urls.
        top_queries: queries shown per page (default 5).
        limit: max candidates (default 50).
        save_to_file: absolute .json path; writes the full result, returns 20 candidates.
    """
    tool = "gsc_striking_distance"
    try:
        cfg = _load_site_config(site_url)
        excluded = set(exclude_urls or []) | set(cfg.get("exclude_urls") or [])
        ctx = await _sa_context(site_url, account_alias)
        res = await _sa_run(ctx, dimensions=["page"], start_date=start_date, end_date=end_date,
                            days=days, data_state=data_state, fetch_all=True, step=tool)
        if cfg.get("ctr_curve"):
            curve = {int(k): float(v) for k, v in cfg["ctr_curve"].items()}
            curve_source = "site_config"
        else:
            curve = _site_ctr_curve(res["rows"])
            curve_source = "site_median_by_position"
        candidates = []
        for r in res["rows"]:
            if r["page"] in excluded or (r.get("impressions") or 0) < min_impressions:
                continue
            if not (pos_from <= (r.get("position") or 0) <= pos_to):
                continue
            expected = _expected_ctr(r["position"], curve)
            gap = (expected - r["ctr"]) if expected is not None else None
            if ctr_below_expected and (gap is None or gap <= 0):
                continue
            candidates.append(dict(
                r, expected_ctr=expected, ctr_gap=gap,
                click_upside=(gap * r["impressions"]) if gap is not None else None,
            ))
        candidates.sort(key=lambda c: (-(c["click_upside"] or 0), c["page"]))
        qualified = len(candidates)
        candidates = candidates[:max(1, int(limit))]

        by_page: Dict[str, List[Dict[str, Any]]] = {c["page"]: [] for c in candidates}
        daily: Dict[str, Dict[str, List[float]]] = {}
        daily_complete = True
        daily_rows = 0
        for chunk in _chunks(list(by_page), 20):
            page_filter = [{"filters": [{"dimension": "page", "operator": "includingRegex",
                                         "expression": _url_regex(chunk)}]}]
            q = await _sa_run(
                ctx, dimensions=["page", "query"], window=res["window"], data_state=data_state,
                filter_groups=page_filter, fetch_all=True, sort_by="impressions", step=f"{tool}.queries",
            )
            for row in q["rows"]:
                by_page.setdefault(row["page"], []).append(row)
            dq = await _sa_run(
                ctx, dimensions=["page", "query", "date"], window=res["window"], data_state=data_state,
                filter_groups=page_filter, fetch_all=True, step=f"{tool}.daily",
            )
            daily_complete = daily_complete and dq["complete"]
            daily_rows += len(dq["rows"])
            for row in dq["rows"]:
                daily.setdefault(row["page"], {}).setdefault(row["query"], []).append(float(row.get("impressions") or 0))
        for c in candidates:
            qrows = by_page.get(c["page"], [])
            flags = _machine_query_flags(qrows, c["impressions"], daily.get(c["page"]))
            c["top_queries"] = [
                {"query": q["query"], "clicks": q["clicks"], "impressions": q["impressions"],
                 "position": q["position"], "flags": flags.get(q["query"], [])}
                for q in qrows[:max(0, int(top_queries))]
            ]
            c["query_flags"] = sorted({f for fl in flags.values() for f in fl})
        out = {
            "ok": True, "tool": tool, "site_url": site_url,
            "candidates": candidates,
            "ctr_curve": {str(k): v for k, v in sorted(curve.items())},
            "meta": _sa_meta(res, site_url=site_url, ctr_curve_source=curve_source,
                             thresholds={"min_impressions": min_impressions, "pos_from": pos_from,
                                         "pos_to": pos_to, "ctr_below_expected": ctr_below_expected},
                             excluded_urls=sorted(excluded), row_count=len(candidates),
                             spike_evidence="complete" if daily_complete else "partial (max_rows cap)",
                             coverage=_coverage_rollup({
                                 "page_rows": res["coverage"],
                                 "candidates_shown": _coverage(len(candidates), qualified, "qualifying pages"),
                                 "daily_spike_evidence": _coverage(daily_rows, daily_rows if daily_complete else None,
                                                                   "daily query rows"),
                             })),
        }
        return await _save_and_trim(out, save_to_file, ("candidates",)) if save_to_file else out
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, site_url=site_url)


@mcp.tool()
async def gsc_movers(
    site_url: str,
    period_a_start: Optional[str] = None,
    period_a_end: Optional[str] = None,
    period_b_start: Optional[str] = None,
    period_b_end: Optional[str] = None,
    dimension: str = "page",
    min_impressions: int = 0,
    top_n: int = 20,
    rank_by: str = "clicks",
    *,
    days: Optional[int] = None,
    data_state: str = "final",
    max_rows: int = _SA_DEFAULT_MAX_ROWS,
    save_to_file: Optional[str] = None,
    account_alias: Optional[str] = None,
) -> Any:
    """Biggest gainers and losers between an earlier period A and a later
    period B (B − A), by page or query: clicks, impressions and position
    deltas, and each row's share of the total change. Both periods are
    fetched in full; a row missing from a capped period is null, not 0.

    Args:
        period_a_* / period_b_*: YYYY-MM-DD, or pass days=N instead (B = the
            last N final days, A = the N days before).
        dimension: page (default) | query.
        min_impressions: keep rows where either period reaches this.
        top_n: gainers and losers returned each (default 20).
        rank_by: clicks (default) | impressions | position (position ranks
            improvement, i.e. a falling average position, as a gain).
        save_to_file: absolute .json path; writes every compared row too.
    """
    tool = "gsc_movers"
    try:
        dim = _norm_enum(dimension, {"page": "page", "query": "query"}, field="dimension")
        metric = _norm_enum(rank_by, {"clicks": "clicks", "impressions": "impressions", "position": "position"},
                            field="rank_by")
        use_days = _comparison_mode([period_a_start, period_a_end, period_b_start, period_b_end], days)
        ctx = await _sa_context(site_url, account_alias)
        if use_days:
            wa, wb = await _resolve_comparison_windows(ctx, days=days, data_state=data_state)
        else:
            wa = await _resolve_window(ctx, start_date=period_a_start, end_date=period_a_end, data_state=data_state)
            wb = await _resolve_window(ctx, start_date=period_b_start, end_date=period_b_end, data_state=data_state)
        a = await _sa_run(ctx, dimensions=[dim], window=wa, data_state=data_state, fetch_all=True,
                          max_rows=max_rows, step=f"{tool}.a")
        b = await _sa_run(ctx, dimensions=[dim], window=wb, data_state=data_state, fetch_all=True,
                          max_rows=max_rows, step=f"{tool}.b")
        a_by = {r[dim]: r for r in a["rows"]}
        b_by = {r[dim]: r for r in b["rows"]}

        def _val(row, key, complete):
            if row is None:
                return 0 if (complete and key != "position") else None
            return row.get(key)

        rows = []
        for key in set(a_by) | set(b_by):
            ra, rb = a_by.get(key), b_by.get(key)
            if max((ra or {}).get("impressions", 0) or 0, (rb or {}).get("impressions", 0) or 0) < min_impressions:
                continue
            row: Dict[str, Any] = {dim: key}
            for m in ("clicks", "impressions", "position"):
                va, vb = _val(ra, m, a["complete"]), _val(rb, m, b["complete"])
                row[f"a_{m}"], row[f"b_{m}"] = va, vb
                row[f"{m}_delta"] = (vb - va) if (va is not None and vb is not None) else None
            rows.append(row)
        totals = {
            "a": {m: sum(r.get(m) or 0 for r in a["rows"]) for m in ("clicks", "impressions")},
            "b": {m: sum(r.get(m) or 0 for r in b["rows"]) for m in ("clicks", "impressions")},
        }
        # Net change over every fetched row (not just those passing
        # min_impressions); exact only when both periods were fetched in full.
        net = {m: totals["b"][m] - totals["a"][m] for m in ("clicks", "impressions")}
        net_complete = a["complete"] and b["complete"]
        for r in rows:
            for m in ("clicks", "impressions"):
                r[f"share_of_{m}_change"] = (r[f"{m}_delta"] / net[m]) if (net[m] and r[f"{m}_delta"] is not None) else None

        sign = -1 if metric == "position" else 1  # a falling position is a gain
        scored = [r for r in rows if r[f"{metric}_delta"] is not None]
        scored.sort(key=lambda r: (-(sign * r[f"{metric}_delta"]), str(r[dim])))
        gainers = [r for r in scored if sign * r[f"{metric}_delta"] > 0][:top_n]
        losers = [r for r in reversed(scored) if sign * r[f"{metric}_delta"] < 0][:top_n]
        out = {
            "ok": True, "tool": tool, "site_url": site_url, "dimension": dim, "rank_by": metric,
            "gainers": gainers, "losers": losers,
            "net_change": net,
            "net_change_complete": net_complete,
            "totals": totals,
            "unknown_rows": sum(1 for r in rows if r["clicks_delta"] is None),
            "meta": _standard_meta(
                None, site_url=site_url, period_a=_window_meta(wa), period_b=_window_meta(wb),
                coverage={"a": "complete" if a["complete"] else "truncated",
                          "b": "complete" if b["complete"] else "truncated",
                          **_coverage_rollup({
                              "period_a_rows": a["coverage"], "period_b_rows": b["coverage"],
                              "gainers_shown": _coverage(len(gainers), sum(1 for r in scored if sign * r[f"{metric}_delta"] > 0), "gainers"),
                              "losers_shown": _coverage(len(losers), sum(1 for r in scored if sign * r[f"{metric}_delta"] < 0), "losers"),
                          })},
                data_state=data_state, latest_final_date=wb["latest_final_date"], timezone=_PT_LABEL,
                row_count=len(gainers) + len(losers), truncated=False,
                warnings=list(wa["warnings"]) + list(wb["warnings"]),
            ),
        }
        if save_to_file:
            out["all_rows"] = rows  # the file gets every compared row
            return await _save_and_trim(out, save_to_file, ("gainers", "losers", "all_rows"))
        return out
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, site_url=site_url)


@mcp.tool()
async def gsc_cannibalisation(
    site_url: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    query_regex: Optional[str] = None,
    page_regex: Optional[str] = None,
    min_pages: int = 2,
    *,
    days: int = 28,
    min_impressions: int = 10,
    limit: int = 50,
    collapse_fragments: bool = True,
    data_state: str = "final",
    max_rows: int = _SA_DEFAULT_MAX_ROWS,
    save_to_file: Optional[str] = None,
    account_alias: Optional[str] = None,
) -> Any:
    """Queries where two or more of the site's URLs earned impressions, with
    each URL's position, impressions, clicks and share of the query's
    impressions. Ordered by the query's total impressions.

    Args:
        query_regex / page_regex: optional RE2 filters (e.g. "\\\\bsage\\\\b").
        min_pages: URLs a query must reach to count (default 2).
        min_impressions: per-URL impressions to count as competing (default 10).
        limit: max queries returned (default 50).
        collapse_fragments: merge jump-link URLs (page#section) into their
            page first (default true) — they are one page, not competitors.
        save_to_file: absolute .json path; writes every cannibalised query.
    """
    tool = "gsc_cannibalisation"
    try:
        filters = []
        if query_regex:
            filters.append({"dimension": "query", "operator": "includingRegex", "expression": query_regex})
        if page_regex:
            filters.append({"dimension": "page", "operator": "includingRegex", "expression": page_regex})
        ctx = await _sa_context(site_url, account_alias)
        res = await _sa_run(ctx, dimensions=["query", "page"], start_date=start_date, end_date=end_date,
                            days=days, data_state=data_state,
                            filter_groups=[{"filters": filters}] if filters else None,
                            fetch_all=True, max_rows=max_rows, step=tool)
        merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for r in res["rows"]:
            page = r["page"].split("#", 1)[0] if collapse_fragments else r["page"]
            m = merged.setdefault((r["query"], page), {"query": r["query"], "page": page, "clicks": 0,
                                                        "impressions": 0, "_posw": 0.0})
            m["clicks"] += r.get("clicks") or 0
            m["impressions"] += r.get("impressions") or 0
            m["_posw"] += (r.get("position") or 0) * (r.get("impressions") or 0)
        by_query: Dict[str, List[Dict[str, Any]]] = {}
        for m in merged.values():
            m["position"] = m.pop("_posw") / m["impressions"] if m["impressions"] else None
            if m["impressions"] >= min_impressions:
                by_query.setdefault(m["query"], []).append(m)
        groups = []
        for q, pages in by_query.items():
            if len(pages) < max(2, int(min_pages)):
                continue
            total_imp = sum(p["impressions"] for p in pages)
            pages.sort(key=lambda p: (-p["impressions"], p["page"]))
            groups.append({
                "query": q,
                "pages": [{"page": p["page"], "position": p["position"], "impressions": p["impressions"],
                           "clicks": p["clicks"], "impression_share": p["impressions"] / total_imp if total_imp else None}
                          for p in pages],
                "page_count": len(pages),
                "total_impressions": total_imp,
                "total_clicks": sum(p["clicks"] for p in pages),
            })
        groups.sort(key=lambda g: (-g["total_impressions"], g["query"]))
        out = {
            "ok": True, "tool": tool, "site_url": site_url,
            "queries": groups if save_to_file else groups[:max(1, int(limit))],
            "total_cannibalised_queries": len(groups),
            "meta": _sa_meta(res, site_url=site_url, query_regex=query_regex, page_regex=page_regex,
                             min_pages=min_pages, min_impressions=min_impressions,
                             row_count=min(len(groups), max(1, int(limit))),
                             coverage=_coverage_rollup({
                                 "query_page_rows": res["coverage"],
                                 "queries_shown": _coverage(len(groups) if save_to_file else min(len(groups), max(1, int(limit))),
                                                            len(groups), "cannibalised queries"),
                             })),
        }
        return await _save_and_trim(out, save_to_file, ("queries",)) if save_to_file else out
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, site_url=site_url)


# --- Safe live fetcher (v1.5.0) ---
# Used by gsc_sitemap_diff and gsc_recrawl_worklist to check what a URL
# really returns. Guards: http(s) only; the host must belong to the GSC
# property (or be the sitemap's host); every resolved address and the
# connected peer must be public (no loopback / private / link-local — the
# peer check closes the DNS-rebinding gap); redirects are never followed,
# only reported; bodies are capped. A browser user agent and
# Accept: text/html get past bot-shy CDNs (e.g. Cloudflare), and callers
# make a control request first so a blocked crawler reads as
# "inconclusive", not as a dead site.
_FETCH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
_FETCH_HEADERS = {
    "User-Agent": _FETCH_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
_FETCH_MAX_BYTES = 10 * 1024 * 1024
_CHALLENGE_RE = re.compile(rb"Checking browser|Just a moment|challenge-platform|cf-chl|Attention Required", re.IGNORECASE)
_FETCH_TIMEOUT_SEC = 15.0
_SITEMAP_MAX_DEPTH = 3
_SITEMAP_MAX_URLS = 50_000
_SITEMAP_MAX_FETCHES = 2_000


class _FetchRefused(Exception):
    """A URL the safe fetcher will not request (scope or address rules)."""


def _property_hosts(site_url: str) -> Tuple[Optional[str], Optional[str]]:
    """``(exact_host, domain)``: a URL-prefix property allows its own host;
    an ``sc-domain:`` property allows the domain and its subdomains."""
    if site_url.startswith("sc-domain:"):
        return None, site_url[len("sc-domain:"):].lower()
    from urllib.parse import urlsplit
    return (urlsplit(site_url).hostname or "").lower(), None


def _host_allowed(host: str, site_url: str, extra_hosts: Tuple[str, ...] = ()) -> bool:
    host = (host or "").lower()
    exact, domain = _property_hosts(site_url)
    if host in extra_hosts:
        return True
    if exact is not None:
        return host == exact
    return host == domain or host.endswith("." + domain)


def _is_public_ip(addr: str) -> bool:
    import ipaddress
    try:
        return ipaddress.ip_address(addr.split("%")[0]).is_global
    except ValueError:
        return False


async def _safe_fetch(client: Any, url: str, site_url: str, *, extra_hosts: Tuple[str, ...] = (),
                      want_body: bool = False, max_bytes: int = _FETCH_MAX_BYTES) -> Dict[str, Any]:
    """GET ``url`` without following redirects. Returns ``{url, status,
    location, body?}``; raises _FetchRefused for an out-of-scope or
    non-public destination, before any request is sent."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise _FetchRefused(f"not an http(s) URL: {url!r}")
    if not _host_allowed(parts.hostname, site_url, extra_hosts):
        raise _FetchRefused(f"host {parts.hostname!r} is outside property {site_url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    infos = await asyncio.to_thread(socket.getaddrinfo, parts.hostname, port, 0, socket.SOCK_STREAM)
    addrs = list(dict.fromkeys(i[4][0] for i in infos))  # the OS's preference order (RFC 6724)
    if not addrs or not all(_is_public_ip(a) for a in addrs):
        raise _FetchRefused(f"{parts.hostname!r} resolves to a non-public address")
    # Pin the connection to the address just validated (no second DNS
    # lookup to rebind), keeping the hostname for Host, SNI and TLS
    # certificate verification.
    pinned = addrs[0]
    ip_host = f"[{pinned}]" if ":" in pinned else pinned
    netloc = ip_host + (f":{parts.port}" if parts.port else "")
    pinned_url = parts._replace(netloc=netloc).geturl()
    host_header = parts.hostname + (f":{parts.port}" if parts.port else "")
    headers = dict(_FETCH_HEADERS, Host=host_header)
    async with client.stream("GET", pinned_url, headers=headers,
                             extensions={"sni_hostname": parts.hostname}) as resp:
        stream = resp.extensions.get("network_stream")
        peer = stream.get_extra_info("server_addr") if stream is not None else None
        if peer and not _is_public_ip(str(peer[0])):
            raise _FetchRefused(f"connected to non-public address {peer[0]!r}")
        out: Dict[str, Any] = {"url": url, "status": resp.status_code, "location": resp.headers.get("location"),
                               "retry_after": resp.headers.get("retry-after")}
        if resp.status_code in (403, 429, 503) and not want_body:
            # A CDN bot check answers with a challenge page, not the page's
            # real status: detect it so it is never reported as one.
            head = b""
            async for chunk in resp.aiter_bytes():
                head += chunk
                if len(head) >= 8192:
                    break
            if resp.headers.get("cf-mitigated") == "challenge" or _CHALLENGE_RE.search(head[:8192]):
                out["bot_challenge"] = True
        if want_body:
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise _FetchRefused(f"response over {max_bytes} bytes: {url!r}")
                chunks.append(chunk)
            out["body"] = b"".join(chunks)
            if resp.status_code in (403, 429, 503) and _CHALLENGE_RE.search(out["body"][:8192]):
                out["bot_challenge"] = True
        return out


def _new_fetch_client() -> Any:
    import httpx
    return httpx.AsyncClient(follow_redirects=False, timeout=_FETCH_TIMEOUT_SEC)


_FETCH_RATE_LIMIT_STATUSES = (429, 503)
_FETCH_MAX_ATTEMPTS = 4
_FETCH_DEFAULT_CONCURRENCY = 3


_FETCH_PACING_SEC = 0.3  # per worker, between requests: a burst trips CDN bot checks


async def _fetch_with_backoff(client: Any, url: str, site_url: str, **kw: Any) -> Dict[str, Any]:
    """_safe_fetch, retrying a 429/503 with backoff (Retry-After honoured
    up to 10 s). A persistent one comes back with ``rate_limited: True``."""
    for attempt in range(1, _FETCH_MAX_ATTEMPTS + 1):
        r = await _safe_fetch(client, url, site_url, **kw)
        if r.get("bot_challenge"):
            return dict(r, rate_limited=True)  # a challenge won't clear in seconds
        if r.get("status") not in _FETCH_RATE_LIMIT_STATUSES:
            return r
        if attempt == _FETCH_MAX_ATTEMPTS:
            return dict(r, rate_limited=True)
        try:
            wait = float(r.get("retry_after") or 0)
        except ValueError:
            wait = 0.0
        await _async_retry_sleep(min(_RETRY_AFTER_MAX_WAIT, max(wait, 2.0 * 2 ** (attempt - 1))))
    return r


async def _fetch_statuses(client: Any, urls: List[str], site_url: str, *, extra_hosts: Tuple[str, ...] = (),
                          concurrency: int = _FETCH_DEFAULT_CONCURRENCY) -> Dict[str, Dict[str, Any]]:
    """Live status of each URL. A 429/503 (a CDN rate-limiting us) is backed
    off and retried — honouring Retry-After up to 10 s — and, if it persists,
    marked ``rate_limited``: the page was not checked, it is not broken."""
    sem = asyncio.Semaphore(max(1, concurrency))
    out: Dict[str, Dict[str, Any]] = {}

    async def one(u: str) -> None:
        async with sem:
            try:
                out[u] = await _fetch_with_backoff(client, u, site_url, extra_hosts=extra_hosts)
            except _FetchRefused as e:
                out[u] = {"url": u, "status": None, "refused": str(e)}
            except Exception as e:  # noqa: BLE001 — network failure is data
                out[u] = {"url": u, "status": None, "error": f"{type(e).__name__}: {e}"}
            await _async_retry_sleep(_FETCH_PACING_SEC)
    await asyncio.gather(*(one(u) for u in urls))
    return out


def _site_home(site_url: str, sample_url: Optional[str] = None) -> str:
    if not site_url.startswith("sc-domain:"):
        return site_url
    from urllib.parse import urlsplit
    if sample_url:
        p = urlsplit(sample_url)
        return f"{p.scheme}://{p.hostname}/"
    return f"https://{site_url[len('sc-domain:'):]}/"


async def _control_ok(client: Any, site_url: str, sample_url: Optional[str]) -> Dict[str, Any]:
    """Control request: the site's home page must answer 200 (or a redirect
    within the property) or every live check is inconclusive."""
    home = _site_home(site_url, sample_url)
    try:
        r = await _fetch_with_backoff(client, home, site_url)
    except Exception as e:  # noqa: BLE001
        return {"url": home, "ok": False, "error": f"{type(e).__name__}: {e}"}
    if r["status"] in (301, 302, 303, 307, 308):
        from urllib.parse import urljoin, urlsplit
        dest = urljoin(home, r.get("location") or "")
        p = urlsplit(dest)
        ok = p.scheme in ("http", "https") and _host_allowed(p.hostname or "", site_url)
        return {"url": home, "ok": ok, "status": r["status"], "location": dest}
    return {"url": home, "ok": r["status"] == 200, "status": r["status"]}


def _normalize_url(u: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(u.strip())
    return urlunsplit((p.scheme.lower(), (p.netloc or "").lower(), p.path or "/", p.query, ""))


def _parse_sitemap_body(body: bytes) -> Tuple[str, List[str]]:
    """``(root_tag, locs)`` from one sitemap document. Gzip is decompressed
    with a bounded budget; only the <loc> directly under each <url> /
    <sitemap> counts (image:loc, video:loc and xhtml:link are not pages)."""
    import xml.etree.ElementTree as ET
    import zlib
    if body[:2] == b"\x1f\x8b":
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = d.decompress(body, _FETCH_MAX_BYTES + 1)
        if len(out) > _FETCH_MAX_BYTES or d.unconsumed_tail:
            raise _FetchRefused(f"gzipped sitemap expands past {_FETCH_MAX_BYTES} bytes")
        body = out
    root = ET.fromstring(body)
    ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
    tag = root.tag[len(ns):]
    if tag not in ("urlset", "sitemapindex"):
        raise ValueError(f"not a sitemap (root element {tag!r})")
    locs = [
        child.text.strip()
        for entry in root for child in entry
        if child.tag == ns + "loc" and child.text and child.text.strip()
    ]
    return tag, locs


async def _read_sitemap_urls(client: Any, sitemap_url: str, site_url: str) -> Dict[str, Any]:
    """Every <loc> in a sitemap, following sitemap indexes (depth ≤ 3,
    cycle-safe, ≤ 50k URLs, ≤ 2,000 fetches)."""
    from urllib.parse import urlsplit
    sm_host = (urlsplit(sitemap_url).hostname or "").lower()
    seen_maps: set = set()
    urls: List[str] = []
    errors: List[Dict[str, Any]] = []
    fetches = 0
    queue = [(sitemap_url, 0)]
    while queue:
        sm, depth = queue.pop(0)
        if sm in seen_maps:
            continue
        if depth > _SITEMAP_MAX_DEPTH:
            errors.append({"sitemap": sm, "error": f"nested deeper than {_SITEMAP_MAX_DEPTH} levels; not read"})
            continue
        seen_maps.add(sm)
        fetches += 1
        if fetches > _SITEMAP_MAX_FETCHES:
            errors.append({"sitemap": sm, "error": "fetch budget exhausted"})
            break
        try:
            r = await _fetch_with_backoff(client, sm, site_url, extra_hosts=(sm_host,), want_body=True)
        except Exception as e:  # noqa: BLE001
            errors.append({"sitemap": sm, "error": f"{type(e).__name__}: {e}"})
            continue
        if r["status"] != 200:
            errors.append({"sitemap": sm, "error": f"HTTP {r['status']}", "location": r.get("location")})
            continue
        try:
            tag, locs = await asyncio.to_thread(_parse_sitemap_body, r["body"])
        except Exception as e:  # noqa: BLE001 — a bad child sitemap is reported, not fatal
            errors.append({"sitemap": sm, "error": f"{type(e).__name__}: {e}"})
            continue
        if tag == "sitemapindex":
            queue.extend((loc, depth + 1) for loc in locs)
        else:
            urls.extend(locs)
        if len(urls) >= _SITEMAP_MAX_URLS:
            errors.append({"sitemap": sm, "error": f"URL cap {_SITEMAP_MAX_URLS} reached"})
            urls = urls[:_SITEMAP_MAX_URLS]
            break
    unread = {sm for sm, _ in queue if sm not in seen_maps}
    return {"urls": urls, "sitemaps_read": sorted(seen_maps), "errors": errors,
            "sitemaps_unread": sorted(unread), "complete": not errors and not unread}


_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_REDIRECT_MAX_HOPS = 5
_DEFAULT_HUB_PATHS = ("/", "/blog", "/events", "/about")


def _url_path(u: str) -> str:
    from urllib.parse import urlsplit
    path = urlsplit(u).path or "/"
    return path.rstrip("/") or "/"


async def _follow_redirect(client: Any, url: str, location: Optional[str], site_url: str) -> Dict[str, Any]:
    """Follow a sitemap URL's redirect hop by hop (each hop via the safe,
    paced fetcher; ≤ 5 hops; loops detected) to its final destination."""
    from urllib.parse import urljoin
    chain = [url]
    current = urljoin(url, location or "")
    note: Optional[str] = None
    final_status: Optional[int] = None
    while True:
        if current in chain:
            note = "redirect loop"
            chain.append(current)
            break
        chain.append(current)
        try:
            r = await _fetch_with_backoff(client, current, site_url)
        except _FetchRefused as e:
            note = f"destination not fetched: {e}"
            break
        except Exception as e:  # noqa: BLE001 — a failed hop is reported, not fatal
            note = f"{type(e).__name__}: {e}"
            break
        finally:
            await _async_retry_sleep(_FETCH_PACING_SEC)
        if r.get("rate_limited"):
            note = "destination rate-limited or bot-checked; not verified"
            break
        if r.get("status") in _REDIRECT_STATUSES and r.get("location"):
            if len(chain) - 1 >= _REDIRECT_MAX_HOPS:
                note = f"more than {_REDIRECT_MAX_HOPS} hops"
                break
            current = urljoin(current, r["location"])
            continue
        final_status = r.get("status")
        break
    return {"final_url": chain[-1], "hops": len(chain) - 1, "chain": chain,
            "target_status": final_status, "note": note}


def _classify_redirect(info: Dict[str, Any], hub_paths: set, hub_rx: Optional["re.Pattern"]) -> str:
    """``chain`` (more than one hop, or a loop), ``unverified`` (the
    destination could not be fetched), ``hub`` (lands on a listing page:
    /, /blog, /events, /about plus the site config's hub_paths / hub_regex),
    else ``equivalent`` (a comparable page)."""
    if info["hops"] > 1 or info.get("note") == "redirect loop":
        return "chain"
    if info.get("target_status") is None:
        # The destination was not observed (rate-limited, refused, failed):
        # it could redirect again, so neither hub nor equivalent is known.
        return "unverified"
    path = _url_path(info["final_url"])
    if path in hub_paths or (hub_rx is not None and hub_rx.search(path)):
        return "hub"
    return "equivalent"


@mcp.tool()
async def gsc_sitemap_diff(
    site_url: str,
    sitemap_url: str,
    urls_from: str = "list",
    urls: Optional[List[str]] = None,
    session_id: Optional[str] = None,
    *,
    dataset: str = "internal_all",
    check_limit: int = 100,
    check_offset: int = 0,
    concurrency: int = _FETCH_DEFAULT_CONCURRENCY,
    classify_redirects: bool = True,
    cms_state: Optional[Dict[str, str]] = None,
    save_to_file: Optional[str] = None,
) -> Any:
    """Compare a sitemap with the site's live pages: live 200 URLs missing
    from the sitemap, and sitemap URLs that redirect or 404. Fetches with a
    browser user agent after a control request to the home page; if the
    control fails, live results are marked inconclusive. Only URLs inside
    the property are fetched. This is the approved route for a full-sitemap
    liveness sweep: it paces itself, because bursts against a CDN-fronted
    site (e.g. Cloudflare) trigger bot challenges.

    Every result states its coverage (`coverage.summary`, e.g. "checked 100
    of 591 sitemap URLs — PARTIAL"); `partial` is true until every sitemap
    URL has been checked across calls (see `next_check_offset`).

    Each redirect is followed hop by hop and classified: `equivalent` (lands
    on a comparable page), `hub` (lands on a listing page — /, /blog,
    /events, /about, plus the site config's `hub_paths` / `hub_regex`) or
    `chain` (more than one hop).

    Args:
        site_url: GSC property. sitemap_url: sitemap or sitemap index URL.
        urls_from: `list` (pass `urls`) | `sf_session` (a Screaming Frog
            session from gsc_load_from_sf_export; rows of `dataset` with
            status 200 and an HTML content type count as live).
        check_limit / check_offset: which sitemap URLs get a live check this
            call (default the first 100; ~20 s at the polite default pace).
            Page through a large sitemap with check_offset; `next_check_offset`
            says where to continue.
        classify_redirects: follow and classify each redirect (default true;
            one paced request per hop).
        cms_state: optional {url: state} from a CMS-aware caller (e.g.
            "published", "draft"); redirect rows then carry `cms_state`, and
            `published_but_redirected` when a published page is behind a
            redirect (it stays in a CMS-generated sitemap).
        save_to_file: absolute .json path for the full result.
    """
    tool = "gsc_sitemap_diff"
    try:
        source = _norm_enum(urls_from, {"list": "list", "sfsession": "sf_session", "crawl": "sf_session"},
                            field="urls_from")
        live_candidates: List[str] = []
        live_known: Dict[str, int] = {}
        if source == "list":
            live_candidates = _parse_url_list(urls or [])
        else:
            if session_id not in _sf_sessions:
                raise _SaValidationError(f"Unknown SF session_id: {session_id!r}.", "Load one with gsc_load_from_sf_export.")
            ds = _sf_sessions[session_id]["datasets"].get(dataset)
            if not ds:
                raise _SaValidationError(f"Dataset {dataset!r} not in session.", "")
            for row in _stream_sf_csv(ds):
                addr = (row.get("address") or "").strip()
                status = str(row.get("status_code") or "").strip()
                ctype = str(row.get("content_type") or "html").lower()
                if addr and status == "200" and "html" in ctype:
                    live_known[addr] = 200
            live_candidates = list(live_known)
        async with _new_fetch_client() as client:
            control = await _control_ok(client, site_url, sitemap_url)
            sm = await _read_sitemap_urls(client, sitemap_url, site_url)
            in_sitemap = {_normalize_url(u) for u in sm["urls"]}
            # Live 200 pages missing from the sitemap.
            to_check = [u for u in live_candidates if u not in live_known]
            fetched = await _fetch_statuses(client, to_check[:check_limit], site_url, concurrency=concurrency) if to_check else {}
            live_200 = [u for u in live_candidates if live_known.get(u) == 200 or (fetched.get(u) or {}).get("status") == 200]
            absent = sorted(u for u in live_200 if _normalize_url(u) not in in_sitemap)
            # Sitemap URLs that do not answer 200.
            check_offset = max(0, int(check_offset))
            sample = sm["urls"][check_offset:check_offset + max(0, int(check_limit))]
            statuses = await _fetch_statuses(client, sample, site_url, concurrency=concurrency)
            follow: Dict[str, Dict[str, Any]] = {}
            if classify_redirects:
                for u, st in statuses.items():
                    if st.get("status") in _REDIRECT_STATUSES:
                        follow[u] = await _follow_redirect(client, u, st.get("location"), site_url)
        out_of_scope = sorted(u for u, s in statuses.items() if s.get("refused"))
        rate_limited = sorted(u for u, s in statuses.items() if s.get("rate_limited"))
        challenged = sum(1 for s in statuses.values() if s.get("bot_challenge"))
        bad = [
            {"url": u, "status": s.get("status"), "location": s.get("location"), "error": s.get("error")}
            for u, s in statuses.items()
            if s.get("status") != 200 and not s.get("refused") and not s.get("rate_limited")
        ]
        bad.sort(key=lambda r: (str(r["status"]), r["url"]))
        cfg = _load_site_config(site_url)
        hub_paths = {(_url_path(p) if p.startswith("http") else (p.rstrip("/") or "/"))
                     for p in list(_DEFAULT_HUB_PATHS) + list(cfg.get("hub_paths") or [])}
        hub_rx = re.compile(cfg["hub_regex"]) if cfg.get("hub_regex") else None
        cms = {_normalize_url(k): str(v) for k, v in (cms_state or {}).items()}
        redirect_summary: Dict[str, int] = {"equivalent": 0, "hub": 0, "chain": 0, "unverified": 0,
                                            "published_but_redirected": 0}
        for b in bad:
            if b["status"] not in _REDIRECT_STATUSES:
                continue
            info = follow.get(b["url"])
            if info:
                b.update(final_url=info["final_url"], hops=info["hops"], chain=info["chain"],
                         target_status=info["target_status"], classification=_classify_redirect(info, hub_paths, hub_rx))
                if info.get("note"):
                    b["note"] = info["note"]
                redirect_summary[b["classification"]] += 1
            state = cms.get(_normalize_url(b["url"]))
            if state is not None:
                b["cms_state"] = state
                b["published_but_redirected"] = state.strip().lower() == "published"
                redirect_summary["published_but_redirected"] += int(b["published_but_redirected"])
        live_checked = len(sample) - len(set(rate_limited) | set(out_of_scope))
        n_redirects = sum(1 for b in bad if b["status"] in _REDIRECT_STATUSES)
        n_verified = sum(1 for b in bad if b.get("classification") not in (None, "unverified")
                         and b["status"] in _REDIRECT_STATUSES)
        parts = {
            "sitemap_urls_live_checked": _coverage(live_checked, len(sm["urls"]), "sitemap URLs live-checked"),
            "sitemap_documents_read": _coverage(
                len(sm["sitemaps_read"]) - len(sm["errors"]),
                len(sm["sitemaps_read"]) + len(sm.get("sitemaps_unread") or []), "sitemap documents read"),
            "live_candidates_checked": _coverage(len(live_known) + len(fetched), len(live_candidates),
                                                 "live candidate URLs checked"),
        }
        if classify_redirects:
            parts["redirects_classified"] = _coverage(n_verified, n_redirects, "redirects with a verified destination")
        coverage = _coverage_rollup(parts)
        out = {
            "ok": True, "tool": tool, "site_url": site_url, "sitemap_url": sitemap_url,
            # Coverage first: counts below cover only what was checked.
            "partial": coverage["partial"],
            "coverage": coverage,
            "inconclusive": not control["ok"],
            "control": control,
            "sitemap_url_count": len(sm["urls"]),
            "sitemaps_read": sm["sitemaps_read"],
            "sitemap_errors": sm["errors"],
            "sitemap_complete": sm["complete"],
            # An incomplete sitemap read cannot prove absence: those URLs are
            # only "possibly" missing.
            "missing_from_sitemap": absent if sm["complete"] else [],
            "possibly_missing_from_sitemap": [] if sm["complete"] else absent,
            "redirect_summary": redirect_summary if classify_redirects else None,
            "sitemap_urls_not_200": {
                "redirect": [b for b in bad if b["status"] in (301, 302, 303, 307, 308)],
                "not_found": [b for b in bad if b["status"] in (404, 410)],
                "other": [b for b in bad if b["status"] not in (301, 302, 303, 307, 308, 404, 410)],
            },
            "sitemap_urls_out_of_scope": out_of_scope,
            # Still rate-limited after retries: NOT checked, not known broken.
            "sitemap_urls_unchecked_rate_limited": rate_limited,
            "checked": {
                "sitemap_urls": len(sample) - len(rate_limited),
                "sitemap_urls_unchecked": max(0, len(sm["urls"]) - len(sample)) + len(rate_limited),
                "check_offset": check_offset,
                "next_check_offset": (check_offset + len(sample)) if check_offset + len(sample) < len(sm["urls"]) else None,
                "blocked_by_bot_check": challenged,
                "live_candidates_supplied": len(live_candidates),
                "live_known_from_crawl": len(live_known),
                "live_fetched": len(fetched),
                "live_unchecked": max(0, len(to_check) - check_limit),
            },
            "meta": _standard_meta(None, site_url=site_url, urls_from=source, coverage=coverage,
                                   hub_paths=sorted(hub_paths),
                                   warnings=([] if not coverage["sitemap_urls_live_checked"]["partial"] else [
                                       f"Live-checked {live_checked} of {len(sm['urls'])} sitemap URLs: redirect "
                                       f"and 404 counts cover only those"
                                       + (f"; continue with check_offset={check_offset + len(sample)}."
                                          if check_offset + len(sample) < len(sm["urls"]) else ".")])
                                   + ([] if control["ok"] else [
                                       "Control request to the home page failed; live statuses may reflect "
                                       "bot blocking, not the site."])
                                   + ([] if sm["complete"] else [
                                       "The sitemap was not read completely (see sitemap_errors); absence "
                                       "from it is reported as 'possibly' only."])
                                   + ([] if not rate_limited else [
                                       f"{len(rate_limited)} sitemap URL(s) were not checked: rate-limited (429/503) "
                                       f"after retries, or served a CDN bot-check page ({challenged}). Rerun later "
                                       f"or lower concurrency."])),
        }
        if save_to_file:
            return await _save_and_trim(out, save_to_file, (
                "missing_from_sitemap", "possibly_missing_from_sitemap", "sitemap_urls_out_of_scope",
                "sitemap_urls_unchecked_rate_limited"))
        return out
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, site_url=site_url)


# --- Recrawl worklist (v1.5.0) ---
_MANUAL_REQUEST_DAILY_CAP = 10  # Search Console's manual Request Indexing allowance is small and undocumented


_worklist_lock = asyncio.Lock()


_ACTIVE_JOB_STATES = ("queued", "running", "throttled")


def _job_view(site_url: str, url: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """For one URL across this site's jobs (newest first): a fresh result
    (inspected within the cache TTL, by its own timestamp), the newest
    active job still working on it, and the newest terminal error."""
    cutoff = time.time() - _INSPECTION_CACHE_TTL_SEC
    fresh = active = error = None
    for job in sorted(_inspect_jobs.values(), key=lambda j: -j["created_at"]):
        if job["site_url"] != site_url or url not in job["urls"]:
            continue
        ts = (job.get("result_ts") or {}).get(url)
        if fresh is None and url in job["results"] and ts and ts >= cutoff:
            fresh = job["results"][url]
        elif active is None and job["state"] in _ACTIVE_JOB_STATES and url not in job["errors"]:
            active = job
        elif error is None and url in job["errors"] and job["created_at"] >= cutoff:
            error = dict(job["errors"][url], job_id=job["job_id"])
    return fresh, active, error


async def _start_inspect_job(site_url: str, service: Any, alias: str, urls: List[str], *,
                             force: bool, concurrency: int) -> Dict[str, Any]:
    _evict_inspect_jobs()
    job_id = f"insp-{uuid4().hex[:12]}"
    now = time.time()
    job: Dict[str, Any] = {
        "job_id": job_id, "site_url": site_url, "account_alias": alias, "urls": urls,
        "force": bool(force), "state": "queued", "created_at": now, "updated_at": now,
        "finished_at": None, "results": {}, "errors": {}, "cached": 0,
    }
    if not force:
        for url in urls:
            hit = await asyncio.to_thread(_cache_get, site_url, url)
            if hit is not None:
                job["results"][url] = _normalize_inspection(url, hit[0], hit[1])
                job.setdefault("result_ts", {})[url] = hit[1]
        job["cached"] = len(job["results"])
    _inspect_jobs[job_id] = job
    if len(job["results"]) == len(urls):
        job["state"] = "done"
        job["finished_at"] = time.time()
    else:
        job["task"] = asyncio.create_task(_run_inspect_job(job, service, concurrency))
    return job


def _parse_changed(value: Any) -> Optional[str]:
    if not value:
        return None
    return _parse_gsc_date(str(value)[:10])


@mcp.tool()
async def gsc_recrawl_worklist(
    site_url: str,
    items: List[Any],
    changed_on: Optional[str] = None,
    daily_cap: int = _MANUAL_REQUEST_DAILY_CAP,
    *,
    retry: bool = False,
    check_live: bool = True,
    account_alias: Optional[str] = None,
) -> Any:
    """Which changed URLs Google hasn't picked up yet — the list for a human
    to paste into Search Console's Request Indexing (the API cannot request
    indexing for normal pages; the Indexing API only covers JobPosting and
    BroadcastEvent). Resubmitting the sitemap also helps.

    Pass `items` as [{url, changed_on}] (or plain URLs plus one `changed_on`).
    URLs not inspected in the last 6 hours are inspected in a background job:
    the first call then returns status "pending" with a job_id — poll
    gsc_inspect_status, then call this again. A URL that failed stays in
    `unresolved` (never a silent pending); pass retry=true to re-inspect it.

    Reasons a URL needs a request: crawled before it changed; not indexed;
    Google sees "Page with redirect" while the live URL answers 200.
    Canonical mismatches are listed separately — Request Indexing does not
    change Google's canonical choice; the fix is on the page. The list is
    ordered by the last 28 days' clicks and capped at `daily_cap`.
    """
    tool = "gsc_recrawl_worklist"
    try:
        plan: Dict[str, Optional[str]] = {}
        for it in items or []:
            if isinstance(it, dict):
                url = str(it.get("url") or "").strip()
                when = it.get("changed_on") or it.get("last_change_date") or changed_on
            else:
                url, when = str(it).strip(), changed_on
            if url:
                plan[url] = _parse_changed(when)
        if not plan:
            raise _SaValidationError("No URLs given.", "Pass items=[{url, changed_on}].")
        missing_dates = [u for u, d in plan.items() if not d]
        if missing_dates:
            raise _SaValidationError(f"{len(missing_dates)} URL(s) have no changed_on date.",
                                     "Give each item a changed_on (YYYY-MM-DD) or pass changed_on for all.")
        urls = list(plan)
        resolved_alias, service = await get_gsc_service_for_site(site_url, account_alias)

        results: Dict[str, Dict[str, Any]] = {}
        unresolved: Dict[str, Any] = {}
        job: Optional[Dict[str, Any]] = None
        # One worklist at a time decides and starts inspections, so two
        # concurrent calls cannot launch duplicate jobs for the same URLs.
        async with _worklist_lock:
            waiting: Dict[str, str] = {}  # url -> active job id
            to_start: List[str] = []
            for url in urls:
                fresh, active, error = _job_view(site_url, url)
                if active is not None and active.get("force"):
                    waiting[url] = active["job_id"]  # a forced re-inspection outranks the cache
                    continue
                hit = await asyncio.to_thread(_cache_get, site_url, url)
                if hit is not None:
                    results[url] = _normalize_inspection(url, hit[0], hit[1])
                elif fresh is not None:
                    results[url] = fresh
                elif active is not None:
                    waiting[url] = active["job_id"]
                elif error is not None and not retry:
                    unresolved[url] = error  # reported, not re-inspected unless retry=true
                else:
                    to_start.append(url)
            if to_start:
                job = await _start_inspect_job(site_url, service, resolved_alias, to_start,
                                               force=False, concurrency=_INSPECT_DEFAULT_CONCURRENCY)
                if job["state"] == "done":
                    results.update(job["results"])
                else:
                    waiting.update({u: job["job_id"] for u in to_start})
            if waiting:
                return {"ok": True, "tool": tool, "status": "pending",
                        "job_id": (job or {}).get("job_id") or next(iter(waiting.values())),
                        "job_ids": sorted(set(waiting.values())), "pending": len(waiting),
                        "poll_with": "gsc_inspect_status", "then_call": tool}

        # Traffic value: clicks over the last 28 final days, per page.
        ctx = _SaContext(site_url, account_alias, resolved_alias, service)
        traffic: Dict[str, Dict[str, Any]] = {}
        for chunk in _chunks(urls, 20):
            t = await _sa_run(ctx, dimensions=["page"], days=28, fetch_all=True, step=f"{tool}.traffic",
                              filter_groups=[{"filters": [{"dimension": "page", "operator": "includingRegex",
                                                           "expression": _url_regex(chunk)}]}])
            traffic.update({r["page"]: r for r in t["rows"]})

        redirects = [u for u, r in results.items() if "redirect" in (r.get("coverage_state") or "").lower()]
        live: Dict[str, Dict[str, Any]] = {}
        control = None
        if check_live and redirects:
            async with _new_fetch_client() as client:
                control = await _control_ok(client, site_url, redirects[0])
                if control["ok"]:  # a failed control makes every live status meaningless
                    live = await _fetch_statuses(client, redirects, site_url)

        for url in urls:  # every URL ends up in exactly one list
            if url not in results and url not in unresolved:
                unresolved[url] = {"error": "not inspected"}
        needs, mismatches, fine = [], [], []
        for url in urls:
            if url in unresolved:
                continue
            r = results[url]
            changed = plan[url]
            crawl = (r.get("last_crawl_time") or "")[:10]
            entry = {
                "url": url, "changed_on": changed, "last_crawl": crawl or None,
                "verdict": r.get("verdict"), "coverage_state": r.get("coverage_state"),
                "clicks_28d": (traffic.get(url) or {}).get("clicks", 0),
                "impressions_28d": (traffic.get(url) or {}).get("impressions", 0),
            }
            if r.get("canonical_mismatch"):
                mismatches.append(dict(entry, google_canonical=r.get("google_canonical"),
                                       user_canonical=r.get("user_canonical")))
                continue
            reasons = []
            if not crawl or crawl < changed:
                reasons.append(f"crawled before the change (last crawl {crawl or 'never'}, changed {changed})")
            coverage = (r.get("coverage_state") or "").lower()
            if "redirect" in coverage:
                status = (live.get(url) or {}).get("status")
                if status == 200:
                    reasons.append("Google sees 'Page with redirect' but the live URL answers 200")
                elif status in (301, 302, 303, 307, 308):
                    entry["note"] = f"live URL answers {status}; the redirect is real"
                elif not reasons:
                    # No usable live evidence and nothing else to go on:
                    # neither "needs a request" nor "fine" can be claimed.
                    why = ("live check skipped" if not check_live else
                           "control request failed" if control and not control["ok"] else
                           f"live check returned {status or (live.get(url) or {}).get('error') or 'nothing'}")
                    unresolved[url] = {"error": f"Google sees 'Page with redirect'; {why}"}
                    continue
            elif r.get("verdict") != "PASS" and not reasons:
                reasons.append(f"not indexed: {r.get('coverage_state')}")
            if reasons:
                needs.append(dict(entry, reasons=reasons))
            else:
                fine.append(entry)
        needs.sort(key=lambda e: (-(e["clicks_28d"] or 0), -(e["impressions_28d"] or 0), e["url"]))
        cap = max(0, int(daily_cap))
        today_list, later = needs[:cap], needs[cap:]
        return {
            "ok": True, "tool": tool, "status": "complete", "site_url": site_url,
            "needs_request_indexing": today_list,
            "needs_request_indexing_later": later,
            "paste_ready": "\n".join(e["url"] for e in today_list),
            "canonical_mismatches": mismatches,
            "no_action_needed": fine,
            "unresolved": [{"url": u, "error": e} for u, e in unresolved.items()],
            "counts": {"needs_request_indexing": len(needs), "canonical_mismatches": len(mismatches),
                       "no_action_needed": len(fine), "unresolved": len(unresolved)},
            "live_check_control": control,
            "coverage": _coverage(len(urls) - len(unresolved), len(urls), "URLs classified"),
            "meta": _standard_meta(None, site_url=site_url, daily_cap=cap, job_id=(job or {}).get("job_id"),
                                   coverage=_coverage(len(urls) - len(unresolved), len(urls), "URLs classified"),
                                   api_limits={"request_indexing": _API_LIMITS["request_indexing"]}),
        }
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, site_url=site_url)


# --- Search Console UI export import (v1.5.0) ---
# For data the API does not have (the Generative AI features report, and
# any UI export): load the CSV/zip/XLSX a human downloaded, then query it
# with SQL in the SQLite dialect. SQLite, not DuckDB, on purpose: exports
# are ~1,000 rows per table, and an engine that cannot touch the
# filesystem matters more than richer SQL (DuckDB's read_csv/COPY can read
# and write arbitrary files unless external access is locked down).
# Each session is an in-memory database with temp_store=MEMORY, a
# read-only authorizer (SELECT/READ/FUNCTION only: no ATTACH, PRAGMA or
# writes), query_only, no extensions, size limits, and a timeout.
_UI_MAX_FILE_BYTES = 20 * 1024 * 1024
_UI_MAX_ZIP_ENTRIES = 50
_UI_MAX_UNZIPPED_BYTES = 50 * 1024 * 1024
_UI_MAX_ROWS_PER_TABLE = 5_000
_UI_MAX_CELL_CHARS = 10_000
_UI_MAX_SESSIONS = 10
_UI_MAX_SESSION_BYTES = 20 * 1024 * 1024
_UI_MAX_SQL_CHARS = 20_000
_UI_MAX_VALUE_BYTES = 1_000_000
_UI_MAX_RESULT_ROWS = 5_000
_UI_MAX_COLUMNS = 200
_UI_MAX_RESULT_BYTES = 10 * 1024 * 1024
_UI_QUERY_TIMEOUT_SEC = 5.0
_UI_METRICS = {"clicks": "clicks", "impressions": "impr", "ctr": "ctr", "position": "pos"}
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_ui_sessions: Dict[str, Dict[str, Any]] = {}


class _UiExportError(ValueError):
    pass


def _check_sqlite_version() -> None:
    if sqlite3.sqlite_version_info < (3, 25, 0):
        raise _UiExportError(
            f"SQLite {sqlite3.sqlite_version} is too old: window functions (LAG, RANK) need 3.25+. "
            "Rebuild the venv on a newer Python."
        )


def _ui_table_name(raw: str, taken: set) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", Path(raw).stem.lower()).strip("_") or "table"
    name = {"top_queries": "queries", "top_pages": "pages", "search_appearance": "search_appearance"}.get(name, name)
    if name[0].isdigit():
        name = "t_" + name
    base, i = name, 2
    while name in taken or name == "meta":
        name = f"{base}_{i}"
        i += 1
    taken.add(name)
    return name


def _ui_value(cell: Any) -> Any:
    """Typed cell: numbers become numbers, '2.5%' becomes 0.025."""
    if cell is None:
        return None
    if isinstance(cell, bool):
        return int(cell)
    if isinstance(cell, float):
        return int(cell) if cell.is_integer() else cell  # XLSX stores counts as floats
    if isinstance(cell, int):
        return cell
    text = str(cell).strip()
    if text == "":
        return None
    if len(text) > _UI_MAX_CELL_CHARS:
        raise _UiExportError(f"A cell is over {_UI_MAX_CELL_CHARS} characters.")
    t = text.replace(",", "")
    try:
        if t.endswith("%"):
            return float(t[:-1]) / 100.0
        if re.fullmatch(r"-?\d+", t):
            return int(t)
        if re.fullmatch(r"-?\d*\.\d+(e-?\d+)?", t, re.IGNORECASE):
            return float(t)
    except ValueError:
        pass
    return text


_SLASH_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _slash_order(labels: List[str]) -> Optional[str]:
    """``"dmy"`` or ``"mdy"`` for slash dates in period labels, decided from
    all labels together (Search Console writes dates in the account's
    locale): a first part over 12 means day-first, a second part over 12
    month-first. None when every date is ambiguous."""
    firsts, seconds = [], []
    for label in labels:
        for a, b, _ in _SLASH_DATE_RE.findall(label):
            firsts.append(int(a))
            seconds.append(int(b))
    if any(v > 12 for v in firsts):
        return "dmy"
    if any(v > 12 for v in seconds):
        return "mdy"
    return None


def _parse_period(label: str, order: Optional[str]) -> Dict[str, Optional[str]]:
    """``{start, end, month}`` for a compare-mode period label such as
    '01/08/2026 - 27/08/2026' (dates ISO; month = start month, 'aug')."""
    dates: List[str] = []
    for y, m, d in _ISO_DATE_RE.findall(label):
        dates.append(f"{y}-{m}-{d}")
    if not dates and order:
        for a, b, y in _SLASH_DATE_RE.findall(label):
            day, mon = (int(a), int(b)) if order == "dmy" else (int(b), int(a))
            year = int(y) + (2000 if len(y) == 2 else 0)
            try:
                dates.append(datetime(year, mon, day).date().isoformat())
            except ValueError:
                pass
    month = _MONTHS[int(dates[0][5:7]) - 1] if dates else None
    if month is None:
        low = label.lower()
        month = next((name for name in _MONTHS if re.search(rf"\b{name}", low)), None)
    return {"start": dates[0] if dates else None, "end": dates[-1] if dates else None, "month": month}


def _ui_columns(headers: List[str]) -> Tuple[List[str], List[Tuple[str, int]], List[Dict[str, Any]]]:
    """Normalised column names, extra alias columns ``(alias, source_index)``
    for compare mode, and a description of the periods found.

    Compare-mode headers look like '<period> <Metric>'. Each period gets
    ``<metric>_a`` / ``<metric>_b`` aliases (a = first in the file) and,
    when its label names a month, ``<metric>_<mon>`` plus the short form
    (``impr_aug``, ``pos_sep``)."""
    seen: Dict[str, int] = {}
    names = [_normalize_column(h, seen) for h in headers]
    prefixes: Dict[str, List[Tuple[str, int]]] = {}
    for i, h in enumerate(headers):
        low = h.strip().lower()
        for metric in _UI_METRICS:
            if low.endswith(metric) and low != metric:
                prefix = h.strip()[: -len(metric)].strip(" -:_")
                if prefix:
                    prefixes.setdefault(prefix, []).append((metric, i))
                break
    aliases: List[Tuple[str, int]] = []
    periods: List[Dict[str, Any]] = []
    if len(prefixes) >= 2:
        taken = set(names)
        order = _slash_order(list(prefixes))
        parsed = {prefix: _parse_period(prefix, order) for prefix in prefixes}
        months = [parsed[p]["month"] for p in prefixes]
        if len(set(months)) != len(months):
            # Two periods in the same month: month aliases would collide.
            for p in parsed.values():
                p["month"] = None
        for letter, (prefix, cols) in zip("abcdefgh", prefixes.items()):
            month = parsed[prefix]["month"]
            periods.append({"label": prefix, "suffix": letter, "month": month,
                            "start": parsed[prefix]["start"], "end": parsed[prefix]["end"],
                            "date_order": order})
            for metric, idx in cols:
                wanted = [f"{metric}_{letter}"]
                if month:
                    wanted += [f"{metric}_{month}", f"{_UI_METRICS[metric]}_{month}"]
                for alias in wanted:
                    if alias not in taken:
                        taken.add(alias)
                        aliases.append((alias, idx))
    return names, aliases, periods


def _check_zip(path: Path) -> "zipfile.ZipFile":
    import zipfile
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise _UiExportError(f"Not a valid zip/xlsx file: {e}")
    infos = zf.infolist()
    if len(infos) > _UI_MAX_ZIP_ENTRIES:
        raise _UiExportError(f"Archive has {len(infos)} entries (max {_UI_MAX_ZIP_ENTRIES}).")
    total = sum(i.file_size for i in infos)
    if total > _UI_MAX_UNZIPPED_BYTES:
        raise _UiExportError(f"Archive expands to {total} bytes (max {_UI_MAX_UNZIPPED_BYTES}).")
    return zf


def _decode_csv_bytes(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def _check_row(row: List[Any], n_rows: int, name: str) -> None:
    """Budgets enforced while reading, before a table is materialised."""
    if len(row) > _UI_MAX_COLUMNS:
        raise _UiExportError(f"Table {name!r} has {len(row)} columns (max {_UI_MAX_COLUMNS}).")
    if n_rows > _UI_MAX_ROWS_PER_TABLE:
        raise _UiExportError(
            f"Table {name!r} has more than {_UI_MAX_ROWS_PER_TABLE} rows; UI exports are "
            f"normally 1,000. Refusing rather than silently truncating."
        )
    for c in row:
        if c is not None and len(str(c)) > _UI_MAX_CELL_CHARS:
            raise _UiExportError(f"A cell in {name!r} is over {_UI_MAX_CELL_CHARS} characters.")


def _csv_table(text: str, name: str = "table") -> Tuple[List[str], List[List[Any]]]:
    header: Optional[List[str]] = None
    rows: List[List[Any]] = []
    for r in csv.reader(io.StringIO(text)):
        if not any(c.strip() for c in r):
            continue
        if header is None:
            _check_row(r, 0, name)
            header = r
        else:
            _check_row(r, len(rows) + 1, name)
            rows.append(r)
    return header or [], rows


def _read_export(path: Path) -> List[Tuple[str, List[str], List[List[Any]]]]:
    """``[(raw_table_name, headers, rows), ...]`` from a .csv, .zip of CSVs or .xlsx."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        headers, rows = _csv_table(_decode_csv_bytes(path.read_bytes()), path.name)
        return [(path.name, headers, rows)]
    if suffix == ".zip":
        zf = _check_zip(path)
        tables = []
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".csv"):
                continue
            headers, rows = _csv_table(_decode_csv_bytes(zf.read(info)), info.filename)
            tables.append((Path(info.filename).name, headers, rows))
        return tables
    if suffix == ".xlsx":
        _check_zip(path).close()
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        tables = []
        try:
            for ws in wb.worksheets:
                if (ws.max_column or 0) > _UI_MAX_COLUMNS:
                    raise _UiExportError(f"Sheet {ws.title!r} spans {ws.max_column} columns (max {_UI_MAX_COLUMNS}).")
                def _trim(r: Any) -> List[Any]:
                    r = list(r or [])
                    while r and (r[-1] is None or str(r[-1]).strip() == ""):
                        r.pop()
                    return r
                it = ws.iter_rows(values_only=True)
                header = _trim(next(it, None))
                if not header:
                    continue
                _check_row(header, 0, ws.title)
                rows = []
                for r in it:
                    r = _trim(r)
                    if not r:
                        continue
                    _check_row(r, len(rows) + 1, ws.title)
                    rows.append(r)
                tables.append((ws.title, [str(h or "") for h in header], rows))
        finally:
            wb.close()
        return tables
    raise _UiExportError(f"Unsupported file type {suffix!r}: pass a .csv, .zip or .xlsx export.")


def _ui_authorizer(action: int, arg1: Any, arg2: Any, dbname: Any, source: Any) -> int:
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
    recursive = getattr(sqlite3, "SQLITE_RECURSIVE", None)
    if recursive is not None:
        allowed.add(recursive)
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def _build_ui_session(path: Path, report_kind: Optional[str]) -> Dict[str, Any]:
    _check_sqlite_version()
    if path.stat().st_size > _UI_MAX_FILE_BYTES:
        raise _UiExportError(f"File is {path.stat().st_size} bytes (max {_UI_MAX_FILE_BYTES}).")
    raw_tables = _read_export(path)
    if not raw_tables:
        raise _UiExportError("No tables found in the export.")
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA temp_store=MEMORY")  # sorts/GROUP BY never spill to temp files
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _UI_MAX_VALUE_BYTES)
    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, _UI_MAX_SQL_CHARS + 1000)
    taken: set = set()
    tables, size = [], 0
    meta_rows: List[Tuple[str, str]] = [
        ("source_file", path.name), ("report_kind", report_kind or ""),
        ("loaded_at", datetime.now(timezone.utc).isoformat()),
    ]
    for raw_name, headers, rows in raw_tables:
        if not headers:
            continue
        name = _ui_table_name(raw_name, taken)
        cols, aliases, periods = _ui_columns(headers)
        all_cols = cols + [a for a, _ in aliases]
        conn.execute(f'CREATE TABLE "{name}" ({", ".join(chr(34) + c + chr(34) for c in all_cols)})')
        # Only metric columns are coerced to numbers: a query "404" or
        # "2026 budget" stays text.
        numeric = [bool(re.search(r"clicks|impressions|impr|ctr|position|pos\b", c)) for c in cols]
        typed = []
        for r in rows:
            vals = []
            for i in range(len(cols)):
                cell = r[i] if i < len(r) else None
                if numeric[i]:
                    vals.append(_ui_value(cell))
                else:
                    text = None if cell is None else str(cell).strip()
                    vals.append(text if text else None)
            vals += [vals[idx] for _, idx in aliases]
            size += sum(len(str(v)) for v in vals if v is not None)
            if size > _UI_MAX_SESSION_BYTES:
                raise _UiExportError(f"Export is over {_UI_MAX_SESSION_BYTES} bytes once loaded.")
            typed.append(vals)
        conn.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" * len(all_cols))})', typed)
        tables.append({"name": name, "source": raw_name, "columns": all_cols,
                       "row_count": len(typed), "compare_periods": periods})
        # The Filters tab records only the first range in compare mode; the
        # headers carry both, so they go into meta from there.
        if periods and not any(k == "period_a" for k, _ in meta_rows):
            for p in periods:
                meta_rows.append((f"period_{p['suffix']}",
                                  f"{p['start']}..{p['end']}" if p["start"] else p["label"]))
                meta_rows.append((f"period_{p['suffix']}_label", p["label"]))
        if name == "filters":
            for r in rows:
                if len(r) >= 2 and r[0]:
                    key = str(r[0]).strip()
                    meta_rows.append((f"filter:{key}", str(r[1] if r[1] is not None else "")))
                    low = key.lower()
                    if low == "date":
                        meta_rows.append(("date_range", str(r[1])))
                    elif low in ("search type", "type"):
                        meta_rows.append(("search_type", str(r[1])))
    meta_rows.append(("tables", ",".join(t["name"] for t in tables)))
    conn.execute('CREATE TABLE meta ("key", "value")')
    conn.executemany("INSERT INTO meta VALUES (?, ?)", meta_rows)
    conn.commit()
    conn.execute("PRAGMA query_only=ON")
    try:
        conn.enable_load_extension(False)
    except AttributeError:
        pass
    conn.set_authorizer(_ui_authorizer)
    return {"conn": conn, "tables": tables, "meta": dict(meta_rows), "size": size,
            "lock": __import__("threading").Lock(), "last_used": time.time(), "path": str(path)}


def _close_ui_session(sess: Dict[str, Any]) -> None:
    with sess["lock"]:
        sess["conn"].close()


def _run_ui_sql(sess: Dict[str, Any], sql: str, limit: int) -> Tuple[List[str], List[List[Any]], bool, bool]:
    deadline = time.monotonic() + _UI_QUERY_TIMEOUT_SEC
    conn = sess["conn"]
    with sess["lock"]:
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        out: List[List[Any]] = []
        more = clipped = False
        budget = _UI_MAX_RESULT_BYTES
        try:
            cur = conn.execute(sql)
            columns = [d[0] for d in cur.description or []]
            # Row by row, clipping each value before keeping it, under a
            # total byte budget — never a bulk fetch of large values.
            for r in cur:
                if len(out) >= limit:
                    more = True
                    break
                vals, cost = [], 0
                for v in r:
                    if isinstance(v, bytes):
                        v, clipped = f"<blob {len(v)} bytes>", True
                    elif isinstance(v, str) and len(v) > _UI_MAX_CELL_CHARS:
                        v, clipped = v[:_UI_MAX_CELL_CHARS], True
                    cost += len(v.encode("utf-8")) if isinstance(v, str) else 16
                    vals.append(v)
                if cost > budget:  # checked before the row is kept
                    more = True
                    break
                budget -= cost
                out.append(vals)
        finally:
            conn.set_progress_handler(None, 0)
    return columns, out, more, clipped


@mcp.tool()
async def gsc_load_ui_export(
    file_path: str,
    report_kind: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Any:
    """Load a Search Console UI export (.zip of CSVs, a single .csv, or .xlsx)
    for SQL — the way to analyse data the API doesn't have, e.g. the
    Generative AI features report (AI Overview / AI Mode). Each tab becomes a
    table (queries, pages, countries, dates, chart, filters, …) plus a
    `meta` table (source file, date range, search type, filters). Compare
    exports get period aliases: `<metric>_a`/`_b` and, where the label names
    a month, `<metric>_<mon>` / `impr_aug`-style short forms. Query with
    `gsc_query_ui_export`.

    Args:
        file_path: absolute path to the export a human downloaded.
        report_kind: free-text label, e.g. "generative_ai_features".
        session_id: reuse an id to replace a loaded session.
    """
    tool = "gsc_load_ui_export"
    try:
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            raise _UiExportError(f"file_path must be absolute, got {file_path!r}.")
        if not path.is_file():
            raise _UiExportError(f"File not found: {file_path!r}.")
        sess = await asyncio.to_thread(_build_ui_session, path, report_kind)
        sid = session_id or f"ui-{uuid4().hex[:12]}"
        retired = [_ui_sessions.pop(sid)] if sid in _ui_sessions else []
        while len(_ui_sessions) >= _UI_MAX_SESSIONS:
            lru = min(_ui_sessions, key=lambda k: _ui_sessions[k]["last_used"])
            retired.append(_ui_sessions.pop(lru))
        _ui_sessions[sid] = sess
        for old in retired:  # waits for any running query, off the event loop
            await asyncio.to_thread(_close_ui_session, old)
        return {
            "ok": True, "tool": tool, "session_id": sid,
            "tables": sess["tables"] + [{"name": "meta", "columns": ["key", "value"], "row_count": len(sess["meta"])}],
            "export_meta": sess["meta"],
            "sql_dialect": f"SQLite {sqlite3.sqlite_version} (window functions supported; read-only)",
            "meta": _standard_meta(None, row_count=sum(t["row_count"] for t in sess["tables"]), truncated=False),
        }
    except _UiExportError as e:
        return _format_error(_make_error_envelope(error=str(e), error_code=ErrorCode.BAD_REQUEST, tool=tool,
                                                  hint="Pass the export exactly as downloaded from Search Console."),
                             response_format="json")
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool)


@mcp.tool()
async def gsc_query_ui_export(
    session_id: str,
    sql: str,
    limit: int = 200,
    response_format: str = "json",
    save_to_file: Optional[str] = None,
) -> Any:
    """Run one read-only SQL SELECT (SQLite dialect: CTEs, window functions
    like LAG and RANK) against a session from `gsc_load_ui_export`.
    Anything but reading is refused (no ATTACH, PRAGMA, writes or
    extensions). Example:
    `select page, impr_aug, impr_sep from pages order by impr_aug - impr_sep desc limit 20`.

    Args:
        session_id: from gsc_load_ui_export.
        sql: a single SELECT statement (max 20,000 characters).
        limit: max rows returned (default 200, max 5000).
        response_format: json (default) | csv | markdown.
        save_to_file: absolute .csv/.json path; writes the rows, returns 20.
    """
    tool = "gsc_query_ui_export"
    try:
        sess = _ui_sessions.get(session_id)
        if sess is None:
            return _format_error(_make_error_envelope(
                error=f"Unknown UI export session {session_id!r}.",
                hint="Sessions live in server memory; reload with gsc_load_ui_export.",
                error_code=ErrorCode.NOT_FOUND, tool=tool), response_format="json")
        if not isinstance(sql, str) or not sql.strip():
            raise _SaValidationError("sql is empty.", "")
        if len(sql) > _UI_MAX_SQL_CHARS:
            raise _SaValidationError(f"sql is over {_UI_MAX_SQL_CHARS} characters.", "")
        limit = max(1, min(int(limit), _UI_MAX_RESULT_ROWS))
        sess["last_used"] = time.time()
        try:
            columns, rows, more, clipped = await asyncio.to_thread(_run_ui_sql, sess, sql, limit)
        except sqlite3.DatabaseError as e:
            msg = str(e)
            hint = ("Only a single read-only SELECT is allowed." if "not authorized" in msg
                    else "Query ran past the time limit; simplify it." if "interrupted" in msg
                    else "Check table/column names: see the tables listed by gsc_load_ui_export.")
            raise _SaValidationError(f"SQL error: {msg}", hint)
        dict_rows = [dict(zip(columns, r)) for r in rows]
        cols = [{"key": c, "display": c, "type": "str"} for c in columns]
        warnings = ["Some text values were clipped to 10,000 characters."] if clipped else []
        if save_to_file:
            return await _saved_summary(
                tool=tool, path=save_to_file, rows=dict_rows, columns=cols,
                meta=_standard_meta(None, session_id=session_id, source_file=sess["meta"].get("source_file"),
                                    row_count=len(dict_rows), truncated=more, warnings=warnings),
                response_format=response_format, truncated=more,
            )
        out = _format_table(
            dict_rows, cols, response_format=response_format, truncated=more,
            truncation_hint=f"More rows exist; raise limit (max {_UI_MAX_RESULT_ROWS}) or add a WHERE/LIMIT.",
            meta=_standard_meta(None, session_id=session_id, source_file=sess["meta"].get("source_file"),
                                export_date_range=sess["meta"].get("date_range"),
                                row_count=len(dict_rows), truncated=more, warnings=warnings,
                                coverage=_coverage(len(dict_rows), None if more else len(dict_rows), "result rows")),
            text_meta=True,
        )
        if isinstance(out, dict):
            out["tool"] = tool
        return out
    except Exception as e:  # noqa: BLE001 — every failure becomes an envelope
        return _tool_error(e, tool=tool, response_format=response_format if response_format in _RESPONSE_FORMATS else "json")


# --- Health check (Add 4) ---

# §0 of the 2026-09-28 change request: things the API cannot do, stated
# plainly so nobody builds for them.
_API_LIMITS = {
    "generative_ai_features": "UI only (AI Overview / AI Mode). Not an API type, not a searchAppearance "
                              "value, not in bulk export. Import a UI export instead; unknown_values "
                              "flags new API values when support appears.",
    "request_indexing": "Not possible for normal pages (the Indexing API covers only JobPosting and "
                        "BroadcastEvent). Submit in the Search Console UI; resubmit sitemaps.",
    "branded_filter": "UI only; there is no brand dimension. Use includingRegex/excludingRegex on query.",
    "all_queries": "The API returns top rows only and excludes anonymised queries; see "
                   "meta.unattributed_share on query-grouped responses.",
}

# Search appearance API values, from Search Console Help 17011259
# ("Performance report (Search results): Dimensions and data groupings"),
# read 2026-09-28. A value outside this set is reported in unknown_values.
_KNOWN_SEARCH_APPEARANCES = frozenset({
    "AMP_TOP_STORIES", "AMP_BLUE_LINK", "AMP_IMAGE_RESULT", "FORUMS", "EDU_Q_AND_A",
    "JOB_DETAILS", "JOB_LISTING", "MATH_SOLVERS", "ACTION", "MERCHANT_LISTINGS",
    "PRACTICE_PROBLEMS", "PRODUCT_SNIPPETS", "TPF_QA", "RECIPE_FEATURE",
    "RECIPE_RICH_SNIPPET", "REVIEW_SNIPPET", "SUBSCRIBED_CONTENT", "TRANSLATED_RESULT",
    "VIDEO", "AMP_STORY",
})

# Enums of searchanalytics.query as this server knows them (live reference,
# last updated 2026-08-11). The live discovery doc is diffed against these.
_KNOWN_DISCOVERY_ENUMS: Dict[str, set] = {
    "type": {"WEB", "IMAGE", "VIDEO", "NEWS", "DISCOVER", "GOOGLE_NEWS"},
    "dataState": {"DATA_STATE_UNSPECIFIED", "FINAL", "ALL", "HOURLY_ALL"},
    "aggregationType": {"AUTO", "BY_PROPERTY", "BY_PAGE", "BY_NEWS_SHOWCASE_PANEL"},
    "filterDimension": {"QUERY", "PAGE", "COUNTRY", "DEVICE", "SEARCH_APPEARANCE"},
}
_DISCOVERY_URL = "https://searchconsole.googleapis.com/$discovery/rest?version=v1"
_DISCOVERY_TTL_SEC = 24 * 3600
_discovery_cache: Dict[str, Any] = {}


def _live_discovery_enums() -> Dict[str, List[str]]:
    """Fetch (daily) the live discovery doc and pull the enums above."""
    hit = _discovery_cache.get("enums")
    if hit and time.time() - hit[0] < _DISCOVERY_TTL_SEC:
        return hit[1]
    import urllib.request
    with urllib.request.urlopen(_DISCOVERY_URL, timeout=10) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    schemas = doc.get("schemas", {})
    req = schemas.get("SearchAnalyticsQueryRequest", {}).get("properties", {})
    filt = schemas.get("ApiDimensionFilter", {}).get("properties", {})
    enums = {
        "type": req.get("type", {}).get("enum", []),
        "dataState": req.get("dataState", {}).get("enum", []),
        "aggregationType": req.get("aggregationType", {}).get("enum", []),
        "filterDimension": filt.get("dimension", {}).get("enum", []),
    }
    _discovery_cache["enums"] = (time.time(), enums)
    return enums


@mcp.tool()
async def gsc_health_check(
    site_url: str,
    *,
    account_alias: Optional[str] = None,
) -> Any:
    """
    One-shot diagnostic for a GSC property. Used at the start of every audit.

    Reports permission/verification, recent data, sitemaps, and (v1.4.0)
    server_version, latest_final_date, per-account auth status, the
    inspection quota estimate, `unknown_values` (search appearances or API
    enum values this server doesn't know — the early warning for
    AI-features API support) and `api_limits` (what the API cannot do).

    Each probe is wrapped independently so one failure
    doesn't poison the rest. Manual actions and security issues are NOT
    exposed by the Search Console API v1 (confirmed — the discovery doc
    only surfaces sites/sitemaps/searchanalytics/urlInspection), so those
    fields are returned as explicit "not available" stubs.

    Args:
        site_url: The GSC site URL (exact match, or sc-domain:example.com for domain properties).
        account_alias: Optional explicit account; omit to auto-resolve.
    """
    result: Dict[str, Any] = {
        "ok": True,
        "site_url": site_url,
        "permission_level": None,
        "verification_state": None,
        "has_recent_data": False,
        "last_data_date": None,
        "sitemaps": {"count": 0, "with_errors": 0, "with_warnings": 0},
        "manual_actions": {
            "available": False,
            "reason": "Not exposed via Search Console API v1",
        },
        "security_issues": {
            "available": False,
            "reason": "Not exposed via Search Console API v1",
        },
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "partial_failures": [],
    }

    try:
        _resolved_alias, service = await get_gsc_service_for_site(
            site_url, account_alias,
        )
        result["account_alias"] = _resolved_alias
    except AccountResolverError as e:
        return e.to_envelope(tool="gsc_health_check")
    except HttpError as e:
        return _http_error_envelope(
            e, tool="gsc_health_check", site_url=site_url
        )
    except Exception as e:
        return _make_error_envelope(
            error=f"auth failed: {type(e).__name__}: {e}",
            hint="Check that `client_secrets.json` is present and that the "
                 "configured account has a valid token; see gsc_list_accounts.",
            tool="gsc_health_check",
        )

    # Track whether any probe actually produced useful data. If all three
    # fail, the health check learned nothing and must return ok=False.
    any_probe_succeeded = False

    # Step 1: sites().get() for permission + verification
    try:
        site_info = await _gsc_execute(
            lambda: service.sites().get(siteUrl=site_url).execute(), step="sites.get",
        )
        result["permission_level"] = site_info.get("permissionLevel")
        verify = site_info.get("siteVerificationInfo", {})
        result["verification_state"] = verify.get("verificationState")
        any_probe_succeeded = True
    except HttpError as e:
        result["partial_failures"].append({
            "step": "sites.get",
            "error": f"HTTP {e.resp.status}: {str(e)[:200]}",
        })
    except Exception as e:
        result["partial_failures"].append({
            "step": "sites.get",
            "error": f"{type(e).__name__}: {e}",
        })

    # Step 2: searchanalytics().query() — find the latest date with data via a
    # 7-day window. Default (ascending) order is fine; we pick the max date.
    try:
        today = _today_pt()
        week_ago = today - timedelta(days=7)
        data_request = {
            "startDate": week_ago.strftime("%Y-%m-%d"),
            "endDate": today.strftime("%Y-%m-%d"),
            "dimensions": ["date"],
            "rowLimit": 7,
        }
        data_response = await _gsc_execute(
            lambda: service.searchanalytics().query(siteUrl=site_url, body=data_request).execute(),
            step="searchanalytics.query",
        )
        rows = data_response.get("rows", [])
        # Filter out rows with missing/empty keys so the max() below can't
        # silently pick an empty string if the API ever returns junk.
        valid_dates = [
            r["keys"][0]
            for r in rows
            if r.get("keys") and r["keys"][0]
        ]
        if valid_dates:
            # ISO date strings sort lex-correctly so max() is safe.
            result["last_data_date"] = max(valid_dates)
            result["has_recent_data"] = True
        # An empty result is still a successful probe — the property simply
        # has no data for the window. Mark the probe as having executed.
        any_probe_succeeded = True
    except HttpError as e:
        result["partial_failures"].append({
            "step": "searchanalytics.query",
            "error": f"HTTP {e.resp.status}: {str(e)[:200]}",
        })
    except Exception as e:
        result["partial_failures"].append({
            "step": "searchanalytics.query",
            "error": f"{type(e).__name__}: {e}",
        })

    # Step 3: sitemaps().list() — count + error/warning totals
    try:
        sitemaps_response = await _gsc_execute(
            lambda: service.sitemaps().list(siteUrl=site_url).execute(), step="sitemaps.list",
        )
        sitemaps = sitemaps_response.get("sitemap", [])
        with_errors = sum(1 for s in sitemaps if int(s.get("errors", 0)) > 0)
        with_warnings = sum(1 for s in sitemaps if int(s.get("warnings", 0)) > 0)
        result["sitemaps"] = {
            "count": len(sitemaps),
            "with_errors": with_errors,
            "with_warnings": with_warnings,
        }
        any_probe_succeeded = True
    except HttpError as e:
        result["partial_failures"].append({
            "step": "sitemaps.list",
            "error": f"HTTP {e.resp.status}: {str(e)[:200]}",
        })
    except Exception as e:
        result["partial_failures"].append({
            "step": "sitemaps.list",
            "error": f"{type(e).__name__}: {e}",
        })

    # v1.4.0 additions. Each is independent and never fails the check.
    result["server_version"] = _SERVER_VERSION
    result["api_limits"] = _API_LIMITS
    cfg = _load_site_config(site_url)
    result["site_config"] = {"present": bool(cfg), "keys": sorted(k for k in cfg if not k.startswith("_"))}

    # Data freshness (dataState=all probe; cached for an hour).
    ctx = _SaContext(site_url, account_alias, _resolved_alias, service)
    try:
        freshness = await _latest_final_date(ctx)
        result["latest_final_date"] = freshness["date"]
        result["latest_final_date_source"] = freshness["source"]
        result["timezone"] = _PT_LABEL
    except Exception as e:  # noqa: BLE001
        result["partial_failures"].append({"step": "freshness_probe", "error": f"{type(e).__name__}: {e}"})

    # Per-account auth status (non-interactive; never opens a browser).
    accounts: List[Dict[str, Any]] = []
    for alias in _list_configured_aliases():
        svc, err = await asyncio.to_thread(_build_service_noninteractive, alias)
        accounts.append({"alias": alias, "auth_ok": svc is not None, "error_code": err})
    result["accounts"] = accounts

    try:
        result["inspection_quota"] = await asyncio.to_thread(_inspection_quota, site_url)
    except Exception as e:  # noqa: BLE001
        result["partial_failures"].append({"step": "inspection_quota", "error": f"{type(e).__name__}: {e}"})

    # Early warning for new API values (e.g. AI-features support): search
    # appearances seen in the last 28 days, and the live discovery doc's
    # enums, each diffed against what this server knows.
    unknown: Dict[str, Any] = {}
    try:
        today = _today_pt()
        appearance = await _gsc_execute(
            lambda: service.searchanalytics().query(siteUrl=site_url, body={
                "startDate": (today - timedelta(days=28)).isoformat(),
                "endDate": today.isoformat(),
                "dimensions": ["searchAppearance"],
                "rowLimit": 100,
            }).execute(),
            step="searchappearance_probe",
        )
        seen = sorted({(r.get("keys") or [""])[0] for r in appearance.get("rows", []) or []} - {""})
        result["search_appearances_seen"] = seen
        unknown["searchAppearance"] = [v for v in seen if v not in _KNOWN_SEARCH_APPEARANCES]
    except Exception as e:  # noqa: BLE001
        result["partial_failures"].append({"step": "searchappearance_probe", "error": f"{type(e).__name__}: {e}"})
    try:
        enums = await asyncio.to_thread(_live_discovery_enums)
        for field, values in enums.items():
            extra = sorted(set(values) - _KNOWN_DISCOVERY_ENUMS.get(field, set()))
            if extra:
                unknown[field] = extra
    except Exception as e:  # noqa: BLE001
        result["partial_failures"].append({"step": "discovery_doc", "error": f"{type(e).__name__}: {e}"})
    result["unknown_values"] = {k: v for k, v in unknown.items() if v}
    ai_like = [
        v for vals in result["unknown_values"].values() for v in vals
        if re.search(r"AI|OVERVIEW|GENERATIVE|GEMINI", v, re.IGNORECASE)
    ]
    if ai_like:
        result["ai_features_signal"] = (
            f"New API values that look AI-related: {ai_like}. The Generative AI features "
            f"report may now be reachable through the API."
        )

    if not any_probe_succeeded:
        result["ok"] = False
        result["error"] = "all health probes failed; see partial_failures"
        result["tool"] = "gsc_health_check"

    return result


# --- Screaming Frog CSV bridge tools ---

@mcp.tool()
async def gsc_load_from_sf_export(
    sf_export_path: str,
    site_url: str,
    include_internal: bool = True,
    session_id: Optional[str] = None,
) -> Any:
    """
    Load a Screaming Frog export folder into an in-memory session for local querying.

    Ingests all search_console_*.csv files and (optionally) internal_all.csv,
    internal_html.csv, internal_pdf.csv. Sessions hold only file paths and
    metadata — rows stream from disk at query time to stay memory-safe on large
    exports. Sessions are process-local and die when the MCP server restarts.

    Args:
        sf_export_path: Absolute path to an SF export folder. The loader accepts
            both the flat layout (CSVs at the root) and the nested layout
            (CSVs under a `search_console/` subfolder).
        site_url: The GSC site URL this export relates to. Echoed back in the
            response; not validated against GSC.
        include_internal: If True (default), also load internal_all.csv /
            internal_html.csv / internal_pdf.csv when present. Set False to
            skip large internal crawl files.
        session_id: Optional explicit session id (useful for idempotent reload
            in tests). A new id is generated if omitted.
    """
    try:
        path = Path(sf_export_path).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            return {
                "ok": False,
                "error": f"path not found or not a directory: {path}",
                "tool": "gsc_load_from_sf_export",
            }

        resolved = _resolve_sf_dir(path)

        # Discover CSVs. Sorted for deterministic test output.
        csv_files: List[Path] = sorted(resolved.glob("search_console_*.csv"))
        if include_internal:
            # Internal crawl CSVs live at the export ROOT in observed SF
            # exports, even when search_console_*.csv files are nested under a
            # search_console/ subfolder. Try the root first, then fall back to
            # the resolved search_console/ dir for forwards-compat with any SF
            # version or custom export that co-locates internals there.
            for internal_name in ("internal_all.csv", "internal_html.csv", "internal_pdf.csv"):
                for candidate in (path / internal_name, resolved / internal_name):
                    if candidate.is_file():
                        csv_files.append(candidate)
                        break

        datasets: Dict[str, Dict[str, Any]] = {}
        warnings: List[str] = []
        loaded_summary: List[Dict[str, Any]] = []

        for csv_path in csv_files:
            dataset_name = csv_path.stem
            try:
                meta = _peek_sf_csv(csv_path)
            except ValueError as e:
                warnings.append(f"{csv_path.name}: {e}")
                continue
            datasets[dataset_name] = meta
            loaded_summary.append({
                "dataset": dataset_name,
                "row_count": meta["row_count"],
                "columns": len(meta["columns"]),
                "empty": meta["empty"],
            })
            if meta["file_size"] > _SF_FILE_SIZE_WARNING_BYTES:
                size_mb = meta["file_size"] / (1024 * 1024)
                warnings.append(
                    f"{csv_path.name} is {size_mb:.1f} MB; queries will stream from disk"
                )

        if not datasets:
            return {
                "ok": False,
                "error": f"no usable CSVs in {resolved}",
                "tool": "gsc_load_from_sf_export",
            }

        if session_id is None:
            session_id = f"sf-{uuid4().hex[:12]}"

        _sf_sessions[session_id] = {
            "session_id": session_id,
            "site_url": site_url,
            "sf_export_path": str(path),
            "loaded_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_date": _extract_snapshot_date(resolved) or _extract_snapshot_date(path),
            "datasets": datasets,
            "warnings": warnings,
        }

        return {
            "ok": True,
            "tool": "gsc_load_from_sf_export",
            "session_id": session_id,
            "site_url": site_url,
            "snapshot_date": _sf_sessions[session_id]["snapshot_date"],
            "sf_export_path": str(path),
            "loaded": loaded_summary,
            "warnings": warnings,
            "meta": {"site_url": site_url, "session_id": session_id},
        }
    except Exception as e:
        return _make_error_envelope(
            error=f"{type(e).__name__}: {e}",
            hint="Verify the SF export directory exists and contains at least "
                 "one `search_console_*.csv`; permissions + encoding must be "
                 "readable by this process.",
            tool="gsc_load_from_sf_export",
        )


@mcp.tool()
async def gsc_query_sf_export(
    session_id: str,
    dataset: str,
    filter: Optional[Dict[str, Any]] = None,
    columns: Optional[List[str]] = None,
    sort_by: Optional[str] = None,
    sort_direction: str = "desc",
    limit: int = 100,
    offset: int = 0,
) -> Any:
    """
    Query a dataset previously loaded via gsc_load_from_sf_export.

    Streams the CSV from disk, applies filter/sort/limit, and returns matched rows.

    Args:
        session_id: Session identifier returned by gsc_load_from_sf_export.
        dataset: Dataset name (filename without .csv, e.g. 'search_console_all',
            'internal_all'). Must match /^[a-z0-9_]+$/ (path traversal guard).
        filter: Optional dict keyed by column name. Values can be scalars
            (eq match) or dicts with {"op": "eq"|"contains"|"gt"|"lt"|"gte"|"lte",
            "value": ...}. Column names are normalized snake_case (e.g. 'address',
            'indexability', 'word_count').
        columns: Optional projection — return only these columns.
        sort_by: Optional column to sort by. Numeric sort is automatic: values
            that coerce to float sort numerically, others fall back to lex.
        sort_direction: 'asc' or 'desc' (default 'desc').
        limit: Max rows to return (default 100).
        offset: Number of matched rows to skip before limit (default 0).

    Note on non-finite values: inf and nan in a numeric column sort into the
    non-numeric sentinel group (always last regardless of direction), but
    remain comparable via gt/lt/gte/lte filters. If you need to exclude them
    from filter results, combine with a bound such as {"op": "lt", "value": 1e308}.
    """
    try:
        if session_id not in _sf_sessions:
            return {
                "ok": False,
                "error": f"unknown session_id: {session_id!r}",
                "tool": "gsc_query_sf_export",
            }
        session = _sf_sessions[session_id]

        if not _ALLOWED_DATASET_RE.match(dataset):
            return {
                "ok": False,
                "error": (
                    f"invalid dataset name: {dataset!r} "
                    "(must match ^[a-z0-9_]+$)"
                ),
                "tool": "gsc_query_sf_export",
            }

        if dataset not in session["datasets"]:
            return {
                "ok": False,
                "error": f"unknown dataset: {dataset!r}",
                "available": sorted(session["datasets"].keys()),
                "tool": "gsc_query_sf_export",
            }

        dataset_meta = session["datasets"][dataset]
        available_columns = dataset_meta["columns"]

        # Validate filter column names up front
        if filter:
            for col in filter.keys():
                if col not in available_columns:
                    return {
                        "ok": False,
                        "error": f"unknown filter column: {col!r}",
                        "available": available_columns,
                        "tool": "gsc_query_sf_export",
                    }

        # Validate sort column
        if sort_by is not None and sort_by not in available_columns:
            return {
                "ok": False,
                "error": f"unknown sort column: {sort_by!r}",
                "available": available_columns,
                "tool": "gsc_query_sf_export",
            }

        # Validate projection
        if columns is not None:
            missing = [c for c in columns if c not in available_columns]
            if missing:
                return {
                    "ok": False,
                    "error": f"unknown projection columns: {missing!r}",
                    "available": available_columns,
                    "tool": "gsc_query_sf_export",
                }

        # Input validation for pagination + sort direction. The old slice-based
        # code tolerated negative offset/limit via Python slice semantics; the
        # new streaming path does not. Reject explicit nonsense inputs rather
        # than create an undocumented contract.
        if offset < 0 or limit < 0:
            return {
                "ok": False,
                "error": "offset and limit must be >= 0",
                "tool": "gsc_query_sf_export",
            }
        direction = sort_direction.lower()
        if direction not in ("asc", "desc"):
            return {
                "ok": False,
                "error": "sort_direction must be 'asc' or 'desc'",
                "tool": "gsc_query_sf_export",
            }

        # Three execution paths, each memory-bounded to the paginated window:
        #   1. limit == 0           → counts-only, stream + count
        #   2. sort_by is None      → streaming short-circuit (O(1) beyond window)
        #   3. sort_by is not None  → heapq-bounded top-K with offset + limit cap
        sliced: List[Dict[str, str]] = []
        total_matched = 0

        try:
            if limit == 0:
                # Counts-only query: no buffer, no heap.
                for row in _stream_sf_csv(dataset_meta):
                    if filter is None or _apply_sf_filter(row, filter):
                        total_matched += 1
            elif sort_by is None:
                # Streaming short-circuit: collect only the paginated window,
                # count everything else for total_matched.
                for row in _stream_sf_csv(dataset_meta):
                    if filter is not None and not _apply_sf_filter(row, filter):
                        continue
                    if total_matched >= offset and len(sliced) < limit:
                        sliced.append(row)
                    total_matched += 1
            else:
                # Bounded top-K via heapq. Memory capped at offset + limit rows.
                k = offset + limit
                reverse = (direction == "desc")

                def _sort_key(row: Dict[str, str]) -> Tuple[int, Any]:
                    v = _to_float_or_none(row.get(sort_by, ""))
                    # Treat inf/nan as non-numeric so they don't produce
                    # nondeterministic placement alongside real values.
                    if v is not None and not math.isfinite(v):
                        v = None
                    if reverse:
                        # nlargest picks largest keys first → numerics want
                        # group 1 so they outrank the non-numeric fallback;
                        # non-numerics end up last regardless of direction.
                        return (1, v) if v is not None else (0, str(row.get(sort_by, "")))
                    # nsmallest picks smallest keys first → numerics want group 0.
                    return (0, v) if v is not None else (1, str(row.get(sort_by, "")))

                def _iter_counted() -> Iterator[Dict[str, str]]:
                    nonlocal total_matched
                    for row in _stream_sf_csv(dataset_meta):
                        if filter is not None and not _apply_sf_filter(row, filter):
                            continue
                        total_matched += 1
                        yield row

                if reverse:
                    top_rows = heapq.nlargest(k, _iter_counted(), key=_sort_key)
                else:
                    top_rows = heapq.nsmallest(k, _iter_counted(), key=_sort_key)

                sliced = top_rows[offset : offset + limit]
        except FileNotFoundError:
            return {
                "ok": False,
                "error": (
                    f"CSV file missing from disk: {dataset_meta['file']}. "
                    "The SF export folder may have been moved or deleted after load."
                ),
                "tool": "gsc_query_sf_export",
            }
        except ValueError as e:
            # Filter validation errors from _apply_sf_filter (bad op, etc.)
            return {
                "ok": False,
                "error": str(e),
                "tool": "gsc_query_sf_export",
            }

        # Project columns if requested
        if columns is not None:
            sliced = [{c: row.get(c, "") for c in columns} for row in sliced]
            returned_columns = list(columns)
        else:
            returned_columns = list(available_columns)

        return {
            "ok": True,
            "tool": "gsc_query_sf_export",
            "session_id": session_id,
            "dataset": dataset,
            "total_matched": total_matched,
            "offset": offset,
            "limit": limit,
            "truncated": total_matched > offset + len(sliced),
            "columns": returned_columns,
            "rows": sliced,
            "meta": {"session_id": session_id, "dataset": dataset},
        }
    except Exception as e:
        return _make_error_envelope(
            error=f"{type(e).__name__}: {e}",
            hint="Inspect the session with `gsc_load_from_sf_export` output for "
                 "dataset and column names; streaming may fail on corrupt CSVs.",
            tool="gsc_query_sf_export",
        )


@mcp.tool()
async def gsc_get_creator_info() -> str:
    """
    Provides information about Amin Foroutan, the creator of the MCP-GSC tool.
    """
    creator_info = """
# About the Creator: Amin Foroutan

Amin Foroutan is an SEO consultant with over a decade of experience, specializing in technical SEO, Python-driven tools, and data analysis for SEO performance.

## Connect with Amin:

- **LinkedIn**: [Amin Foroutan](https://www.linkedin.com/in/ma-foroutan/)
- **Personal Website**: [aminforoutan.com](https://aminforoutan.com/)
- **YouTube**: [Amin Forout](https://www.youtube.com/channel/UCW7tPXg-rWdH4YzLrcAdBIw)
- **X (Twitter)**: [@aminfseo](https://x.com/aminfseo)

## Notable Projects:

Amin has created several popular SEO tools including:
- Advanced GSC Visualizer (6.4K+ users)
- SEO Render Insight Tool (3.5K+ users)
- Google AI Overview Impact Analysis (1.2K+ users)
- Google AI Overview Citation Analysis (900+ users)
- SEMRush Enhancer (570+ users)
- SEO Page Inspector (115+ users)

## Expertise:

Amin combines technical SEO knowledge with programming skills to create innovative solutions for SEO challenges.
"""
    return creator_info

def main() -> None:
    """Console-script entry point (see [project.scripts] in pyproject.toml).

    Installed non-editable, the server is launched as ``gsc-mcp-server``
    (or ``python -m gsc_server``) with no on-share path argument.
    """
    # Start the MCP server on stdio transport
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

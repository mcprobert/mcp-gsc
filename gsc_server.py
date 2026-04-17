from typing import Any, Dict, Iterator, List, Optional, Tuple
import asyncio
import os
import json
import re
import shutil
import csv
import heapq
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

URL_INSPECTION_PACING_SEC = 0.1


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

# MCP
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("gsc-server")

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

# Token file path for storing OAuth tokens
TOKEN_FILE = os.path.join(SCRIPT_DIR, "token.json")

# Environment variable to skip OAuth authentication
SKIP_OAUTH = os.environ.get("GSC_SKIP_OAUTH", "").lower() in ("true", "1", "yes")

SCOPES = ["https://www.googleapis.com/auth/webmasters"]
OAUTH_SCOPES = SCOPES + [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

# Multi-account support
ACCOUNTS_DIR = os.path.join(SCRIPT_DIR, "accounts")
ACCOUNTS_MANIFEST = os.path.join(ACCOUNTS_DIR, "accounts.json")
_active_account: Optional[str] = None

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
        # Resolve relative paths against SCRIPT_DIR
        if not os.path.isabs(token_path):
            token_path = os.path.join(SCRIPT_DIR, token_path)
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

def _maybe_migrate_legacy_token() -> None:
    """One-time migration of existing token.json into accounts/default/token.json.
    Deferred to first use — no network I/O, no blocking at import time."""
    global _migration_checked, _active_account
    if _migration_checked:
        return
    _migration_checked = True

    manifest = _load_manifest()
    # Skip if accounts already exist
    if manifest.get("accounts"):
        return
    # Check for legacy token
    if not os.path.exists(TOKEN_FILE):
        return
    # Copy token (original left in place for safe rollback)
    default_dir = os.path.join(ACCOUNTS_DIR, "default")
    os.makedirs(default_dir, exist_ok=True)
    dest = os.path.join(default_dir, "token.json")
    shutil.copy2(TOKEN_FILE, dest)

    manifest = {
        "active_account": "default",
        "accounts": {
            "default": {
                "alias": "default",
                "email": None,
                "token_file": "accounts/default/token.json",
                "added_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    }
    _save_manifest(manifest)
    _active_account = "default"


# --- Shared date helper (used by landing-page tools) ---

def _parse_gsc_date(s: str) -> str:
    """Accepts 'today', 'yesterday', 'Ndaysago' (case-insensitive), or 'YYYY-MM-DD'.
    Returns an ISO date string (YYYY-MM-DD). Raises ValueError on unrecognised input.
    """
    if not isinstance(s, str) or not s.strip():
        raise ValueError(f"invalid date: {s!r}")
    normalized = s.strip().lower()
    today = datetime.now().date()
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
    Resolves token file: explicit param → active account → legacy fallback.
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
                with open(token_file, 'w') as token:
                    token.write(creds.to_json())
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
            os.makedirs(os.path.dirname(token_file), exist_ok=True)
            with open(token_file, 'w') as token:
                token.write(creds.to_json())

    # Build and return the service
    return build("searchconsole", "v1", credentials=creds)

@mcp.tool()
async def list_properties(
    name_contains: Optional[str] = None,
    limit: int = 50,
) -> str:
    """List the GSC properties the active account can see.

    Args:
        name_contains: Optional case-insensitive substring filter on the
            site URL (e.g. `name_contains='whitehat'`). Use this first on
            agency accounts with many properties.
        limit: Max properties to return (default 50; clamped to [1, 1000]).
    """
    try:
        limit = max(1, min(int(limit), 1000))

        service = get_gsc_service()
        site_list = service.sites().list().execute()

        sites = site_list.get("siteEntry", [])

        if name_contains:
            needle = name_contains.lower()
            sites = [s for s in sites if needle in s.get("siteUrl", "").lower()]

        if not sites:
            if name_contains:
                return f"No Search Console properties matching {name_contains!r}."
            return "No Search Console properties found."

        total_available = len(sites)
        truncated = total_available > limit
        shown_sites = sites[:limit]

        # Format the results for easy reading
        lines = []
        for site in shown_sites:
            site_url = site.get("siteUrl", "Unknown")
            permission = site.get("permissionLevel", "Unknown permission")
            lines.append(f"- {site_url} ({permission})")

        if truncated:
            lines.append("")
            lines.append(
                f"⚠ Showing first {limit} of {total_available} properties. "
                f"Pass `name_contains='…'` to filter or raise `limit` "
                f"(max 1000) to see more."
            )

        return "\n".join(lines)
    except FileNotFoundError as e:
        return (
            "Error: Service account credentials file not found.\n\n"
            "To access Google Search Console, please:\n"
            "1. Create a service account in Google Cloud Console\n"
            "2. Download the JSON credentials file\n"
            "3. Save it as 'service_account_credentials.json' in the same directory as this script\n"
            "4. Share your GSC properties with the service account email"
        )
    except Exception as e:
        return f"Error retrieving properties: {str(e)}"

@mcp.tool()
async def add_site(site_url: str) -> str:
    """
    Add a site to your Search Console properties.
    
    Args:
        site_url: The URL of the site to add (must be exact match e.g. https://example.com, or https://www.example.com, or https://subdomain.example.com/path/, for domain properties use format: sc-domain:example.com)
    """
    try:
        service = get_gsc_service()
        
        # Add the site
        response = service.sites().add(siteUrl=site_url).execute()
        
        # Format the response
        result_lines = [f"Site {site_url} has been added to Search Console."]
        
        # Add permission level if available
        if "permissionLevel" in response:
            result_lines.append(f"Permission level: {response['permissionLevel']}")
        
        return "\n".join(result_lines)
    except HttpError as e:
        error_content = json.loads(e.content.decode('utf-8'))
        error_details = error_content.get('error', {})
        error_code = e.resp.status
        error_message = error_details.get('message', str(e))
        error_reason = error_details.get('errors', [{}])[0].get('reason', '')
        
        if error_code == 409:
            return f"Site {site_url} is already added to Search Console."
        elif error_code == 403:
            if error_reason == 'forbidden':
                return f"Error: You don't have permission to add this site. Please verify ownership first."
            elif error_reason == 'quotaExceeded':
                return f"Error: API quota exceeded. Please try again later."
            else:
                return f"Error: Permission denied. {error_message}"
        elif error_code == 400:
            if error_reason == 'invalidParameter':
                return f"Error: Invalid site URL format. Please check the URL format and try again."
            else:
                return f"Error: Bad request. {error_message}"
        elif error_code == 401:
            return f"Error: Unauthorized. Please check your credentials."
        elif error_code == 429:
            return f"Error: Too many requests. Please try again later."
        elif error_code == 500:
            return f"Error: Internal server error from Google Search Console API. Please try again later."
        elif error_code == 503:
            return f"Error: Service unavailable. Google Search Console API is currently down. Please try again later."
        else:
            return f"Error adding site (HTTP {error_code}): {error_message}"
    except Exception as e:
        return f"Error adding site: {str(e)}"

@mcp.tool()
async def delete_site(site_url: str) -> str:
    """
    Remove a site from your Search Console properties.
    
    Args:
        site_url: The URL of the site to remove (must be exact match e.g. https://example.com, or https://www.example.com, or https://subdomain.example.com/path/, for domain properties use format: sc-domain:example.com)
    """
    try:
        service = get_gsc_service()
        
        # Delete the site
        service.sites().delete(siteUrl=site_url).execute()
        
        return f"Site {site_url} has been removed from Search Console."
    except HttpError as e:
        error_content = json.loads(e.content.decode('utf-8'))
        error_details = error_content.get('error', {})
        error_code = e.resp.status
        error_message = error_details.get('message', str(e))
        error_reason = error_details.get('errors', [{}])[0].get('reason', '')
        
        if error_code == 404:
            return f"Site {site_url} was not found in Search Console."
        elif error_code == 403:
            if error_reason == 'forbidden':
                return f"Error: You don't have permission to remove this site."
            elif error_reason == 'quotaExceeded':
                return f"Error: API quota exceeded. Please try again later."
            else:
                return f"Error: Permission denied. {error_message}"
        elif error_code == 400:
            if error_reason == 'invalidParameter':
                return f"Error: Invalid site URL format. Please check the URL format and try again."
            else:
                return f"Error: Bad request. {error_message}"
        elif error_code == 401:
            return f"Error: Unauthorized. Please check your credentials."
        elif error_code == 429:
            return f"Error: Too many requests. Please try again later."
        elif error_code == 500:
            return f"Error: Internal server error from Google Search Console API. Please try again later."
        elif error_code == 503:
            return f"Error: Service unavailable. Google Search Console API is currently down. Please try again later."
        else:
            return f"Error removing site (HTTP {error_code}): {error_message}"
    except Exception as e:
        return f"Error removing site: {str(e)}"

@mcp.tool()
async def get_search_analytics(
    site_url: str,
    days: int = 28,
    dimensions: str = "query",
    row_limit: int = 100,
) -> str:
    """Overview of a GSC property's top rows. Pick me for a single-dimension
    summary; use `get_advanced_search_analytics` for sorting/filtering, or
    `get_search_by_page_query` to break queries down for one page.

    Args:
        site_url: GSC site URL (exact match; for domain properties use `sc-domain:example.com`).
        days: Look-back window (default 28, clamped to min 1).
        dimensions: Comma-separated GSC dimensions (default `query`; options: query, page, device, country, date).
        row_limit: Max rows returned (default 100; clamped to [1, 25000]).
    """
    try:
        # Clamp inputs so the API request is always well-formed.
        days = max(int(days), 1)
        row_limit = max(1, min(int(row_limit), 25000))

        service = get_gsc_service()

        # Calculate date range
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)

        # Parse dimensions
        dimension_list = [d.strip() for d in dimensions.split(",")]

        # Build request
        request = {
            "startDate": start_date.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
            "dimensions": dimension_list,
            "rowLimit": row_limit,
        }

        # Execute request
        response = service.searchanalytics().query(siteUrl=site_url, body=request).execute()

        rows = response.get("rows") or []
        if not rows:
            return f"No search analytics data found for {site_url} in the last {days} days."

        # Format results
        result_lines = [f"Search analytics for {site_url} (last {days} days):"]
        result_lines.append("\n" + "-" * 80 + "\n")

        # Create header based on dimensions
        header = [dim.capitalize() for dim in dimension_list]
        header.extend(["Clicks", "Impressions", "CTR", "Position"])
        result_lines.append(" | ".join(header))
        result_lines.append("-" * 80)

        # Add data rows
        for row in rows:
            data = [dim_value[:100] for dim_value in row.get("keys", [])]
            data.append(str(row.get("clicks", 0)))
            data.append(str(row.get("impressions", 0)))
            data.append(f"{row.get('ctr', 0) * 100:.2f}%")
            data.append(f"{row.get('position', 0):.1f}")
            result_lines.append(" | ".join(data))

        if len(rows) >= row_limit:
            result_lines.append("-" * 80)
            result_lines.append(
                f"⚠ Showing {row_limit} of possibly-more rows. Pass a larger "
                f"`row_limit` (max 25000) or use `get_advanced_search_analytics` "
                f"with `start_row` to paginate."
            )

        return "\n".join(result_lines)
    except Exception as e:
        return f"Error retrieving search analytics: {str(e)}"

@mcp.tool()
async def get_site_details(site_url: str) -> str:
    """
    Get detailed information about a specific Search Console property.
    
    Args:
        site_url: The URL of the site in Search Console (must be exact match)
    """
    try:
        service = get_gsc_service()
        
        # Get site details
        site_info = service.sites().get(siteUrl=site_url).execute()
        
        # Format the results
        result_lines = [f"Site details for {site_url}:"]
        result_lines.append("-" * 50)
        
        # Add basic info
        result_lines.append(f"Permission level: {site_info.get('permissionLevel', 'Unknown')}")
        
        # Add verification info if available
        if "siteVerificationInfo" in site_info:
            verify_info = site_info["siteVerificationInfo"]
            result_lines.append(f"Verification state: {verify_info.get('verificationState', 'Unknown')}")
            
            if "verifiedUser" in verify_info:
                result_lines.append(f"Verified by: {verify_info['verifiedUser']}")
                
            if "verificationMethod" in verify_info:
                result_lines.append(f"Verification method: {verify_info['verificationMethod']}")
        
        # Add ownership info if available
        if "ownershipInfo" in site_info:
            owner_info = site_info["ownershipInfo"]
            result_lines.append("\nOwnership Information:")
            result_lines.append(f"Owner: {owner_info.get('owner', 'Unknown')}")
            
            if "verificationMethod" in owner_info:
                result_lines.append(f"Ownership verification: {owner_info['verificationMethod']}")
        
        return "\n".join(result_lines)
    except Exception as e:
        return f"Error retrieving site details: {str(e)}"

@mcp.tool()
async def get_sitemaps(site_url: str) -> str:
    """
    List all sitemaps for a specific Search Console property.
    
    Args:
        site_url: The URL of the site in Search Console (must be exact match)
    """
    try:
        service = get_gsc_service()
        
        # Get sitemaps list
        sitemaps = service.sitemaps().list(siteUrl=site_url).execute()
        
        if not sitemaps.get("sitemap"):
            return f"No sitemaps found for {site_url}."
        
        # Format the results
        result_lines = [f"Sitemaps for {site_url}:"]
        result_lines.append("-" * 80)
        
        # Header
        result_lines.append("Path | Last Downloaded | Status | Indexed URLs | Errors")
        result_lines.append("-" * 80)
        
        # Add each sitemap
        for sitemap in sitemaps.get("sitemap", []):
            path = sitemap.get("path", "Unknown")
            last_downloaded = sitemap.get("lastDownloaded", "Never")
            
            # Format last downloaded date if it exists
            if last_downloaded != "Never":
                try:
                    # Convert to more readable format
                    dt = datetime.fromisoformat(last_downloaded.replace('Z', '+00:00'))
                    last_downloaded = dt.strftime("%Y-%m-%d %H:%M")
                except:
                    pass
            
            # GSC Sitemaps API returns errors/warnings as strings
            # (e.g. "0", "7"), so coerce before any numeric compare.
            try:
                errors = int(sitemap.get("errors", 0) or 0)
            except (TypeError, ValueError):
                errors = 0
            try:
                warnings = int(sitemap.get("warnings", 0) or 0)
            except (TypeError, ValueError):
                warnings = 0

            status = "Has errors" if errors > 0 else "Valid"
            
            # Get contents if available
            indexed_urls = "N/A"
            if "contents" in sitemap:
                for content in sitemap["contents"]:
                    if content.get("type") == "web":
                        indexed_urls = content.get("submitted", "0")
                        break
            
            result_lines.append(f"{path} | {last_downloaded} | {status} | {indexed_urls} | {errors}")
        
        return "\n".join(result_lines)
    except Exception as e:
        return f"Error retrieving sitemaps: {str(e)}"

@mcp.tool()
async def inspect_url_enhanced(site_url: str, page_url: str) -> str:
    """Inspect a single URL's indexing status + rich results in Google.
    Pick me for one URL; use `batch_url_inspection` for up to 10 URLs
    or `check_indexing_issues` to bucket several URLs by problem type.

    Args:
        site_url: GSC site URL (exact match; `sc-domain:example.com`
            for domain properties).
        page_url: The URL to inspect.
    """
    try:
        service = get_gsc_service()
        
        # Build request
        request = {
            "inspectionUrl": page_url,
            "siteUrl": site_url
        }
        
        # Execute request
        response = service.urlInspection().index().inspect(body=request).execute()
        
        if not response or "inspectionResult" not in response:
            return f"No inspection data found for {page_url}."
        
        inspection = response["inspectionResult"]
        
        # Format the results
        result_lines = [f"URL Inspection for {page_url}:"]
        result_lines.append("-" * 80)
        
        # Add inspection result link if available
        if "inspectionResultLink" in inspection:
            result_lines.append(f"Search Console Link: {inspection['inspectionResultLink']}")
            result_lines.append("-" * 80)
        
        # Indexing status section
        index_status = inspection.get("indexStatusResult", {})
        verdict = index_status.get("verdict", "UNKNOWN")
        
        result_lines.append(f"Indexing Status: {verdict}")
        
        # Coverage state
        if "coverageState" in index_status:
            result_lines.append(f"Coverage: {index_status['coverageState']}")
        
        # Last crawl
        if "lastCrawlTime" in index_status:
            try:
                crawl_time = datetime.fromisoformat(index_status["lastCrawlTime"].replace('Z', '+00:00'))
                result_lines.append(f"Last Crawled: {crawl_time.strftime('%Y-%m-%d %H:%M')}")
            except:
                result_lines.append(f"Last Crawled: {index_status['lastCrawlTime']}")
        
        # Page fetch
        if "pageFetchState" in index_status:
            result_lines.append(f"Page Fetch: {index_status['pageFetchState']}")
        
        # Robots.txt status
        if "robotsTxtState" in index_status:
            result_lines.append(f"Robots.txt: {index_status['robotsTxtState']}")
        
        # Indexing state
        if "indexingState" in index_status:
            result_lines.append(f"Indexing State: {index_status['indexingState']}")
        
        # Canonical information
        if "googleCanonical" in index_status:
            result_lines.append(f"Google Canonical: {index_status['googleCanonical']}")
        
        if "userCanonical" in index_status and index_status.get("userCanonical") != index_status.get("googleCanonical"):
            result_lines.append(f"User Canonical: {index_status['userCanonical']}")
        
        # Crawled as
        if "crawledAs" in index_status:
            result_lines.append(f"Crawled As: {index_status['crawledAs']}")
        
        # Referring URLs
        if "referringUrls" in index_status and index_status["referringUrls"]:
            result_lines.append("\nReferring URLs:")
            for url in index_status["referringUrls"][:5]:  # Limit to 5 examples
                result_lines.append(f"- {url}")
            
            if len(index_status["referringUrls"]) > 5:
                result_lines.append(f"... and {len(index_status['referringUrls']) - 5} more")
        
        # Rich results
        if "richResultsResult" in inspection:
            rich = inspection["richResultsResult"]
            result_lines.append(f"\nRich Results: {rich.get('verdict', 'UNKNOWN')}")
            
            if "detectedItems" in rich and rich["detectedItems"]:
                result_lines.append("Detected Rich Result Types:")
                
                for item in rich["detectedItems"]:
                    rich_type = item.get("richResultType", "Unknown")
                    result_lines.append(f"- {rich_type}")
                    
                    # If there are items with names, show them
                    if "items" in item and item["items"]:
                        for i, subitem in enumerate(item["items"][:3]):  # Limit to 3 examples
                            if "name" in subitem:
                                result_lines.append(f"  • {subitem['name']}")
                        
                        if len(item["items"]) > 3:
                            result_lines.append(f"  • ... and {len(item['items']) - 3} more items")
            
            # Check for issues
            if "richResultsIssues" in rich and rich["richResultsIssues"]:
                result_lines.append("\nRich Results Issues:")
                for issue in rich["richResultsIssues"]:
                    severity = issue.get("severity", "Unknown")
                    message = issue.get("message", "Unknown issue")
                    result_lines.append(f"- [{severity}] {message}")
        
        return "\n".join(result_lines)
    except Exception as e:
        return f"Error inspecting URL: {str(e)}"

@mcp.tool()
async def batch_url_inspection(
    site_url: str,
    urls: str = "",
    from_session: Optional[str] = None,
    dataset: str = "search_console_all",
    offset: int = 0,
    limit: int = 10,
) -> str:
    """Inspect up to 10 URLs in batch (URL Inspection API quota limit).
    Pick me when you have several URLs and want the same 4-field
    per-URL output; use `inspect_url_enhanced` for a single URL with
    full detail, or `check_indexing_issues` to bucket URLs by problem
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
    """
    try:
        # --- Phase 1: resolve URL list (no network) ---
        # Session/input validation must fail fast WITHOUT authenticating so
        # session errors don't get masked behind OAuth failures.
        clamp_note = ""
        next_offset_note = ""
        if from_session is not None:
            if from_session not in _sf_sessions:
                return f"Unknown SF session_id: {from_session!r}"
            session = _sf_sessions[from_session]
            if not _ALLOWED_DATASET_RE.match(dataset):
                return f"Invalid dataset name: {dataset!r} (must match ^[a-z0-9_]+$)"
            if dataset not in session["datasets"]:
                available = sorted(session["datasets"].keys())
                return f"Unknown dataset {dataset!r} in session {from_session!r}. Available: {available}"
            dataset_meta = session["datasets"][dataset]
            if "address" not in dataset_meta["columns"]:
                return (
                    f"Dataset {dataset!r} has no 'address' column. "
                    f"Available columns: {dataset_meta['columns']}"
                )

            # Explicit pagination validation. Reject limit<1 and negative
            # offset rather than silently clamping to 1 (the old code did
            # min(max(1, limit), 10) which hid these errors).
            if offset < 0:
                return f"Invalid offset: {offset}. Must be >= 0."
            if limit < 1:
                return (
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
            next_offset_note = f"\nNext offset: {next_offset}"
        else:
            # Parse URLs from the `urls` string (original behavior).
            url_list = [url.strip() for url in urls.split('\n') if url.strip()]

        if not url_list:
            return "No URLs provided for inspection."

        if len(url_list) > 10:
            return f"Too many URLs provided ({len(url_list)}). Please limit to 10 URLs per batch to avoid API quota issues."

        # --- Phase 2: authenticate and inspect ---
        service = get_gsc_service()

        # Process each URL
        results = []

        for i, page_url in enumerate(url_list):
            if i > 0 and URL_INSPECTION_PACING_SEC > 0:
                await asyncio.sleep(URL_INSPECTION_PACING_SEC)

            request = {
                "inspectionUrl": page_url,
                "siteUrl": site_url
            }

            try:
                response = service.urlInspection().index().inspect(body=request).execute()
                
                if not response or "inspectionResult" not in response:
                    results.append(f"{page_url}: No inspection data found")
                    continue
                
                inspection = response["inspectionResult"]
                index_status = inspection.get("indexStatusResult", {})
                
                # Get key information
                verdict = index_status.get("verdict", "UNKNOWN")
                coverage = index_status.get("coverageState", "Unknown")
                last_crawl = "Never"
                
                if "lastCrawlTime" in index_status:
                    try:
                        crawl_time = datetime.fromisoformat(index_status["lastCrawlTime"].replace('Z', '+00:00'))
                        last_crawl = crawl_time.strftime('%Y-%m-%d')
                    except:
                        last_crawl = index_status["lastCrawlTime"]
                
                # Check for rich results
                rich_results = "None"
                if "richResultsResult" in inspection:
                    rich = inspection["richResultsResult"]
                    if rich.get("verdict") == "PASS" and "detectedItems" in rich and rich["detectedItems"]:
                        rich_types = [item.get("richResultType", "Unknown") for item in rich["detectedItems"]]
                        rich_results = ", ".join(rich_types)
                
                # Format result
                results.append(f"{page_url}:\n  Status: {verdict} - {coverage}\n  Last Crawl: {last_crawl}\n  Rich Results: {rich_results}\n")
            
            except Exception as e:
                results.append(f"{page_url}: Error - {str(e)}")

        # Combine results
        header = clamp_note + f"Batch URL Inspection Results for {site_url}:\n\n"
        return header + "\n".join(results) + next_offset_note

    except Exception as e:
        return f"Error performing batch inspection: {str(e)}"

@mcp.tool()
async def check_indexing_issues(site_url: str, urls: str) -> str:
    """Bucket up to 10 URLs by indexing problem (not-indexed, canonical
    conflict, robots-blocked, fetch failure, indexed). Pick me when you
    want a triage summary across several URLs; use `inspect_url_enhanced`
    for one URL in full detail, or `batch_url_inspection` for uniform
    per-URL output.

    Args:
        site_url: GSC site URL (exact match; `sc-domain:example.com`
            for domain properties).
        urls: Newline-separated URLs (max 10).
    """
    try:
        service = get_gsc_service()
        
        # Parse URLs
        url_list = [url.strip() for url in urls.split('\n') if url.strip()]
        
        if not url_list:
            return "No URLs provided for inspection."
        
        if len(url_list) > 10:
            return f"Too many URLs provided ({len(url_list)}). Please limit to 10 URLs per batch to avoid API quota issues."
        
        # Track issues by category
        issues_summary = {
            "not_indexed": [],
            "canonical_issues": [],
            "robots_blocked": [],
            "fetch_issues": [],
            "indexed": []
        }
        
        # Process each URL
        for i, page_url in enumerate(url_list):
            if i > 0 and URL_INSPECTION_PACING_SEC > 0:
                await asyncio.sleep(URL_INSPECTION_PACING_SEC)

            request = {
                "inspectionUrl": page_url,
                "siteUrl": site_url
            }

            try:
                response = service.urlInspection().index().inspect(body=request).execute()
                
                if not response or "inspectionResult" not in response:
                    issues_summary["not_indexed"].append(f"{page_url} - No inspection data found")
                    continue
                
                inspection = response["inspectionResult"]
                index_status = inspection.get("indexStatusResult", {})
                
                # Check indexing status
                verdict = index_status.get("verdict", "UNKNOWN")
                coverage = index_status.get("coverageState", "Unknown")
                
                if verdict != "PASS" or "not indexed" in coverage.lower() or "excluded" in coverage.lower():
                    issues_summary["not_indexed"].append(f"{page_url} - {coverage}")
                else:
                    issues_summary["indexed"].append(page_url)
                
                # Check canonical issues
                google_canonical = index_status.get("googleCanonical", "")
                user_canonical = index_status.get("userCanonical", "")
                
                if google_canonical and user_canonical and google_canonical != user_canonical:
                    issues_summary["canonical_issues"].append(
                        f"{page_url} - Google chose: {google_canonical} instead of user-declared: {user_canonical}"
                    )
                
                # Check robots.txt status
                robots_state = index_status.get("robotsTxtState", "")
                if robots_state == "BLOCKED":
                    issues_summary["robots_blocked"].append(page_url)
                
                # Check fetch issues
                fetch_state = index_status.get("pageFetchState", "")
                if fetch_state != "SUCCESSFUL":
                    issues_summary["fetch_issues"].append(f"{page_url} - {fetch_state}")
            
            except Exception as e:
                issues_summary["not_indexed"].append(f"{page_url} - Error: {str(e)}")
        
        # Format results
        result_lines = [f"Indexing Issues Report for {site_url}:"]
        result_lines.append("-" * 80)
        
        # Summary counts
        result_lines.append(f"Total URLs checked: {len(url_list)}")
        result_lines.append(f"Indexed: {len(issues_summary['indexed'])}")
        result_lines.append(f"Not indexed: {len(issues_summary['not_indexed'])}")
        result_lines.append(f"Canonical issues: {len(issues_summary['canonical_issues'])}")
        result_lines.append(f"Robots.txt blocked: {len(issues_summary['robots_blocked'])}")
        result_lines.append(f"Fetch issues: {len(issues_summary['fetch_issues'])}")
        result_lines.append("-" * 80)
        
        # Detailed issues
        if issues_summary["not_indexed"]:
            result_lines.append("\nNot Indexed URLs:")
            for issue in issues_summary["not_indexed"]:
                result_lines.append(f"- {issue}")
        
        if issues_summary["canonical_issues"]:
            result_lines.append("\nCanonical Issues:")
            for issue in issues_summary["canonical_issues"]:
                result_lines.append(f"- {issue}")
        
        if issues_summary["robots_blocked"]:
            result_lines.append("\nRobots.txt Blocked URLs:")
            for url in issues_summary["robots_blocked"]:
                result_lines.append(f"- {url}")
        
        if issues_summary["fetch_issues"]:
            result_lines.append("\nFetch Issues:")
            for issue in issues_summary["fetch_issues"]:
                result_lines.append(f"- {issue}")
        
        return "\n".join(result_lines)
    
    except Exception as e:
        return f"Error checking indexing issues: {str(e)}"

@mcp.tool()
async def get_performance_overview(site_url: str, days: int = 28) -> str:
    """
    Get a performance overview for a specific property.
    
    Args:
        site_url: The URL of the site in Search Console (must be exact match)
        days: Number of days to look back (default: 28)
    """
    try:
        service = get_gsc_service()
        
        # Calculate date range
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        
        # Get total metrics
        total_request = {
            "startDate": start_date.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
            "dimensions": [],  # No dimensions for totals
            "rowLimit": 1
        }
        
        total_response = service.searchanalytics().query(siteUrl=site_url, body=total_request).execute()
        
        # Get by date for trend
        date_request = {
            "startDate": start_date.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
            "dimensions": ["date"],
            "rowLimit": days
        }
        
        date_response = service.searchanalytics().query(siteUrl=site_url, body=date_request).execute()
        
        # Format results
        result_lines = [f"Performance Overview for {site_url} (last {days} days):"]
        result_lines.append("-" * 80)
        
        # Add total metrics
        if total_response.get("rows"):
            row = total_response["rows"][0]
            result_lines.append(f"Total Clicks: {row.get('clicks', 0):,}")
            result_lines.append(f"Total Impressions: {row.get('impressions', 0):,}")
            result_lines.append(f"Average CTR: {row.get('ctr', 0) * 100:.2f}%")
            result_lines.append(f"Average Position: {row.get('position', 0):.1f}")
        else:
            result_lines.append("No data available for the selected period.")
            return "\n".join(result_lines)
        
        # Add trend data
        if date_response.get("rows"):
            result_lines.append("\nDaily Trend:")
            result_lines.append("Date | Clicks | Impressions | CTR | Position")
            result_lines.append("-" * 80)
            
            # Sort by date
            sorted_rows = sorted(date_response["rows"], key=lambda x: x["keys"][0])
            
            for row in sorted_rows:
                date_str = row["keys"][0]
                # Format date from YYYY-MM-DD to MM/DD
                try:
                    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                    date_formatted = date_obj.strftime("%m/%d")
                except:
                    date_formatted = date_str
                
                clicks = row.get("clicks", 0)
                impressions = row.get("impressions", 0)
                ctr = row.get("ctr", 0) * 100
                position = row.get("position", 0)
                
                result_lines.append(f"{date_formatted} | {clicks:.0f} | {impressions:.0f} | {ctr:.2f}% | {position:.1f}")
        
        return "\n".join(result_lines)
    except Exception as e:
        return f"Error retrieving performance overview: {str(e)}"

@mcp.tool()
async def get_advanced_search_analytics(
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
) -> str:
    """GSC search analytics with sorting, filtering, and pagination. Pick me
    when you need more than a plain top-N summary; use `get_search_analytics`
    for a quick overview or `get_search_by_page_query` to break one page
    down by query.

    Args:
        site_url: GSC site URL (exact match).
        start_date: YYYY-MM-DD (defaults to 28 days ago).
        end_date: YYYY-MM-DD (defaults to today).
        dimensions: Comma-separated (e.g. "query,page,device").
        search_type: WEB, IMAGE, VIDEO, NEWS, or DISCOVER.
        row_limit: Rows per page (default 100; clamped to [1, 25000]).
            Pass `row_limit=1000` for large pulls; paginate via
            `start_row` for more.
        start_row: Starting row for pagination.
        sort_by: clicks | impressions | ctr | position.
        sort_direction: ascending | descending.
        filter_dimension: query | page | country | device.
        filter_operator: contains | equals | notContains | notEquals.
        filter_expression: value to filter on.
    """
    try:
        row_limit = max(1, min(int(row_limit), 25000))

        service = get_gsc_service()

        # Calculate date range if not provided
        if not end_date:
            end_date = datetime.now().date().strftime("%Y-%m-%d")
        if not start_date:
            start_date = (datetime.now().date() - timedelta(days=28)).strftime("%Y-%m-%d")

        # Parse dimensions
        dimension_list = [d.strip() for d in dimensions.split(",")]

        # Build request
        request = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": dimension_list,
            "rowLimit": row_limit,
            "startRow": start_row,
            "searchType": search_type.upper(),
        }
        
        # Add sorting
        if sort_by:
            metric_map = {
                "clicks": "CLICK_COUNT",
                "impressions": "IMPRESSION_COUNT",
                "ctr": "CTR",
                "position": "POSITION"
            }
            
            if sort_by in metric_map:
                request["orderBy"] = [{
                    "metric": metric_map[sort_by],
                    "direction": sort_direction.lower()
                }]
        
        # Add filtering if provided
        if filter_dimension and filter_expression:
            filter_group = {
                "filters": [{
                    "dimension": filter_dimension,
                    "operator": filter_operator,
                    "expression": filter_expression
                }]
            }
            request["dimensionFilterGroups"] = [filter_group]
        
        # Execute request
        response = service.searchanalytics().query(siteUrl=site_url, body=request).execute()
        
        if not response.get("rows"):
            return (f"No search analytics data found for {site_url} with the specified parameters.\n\n"
                   f"Parameters used:\n"
                   f"- Date range: {start_date} to {end_date}\n"
                   f"- Dimensions: {dimensions}\n"
                   f"- Search type: {search_type}\n"
                   f"- Filter: {filter_dimension} {filter_operator} '{filter_expression}'" if filter_dimension else "- No filter applied")
        
        # Loud truncation warning goes at the TOP so agents can't skim past
        # it. A tail nudge is also emitted further below (belt and braces)
        # since agents vary in whether they read from the top or bottom.
        rows_returned = len(response.get("rows", []))
        truncated = rows_returned >= row_limit

        result_lines: List[str] = []
        if truncated:
            result_lines.append(
                f"⚠ TRUNCATED: returned {rows_returned} rows and hit "
                f"`row_limit={row_limit}`. There may be more data. Pass a "
                f"larger `row_limit` (max 25000) or paginate via "
                f"`start_row={start_row + row_limit}`."
            )
            result_lines.append("")

        result_lines.append(f"Search analytics for {site_url}:")
        result_lines.append(f"Date range: {start_date} to {end_date}")
        result_lines.append(f"Search type: {search_type}")
        if filter_dimension:
            result_lines.append(f"Filter: {filter_dimension} {filter_operator} '{filter_expression}'")
        result_lines.append(f"Showing rows {start_row+1} to {start_row+rows_returned} (sorted by {sort_by} {sort_direction})")
        result_lines.append("\n" + "-" * 80 + "\n")
        
        # Create header based on dimensions
        header = []
        for dim in dimension_list:
            header.append(dim.capitalize())
        header.extend(["Clicks", "Impressions", "CTR", "Position"])
        result_lines.append(" | ".join(header))
        result_lines.append("-" * 80)
        
        # Add data rows
        for row in response.get("rows", []):
            data = []
            # Add dimension values
            for dim_value in row.get("keys", []):
                data.append(dim_value[:100])  # Increased truncation limit to 100 characters
            
            # Add metrics
            data.append(str(row.get("clicks", 0)))
            data.append(str(row.get("impressions", 0)))
            data.append(f"{row.get('ctr', 0) * 100:.2f}%")
            data.append(f"{row.get('position', 0):.1f}")
            
            result_lines.append(" | ".join(data))
        
        if truncated:
            next_start = start_row + row_limit
            result_lines.append("\nThere may be more results available. To see the next page, use:")
            result_lines.append(f"start_row: {next_start}, row_limit: {row_limit}")

        return "\n".join(result_lines)
    except Exception as e:
        return f"Error retrieving advanced search analytics: {str(e)}"

@mcp.tool()
async def compare_search_periods(
    site_url: str,
    period1_start: str,
    period1_end: str,
    period2_start: str,
    period2_end: str,
    dimensions: str = "query",
    limit: int = 10,
    *,
    upstream_row_limit: int = 500,
) -> str:
    """Compare GSC analytics between two time periods.

    Args:
        site_url: GSC site URL (exact match).
        period1_start: Start date for period 1 (YYYY-MM-DD).
        period1_end: End date for period 1 (YYYY-MM-DD).
        period2_start: Start date for period 2 (YYYY-MM-DD).
        period2_end: End date for period 2 (YYYY-MM-DD).
        dimensions: Dimensions to group by (default: query).
        limit: Number of top-N results to return after the diff (default 10).
        upstream_row_limit: Per-period rows pulled from GSC before the
            join (default 500; clamped to [1, 25000]). Raise this if
            long-tail queries aren't matching between periods.
    """
    try:
        upstream_row_limit = max(1, min(int(upstream_row_limit), 25000))

        service = get_gsc_service()

        # Parse dimensions
        dimension_list = [d.strip() for d in dimensions.split(",")]

        # Build requests for both periods
        period1_request = {
            "startDate": period1_start,
            "endDate": period1_end,
            "dimensions": dimension_list,
            "rowLimit": upstream_row_limit,
        }

        period2_request = {
            "startDate": period2_start,
            "endDate": period2_end,
            "dimensions": dimension_list,
            "rowLimit": upstream_row_limit,
        }
        
        # Execute requests
        period1_response = service.searchanalytics().query(siteUrl=site_url, body=period1_request).execute()
        period2_response = service.searchanalytics().query(siteUrl=site_url, body=period2_request).execute()
        
        period1_rows = period1_response.get("rows", [])
        period2_rows = period2_response.get("rows", [])
        
        if not period1_rows and not period2_rows:
            return f"No data found for either period for {site_url}."
        
        # Create dictionaries for easy lookup
        period1_data = {tuple(row.get("keys", [])): row for row in period1_rows}
        period2_data = {tuple(row.get("keys", [])): row for row in period2_rows}
        
        # Find common keys and calculate differences
        all_keys = set(period1_data.keys()) | set(period2_data.keys())
        comparison_data = []
        
        for key in all_keys:
            p1_row = period1_data.get(key, {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0})
            p2_row = period2_data.get(key, {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0})
            
            # Calculate differences
            click_diff = p2_row.get("clicks", 0) - p1_row.get("clicks", 0)
            click_pct = (click_diff / p1_row.get("clicks", 1)) * 100 if p1_row.get("clicks", 0) > 0 else float('inf')
            
            imp_diff = p2_row.get("impressions", 0) - p1_row.get("impressions", 0)
            imp_pct = (imp_diff / p1_row.get("impressions", 1)) * 100 if p1_row.get("impressions", 0) > 0 else float('inf')
            
            ctr_diff = p2_row.get("ctr", 0) - p1_row.get("ctr", 0)
            pos_diff = p1_row.get("position", 0) - p2_row.get("position", 0)  # Note: lower position is better
            
            comparison_data.append({
                "key": key,
                "p1_clicks": p1_row.get("clicks", 0),
                "p2_clicks": p2_row.get("clicks", 0),
                "click_diff": click_diff,
                "click_pct": click_pct,
                "p1_impressions": p1_row.get("impressions", 0),
                "p2_impressions": p2_row.get("impressions", 0),
                "imp_diff": imp_diff,
                "imp_pct": imp_pct,
                "p1_ctr": p1_row.get("ctr", 0),
                "p2_ctr": p2_row.get("ctr", 0),
                "ctr_diff": ctr_diff,
                "p1_position": p1_row.get("position", 0),
                "p2_position": p2_row.get("position", 0),
                "pos_diff": pos_diff
            })
        
        # Sort by absolute click difference (can change to other metrics)
        comparison_data.sort(key=lambda x: abs(x["click_diff"]), reverse=True)
        
        # Format results
        result_lines = [f"Search analytics comparison for {site_url}:"]
        result_lines.append(f"Period 1: {period1_start} to {period1_end}")
        result_lines.append(f"Period 2: {period2_start} to {period2_end}")
        result_lines.append(f"Dimension(s): {dimensions}")
        result_lines.append(f"Top {min(limit, len(comparison_data))} results by change in clicks:")
        result_lines.append("\n" + "-" * 100 + "\n")
        
        # Create header
        dim_header = " | ".join([d.capitalize() for d in dimension_list])
        result_lines.append(f"{dim_header} | P1 Clicks | P2 Clicks | Change | % | P1 Pos | P2 Pos | Pos Δ")
        result_lines.append("-" * 100)
        
        # Add data rows (limited to requested number)
        for item in comparison_data[:limit]:
            key_str = " | ".join([str(k)[:100] for k in item["key"]])
            
            # Format the click change with color indicators
            click_change = item["click_diff"]
            click_pct = item["click_pct"] if item["click_pct"] != float('inf') else "N/A"
            click_pct_str = f"{click_pct:.1f}%" if click_pct != "N/A" else "N/A"
            
            # Format position change (positive is good - moving up in rankings)
            pos_change = item["pos_diff"]
            
            result_lines.append(
                f"{key_str} | {item['p1_clicks']} | {item['p2_clicks']} | "
                f"{click_change:+d} | {click_pct_str} | "
                f"{item['p1_position']:.1f} | {item['p2_position']:.1f} | {pos_change:+.1f}"
            )
        
        return "\n".join(result_lines)
    except Exception as e:
        return f"Error comparing search periods: {str(e)}"

@mcp.tool()
async def get_search_by_page_query(
    site_url: str,
    page_url: str,
    days: int = 28,
    row_limit: int = 20,
    response_format: str = "markdown",
):
    """Break down GSC queries for a single page. Pick me when you already
    know which URL you want to analyse; use `get_search_analytics` for
    a property-wide overview or `get_advanced_search_analytics` for
    filtered/paginated analytics.

    Args:
        site_url: GSC site URL (exact match).
        page_url: Full page URL (scheme + host + path) matching the GSC
            `page` dimension exactly.
        days: Look-back window (default 28; clamped to min 1).
        row_limit: Max query rows (default 20; clamped to [1, 25000]).
            Raise to 500–1000 on pages ranking for many queries to
            avoid silent impression undercounts. Summary aggregates
            in json mode are only accurate across returned rows —
            if total_rows_returned == row_limit, retry with a larger
            row_limit.
        response_format: "markdown" (default, compact) or "json"
            (structured; ~2× tokens but parseable). Case-insensitive.

    Returns:
        markdown mode (default): str, pre-0.5 byte-compatible.
        json mode: dict with keys `ok, site_url, page_url, days,
        row_limit, total_rows_returned, possibly_truncated, queries,
        summary`. `queries` is a list of `{query, clicks, impressions,
        ctr, position}`. `summary` is
        `{total_clicks, total_impressions, average_position
        (impression-weighted), average_ctr}`.
        On error: `{ok: False, error, tool}` in json mode, or a
        string prefixed `"Error retrieving page query data: ..."` in
        markdown mode. Invalid response_format always returns a string
        error (conservative default).
    """
    fmt = str(response_format).strip().lower()
    if fmt not in ("markdown", "json"):
        return (
            "Error retrieving page query data: "
            f"response_format must be 'markdown' or 'json', got {response_format!r}"
        )

    if fmt == "markdown":
        # Near-verbatim copy of the pre-0.5 body. The ONLY intentional
        # differences vs pre-0.5 are:
        #   1. rowLimit: 20 (hardcoded) → effective_row_limit (the bug fix)
        #   2. days clamped via max(1, int(days)) so negative/zero inputs
        #      don't break date math (strict improvement for pathological
        #      input only; normal positive int values render identically).
        try:
            effective_days = max(1, int(days))
            effective_row_limit = max(1, min(int(row_limit), 25000))

            service = get_gsc_service()

            # Calculate date range
            end_date = datetime.now().date()
            start_date = end_date - timedelta(days=effective_days)

            # Build request with page filter
            request = {
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate": end_date.strftime("%Y-%m-%d"),
                "dimensions": ["query"],
                "dimensionFilterGroups": [{
                    "filters": [{
                        "dimension": "page",
                        "operator": "equals",
                        "expression": page_url
                    }]
                }],
                "rowLimit": effective_row_limit,
                "orderBy": [{"metric": "CLICK_COUNT", "direction": "descending"}]
            }

            # Execute request
            response = service.searchanalytics().query(siteUrl=site_url, body=request).execute()

            if not response.get("rows"):
                return f"No search data found for page {page_url} in the last {effective_days} days."

            # Format results
            result_lines = [f"Search queries for page {page_url} (last {effective_days} days):"]
            result_lines.append("\n" + "-" * 80 + "\n")

            # Create header
            result_lines.append("Query | Clicks | Impressions | CTR | Position")
            result_lines.append("-" * 80)

            # Add data rows (byte-for-byte pre-0.5: raw row values, no
            # int/float coercion, "Unknown" fallback for missing keys).
            for row in response.get("rows", []):
                query = row.get("keys", ["Unknown"])[0]
                clicks = row.get("clicks", 0)
                impressions = row.get("impressions", 0)
                ctr = row.get("ctr", 0) * 100
                position = row.get("position", 0)

                result_lines.append(f"{query[:100]} | {clicks} | {impressions} | {ctr:.2f}% | {position:.1f}")

            # Add total metrics
            total_clicks = sum(row.get("clicks", 0) for row in response.get("rows", []))
            total_impressions = sum(row.get("impressions", 0) for row in response.get("rows", []))
            avg_ctr = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0

            result_lines.append("-" * 80)
            result_lines.append(f"TOTAL | {total_clicks} | {total_impressions} | {avg_ctr:.2f}% | -")

            return "\n".join(result_lines)
        except Exception as e:
            return f"Error retrieving page query data: {str(e)}"

    # response_format == "json" — structured output with summary aggregates
    try:
        effective_days = max(1, int(days))
        effective_row_limit = max(1, min(int(row_limit), 25000))

        service = get_gsc_service()

        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=effective_days)

        request = {
            "startDate": start_date.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
            "dimensions": ["query"],
            "dimensionFilterGroups": [{
                "filters": [{
                    "dimension": "page",
                    "operator": "equals",
                    "expression": page_url
                }]
            }],
            "rowLimit": effective_row_limit,
            "orderBy": [{"metric": "CLICK_COUNT", "direction": "descending"}]
        }

        response = service.searchanalytics().query(siteUrl=site_url, body=request).execute()

        queries: List[Dict[str, Any]] = []
        for row in response.get("rows", []) or []:
            keys = row.get("keys", [])
            query = keys[0] if keys else ""
            queries.append({
                "query": query,
                "clicks": int(row.get("clicks", 0)),
                "impressions": int(row.get("impressions", 0)),
                "ctr": float(row.get("ctr", 0.0)),
                "position": float(row.get("position", 0.0)),
            })

        # Inline aggregation (see NOTE above _period_totals in
        # gsc_compare_periods_landing_pages — future DRY opportunity).
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

        return {
            "ok": True,
            "site_url": site_url,
            "page_url": page_url,
            "days": effective_days,
            "row_limit": effective_row_limit,
            "total_rows_returned": len(queries),
            "possibly_truncated": len(queries) >= effective_row_limit,
            "queries": queries,
            "summary": {
                "total_clicks": total_clicks,
                "total_impressions": total_impressions,
                "average_position": average_position,
                "average_ctr": average_ctr,
            },
        }
    except HttpError as e:
        try:
            error_content = json.loads(e.content.decode("utf-8"))
            message = error_content.get("error", {}).get("message", str(e))
        except Exception:
            message = str(e)
        return {"ok": False, "error": message, "tool": "get_search_by_page_query"}
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": "get_search_by_page_query"}


# --- Aggregated landing-page tools (Adds 2 + 3) ---

@mcp.tool()
async def gsc_get_landing_page_summary(
    site_url: str,
    start_date: str = "90daysAgo",
    end_date: str = "yesterday",
    top_n: int = 25,
    striking_distance_range: Tuple[float, float] = (11.0, 20.0),
    high_impression_min: int = 500,
    low_ctr_ratio: float = 0.5,
    country: Optional[str] = None,
    device: Optional[str] = None,
) -> Dict[str, Any]:
    """Compact top-N landing pages for a GSC property with striking-distance
    and high-impression/low-CTR flags. Returns a dict (~2k tokens) so
    callers can ingest many pages without blowing context. Makes 2 API
    calls (site totals + top-N rows).

    Args:
        site_url: GSC site URL (exact match; `sc-domain:example.com` for
            domain properties).
        start_date: Window start. Accepts 'today', 'yesterday',
            'Ndaysago', or YYYY-MM-DD.
        end_date: Window end, same format as start_date.
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
    """
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
                "tool": "gsc_get_landing_page_summary",
            }
        if not math.isfinite(sd_lo) or not math.isfinite(sd_hi) or sd_lo > sd_hi:
            return {
                "ok": False,
                "error": "striking_distance_range must contain finite numbers with min <= max",
                "tool": "gsc_get_landing_page_summary",
            }

        try:
            resolved_start = _parse_gsc_date(start_date)
            resolved_end = _parse_gsc_date(end_date)
        except ValueError as e:
            return {"ok": False, "error": str(e), "tool": "gsc_get_landing_page_summary"}

        service = get_gsc_service()

        # Build common filter group (country + device) if needed.
        filter_group: Optional[Dict[str, Any]] = None
        if country or device:
            filters: List[Dict[str, Any]] = []
            if country:
                filters.append({"dimension": "country", "operator": "equals", "expression": country})
            if device:
                filters.append({"dimension": "device", "operator": "equals", "expression": device.upper()})
            filter_group = {"filters": filters}

        # Call 1: site totals
        totals_request: Dict[str, Any] = {
            "startDate": resolved_start,
            "endDate": resolved_end,
            "dimensions": [],
            "rowLimit": 1,
        }
        if filter_group:
            totals_request["dimensionFilterGroups"] = [filter_group]

        totals_response = service.searchanalytics().query(
            siteUrl=site_url, body=totals_request
        ).execute()

        totals_rows = totals_response.get("rows", [])
        if totals_rows:
            t = totals_rows[0]
            site_totals = {
                "clicks": int(t.get("clicks", 0)),
                "impressions": int(t.get("impressions", 0)),
                "ctr": float(t.get("ctr", 0.0)),
                "position": float(t.get("position", 0.0)),
            }
        else:
            site_totals = {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}

        site_avg_ctr = site_totals["ctr"]

        # Call 2: top-N landing pages by clicks
        pages_request: Dict[str, Any] = {
            "startDate": resolved_start,
            "endDate": resolved_end,
            "dimensions": ["page"],
            "rowLimit": max(1, min(top_n, 25000)),
            "orderBy": [{"metric": "CLICK_COUNT", "direction": "descending"}],
        }
        if filter_group:
            pages_request["dimensionFilterGroups"] = [filter_group]

        pages_response = service.searchanalytics().query(
            siteUrl=site_url, body=pages_request
        ).execute()

        top_pages: List[Dict[str, Any]] = []
        for row in pages_response.get("rows", []):
            keys = row.get("keys", [])
            page = keys[0] if keys else ""
            clicks = int(row.get("clicks", 0))
            impressions = int(row.get("impressions", 0))
            ctr = float(row.get("ctr", 0.0))
            position = float(row.get("position", 0.0))
            low_ctr_threshold = site_avg_ctr * low_ctr_ratio if site_avg_ctr > 0 else 0.0
            top_pages.append({
                "page": page,
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
            "site_url": site_url,
            "start_date": resolved_start,
            "end_date": resolved_end,
            "site_totals": site_totals,
            "top_pages": top_pages,
            "thresholds": {
                "striking_distance_range": [sd_lo, sd_hi],
                "high_impression_min": high_impression_min,
                "low_ctr_ratio": low_ctr_ratio,
                "site_avg_ctr": site_avg_ctr,
            },
            "filters": {"country": country, "device": device},
        }
    except HttpError as e:
        try:
            error_content = json.loads(e.content.decode("utf-8"))
            message = error_content.get("error", {}).get("message", str(e))
        except Exception:
            message = str(e)
        return {
            "ok": False,
            "error": f"HTTP {e.resp.status}: {message}",
            "tool": "gsc_get_landing_page_summary",
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "tool": "gsc_get_landing_page_summary",
        }


@mcp.tool()
async def gsc_compare_periods_landing_pages(
    site_url: str,
    period_a_start: str,
    period_a_end: str,
    period_b_start: str,
    period_b_end: str,
    min_impressions: int = 100,
    limit: int = 50,
    decay_threshold_pct: float = -0.20,
    sort_by: str = "clicks_delta",
    sort_direction: str = "asc",
) -> Dict[str, Any]:
    """Landing-page period-vs-period diff with decay_flag for content-rot
    detection. 2 API calls (one per period), join on page URL, sort
    by a chosen delta column. Use `sort_by='clicks_delta'` +
    `sort_direction='asc'` for decayers (default); `desc` for risers.

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
    """
    try:
        if limit < 1:
            return {
                "ok": False,
                "error": "limit must be >= 1",
                "tool": "gsc_compare_periods_landing_pages",
            }

        try:
            a_start = _parse_gsc_date(period_a_start)
            a_end = _parse_gsc_date(period_a_end)
            b_start = _parse_gsc_date(period_b_start)
            b_end = _parse_gsc_date(period_b_end)
        except ValueError as e:
            return {"ok": False, "error": str(e), "tool": "gsc_compare_periods_landing_pages"}

        valid_sort_keys = {
            "clicks_delta", "clicks_pct",
            "impressions_delta", "impressions_pct",
            "position_delta", "ctr_delta",
        }
        if sort_by not in valid_sort_keys:
            return {
                "ok": False,
                "error": f"invalid sort_by: {sort_by!r}. Valid: {sorted(valid_sort_keys)}",
                "tool": "gsc_compare_periods_landing_pages",
            }

        direction_normalized = str(sort_direction).strip().lower()
        if direction_normalized not in ("asc", "desc"):
            return {
                "ok": False,
                "error": "sort_direction must be 'asc' or 'desc'",
                "tool": "gsc_compare_periods_landing_pages",
            }

        service = get_gsc_service()

        def _query(start: str, end: str) -> List[Dict[str, Any]]:
            req = {
                "startDate": start,
                "endDate": end,
                "dimensions": ["page"],
                "rowLimit": 25000,
            }
            resp = service.searchanalytics().query(siteUrl=site_url, body=req).execute()
            return resp.get("rows", [])

        a_rows = _query(a_start, a_end)
        b_rows = _query(b_start, b_end)

        # Index by page URL (single-dim 'keys' tuple). Reuses the pattern from
        # compare_search_periods at gsc_server.py:1155-1156.
        a_by_page = {tuple(r.get("keys", [])): r for r in a_rows}
        b_by_page = {tuple(r.get("keys", [])): r for r in b_rows}
        all_pages = set(a_by_page.keys()) | set(b_by_page.keys())

        def _metric(row: Optional[Dict[str, Any]], key: str, default: float = 0.0) -> float:
            if row is None:
                return default
            return float(row.get(key, default))

        # NOTE: This duplicates the inline aggregation in get_search_by_page_query.
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

        diffs: List[Dict[str, Any]] = []
        for page_key in all_pages:
            a_row = a_by_page.get(page_key)
            b_row = b_by_page.get(page_key)
            a_impr = int(_metric(a_row, "impressions"))
            b_impr = int(_metric(b_row, "impressions"))

            # OR semantics on min_impressions: keep rows where either period clears the bar.
            if a_impr < min_impressions and b_impr < min_impressions:
                continue

            a_clicks = int(_metric(a_row, "clicks"))
            b_clicks = int(_metric(b_row, "clicks"))
            a_ctr = _metric(a_row, "ctr")
            b_ctr = _metric(b_row, "ctr")
            a_pos = _metric(a_row, "position")
            b_pos = _metric(b_row, "position")

            clicks_delta = b_clicks - a_clicks
            impressions_delta = b_impr - a_impr
            ctr_delta = b_ctr - a_ctr
            position_delta = b_pos - a_pos  # positive = worse (further down)

            clicks_pct = (clicks_delta / a_clicks) if a_clicks > 0 else None
            impressions_pct = (impressions_delta / a_impr) if a_impr > 0 else None

            decay_flag = (
                clicks_pct is not None
                and clicks_pct < decay_threshold_pct
                and position_delta > 0
            )

            diffs.append({
                "page": page_key[0] if page_key else "",
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
            })

        # Sort with None-safe helper: None values for the sort column always
        # appear LAST regardless of direction (the naive (group, value) key
        # gets flipped by reverse=True and puts None rows at the front).
        diffs = _sort_landing_page_diffs(diffs, sort_by, direction_normalized)
        sliced = diffs[:limit]

        return {
            "ok": True,
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
        }
    except HttpError as e:
        try:
            error_content = json.loads(e.content.decode("utf-8"))
            message = error_content.get("error", {}).get("message", str(e))
        except Exception:
            message = str(e)
        return {
            "ok": False,
            "error": f"HTTP {e.resp.status}: {message}",
            "tool": "gsc_compare_periods_landing_pages",
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "tool": "gsc_compare_periods_landing_pages",
        }


@mcp.tool()
async def list_sitemaps_enhanced(site_url: str, sitemap_index: str = None) -> str:
    """
    List all sitemaps for a specific Search Console property with detailed information.
    
    Args:
        site_url: The URL of the site in Search Console (must be exact match)
        sitemap_index: Optional sitemap index URL to list child sitemaps
    """
    try:
        service = get_gsc_service()
        
        # Get sitemaps list
        if sitemap_index:
            sitemaps = service.sitemaps().list(siteUrl=site_url, sitemapIndex=sitemap_index).execute()
            source = f"child sitemaps from index: {sitemap_index}"
        else:
            sitemaps = service.sitemaps().list(siteUrl=site_url).execute()
            source = "all submitted sitemaps"
        
        if not sitemaps.get("sitemap"):
            return f"No sitemaps found for {site_url}" + (f" in index {sitemap_index}" if sitemap_index else ".")
        
        # Format the results
        result_lines = [f"Sitemaps for {site_url} ({source}):"]
        result_lines.append("-" * 100)
        
        # Header
        result_lines.append("Path | Last Submitted | Last Downloaded | Type | URLs | Errors | Warnings")
        result_lines.append("-" * 100)
        
        # Add each sitemap
        for sitemap in sitemaps.get("sitemap", []):
            path = sitemap.get("path", "Unknown")
            
            # Format dates
            last_submitted = sitemap.get("lastSubmitted", "Never")
            if last_submitted != "Never":
                try:
                    dt = datetime.fromisoformat(last_submitted.replace('Z', '+00:00'))
                    last_submitted = dt.strftime("%Y-%m-%d %H:%M")
                except:
                    pass
            
            last_downloaded = sitemap.get("lastDownloaded", "Never")
            if last_downloaded != "Never":
                try:
                    dt = datetime.fromisoformat(last_downloaded.replace('Z', '+00:00'))
                    last_downloaded = dt.strftime("%Y-%m-%d %H:%M")
                except:
                    pass
            
            # Determine type
            sitemap_type = "Index" if sitemap.get("isSitemapsIndex", False) else "Sitemap"
            
            # Get counts
            errors = sitemap.get("errors", 0)
            warnings = sitemap.get("warnings", 0)
            
            # Get URL counts
            url_count = "N/A"
            if "contents" in sitemap:
                for content in sitemap["contents"]:
                    if content.get("type") == "web":
                        url_count = content.get("submitted", "0")
                        break
            
            result_lines.append(f"{path} | {last_submitted} | {last_downloaded} | {sitemap_type} | {url_count} | {errors} | {warnings}")
        
        # Add processing status if available
        pending_count = sum(1 for sitemap in sitemaps.get("sitemap", []) if sitemap.get("isPending", False))
        if pending_count > 0:
            result_lines.append(f"\nNote: {pending_count} sitemaps are still pending processing by Google.")
        
        return "\n".join(result_lines)
    except Exception as e:
        return f"Error retrieving sitemaps: {str(e)}"

@mcp.tool()
async def get_sitemap_details(site_url: str, sitemap_url: str) -> str:
    """
    Get detailed information about a specific sitemap.
    
    Args:
        site_url: The URL of the site in Search Console (must be exact match)
        sitemap_url: The full URL of the sitemap to inspect
    """
    try:
        service = get_gsc_service()
        
        # Get sitemap details
        details = service.sitemaps().get(siteUrl=site_url, feedpath=sitemap_url).execute()
        
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
            result_lines.append(f"list_sitemaps_enhanced with sitemap_index={sitemap_url}")
        
        return "\n".join(result_lines)
    except Exception as e:
        return f"Error retrieving sitemap details: {str(e)}"

@mcp.tool()
async def submit_sitemap(site_url: str, sitemap_url: str) -> str:
    """
    Submit a new sitemap or resubmit an existing one to Google.
    
    Args:
        site_url: The URL of the site in Search Console (must be exact match)
        sitemap_url: The full URL of the sitemap to submit
    """
    try:
        service = get_gsc_service()
        
        # Submit the sitemap
        service.sitemaps().submit(siteUrl=site_url, feedpath=sitemap_url).execute()
        
        # Verify submission by getting details
        try:
            details = service.sitemaps().get(siteUrl=site_url, feedpath=sitemap_url).execute()
            
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
        except:
            # If we can't get details, just return basic success message
            return f"Successfully submitted sitemap: {sitemap_url}\n\nGoogle will queue it for processing."
    
    except Exception as e:
        return f"Error submitting sitemap: {str(e)}"

@mcp.tool()
async def delete_sitemap(site_url: str, sitemap_url: str) -> str:
    """
    Delete (unsubmit) a sitemap from Google Search Console.
    
    Args:
        site_url: The URL of the site in Search Console (must be exact match)
        sitemap_url: The full URL of the sitemap to delete
    """
    try:
        service = get_gsc_service()
        
        # First check if the sitemap exists
        try:
            service.sitemaps().get(siteUrl=site_url, feedpath=sitemap_url).execute()
        except Exception as e:
            if "404" in str(e):
                return f"Sitemap not found: {sitemap_url}. It may have already been deleted or was never submitted."
            else:
                raise e
        
        # Delete the sitemap
        service.sitemaps().delete(siteUrl=site_url, feedpath=sitemap_url).execute()
        
        return f"Successfully deleted sitemap: {sitemap_url}\n\nNote: This only removes the sitemap from Search Console. Any URLs already indexed will remain in Google's index."
    
    except Exception as e:
        return f"Error deleting sitemap: {str(e)}"

@mcp.tool()
async def manage_sitemaps(site_url: str, action: str, sitemap_url: str = None, sitemap_index: str = None) -> str:
    """
    All-in-one tool to manage sitemaps (list, get details, submit, delete).
    
    Args:
        site_url: The URL of the site in Search Console (must be exact match)
        action: The action to perform (list, details, submit, delete)
        sitemap_url: The full URL of the sitemap (required for details, submit, delete)
        sitemap_index: Optional sitemap index URL for listing child sitemaps (only used with 'list' action)
    """
    try:
        # Validate inputs
        action = action.lower().strip()
        valid_actions = ["list", "details", "submit", "delete"]
        
        if action not in valid_actions:
            return f"Invalid action: {action}. Please use one of: {', '.join(valid_actions)}"
        
        if action in ["details", "submit", "delete"] and not sitemap_url:
            return f"The {action} action requires a sitemap_url parameter."
        
        # Perform the requested action
        if action == "list":
            return await list_sitemaps_enhanced(site_url, sitemap_index)
        elif action == "details":
            return await get_sitemap_details(site_url, sitemap_url)
        elif action == "submit":
            return await submit_sitemap(site_url, sitemap_url)
        elif action == "delete":
            return await delete_sitemap(site_url, sitemap_url)
    
    except Exception as e:
        return f"Error managing sitemaps: {str(e)}"


# --- Account Management Tools ---

def _read_account_scopes(token_file_relative: Optional[str]) -> List[str]:
    """Load an account's OAuth credentials and return its granted scope names.
    Never include exception repr in the return — Credentials objects can
    leak refresh tokens when stringified."""
    if not token_file_relative:
        return ["<unavailable>"]
    token_path = token_file_relative
    if not os.path.isabs(token_path):
        token_path = os.path.join(SCRIPT_DIR, token_path)
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
async def list_accounts() -> str:
    """
    Lists all configured Google accounts with their aliases, emails, active
    status, and granted OAuth scopes.
    """
    try:
        manifest = _load_manifest()
        accounts = manifest.get("accounts", {})
        active = manifest.get("active_account")

        if not accounts:
            return (
                "No accounts configured.\n\n"
                "Use `add_account` to add a Google account, or if you have an existing "
                "token.json it will be auto-migrated on next server restart."
            )

        lines = ["# Google Search Console Accounts\n"]
        for alias, info in sorted(accounts.items()):
            marker = " **(active)**" if alias == active else ""
            email = info.get("email") or "unknown"
            added = info.get("added_at", "unknown")
            lines.append(f"- **{alias}**{marker}: {email} (added {added})")
            scopes = _read_account_scopes(info.get("token_file"))
            lines.append(f"  - scopes: {', '.join(scopes)}")

        lines.append(f"\nTotal: {len(accounts)} account(s)")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing accounts: {str(e)}"


@mcp.tool()
async def get_active_account() -> str:
    """
    Shows the currently active Google account alias and email.
    All GSC operations use the active account's credentials.
    """
    try:
        global _active_account
        # Lazy init
        if _active_account is None:
            manifest = _load_manifest()
            _active_account = manifest.get("active_account")

        if _active_account is None:
            return "No active account. Use `add_account` to add one, or existing token.json will be used as fallback."

        manifest = _load_manifest()
        acct = manifest.get("accounts", {}).get(_active_account)
        if not acct:
            return f"Active account '{_active_account}' not found in manifest. Use `list_accounts` to see available accounts."

        email = acct.get("email") or "unknown"
        return f"Active account: **{_active_account}** ({email})"
    except Exception as e:
        return f"Error getting active account: {str(e)}"


@mcp.tool()
async def add_account(alias: str) -> str:
    """
    Adds a new Google account. Opens a browser window for Google OAuth login.
    The new account becomes the active account after successful authentication.

    Args:
        alias: A short name for this account (lowercase alphanumeric and hyphens, 1-30 chars).
               Examples: 'client-a', 'personal', 'agency-main'
    """
    try:
        alias = _validate_alias(alias)
    except ValueError as e:
        return f"Invalid alias: {str(e)}"

    try:
        manifest = _load_manifest()

        # Check for alias collision
        if alias in manifest.get("accounts", {}):
            return f"Account '{alias}' already exists. Use a different alias or remove it first with `remove_account`."

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
            return f"Account directory for '{alias}' already exists. Remove it first with `remove_account`."
        token_path = os.path.join(acct_dir, "token.json")

        # Run OAuth flow
        try:
            flow = InstalledAppFlow.from_client_secrets_file(OAUTH_CLIENT_SECRETS_FILE, OAUTH_SCOPES)
            creds = _start_oauth_flow(flow, context=f"add_account('{alias}')")
        except HeadlessOAuthError as e:
            shutil.rmtree(acct_dir, ignore_errors=True)
            return str(e)
        except Exception as e:
            # Clean up partial directory on OAuth failure
            shutil.rmtree(acct_dir, ignore_errors=True)
            return f"OAuth flow failed: {str(e)}"

        # Save token
        with open(token_path, "w") as f:
            f.write(creds.to_json())

        # Detect email
        email = _detect_email(creds)

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
        return f"Error adding account: {str(e)}"


@mcp.tool()
async def switch_account(alias: str) -> str:
    """
    Switches the active Google account. All subsequent GSC operations will use this account's credentials.

    Args:
        alias: The alias of the account to switch to. Use `list_accounts` to see available accounts.
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

        manifest["active_account"] = alias
        _save_manifest(manifest)

        global _active_account
        _active_account = alias

        email = manifest["accounts"][alias].get("email") or "unknown"
        return f"Switched to account '{alias}' ({email}). All GSC operations now use this account."
    except Exception as e:
        return f"Error switching account: {str(e)}"


@mcp.tool()
async def remove_account(alias: str) -> str:
    """
    Removes a Google account and its stored credentials.
    If the removed account was active, switches to the first remaining account.

    Args:
        alias: The alias of the account to remove. Use `list_accounts` to see available accounts.
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

        # Remove account directory
        acct_dir = os.path.join(ACCOUNTS_DIR, alias)
        if os.path.isdir(acct_dir):
            shutil.rmtree(acct_dir)

        # Remove from manifest
        del manifest["accounts"][alias]

        # If this was the active account, switch to first remaining or None
        global _active_account
        if manifest.get("active_account") == alias:
            remaining = sorted(manifest.get("accounts", {}).keys())
            new_active = remaining[0] if remaining else None
            manifest["active_account"] = new_active
        _save_manifest(manifest)
        # Always sync in-memory state from manifest
        _active_account = manifest.get("active_account")

        if _active_account:
            return f"Account '{alias}' removed. Active account is now '{_active_account}'."
        else:
            return f"Account '{alias}' removed. No accounts remaining — GSC will fall back to legacy token.json if present."
    except Exception as e:
        return f"Error removing account: {str(e)}"


# --- Health check (Add 4) ---

@mcp.tool()
async def gsc_health_check(site_url: str) -> Dict[str, Any]:
    """
    One-shot diagnostic for a GSC property. Used at the start of every audit.

    Makes up to three API calls, each wrapped independently so one failure
    doesn't poison the rest. Manual actions and security issues are NOT
    exposed by the Search Console API v1 (confirmed — the discovery doc
    only surfaces sites/sitemaps/searchanalytics/urlInspection), so those
    fields are returned as explicit "not available" stubs.

    Args:
        site_url: The GSC site URL (exact match, or sc-domain:example.com for domain properties).
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
        service = get_gsc_service()
    except Exception as e:
        return {
            "ok": False,
            "error": f"auth failed: {type(e).__name__}: {e}",
            "tool": "gsc_health_check",
        }

    # Track whether any probe actually produced useful data. If all three
    # fail, the health check learned nothing and must return ok=False.
    any_probe_succeeded = False

    # Step 1: sites().get() for permission + verification
    try:
        site_info = service.sites().get(siteUrl=site_url).execute()
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
        today = datetime.now().date()
        week_ago = today - timedelta(days=7)
        data_request = {
            "startDate": week_ago.strftime("%Y-%m-%d"),
            "endDate": today.strftime("%Y-%m-%d"),
            "dimensions": ["date"],
            "rowLimit": 7,
        }
        data_response = service.searchanalytics().query(
            siteUrl=site_url, body=data_request
        ).execute()
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
        sitemaps_response = service.sitemaps().list(siteUrl=site_url).execute()
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
) -> Dict[str, Any]:
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
            "session_id": session_id,
            "site_url": site_url,
            "snapshot_date": _sf_sessions[session_id]["snapshot_date"],
            "sf_export_path": str(path),
            "loaded": loaded_summary,
            "warnings": warnings,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "tool": "gsc_load_from_sf_export",
        }


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
) -> Dict[str, Any]:
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
            "session_id": session_id,
            "dataset": dataset,
            "total_matched": total_matched,
            "offset": offset,
            "limit": limit,
            "truncated": total_matched > offset + len(sliced),
            "columns": returned_columns,
            "rows": sliced,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "tool": "gsc_query_sf_export",
        }


@mcp.tool()
async def get_creator_info() -> str:
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

if __name__ == "__main__":
    # Start the MCP server on stdio transport
    mcp.run(transport="stdio")

# GSC MCP Server

Google Search Console MCP server — forked from [AminForou/mcp-gsc](https://github.com/AminForou/mcp-gsc).

## Quick Start

```bash
uv venv .venv
uv pip install -r requirements.txt
.venv/bin/python gsc_server.py
```

## Architecture

Single-file FastMCP server (`gsc_server.py`) with 24 tools covering:

- **Properties**: `gsc_list_properties`, `gsc_add_site`, `gsc_delete_site`, `gsc_get_site_details`
- **Search Analytics**: `gsc_get_search_analytics`, `gsc_get_advanced_search_analytics`, `gsc_compare_search_periods`, `gsc_get_search_by_page_query`, `gsc_get_performance_overview`
- **URL Inspection**: `gsc_inspect_url_enhanced`, `gsc_batch_url_inspection`, `gsc_check_indexing_issues`
- **Sitemaps**: `gsc_get_sitemaps`, `gsc_list_sitemaps_enhanced`, `gsc_get_sitemap_details`, `gsc_submit_sitemap`, `gsc_delete_sitemap`, `gsc_manage_sitemaps`
- **Account Management**: `gsc_list_accounts`, `gsc_get_active_account`, `gsc_add_account`, `gsc_switch_account`, `gsc_remove_account`
- **Meta**: `gsc_get_creator_info`

## Auth

Uses OAuth 2.0 (Desktop app flow). Set `GSC_OAUTH_CLIENT_SECRETS_FILE` env var to point at your `client_secrets.json`. On first run, opens browser for Google login; caches token in `token.json`.

### Multi-Account Support

Multiple Google accounts are supported for agency workflows. Tokens are stored per-account under `accounts/`:

```
accounts/
  accounts.json          # manifest: alias → token path, email, timestamps
  client-a/token.json
  client-b/token.json
```

**Account tools:**
- `gsc_list_accounts` — show all configured accounts
- `gsc_get_active_account` — show which account is currently active
- `gsc_add_account(alias)` — authenticate a new Google account via browser OAuth
- `gsc_switch_account(alias)` — switch active account (all GSC tools use this)
- `gsc_remove_account(alias)` — delete an account and its stored token

**Migration:** On first start after upgrade, existing `token.json` is automatically copied to `accounts/default/token.json`. The original file is preserved for safe rollback. If no accounts are configured, the legacy `token.json` is used as fallback.

## Git Remotes

- `origin` — `mcprobert/mcp-gsc` (our fork, primary development)
- `upstream` — `AminForou/mcp-gsc` (original repo, for cherry-picking bug fixes)

## MCP Config

- Claude Code: `.mcp.json` in project root
- Claude Desktop: `~/Library/Application Support/Claude/claude_desktop_config.json`

Both need `GSC_OAUTH_CLIENT_SECRETS_FILE` updated with the real path to credentials.

## Dependencies

Python 3.11+ (pinned in `.python-version`). Key deps: `mcp`, `google-api-python-client`, `google-auth-oauthlib`, `oauth2client`.

## Response envelope convention

Every tool's JSON output follows a flat top-level envelope — no `result:`
wrapper. These invariants hold across the server:

1. **Success spine:** `{ok: true, tool: str, ..., meta: {...}}`.
2. **Error spine:** `{ok: false, error: str, hint: str, retry_after?: int, tool: str}`.
   Built via `_make_error_envelope` / `_http_error_envelope`.
3. **Tabular tools** (analytics family, `gsc_get_sitemaps`,
   `gsc_list_sitemaps_enhanced`, `gsc_compare_search_periods`,
   `gsc_batch_url_inspection`) use the
   `columns + rows + row_count + truncated + truncation_hint + meta`
   skeleton, emitted by `_format_table`.
4. **Domain-shaped tools** (landing pages, SF bridge, single-URL
   inspection, site details) return `{ok, tool, ...domain_fields, meta}`
   directly — the payload is tree-shaped, not a table.
5. **`response_format`** is `markdown` (default) | `json` on most tools,
   plus `csv` where tabular. Markdown / CSV return a `str`; JSON
   returns a `dict`.

House conventions for numeric fields:

- **Percentages** are raw float ratios (`-0.5353` = −53.53%). Callers
  format for display. `_format_table`'s `"pct"` column type handles
  markdown/CSV rendering (line 412 in `gsc_server.py`).
- **Positions** are 1-indexed floats. Absent data is `null`, never `0`
  — `0` would falsely imply "ranked first" (see compare_search_periods F4).
- **Counts** are ints. String coercion from the Google API is handled
  defensively at the tool boundary (see sitemap `indexed_urls` F7).

When you add a new tool, route JSON through `_format_table` when
tabular; otherwise emit a flat dict that satisfies the success spine
above. Always include `tool` and `meta` so downstream code can identify
the payload without inspecting keys.

**FastMCP return-type gotcha.** `@mcp.tool()` functions must declare
`-> Any` (or have no return annotation). Any generic type triggers
FastMCP's structured-output path, which wraps the payload in
`{result: {...}}` at the protocol boundary — breaking flat-envelope
consistency for consumers even though the source returns a flat dict.
Traps include `-> Dict[str, Any]`, `-> list[...]`, and `-> Optional[T]`
/ `-> Union[...]` (the FastMCP docstring at `func_metadata.py:212`
lists "list, dict, Union, etc." as the wrapping set). Safe annotations:
`-> Any`, `-> str`, `-> int`, `-> bool`, or no annotation. Verified
against `mcp/server/fastmcp/utilities/func_metadata.py:121-131`
(the `wrap_output` branch). `tests/test_envelope_annotations.py` pins
the rule — any generic annotation triggers a CI failure with a
pointer back to this section.

## Telemetry + stderr channel

Stdio transport reserves **stdout** for JSON-RPC. All telemetry
(`_log`, `_instrument`) writes JSONL to **stderr**. When you launch
`gsc_server.py` as a subprocess, capture stderr separately if you
want the telemetry stream; mixing stderr into stdout will corrupt
the MCP protocol.

Telemetry is opt-in via `GSC_MCP_TELEMETRY=1`. When enabled, the
`tool_enter` events include initial fields like `site_url` and
`page_url` so operators can correlate performance with the GSC
property being queried. These URLs are already in the API requests
being observed — treat them as business-sensitive but not secret.
**Never** pass OAuth tokens, client secrets, or API keys as
`initial_fields` to `_instrument` (they would be `repr()`-ed via
`default=str` in `json.dumps` and land in log aggregators).

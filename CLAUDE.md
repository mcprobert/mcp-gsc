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

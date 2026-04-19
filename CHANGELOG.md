# Changelog

All notable changes to this fork are documented here.
Dates are ISO-8601. Pre-1.0 minor bumps may include behaviour-breaking
changes; see `audit/03-remediation-plan.md` for the multi-tranche plan
these releases are executing against.

## [1.2.1] — 2026-04-19 — Post-analyst review residuals

Non-breaking patch closing out the two residuals the CMO analyst
flagged while verifying v1.2.0 against its acceptance criteria
(nine-for-nine pass; verdict "ship"). The same sweep surfaced a
live production finding — `sc-domain:chaserhq.com` was accessible
from two accounts, and the pre-refactor server silently routed to
whichever won the global-state race. The new `AMBIGUOUS_ACCOUNT`
error caught it on first probe. That's the v1.2.0 refactor's
safety value made real; this patch is cleanup alongside.

### Added

- `gsc_list_properties(response_format="markdown"|"json")`. JSON
  mode emits the standard `_format_table` envelope — `{ok, columns,
  rows, row_count, truncated, truncation_hint, meta}` — with columns
  `account`, `site_url`, `permission` and meta including
  `total_available`, `accounts_queried`, `partial_failures`. Every
  other tabular tool already had this; `gsc_list_properties` had
  lagged. Markdown remains the default (back-compat).
- JSON mode always tags rows with `account` for machine-readability,
  even when only one account is queried. Markdown drops the tag in
  the single-account case for human compactness.

### Changed

- `gsc_list_properties` return annotation `-> str` → `-> Any` to
  support the dual-format return. Safe per the FastMCP return-type
  pin in `tests/test_envelope_annotations.py`.
- `gsc_add_account` docstring: removed stale "becomes the active
  account" copy that survived the v1.2.0 refactor; replaced with a
  description of per-call routing and a note that `default` is a
  reserved alias.
- `get_gsc_service_oauth` docstring: removed "active account →
  legacy fallback" wording; clarified this path is now only for the
  interactive `gsc_add_account` OAuth flow (routed tool calls go via
  `_build_service_noninteractive`).

### Out of scope (flagged for later)

- `gsc_remove_account` still contains runtime strings referencing
  "Active account is now '{_active_account}'". That's functional
  code, not docstring cleanup — defer to a targeted modernisation
  pass. The user-facing footprint is zero (no production skill
  reads that string to branch).
- The `_active_account` module global stays. It's still consulted by
  the legacy OAuth fallback in `get_gsc_service_oauth` for
  back-compat reads of older manifests. Removal is a v1.3.0 concern
  alongside the deprecated `gsc_switch_account` /
  `gsc_get_active_account` hard-removal.

### Tests

435 passing (up from 426 at v1.2.0 release; +9 new in
`tests/test_list_properties.py`).

- `tests/test_list_properties.py` (new) — pins R1:
  - `test_json_mode_returns_table_envelope`
  - `test_json_mode_rows_tagged_with_account`
  - `test_json_mode_respects_name_contains_filter`
  - `test_json_mode_single_account_still_tags_rows_for_machine_readability`
  - `test_json_mode_partial_failures_surface_in_meta`
  - `test_json_mode_empty_manifest_returns_no_accounts_configured_envelope`
  - `test_markdown_mode_is_default_and_unchanged` (regression pin)
  - `test_invalid_response_format_returns_validation_string`

## [1.2.0] — 2026-04-19 — Agent-first account resolution (BREAKING)

**Breaking change.** The "active account" concept has been removed.
Every tool that takes `site_url` now auto-resolves which configured
account serves the request from the property itself. Callers that
relied on `gsc_switch_account` sticking across calls will break —
that was the root cause of two production incidents (silent reset on
MCP restart producing 403s; race between concurrent agents
stomping each other's routing decision). The fix is to remove the
state, not to add discipline around managing it.

### New

- `gsc_whoami(site_url=...)` — diagnostic that returns which
  configured account will serve a given property, without making a
  real GSC call. Emits `{resolved_account, alternatives}`; on
  ambiguity, `resolved_account: null` and `alternatives` lists
  candidates.
- `account_alias` keyword-only argument on 19 routed tools
  (search-analytics family, URL inspection family, sitemap CRUD,
  `gsc_health_check`, etc.). Omit for auto-resolve; pass explicitly
  to disambiguate or force a specific account.
- `gsc_list_accounts(include_properties=True)` enriches the listing
  with each account's `properties[]` list and `property_count`.
  Default remains False for privacy + speed.
- `ErrorCode` taxonomy: stable string enum
  (`ACCOUNT_SITE_MISMATCH`, `AMBIGUOUS_ACCOUNT`,
  `NO_ACCOUNT_FOR_PROPERTY`, `ACCOUNT_RESOLUTION_INCOMPLETE`,
  `AUTH_EXPIRED`, `PERMISSION_DENIED`, `QUOTA_EXCEEDED`,
  `DEPRECATED_TOOL`, etc.). Error envelopes gain `error_code: str`
  and `retryable: bool` fields; the stable codes are the canonical
  decision pivot for agents (don't regex the `error` string).
- `_build_service_noninteractive(alias)`: no browser OAuth, no token
  deletion on refresh failure, no service-account fallback. Used by
  the resolver and every routed tool.

### Changed

- `gsc_list_properties(account_alias=None)`: when omitted, lists
  properties across every configured account and tags each row with
  its source account. Pass `account_alias="..."` to restrict.
- `gsc_add_site` uses a different code path (the resolver can't verify
  a property that isn't yet in GSC): explicit alias → used as-is; no
  alias + one account → auto-picks; no alias + multiple accounts →
  `AMBIGUOUS_ACCOUNT`. On success, invalidates that alias's property
  cache so the next read tool sees the new property.
- Error envelope `hint` strings updated to reference `gsc_whoami` /
  `gsc_list_accounts` rather than the deprecated
  `gsc_get_active_account`.
- `gsc_list_accounts` no longer renders an "active" marker.

### Deprecated (removed in v1.3.0)

- `gsc_switch_account(alias)` — returns `{ok:false,
  error_code:"DEPRECATED_TOOL"}`. Still validates `alias` so a typo
  surfaces distinctly. Mutates no state.
- `gsc_get_active_account()` — returns `{ok:false,
  error_code:"DEPRECATED_TOOL"}`. Use `gsc_whoami(site_url=...)`.

### Migration (v1.1.x → v1.2.0)

- Manifest alias `default` → renamed in-place to `legacy` on first
  startup (`legacy_<timestamp>` on collision). Token file path is
  left untouched — no filesystem churn; the alias is a pure
  user-facing label.
- Bare legacy `token.json` next to the script → copied to
  `accounts/legacy/token.json` with alias `legacy` (previously was
  `accounts/default/token.json` with alias `default`). Original
  `token.json` preserved for rollback.
- `active_account` field dropped from the manifest on first save;
  silently ignored on read for back-compat.
- `gsc_add_account("default")` now rejected with `BAD_REQUEST`.

### Safety contract for routed calls

The resolver + `_build_service_noninteractive` enforce three
invariants that the review specifically flagged as regression risks:

1. No service-account fallback on the alias-routed path — would be
   a confused-deputy bug (silently satisfying an alias-routed call
   with different credentials).
2. No browser OAuth in the discovery / routed-call path — would
   wedge an MCP stdio subprocess indefinitely.
3. No token-file deletion on refresh failure — a transient network
   error must not force a full manual re-auth.

`gsc_add_account` keeps the interactive path it has always had; it's
the single tool where a browser prompt is legitimate.

### Resolver cache

In-memory tri-state per alias (`never | ok | error`) — transient
discovery failures do NOT get conflated with "known empty" (that
would convert a 500 into a false `NO_ACCOUNT_FOR_PROPERTY` or
`ACCOUNT_SITE_MISMATCH`). Per-alias `asyncio.Lock` keeps concurrent
refreshes from fighting each other. Stale-positive 403 on the
auto-resolved path triggers one invalidate-and-re-resolve retry.

### Tests

- `tests/test_account_resolution.py` — resolver unit tests (19)
- `tests/test_noninteractive_auth.py` — auth-safety contract (11)
- `tests/test_gsc_whoami.py` — diagnostic tool (4)
- `tests/test_list_accounts_enriched.py` — enrichment shape (8)
- `tests/test_positional_compatibility.py` — keyword-only
  `account_alias` can't clobber old positional args (30)
- `tests/test_403_reresolve.py` — stale-positive recovery (4)
- `tests/test_add_site_special_case.py` — add-site routing (5)
- `tests/test_migration_default_rename.py` — default→legacy (6)
- `tests/test_conftest_shim.py` — loud-failure gate on forgotten test setup (1)

Total: 426 tests passing.

### Pre-release review remediation (F11–F17)

The v1.2.0 diff went through two review passes before cutting the
release. The pre-implementation pass (open-gem `think_deeper`) caught
four regression risks baked into the plan — keyword-only placement,
`gsc_add_site` resolver bypass, error-state false-uniqueness,
service-account fallback — all addressed in the primary implementation.
The post-implementation review caught seven further issues:

- **F11 (High)** `AUTH_EXPIRED` no longer retryable by default. Four
  of five emission sites are strictly non-retryable (corrupt/missing
  token, missing refresh token, expired creds without refresh). Agents
  auto-retrying on these would spin. The one transient site — token
  race between discovery and use in `get_gsc_service_for_site` — opts
  in via `retryable=True` explicitly.
- **F12** `_make_error_envelope` now raises `TypeError` if
  `**extras` overlaps with core envelope fields. Prevents future
  caller bugs that would silently flip `ok=True` on error envelopes.
- **F13** Rewrote a stale comment in `_ensure_property_cache` that
  claimed we preserved a "last-known-good" snapshot on discovery
  failure; in fact we deliberately don't, to avoid confused-deputy
  risk from a revoked property reading as "reachable via stale alias".
- **F14** `_migrate_legacy_state` collision fallback now counter-loops
  (`legacy_<ts>_1`, `_2`, ...) instead of single-shotting the
  timestamp; closes a sub-second race window.
- **F15** `replacement.tool` → `replacement.suggested_tool` in both
  deprecation envelopes, removing the visual collision with the
  envelope-level `tool` field that names the deprecated caller.
- **F16** The conftest shim's legacy fallback now gates on
  identity-matching the original `get_gsc_service`. New tests that
  forget BOTH manifest setup AND the patch surface
  `NO_ACCOUNTS_CONFIGURED` loudly — previously limped through to a
  misleading OAuth-setup error.
- **F17** Two helper docstrings (`_invalidate_property_cache`,
  `_list_configured_aliases`) tightened to emphasise WHY over WHAT
  per the CLAUDE.md steer.

## [1.1.1] — 2026-04-19 — F1 completion + reviewer-note correction

Closes the gap the analyst flagged when re-verifying v1.1.0: six of
seven findings were fully resolved, but F1 (envelope normalisation)
was only partial. Four tools still emitted `{result: {...}}` wrapping
at the MCP protocol boundary while the other 10+ emitted flat JSON.

### Root cause (corrected)

FastMCP auto-detects structured output from the return-type annotation
(`mcp/server/fastmcp/utilities/func_metadata.py:121-131`). Generic
types like `Dict[str, Any]` trigger the `wrap_output` branch which
literally wraps the payload in `{"result": ...}` for structured-content
emission. Tools annotated `-> Any` take the unstructured path and emit
flat JSON via TextContent.

My v1.1.0 CHANGELOG had a reviewer note dismissing the analyst's
"wrapped in `result:`" framing as not reflecting the code. That was
factually right at source level (no `{"result": ...}` string in
`gsc_server.py`) but wrong in substance — FastMCP emits it at the
protocol layer based on return annotations. The analyst was right.
This release closes what F1 should have closed in v1.1.0.

### Fixed

- Flipped five tools from `-> Dict[str, Any]` to `-> Any`:
  `gsc_get_landing_page_summary` and `gsc_compare_periods_landing_pages`
  (analyst-flagged), `gsc_load_from_sf_export` and `gsc_query_sf_export`
  (analyst flagged as unverified; same pattern), plus `gsc_health_check`
  (preemptive — not in the analyst sweep but matches the pattern).
  Consumers of these five move from `{result: {ok, ...payload}}` to
  the flat `{ok, tool, ...payload, meta}` shape that matches every
  other tool.

  *Migration:* only affects consumers using MCP's structured-content
  capability. Consumers reading the text-content path (JSON-parsing
  the TextContent string) were never seeing the `result:` wrapper —
  FastMCP's `convert_result` emits the unwrapped form as text
  regardless. Structured-content consumers: if you were reading
  `out["result"]["ok"]` on any of these, drop the `["result"]` layer.
  Shape inside the payload is unchanged.

### Added

- `tests/test_envelope_annotations.py` — regression guard that
  introspects every `@mcp.tool()`-decorated function and fails if any
  declare a generic return annotation. The existing 323 tests call
  tool functions directly and bypass FastMCP, so they couldn't catch
  this class of bug.

### Docs

- `CLAUDE.md` "Response envelope" section now explicitly names the
  FastMCP return-type rule and links to the source line responsible.

## [1.1.0] — 2026-04-18 — post-refactor review remediation (F1–F7)

Resolves the seven findings from the 2026-04-18 analyst review of the
post-v1.0 token-reduction refactor. No functional regressions were
found in the refactor itself; all seven items were cosmetic or
shape-level drift in the older tools that the refactor hadn't touched.
Three of them are breaking field-type changes in niche fields of three
tools — the minor bump reflects that, with migration notes below.

### Breaking (opt-in: only affects `response_format="json"` consumers)

- **F3 — `gsc_compare_search_periods.rows[].click_pct` → `clicks_pct`**.
  Field renamed and type changed: was a pre-formatted string
  `"-58.3%"` (or `"N/A"`); is now a float ratio (e.g. `-0.583` =
  −58.3%) or `null` when p1 clicks is zero. Display formatting is
  handled by `_format_table`'s `"pct"` column type — markdown/CSV
  callers see the same shape. Brings this tool into alignment with
  `gsc_compare_periods_landing_pages.rows[].clicks_pct`, which was
  already a float ratio.

  *Migration:* if you were reading `click_pct`, rename to `clicks_pct`
  and multiply by 100 for display. `None` replaces the `"N/A"` string.

  *Note:* markdown rendering of `clicks_pct` now shows 2 decimal places
  (e.g. `-58.30%`) instead of 1 (`-58.3%`), since the unified
  `_format_table` `"pct"` column type already used 2 decimals for
  sibling CTR columns in three older tools. Changing it globally to
  1 decimal would regress that rendering; the drift is the lesser
  trade. Consumers reading the JSON float are unaffected.

- **F4 — `gsc_compare_search_periods.rows[].{p1,p2}_position` nullable**.
  When a query is present in one period but absent from the other,
  the missing side's position is now `null` instead of the sentinel
  `0`. GSC positions are 1-indexed — `0` was a lie that naive consumers
  read as "ranked first". `pos_diff` is also `null` when either side
  is null. Clicks and impressions for absent sides remain `0` (zero
  events is truthful; position is not).

  *Migration:* guard position reads with `is not None` before numeric
  comparison or arithmetic.

  *Note:* rows with a present `keys` entry but a missing `position`
  field also resolve to `null` now (previously `0`). In practice GSC
  always populates `position`, so this secondary path is unreachable
  — just aligns the two edge cases under the same null convention.

- **F7 — sitemap URL counts are int, not string**. Both
  `gsc_get_sitemaps.rows[].indexed_urls` and
  `gsc_list_sitemaps_enhanced.rows[].urls` now coerce the GSC API's
  string `submitted` value to an int (or `null` on parse failure /
  missing `web` contents entry), replacing the legacy `"N/A"` sentinel.
  The `errors` and `warnings` columns were already int-coerced;
  this drops the drift.

  *Migration:* numeric comparisons / sorts on these fields now work as
  expected; string comparisons (`x == "673"`) must be updated.

### Added

- **F2 — `response_format="json"` on the URL-inspection family**.
  `gsc_inspect_url_enhanced`, `gsc_batch_url_inspection`, and
  `gsc_check_indexing_issues` now accept `response_format="markdown"`
  (default, byte-equivalent to pre-F2) or `response_format="json"` with
  structured envelopes: nested single-URL payload for `inspect`,
  tabular `rows`+`row_count`+`next_offset` for `batch`, and
  `summary`+`buckets` (where canonical_conflict/fetch_failure carry
  structured entries) for `check`. Programmatic consumers no longer
  need to regex-parse markdown.

  *Incidental fix (not strictly part of F2):* `gsc_check_indexing_issues`
  no longer appends spurious entries to the `fetch_failure` bucket
  when the API omits `pageFetchState`. The pre-F2 guard
  (`if fetch_state != "SUCCESSFUL"`) fired for empty strings too,
  producing lines like `"{url} - "` with no state. The F2 refactor
  added a truthiness check to skip that case. Latent bug; low
  impact (GSC usually populates the field).

- **F5 — `response_format` on `gsc_get_site_details`**. JSON mode
  emits `{ok, tool, site_url, permission_level, verification|null,
  ownership|null, meta}`. The docstring was also trimmed to reflect
  what the Google `sites.get` API actually returns (most
  `sc-domain:` properties carry only `permissionLevel`).

- **F6 — `truncation_hint` on `gsc_get_search_by_page_query`**. An
  actionable one-sentence hint is now emitted when `possibly_truncated`
  is `true`, pointing at the `row_limit` lever (up to 25000). Matches
  the convention on the sibling analytics tools.

### Docs

- **F1 — response envelope convention documented** in `CLAUDE.md`.
  Flat top-level spine `{ok, tool, ...payload, meta}` with error
  envelopes via `_make_error_envelope`; analytics tools use the
  `columns+rows+truncation_hint` skeleton via `_format_table`;
  domain-shaped tools use flat dicts with the same spine. Percentages
  are raw float ratios, positions are 1-indexed with null for absent
  data, counts are int.

- Added `tool` field + minimal `meta` block to the four domain tools
  that were returning flat dicts without them: `gsc_get_landing_page_summary`,
  `gsc_compare_periods_landing_pages`, `gsc_load_from_sf_export`,
  `gsc_query_sf_export`. Additive; no behaviour change for consumers
  that weren't branching on these keys.

### Reviewer note

The analyst's "wrapped in `result:`" framing from the review turned out
not to reflect the code — there was no `result:` wrapper anywhere. The
real drift was `str`-vs-`dict` returns on the URL-inspection family
(addressed by F2) and missing spine fields on a handful of domain
tools (addressed by this release). F1 is therefore documentation +
light spine additions rather than a structural rewrite.

## [1.0.1] — 2026-04-17 — v1.0.0 post-release review cleanup

Addresses the `should-fix` items from the full codebase review after
v1.0.0 tagged. No behaviour changes agents can rely on — only
consistency polish.

- **b.1** Three outlier tools that still used the legacy
  `f"Error ...: {str(e)}"` error path now use the full B.4 envelope
  helpers: `gsc_list_properties`, `gsc_get_active_account`,
  `gsc_get_performance_overview`. `gsc_list_properties` also gained
  a dedicated `HttpError` branch. The pre-existing
  `FileNotFoundError` branch for missing service-account credentials
  is preserved as a plain string (environment-setup concern, not a
  tool failure).

- **b.2** Hoisted `_PAGE_QUERY_SUMMARY_MIN_ROWS` from mid-file
  (~line 2240) to the top-of-file tuning-constants block next to
  `URL_INSPECTION_PACING_SEC`. All module-scope tunables now live
  together and a single scroll covers them.

- **b.3** Harmonised URL-inspection telemetry. `gsc_batch_url_inspection`
  and `gsc_check_indexing_issues` now emit `tool_enter` / `tool_exit` /
  `tool_error` events as a single batch-level span (not per URL). The
  per-URL inner loop stays string-based — per-URL telemetry would
  drown out the batch-latency signal.

- **b.4** Migrated `gsc_get_performance_overview` to `_format_table`
  and added a `response_format` enum. Markdown output still shows
  totals + daily trend; csv and json now available. Meta includes
  totals for downstream code. B.4 envelopes apply on the error path.

- **b.5** `_detect_email`'s sync `urllib` GET (10s timeout) is now
  offloaded via `asyncio.to_thread` so it doesn't block the asyncio
  loop during `gsc_add_account`.

- **(c)** Added a load-bearing comment on the module-global cluster
  (`_active_account`, `_sf_sessions`, `_migration_checked`) noting
  that lock-free access is safe under FastMCP's stdio transport but
  would become racy under SSE/HTTP multi-tenant. Added a telemetry
  PII + stderr-channel section to `CLAUDE.md` explaining that
  `page_url` and `site_url` flow into `_instrument` on purpose and
  warning against passing credentials as initial fields.

Tests unchanged at 308 (no behaviour diff, only polish).

---

## [1.0.0] — 2026-04-17 — Namespace rename (A.4) + Tranche B complete

### Breaking — tool namespace rename

The deferred A.4 item from the original audit lands. Every one of the
24 previously-bare tools is now prefixed `gsc_`. The other 5 tools
already had the prefix and are unchanged. **Every client config that
pins old tool names MUST be updated.**

| Old name                       | New name                           |
|--------------------------------|------------------------------------|
| `list_properties`              | `gsc_list_properties`              |
| `add_site`                     | `gsc_add_site`                     |
| `delete_site`                  | `gsc_delete_site`                  |
| `get_site_details`             | `gsc_get_site_details`             |
| `get_search_analytics`         | `gsc_get_search_analytics`         |
| `get_advanced_search_analytics`| `gsc_get_advanced_search_analytics`|
| `compare_search_periods`       | `gsc_compare_search_periods`       |
| `get_search_by_page_query`     | `gsc_get_search_by_page_query`     |
| `get_performance_overview`     | `gsc_get_performance_overview`     |
| `inspect_url_enhanced`         | `gsc_inspect_url_enhanced`         |
| `batch_url_inspection`         | `gsc_batch_url_inspection`         |
| `check_indexing_issues`        | `gsc_check_indexing_issues`        |
| `get_sitemaps`                 | `gsc_get_sitemaps`                 |
| `list_sitemaps_enhanced`       | `gsc_list_sitemaps_enhanced`       |
| `get_sitemap_details`          | `gsc_get_sitemap_details`          |
| `submit_sitemap`               | `gsc_submit_sitemap`               |
| `delete_sitemap`               | `gsc_delete_sitemap`               |
| `manage_sitemaps`              | `gsc_manage_sitemaps`              |
| `list_accounts`                | `gsc_list_accounts`                |
| `get_active_account`           | `gsc_get_active_account`           |
| `add_account`                  | `gsc_add_account`                  |
| `switch_account`               | `gsc_switch_account`               |
| `remove_account`               | `gsc_remove_account`               |
| `get_creator_info`             | `gsc_get_creator_info`             |

**Why this matters.** The pre-1.0 tools had names generic enough to
collide with any other MCP server that exposes `list_properties`,
`inspect_url_enhanced`, `list_accounts`, etc. `gsc_*` prefix prevents
accidental cross-server tool routing and makes the server's namespace
discoverable via simple prefix filter.

**Rollout.** Hard cutover — no legacy-alias wrappers. The
remediation plan called out that dual-register would double the
schema tax for 24 tools. 397 substitutions across `gsc_server.py`,
tests, README, CLAUDE.md, and `audit/eval/prompts.json` applied via
`audit/_work/rename_tools_a4.py`. All 308 tests pass unchanged.

**Audit docs** (01/02/03/04 markdown) preserved with old names — they
are historical snapshots, not a running ledger.

### Tranche B — all items landed (shipped incrementally during v0.6.x)

The full Tranche B rollout from `audit/03-remediation-plan.md` is now
on main:

- **B.3** shared `_format_table` helper (markdown / csv / json
  renderer) — `3e3a5cd`.
- **B.6** opt-in structured telemetry behind `GSC_MCP_TELEMETRY=1`;
  hot-path tools (`gsc_list_properties`, `gsc_get_search_analytics`,
  `gsc_get_advanced_search_analytics`, `gsc_compare_search_periods`,
  `gsc_get_search_by_page_query`, `gsc_inspect_url_enhanced`,
  `gsc_get_performance_overview`, `gsc_compare_periods_landing_pages`,
  `gsc_get_active_account`) emit `tool_enter` / `tool_exit` /
  `tool_error` JSON lines on stderr — `0d74f83` + `4bcce20`.
- **B.1** consolidation — deferred as not-justified; A.5
  disambiguation already addressed the agent-confusion rationale
  (`8a56609`).
- **B.2** `response_format="markdown" | "csv" | "json"` enum on every
  tabular tool: the three analytics tools (`a9da902`), plus
  `gsc_get_sitemaps` and `gsc_list_sitemaps_enhanced` (`ab609f3`).
- **B.5** `get_search_by_page_query`'s JSON `summary` block now
  suppressed by default when `row_limit <= 50` — `cf34e93`.
- **B.4** structured error envelopes (`{ok, error, hint, retry_after,
  tool}`) with status-aware hints rolled out to every tool:
  analytics (`7a654ca`), sitemap reads (`ab609f3`), site CRUD
  (`51d8e7e`), URL inspection (`63b0880`), account mutation
  (`4cda1dc`), sitemap writes (`bdb3b0b`), composed + SF bridge
  (`fc3ef4c`).
- **B.7** MCP Inspector dev target via `make inspect` — `aa9e09d`.

Post-rollout review fixes for A.1/A.3-style blockers (truncation
placement, silent compare_periods truncation, CSV formula injection
guard, HTTP-date Retry-After parsing) landed in `6443199`.

### Notable idempotent edges preserved (NOT wrapped in envelopes)

- `gsc_add_site` on HTTP 409 → "already added" (idempotent success).
- `gsc_delete_site` on HTTP 404 → "was not found" (idempotent success).
- `gsc_delete_sitemap` on pre-check 404 → "already been deleted".
- Validation-layer errors (bad alias, invalid date, unknown session
  id, out-of-range numerics) stay as lightweight
  `{ok: False, error, tool}` dicts where the error string IS the hint.

### Tests

132 (pre-audit) → 308 (at this release). See individual v0.6.x
commits for per-cluster test breakdowns.

---

## [0.6.0] — 2026-04-17 — Tranche A remediation

Implements eight of the nine Tranche-A items from the MCP Optimal Design
audit (`audit/` directory). The ninth, a namespace rename (A.4), is
scoped to a separate release candidate because it renames every tool
and warrants its own migration path.

### Breaking changes

- **A.1 — `get_advanced_search_analytics` default `row_limit`: 1000 → 100.**
  At the old default a single call projected to ~25k response tokens
  (Claude Code's response cap). The new default is 100 rows. When a
  call hits the cap, the response now starts with a loud
  `⚠ TRUNCATED` warning on the first line plus a pagination cursor in
  the tail. Callers who want 1,000 rows must pass `row_limit=1000`
  explicitly.
- **A.2 — `get_search_analytics` silent 20-row cap → parameterised
  `row_limit` defaulting to 100.** The old code hardcoded `rowLimit=20`
  and had no way to override. Now accepts `row_limit` (1–25,000,
  default 100) and surfaces a truncation nudge when the cap is hit.
- **A.6 — `list_properties` now caps at 50 by default** and accepts a
  new `limit` parameter (1–1000). Agency accounts with hundreds of
  properties no longer blow up context on the first call; a nudge
  suggests `name_contains` or a higher `limit`.
- **A.8 — `compare_search_periods` now exposes `upstream_row_limit`
  (default 500).** Previously hardcoded 1,000 per period (2,000-row
  hidden upstream pull per call regardless of the agent's declared
  `limit`). Match rate on long-tail queries may drop slightly at the
  new default; pass `upstream_row_limit=1000` to restore old
  behaviour.

### Bug fixes

- **A.3 — `get_sitemaps` no longer crashes with
  `"'>' not supported between instances of 'str' and 'int'"`.** The GSC
  Sitemaps API returns `errors`/`warnings` as strings (e.g. `"0"`,
  `"7"`); the tool previously did `sitemap["errors"] > 0` without
  coercion. Now coerces via `int(...)` with a defensive fallback for
  non-numeric strings. Regression tested in `tests/test_sitemaps.py`.

- **A.7 — `batch_url_inspection` and `check_indexing_issues` now pace
  their per-URL API calls.** The old loops had a misleading comment
  promising a delay but no actual delay, firing up to 10
  `urlInspection.index.inspect` calls in a tight loop. Under realistic
  GSC quota this triggered 429 rate-limit errors. A 0.1s delay is now
  inserted between iterations; tunable via
  `URL_INSPECTION_PACING_SEC` at the top of `gsc_server.py`.

- **A.9 — OAuth flow no longer silently hangs in headless MCP
  contexts.** `flow.run_local_server(port=0)` at two sites would block
  indefinitely when no browser was available (Claude Desktop
  subprocess, CI, SSH). A new `_start_oauth_flow` helper checks the
  `GSC_MCP_HEADLESS` env var: when `1` it raises `HeadlessOAuthError`
  with remediation steps; when unset it still opens the browser but
  prints a stderr warning first.

### Improvements

- **A.5 — Top-5 tool descriptions tightened + disambiguation added.**
  The three analytics tools (`get_search_analytics`,
  `get_advanced_search_analytics`, `get_search_by_page_query`) and the
  three URL-inspection tools (`inspect_url_enhanced`,
  `batch_url_inspection`, `check_indexing_issues`) now each include a
  **Pick me when…** guidance sentence. Heaviest docstring
  (`get_search_by_page_query`) dropped 606 → 453 tokens.

### Audit deliverables

This release is governed by a four-document audit in `audit/`:

- `audit/01-discovery.md` — factual inventory of tools, response
  shapes, error patterns, observability gaps.
- `audit/02-scorecard.md` — 14-criterion rubric, overall maturity 2.4/5,
  top-3 token-bleed offenders, top-3 quick wins.
- `audit/03-remediation-plan.md` — three-tranche plan (A quick wins, B
  structural, C code-execution-not-justified).
- `audit/04-eval-harness.md` — proposed eval harness for measuring
  before/after impact.

### Deferred

- **A.4 — Namespace rename of all 24 bare tools to `gsc_*` prefix.**
  Deferred to v1.0.0 because it breaks every client config that pins
  tool names and warrants a migration window. See remediation plan §A.4
  for the three migration options (hard cutover, dual-register for one
  cycle, or FastMCP-internals spike for hidden aliasing).

### Testing

All 142 tests pass (up from 132 at v0.5.0 — 10 new regression tests
added for A.3 and A.9).

## [0.5.0] — earlier

See `git log` — `get_search_by_page_query` gained `row_limit` and
`response_format="json"` opt-in mode.

## [0.4.x] — earlier

See `git log` — v0.4.0 added SF CSV bridge, landing-page aggregators,
and health check; v0.4.1 was a validation patch.

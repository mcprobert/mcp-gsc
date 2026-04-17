# Changelog

All notable changes to this fork are documented here.
Dates are ISO-8601. Pre-1.0 minor bumps may include behaviour-breaking
changes; see `audit/03-remediation-plan.md` for the multi-tranche plan
these releases are executing against.

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

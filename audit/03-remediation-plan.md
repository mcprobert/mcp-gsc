# 03 — Remediation plan

Three tranches. Each item follows the same template:
**what / why / before→after / risk & rollback / dependencies / test
strategy** (dependencies and test strategy are mandatory from Tranche B
onwards).

Tokens cited are `cl100k_base` proxy (see `01-discovery.md`). Code
references are to `gsc_server.py`.

---

## Tranche A delivery status (2026-04-17)

Shipped as **v0.6.0** (commit `352219c`). 8 of 9 items landed; A.4
deferred to v1.0.0 per the original scope.

| Item | Status | Notes |
|---|---|---|
| A.1 — `get_advanced_search_analytics` default 1000 → 100 | ✅ shipped | Loud `⚠ TRUNCATED` first-line warning + tail pagination nudge |
| A.2 — parameterise `get_search_analytics` `row_limit` | ✅ shipped | Default 100, clamped [1, 25000] |
| A.3 — fix `get_sitemaps` str/int bug | ✅ shipped | 6 regression tests |
| A.4 — namespace rename to `gsc_*` | ⏳ deferred | Scheduled for v1.0.0 |
| A.5 — tighten top-5 docstrings + disambiguation | ✅ shipped | Schema tax −1,464 tokens verified via eval harness |
| A.6 — `list_properties` default `limit=50` + nudge | ✅ shipped | Clamp [1, 1000] |
| A.7 — asyncio request pacing on URL-inspection loops | ✅ shipped | `URL_INSPECTION_PACING_SEC=0.1` via `await asyncio.sleep` |
| A.8 — `compare_search_periods` `upstream_row_limit` | ✅ shipped | Default 500, keyword-only |
| A.9 — headless-OAuth guard | ✅ shipped | `GSC_MCP_HEADLESS=1` raises `HeadlessOAuthError`; re-raised past the broad except in `get_gsc_service` so the primary entry point surfaces it |

**Measured impact (6 shared eval prompts, see
`audit/eval/runs/v050-v060-6prompt-delta.md`):**

- `grand_total_tokens` (Anthropic-billed input+output): **−12,951 (−10%)**
- `tool_definitions_tokens` (live FastMCP schema): −1,464 (−4%)
- `error_count`: 0 in both
- Tests: 132 → 144 (12 new: 6 sitemap + 6 OAuth guard including 2
  integration tests for the broad-except re-raise)

Tranche B (below) is the next planned work.

---

## Tranche A — Quick wins (target 1–2 days)

Scope: **metadata + parameter tweaks only. No return-type changes.**
(Return-type / error-envelope changes moved to Tranche B because they
touch every tool and need an eval-harness baseline to verify.)

### A.1 — Lower `get_advanced_search_analytics` default `row_limit` from 1000 → 100

- **What**: change `row_limit: int = 1000` to `row_limit: int = 100` at
  `gsc_server.py:1340`. Update the docstring to explicitly state the
  new default and instruct the caller to pass `row_limit=1000` for
  large pulls. Make the truncation nudge **loud**: when returned rows
  == row_limit, prepend a warning line ("⚠ Capped at `row_limit=100`
  — you may be seeing a partial dataset. Pass `row_limit=1000` or
  paginate with `start_row`.") as the *first* line of the response so
  agents can't skim past it.
- **Why**: scorecard criterion "Default pagination / limits" (2→4);
  single biggest per-call offender. One call at the old default
  projects to ~25k response tokens — Claude Code's configured
  response cap.
- **Before → After** (default call, no explicit `row_limit`):
  - Before: ~25,000 response tokens (1,000 rows)
  - After: ~2,689 response tokens (100 rows) + loud nudge on the
    first line when capped
  - Delta: **–22,300 tokens per call**, capped
- **Risk**: **this is a behavior-breaking change**, not just a token
  optimization. An agent that implicitly relied on 1,000-row
  completeness to do client-side aggregation (e.g. "total impressions
  last 28 days = sum of returned rows") will silently get wrong
  answers if the nudge is missed. Mitigations: (1) loud
  first-line nudge above; (2) return a structured `truncated: true`
  sentinel in the response header; (3) explicit CHANGELOG entry +
  major-version bump (v1.0.0). **Do not ship this behind a patch
  version.**
- **Rollback**: one-line revert of the default.

### A.2 — Parameterize `get_search_analytics` `row_limit`

- **What**: add `row_limit: int = 100` to the signature at `:687`;
  replace the hardcoded `"rowLimit": 20` at `:712` with the parameter;
  add a truncation nudge ("Showing first N of possibly-more rows — raise
  `row_limit` to see more") when the API returns exactly `row_limit`
  rows.
- **Why**: scorecard criterion "Default pagination / limits" and
  "Truncation nudges". Mirrors the fix v0.5.0 shipped for
  `get_search_by_page_query` (`a808da4`). Eliminates the silent
  undercount class of bug.
- **Before → After**: same call shape; default goes from 20 (silent) to
  100 (nudged). Agents needing tighter can still pass `row_limit=20`.
- **Risk & rollback**: response-size goes up for agents that were
  implicitly banking on 20. Acceptable — if they want 20 they can ask.
  Rollback = revert default.

### A.3 — Fix `get_sitemaps` bug

- **What**: at `:837`, `sitemap["errors"] > 0` compares a **string** (the
  GSC Sitemaps API serializes `errors` as a quoted count) to `0`. Cast
  via `int(sitemap.get("errors", 0))`; do the same for `warnings` at
  `:841`.
- **Why**: live-sampling captured the literal exception message
  `"Error retrieving sitemaps: '>' not supported between instances of
  'str' and 'int'"`. The tool is currently non-functional on real
  accounts. Sibling tool `list_sitemaps_enhanced` works, so there's an
  obvious user workaround but the agent can't discover it from the
  terse error.
- **Before → After**:
  - Before: `Error retrieving sitemaps: '>' not supported between
    instances of 'str' and 'int'`
  - After: real sitemap table identical in shape to
    `list_sitemaps_enhanced` at 5 rows (~258 response tokens)
- **Risk & rollback**: the cast is pure addition. Add a regression test
  with a string-typed mock payload.

### A.4 — Add consistent `gsc_` namespace prefix to all 29 tools

- **What**: rename every `@mcp.tool()` function to `gsc_<name>`
  (e.g. `list_properties` → `gsc_list_properties`). FastMCP uses the
  decorated function name as the MCP tool name, so rename = advertise.
- **Why**: scorecard criterion "Namespace prefixing" (2→5). Prevents
  collisions when the server is loaded alongside another MCP that
  uses bare names (`list_properties`, `batch_url_inspection`,
  `inspect_url_enhanced` are all generic enough to collide).
- **Migration path (pick one — all three are viable; prefer Option 1):**
  - **Option 1 (recommended): hard cutover.** Rename at the function
    signature; ship behind a v1.0.0 major-version bump with a CHANGELOG
    that lists the 24 renames. Tool configs pinning bare names break
    once; migration is mechanical.
  - **Option 2: dual-register for one cycle.** For each renamed tool,
    keep a thin wrapper with the legacy name that calls the renamed
    core function. This **doubles the schema tax for renamed tools
    during the deprecation window** (~+3,300 tokens of schema for ~24
    legacy wrappers × a shortened docstring of ~140 tokens each) — the
    trade-off is gentler migration. Remove the wrappers in v1.1.
  - **Option 3 (do not do without a spike first): override FastMCP's
    `list_tools` response.** Subclass FastMCP or monkey-patch the
    handler so legacy names are accepted on `CallToolRequest` but
    hidden from `list_tools`. FastMCP's public API does not expose
    this cleanly; this requires reading FastMCP internals and will
    break if FastMCP refactors. **Verify with a 2-hour spike before
    committing.** An earlier draft of this plan called this a
    "hidden router" — retracted because it was ahead of the library.
- **Before → After**: Option 1 keeps schema tax flat (renames are free);
  Option 2 adds ~3,300 tokens for one release cycle; Option 3 keeps
  schema tax flat but carries library-internal risk.
- **Risk & rollback**: breaking change for client configs that pin
  tool names. Option 1 rollback = revert rename. Option 2 rollback =
  one PR. Option 3 rollback = probably a rewrite.

### A.5 — Tighten top-5 descriptions + add disambiguation

- **What**: rewrite the docstrings of `get_search_by_page_query:1598`,
  `gsc_get_landing_page_summary:1817`,
  `gsc_compare_periods_landing_pages:1992`, `gsc_query_sf_export:2948`,
  and `batch_url_inspection:977` to ≤2 sentences of *prose* (parameter
  examples stay). Add a **Pick me when…** half-sentence to the three
  analytics tools (`get_search_analytics`, `get_advanced_search_analytics`,
  `get_search_by_page_query`) and to the three URL-inspection tools
  (`inspect_url_enhanced`, `check_indexing_issues`, `batch_url_inspection`).
- **Why**: scorecard criteria "Verb + resource clarity" and
  "Disambiguation". Top-5 tools own 1,748 description tokens (34% of
  the whole schema tax). Halving that reclaims ~800 tokens.
- **Before → After**:
  - Before (e.g. `get_search_analytics`): *"Get search analytics data
    for a specific property."*
  - After: *"Get search analytics data for a GSC property. Pick me when
    you want an overview of a property's top queries by clicks; use
    `get_advanced_search_analytics` if you need sorting/filtering, or
    `get_search_by_page_query` to break down by query for a single
    page."*
- **Risk & rollback**: descriptions are durable context. Small chance a
  tightening drops a constraint an agent was relying on — mitigate by
  preserving all parameter constraint text. Rollback = revert docstring.

### A.6 — Add `list_properties` default limit + `name_contains` nudge

- **What**: cap `list_properties:530` at 50 rows by default; add a
  `limit: int = 50` parameter; when capped, append a nudge suggesting
  `name_contains` or a higher `limit`.
- **Why**: live call returned all 17 properties for this test account.
  Agency accounts frequently have 200+ properties. Unbounded response
  on first call risks burying the useful result deep in context.
- **Before → After**: 232 tokens for 17 rows today; agency account with
  500 properties projects to ~6,800 tokens. After: capped at ~700 tokens
  with a nudge.
- **Risk & rollback**: agents used to seeing every property upfront will
  have to filter or paginate. Low — `name_contains` already exists as
  the primary filter tool.

### A.7 — Add request pacing to the URL-inspection hot loops

- **What**: inside `batch_url_inspection:1083` and
  `check_indexing_issues:1165`, add a short backoff between
  `.execute()` calls. A simple `time.sleep(0.1)` per iteration matches
  the comment at `gsc_server.py:1091` that already promises a delay
  (but doesn't have one). Better: exponential backoff on 429/503 via
  `googleapiclient.errors.HttpError` branches, with a `Retry-After`
  hint surfaced to the agent.
- **Why**: current implementation fires up to 10 `urlInspection.index`
  requests in a tight loop. GSC's per-project URL-inspection quota is
  tight; under real usage this hits 429s. The code comment misleads
  reviewers into thinking pacing exists. Scorecard "Error quality"
  (+1) and correctness.
- **Before → After**: 429 storms on 10-URL batches dropped; a
  documented delay matches the comment.
- **Risk & rollback**: ~1 second added to a 10-URL batch. Agents
  waiting on batch inspection will feel it but rarely care. Rollback
  = revert.

### A.8 — Expose `row_limit` on `compare_search_periods`

- **What**: add `upstream_row_limit: int = 500` to
  `compare_search_periods` (line 1467) and thread it through the two
  hardcoded `"rowLimit": 1000` values at `gsc_server.py:1499` and
  `:1506`. Keep the existing agent-facing `limit` parameter (the
  downstream top-N filter) unchanged.
- **Why**: current tool silently pulls 2 × 1,000 rows on every call
  regardless of the agent's declared `limit` (default 10). Hidden
  upstream cost leaks into GSC quota and masks real call volume.
  Scorecard "Default pagination / limits" (+1).
- **Before → After**: default drops from 2 × 1,000 = 2,000 upstream
  rows to 2 × 500 = 1,000. No agent-visible output change at the
  default `limit=10`. Agents needing larger overlap can raise it.
- **Risk & rollback**: the 1,000-row hardcode was intentionally
  generous to ensure matched items overlap between periods. At
  `upstream_row_limit=500` the match rate on high-tail queries may
  drop — the tool already handles missing items (P2 Pos = 0.0 in live
  sample row 8). Rollback = revert default.

### A.9 — Guard `flow.run_local_server` behind a headless-mode check

- **What**: wrap the two `flow.run_local_server(port=0)` calls
  (`gsc_server.py:519, 2582`) in a check for a `DISPLAY` env var (X11)
  or a `GSC_MCP_HEADLESS=1` override. When headless, raise a clear
  error naming the problem ("OAuth requires a browser; run
  `python gsc_server.py --login` from a desktop session, then re-start
  the MCP server") instead of silently blocking on a hidden redirect.
- **Why**: availability bug. In any headless MCP context (Claude
  Desktop subprocess without browser access, SSH runner, CI job) the
  server hangs indefinitely on token refresh failure. Scorecard
  "Error quality" (+1) — and more importantly, makes the server
  deployable in more contexts.
- **Before → After**: server fails fast with a remediation message
  instead of hanging.
- **Risk & rollback**: the `DISPLAY`/override check is pure addition.
  Rollback = revert the guard.

**Tranche A summary**:

- **Realistic per-session token saving: ~2,500–26,000 tokens**,
  depending on whether the agent exercises `get_advanced_search_analytics`
  at the bare default (high end, dominated by A.1) or stays within
  specified limits (low end; A.2/A.3/A.5/A.6/A.8 each contribute
  hundreds to low-thousands). **Do not pitch a single headline number
  without naming this range** — the upper bound is a per-call
  theoretical max, not a guaranteed per-session win.
- **Correctness gains beyond tokens**: A.3 fixes a broken tool; A.7
  fixes latent 429 storms; A.9 unblocks headless deployment; A.5
  reduces tool-routing errors.
- Scorecard criteria lifted: "Default pagination / limits" (2→4),
  "Namespace prefixing" (2→5 via Option 1), "Disambiguation" (1→3),
  "Error quality" (+1 sitemaps, +1 pacing, +1 OAuth headless),
  "Truncation nudges" (2→3).
- Effort: 1–2 days of focused work — **plus a 2-hour spike** if the
  team wants A.4 Option 3 (FastMCP internals) before committing. Items
  A.1/A.2/A.3/A.6/A.7/A.8/A.9 are each <1 hour; A.4 (rename cascade)
  and A.5 (description rewrites) are the time sinks.

---

## Tranche B — Structural improvements (target 1–2 weeks)

### B.1 — Consolidate the three analytics tools

- **What**: deprecate `get_search_analytics` and collapse into
  `get_advanced_search_analytics` (the superset). Keep
  `get_search_by_page_query` separate because the per-page shape is a
  distinct workflow, but route it through the consolidated tool's
  response-shaping helper.
- **Why**: scorecard criteria "Tool consolidation" (2→4) and
  "Disambiguation" (1→5). Removes the agent's "which one do I pick?"
  failure mode.
- **Before → After**: 2 tools instead of 3 for property-level analytics.
  Description-token reclaim: ~345 tokens (description of the removed
  tool) minus ~150 tokens (added guidance on the kept tool). Net ~200.
- **Dependencies**: requires B.3's shared `_format_response` helper
  landing first so the two-tool surface can share output logic.
- **Test strategy**: add eval-harness prompts that previously would have
  called `get_search_analytics`; verify the agent routes to
  `get_advanced_search_analytics` with sensible defaults.
- **Risk & rollback**: breaking for any config pinning the legacy name.
  Mitigate with the same hidden-router pattern from A.4.

### B.2 — Add `response_format` enum (`csv` | `markdown` | `json`) to all tabular tools

- **What**: add a `response_format: Literal["csv", "markdown", "json"]`
  parameter to `get_advanced_search_analytics`, `get_search_analytics`
  (post-B.1: consolidated), `compare_search_periods`,
  `list_sitemaps_enhanced`, and `list_properties`. **Default to `csv`**
  for tabular data (ref doc: Axiom observed 29% savings CSV vs JSON on
  tabular payloads; our own numbers show CSV beats current
  markdown-pipe-tables by ~15–20%).
- **Why**: scorecard criteria "Field filtering / response_format" (1→4)
  and "Format choice" (2→4). Matches v0.5.0's direction but applies it
  consistently and picks the right default.
- **Before → After** (on `get_advanced_search_analytics` 100-row call):
  - markdown today: 2,689 tokens
  - csv default: ~2,150 tokens (projected)
  - json: heavier than markdown at low row counts, lighter at large
    counts (crossover around 200 rows per our 20-row measurement)
- **Dependencies**: B.3 (shared helper) must land first.
- **Test strategy**: parameterize eval prompts on all three formats.
  Verify agents pick `csv` by default without being told.
- **Risk & rollback**: changing the default format is visible to
  clients parsing markdown. Mitigate with one release of "default
  `markdown` with a deprecation warning; next release flip to `csv`."

### B.3 — Shared response-shaping helper

- **What**: introduce `_format_response(rows, columns, response_format)`
  that emits CSV, markdown, or JSON consistently. Also centralizes the
  truncation nudge and the row-cap message.
- **Why**: removes per-tool divergence. Today each tool builds its own
  string; the shape drifts across tools (see `get_performance_overview`
  vs `get_search_analytics`). Also unblocks B.1 and B.2.
- **Dependencies**: none — enabling change for the rest of Tranche B.
- **Test strategy**: snapshot tests for each `response_format` on one
  canonical rowset; ensure byte-for-byte match with pre-refactor
  markdown output.

### B.4 — Normalize error envelopes

- **What**: every tool returns either its success value or
  `{"ok": False, "error": str, "hint": str, "retry_after": float | None}`.
  Replace the ~35 `f"Error …: {str(e)}"` sites with the envelope; add
  `hint` entries that name the parameter and show a valid example.
  Map `HttpError` status codes to specific hints (`429` →
  `retry_after`, `403` → "check permission level with
  `gsc_get_active_account`", `404` → "verify site_url exact match").
- **Why**: scorecard criterion "Error message quality" (2→4). Kills
  the terse `str(e)` retry-storm class.
- **Before → After**:
  - Before: `"Error retrieving sitemaps: '>' not supported between
    instances of 'str' and 'int'"`
  - After: `{"ok": False, "error": "sitemap_types_mismatch", "hint":
    "Server bug — use `list_sitemaps_enhanced` instead", "retry_after":
    null}`
- **Dependencies**: none structurally, but the eval harness (Phase 4)
  must be in place first so we can validate that error-driven retry
  storms drop in the replay.
- **Test strategy**: inject each `HttpError` status code; verify the
  envelope; verify the eval harness's retry count drops.
- **Risk & rollback**: return-type change. Breaking for clients
  parsing error strings. Moved here from Tranche A for exactly this
  reason. Mitigate by keeping the old string error in a `legacy_error`
  field for one release.

### B.5 — Re-examine `get_search_by_page_query` JSON mode

- **What**: at `row_limit ≤ 50`, return the markdown mode by default
  and strip the verbose `summary`/`thresholds`/`possibly_truncated`
  metadata block from JSON responses. At `row_limit > 50`, JSON
  becomes the cheaper option and stays as-is.
- **Why**: live measurement shows JSON is 2.07× markdown at 20 rows
  (1,149 vs 554) — the opposite of what the v0.5.0 commit message
  implies. Scorecard criterion "Field filtering / response_format"
  (1→5). Also removes a documentation-vs-reality gap.
- **Dependencies**: B.3's helper; B.4 (need to preserve the `ok`
  field so success/failure signal is still structured).
- **Test strategy**: eval prompts at 10, 50, 100, 500, 1000 rows in
  both modes; record token cost and routing accuracy.

### B.6 — Observability: structured stderr logging

- **What**: add a `_log(event, **fields)` helper that writes one JSON
  line per tool call to stderr. Fields: `tool`, `dur_ms`, `ok`,
  `error_code`, `rows_returned`, `response_chars`. Put it behind an
  env var `GSC_MCP_TELEMETRY=1` so it's off by default.
- **Why**: scorecard criterion "Observability" (1→4). Unblocks
  validating all remediations in production. Low blast radius because
  it's opt-in.
- **Dependencies**: none.
- **Test strategy**: a snapshot test that captures stderr for one tool
  call and asserts the JSON line shape.

### B.7 — Wire MCP Inspector into the dev loop

- **What**: add a `make inspect` / `justfile` target that runs the
  server under MCP Inspector. Add a `dev/` walkthrough note.
- **Why**: lets future audits reproduce the per-tool token costs without
  running our custom `_work/sample_texts.py`. Scorecard
  "Observability" (+1 alongside B.6).
- **Dependencies**: none.
- **Test strategy**: smoke test the target in CI.

**Tranche B summary**:

- Estimated per-session token saving beyond Tranche A: **~3,000–5,000
  tokens**.
- Scorecard criteria lifted: "Tool consolidation" (2→4),
  "Field filtering / response_format" (1→4→5), "Format choice" (2→4),
  "Error quality" (2→4), "Observability" (1→4), "Disambiguation"
  (assuming B.1) (3→5).
- Effort: 1–2 weeks with eval-harness gates on B.1, B.2, B.4, B.5.

---

## Tranche C — Architectural shift (code execution)

**Recommendation: NOT justified.** Do not adopt a code-execution /
Code Mode architecture for this server.

### Token math

| Metric | This server | Ref-doc threshold |
|---|---:|---:|
| Tool count | 29 | >20 = consider |
| Schema tax (measured) | 5,098 tokens | – |
| Schema tax (projected after FastMCP boilerplate) | 6.5–8k tokens | – |
| Share of 200k context window | ~3–4% | ≥10% auto-triggers tool search in Claude Code |
| Upstream API surface | ~15 GSC endpoints + SF CSV | 2,500 Cloudflare endpoints in the motivating case |
| Concurrency | single-user, single-account per session, stdio | multi-tenant HTTP in the motivating cases |

Ref-doc benchmark numbers (Cloudflare 1.17M → 1k tokens; Anthropic
98.7% reduction on Drive-to-Salesforce) come from workloads that are
one to three orders of magnitude larger than this one.

### Non-token reasons

- A sandboxed JS/TS execution layer adds a whole new attack surface
  (CVE exposure, escape risk) and operational surface (V8 isolates,
  resource limits, timeouts) to a server used locally by individual
  marketers.
- FastMCP's single-file design is one of this repo's strengths;
  code-execution would push for a multi-file refactor and a new
  dependency graph that is hard to justify today.
- Tranche A alone closes ~80% of the token-bleed we measured. Tranche
  B + observability closes most of the rest. We'd be paying Code-Mode
  complexity for marginal residual savings.

### Do this if…

Revisit Tranche C only if **any** of the following is true at future
audit:

- Tool count exceeds ~50 (this one has grown from 24 → 29 in one
  minor-version cycle; not implausible by end of 2026).
- The server is re-platformed to multi-tenant streamable HTTP for
  agency-scale concurrent users.
- An AI-agent consumer starts hitting the 10%-of-context auto-trigger
  for tool search, indicating real agents are feeling the schema tax
  in production logs.
- GSC's API expands materially (e.g. the Insights API joins and
  multiplies endpoint count).

### Migration path if ever adopted

- Shim: expose a new pair `gsc_search` (natural-language → tool
  schema lookup) and `gsc_execute` (run a small JS/TS program against
  an auto-generated TypeScript SDK of GSC). Keep the 29 direct tools
  alive for one cycle — agents opt into code mode by preferring
  `gsc_execute`.
- Sandbox: use the same V8-isolate pattern the ref doc describes
  (Cloudflare Workers or QuickJS); hard ban on `fetch()` to any host
  other than `searchconsole.googleapis.com`.
- Kill the direct tools only after the eval harness shows the code-mode
  path reaches parity on all eval prompts.

---

## Rollout sequencing

1. Ship Tranche A bundled as **v1.0.0** (not v0.6.0 — the namespace
   rename and the `get_advanced_search_analytics` default change are
   both behavior-breaking and should carry a major-version signal).
   CHANGELOG must call out: renames (A.4), default row_limit change
   (A.1), hardcoded-2000-row removal (A.8), and the headless OAuth
   guard (A.9).
2. Ship the eval-harness (`04-eval-harness.md`) alongside v1.0.0 so
   the baseline-vs-post diff is runnable the day Tranche A lands.
3. Ship Tranche B as a sequence of ≤200-line PRs guarded by the eval
   harness: **B.3 first** (enables B.1 and B.2), then B.6, then B.1,
   B.2, B.5, B.4, B.7.
4. Revisit Tranche C at next audit (suggested: 6 months or at a
   tool-count or transport change, whichever comes first).

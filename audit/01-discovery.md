# 01 — Discovery

**Audit date:** 2026-04-17
**Branch / commit:** `main` @ `a808da4` (v0.5.0)
**Tokenizer:** `tiktoken` `cl100k_base` — used as a **relative proxy** for
Claude tokens. Anthropic's tokenizer diverges from `cl100k_base` by ~5–10%
in practice; numbers here are fit for baseline-vs-post diffs, not Anthropic
billing estimates.
**Property used for live sampling:** `sc-domain:chaserhq.com` (active
account: `chaser`, scope `siteFullUser`). All site URLs, page URLs, and
query text redacted in captured samples (see `_work/sample_texts.py` for
the reproducible text blocks).

---

## 1. Server inventory

| Item | Value |
|---|---|
| Servers in repo | 1 |
| File | `gsc_server.py` (3,195 lines, single file) |
| Framework | FastMCP (`mcp.server.fastmcp`) |
| Transport | stdio |
| Upstream API | Google Search Console API v1 (+ Screaming Frog CSV exports) |
| Deployment context | Local CLI / desktop agent (Claude Code, Claude Desktop) |
| Helper modules | None — all logic in `gsc_server.py` |
| Tests | `tests/` (unit + validation tests); no audit tooling |
| Observability | **None** — no `logging`, no telemetry, no MCP Inspector wiring, no token counting |

**Documentation drift (flagged, not fixed this pass):** `CLAUDE.md:11` and
the server's README both claim **24 tools**. Actual count from AST parse
is **29 tools**. Delta accounts for the v0.4.0 landing-page aggregators,
SF bridge, and health check that shipped in `7953d0b` plus v0.5.0 edits.

---

## 2. Tool surface

**29 tools. Total definition token cost: 5,098** (= 3,504 description +
1,483 schema, per our pseudo-JSON schema approximation; the real
FastMCP-serialized `inputSchema` is probably 1.3–1.6× larger in production
because it adds JSON-Schema boilerplate and per-parameter docs).

**Namespace prefixing:** **5 of 29** tools use the `gsc_` prefix
(`gsc_get_landing_page_summary`, `gsc_compare_periods_landing_pages`,
`gsc_load_from_sf_export`, `gsc_query_sf_export`, `gsc_health_check`).
**24 are bare** (`list_properties`, `get_search_analytics`,
`inspect_url_enhanced`, etc.). Inconsistent. Collision-prone in any
config that runs this server alongside another MCP that exposes tools
like `list_properties` or `batch_url_inspection`.

**1:1 wrapper vs composed:**
- **1:1 wrappers (17 tools)**: `list_properties`, `add_site`, `delete_site`,
  `get_site_details`, `get_search_analytics`, `get_advanced_search_analytics`,
  `get_sitemaps`, `list_sitemaps_enhanced`, `get_sitemap_details`,
  `submit_sitemap`, `delete_sitemap`, `inspect_url_enhanced`,
  `get_creator_info`, `list_accounts`, `get_active_account`, `switch_account`,
  `remove_account`.
- **Composed / workflow-oriented (12 tools)**: `batch_url_inspection`,
  `check_indexing_issues`, `get_performance_overview`,
  `compare_search_periods`, `get_search_by_page_query`,
  `gsc_get_landing_page_summary`, `gsc_compare_periods_landing_pages`,
  `manage_sitemaps`, `add_account`, `gsc_health_check`,
  `gsc_load_from_sf_export`, `gsc_query_sf_export`.

**Top-10 tools by definition token cost** (full table in
`_work/tool_tokens.json`):

| rk | total | desc | sch | pc | pfx | name | line |
|---:|---:|---:|---:|---:|:---:|:---|---:|
| 1 | 606 | 531 | 70 | 5 | – | `get_search_by_page_query` | 1598 |
| 2 | 464 | 339 | 118 | 9 | gsc | `gsc_get_landing_page_summary` | 1817 |
| 3 | 458 | 309 | 142 | 10 | gsc | `gsc_compare_periods_landing_pages` | 1992 |
| 4 | 434 | 323 | 106 | 8 | gsc | `gsc_query_sf_export` | 2948 |
| 5 | 426 | 347 | 75 | 6 | – | `batch_url_inspection` | 977 |
| 6 | 345 | 204 | 136 | 12 | – | `get_advanced_search_analytics` | 1334 |
| 7 | 297 | 226 | 65 | 4 | gsc | `gsc_load_from_sf_export` | 2835 |
| 8 | 232 | 120 | 109 | 7 | – | `compare_search_periods` | 1467 |
| 9 | 164 | 101 | 59 | 4 | – | `manage_sitemaps` | 2418 |
| 10 | 144 | 113 | 27 | 1 | gsc | `gsc_health_check` | 2697 |

The **top 5 tools own 47%** of the schema tax (2,388 / 5,098 tokens).

**Disambiguation risk — three near-duplicate analytics tools**:
`get_search_analytics` (line 687), `get_advanced_search_analytics` (line
1334), `get_search_by_page_query` (line 1598). All three operate on the
same upstream endpoint (`searchanalytics.query`). Descriptions do not
call out the decision boundary. First-sentence comparison:

- `get_search_analytics` — "Get search analytics data for a specific
  property."
- `get_advanced_search_analytics` — "Get advanced search analytics data
  with sorting, filtering, and pagination."
- `get_search_by_page_query` — "Get search analytics data for a specific
  page, broken down by query."

No tool tells the agent "pick me when X, pick the other when Y."

---

## 3. Response shapes

**Method:** live calls against `sc-domain:chaserhq.com` for read-only
tools; text responses captured, redacted, then tokenized. Raw capture
logic in `_work/sample_texts.py`; results in `_work/sample_tokens.json`.
Write-path and account-mutation tools were not called live (safety); I
report their shape from source code and label accordingly.

### 3.1 Token-count of typical responses

| Tool | Call | Shape | Tokens |
|---|---|---|---:|
| `get_advanced_search_analytics` | 100 rows, `dimensions=query` | markdown table | **2,689** |
| `gsc_get_landing_page_summary` | `top_n=25`, default window | JSON dict | **2,010** |
| `get_search_by_page_query` | 20 rows, `response_format=json` | JSON dict | **1,149** |
| `get_performance_overview` | 28 days | markdown (totals + per-day) | 664 |
| `get_search_by_page_query` | 20 rows, `response_format=markdown` | markdown | 554 |
| `get_search_analytics` | 28 days, 20-row default (hardcoded) | markdown | 532 |
| `compare_search_periods` | 10 rows | markdown | 474 |
| `list_sitemaps_enhanced` | 5 rows | markdown | 258 |
| `list_properties` | 17 rows | bullet list | 232 |
| `inspect_url_enhanced` | 1 URL | markdown multi-section | 206 |
| `gsc_health_check` | — | JSON dict | 180 |
| `get_sitemaps` | — | **ERROR string** (live bug) | 19 |

### 3.2 Per-tool notes

**`get_advanced_search_analytics` (line 1334)** — largest per-call payload.
Default `row_limit=1000` declared at `gsc_server.py:1340`; clamped to
25,000 at `gsc_server.py:1382`. At 1,000 rows of dense query data the
response is projected at ~25,000+ tokens (ref-doc's Claude Code cap).
**No `response_format` toggle** — markdown only. **Has a pagination
cursor nudge** at the tail of the response ("There may be more results
available. To see the next page, use: start_row: 100, row_limit: 100")
— good. No field-filtering parameter.

**`get_search_by_page_query` (line 1598)** — **the most expensive tool
definition** (606 tokens) and the only tool with a `response_format` enum.
**Surprising finding:** at the default 20-row size the JSON mode is
**2.07× more expensive** than the markdown mode (1,149 vs 554 tokens). The
v0.5.0 commit message says the JSON mode is lighter than the markdown it
replaced, but the markdown path is still active and measurably cheaper at
low row counts. JSON's overhead comes from repeated field names and the
`summary`/`possibly_truncated`/`total_rows_returned` metadata block.
JSON only wins at row counts where markdown's per-row pipe-separator
overhead overtakes JSON's field-name duplication — that crossover is
well above 20. Agents using the default `row_limit=20` will spend more
tokens by opting into JSON, not fewer.

**`get_search_analytics` (line 687)** — `rowLimit: 20` is **hardcoded**
(`gsc_server.py:712`). No parameter to change it. In the live sample the
full 28-day query response exhausts at 20 rows (532 tokens). Silent cap —
does not inform the agent more rows exist.

**`gsc_get_landing_page_summary` (line 1817)** — docstring claims
"≤~3k tokens" (line 1831). Live measurement at `top_n=25`: **2,010
tokens**. Within the claim. Cleanly structured compact dict. No
`response_format` toggle but the dict shape is already dense.

**`batch_url_inspection` (line 977)** — `limit` clamped to 10
(`gsc_server.py:1049`). Pagination validation occurs **before** OAuth
(good, shipped in v0.4.1 `45c0539`). Fixed 4-field output. No live
sample (quota-sensitive) — synthesized from code.

**Hidden bug found during source review:** the inner loop at
`gsc_server.py:1083` iterates over URLs and at `:1091` has the comment
`# Execute request with a small delay to avoid rate limits` — but
**there is no delay**. The next line calls `.execute()` immediately.
The sibling tool `check_indexing_issues` has the same hot-loop
pattern at `gsc_server.py:1165` with no delay. Under realistic GSC
quota (600 queries per minute per project), 10-URL batches will
occasionally trigger 429 rate-limit errors; 10-URL batches called
back-to-back by an agent will trigger them often. See Remediation
Plan item A.7.

**`inspect_url_enhanced` (line 859)** — verbose multi-section markdown,
including the GSC web-UI tracking link with `utm_medium`/`utm_source`
query params in the response (~60 tokens of pure tracking parameters
that the agent cannot use). Referring URLs can blow up on pages with
many backlinks. No truncation.

**`list_properties` (line 530)** — **unpaginated**. Returned all 17
verified properties in our sample. Agency accounts with hundreds of
properties would pay the full list every call. Only filter is a
substring match (`name_contains`) on the site URL, added in v0.4.0.

**`gsc_health_check` (line 2697)** — excellent structured dict; returns
`"not available"` stubs for the manual-actions and security-issues
checks that the GSC v1 API doesn't expose (honest). `ok: true/false`
envelope is a quality error shape that the rest of the server doesn't
share. 180 tokens on the happy path.

**`compare_search_periods` (line 1467)** — second hidden token hog.
Hardcodes `"rowLimit": 1000` for **both** period requests
(`gsc_server.py:1499` and `:1506`). No user-facing parameter to
tune. Every call pulls 2,000 rows from the upstream API and joins
them in-memory; the response is capped downstream by the user's
`limit` parameter (default 10), but the hidden upstream cost is
always 2 × 1,000 rows. Upstream token cost masquerades as
response-shape discipline. See Remediation Plan item A.8.

**`get_creator_info` (line 3161)** — static attribution. Low value per
call but also low cost (36 tokens schema / ~40 token response). Fine.

---

## 4. Error handling

Pulled every `raise | except | ValueError` via grep — 97 occurrences.
Classification:

| Pattern | Count | Example | Quality |
|---|---:|---|---|
| Input validation with instructive `ValueError` | ~14 | `gsc_server.py:80–84` (alias regex rules), `1856–1871` (landing-page range validation), `1040–1046` (batch pagination) | **Instructive** — names the parameter, shows valid range/regex, sometimes gives an example |
| Generic `except Exception` → `f"Error …: {str(e)}"` | ~35 | `gsc_server.py:795, 856, 1124–1125, 1211–1212` | **Terse** — no remediation hint, no parameter, just the upstream exception wrapped in a string |
| Structured `{ok: False, error, tool}` envelope | ~5 | `gsc_server.py:2733–2737` (`gsc_health_check`), `2826–2827` (SF bridge) | **Good** — parseable, composable |

**Live bug found during sampling.** `get_sitemaps` returned:

```
Error retrieving sitemaps: '>' not supported between instances of 'str' and 'int'
```

Root cause: `gsc_server.py:837` does `sitemap["errors"] > 0`, but the GSC
Sitemaps API returns `errors` as a **string**. The exception message is
leaked directly to the caller with no hint that this is a server bug
(not a user error). The sibling tool `list_sitemaps_enhanced` works
fine against the same property, so there's no fallback guidance. This
is both a **correctness bug** and an **error-quality bug** — the agent
has no way to recover.

Retry-storm risk: **moderate**. The terse `"Error retrieving X: {str(e)}"`
pattern gives agents no parameter-shaped feedback, so an LLM may retry
with the same arguments. Observed retry-storm example class: rate-limit
errors from `batch_url_inspection` surface as `HttpError` string wrap
(`gsc_server.py:1124–1125`) with no backoff guidance and no "wait N
seconds" hint.

---

## 4a. Auth / deployment hazard

Found during source review after live sampling: `get_gsc_service_oauth`
calls `flow.run_local_server(port=0)` at `gsc_server.py:519` (on token
refresh failure path) and at `gsc_server.py:2582` (inside
`add_account`). In a headless MCP context — e.g. Claude Desktop's MCP
subprocess, any CI runner, or an SSH-hosted agent — there is no
browser to complete the OAuth flow. `run_local_server` blocks the
server indefinitely waiting for a redirect that will never arrive. No
timeout, no "headless mode detected" error. This is an availability
bug, not a token bug — but it belongs in the remediation plan. See
Remediation Plan item A.9.

## 5. Observability

Zero. No `logging` import in `gsc_server.py` (grepped). No `print` to
stderr on tool entry/exit. No token accounting. No request/response
correlation IDs. No MCP Inspector configuration committed.

The server can be connected to MCP Inspector manually (standard FastMCP
wiring works) but there is no convenience script or `Makefile` target for
it.

**Gap:** without observability, any remediation we ship will be
unmeasurable in production. The `audit/04-eval-harness.md` doc proposes
a local harness to close this gap for before/after comparisons.

---

## 6. Summary of factual findings

1. **29 tools** exposed (CLAUDE.md says 24 — stale).
2. **Schema tax ≈ 5,098 tokens** (cl100k_base). Probably 6.5–8k after
   FastMCP's real inputSchema boilerplate. Well under the 10% of
   200k-context window that would auto-trigger tool search in Claude.
3. **5 of 29 tools use the `gsc_` prefix**. Inconsistent.
4. **Three overlapping analytics tools** (`get_search_analytics`,
   `get_advanced_search_analytics`, `get_search_by_page_query`) with no
   disambiguation guidance.
5. **Heaviest per-call response**: `get_advanced_search_analytics` at 100
   rows ≈ 2,689 tokens; at the default 1,000 rows it projects to
   ~25k+.
6. **`get_search_by_page_query` JSON mode is heavier, not lighter, than
   markdown at the default 20-row call**: 1,149 vs 554 tokens. The
   v0.5.0 commit claim is not borne out at typical row counts.
7. **`get_search_analytics` is capped at a silent hardcoded rowLimit=20**
   (`gsc_server.py:712`).
8. **`list_properties` is unpaginated** — 17 rows in our sample, scales
   linearly with account size.
9. **`get_sitemaps` is broken** (`gsc_server.py:837` — str/int compare).
   Terse error message.
10. **Error quality is bimodal**: input validation is instructive,
    upstream API failures fall back to `str(e)`. No structured
    error envelope on the ~24 legacy tools.
11. **`batch_url_inspection` and `check_indexing_issues` have no
    request-pacing delay** despite a code comment promising one
    (`gsc_server.py:1091, 1165`). Rate-limit exposure.
12. **`compare_search_periods` hardcodes `rowLimit=1000` for both
    periods** (`gsc_server.py:1499, 1506`). 2,000-row hidden upstream
    fan-out, no user knob.
13. **Headless-unsafe OAuth fallback** — `flow.run_local_server(port=0)`
    blocks the server indefinitely if no browser is available
    (`gsc_server.py:519, 2582`).
14. **No observability layer**.
15. **Documentation drift** — CLAUDE.md + README say 24 tools; actual
    is 29.

# 02 — Scorecard

**Rubric:** the four-layer framework from `docs/MCP-Optimal-Design.md`.
Score 1 = absent, 5 = exemplary. Evidence cites source lines in
`gsc_server.py` or files under `audit/_work/`.
**Token numbers:** `tiktoken` `cl100k_base` proxy.

---

## Post-Tranche-A validation (2026-04-17)

Tranche A (8 of 9 items) shipped as **v0.6.0** (commit `352219c`). A.4
namespace rename stays deferred to v1.0.0. Measured delta from the
eval harness against `sc-domain:chaserhq.com` (6 shared prompts,
`claude-sonnet-4-6`, temperature=0, caching disabled):

| Metric | Baseline (a808da4) | v0.6.0 | Δ |
|---|---:|---:|---:|
| `tool_definitions_tokens` | 41,736 | 40,272 | **−1,464 (−4%)** |
| `prompt_tokens` | 134,071 | 119,476 | −14,595 (−11%) |
| `grand_total_tokens` | 178,522 | 166,096 | **−12,426 (−7%)** |
| `total_tool_calls` | 9 | 7 | −2 (−22%) |
| `error_count` | 0 | 0 | — |

Full delta in `audit/eval/runs/v050-v060-6prompt-delta.md`. Biggest
single-prompt win: P7 analytics (−14,436 tokens). Schema-tax drop
(−1,464 across 6 prompts; live FastMCP schema size moved from 6,956
→ 6,712 tokens per call, verified via `run.py --probe-mcp`)
confirms A.5 description tightening measurably improves real calls,
not just `cl100k_base` proxy counts.

The pre-implementation projection "~2,500–26,000 tokens per session,
dominated by A.1" still stands; the eval subset didn't include
enough calls that actually hit the old 1,000-row default to realize
the top end.

---

## Scores

| Layer | Criterion | Score | Evidence | Token impact (tokens per typical session) |
|---|---|---:|---|---|
| **1. Tool surface** | Tool consolidation (workflow-oriented vs endpoint-mirror) | **2/5** | 17 of 29 tools are 1:1 wrappers of GSC endpoints; 12 are composed. Newer `gsc_*` tools are workflow-oriented but legacy tools dominate. Three overlapping analytics tools (`get_search_analytics:687`, `get_advanced_search_analytics:1334`, `get_search_by_page_query:1598`) mirror one upstream endpoint with no shared abstraction. | Each endpoint-mirror tool adds 60–400 schema tokens without composing a high-value workflow. Estimated ~1,200 tokens recoverable via consolidation. |
| **1. Tool surface** | Namespace prefixing | **2/5** | 5 of 29 tools prefixed `gsc_*` (`gsc_get_landing_page_summary:1817`, `gsc_compare_periods_landing_pages:1992`, `gsc_load_from_sf_export:2835`, `gsc_query_sf_export:2948`, `gsc_health_check:2697`). 24 are bare. Names like `list_properties`, `get_search_analytics`, `batch_url_inspection` collide readily with other MCP servers. | No direct token cost but raises hallucination and agent-misrouting risk in multi-server configs. |
| **1. Tool surface** | Progressive disclosure / tool count discipline | **2/5** | All 29 tool schemas load on every session start. No dynamic tool loading, no `tool_search` pattern. 29 is just over the ref doc's ~20-tool threshold but the schema tax (5,098 tokens) is ~2.5% of a 200k context, so auto-trigger hasn't fired. | **Schema tax ≈ 5,098 tokens, paid every session.** Likely ~6.5–8k after FastMCP inputSchema boilerplate. |
| **2. Response shaping** | Default pagination / limits | **2/5** | Hardcoded silent cap: `get_search_analytics` rowLimit=20 (`gsc_server.py:712`). `get_advanced_search_analytics` default `row_limit=1000` (`gsc_server.py:1340`; clamp at `:1382`) — too permissive; one call at 1000 rows projects to ~25k+ tokens. `compare_search_periods` hardcodes `rowLimit=1000` for **both** periods (`:1499, :1506`) — 2,000-row hidden upstream fan-out per call, not user-tunable. `batch_url_inspection` quota-bound at 10 (`:1049`) — appropriate. `list_properties:530` **unpaginated** (returned all 17 properties in sample; unbounded for agency accounts). | A single default-configured call to `get_advanced_search_analytics` can burn ~25k tokens. Fixing the default to 100 saves ~22k tokens per call where the agent doesn't explicitly specify. `compare_search_periods` is a silent 2,000-row upstream pull every call. |
| **2. Response shaping** | Field filtering / `response_format` options | **2/5** | Only `get_search_by_page_query:1598` has a `response_format` enum (`markdown`\|`json`, v0.5.0). **Its JSON mode is 2.07× heavier than markdown at the default 20-row call: 1,149 vs 554 tokens** (`_work/sample_tokens.json`). This is a **trade-off, not strictly an anti-feature** — JSON is more parseable for downstream code (exact types, no table-parsing heuristics) and carries a `summary` block markdown lacks. But neither the tool description nor the default steers the agent toward the cheaper shape for summary-style questions. No tool exposes field selection. | Mixed: JSON's extra tokens buy downstream parseability. Agents doing in-line summarization should prefer markdown (-595 tokens/call × ~4 drill-downs/session ≈ 2,400 tokens). Agents passing data to further code justifiably pay the JSON cost. Remediation target: steer the default, don't remove JSON. |
| **2. Response shaping** | Truncation with helpful nudges | **2/5** | `get_advanced_search_analytics` ends its response with a textual pagination cursor nudge ("use: start_row: 100, row_limit: 100") — **good**. `get_search_analytics:712` silently caps at 20 with no nudge. `get_search_by_page_query` JSON mode returns `possibly_truncated: true` structured but no prose nudge — ref doc warns models routinely ignore structured `nextCursor` fields without prose. `batch_url_inspection` clamps `limit` to 10 silently. | Silent capping causes agents to believe they have the full dataset and make wrong conclusions (see v0.5.0 commit `a808da4` that fixed exactly this class of bug). |
| **2. Response shaping** | Format choice (CSV vs JSON for tabular data) | **2/5** | Markdown pipe-tables are used for most tabular responses — density is close to CSV (~15–20% overhead vs true CSV based on our sample). No CSV mode anywhere. JSON mode exists on one tool but is heavier than markdown at default sizes (see above). | CSV would save ~15–20% on the largest tabular responses. On `get_advanced_search_analytics` at 100 rows (2,689 tokens) that's ~450 tokens per call. |
| **2. Response shaping** | Human-readable IDs vs cryptic UUIDs | **4/5** | All primary identifiers are URLs (`site_url`, `page_url`, `sitemap_url`), account aliases (free-form, validated via regex at `gsc_server.py:80–84`), or GSC property URLs with `sc-domain:` prefix. Only UUID in sight is the `session_id` returned by `gsc_load_from_sf_export:2835` (UUID4) — minor. | Low token impact but high routing-accuracy impact. |
| **3. Descriptions** | Verb + resource clarity, 1–2 sentence discipline | **3/5** | Most first-sentences follow `Verb + Resource + (qualifier)` (e.g. "Get search analytics data for a specific property"). But some docstrings are multi-paragraph: `get_search_by_page_query` description is 531 tokens — **single biggest description in the server**. `gsc_get_landing_page_summary:1817` is 339 tokens. That prose is sometimes necessary (parameter constraints, row_limit guidance) but often duplicates the signature. | Top 5 tools own 1,748 description tokens. Tightening to the ref-doc 1–2 sentence discipline could halve that (~800 tokens). |
| **3. Descriptions** | Disambiguation between similar tools | **1/5** | Three analytics tools (`get_search_analytics`, `get_advanced_search_analytics`, `get_search_by_page_query`) have near-identical first sentences and no "pick me when…" guidance. Three URL-inspection tools (`inspect_url_enhanced`, `check_indexing_issues`, `batch_url_inspection`) similarly unlabeled. `get_sitemaps` vs `list_sitemaps_enhanced` are essentially the same tool (and `get_sitemaps` is broken — see Discovery §4). | Misrouting costs whole tool calls, not just tokens. Each wrong pick is 200–2,700 tokens of wasted response plus a retry. |
| **3. Descriptions** | Parameter naming and examples | **3/5** | Parameter names are strong (`site_url`, `page_url`, `row_limit`, `response_format`). Newer tools include examples in docstring (e.g. `gsc_get_landing_page_summary:1831` — "`'today'`, `'yesterday'`, `'Ndaysago'`, or YYYY-MM-DD"). Older tools only list params with one-liners (`get_search_analytics:687`). | Missing examples force agents to guess and retry on error; bounded but real. |
| **4. Architecture** | Code-execution suitability assessment | **4/5** | Server is **correctly not** using a code-execution architecture. 29 tools × ~180 tokens avg definition = 5k schema tax; upstream GSC API is ~15 endpoints, not 2,500; stdio + single-user session. Ref-doc's code-mode payoff curves are built for huge APIs and/or HTTP multi-tenant servers. One hairline concern: server-side composed tools like `compare_search_periods:1467` and `gsc_get_landing_page_summary:1817` do in-memory data wrangling (2,000-row join, heap-sort) that a code-execution sandbox would let the agent do itself on raw rows. For this server's scale the server-side composition is the right call, but at ~50+ tools the arithmetic flips. | No implementation cost; the architectural choice is appropriate at current scale. |
| **Cross-cutting** | Error message quality | **2/5** | Bimodal. Input validation is instructive (`gsc_server.py:80–84` alias regex, `1856–1871` landing-page range, `1040–1046` batch pagination, `3009–3042` SF query). Upstream API failures fall back to `f"Error …: {str(e)}"` (`:795, 856, 1124–1125, 1211–1212`). **Live bug:** `get_sitemaps` returned `"Error retrieving sitemaps: '>' not supported between instances of 'str' and 'int'"` — root cause is `gsc_server.py:837` comparing a string-typed `sitemap["errors"]` to `0`. Agent has no recovery hint. | Each terse error risks an agent retry with unchanged args. Conservative estimate: 1–2 retry storms per typical session × 500–1,000 tokens each = 500–2,000 tokens. |
| **Cross-cutting** | Observability / measurability | **1/5** | No `logging` import. No token counting. No structured request/response logging to stderr. No MCP Inspector config committed. Any remediation we ship will not be measurable in production without first instrumenting. | Not directly token-costly, but blocks validation of every recommendation below. |

---

## Overall maturity

**Average: 2.4/5** (sum 33 ÷ 14). The server has a solid functional
core, some recent forward-looking work (v0.4.0 composed tools, v0.5.0
opt-in JSON), but is still bleeding tokens from silent defaults,
duplicated legacy analytics tools, inconsistent namespacing, hidden
upstream fan-outs (`compare_search_periods` / `batch_url_inspection`
rate pacing), and unmeasurable observability. The one response-shape
feature (JSON mode) is a trade-off that isn't documented as one.

---

## Top-3 token-bleed offenders (ranked by tokens × calls-per-session)

Ranking metric: tokens wasted *per typical analyst session*, not just
per-call. A "typical session" here = one property audit that involves a
performance overview, a top-pages roll-up, and 3–5 per-page query
drill-downs. Schema tax is paid once.

| # | Offender | Per-call tokens | Calls per session | Tokens wasted per session vs best-available shape | Evidence |
|---|---|---:|---:|---:|---|
| 1 | **`get_advanced_search_analytics` default `row_limit=1000`** (`gsc_server.py:1340`; clamp `:1382`) | up to ~25,000 at 1000 rows | 0–2 (only sessions where agent hits the bare default) | ~22,000 per call when hit (vs 100-row default delivering ~2,700) | `_work/sample_tokens.json`; ref doc 25k cap |
| 2 | **`get_search_by_page_query` JSON mode heavier than markdown at 20 rows** | 1,149 JSON vs 554 markdown | 3–5 per-page drill-downs | ~2,400 *if* agent opts into JSON for summary-style questions | `_work/sample_tokens.json`; commit `a808da4` |
| 3 | **Schema tax from 29 tools with verbose descriptions** (top-5 tools own 47%) | 5,098 total, heaviest is `get_search_by_page_query` at 606 tokens | 1 load per session | ~1,500 recoverable via tight 1–2-sentence descriptions + disambiguation | `_work/tool_tokens.json` |

**Honest reading of the headline number.** Offender #1 is a *per-call
upper bound*, not a per-session guarantee. A session only realizes the
~22k saving if the agent reaches for `get_advanced_search_analytics`
without specifying `row_limit` and the 1,000-row response actually
flows into context. The 2,400-token saving from offender #2 depends
on the agent choosing JSON today (the default is markdown, so this
is avoidable bleed only for agents that have been told to prefer
structured output). **Realistic per-session range is
~2,500–26,000 tokens**, with the low end representing a session that
never triggers offender #1 and the high end representing one that
triggers it multiply. Report both bounds when pitching savings.

---

## Top-3 quick wins

Ranked by (token saving × probability agent hits it) ÷ effort.

| # | Quick win | Effort | Token saving per session | Risk |
|---|---|---|---:|---|
| 1 | **Lower `get_advanced_search_analytics` default `row_limit` from 1000 → 100** and keep the existing pagination cursor nudge. | 1 line + 1 line in docstring. | ~22,000 tokens per call where the agent didn't specify. Biggest win by a mile. | Low — agents who want 1000 rows can still pass `row_limit=1000`. |
| 2 | **Fix the `get_sitemaps` bug** at `gsc_server.py:837` (cast `sitemap["errors"]` via `int(...)` or `str>0` guard) **and** wrap upstream errors in the `{ok, error, hint}` envelope pattern already used by `gsc_health_check`. | Few lines, but touches every `except Exception` site. Scope to the three sitemap tools + the two inspection tools for Tranche A. | Prevents retry loops (~500–2,000 tokens per storm). Also unblocks a currently-broken tool — correctness win. | Low — bug fix is pure addition; envelope change needs a migration note. |
| 3 | **Parameterize `get_search_analytics` `row_limit`** (drop hardcoded 20 at `:712`) and add a textual truncation nudge wherever capping happens. | One parameter addition + a constant nudge string in the 3 capped tools. | ~500 tokens per agent that otherwise retries blind. Also fixes the silent-undercount class of bug that v0.5.0 fixed for one sibling tool. | Low — backward-compatible default. |

Honorable mention (not in top-3 because it needs a structural
change, not a parameter tweak): **re-examine `get_search_by_page_query`
JSON mode**. At the default `row_limit=20` it's heavier than markdown.
Either raise the default row count at which JSON becomes the default
output, or strip the metadata block from low-row-count JSON responses.
Moved to `03-remediation-plan.md` as Tranche B work because it touches
the return type.

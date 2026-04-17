# 04 — Evaluation harness (proposal, not built)

Purpose: measure whether the Tranche A and Tranche B remediations from
`03-remediation-plan.md` actually save tokens without hurting answer
quality or adding tool-call churn. Without this harness we are
shipping on vibes.

---

## 1. Test prompt set

**25 multi-step prompts.** Within the ref doc's 20–50 range. Mix is:

| # | Prompt (abbreviated) | Expected tool path | Why it's here |
|---|---|---|---|
| 1 | "What's my active GSC account?" | `get_active_account` | Smoke test |
| 2 | "List the GSC properties I have access to that contain 'idr'" | `list_properties(name_contains='idr')` | `name_contains` happy path |
| 3 | "Show every property I can see" | `list_properties` | Tests unpaginated behavior / post-A.6 cap |
| 4 | "Switch to the 'client-a' account" | `list_accounts` then `switch_account` | Multi-account flow |
| 5 | "Run a health check on `sc-domain:example.com`" | `gsc_health_check` | Structured-dict tool baseline |
| 6 | "Give me a 28-day performance overview of `sc-domain:example.com`" | `get_performance_overview` | Medium-cost tabular |
| 7 | "Top 100 queries by clicks for `sc-domain:example.com` last 28 days" | `get_advanced_search_analytics(row_limit=100)` | Tests default-limit change (A.1) |
| 8 | "Top 1000 queries for last 7 days, sorted by impressions descending" | `get_advanced_search_analytics(row_limit=1000, sort_by='impressions')` | Explicit high-limit case |
| 9 | "Compare last 28 days vs the prior 28 days by query" | `compare_search_periods` | Period-comparison routing |
| 10 | "Which of my landing pages are in striking distance of page 1?" | `gsc_get_landing_page_summary` | Composed-tool happy path |
| 11 | "Compare landing-page performance this quarter vs last quarter" | `gsc_compare_periods_landing_pages` | Composed-tool diff |
| 12 | "Which queries drive the top-performing page on `sc-domain:example.com`?" | `gsc_get_landing_page_summary` → `get_search_by_page_query` | **Tests disambiguation** between three analytics tools |
| 13 | "Break down queries for `https://example.com/path-1/` as JSON" | `get_search_by_page_query(response_format='json')` | Tests the 2.07× JSON overhead discovery; post-B.5 should flip |
| 14 | "Is `https://example.com/path-1/` indexed?" | `inspect_url_enhanced` | Single URL inspection |
| 15 | "Batch-check these 8 URLs for indexing issues: …" | `batch_url_inspection` | Tests quota-bound limit (10) |
| 16 | "Which of these URLs have indexing problems? [list of 5]" | `check_indexing_issues` | **Tests disambiguation** between three URL-inspection tools |
| 17 | "List all sitemaps for `sc-domain:example.com`" | `list_sitemaps_enhanced` (not broken `get_sitemaps`) | **Tests the sitemap bug** — agent should route to the working tool pre-A.3, either tool post-A.3 |
| 18 | "Show me details for sitemap `https://example.com/sitemap.xml`" | `get_sitemap_details` | Simple drill-down |
| 19 | "Submit a new sitemap at `https://example.com/new-sitemap.xml`" | `submit_sitemap` (write op — flag in harness) | Write-path routing (may be skipped on read-only eval runs) |
| 20 | "Load my Screaming Frog export from `/path/to/export.csv` for `sc-domain:example.com`" | `gsc_load_from_sf_export` | SF bridge entry |
| 21 | "Query the SF session for all URLs with status 200 ordered by inlinks" | `gsc_query_sf_export` | SF bridge query |
| 22 | "Find the 5 striking-distance pages, then run per-page query drilldown on each" | `gsc_get_landing_page_summary` → 5× `get_search_by_page_query` | **Chained workflow** — ref doc's "multi-step task" requirement |
| 23 | "Show me striking-distance pages for UK desktop only" | `gsc_get_landing_page_summary(country='gbr', device='DESKTOP')` | Filter-path routing |
| 24 | "Compare `chaser login` query position this month vs last" | `compare_search_periods(filter_expression='chaser login')` — or a sequence if filter isn't on that tool | Tests param filtering vs adjacency |
| 25 | "Drop the 'client-a' account" | `remove_account` (write op — flag) | Account mutation routing |
| 26 | "My GSC quota is exceeded right now, check sitemaps anyway" | `list_sitemaps_enhanced` after 429 from injected failure | **Error-recovery** — does the agent retry sensibly or fall into a retry storm? |
| 27 | "Inspect this URL, I don't have permission for it" | `inspect_url_enhanced` returning 403 | **Error-recovery** for permission failures — tests A.9 guard and terse→instructive error shape (B.4) |
| 28 | "Get all 2,500 queries from sc-domain:example.com" | `get_advanced_search_analytics` → agent should follow the pagination cursor nudge ≥3 times | **Tests pagination follow-through**: does the agent actually use the `start_row: 100, row_limit: 100` tail nudge, or stop at the first page? |
| 29 | "Scan every sitemap for errors" | `list_sitemaps_enhanced` → fans out to `get_sitemap_details` per row | Multi-step routing across sitemap tools |
| 30 | "Compare last month vs this month for my top 50 landing pages" | `gsc_compare_periods_landing_pages` with large `limit` | Composed tool under larger N |

**Read-only vs write-path split.** Prompts 4, 19, 25 are write/mutate.
Baseline harness runs read-only (27 prompts; 22 original + 26/27/28/29/30).
A separate quarterly run exercises all 30 against a throwaway test
account. The ref doc's 20–50 range is now met at 30.

---

## 2. Metrics captured per run

One JSON record per prompt per run:

```json
{
  "run_id": "v0.5.0-baseline-2026-04-17",
  "prompt_id": 7,
  "model": "claude-sonnet-4-6",
  "temperature": 0.0,
  "prompt_cache": "disabled",
  "tool_definitions_tokens": 5098,
  "prompt_tokens": 142,
  "completion_tokens": 318,
  "cache_read_tokens": 0,
  "cache_write_tokens": 0,
  "tool_calls": [
    {"name": "get_advanced_search_analytics", "args": {...}, "response_tokens": 25041, "dur_ms": 812, "error": null}
  ],
  "total_tool_calls": 1,
  "total_response_tokens": 25041,
  "grand_total_tokens": 30599,
  "wall_clock_ms": 1104,
  "error_count": 0,
  "retry_count": 0,
  "final_answer": "...",
  "golden_diff": "exact_match" | "text_differs_semantic_match" | "mismatch"
}
```

Aggregated per-run roll-up: sum of each metric across the 22 or 25
prompts; plus the `tool_definitions_tokens` counted once per session
(not per prompt).

### Why split tokens this way

- **`tool_definitions_tokens`**: schema tax. Paid once per session.
  Changes when tool surface changes (Tranche A.4 rename, B.1
  consolidation).
- **`prompt_tokens` / `completion_tokens`**: agent reasoning overhead.
  Changes when tool descriptions tighten (Tranche A.5) or
  disambiguation improves (B.1).
- **`total_response_tokens`**: the ref doc's "result tax". This is where
  default-limit and response_format changes (A.1, A.2, A.6, B.2, B.5)
  show up.
- **`cache_read` vs `cache_write`**: never aggregate into a single
  "tokens" number. Caching can make absolute totals look artificially
  low; keeping them separate prevents lying to ourselves.
- **`tool_calls` list + `retry_count`**: tool-routing regressions and
  retry-storm changes (B.4 error-envelope work).

---

## 3. Golden-answer source

There is no pre-existing test corpus. Bootstrapping protocol:

1. **First baseline run against `main` (v0.5.0)**: run the 22 read-only
   prompts. I (Claude) produce a first-pass transcript for each.
2. **Human review**: you sign off on each `final_answer` as either
   correct, partially correct, or wrong. Sign-off comments become the
   authoritative `golden_answer` for that prompt.
3. **Freeze**: commit the signed-off transcripts to
   `audit/eval/golden/<prompt_id>.md`.
4. **Subsequent runs** diff against golden:
   - `exact_match` — character-identical
   - `text_differs_semantic_match` — different wording, same facts
     (re-verified by a judge LLM run; results spot-checked)
   - `mismatch` — requires human review; block merge if uninvestigated

This gives us a reproducible ground truth without pretending we have
one on day 1.

### Judge-LLM protocol

For the `text_differs_semantic_match` check, use a separate model call
with temperature=0, prompt = "Does response A assert the same set of
facts as golden B? Yes/no with reasoning." Log the judge's transcript
too — judge drift is itself a metric worth watching.

**Honest caveat — temperature=0 is not deterministic in practice.**
Even at T=0, Anthropic (and every other LLM provider) has position-bias
and float-nondeterminism in batched inference. Two runs of the same
prompt under T=0 can disagree. Mitigations:
- **Run each judge call three times; take majority vote.** Log all
  three transcripts. A 3-vs-0 vote is trustworthy; a 2-vs-1 escalates
  to human review.
- **For structured tool outputs** (dict shapes from
  `gsc_get_landing_page_summary`, `gsc_health_check`, etc.) prefer a
  **deterministic structured diff** (keys present, numeric values
  within ±0.5%) over the judge LLM. Only fall back to the judge for
  free-text markdown responses where wording legitimately varies.
- **Report the judge model's version** with every verdict. When the
  judge model is upgraded, all existing verdicts are stale until
  re-verified on a random 10% sample.

This protocol trades some runtime (3× judge calls on ~30% of prompts
that aren't exact-match) for defensibility of the numbers.

---

## 4. Eval controls (critical for diff validity)

Non-negotiable controls on every run:

- `temperature=0.0` always. Even `0.1` introduces enough routing
  variance to swamp Tranche A's ~2k-token savings on some prompts.
- **Prompt caching: disabled for "absolute measurement" runs.** If
  caching is enabled for realism runs, `cache_read_tokens` and
  `cache_write_tokens` are logged separately and **never summed into a
  single token figure** alongside non-cached runs.
- **Model pinned**: exactly one model ID across baseline and post
  runs. Proposed: `claude-sonnet-4-6`. If the team wants to show the
  impact under Claude Opus, run Opus separately and never cross-pool.
- **Seed prompts replayed verbatim.** Prompts live in
  `audit/eval/prompts.yaml`; changing a prompt creates a new prompt ID.
- **Same test GSC property / same SF export.** `sc-domain:chaserhq.com`
  is the reference property for this audit; rotate before release if
  the property's data materially changes.
- **Same day-of-month for date-relative prompts.** GSC data changes
  daily. "Last 28 days" must be interpreted against a frozen "today"
  in the harness (inject a fake date at the tool layer, or use
  absolute dates in prompts).

---

## 5. Harness execution

### Local (default)

- Python script `audit/eval/run.py` wraps the Anthropic SDK with
  `client.messages.create(...)` using MCP stdio transport to our
  `gsc_server.py`.
- One command: `uv run python audit/eval/run.py --run-id v0.5.0-baseline
  --read-only`.
- Output: `audit/eval/runs/<run_id>.jsonl`. One JSON line per prompt.
- Aggregator: `audit/eval/aggregate.py` reads a run file and emits a
  summary markdown (totals + per-prompt deltas vs the named baseline).

### CI (optional, second phase)

- Run the read-only harness on every PR that touches `gsc_server.py`.
- Gate merges on: `total_tool_calls` not increased, `grand_total_tokens`
  not regressed by more than 5%, `mismatch` count = 0.
- Requires a CI secret for the Anthropic API key and a test OAuth
  token for GSC. Only do this once the human sign-off in §3.2 has
  happened and a stable golden exists.

---

## 6. Baseline → post comparison protocol

1. Tag `main` at v0.5.0.
2. Run the harness against the tag: `run.py --run-id v0.5.0-baseline`.
3. Human sign-off on goldens (§3.2).
4. Freeze goldens to `audit/eval/golden/`.
5. For each remediation PR:
   - Run `run.py --run-id <pr-branch>`.
   - Run `aggregate.py --compare v0.5.0-baseline <pr-branch>`.
   - Expected effects per item (Tranche A):
     - A.1 on prompt 7: `total_response_tokens` drop from ~25k → ~2.7k.
     - A.2 on prompt 6: unchanged tokens but `retry_count` drops where
       agents were retrying against the silent 20-row cap.
     - A.3 on prompt 17: `error_count` drops from ≥1 to 0 when the
       agent tries `get_sitemaps` first.
     - A.4: `tool_definitions_tokens` flat (rename is free), but
       routing accuracy in multi-server configs improves (separate
       cross-server eval needed — out of scope for this harness).
     - A.5 on prompts 12, 16: `tool_definitions_tokens` drops ~800;
       mis-routing on the three-analytics-tool disambiguation drops.
     - A.6 on prompt 3: response size capped at ~700 tokens.
   - PR merges only if no `mismatch` regressions and `grand_total_tokens`
     does not regress.
6. Aggregate expected Tranche A savings into a single before/after
   panel in the v0.6.0 CHANGELOG.

---

## 7. Honest gaps

- **No production logs.** Baselines are synthetic (one account, one
  set of prompts) and cannot represent real user workload mix. Treat
  post-change numbers as *directional*, not absolute.
- **One property, one account.** A multi-property / multi-account
  matrix would be more robust; current scope says one property.
- **Judge-LLM drift.** `text_differs_semantic_match` is itself a model
  call; its verdicts may shift as the judge model upgrades. Log the
  judge model ID with every verdict.
- **Harness ≠ production.** An agent in Claude Code behaves subtly
  differently from an agent driven by our thin SDK wrapper (tool-use
  selection, context management). Use the harness for A/B on this
  server, not as a proxy for end-user experience.

---

## 8. Out of scope

- Building the harness. This doc proposes it; implementation starts
  after sign-off on `03-remediation-plan.md`.
- Cross-server eval (loading gsc-mcp alongside other MCP servers to
  test namespace collision). Valuable but separate.
- Accuracy benchmarking against the real GSC UI. Our ground truth is
  the server's own API responses; if the server is wrong we'd be
  scoring against a wrong golden. Out of scope unless user sign-off.

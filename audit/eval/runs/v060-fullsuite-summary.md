# v0.6.0 read-only suite — partial run (25/27 prompts)

Ran against `HEAD` (commit `352219c`, v0.6.0) against
`sc-domain:chaserhq.com`. Stopped at prompt 28 when the Anthropic org's
**20M prompt-bytes/hour** rate limit kicked in; the baseline run
against `a808da4` needs to wait for the window to roll.

## Totals (25 shared, read-only prompts)

| Metric | Value | Per-prompt avg |
|---|---:|---:|
| `tool_definitions_tokens` (paid per-prompt) | 167,800 (6,712 × 25) | 6,712 |
| `prompt_tokens` | 588,001 | 23,520 |
| `completion_tokens` | 20,563 | 822 |
| `total_response_tokens` | 67,364 | 2,694 |
| `grand_total_tokens` | 843,728 | 33,749 |
| `total_tool_calls` | 35 | 1.4 |
| `error_count` | 0 | — |

## Routing match distribution

- `exact`: 17 / 25 (68%)
- `subset`: 2 / 25 (8%)
- `different`: 6 / 25 (24%)

"Different" does **not** mean "wrong" — it means the agent picked a
valid but different tool than the one we noted in `prompts.json`.
Most of the `different` cases post-A.2 happen because
`get_search_analytics` now handles `row_limit=100` natively, so the
agent reasonably picks it over `get_advanced_search_analytics` when
the user asked for "top 100 …".

## Top-5 token consumers

| ID | Category | Tool calls | Grand total | Routing |
|---:|---|---:|---:|---|
| 28 | pagination | 3 | 147,576 | `exact` |
| 22 | chained | 6 | 57,870 | `exact` |
| 11 | composed | 1 | 47,417 | `exact` |
| 12 | disambiguation | 2 | 36,960 | `different` |
| 24 | filter | 3 | 36,738 | `subset` |

P28 alone is 17% of the total. It paginates through 2,500 rows at
row_limit=1000 (3 pages × 1000 rows), which is exactly the workflow
A.1 is designed to make the *default* smaller but still handle
cleanly when explicitly requested. Routing=`exact` confirms agents
correctly follow the pagination cursor nudge.

## Failures / notable

- P20, P21 (SF bridge): `tools=0`, agent refused because
  `{{SF_EXPORT_PATH}}` was unresolved (no `--sf-export` arg). Expected.
- P29, P30: did not run before the rate limit fired.
- No exceptions in the harness itself. 0 `error_count` across all 25
  completed prompts.

## What we still need

Baseline (commit `a808da4` pre-audit) run on the same 25 prompts so
we can compute proper before/after deltas. Blocked by rate limit
until the 20M prompt-bytes/hour window rolls (~45 min from ~16:42 BST).

For the 4 prompts where we already have both sides
(see `v050-v060-sample-delta.md`), the delta is **−11,455 tokens
(−9%)** with the biggest win on P7 analytics (−14,457, agent made 1
clean call vs baseline's 3 confused calls).

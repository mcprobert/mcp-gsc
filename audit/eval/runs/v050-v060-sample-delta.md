# Eval delta: `v050-baseline-sample` → `v060-sample`

Baseline: 4 prompts. Compare: 4 prompts. Shared: 4.

## Totals across shared prompts

| Metric | Baseline | Compare | Delta |
|---|---:|---:|---:|
| `tool_definitions_tokens` | 27824 | 26848 | -976 (-4%) |
| `prompt_tokens` | 98705 | 84336 | -14369 (-15%) |
| `completion_tokens` | 1521 | 3339 | +1818 (+120%) |
| `total_response_tokens` | 503 | 2575 | +2072 (+412%) |
| `grand_total_tokens` | 128553 | 117098 | -11455 (-9%) |
| `total_tool_calls` | 7 | 5 | -2 (-29%) |
| `wall_clock_ms` | 41401 | 61229 | +19828 (+48%) |
| `error_count` | 0 | 0 | +0 (+0%) |

## Routing match counts (compare run)

- `exact`: 2
- `subset`: 0
- `different`: 2
- `unknown`: 0

## Per-prompt deltas (by grand_total_tokens)

| ID | Category | Baseline tokens | Compare tokens | Δ | Calls Δ | Routing (compare) |
|---:|---|---:|---:|---:|---:|---|
| 7 | analytics | 43817 | 29360 | -14457 | -2 | `different` |
| 3 | discovery | 25073 | 25331 | +258 | +0 | `exact` |
| 17 | sitemap | 25233 | 25558 | +325 | +0 | `exact` |
| 12 | disambiguation | 34430 | 36849 | +2419 | +0 | `different` |

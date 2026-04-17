# Eval delta: `v050-baseline-fullsuite` → `v060-fullsuite`

Baseline: 6 prompts. Compare: 25 prompts. Shared: 6.

## Totals across shared prompts

| Metric | Baseline | Compare | Delta |
|---|---:|---:|---:|
| `tool_definitions_tokens` | 41736 | 40272 | -1464 (-4%) |
| `prompt_tokens` | 134071 | 119476 | -14595 (-11%) |
| `completion_tokens` | 2106 | 3750 | +1644 (+78%) |
| `total_response_tokens` | 609 | 2598 | +1989 (+327%) |
| `grand_total_tokens` | 178522 | 166096 | -12426 (-7%) |
| `total_tool_calls` | 9 | 7 | -2 (-22%) |
| `wall_clock_ms` | 54712 | 67518 | +12806 (+23%) |
| `error_count` | 0 | 0 | +0 (+0%) |

## Routing match counts (compare run)

- `exact`: 4
- `subset`: 0
- `different`: 2
- `unknown`: 0

## Per-prompt deltas (by grand_total_tokens)

| ID | Category | Baseline tokens | Compare tokens | Δ | Calls Δ | Routing (compare) |
|---:|---|---:|---:|---:|---:|---|
| 7 | analytics | 43817 | 29381 | -14436 | -2 | `different` |
| 2 | discovery | 25168 | 24465 | -703 | +0 | `exact` |
| 1 | meta | 24809 | 24379 | -430 | +0 | `exact` |
| 3 | discovery | 25065 | 25332 | +267 | +0 | `exact` |
| 17 | sitemap | 25233 | 25579 | +346 | +0 | `exact` |
| 12 | disambiguation | 34430 | 36960 | +2530 | +0 | `different` |

## Coverage asymmetry

- Compare only: [5, 6, 8, 9, 10, 11, 13, 14, 15, 16, 18, 20, 21, 22, 23, 24, 26, 27, 28]

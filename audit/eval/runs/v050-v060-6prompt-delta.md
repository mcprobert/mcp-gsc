# Eval delta: `v050-baseline-fullsuite` → `v060-fullsuite`

Baseline: 6 prompts. Compare: 25 prompts. Shared: 6.

## Totals across shared prompts

| Metric | Baseline | Compare | Delta |
|---|---:|---:|---:|
| `tool_definitions_tokens` | 41736 | 40272 | -1464 (-4%) |
| `prompt_tokens` | 134071 | 119476 | -14595 (-11%) |
| `completion_tokens` | 2106 | 3750 | +1644 (+78%) |
| `total_response_tokens` | 609 | 2598 | +1989 (+327%) |
| `grand_total_tokens` | 136177 | 123226 | -12951 (-10%) |
| `total_tool_calls` | 9 | 7 | -2 (-22%) |
| `wall_clock_ms` | 54712 | 67518 | +12806 (+23%) |
| `error_count` | 0 | 0 | no change |

## Routing match counts (compare run)

- `exact`: 4
- `subset`: 0
- `different`: 2
- `unknown`: 0

## Per-prompt deltas (by grand_total_tokens)

| ID | Category | Baseline tokens | Compare tokens | Δ | Calls Δ | Routing (compare) |
|---:|---|---:|---:|---:|---:|---|
| 7 | analytics | 36695 | 21291 | -15404 | -2 | `different` |
| 2 | discovery | 18130 | 17744 | -386 | +0 | `exact` |
| 1 | meta | 17829 | 17653 | -176 | +0 | `exact` |
| 3 | discovery | 18027 | 18434 | +407 | +0 | `exact` |
| 17 | sitemap | 18177 | 18660 | +483 | +0 | `exact` |
| 12 | disambiguation | 27319 | 29444 | +2125 | +0 | `different` |

## Coverage asymmetry

- Compare only: [5, 6, 8, 9, 10, 11, 13, 14, 15, 16, 18, 20, 21, 22, 23, 24, 26, 27, 28]

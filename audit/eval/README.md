# GSC MCP — evaluation harness

Measures the token / accuracy / routing impact of Tranche A and Tranche B
remediations on the GSC MCP server. Implements the protocol spec'd in
`audit/04-eval-harness.md`.

## Layout

```
audit/eval/
  prompts.json         authoritative catalogue of 30 test prompts
  schema.py            dataclasses for run records (type-safe logging)
  run.py               runner — invokes a prompt set against the MCP
                       server via the Anthropic SDK with tool use
  aggregate.py         baseline-vs-post diff tool (emits markdown)
  requirements.txt     pinned deps (anthropic + stdlib)
  runs/                run outputs (jsonl, one line per prompt)
  golden/              human-signed-off golden answers for diff
```

## Prerequisites

- `ANTHROPIC_API_KEY` env var (for the `anthropic.Anthropic` client).
- GSC OAuth already configured in `accounts/` (same as the main server).
- Python deps: `pip install -r audit/eval/requirements.txt`.

## Quick start

```bash
# List the 30 prompts
.venv/bin/python audit/eval/run.py --list

# Smoke test a single prompt end-to-end
.venv/bin/python audit/eval/run.py \
    --run-id smoke-$(date +%s) \
    --only 1 \
    --property sc-domain:example.com

# Full read-only suite (27 prompts, skips write/mutate)
.venv/bin/python audit/eval/run.py \
    --run-id v0.6.0-postchange \
    --read-only \
    --property sc-domain:example.com

# Compare two runs
.venv/bin/python audit/eval/aggregate.py \
    --baseline runs/v0.5.0-baseline.jsonl \
    --compare  runs/v0.6.0-postchange.jsonl \
    --out      runs/v0.6.0-delta.md
```

## Controls (per audit/04-eval-harness.md §4)

Every run enforces:

- `temperature=0.0`
- Prompt caching **disabled** for absolute-measurement runs (caching
  changes token accounting — `cache_read` vs `cache_write` must never
  be aggregated with non-cached runs).
- Model pinned via `--model` (default `claude-sonnet-4-6`).
- Seed prompts replayed verbatim from `prompts.json`. Changing a
  prompt's wording requires a new prompt ID.

## Baseline bootstrap

No pre-existing golden corpus. First time through:

1. `git checkout a808da4` (pre-audit v0.5.0).
2. Run `run.py --run-id v0.5.0-baseline --read-only`.
3. Human review each `final_answer` in
   `runs/v0.5.0-baseline.jsonl`; mark correct / wrong / partial.
4. Copy the signed-off transcripts into `golden/`.
5. `git checkout main` (or v0.6.0 tag).
6. Run `run.py --run-id v0.6.0-postchange --read-only`.
7. `aggregate.py --baseline ... --compare ...` for the delta.

## What's **not** built yet (explicit)

- Judge-LLM semantic-match protocol (§3 in the eval doc). The runner
  records answers; a future pass adds `llm_judge.py` that runs a
  3-votes-majority judge over non-exact-match pairs.
- CI integration. Harness is strictly local for now.
- Multi-model matrix (the `--model` flag exists but we only use one
  at a time).

## Cost warning

Each full read-only run makes ~30 Anthropic API calls plus ~30 live
GSC tool calls. Budget: ~$0.50-$2 per run depending on model and
tool response sizes. Don't loop this in a CI cron without a cost cap.

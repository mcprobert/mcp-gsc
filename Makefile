# GSC MCP — dev loop targets.
#
# All targets use the in-repo .venv by default. Override with:
#     make PY=/usr/bin/python3 test
#
# `make inspect` spawns the server under MCP Inspector via npx so you
# can list tools, call them, and inspect responses from a browser UI.
# Requires Node.js + npx on PATH. The Inspector will print its UI URL
# to stderr when it starts.

PY ?= .venv/bin/python
PYTEST ?= .venv/bin/python -m pytest
SERVER := gsc_server.py

.PHONY: help
help:  ## Show this help
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# --- run the server ---

.PHONY: serve
serve:  ## Run the MCP server directly (stdio transport)
	$(PY) $(SERVER)

.PHONY: inspect
inspect:  ## Run the server under MCP Inspector (browser UI). Requires npx.
	@which npx >/dev/null || { echo "npx not found. Install Node.js first."; exit 1; }
	npx @modelcontextprotocol/inspector $(PY) $(SERVER)

# --- tests ---

.PHONY: test
test:  ## Run the full pytest suite
	$(PYTEST) -q

.PHONY: test-v
test-v:  ## Run pytest with verbose output
	$(PYTEST) -v

.PHONY: test-count
test-count:  ## Print the exact test count (used by commit messages)
	@$(PYTEST) --collect-only -q 2>/dev/null | tail -1

# --- eval harness ---

.PHONY: eval-list
eval-list:  ## List all eval-harness prompts
	$(PY) audit/eval/run.py --list

.PHONY: eval-probe
eval-probe:  ## Probe MCP plumbing without burning API credits
	$(PY) audit/eval/run.py --probe-mcp

# --- audit artefacts ---

.PHONY: tokenize
tokenize:  ## Rebuild audit/_work/tool_tokens.json (reproducibility)
	$(PY) audit/_work/tokenize_tools.py

.PHONY: rank
rank:  ## Rank tools by schema tax (reads tokenize output)
	$(PY) audit/_work/rank_tools.py

# --- telemetry one-shot ---

.PHONY: telemetry-probe
telemetry-probe:  ## Run probe-mcp with telemetry enabled so you can see stderr events
	GSC_MCP_TELEMETRY=1 $(PY) audit/eval/run.py --probe-mcp

.DEFAULT_GOAL := help

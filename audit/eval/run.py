"""GSC MCP eval-harness runner.

Invokes a prompt set through the Anthropic SDK with tool use. Tools are
bridged to the GSC MCP server over stdio via the stock `mcp` client
library.

Per audit/04-eval-harness.md §4, runs enforce:
    - temperature=0.0
    - prompt caching disabled (for absolute-measurement runs)
    - a pinned model id
    - seed prompts replayed verbatim from prompts.json

Usage:
    python audit/eval/run.py --list
    python audit/eval/run.py --run-id smoke --only 1 --property sc-domain:example.com
    python audit/eval/run.py --run-id v0.6.0-postchange --read-only \\
        --property sc-domain:example.com --page-url https://example.com/a
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional

# --- anthropic sdk ------------------------------------------------------
try:
    from anthropic import Anthropic
except ImportError:  # pragma: no cover — hinted to the user, not caught.
    sys.stderr.write(
        "Missing anthropic SDK. Install with:\n"
        "    uv pip install -r audit/eval/requirements.txt\n"
    )
    raise

# Load a project-local .env (if present) before constructing the client.
# The .env file is gitignored; put ANTHROPIC_API_KEY=... in `gsc-mcp/.env`.
try:
    from dotenv import load_dotenv
    _DOTENV_PATH = Path(__file__).resolve().parents[2] / ".env"
    if _DOTENV_PATH.exists():
        load_dotenv(_DOTENV_PATH)
except ImportError:
    # python-dotenv is an eval-harness dep; harness still runs if the key
    # is exported in the shell environment instead.
    pass

# --- mcp client ---------------------------------------------------------
# The `mcp` package is already a runtime dep of the server (via FastMCP).
# We use the stock stdio client to spawn `gsc_server.py` as a subprocess
# and enumerate + call its tools.
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = Path(__file__).resolve().parent
RUNS_DIR = EVAL_DIR / "runs"
PROMPTS_PATH = EVAL_DIR / "prompts.json"

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TURNS = 12
DEFAULT_MAX_TOKENS = 4096


# --- prompt loading -----------------------------------------------------


def load_prompts() -> List[Dict[str, Any]]:
    with PROMPTS_PATH.open() as f:
        data = json.load(f)
    return data["prompts"]


def resolve_placeholders(prompt_text: str, *, site_url: str, page_url: Optional[str], sf_export: Optional[str]) -> str:
    text = prompt_text.replace("{{SITE_URL}}", site_url)
    if page_url:
        text = text.replace("{{PAGE_URL}}", page_url)
    if sf_export:
        text = text.replace("{{SF_EXPORT_PATH}}", sf_export)
    return text


# --- tool schema translation --------------------------------------------


def mcp_tool_to_anthropic(tool) -> Dict[str, Any]:
    """Translate an MCP ``Tool`` object into Anthropic ``tools=[...]`` shape.

    Both use JSON-Schema for input; the outer envelope differs. This is
    a shallow remap, not a deep rewrite.
    """
    return {
        "name": tool.name,
        "description": (tool.description or "").strip(),
        "input_schema": tool.inputSchema,
    }


# --- runner core --------------------------------------------------------


async def _list_and_run(
    *,
    run_id: str,
    model: str,
    prompts: List[Dict[str, Any]],
    site_url: str,
    page_url: Optional[str],
    sf_export: Optional[str],
    account: Optional[str],
    max_turns: int,
    max_tokens: int,
    output_path: Path,
    server_path: Optional[Path] = None,
) -> None:
    """Connect to the MCP server, list tools, then loop over prompts.

    Writes one JSONL record per prompt to `output_path` as we go so a
    crash mid-run still leaves partial results on disk.
    """
    resolved_server = server_path or (ROOT / "gsc_server.py")
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(resolved_server)],
        env={**os.environ},
    )

    client = Anthropic()  # reads ANTHROPIC_API_KEY from env

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Truncate so a previous run for the same run_id doesn't leak.
    output_path.write_text("")

    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(server_params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        tools_list = await session.list_tools()
        mcp_tools = tools_list.tools
        anthropic_tools = [mcp_tool_to_anthropic(t) for t in mcp_tools]

        # Size the schema tax once — same for every prompt in this run.
        schema_tokens_est = _estimate_tokens(json.dumps(anthropic_tools))

        sys.stderr.write(
            f"[eval] connected to MCP server; {len(mcp_tools)} tools "
            f"(~{schema_tokens_est} schema tokens).\n"
        )

        # If an account was named, drive the server to the right account
        # before the eval starts (doesn't count against metrics). We
        # raise if the switch failed so the operator doesn't silently
        # eval against the wrong account.
        if account:
            switch_response = await session.call_tool(
                "gsc_switch_account", arguments={"alias": account}
            )
            if getattr(switch_response, "isError", False):
                body = "\n".join(
                    getattr(item, "text", repr(item))
                    for item in switch_response.content
                )
                raise RuntimeError(
                    f"gsc_switch_account({account!r}) failed before eval started: {body}"
                )

        for p in prompts:
            record = await _run_one_prompt(
                session=session,
                client=client,
                model=model,
                tools=anthropic_tools,
                schema_tokens_est=schema_tokens_est,
                prompt_def=p,
                run_id=run_id,
                site_url=site_url,
                page_url=page_url,
                sf_export=sf_export,
                max_turns=max_turns,
                max_tokens=max_tokens,
            )
            with output_path.open("a") as f:
                f.write(json.dumps(record) + "\n")
            sys.stderr.write(
                f"[eval] prompt {p['id']:>2} ({p['category']:<13}) "
                f"tools={record['total_tool_calls']} "
                f"response_tokens={record['total_response_tokens']} "
                f"routing={record['routing_match']}\n"
            )


async def _run_one_prompt(
    *,
    session: ClientSession,
    client: Anthropic,
    model: str,
    tools: List[Dict[str, Any]],
    schema_tokens_est: int,
    prompt_def: Dict[str, Any],
    run_id: str,
    site_url: str,
    page_url: Optional[str],
    sf_export: Optional[str],
    max_turns: int,
    max_tokens: int,
) -> Dict[str, Any]:
    prompt_text = resolve_placeholders(
        prompt_def["prompt"],
        site_url=site_url,
        page_url=page_url,
        sf_export=sf_export,
    )
    messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt_text}]

    tool_calls: List[Dict[str, Any]] = []
    total_response_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0
    error_count = 0
    incomplete = False
    final_stop_reason: Optional[str] = None
    final_answer = ""
    start = time.perf_counter()

    for _turn in range(max_turns):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0.0,
            tools=tools,
            messages=messages,
        )
        # Accumulate per-turn usage. These counts are the CUMULATIVE-over-turns
        # billable input/output tokens — Anthropic bills input_tokens for every
        # turn's full messages array (which repeats tool_use / tool_result
        # blocks from prior turns), so summing across turns reflects what we
        # actually paid.
        usage = response.usage
        prompt_tokens += usage.input_tokens
        completion_tokens += usage.output_tokens

        final_stop_reason = response.stop_reason
        if response.stop_reason != "tool_use":
            final_answer = "".join(
                block.text for block in response.content if block.type == "text"
            )
            # Anthropic stop_reasons other than tool_use / end_turn indicate
            # we stopped short: max_tokens = answer truncated, pause_turn =
            # extended-thinking checkpoint. Treat as incomplete so the eval
            # doesn't silently score a partial answer as success.
            if response.stop_reason not in ("end_turn", "stop_sequence"):
                incomplete = True
                error_count += 1
            break

        # Execute each tool_use block, one by one, sequentially.
        assistant_blocks = []
        tool_results_blocks = []
        transport_failed = False
        for block in response.content:
            assistant_blocks.append(block.model_dump())
            if block.type != "tool_use":
                continue
            tool_name = block.name
            tool_args = block.input
            tool_start = time.perf_counter()
            try:
                tool_response = await session.call_tool(tool_name, arguments=tool_args)
                response_text_parts = []
                for item in tool_response.content:
                    # MCP content items typically have a `.text` attribute when
                    # they are TextContent; other types (image, resource) fall
                    # through to their repr.
                    text = getattr(item, "text", None)
                    if text is not None:
                        response_text_parts.append(text)
                    else:
                        response_text_parts.append(repr(item))
                response_text = "\n".join(response_text_parts)
                response_chars = len(response_text)
                response_tokens = _estimate_tokens(response_text)
                error_str = None
            except (ConnectionError, BrokenPipeError, EOFError) as e:
                # Transport-level failure — subprocess dead or pipe broken.
                # Retrying the same call_tool will only burn turns and API
                # dollars, so abort both this block loop and the outer turn
                # loop.
                sys.stderr.write(
                    f"[eval] transport failure on {tool_name}: {e!r}. "
                    "Aborting this prompt's tool loop.\n"
                )
                final_answer = f"__TRANSPORT_FAILURE__ {e!r}"
                error_count += 1
                incomplete = True
                final_stop_reason = "transport_failure"
                transport_failed = True
                break
            except Exception as e:
                response_text = f"__TOOL_ERROR__ {e!r}"
                response_chars = len(response_text)
                response_tokens = _estimate_tokens(response_text)
                error_str = repr(e)
                error_count += 1

            dur_ms = int((time.perf_counter() - tool_start) * 1000)
            total_response_tokens += response_tokens

            tool_calls.append({
                "name": tool_name,
                "args": tool_args,
                "response_chars": response_chars,
                "response_tokens_est": response_tokens,
                "duration_ms": dur_ms,
                "error": error_str,
            })
            tool_results_blocks.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": response_text,
                "is_error": error_str is not None,
            })

        if transport_failed:
            break

        messages.append({"role": "assistant", "content": assistant_blocks})
        messages.append({"role": "user", "content": tool_results_blocks})
    else:
        # Hit max_turns without a final answer.
        final_answer = "__MAX_TURNS_EXCEEDED__"
        final_stop_reason = "max_turns_exhausted"
        incomplete = True
        error_count += 1

    wall_clock_ms = int((time.perf_counter() - start) * 1000)
    actual_path = [tc["name"] for tc in tool_calls]
    expected_path = prompt_def.get("expected_tool_path", [])
    routing_match = _classify_routing(expected_path, actual_path)

    # Honest grand_total: Anthropic bills input_tokens + output_tokens.
    # Tool schemas are already counted inside prompt_tokens on every turn
    # the API sends them, and tool_result text is already inside prompt_tokens
    # on the NEXT turn. Adding schema_tokens_est or total_response_tokens
    # separately would double-count (previous versions of this runner did).
    # The sidecar fields are kept so aggregators can still explain *why*
    # prompt_tokens is what it is.
    grand_total = prompt_tokens + completion_tokens

    return {
        "run_id": run_id,
        "prompt_id": prompt_def["id"],
        "prompt_category": prompt_def.get("category", "unknown"),
        "prompt_text": prompt_text,
        "model": model,
        "temperature": 0.0,
        "prompt_cache": "disabled",
        "tool_definitions_tokens": schema_tokens_est,  # informational, NOT in grand_total
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_response_tokens": total_response_tokens,  # informational sidecar
        "grand_total_tokens": grand_total,
        "tool_calls": tool_calls,
        "total_tool_calls": len(tool_calls),
        "expected_tool_path": expected_path,
        "actual_tool_path": actual_path,
        "routing_match": routing_match,
        "stop_reason": final_stop_reason,
        "incomplete": incomplete,
        "wall_clock_ms": wall_clock_ms,
        "error_count": error_count,
        # retry_count is always 0 — the Anthropic SDK retries 429/5xx
        # internally but does not surface the attempt count on responses.
        "retry_count": 0,
        "final_answer": final_answer,
        "golden_diff": "pending",
    }


def _classify_routing(expected: List[str], actual: List[str]) -> str:
    """Classify actual tool-call sequence against the prompt's expected.

    exact          — same tools in same order.
    ordered_subset — expected appears as an in-order subsequence of actual.
    subset         — every expected tool appears somewhere in actual (any order).
    different      — at least one expected tool is missing.
    unknown        — no expectation was specified.
    """
    if not expected:
        return "unknown"
    if expected == actual:
        return "exact"
    # Check in-order subsequence: walk actual, matching expected one-at-a-time.
    i = 0
    for name in actual:
        if i < len(expected) and name == expected[i]:
            i += 1
    if i == len(expected):
        return "ordered_subset"
    if set(expected).issubset(set(actual)):
        return "subset"
    return "different"


# --- cheap token estimator ---------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Rough chars/4 proxy. Good enough for delta tracking within a run.

    We deliberately don't re-import tiktoken here — the harness runs on
    any Python; the proxy is stable enough to catch regressions.
    """
    return max(1, len(text) // 4) if text else 0


# --- mcp probe ---------------------------------------------------------


async def _probe_mcp() -> None:
    """Spawn the MCP server, list tools, print a summary, then exit.

    Lets us verify the stdio plumbing + schema translation without
    paying for an Anthropic API call.
    """
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "gsc_server.py")],
        env={**os.environ},
    )
    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(server_params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools_list = await session.list_tools()
        anth = [mcp_tool_to_anthropic(t) for t in tools_list.tools]
        schema_json = json.dumps(anth)
        print(f"connected to {ROOT / 'gsc_server.py'}")
        print(f"tools advertised: {len(tools_list.tools)}")
        print(f"schema size: {len(schema_json)} chars, ~{_estimate_tokens(schema_json)} tokens")
        print("first 5 tool names:")
        for t in tools_list.tools[:5]:
            print(f"  - {t.name}  ({(t.description or '').splitlines()[0][:80] if t.description else ''})")


# --- cli ----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", help="Identifier for this run. Output goes to audit/eval/runs/<run-id>.jsonl.")
    parser.add_argument("--list", action="store_true", help="List all prompts and exit.")
    parser.add_argument("--probe-mcp", action="store_true", help="Connect to the MCP server, list tools + schema-token estimate, then exit. Does not call the Anthropic API.")
    parser.add_argument("--only", type=int, nargs="+", help="Only run the given prompt IDs.")
    parser.add_argument("--read-only", action="store_true", help="Skip mutating prompts (4, 19, 25).")
    parser.add_argument("--property", dest="site_url", help="GSC site URL (substituted for {{SITE_URL}}).")
    parser.add_argument("--page-url", dest="page_url", help="Page URL (substituted for {{PAGE_URL}}).")
    parser.add_argument("--sf-export", dest="sf_export", help="SF export path (prompts 20-21).")
    parser.add_argument("--account", help="GSC account alias to switch to before running.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument(
        "--server",
        type=Path,
        help="Path to the gsc_server.py to spawn (default: project root). "
             "Pointing this at a historical checkout enables baseline-vs-post comparisons.",
    )
    args = parser.parse_args()

    prompts = load_prompts()

    if args.list:
        for p in prompts:
            mutate_marker = "(mutate)" if p.get("mutate") else ""
            print(f"{p['id']:>2} [{p['category']:<14}] {mutate_marker:<10} {p['prompt'][:80]}")
        return 0

    if args.probe_mcp:
        asyncio.run(_probe_mcp())
        return 0

    if not args.run_id:
        parser.error("--run-id is required unless --list is used")
    if not args.site_url:
        parser.error("--property is required (e.g. --property sc-domain:example.com)")

    # Prompt filtering.
    if args.only:
        prompts = [p for p in prompts if p["id"] in set(args.only)]
    if args.read_only:
        prompts = [p for p in prompts if not p.get("mutate", False)]
    if not prompts:
        parser.error("no prompts selected after filtering")

    output_path = RUNS_DIR / f"{args.run_id}.jsonl"
    asyncio.run(_list_and_run(
        run_id=args.run_id,
        model=args.model,
        prompts=prompts,
        site_url=args.site_url,
        page_url=args.page_url,
        sf_export=args.sf_export,
        account=args.account,
        max_turns=args.max_turns,
        max_tokens=args.max_tokens,
        output_path=output_path,
        server_path=args.server,
    ))
    sys.stderr.write(f"[eval] wrote {output_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

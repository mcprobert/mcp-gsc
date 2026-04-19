"""Regression guard for FastMCP's structured-output wrapping.

FastMCP (`mcp/server/fastmcp/utilities/func_metadata.py:121-131`) wraps
@mcp.tool() return values in `{"result": ...}` at the protocol boundary
when the return annotation is a generic type like `Dict[str, Any]` or
`list[...]`. Tools annotated `-> Any` (or `-> str`, etc.) take the
unstructured path and emit flat JSON via TextContent — which is the
house envelope convention (see CLAUDE.md "Response envelope" section).

This caught us out between v1.1.0 and v1.1.1: four tools still had
`-> Dict[str, Any]` after the F1 normalisation pass, and consumers saw
`{result: {ok, ...}}` instead of the flat `{ok, tool, ...}` that the
other 10+ tools emit. The existing 323 tests call tool functions
directly and bypass FastMCP, so they couldn't catch the drift.

This test pins the rule: every @mcp.tool() return annotation must be in
a safe set (`Any`, `str`, or no annotation). If anyone adds `-> Dict`
or `-> list` or similar, this test fails loudly at CI time — before the
wrapping reaches a consumer.
"""
from __future__ import annotations

import inspect
from typing import Any

import gsc_server


# Safe return annotations for @mcp.tool() functions. Strings, Any, and
# bare (no annotation) don't trigger FastMCP's structured-output path.
_SAFE_ANNOTATIONS = {Any, str, inspect.Signature.empty}


def test_no_tool_uses_generic_return_annotation():
    tools = gsc_server.mcp._tool_manager.list_tools()
    assert tools, "no tools registered — test fixture broken"

    offenders: list[tuple[str, str]] = []
    for tool in tools:
        sig = inspect.signature(tool.fn)
        ann = sig.return_annotation
        if ann in _SAFE_ANNOTATIONS:
            continue
        offenders.append((tool.name, repr(ann)))

    assert not offenders, (
        "The following @mcp.tool() functions declare return annotations "
        "that trigger FastMCP's structured-output wrapping "
        "({result: {...}} at the protocol boundary). Change them to "
        "`-> Any` or remove the annotation. See CLAUDE.md 'Response "
        "envelope' section and mcp/server/fastmcp/utilities/"
        "func_metadata.py:121-131 for the mechanism.\n\n"
        f"Offenders: {offenders}"
    )

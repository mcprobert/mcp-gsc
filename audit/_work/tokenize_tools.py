"""Parse gsc_server.py, extract every @mcp.tool decorated function,
tokenize the tool definition with cl100k_base, and write results to JSON.

Output schema (list of dicts):
  name, line, sig_params_source, sig_param_count, sig_required, sig_optional,
  description_first_sentence, description_full, description_tokens,
  schema_params_pseudo_json, schema_tokens, total_definition_tokens,
  return_hint
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import tiktoken

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "gsc_server.py"
OUT = ROOT / "audit" / "_work" / "tool_tokens.json"

ENC = tiktoken.get_encoding("cl100k_base")


def count(text: str) -> int:
    return len(ENC.encode(text or ""))


def is_tool_decorator(dec: ast.expr) -> bool:
    # Matches @mcp.tool() or @mcp.tool
    if isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Attribute) and dec.attr == "tool":
        return True
    return False


def type_to_str(node: ast.AST | None) -> str:
    if node is None:
        return "Any"
    try:
        return ast.unparse(node)
    except Exception:
        return "?"


def default_to_str(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return "?"


def first_sentence(docstring: str) -> str:
    if not docstring:
        return ""
    s = docstring.strip()
    m = re.search(r"[.!?](\s|$)", s)
    if m:
        return s[: m.end()].strip()
    # Fallback: first line
    return s.splitlines()[0].strip()


def extract_tools(src: str) -> list[dict]:
    tree = ast.parse(src)
    tools = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(is_tool_decorator(d) for d in node.decorator_list):
            continue
        args = node.args
        all_pos = args.args
        defaults = args.defaults
        num_defaults = len(defaults)
        num_args = len(all_pos)
        num_required = num_args - num_defaults
        params = []
        sig_parts = []
        required = []
        optional = []
        for i, a in enumerate(all_pos):
            ann = type_to_str(a.annotation)
            if i < num_required:
                params.append({"name": a.arg, "type": ann, "required": True, "default": None})
                sig_parts.append(f"{a.arg}: {ann}")
                required.append(a.arg)
            else:
                d = defaults[i - num_required]
                dflt = default_to_str(d)
                params.append({"name": a.arg, "type": ann, "required": False, "default": dflt})
                sig_parts.append(f"{a.arg}: {ann} = {dflt}")
                optional.append(a.arg)
        # kw-only args
        for j, a in enumerate(args.kwonlyargs):
            ann = type_to_str(a.annotation)
            d = args.kw_defaults[j] if j < len(args.kw_defaults) else None
            if d is None:
                params.append({"name": a.arg, "type": ann, "required": True, "default": None})
                sig_parts.append(f"*, {a.arg}: {ann}")
                required.append(a.arg)
            else:
                dflt = default_to_str(d)
                params.append({"name": a.arg, "type": ann, "required": False, "default": dflt})
                sig_parts.append(f"*, {a.arg}: {ann} = {dflt}")
                optional.append(a.arg)

        returns = type_to_str(node.returns)
        sig_src = f"def {node.name}({', '.join(sig_parts)}) -> {returns}"

        docstring = ast.get_docstring(node) or ""
        first = first_sentence(docstring)

        # Pseudo-JSON schema approximation to estimate schema JSON token cost.
        # This mirrors what FastMCP serializes as inputSchema; enough to rank
        # tools and compute relative schema tax.
        props = {}
        required_names = []
        for p in params:
            props[p["name"]] = {"type": p["type"]}
            if p["required"]:
                required_names.append(p["name"])
        schema_obj = {
            "type": "object",
            "properties": props,
            "required": required_names,
        }
        schema_json = json.dumps(schema_obj, ensure_ascii=False)

        desc_tokens = count(docstring)
        schema_tokens = count(schema_json)
        # Total definition footprint: name + description + schema
        total_tokens = count(node.name) + desc_tokens + schema_tokens

        tools.append({
            "name": node.name,
            "line": node.lineno,
            "signature": sig_src,
            "param_count": len(params),
            "required": required,
            "optional": optional,
            "return_hint": returns,
            "description_first_sentence": first,
            "description_full": docstring,
            "description_tokens": desc_tokens,
            "schema_tokens": schema_tokens,
            "total_definition_tokens": total_tokens,
            "namespaced_gsc": node.name.startswith("gsc_"),
        })
    return tools


def main() -> None:
    src = SERVER.read_text(encoding="utf-8")
    tools = extract_tools(src)
    tools.sort(key=lambda t: t["line"])
    total_def = sum(t["total_definition_tokens"] for t in tools)
    total_desc = sum(t["description_tokens"] for t in tools)
    total_schema = sum(t["schema_tokens"] for t in tools)
    summary = {
        "tool_count": len(tools),
        "total_definition_tokens": total_def,
        "total_description_tokens": total_desc,
        "total_schema_tokens": total_schema,
        "namespaced_gsc_count": sum(1 for t in tools if t["namespaced_gsc"]),
        "non_namespaced_count": sum(1 for t in tools if not t["namespaced_gsc"]),
        "tools": tools,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Tools: {len(tools)}")
    print(f"Total definition tokens: {total_def}")
    print(f"  description tokens: {total_desc}")
    print(f"  schema tokens: {total_schema}")
    print(f"gsc_* prefixed: {summary['namespaced_gsc_count']} / {len(tools)}")
    print(f"Output: {OUT}")


if __name__ == "__main__":
    main()

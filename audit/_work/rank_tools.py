import json
from pathlib import Path

d = json.loads(Path("audit/_work/tool_tokens.json").read_text())
tools = sorted(d["tools"], key=lambda t: -t["total_definition_tokens"])

hdr = f"{'rk':>3} {'total':>6} {'desc':>5} {'sch':>5} {'pc':>3} {'pfx':>4}  {'name':<40} line"
print(hdr)
print("-" * len(hdr))
for i, t in enumerate(tools, 1):
    pfx = "gsc" if t["namespaced_gsc"] else "-"
    row = (
        f"{i:>3} {t['total_definition_tokens']:>6} {t['description_tokens']:>5} "
        f"{t['schema_tokens']:>5} {t['param_count']:>3} {pfx:>4}  {t['name']:<40} {t['line']}"
    )
    print(row)

print()
print(f"TOTAL: {d['tool_count']} tools")
print(f"  total_definition_tokens: {d['total_definition_tokens']}")
print(f"  total_description_tokens: {d['total_description_tokens']}")
print(f"  total_schema_tokens (pseudo): {d['total_schema_tokens']}")
print(f"  gsc_* prefixed: {d['namespaced_gsc_count']} / {d['tool_count']}")

# Bucket by prefix
prefixed = [t for t in tools if t["namespaced_gsc"]]
bare = [t for t in tools if not t["namespaced_gsc"]]
print()
print(f"Token share:")
print(f"  prefixed (gsc_*): {sum(t['total_definition_tokens'] for t in prefixed)} tokens across {len(prefixed)} tools")
print(f"  bare: {sum(t['total_definition_tokens'] for t in bare)} tokens across {len(bare)} tools")

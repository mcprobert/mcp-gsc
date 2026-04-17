# Designing MCP servers that don't burn your context window

The single biggest lever is stopping yourself from exposing every API endpoint as a tool and every API field as a response. **Direct tool-call MCP scales poorly: Cloudflare measured ~1.17M tokens to expose the full Cloudflare API as traditional MCP tools, and even a minimal schema still cost ~244k tokens — more than most frontier context windows.** The fix is a stack of four complementary techniques: shrink the tool surface the model sees, shape responses aggressively, engineer descriptions like system prompts, and — for large APIs — replace tool-calling with code execution. Anthropic reports ~98.7% token reduction from Google-Drive-to-Salesforce workflows once they moved from direct MCP calls to code execution; Cloudflare reports 99.9% for their 2,500-endpoint API. The patterns below are drawn from Anthropic's engineering team, Cloudflare, Atlassian, StackOne, Axiom, Apollo, and the draft SEP-1576 "Mitigating Token Bloat in MCP."

## Where the tokens actually go

Before optimizing, understand the two separate taxes MCP imposes. **The first is the schema tax**: every tool definition — name, description, JSON Schema for parameters — is loaded into context at session start, whether used or not. GitHub's official MCP server exposes 93 operations that consume roughly 47,000 tokens on every request; Claude Code users routinely see 40k+ tokens spent before a single user prompt is processed. **The second is the result tax**: intermediate tool outputs flow through the model to reach the next tool call. Anthropic's Google-Drive-to-Salesforce example has a 50,000-token meeting transcript passing through the model twice — once on read, once on write — even though the model never needs to reason about the content.

Both taxes compound. Stanford's "lost in the middle" finding shows LLM accuracy drops over 20% when relevant content sits in the middle of a long context, so bloated tool output doesn't just cost money — it degrades the answers.

## Shrink the tool surface before the model ever sees it

**The first design decision is how many tools to implement, not how to describe them.** Anthropic's explicit guidance: wrapping every API endpoint one-to-one is an anti-pattern because agents have different affordances than traditional software. Build fewer, more consolidated tools targeting specific high-value workflows. Instead of `list_users` + `list_events` + `create_event`, ship a single `schedule_event`. Instead of `get_customer_by_id` + `list_transactions` + `list_notes`, ship `get_customer_context`. This both reduces schema tokens and offloads agentic reasoning into deterministic server code.

For genuinely large APIs where consolidation isn't enough, **progressive disclosure** — load tools on demand — has emerged as the dominant 2025-2026 pattern. Four variants are now in production:

| Approach | Mechanism | Reported token reduction |
|---|---|---|
| Claude Code Tool Search | Auto-activates when tool defs exceed 10% of context; replaces schema dump with searchable registry | ~95% on initial load |
| GitHub MCP progressive disclosure | Dynamic toolsets revealed by context/request | 60-80% context reduction |
| Atlassian mcp-compressor | Proxy strips descriptions/enums at configurable levels | 70-97% on schemas |
| Cloudflare Code Mode (server-side) | Exposes `search()` + `execute()` against OpenAPI spec | 99.9% on 2,500-endpoint API |

Anthropic's own principles for the Claude-ecosystem servers reinforce this: **namespace tools with consistent prefixes** (`asana_projects_search`, not `search`), because namespace collisions across dozens of connected servers confuse tool selection. SEP-1576 adds two more ideas now being debated in the MCP spec: **schema deduplication via JSON `$ref`** to eliminate repeated type definitions across tools, and **embedding-based tool retrieval** where the server pre-filters to top-k relevant tools before any definitions reach the model.

## Shape responses like you're on a token budget

Tool output is where most teams leak the most context, and the fixes are well-understood. Anthropic's Claude Code restricts tool responses to **25,000 tokens by default** and surfaces helpful truncation messages that steer the agent toward pagination or filters. Every response should implement some combination of pagination, range selection, field filtering, and truncation with sensible defaults.

The Slack MCP team's concrete example: a "detailed" tool response was 206 tokens; the "concise" variant, controlled by a `response_format` enum, was 72 tokens — a 65% cut on the same underlying data, achieved by dropping IDs the agent rarely needs. Apollo's GraphQL MCP server takes this further with field selection at query time, letting agents pull only the weather fields they asked about instead of a 20-field payload. **Axiom found CSV was ~29% cheaper than JSON for tabular data** (166 vs 235 tokens on 5 rows) with no accuracy loss, because repeated JSON keys are pure overhead for wide tables.

Two less obvious design choices matter a lot. **Replace cryptic identifiers with natural-language names** wherever possible — Anthropic found that resolving UUIDs to semantic names or 0-indexed IDs measurably improved retrieval precision and reduced hallucinations, because models handle human-readable tokens far better than alphanumeric strings. **Context-aware pagination cursors** — as Blockscout implemented — should be based on the last returned item, not the underlying API's page offset, and should include a textual nudge in the response telling the agent more data exists (LLMs notoriously ignore structured `nextCursor` fields without prose instructions).

For error responses, follow the same principle: a terse "Invalid parameter" wastes a turn; a message like "The `date` parameter must be ISO 8601. Example: `2026-04-17`" costs a few extra tokens and usually saves a retry.

## Write tool descriptions like you're onboarding a new hire

Anthropic's Claude Sonnet 3.5 hit state-of-the-art on SWE-bench Verified after precise refinements to tool descriptions alone — no model training change. Tool descriptions sit permanently in context, so they're the highest-leverage prompt engineering surface in your whole stack.

The concrete best practices converge across sources. Keep descriptions to 1–2 sentences structured around a verb and a resource. Name parameters unambiguously (`user_id`, not `user`). Spell out specialized terminology, non-obvious relationships between resources, and query-format quirks you'd otherwise leave implicit. Include a short counter-example when a neighboring tool exists — Gong's `get_calls` description explicitly says "List ALL calls in date range - no user/workspace filtering. To filter by user/workspace, use search_calls_extensive instead," which both routes the agent correctly and prevents wasted calls. Anthropic specifically warns against appending unhelpful framing: when they launched Claude's web search, Claude kept appending "2025" to queries until a tool description tweak fixed it.

One subtle gotcha: **GraphQL-derived tool descriptions balloon if you auto-generate them from developer-facing schema comments.** Apollo recommends overriding field descriptions with concise LLM-targeted prose rather than shipping the full developer docs.

## For large APIs, stop calling tools — write code

The most significant architectural shift of 2025 is treating MCP servers as code APIs rather than tool-call endpoints. The insight, independently arrived at by Anthropic and Cloudflare, is that **LLMs are trained on orders of magnitude more TypeScript than tool-call traces, so they write code more reliably than they chain tool calls**. Three concrete benefits flow from this:

1. **Tools load on demand via filesystem exploration.** Anthropic's implementation exposes each tool as a TypeScript file the agent reads only when needed. A 100-tool server costs the tokens of its directory listing plus the 2-3 files actually relevant to the current task.
2. **Intermediate results stay in the execution sandbox.** The agent filters a 10,000-row spreadsheet in code and logs only the 5 matching rows back to the model — solving the result tax directly.
3. **Control flow runs at the edge.** Loops, conditionals, retries, and sleeps execute in the sandbox without round-tripping through the model, which cuts both latency and token cost.

Cloudflare's production Code Mode MCP server collapses their entire 2,500-endpoint, 1.17M-token API into just two tools — `search()` and `execute()` — that together consume ~1,000 tokens regardless of API size. The tradeoff is real: you need a secure sandbox (V8 isolates, Deno, or equivalent), outbound network controls, and PII-tokenization hooks if sensitive data flows through. These aren't trivial to build, so the guidance from both Anthropic and Cloudflare is to reach for code execution only when the API is genuinely large — below ~20 tools, direct calling with good response shaping usually wins on simplicity.

## Measure with evaluations, not intuition

Every source with production data insists on evaluation-driven iteration, because tool-calling behavior is model-specific and counterintuitive. Anthropic's internal process: generate 20-50 realistic multi-step tasks grounded in actual workflows (not toy prompts like "search for X"), run programmatic agentic loops against your tools, and track **accuracy, total tool calls per task, runtime, total token consumption, and error types** as separate metrics. Then paste the transcripts into Claude Code and ask it to refactor your tool definitions — Anthropic reports the agent-optimized versions beat expert-written tools on held-out test sets.

For live monitoring, **MCP Inspector** is the de-facto local debugger (stdio + streamable HTTP, full JSON-RPC observability) and handles the "does my server speak the protocol" question. For production, gateway-level observability — Datadog LLM Observability, Portkey, Apigene — captures per-call logs, tool-error rates, and token usage across servers, which is where retry storms and description-mismatch failures actually show up. MCPJam Inspector now includes token-usage views and multi-model eval runs directly in the UI, which is the fastest way to compare how GPT-5, Claude, and Gemini call the same server differently.

The 2025 warnings are worth internalizing: the `--read-only` default is a secure-by-default anti-pattern (use `--allow-edit` instead), token passthrough is explicitly forbidden by the MCP spec, and hundreds of production servers have been found deployed with no authentication at all. Token efficiency and security co-evolve: every unnecessary tool you expose both wastes context and widens attack surface.

## Conclusion: optimize in layers, in order

The wasteful MCP server is one that exposes every API endpoint as a separately-described tool returning full upstream JSON payloads. The efficient MCP server treats context as its scarcest resource and applies four layers in order: **(1) fewer, consolidated tools with clear namespaces; (2) responses paginated, filtered, and formatted for the agent rather than the developer; (3) descriptions prompt-engineered and evaluated against real tasks; (4) code execution for APIs large enough that the first three can't keep up.** The empirical results are striking — 70-99% token reductions at each layer — and the accuracy gains are equally real, because the same surgery that shrinks the context also surfaces the right tools and data at the right time.

The direction of the ecosystem is clear. The MCP spec itself is moving toward token-aware features (SEP-1576's `$ref` deduplication, budget hints, paging hints); clients like Claude Code are auto-activating tool search at 10% context consumption; and platforms like Cloudflare are shifting code execution from an optimization to a default. Teams building MCP servers today should design as if every description and every response will be rendered, read, and paid for — because they will.
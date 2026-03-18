# Tool Calling in Large Language Models: Architecture, Patterns, and Practice

*A comprehensive survey of function calling in LLM-based agents*

---

## Table of Contents

1. [Introduction](#introduction)
2. [What Is Tool Calling and How Does It Work](#what-is-tool-calling-and-how-does-it-work)
3. [The ReAct Loop: Reason + Act](#the-react-loop-reason--act)
4. [Categories of Useful Tools](#categories-of-useful-tools)
5. [Real-World Use Cases](#real-world-use-cases)
6. [Best Practices](#best-practices)
7. [Local vs Cloud LLM Tool Calling](#local-vs-cloud-llm-tool-calling)
8. [Future Directions](#future-directions)
9. [Conclusion](#conclusion)

---

## Introduction

Large language models began as sophisticated text predictors. Ask one a question, and it generates plausible-sounding text based on patterns learned during training. This makes them impressive for summarization, writing, and conversation — but fundamentally limited: a pure language model cannot tell you the current weather, cannot write a file to disk, cannot look up a live stock price, and cannot run a shell command. Its knowledge is frozen at its training cutoff, and it has no hands.

**Tool calling** — also called function calling — is the mechanism by which LLMs break out of this limitation. The model is given descriptions of external capabilities ("tools"), and when it determines that one of those capabilities is needed to satisfy a user request, it emits a structured call to that tool. The system intercepts the call, executes the real function, feeds the result back to the model, and the model continues reasoning toward a final answer.

This pattern transforms an LLM from a static text generator into a dynamic agent capable of interacting with the real world: reading files, querying databases, browsing the web, controlling software, and coordinating with other AI services. Understanding how tool calling works — and how to design it well — is one of the most practically important topics in applied AI engineering today.

---

## What Is Tool Calling and How Does It Work

### The Core Mechanism

At its most basic, tool calling is a structured conversation protocol. Before the conversation begins, the model is provided with a list of tool definitions. Each definition is a JSON schema that describes:

- **name** — a unique identifier for the tool
- **description** — what the tool does and when to use it
- **parameters** — the typed inputs the tool accepts

When the model's reasoning process determines that a tool is needed, instead of generating a prose response it generates a structured tool-call object — typically JSON — specifying which tool to invoke and what parameters to pass. The hosting system parses this structured output, runs the actual function, and injects the result back into the conversation as a "tool result" message. The model then reads that result and either calls another tool or produces a final answer.

### A Concrete Schema Example

```json
{
  "name": "search.web",
  "description": "Search the web for current information. Use when the user asks about recent events, live data, or anything you are not certain about from training. Always cite the source URL in your reply.",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "The search query. Be specific and concise."
      },
      "num_results": {
        "type": "integer",
        "description": "Number of results to return (1-10). Default 5.",
        "default": 5
      }
    },
    "required": ["query"]
  }
}
```

```json
{
  "name": "files.write",
  "description": "Write text content to a file on disk. After writing, confirm the file path and byte count to the user.",
  "parameters": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "description": "Absolute file path to write to."
      },
      "content": {
        "type": "string",
        "description": "The full text content to write."
      },
      "append": {
        "type": "boolean",
        "description": "If true, append to existing file rather than overwriting.",
        "default": false
      }
    },
    "required": ["path", "content"]
  }
}
```

### How the Model Decides to Call a Tool

Modern LLMs fine-tuned for tool use are trained on datasets that include tool-call sequences. During inference, the model learns to recognize linguistic signals in user input that suggest a tool would be helpful:

- "What is the weather today?" → triggers a `weather.current` call
- "Create a file with these notes" → triggers a `files.write` call
- "Run this Python snippet" → triggers a `code.execute` call

Some systems enhance this with explicit signal detection: a regex or classifier pre-screens the user message and determines whether to even attempt a tool-calling round. If the message is clearly casual conversation ("thanks!"), running the full tool-selection pass wastes tokens and latency.

The output format varies by system. OpenAI's Chat Completions API produces tool call objects with `id`, `type`, `function.name`, and `function.arguments` fields. Ollama and llama.cpp expose a similar format for models that support it natively. Models without native tool support can still be prompted to produce JSON in a constrained output mode or via guided generation (e.g., `format: "json"` in Ollama's API).

---

## The ReAct Loop: Reason + Act

### Origins

The **ReAct framework** (Yao et al., 2022, *ReAct: Synergizing Reasoning and Acting in Language Models*) formalized the pattern of interleaving reasoning traces with action calls. ReAct showed that asking the model to reason out loud — producing a "Thought:" before each action — significantly improved reliability on complex multi-step tasks compared to pure action chains or pure chain-of-thought reasoning.

### The Loop in Practice

A ReAct agent operates in a cycle:

```
Thought  →  Action  →  Observation  →  Thought  →  Action  → ...  →  Final Answer
```

1. **Thought** — The model reasons about what it knows, what it needs, and what tool (if any) to call next.
2. **Action** — The model emits a structured tool call.
3. **Observation** — The system executes the tool and returns the result as an observation.
4. The model reads the observation, updates its reasoning, and either calls another tool or produces a final answer.

A concrete example for the query "What is the disk usage on drive C and how full is it?":

```
Thought: The user wants current disk usage for drive C. I should call the system tool
         that reports disk statistics.

Action: system.disk_usage({"drive": "C"})

Observation: {"total_gb": 953.9, "used_gb": 412.7, "free_gb": 541.2, "percent_used": 43.3}

Thought: I now have the data. Drive C has 953.9 GB total, 412.7 GB used (43.3%),
         and 541.2 GB free. I can give a clear answer now.

Final Answer: Drive C has 953.9 GB of total capacity. Currently 412.7 GB (43.3%) is
              used, leaving 541.2 GB free.
```

### Why Thought Traces Matter

The explicit reasoning step serves several functions:

- **Self-correction** — the model can catch its own errors before committing to an action
- **Tool selection accuracy** — articulating the need makes the right tool more likely to be chosen
- **Debugging** — developers can inspect reasoning traces to understand failures
- **User transparency** — surfacing thoughts (optionally) helps users understand what the agent is doing

In practice, many production systems surface the thought trace in a collapsible UI panel or log it internally for debugging, rather than showing it in the main response. The `<think>` tag convention used by models like Qwen3 and DeepSeek-R1 achieves a similar effect: extended reasoning is hidden from the final reply but available in the raw output.

### Multi-Turn and Multi-Tool Chains

Real tasks often require several tool calls in sequence, or branching based on intermediate results. A coding assistant asked to "find all Python files that import os and count the unique ones" might:

1. Call `files.list` to enumerate the project directory
2. Call `files.read` on each `.py` file (or use `search.grep` to scan for the import)
3. Aggregate the results in its reasoning trace
4. Return the count and file list

This multi-hop pattern is where the ReAct loop's structured discipline provides the most value. Without explicit reasoning steps, models tend to hallucinate intermediate results rather than actually calling the tools needed to obtain them.

---

## Categories of Useful Tools

Tool libraries for AI agents cluster into several natural categories. Well-designed agents expose tools in each category to cover the full range of tasks users reasonably expect.

### 1. File System Tools
Read, write, list, search, and delete files. These are foundational — nearly every non-trivial task involves persisting or retrieving data.

- `files.read(path)` — read file content
- `files.write(path, content)` — write or overwrite a file
- `files.list(directory, pattern)` — list files matching a glob
- `files.search(directory, query)` — full-text search across files

### 2. System / OS Tools
Query and control the host operating system.

- `system.run_command(cmd)` — execute a shell command and return stdout/stderr
- `system.processes()` — list running processes
- `system.disk_usage(drive)` — disk statistics
- `system.crashes()` — query Windows Event Log for application crash records

### 3. Search Tools
Retrieve live information from the internet or local indexes.

- `search.web(query)` — DuckDuckGo or SearXNG search
- `search.wikipedia(topic)` — Wikipedia article lookup
- `search.local_index(query)` — semantic search over a local knowledge base

### 4. Memory Tools
Read and write the agent's own memory systems — critical for agents that need to maintain continuity across sessions.

- `memory.save(key, value)` — persist a fact to long-term memory
- `memory.recall(query)` — retrieve semantically similar memories
- `memory.list_notes()` — enumerate saved notes
- `memory.forget(key)` — delete a memory entry

### 5. Code Execution Tools
Execute code and return the result — arguably the most powerful and dangerous category.

- `code.run_python(code)` — run Python in a sandboxed subprocess
- `code.run_shell(script)` — run a shell script
- `code.lint(code, language)` — static analysis

### 6. Network Tools
Make HTTP requests, monitor connectivity, interact with APIs.

- `network.http_get(url, headers)` — fetch a URL
- `network.http_post(url, payload)` — POST to an endpoint
- `network.ping(host)` — connectivity check
- `network.port_scan(host, ports)` — service discovery

### 7. Time / Calendar Tools
Provide temporal grounding — models have no internal clock.

- `time.now()` — current date and time with timezone
- `time.convert(timestamp, tz)` — timezone conversion
- `calendar.events(range)` — fetch calendar events

### 8. Weather Tools
Live weather data by location.

- `weather.current(location)` — current conditions
- `weather.forecast(location, days)` — multi-day forecast

### 9. Domain-Specific Tools
These vary by application: e-commerce agents get product search and order management tools; medical agents get drug interaction lookup; D&D dungeon master agents get campaign state tools.

---

## Real-World Use Cases

### PC and Desktop Agents

A personal AI agent running locally (like the Kai project this paper accompanies) has access to the host PC's file system, processes, and network. Such an agent can:

- Analyze crash logs from the Windows Event Viewer
- Read and organize documents in a user's folder
- Monitor disk usage and alert when drives are filling up
- Run maintenance scripts on behalf of the user
- Search local notes using semantic vector search

The defining characteristic here is **privileged local access** — the agent can do things a cloud-based chatbot fundamentally cannot, because the tools run on the same machine as the user's data.

### Coding Assistants

Tools like GitHub Copilot Workspace, Cursor, and Claude Code operate as agentic coding assistants that can:

- Read the entire codebase to understand context
- Write new files and edit existing ones
- Run tests and interpret results
- Search for usages of a function across the project
- Execute linters and fix the reported issues

These agents run dozens of tool calls per user request in a tightly coordinated sequence. The reliability of their tool descriptions and error handling directly determines whether they produce working code or make a mess.

### Customer Service and Enterprise Bots

Enterprise deployments give LLMs access to internal APIs:

- CRM lookup: "Who is this customer and what have they ordered?"
- Order management: "Issue a refund for order #84921"
- Knowledge base retrieval: "What is our return policy for electronics?"
- Ticket creation: "Open a support ticket with priority HIGH"

Here, tool calling bridges the LLM's natural-language understanding with structured business data and workflows. The model acts as a natural-language interface to systems that previously required trained human operators.

### Research Agents

Multi-step research tasks benefit enormously from tool-enabled agents:

1. `search.web` to identify sources on a topic
2. `network.http_get` to fetch full article text
3. `memory.save` to store key findings
4. `files.write` to produce a structured report

Research agents are essentially automating the human research workflow — read, take notes, synthesize, write — at machine speed.

### Data Analysis Agents

When equipped with `code.run_python` and database query tools, LLMs can perform full analytical workflows:

- Load a CSV file
- Write and execute Pandas code to compute statistics
- Produce a chart with Matplotlib
- Summarize the findings in prose

---

## Best Practices

### 1. Write Descriptions as Post-Processing Instructions

The `description` field in a tool schema is more than documentation — it is an instruction to the model about how to behave *after* the tool runs. Rather than just describing what the tool does:

**Poor description:**
```
"Returns disk usage statistics for the specified drive."
```

**Better description:**
```
"Returns disk usage statistics for the specified drive. After calling this tool,
present the results in a human-readable format: show total, used, and free space
in GB with one decimal place, and express usage as a percentage. If usage exceeds
85%, warn the user proactively."
```

This technique leverages the model's instruction-following capability to control output formatting and behavior without adding system-prompt complexity. It keeps the behavior specification co-located with the tool itself.

### 2. Design Parameters to Minimize Ambiguity

Each parameter should have a clear, unambiguous description with examples where helpful:

```json
{
  "path": {
    "type": "string",
    "description": "Absolute file path. On Windows use forward slashes: C:/Users/kai/notes.txt"
  }
}
```

Avoid optional parameters with complex conditional semantics. If a parameter is only meaningful when another parameter has a specific value, split it into separate tools.

### 3. Namespace Your Tools

As tool libraries grow, flat names collide and descriptions blur together. Use namespacing consistently:

- `files.read`, `files.write`, `files.list`
- `system.processes`, `system.disk_usage`, `system.crashes`
- `search.web`, `search.wikipedia`

This makes it easier for the model to navigate the tool catalog and easier for developers to maintain it.

### 4. Error Handling: Return Structured Errors, Not Exceptions

Tool functions should never raise unhandled exceptions into the model's context. Instead, return a structured error object:

```python
def files_read(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {"success": True, "content": f.read(), "bytes": len(f.read())}
    except FileNotFoundError:
        return {"success": False, "error": "FILE_NOT_FOUND", "path": path}
    except PermissionError:
        return {"success": False, "error": "PERMISSION_DENIED", "path": path}
```

This allows the model to reason about the error and either retry with corrected parameters, try an alternative approach, or explain the failure to the user — rather than crashing or hallucinating a result.

### 5. Retry Logic with Exponential Backoff

For tools that call external services, transient failures are common. Implement retry with backoff inside the tool function so the model does not have to manage retries explicitly:

```python
import time

def search_web(query: str, num_results: int = 5) -> dict:
    for attempt in range(3):
        try:
            results = _do_search(query, num_results)
            return {"success": True, "results": results}
        except NetworkError as e:
            if attempt == 2:
                return {"success": False, "error": str(e)}
            time.sleep(2 ** attempt)
```

### 6. Gate Tool-Calling Rounds with Signal Detection

Not every message needs a tool round. Pure conversation ("thank you", "good morning", "what do you think about X?") wastes tokens and adds latency if forced through a tool-selection pass. A lightweight pre-filter using regex or a small classifier can determine whether the message plausibly triggers any registered tool, short-circuiting the overhead for pure-chat turns.

```python
_TOOL_SIGNALS = re.compile(
    r"(file|disk|weather|search|run|find|crash|memory|note|time|ping|"
     r"process|install|schedule|reminder|open|write|read|scan)",
    re.IGNORECASE
)

def needs_tool_round(message: str) -> bool:
    return bool(_TOOL_SIGNALS.search(message))
```

### 7. Limit Tool Output Size

Models have finite context windows. A tool that returns a 500KB log file will consume the entire context and degrade reasoning quality. Implement output truncation with a clear signal:

```python
MAX_TOOL_OUTPUT = 8000  # characters

def _truncate(text: str) -> str:
    if len(text) > MAX_TOOL_OUTPUT:
        return text[:MAX_TOOL_OUTPUT] + f"\n[...truncated, {len(text)} total chars]"
    return text
```

The model will see the truncation marker and can ask for a more targeted query if needed.

### 8. Keep the Tool Registry Auditable

Maintain a single source of truth for all registered tools — a registry module or a YAML/JSON manifest. This enables:

- Counting tools (useful for sanity checks — model attention degrades with too many tools)
- Generating documentation automatically
- Disabling tools at runtime without code changes
- Auditing what capabilities an agent has before deploying it

---

## Local vs Cloud LLM Tool Calling

### Cloud LLMs (OpenAI, Anthropic, Google)

Cloud models like GPT-4o and Claude 3.5 Sonnet have native, first-class tool calling support baked into their APIs. The protocol is standardized, well-documented, and optimized. These models were fine-tuned on millions of tool-call examples and reliably produce well-formed JSON arguments even for complex schemas.

**Advantages:**
- High tool-call accuracy out of the box
- Support for parallel tool calls (multiple calls in a single response)
- Automatic schema validation in many SDKs
- Streaming support for progressive output

**Disadvantages:**
- Data leaves the user's machine — privacy concern for sensitive tools
- API costs per token make high-frequency tool loops expensive
- Rate limits can throttle agentic workflows
- Latency from network round-trips

### Local LLMs (Ollama, llama.cpp, LM Studio)

Running LLMs locally via Ollama or llama.cpp changes the trade-off profile significantly.

**Advantages:**
- Complete data privacy — nothing leaves the host
- No API costs — inference is only electricity
- No rate limits — run as many tool loops as needed
- Lower latency for models that fit comfortably in VRAM

**Disadvantages:**
- Tool-call accuracy varies widely by model and quantization level
- Smaller models (7B-14B parameters) are more prone to malformed JSON or wrong tool selection
- Context windows are smaller on consumer hardware
- No native parallel tool calls in most local model servers (as of early 2025)

### Tool Calling with Ollama

Ollama supports tool calling for models that implement it (llama3.1, Qwen2.5, Qwen3, Mistral-Nemo, etc.) via the `/api/chat` endpoint with a `tools` field:

```python
import ollama

response = ollama.chat(
    model="qwen3:8b",
    messages=[{"role": "user", "content": "What files are in my documents folder?"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "files.list",
            "description": "List files in a directory. Return results as a formatted list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Absolute path to list."},
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. *.txt"}
                },
                "required": ["directory"]
            }
        }
    }]
)

if response.message.tool_calls:
    for tool_call in response.message.tool_calls:
        name = tool_call.function.name
        args = tool_call.function.arguments
        result = dispatch_tool(name, args)
        # Feed result back into conversation and continue
```

For models without native tool support, structured output mode (`format: "json"`) combined with a well-crafted system prompt that describes the tool schema and expected JSON output format can approximate tool calling behavior, though with lower reliability.

### Practical Recommendations for Local Deployment

1. **Use the largest model you can fit** — tool-call accuracy scales strongly with model size
2. **Prefer instruction-tuned chat models** over base models
3. **Keep tool lists under 20-25 tools** — local models saturate faster than cloud models
4. **Test JSON output thoroughly** — validate and sanitize all tool call arguments before execution
5. **Implement a fallback** — if the model produces malformed JSON, catch it and ask the model to retry

---

## Future Directions

### Tool Learning and Self-Extension

Current agents use static, developer-curated tool sets. Emerging research explores agents that can write new tools on-demand: generating a Python function, testing it, and registering it in the tool registry for future use. This enables unbounded capability expansion within a session.

### Multi-Agent Tool Sharing

As agentic systems grow more complex, multiple specialized agents collaborate. One agent orchestrates; others are specialists (search agent, code agent, memory agent). Tool calling becomes inter-agent communication, where "calling a tool" means delegating to a peer agent. Protocols like MCP (Model Context Protocol, introduced by Anthropic in late 2024) standardize how tools are discovered and called across agent boundaries.

### Grounded Tool Verification

A persistent challenge is that models can confidently call a tool with plausible-but-wrong parameters, and then confidently misinterpret the result. Future work focuses on verification steps: after a tool call, a second model pass checks whether the result actually answers the question, triggering a re-call with corrected parameters if not.

### Reducing Hallucination in Tool Selection

Models sometimes call a tool when none is needed, or call the wrong tool. Classifier-gated tool calling (deciding *whether* to tool-call with a lightweight discriminator model before invoking the full tool-selection pass) is an active area of optimization that can significantly reduce false-positive tool calls and improve perceived response quality.

### Persistent Tool Memory

Today, tool call history is typically discarded at the end of a session. Future agents will maintain a persistent log of what tools were called, with what arguments, and what results came back. This log becomes a form of episodic memory — the agent can look back at "last Tuesday I checked disk usage and it was at 43%" without re-calling the tool.

---

## Conclusion

Tool calling is the keystone capability that transforms a language model from a static knowledge retriever into an autonomous agent. The mechanism is conceptually simple — structured JSON calls intercepted by a host system — but the engineering surface is rich: schema design, error handling, signal detection, output sizing, retry logic, and the discipline of the ReAct loop all combine to determine whether an agent is reliable or brittle.

The most important design insight is that **tool descriptions are instructions, not documentation**. They do not merely tell the model what a tool does — they shape the model's behavior before, during, and after each tool invocation. Treating them as post-processing directives, with explicit guidance on how to interpret and present results, is one of the highest-leverage improvements available to agent developers.

Local LLM deployments via Ollama offer a compelling privacy-preserving alternative to cloud API agents, with the trade-off of lower base accuracy and the need for more careful schema design and output validation. As local models continue to improve — Qwen3 8B performing comparably to GPT-4-class models from just two years prior is a striking example — the gap between local and cloud agent capability is narrowing rapidly.

The field is moving quickly. Multi-agent collaboration, self-extending tool registries, and persistent episodic tool memory are no longer theoretical — they are active areas of engineering in 2025. The practitioners who understand the fundamentals of how tool calling works will be best positioned to build on these advances as they mature.

---

*Paper generated: 2026-02-28*
*Knowledge coverage: Tool calling in LLMs through mid-2025*
*Related project: Kai — local AI agent at `c:/newB` using Ollama + Python + SQLite*

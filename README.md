# LangBang

Agentic chat server in the spirit of llama.cpp's built-in web server —
but built on the LangChain/LangGraph ecosystem:

- **LangGraph** ReAct agent with SQLite checkpointing (thread persistence)
- **LangChain** `ChatOpenAI` pointed at any OpenAI-compatible backend
  (default: sglang on `spark-ee93:30000` serving `RadixArk/Qwen3.8-Flash-Next-NVFP4`)
- **Built-in local tools** (`server/local_tools.py`): `run_bash` (shell on the
  server host, cwd=home, 300s cap), `read_file`, `write_file`, `list_dir`,
  `crawl_url` (fetch any web page as Markdown)
- **MCP servers** via `langchain-mcp-adapters` (stdio / SSE / streamable-HTTP)

## Spark services wired in by default

Both run on `spark-ee93` as the user's own long-lived services — LangBang just
points at them:

- **`rag-mcp`** (`~/projects/rag-mcp`, streamable-HTTP MCP at `:8004/mcp`):
  hybrid RAG over distilled Claude/Codex/Hermes session history. Tools:
  `rag_search`, `rag_ingest_text`, `rag_ingest_url`, `rag_status`.
- **`crawl4ai-workbench`** (`~/projects/crawl4ai-workbench`, REST at `:8088`):
  it exposes no MCP transport, so LangBang wraps its `POST /api/crawl` as the
  `crawl_url` local tool instead (override target with
  `LANGBANG_CRAWL4AI_URL`). Note the workbench blocks private-network crawl
  targets (SSRF guard) — crawl public URLs only.
- **LangSmith** tracing — set `LANGCHAIN_API_KEY` + `LANGCHAIN_TRACING_V2=true`
  in the environment and it activates automatically via langchain-core
- Web UI: dark Mega Man X HUD by default, streaming chat, visible
  thinking/tool-call cards, thread management, live config editor
- Sound effects: wired up, assets deferred → see `SOUND_DESIGN.md`

## Run

```bash
uv sync
uv run uvicorn server.main:app --port 8123
# open http://localhost:8123
```

## ⚠ Security

The built-in tools give the agent **unsandboxed shell and file access on the
machine running the server** (`run_bash` = `bash -lc`). There is no approval
prompt — by design, this is a personal LAN tool. Do **not** expose
`/api/chat` beyond localhost/LAN, and do not add auth-bypassing proxies in
front of it. Anyone who can post to `/api/chat` can run commands as you.

## Layout

```
server/    FastAPI + LangGraph agent + MCP manager + config store
web/       static vanilla-JS UI (no build step)
data/      SQLite store + settings.json (gitignored)
```

## Performance note (single-node Spark)

The backing model runs on one DGX Spark (GB10): ~20–25 tok/s decode,
~1.5–2.5k tok/s prefill. Keep system prompts and histories small; the agent
is configured for short prompts and the UI shows token streaming so nothing
feels frozen. Long MCP tool outputs are truncated in the UI.

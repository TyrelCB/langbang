# LangBang

Agentic chat server in the spirit of llama.cpp's built-in web server —
but built on the LangChain/LangGraph ecosystem:

- **LangGraph** ReAct agent with SQLite checkpointing (thread persistence)
- **LangChain** `ChatOpenAI` pointed at any OpenAI-compatible backend
  (default: sglang on `spark-ee93:30000` serving `RadixArk/Qwen3.8-Flash-Next-NVFP4`)
- **MCP servers** via `langchain-mcp-adapters` (stdio / SSE / streamable-HTTP)
- **LangSmith** tracing — set `LANGCHAIN_API_KEY` + `LANGCHAIN_TRACING_V2=true`
  in the environment and it activates automatically via langchain-core
- Web UI: dark Mega Man X HUD by default, streaming chat, visible
  thinking/tool-call cards, thread management, live config editor
- Sound effects: wired up, assets deferred → see `SOUND_DESIGN.md`

## Run

```bash
uv sync
uv run uvicorn server.main:app --host 0.0.0.0 --port 8080
# open http://localhost:8080
```

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

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
- **Vision / multimodal** — paste or attach images in the composer, gated by
  the `capabilities.vision` toggle in CONFIG (sglang can't advertise input
  modes, so it's declared). `RadixArk/Qwen3.8-Flash-Next-NVFP4` verified
  vision-capable on the Spark → on by default. Max 4 images / ~5 MB each per
  message; note images re-prefill on every history replay, so attachments are
  expensive on a single Spark — keep them few and small.
- **Reasoning (thinking)** — off by default; CONFIG toggle "enable thinking"
  sends `chat_template_kwargs.enable_thinking` to sglang/vLLM, whose separated
  `reasoning_content` deltas are recovered via a small `ChatOpenAI` subclass
  (langchain-openai ≥1.x drops non-spec fields by design). Thinking streams
  into collapsible ◈ THINKING cards and persists in thread history. The
  topbar **◈ REASONING** button shows/hides those cards per browser
  (localStorage). Thinking costs extra decode tokens per turn — worth it on
  hard problems, wasteful as a default on a single Spark.
- **Context auto-compaction** — on by default. LangGraph replays the whole
  thread into every model call, which on a single Spark means seconds of dead
  air per 10k history tokens. When a thread's (approximate) prompt size crosses
  `compact_trigger_tokens` (default 40k), a `pre_model_hook` folds everything
  before the last ~20 messages into one model-written summary; the archived
  originals stay in SQLite so the UI still shows the full transcript, with a
  collapsible `⟲ CONTEXT COMPACTED` card where the fold happened. All knobs
  (enable, trigger, keep-count, summary budget) live in CONFIG. The turn that
  triggers compaction pays one extra summary call (prefill of the old prefix)
  before it gets cheaper forever.
- **Token & speed readout** — every model call streams a `⚡ IN → OUT · TTFT ·
  PREFILL ~t/s · DECODE t/s` line (usage chunks requested via
  `stream_usage=True`; the prefill figure includes time-to-first-token, so
  treat it as a lower bound). ReAct rounds and the compaction summarizer each
  report their own line.
- **Per-thread context size** — a `~tokens` chip on each sidebar thread
  (same chars/4 estimator as compaction) and a topbar `CTX ~N` for the open
  thread: what the *next* model call re-prefills (post-compaction state, not
  the full UI transcript).
- **Chat search** — `⌕ SEARCH CHATS` scans every thread's full transcript
  (compaction archives, thinking and tool-call args included, capped at 50
  hits); clicking a hit opens the thread and scrolls to the matching message
  with a flash highlight. Deep-linkable with `?q=`.
- Web UI: dark Mega Man X HUD by default, streaming chat, visible
  thinking/tool-call cards, thread management, live config editor
- Sound effects: wired up, assets deferred → see `SOUND_DESIGN.md`
- X-SIM: built-in Mega Man X style platformer (⚔ button / `G`) for waiting on
  agent runs — deep link with `?game=1`

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
~1.5–2.5k tok/s prefill. Keep system prompts small and let auto-compaction
(above) keep histories bounded; the UI shows token streaming so nothing feels
frozen. Long MCP tool outputs are truncated at the source and in the UI.

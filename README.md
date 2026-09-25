# LangBang

Agentic chat server in the spirit of llama.cpp's built-in web server —
but built on the LangChain/LangGraph ecosystem:

- **LangGraph** ReAct agent with SQLite checkpointing (thread persistence)
- **LangChain** `ChatOpenAI` pointed at any OpenAI-compatible backend
  (default: sglang on `spark-ee93:30000` serving `RadixArk/Qwen3.8-Flash-Next-NVFP4`)
- **Built-in local tools** (`server/local_tools.py`): `run_bash` (shell on the
  server host, cwd=home, 300s cap), `read_file`, `write_file`, `list_dir`,
  `crawl_url` (fetch any web page as Markdown)
- **Deep agent mode** (`deepagents`, on by default; CONFIG toggle): the agent
  gets `write_todos` planning and a `task` tool that spawns autonomous
  sub-agents, and its file tools (`ls`, `read_file`, `write_file`,
  `edit_file`, `glob`, `grep`) come from the harness on the real filesystem
  — our same-named `read_file`/`write_file` step aside, and oversize tool
  results get auto-evicted to disk). `run_bash` stays the only
  shell — the harness's extra `execute` tool is excluded. Turning the toggle
  off falls back to the plain LangGraph ReAct graph
- **Skills** (Agent Skills spec; deep mode, CONFIG toggle): layers
  `~/.hermes/skills` — your existing Hermes tree, categories included —
  under a LangBang-owned `~/.langbang/skills` dir. Hermes stays the package
  manager (its tree is loaded read-only; a deny rule blocks the harness file
  tools from writing it — keep installing/updating via the `hermes` CLI);
  the LangBang dir is fully writable, so the agent self-authors skills there
  when a procedure proves reusable, and a LangBang skill shadows a same-name
  Hermes one. Sources re-scan every turn (add/remove a SKILL.md and it's
  live next turn); the model sees name+description and `read_file`s the full
  SKILL.md when a task matches. `task` sub-agents don't get skills yet
- **MCP servers** via `langchain-mcp-adapters` (stdio / SSE / streamable-HTTP),
  managed as structured rows in CONFIG (no raw JSON): per-server enable
  toggle, ↻ Test connection (live tool listing via a throwaway probe client —
  `POST /api/mcp/test`), edit with rename + collision guard, delete, and
  Import config from file (accepts Claude Desktop `mcpServers` and bare-dict
  shapes). A parked server (`disabled`) keeps its entry but contributes no
  tool schemas; saves apply on the next chat turn (config fingerprint is
  re-checked every run — no restart)
- **Shell mode**: a chat message starting with `!` runs the rest as a bash
  command on the server (same shell + output cap as `run_bash`) **without
  spending a model call** — the exchange is appended to the thread, so the
  agent sees the output on its next turn (Claude Code's `!cmd`, same idea)
- **Scheduled tasks** (⏰ SCHEDULES): cron-driven agent turns that run with no
  client — each task owns a dedicated thread where every firing posts its
  prompt as a real user turn, so run history accumulates and the agent's
  context carries over between runs. Editor: 5-field cron + preset chips +
  live preview (`GET /api/schedules/next`). Runs are serialized one at a time;
  fires missed while the server was down are skipped, never replayed;
  "▶ RUN" fires one off without shifting the cron rhythm. Each run's user
  bubble carries a `⏰ SCHEDULED RUN · … · YYYY-MM-DD HH:MM:SS` stamp (UI-only,
  stored as message kwargs) so you can tell which output came from which run.
  Deleting a task takes its notebook thread with it. The agent manages the same surface via
  tools (`create/list/update/set_scheduled_task_enabled/run_scheduled_task_now/
  delete_scheduled_task` in CONFIG → LOCAL TOOLS), so "check X every 4 hours"
  said in chat becomes a real LangBang schedule — not crontab improvisation.

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
- **`all-media`** (streamable-HTTP MCP at `:8005/mcp`): 28 media-gen tools
  incl. `audio_sfx` (stable-audio-sfx backend). Registered parked
  (`disabled`) by default so its tool schemas don't ride every prefill —
  enable it in CONFIG → MCP SERVERS when you want media gen from chat. It
  rendered this app's own sound pack and backs CONFIG → SOUNDBOARD cue
  regeneration even while parked (see `SOUND_DESIGN.md`, `server/sfxgen.py`)
- **LangSmith** tracing — set `LANGCHAIN_API_KEY` + `LANGCHAIN_TRACING_V2=true`
  in the environment and it activates automatically via langchain-core
- **Vision / multimodal** — paste or attach images in the composer, gated by
  the `capabilities.vision` toggle in CONFIG (sglang can't advertise input
  modes, so it's declared). `RadixArk/Qwen3.8-Flash-Next-NVFP4` verified
  vision-capable on the Spark → on by default. Max 4 images / ~5 MB each per
  message; note images re-prefill on every history replay, so attachments are
  expensive on a single Spark — keep them few and small.
- **File attachments** — 📎 ATTACH takes any file (json/mp4/mp3/…): recognized
  images ride the base64 vision path when vision is on; everything else
  uploads to `data/uploads/<token>/` (`POST /api/upload`, ≤6 files / ≤20 MB
  each) and the message gains an `[attached file] … read it from: <path>`
  line — the agent opens it with its file tools or `run_bash` (ffprobe on a
  video, parse a ComfyUI workflow json…). Attach works with vision off.
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
- **Thread titles** — every sidebar row carries ✎ (manual inline rename), ⚡
  (LLM auto-title from the conversation) and ⟲ (restore the *initial* title —
  the first message's seed or the scheduled task's name). Nothing is locked:
  ⚡ may re-title over a manual name any time.
- **✦ RECAP** — topbar button asks the model for a cold-resume brief of the
  open thread (goal / established facts / current state / open items) shown
  in a modal — one call over a head+tail transcript slice, so 400-message
  threads recap in seconds.
- **Empty-thread hygiene** — `+ NEW CHAT` only materializes a sidebar row
  once the first message is sent, and an hourly server sweep deletes
  message-less `New chat` rows untouched for over an hour.
- Web UI: dark Mega Man X HUD by default, streaming chat, visible
  thinking/tool-call cards, thread management, live config editor
- Sound effects: Mega Man X-style cues in `web/sounds/` (generated via
  `audio_sfx` on the all-media MCP server) — `♪ SOUND` button toggles them;
  CONFIG → SOUNDBOARD auditions any cue and ↻ REGEN re-renders just that one
  (committed prompt, fresh seed) live, without a restart or reload. All cues
  share one AudioContext (resumed on first input), so rAF-driven game cues
  are as audible as click-driven chat ones
- X-SIM: built-in Mega Man X style platformer (⚔ button / `G`) for waiting on
  agent runs — deep link with `?game=1`

## Run

```bash
uv sync
uv run uvicorn server.main:app --host 0.0.0.0 --port 8123
# open http://localhost:8123        (or http://<LAN-IP>:8123 from any device)
```

The harness box also runs **Caddy on :80** as a friendly LAN front
(`/etc/caddy/Caddyfile` → `reverse_proxy 127.0.0.1:8123` with
`flush_interval -1` so chat SSE streams instead of buffering).
⚠ This makes `/api/shell` reachable by everything on the LAN — and over
Tailscale if you're off-network — never expose either port beyond
LAN/tailnet (no port-forwards, no internet tunnels).

## Voice (read-aloud + STT)

Every finished assistant reply gets a 🔊 button; the topbar **VOICE: SPEAK**
toggle reads each finished reply automatically. Markdown is stripped before
synthesis (code blocks become a spoken placeholder), so code-heavy answers
stay listenable.

Defaults ride the **same keyless Google endpoints telemarketing used** — gTTS
(Translate TTS) for speech and `SpeechRecognition.recognize_google` for
transcription. Both are unofficial/gray-ToS: fine for a personal LAN tool,
but occasional 429s under heavy use are possible. If that bites, CONFIG →
VOICE switches either direction to Google Cloud behind a service-account key
(`uv sync --extra voice-gcloud`, key file dropped under `data/keys/` — that
directory is gitignored; never commit keys).

The mic/voice-chat half is **ready server-side but not wired client-side**:
`POST /api/stt` takes 16 kHz mono PCM16 WAV (or bare PCM) and returns text.
Browser recording needs a secure context, so on `http://<LAN-IP>` (or
:8123 directly) `getUserMedia` is refused.

**Mic access options (deferred — revisit before building the recorder):**

1. **Caddy + public zone + ACME DNS-01 (decided direction).** Caddy now
   runs on the harness box itself (currently plain HTTP on :80);
   `tls dns <provider>` validates via the DNS API, so
   nothing opens inbound (no port-forwards — the ⚠ Security rule stands).
   Real wildcard cert → trusted on every machine with zero per-machine CA
   imports. The `langbang` A record points at the LAN IP of whichever box
   hosts the harness today: the record travels with the harness, which is
   what makes it portable. Add the hostname + `tls dns` block to the local
   Caddyfile when the DNS provider is settled; until then the mic only
   works over `http://localhost` (secure context) or the SSH tunnel below.
   DNS API token lives in a chmod-600 EnvironmentFile for the Caddy unit,
   never in the repo.
2. **Self-signed + `uvicorn --ssl-*`** — zero new moving parts, but a
   per-machine CA import and a regen dance whenever the hostname changes.
   Fine as a stopgap.
3. **SSH tunnel (`ssh -L 8123:localhost:8123 tyrel@<harness-host>`)** — no TLS
   work at all; the mic works because `http://localhost` is *already* a
   secure context. That nuance also means: on whichever machine literally
   hosts the process, voice chat needs no TLS.
4. Never: a tunnel/port-forward that makes the URL internet-reachable —
   an unauthenticated `run_bash` behind a padlock is still an
   unauthenticated shell.

## ⚠ Security

The built-in tools give the agent **unsandboxed shell and file access on the
machine running the server** (`run_bash` = `bash -lc`). There is no approval
prompt — by design, this is a personal LAN tool. Do **not** expose
`/api/chat` beyond localhost/LAN, and do not add auth-bypassing proxies in
front of it. Anyone who can post to `/api/chat` (or `/api/shell`, the `!cmd`
route) can run commands as you.

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

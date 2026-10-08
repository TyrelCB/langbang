# LangBang — feature notes & design log

The full, detailed reference for every LangBang feature: what it does, why it's
built the way it is, and the incidents that shaped it. The short version lives in
the [README](../README.md).

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
  shell — the harness's extra `execute` tool is excluded. If a multi-step
  task burns several tool calls without ever opening a todo list, a staged
  enforcer nudges the model to plan (harder "FINAL WARNING" if the first
  nudge is ignored, then accepted — never a nudge spiral; a finish-line
  nudge reconciles stale items before the final answer). Turning the toggle
  off falls back to the plain LangGraph ReAct graph
- **Tool-call reliability**: local models sometimes mangle tool-call args in
  transit — Qwen drifts `file_path` to `path`, and multi-KB `content` can be
  dropped whole (a `→ {}` call). `_FileArgAlias` middleware renames known
  aliases before validation (server log: `arg-repair:`), schema-loss failures
  surface as red ✕ FAILED cards + trajectory rows instead of frozen "…"
  spinners (`on_tool_error`), and the deep prompt teaches the agent to chunk
  big files (write a skeleton, append the rest via `run_bash` heredocs)
- **Skills** (Agent Skills spec; deep mode, CONFIG toggle): layers
  `~/.hermes/skills` — your existing Hermes tree, categories included —
  under a LangBang-owned `~/.langbang/skills` dir. Hermes stays the package
  manager (its tree is loaded read-only; a deny rule blocks the harness file
  tools from writing it — keep installing/updating via the `hermes` CLI);
  the LangBang dir is fully writable, so the agent self-authors skills there
  when a procedure proves reusable, and a LangBang skill shadows a same-name
  Hermes one. Sources re-scan every turn (add/remove a SKILL.md and it's
  live next turn); the model sees name+description and `read_file`s the full
  SKILL.md when a task matches. `task` sub-agents don't get skills yet.
  Every agent write to a LangBang `SKILL.md` is validated on the spot (broken
  frontmatter = a skill that silently never loads → the tool result tells
  the agent to fix it)
- **Providers + per-thread model override** (CONFIG → MODEL): a list of named
  OpenAI-compatible backends (sglang, vLLM, llama-server, Ollama `/v1`,
  OpenAI, OpenRouter…) — base URL, API key, vision, and whether to send
  sglang/vLLM `chat_template_kwargs` (the Qwen thinking switch; turn it off
  for OpenAI, which rejects unknown args). One is the global default
  provider + model. Click the model name in a thread's top bar to override
  provider and/or model for that thread only (◇ marks an override; USE
  DEFAULT clears it) — a scheduled task runs in its own thread, so it can
  be pointed elsewhere the same way. Model dropdowns are filled live from
  each backend's `/models` (`POST /api/models`); overrides live in
  `threads.model` (`GET/PUT /api/threads/{id}/model`) and apply from the
  thread's next run. Titles, recaps and the learning review keep using the
  global default.
- **Learning loop** (`server/learning.py`, CONFIG → MEMORY & LEARNING):
  - *Per-thread prompt* — ✎ PROMPT in the top bar: instructions for that
    thread only, appended to the global system prompt on every run there
    (scheduled runs too); lit while set. `GET/PUT /api/threads/{id}/prompt`.
  - *Memory* — one markdown file per fact in `~/.langbang/memory/`
    (frontmatter `name`/`description`/`type` = user|feedback|project|reference).
    The one-line index, built from the files themselves, rides every run's
    system prompt; the agent reads a body on demand and saves/updates with
    the `remember` / `forget` tools. Edit or delete them in FILES or CONFIG.
  - *Automatic review* — after a clean interactive run with ≥ N tool calls
    (default 6; scheduled runs opt-in), one background no-tool model call
    reads the turn and may create a LangBang skill, patch one the turn read
    (SEARCH/REPLACE), and/or save memories — usually it decides nothing is
    worth keeping. Results are validated before writing (Hermes skills are
    never touched), the previous version goes to `~/.langbang/skill-history/`,
    every review is logged to `data/learning.jsonl` (CONFIG lists them), and
    changes pop a 📘 LEARNED toast. 📘 REVIEW THIS THREAD NOW runs it by hand.
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
- **Scheduled tasks** (⏰ SCHEDULES): repeating (5-field cron) or **one-off**
  ("run once at…" — date-time picker + quick chips; the agent's
  `create_scheduled_task(run_at="YYYY-MM-DD HH:MM")` handles "remind me at 3pm")
  agent turns that run with no client; a fired one-off shows ✓ DONE for 24 h,
  then leaves the list while its thread keeps the result (a one-off missed while
  the server was down fires late on startup). Each task owns a dedicated thread where every firing posts its
  prompt as a real user turn, so run history accumulates and the agent's
  context carries over between runs. Editor: 5-field cron + preset chips +
  live preview (`GET /api/schedules/next`). Runs are serialized one at a time;
  repeating fires missed while the server was down are skipped, never replayed;
  "▶ RUN" fires one off without shifting the cron rhythm. Each run's user
  bubble carries a `⏰ SCHEDULED RUN · … · YYYY-MM-DD HH:MM:SS` stamp (UI-only,
  stored as message kwargs) so you can tell which output came from which run.
  Deleting a task takes its notebook thread with it. The agent manages the same surface via
  tools (`create/list/update/set_scheduled_task_enabled/run_scheduled_task_now/
  delete_scheduled_task` in CONFIG → LOCAL TOOLS), so "check X every 4 hours"
  said in chat becomes a real LangBang schedule — not crontab improvisation.
- **Image generation + editing** (`server/comfy.py`): Qwen-Image 2.1
  (uncensored Q4_K_M GGUF) on the ComfyUI instance on spark-ee93, one merged
  workflow — text-to-image with no inputs, edit/compose with 1–10 reference
  images (the prompt calls them image_1…). The agent's `generate_image` tool
  ("make an image of…", "edit this photo…") and FILES → ✨ EDIT WITH AI both
  use it; results (+ a JSON sidecar with prompt/seed/inputs) land in
  `~/Pictures/langbang/` and show inline when cited. Pasted/attached images
  are now also saved to `data/uploads/` with their path in the message, so
  the agent can edit them. ~40–60 s per image. CONFIG → IMAGE GENERATION.
- **Notifications** (`server/notify.py`, CONFIG → NOTIFICATIONS): when a
  run **needs your input** (ask_user / plan), **finishes**, or **fails** —
  chat, scheduled or `!cmd` — and you're not looking at that thread: every
  open tab shows a toast (click opens the thread) and a `(n)` title badge,
  desktop popups fire where the browser allows them (https or
  `http://localhost` only — enable per browser), and your phone gets a
  **ntfy** push (free app; private random topic; tap opens the thread via
  the "tap opens" URL). Pushes are skipped while a visible tab shows that
  thread (tabs report presence on the 4 s runs poll). A STOP you pressed
  isn't an event. Preview text passes through the ntfy server — this box
  self-hosts it: Docker container `ntfy` (restart unless-stopped, config +
  cache in `~/.local/share/ntfy/`), bound to localhost + the tailnet IP only
  (`http://<host>.<tailnet>.ts.net:2586`, not the LAN); LangBang publishes to
  `http://127.0.0.1:2586`. Phones subscribe over Tailscale.
- **Detached runs** (`server/runs.py`): every run — chat turn, `!cmd`, or
  scheduled firing — belongs to a server-side hub, not to a browser tab. A
  dropped connection (phone screen timeout, app switch, refresh, proxy blip)
  never cancels work in flight; reopening the thread reattaches and replays
  what was missed (`GET /api/runs` + `GET /api/threads/{tid}/stream?since=N`,
  heartbeats keep Caddy/phones attached). Only STOP cancels a run. Many
  threads can run at once (cap 4 — shared GPU), each with its own SEND/STOP,
  and busy threads get a pulsing ◉ in the sidebar.
- **Inline media**: absolute file paths (`.png`, `.mp4`, `.wav`, …) cited
  in an answer — plain text, or an inline-code span that is exactly the
  path — render as inline images/players via `GET /api/media?path=…`
  (Range/206 → seekable video), scanned at finalization only and never
  inside code blocks; a path missing on disk collapses to a dim "missing"
  chip instead of a dead player. `read_file` on audio/video never feeds the
  base64 to the model (a note tells it to cite the path instead).

- **Stale-tab guard**: `/api/health` carries a fingerprint of `web/`; a tab
  that was open when the frontend changed shows a ⟳ UPDATED bar (RELOAD keeps
  the open thread and the unsent draft; LATER hides it until the next change).
- **Human gates + plan mode**: the agent has an `ask_user` tool — when it's
  blocked on a decision only you can make it asks 1–4 questions (option
  chips + free text) and the run *pauses* (a langgraph `interrupt`, stored
  in the checkpoint, so it survives reloads, other devices and server
  restarts — `GET /api/threads/{tid}/gate`). Answering resumes the same graph
  (`POST /api/threads/{tid}/resume`); typing in the composer while a gate
  waits also counts as the answer. **◇ PLAN** (composer button or
  Shift+Tab, per thread) turns on plan mode: the agent investigates
  read-only (write/edit/schedule tools are withheld and hard-blocked;
  `run_bash` is instructed inspect-only), asks what it needs, then calls
  `exit_plan_mode` with a Markdown plan → ✓ APPROVE & BUILD (plan mode
  switches off and it implements in the same run) or ↺ REVISE with notes.
  Gates are main-agent only (`task` sub-agents never block on a human) and
  disabled for scheduled runs, which have nobody to answer.
- **✎ FILES editor**: topbar ✎ FILES opens a browse-and-edit panel for any
  text file on the server (folder tree, path box — Enter opens a folder or
  file, an unknown path starts a new file. Typing a path autocompletes from
  that folder (↑↓, Tab = shell-style completion); plain words search file
  and folder names recursively under the current folder via `rg --files`
  (`GET /api/fs/find`, time-boxed, .gitignore-aware, separator-insensitive).
  Syntax highlighting for ~36 languages (the vendored highlight.js,
  picked by extension; a colored layer mirrors a transparent textarea, so
  native undo/selection/IME keep working — files over 400 KB stay plain),
  and Markdown files get EDIT / SPLIT / PREVIEW (chat's renderer; relative
  images resolve through `/api/media`, relative links open in the editor).
  Images, audio, video and PDFs open in a viewer instead (SVG adds ✎ EDIT
  SOURCE). Under a text file sits an **assist bar**: ask about the file or
  tell the AI what to change — one tool-less model call (`POST /api/fs/assist`)
  that sees the current buffer (unsaved edits included), your selection and
  the last few exchanges about this file, and proposes search/replace edits
  shown as a red/green diff; ✓ APPLY puts them in the buffer (Ctrl+Z undoes),
  SAVE is still yours. It can't touch any other file.
  Line gutter, Tab indents,
  Ctrl+S saves). File-tool cards in the chat (`read_file`/`write_file`/
  `edit_file`) carry a ✎ open shortcut. Saves are atomic and content-hash-guarded
  (`GET /api/fs/list|read`, `PUT /api/fs/write`): if the agent rewrites the
  file while it's open you get RELOAD / OVERWRITE, never a silent clobber.
  CRLF files round-trip; binary, non-UTF-8 and >2 MB files are refused.
  Same LAN trust posture as `/api/shell`.

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
- **Mid-run thread switching** — a run's live output belongs to *its* thread:
  switch panes while it streams and the bubbles, tool cards and usage lines
  park off-screen instead of painting over the thread you're reading; switch
  back and they re-attach in place and keep filling in (`!cmd` shell cards
  included). Persistence was never the bug — this is the display layer
  keeping each thread's pane honest.
- Web UI: dark Mega Man X HUD by default, streaming chat, visible
  thinking/tool-call cards, thread management, live config editor
- Phone-friendly: below ~720px the sidebar becomes an off-canvas drawer (☰ +
  tap-to-dismiss scrim), row actions (✎⚡⟲✕) show without hover, the topbar
  buttons wrap instead of stretching the page, and the config modal fits a
  phone screen — usable straight from a phone on the LAN
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

Synthesis is **cached on disk** (`data/tts/`, sha256 of the cleaned text +
provider, pruned after 30 days), so a reply is only ever synthesized once —
replays hit the file (or even the page's own blob) and start instantly. The
first play opens a mini player inside the bubble: ⏸/▶, −5/+5 s, click the bar
to seek. Each bubble keeps its own audio (the player stays for replay);
playing a new bubble pauses whichever was going.

Defaults ride the **same keyless Google endpoints telemarketing used** — gTTS
(Translate TTS) for speech and `SpeechRecognition.recognize_google` for
transcription. Both are unofficial/gray-ToS: fine for a personal LAN tool,
but occasional 429s under heavy use are possible. If that bites, CONFIG →
VOICE switches either direction to Google Cloud behind a service-account key
(`uv sync --extra voice-gcloud`, key file dropped under `data/keys/` — that
directory is gitignored; never commit keys).

**Pocket TTS (local, CPU-only, streaming)** — CONFIG → VOICE → `pocket`:
kyutai's 100M-param [pocket-tts](https://huggingface.co/kyutai/pocket-tts)
runs inside the LangBang process on CPU torch (pinned to PyTorch's CPU wheel
index in `pyproject.toml` — no CUDA download). 27 preset voices (alba, eve,
marius, …), English plus French/German/Spanish/Italian/Portuguese/Dutch.
Measured on this box (i9-12900HK): model load ~6 s (prewarmed at startup
when it's the active provider), **first audio ~0.2 s**, ~4.5x realtime,
2 threads as fast as 8, ~1.3 GB RSS. 🔊 doesn't wait for the whole reply:
`POST /api/tts` returns a clip URL and the bubble's `<audio>` plays the MP3
while it's generated (`server/ttsjobs.py`: model → ffmpeg → followers); the
finished stream becomes the `data/tts/` cache file, so replays are instant
and seekable. STOP on a long reply cancels the synthesis after ~20 s without
a listener. CONFIG → VOICE's **Pocket voice** dropdown lists MY VOICES (saved clones),
the 27 PRESETS and **＋ Clone a new voice…**; ▶ previews any of them
without saving, ✕ deletes a saved one (two clicks). Cloning: upload 10–30 s
of one speaker (any audio/video ffmpeg reads; first 30 s used), or
**● RECORD** it in place (secure context; native sample rate with no noise
suppression or echo cancellation, which would reshape the voice; a read-aloud
passage is shown, auto-stops at 30 s, ▶ to check the take; consent box
required — kyutai's terms forbid cloning without permission). The server
encodes it once into a voice state, `data/voices/<name>.safetensors`
(gitignored; the uploaded clip is deleted), which then loads on the normal
ungated model like a preset. Presets and saved voices need no Hugging Face
account; the clone step itself needs kyutai's gated weights once: accept
the terms on the model page and run `.venv/bin/hf auth login` (no restart).
Needs `ffmpeg` on PATH for streaming (falls back to whole-clip WAV).

**🎙 MIC (speech → text).** Tap 🎙 in the composer and talk: recording
stops itself after ~1.4 s of quiet once speech was heard (or tap again; it
gives up after 8 s of nothing, caps at 60 s), the browser downsamples to
16 kHz mono PCM16 WAV, and `POST /api/stt` transcribes it (CONFIG → VOICE
STT provider). The text lands in the composer; with **VOICE: SPEAK** on and
an empty box it sends straight away, so you talk and hear the answer back.
The button turns red with a live input-level fill while recording, and
starting it stops any read-aloud so the mic never records the app itself.

**TALK (hands-free).** The topbar VOICE button cycles OFF → SPEAK → TALK.
TALK opens the mic, sends when you pause, reads the answer aloud, then
listens again. The mic re-opens only once the thread's run is done and
nothing is playing, synthesizing or queued, so it never records the
read-aloud (no barge-in: tap 🎙 to cut an answer short and talk). Silence
or noise just re-arms it; VOICE → OFF ends the loop. TALK is never
restored on reload, because opening the mic needs a click.

Browsers only allow the mic in a **secure context**: an `https://` address
or `http://localhost`. On plain `http://<LAN-IP>` the button is disabled
with a hint. Ways to get HTTPS without exposing anything:

1. **Caddy + a real domain + ACME DNS-01 (what this box uses).** A
   wildcard A record points at the harness box's LAN/tailnet IP, certbot
   or Caddy validates via the DNS provider's API (no inbound ports), and
   Caddy terminates TLS and reverse-proxies to `127.0.0.1:8123` with
   `flush_interval -1` for SSE. Trusted on every device, no CA imports.
2. **SSH tunnel** (`ssh -L 8123:localhost:8123 <harness-host>`): the mic
   works because `http://localhost` is already a secure context.
3. **Self-signed + `uvicorn --ssl-*`**: a per-device CA import; stopgap only.
4. Never: a tunnel or port-forward that makes the URL internet-reachable.
   An unauthenticated `run_bash` behind a padlock is still an
   unauthenticated shell.

## ⚠ Security

The built-in tools give the agent **unsandboxed shell and file access on the
machine running the server** (`run_bash` = `bash -lc`). There is no approval
prompt — by design, this is a personal LAN tool. Do **not** expose
`/api/chat` beyond localhost/LAN, and do not add auth-bypassing proxies in
front of it. Anyone who can post to `/api/chat` (or `/api/shell`, the `!cmd`
route) can run commands as you. `GET /api/media?path=` serves any absolute
path on this machine (for the inline players) — the same read surface
`!cat /etc/shadow` already grants, so it adds no new capability, but the
same LAN/tailnet-only rule covers it.

## License & credits

LangBang is licensed under the [Apache License 2.0](../LICENSE); see
[NOTICE](../NOTICE) for attributions and
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) for bundled third-party code.

Built on [LangChain](https://github.com/langchain-ai/langchain),
[LangGraph](https://github.com/langchain-ai/langgraph) and
[deepagents](https://github.com/langchain-ai/deepagents) (MIT), served by
[FastAPI](https://github.com/fastapi/fastapi) — installed as dependencies,
not redistributed. Text-to-speech uses
[Pocket TTS](https://github.com/kyutai-labs/pocket-tts) by Kyutai (code MIT;
model weights CC-BY-4.0, gated on Hugging Face and downloaded by each user —
not included here). The UI sound effects in `web/sounds/` were generated with
Stability AI's Stable Audio 3 small-sfx model — **Powered by Stability AI**.
`tools/sglang-patches/` contains files derived from
[SGLang](https://github.com/sgl-project/sglang) (Apache-2.0).

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

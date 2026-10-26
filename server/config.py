"""Persistent app settings (data/settings.json) with sane defaults."""
import json
import os
import threading

# LANGBANG_DATA_DIR relocates everything stateful (settings, threads,
# trajectory, uploads, TTS cache, voices) — e.g. a second, isolated instance
DATA_DIR = os.path.expanduser(os.environ.get("LANGBANG_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
DB_PATH = os.path.join(DATA_DIR, "langbang.db")
# Trajectory (run_events) lives apart from the checkpoint file: per-event
# appends and multi-MB checkpoint commits must not fight over one WAL lock.
EVENTS_DB_PATH = os.path.join(DATA_DIR, "events.db")

os.makedirs(DATA_DIR, exist_ok=True)

_lock = threading.Lock()

DEFAULTS = {
    "base_url": os.environ.get("LANGBANG_BASE_URL", "http://spark-ee93:30000/v1"),
    "api_key": os.environ.get("LANGBANG_API_KEY", "none"),
    "model": os.environ.get("LANGBANG_MODEL", "RadixArk/Qwen3.8-Flash-Next-NVFP4"),
    "temperature": 0.6,
    "max_tokens": 8192,
    # Keep this short: every token costs ~2-4s of prefill on the Spark.
    # The language rule matters: Qwen-style hybrids drift to Chinese on short
    # prompts unless told to track the user's language.
    "system_prompt": (
        "You are LangBang, a concise, capable agent. Use tools when they help. "
        "Always answer in the language the user writes in (English by default); "
        "never switch languages unprompted."
    ),
    "mcp_servers": {
        # User's hybrid session-history RAG on spark-ee93 (systemd unit: rag-mcp).
        "rag-mcp": {
            "transport": "streamable_http",
            "url": os.environ.get("LANGBANG_RAG_MCP_URL", "http://spark-ee93:8004/mcp"),
        },
    },
    "max_react_iterations": 12,
    # deepagents harness: write_todos planning, a `task` sub-agent for
    # parallel/parallelizable digging, and built-in file tools on the real
    # filesystem (these replace our read_file/write_file toggles, which stay
    # inert in deep mode; run_bash remains the only shell — the harness's
    # `execute` tool is excluded in agent.py). Off = plain create_react_agent.
    "deep_agent": True,
    # Skills (Agent Skills spec, agentskills.io) — deep mode only. Sources
    # layer last-one-wins: the Hermes tree comes first (installed/updated by
    # the `hermes` CLI; a deny rule keeps the agent from writing there) and
    # the LangBang dir goes last — the agent's own, freely writable, so it
    # can self-author skills and override a Hermes one by same name.
    "skills_enabled": True,
    "skills_hermes_dir": os.environ.get("LANGBANG_HERMES_SKILLS", "~/.hermes/skills"),
    "skills_dir": "~/.langbang/skills",
    # LangBang memory: one .md per fact; the index rides every system prompt
    "memory_enabled": True,
    "memory_dir": "~/.langbang/memory",
    # after a clean run with >= min_tool_calls tool calls, a background
    # no-tool review may create/patch LangBang skills and save memories
    "skill_review": {"enabled": True, "min_tool_calls": 6, "scheduled": False},
    # ⏱ get-current check (projects.staleness): a turn that resumes a thread
    # after_h hours idle — or after min_gap_min+ minutes when its ◫ project's
    # repo got commits / sibling threads ran meanwhile — carries a note of
    # what changed, so the agent re-checks instead of trusting old state
    "staleness": {"enabled": True, "after_h": 6, "min_gap_min": 10},
    # Off by default: Qwen3-style hybrids answer silently unless asked, and
    # thinking adds a few hundred decode tokens per turn (~40-50 ms each on
    # a single Spark). Enable in CONFIG to stream reasoning into the
    # ◈ THINKING cards (the topbar REASONING button only shows/hides cards).
    "enable_thinking": False,
    # chat_template_kwargs.reasoning_effort while thinking: xhigh | medium |
    # low. The Qwen3.8 template defaults to xhigh when it isn't sent.
    "reasoning_effort": "xhigh",
    # send earlier reasoning back to the model (sglang/vLLM providers only):
    # off = never (LangBang's original behaviour) | turn = this turn's tool
    # steps (Qwen's agentic setup) | all = every past assistant message
    "keep_reasoning": "off",
    # Model input modalities. sglang/llama.cpp can't tell us, so the user
    # declares them; the UI only offers image paste/attach when vision=true.
    # RadixArk/Qwen3.8-Flash-Next-NVFP4 verified vision-capable on the Spark
    # (1x1 pixel color tests) -> on by default. Images re-prefill every turn,
    # so keep attachments small/few.
    "capabilities": {"vision": True},
    # images per chat message. 4 was a guess (09-15); measured 10-17 on the
    # vLLM Spark model: 48 in one request all read right, ~0.63 s and ~770
    # prompt tokens per 1024x768 image (4x that at the 2048 px paste edge),
    # repeats served from the prefix cache. Clamped to MAX_IMAGES_CAP.
    "max_images": 16,
    # image shown on the empty NEW CHAT screen (absolute path, ~ ok; GIFs
    # animate). Lives outside the repo — served by GET /api/splash.
    "splash_image": "",
    # Context compaction. LangGraph replays the whole thread into every model
    # call (~1.5-2.5k tok/s prefill on the Spark = ~5s dead air per 10k
    # tokens, and the 262k window is the hard stop). Past the trigger, a
    # pre_model_hook folds everything before the kept tail into one
    # model-written summary; archived originals stay in SQLite for the UI.
    "compact_enabled": True,
    "compact_trigger_tokens": 40000,
    "compact_keep_messages": 20,
    "compact_summary_tokens": 800,
    "local_tools": {
        "run_bash": True,
        "read_file": True,
        "write_file": True,
        "list_dir": True,
        "crawl_url": True,
        "generate_image": True,  # Qwen-Image 2.1 on ComfyUI (server/comfy.py)
        # RAG node (rag/ package, langbang-rag.service :8900)
        "semantic_search": True,
        "semantic_search_image": True,
        # LangBang's own scheduler, agent-facing (server/schedule.py)
        "create_scheduled_task": True,
        "list_scheduled_tasks": True,
        "update_scheduled_task": True,
        "set_scheduled_task_enabled": True,
        "run_scheduled_task_now": True,
        "delete_scheduled_task": True,
    },
    # Voice. Defaults ride the same keyless Google endpoints telemarketing
    # used (gTTS + SpeechRecognition.recognize_google) — unofficial/gray-ToS,
    # occasional 429s possible; the "gcloud" providers (service-account key in
    # data/keys/, never committed) are the supported escape hatch.
    "voice": {
        "tts_provider": "gtts",  # "gtts" | "gcloud" | "pocket" (local CPU, kyutai)
        "tts_lang": "en",
        "tts_tld": "com",  # accent: com=US, co.uk=UK, co.in=IN
        "stt_provider": "sr",  # "sr" | "gcloud"
        "stt_lang": "en-US",
        "gcloud_key_file": "",  # e.g. "data/keys/tts.json"; blank = keyless
        "gcloud_tts_lang": "en-US",
        "gcloud_tts_voice": "en-US-Wavenet-J",
        # Pocket TTS (kyutai, 100M params, CPU): preset voice name — or an
        # absolute path to a .wav/.mp3 to clone (needs the gated weights:
        # accept terms on huggingface.co/kyutai/pocket-tts + `hf auth login`)
        "pocket_voice": "alba",
        "pocket_language": "english",
        "pocket_threads": 2,
        # Qwen3-TTS ("qwen" provider): qwen-tts-stream.service on spark-ee93.
        # Cloned voices live in that service's library; "Tyrel" is registered
        # there. Presets are the Qwen CustomVoice speakers (see GET /voices).
        "qwen_tts_url": "http://spark-ee93:8098",
        "qwen_voice": "Tyrel",
        "qwen_language": "English",
        # numbers/dates/times → words before synthesis: pocket | all | off
        "tts_normalize": "pocket",  # benchmarked: 2 = 4 = 8 on this i9 (~4.5x realtime)
        # VOICE: SPEAK auto-read of ◈ THINKING cards (client-side): off | on.
        # Off by default — xhigh reasoning runs to minutes of audio, and the
        # queue is serial, so the answer would wait behind it.
        "speak_reasoning": "off",
        "spoken_hud": "on",  # read-aloud: show the spoken words in a HUD card
    },
    # Phone IVR front door (server/ivr.py, /api/ivr/*): a separate, small
    # agent — NOT the chat agent. Only tools named here are built in (MCP or
    # local, by tool name); no shell, no file writes, no memory. Auth is the
    # bearer token in data/keys/ivr_token (created on first start).
    "ivr": {
        "enabled": True,
        "tools": ["rag_search", "take_message"],
        "allow_ips": [],  # empty = any caller with the token
        "max_concurrent": 2,  # calls in a turn at once; more → 503 busy
        "max_steps": 6,  # model↔tool round trips per turn
        # which model answers calls: a provider name from "providers" ("" = the
        # chat default) and a model id ("" = that provider's own default model).
        # A small model on another box keeps calls off the chat model's
        # concurrency slots; if that box is unreachable the turn falls back
        # to the chat default.
        "provider": "",
        "model": "",
        "llm_timeout_s": 30,
        "max_tokens": 600,
        # streamed turns stop speaking at the first sentence end past this many
        # words (the model is cancelled); 0 = no cap. Phone answers that run
        # long lose the caller — the prompt asks for brevity, this enforces it.
        "max_spoken_words": 50,
        "idle_s": 1800,  # forget a call's history after this much quiet
        # messages of call history sent to the model (~25 tokens each); past
        # this the first two exchanges are kept plus the most recent. 160 ≈ a
        # 30+ minute call in ~4k tokens — size it to the IVR model's context.
        "max_history": 160,
        # TTS for calls: "" = the chat provider (voice.tts_provider), else
        # pocket | qwen | gtts | gcloud. pocket_voice is the voice NAME for
        # whichever provider speaks (qwen_voice under qwen); "" = chat's.
        "tts_provider": "",
        "pocket_voice": "alba",  # "" = the chat voice (voice.pocket_voice)
        "greeting": "Hi, this is LangBang, Tyrel's assistant. How can I help?",
        # What the IVR knows about Tyrel: a text/markdown file (relative paths
        # are under data/) appended to the system prompt on every turn and
        # re-read when it changes. Small enough to sit in the prompt, so the
        # provider's prefix cache makes it nearly free after the first turn.
        # "" = no reference document.
        "knowledge_file": "ivr/knowledge.md",
        "system_prompt": (
            "You are LangBang, Tyrel Barstow's phone assistant, answering a call. "
            "Everything you write is spoken aloud by text-to-speech, so: plain "
            "sentences only — no markdown, lists, tables, code, URLs or emoji. "
            "Keep every answer short: one or two sentences, under about forty "
            "words. Callers can't skim, so give the single most useful fact and "
            "offer more instead of listing everything; only go longer if they "
            "ask for detail. Get dates right — check them against the document. "
            "The caller's words come from speech recognition and may "
            "contain mistakes; if a request is unclear, ask a brief clarifying "
            "question. Speech recognition mangles Tyrel's name (Tyrell, Terrell, "
            "Tyler, Tara, Darrell…): anyone asking about \"Barstow\", or about a "
            "name that sounds like Tyrel, means Tyrel Barstow — answer about him "
            "and don't point out the misspelling. "
            "Answer questions about Tyrel — his work history, skills, projects, "
            "education and background — from the reference document below, and "
            "use your general knowledge for everything else, such as explaining "
            "a technology or a company he worked with. Do not use the rag_search "
            "tool: it searches private working notes that are not for callers, "
            "and the reference document is everything you should say about "
            "Tyrel. If the document doesn't cover something about him, say you "
            "don't know and offer to take a message rather than guessing. "
            "If the caller wants to reach Tyrel, asks for a call back, or needs "
            "something you can't do, offer to take a message: get their name and "
            "message (and a callback number if they want one), then use "
            "take_message. You cannot change anything else, run commands or "
            "reach the internet — say so if asked. Never read out secrets, keys, "
            "passwords, phone numbers or email addresses; for contact, offer to "
            "take a message or mention his LinkedIn or GitHub by name."
        ),
    },
    # Image generation / editing: Qwen-Image 2.1 GGUF on ComfyUI (server/comfy.py)
    # RAG node client (the node's own settings: data/rag/node.json)
    "rag": {
        "enabled": True,
        "url": "http://127.0.0.1:8900",
        "auto_ingest": True,  # push outputs/uploads/memories/skills as they're written
        "index_threads": True,  # each finished exchange becomes a searchable doc
        "scope": "fleet",  # semantic_search default: fleet | local
        # auto-search before the first model call (server/prefetch.py): a
        # reference phrase or a Laya gate >= threshold attaches the hits
        "prefetch": {"enabled": True, "threshold": 0.5, "top_k": 6, "media_k": 3, "budget_s": 1.5},
    },
    # Decision endpoint POST /v1/systemone (server/decide.py). backends speak
    # the Jev/Laya systemone protocol and are tried in order; key_file names a
    # file in data/keys/. When all fail (or a call asks x_backend: "llm") the
    # chat model — or llm_provider/llm_model — answers through a JSON schema.
    # escalate_below > 0 re-asks System 1 answers under that confidence.
    "decide": {
        "enabled": True,
        "backends": [{"name": "laya", "url": "http://127.0.0.1:8901", "key_file": "", "enabled": True}],
        "llm_fallback": True,
        "llm_provider": "",
        "llm_model": "",
        "escalate_below": 0.0,
        "timeout_s": 10,
    },
    # ▮ TERM fleet (server/term.py): [{name, ssh: alias | user@host[:port], note}]
    "fleet": {"hosts": []},
    "image_gen": {
        "comfy_url": "http://spark-ee93:8188",
        "out_dir": "~/Pictures/langbang",
        "steps": 25,
    },
    # Notifications (server/notify.py): phone push via ntfy for runs that
    # need input / finished / failed while nobody watches that thread.
    "notify": {
        "ntfy_enabled": False,
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",
        "events": {"input": True, "done": True, "failed": True},
        "preview": True,
        "click_base": "",
    },
}


KEEP_REASONING = ("off", "turn", "all")


def _provider_name(url: str) -> str:
    """'http://spark-da36:30000/v1' → 'spark-da36' (first provider's name)."""
    from urllib.parse import urlparse
    return (urlparse(url or "").hostname or "default").split(".")[0] or "default"


def _providers(merged: dict) -> dict:
    """Named OpenAI-compatible backends (2026-10-06). Settings from before
    providers existed carry one top-level base_url/api_key/capabilities —
    that becomes the first provider, and stays the default."""
    provs = {k: dict(v) for k, v in (merged.get("providers") or {}).items() if isinstance(v, dict)}
    if not provs:
        name = _provider_name(merged.get("base_url"))
        provs = {name: {"base_url": merged.get("base_url") or DEFAULTS["base_url"],
                        "api_key": merged.get("api_key") or "none",
                        "vision": bool((merged.get("capabilities") or {}).get("vision")),
                        "template_kwargs": True}}
        merged["provider"] = name
    for p in provs.values():
        p.setdefault("api_key", "none")
        p.setdefault("vision", False)
        p.setdefault("template_kwargs", True)  # sglang/vLLM chat_template_kwargs (thinking flags)
        p.setdefault("model", "")              # this provider's default model ("" = global model)
    if merged.get("provider") not in provs:
        merged["provider"] = next(iter(provs))
    return provs


def effective(s: dict, override: dict | None = None) -> dict:
    """Settings with the connection fields resolved for one run: the global
    default provider/model, or a thread's override {provider, model} (either
    may be empty = inherit). Everything that builds a model client reads
    base_url / api_key / model / capabilities.vision / template_kwargs."""
    ov = override or {}
    if ov.get("keep_reasoning") in KEEP_REASONING:
        s = {**s, "keep_reasoning": ov["keep_reasoning"]}
    provs = s.get("providers") or {}
    pname = ov.get("provider") if ov.get("provider") in provs else s.get("provider")
    p = provs.get(pname)
    if p is None:  # no providers at all (shouldn't happen after _backfill)
        return s
    if ov.get("model"):
        model = ov["model"]
    elif pname != s.get("provider"):
        model = p.get("model") or s.get("model")  # other provider: its own default model
    else:
        model = s.get("model")
    return {**s, "provider": pname, "base_url": p["base_url"], "api_key": p.get("api_key") or "none",
            "model": model, "template_kwargs": bool(p.get("template_kwargs", True)),
            "capabilities": {**(s.get("capabilities") or {}), "vision": bool(p.get("vision"))}}


# Per-thread run options (threads.opts JSON, agent.get_thread_opts). Absent
# key = inherit CONFIG. Applied after effective() so one settings dict drives
# build_agent, prefetch and the learning review alike.
#   bare      — no tools at all (system prompt + thread prompt kept); implies
#               memory/skills/prefetch off and the react graph (no harness)
#   tools     — {name: bool}; local + ⚙ background tools may be forced either
#               way, anything else (harness, MCP tools) can only be turned off
#   mcp       — {server: false} drops that server's tools for this thread
#               (restrict-only: mcp.get_tools keeps ONE cached client set)
#   memory / skills / compact / review / prefetch — bool feature switches
#   thinking  — the model's reasoning switch (enable_thinking) for this thread
#   ephemeral — throwaway: no learning by default, hidden from other threads'
#               auto-search hints, swept after EPHEMERAL_TTL idle
THREAD_OPT_FLAGS = ("memory", "skills", "compact", "review", "prefetch", "thinking")
EPHEMERAL_TTL = 24 * 3600


def clean_opts(o: dict | None) -> dict:
    """Normalize a client-sent opts dict; drops unknown keys and non-bools."""
    o = o or {}
    out: dict = {}
    for k in ("bare", "ephemeral", *THREAD_OPT_FLAGS):
        if isinstance(o.get(k), bool):
            out[k] = o[k]
    for k in ("tools", "mcp"):
        m = {str(n): v for n, v in (o.get(k) or {}).items() if isinstance(v, bool)}
        if k == "mcp":
            m = {n: v for n, v in m.items() if v is False}
        if m:
            out[k] = m
    return out


def apply_opts(s: dict, opts: dict | None) -> dict:
    """Settings for one thread's run with its opts layered on (see above)."""
    o = opts or {}
    if not o:
        return s
    eph = bool(o.get("ephemeral"))
    flag = lambda k, cur, eph_default=None: (  # noqa: E731
        o[k] if k in o else (eph_default if eph and eph_default is not None else cur))
    tools = o.get("tools") or {}
    s = {**s,
         "local_tools": {**(s.get("local_tools") or {}), **tools},
         "lb_tools_off": sorted(n for n, v in tools.items() if v is False),
         "lb_mcp_off": sorted(o.get("mcp") or {}),
         "lb_bare": bool(o.get("bare")),
         "memory_enabled": flag("memory", s.get("memory_enabled", True), False),
         "skills_enabled": flag("skills", s.get("skills_enabled", True)),
         "compact_enabled": flag("compact", s.get("compact_enabled", True)),
         "enable_thinking": flag("thinking", bool(s.get("enable_thinking"))),
         "skill_review": {**(s.get("skill_review") or {}),
                          "enabled": flag("review", (s.get("skill_review") or {}).get("enabled", True), False)},
         }
    rag = s.get("rag") or {}
    pf = rag.get("prefetch") or {}
    on = False if s["lb_bare"] else flag("prefetch", pf.get("enabled", True))
    s["rag"] = {**rag, "prefetch": {**pf, "enabled": on}}
    if s["lb_bare"]:
        s.update(memory_enabled=False, skills_enabled=False)
    return s


def _backfill(merged: dict) -> dict:
    # top-level merge can't introduce sub-keys added since a settings file
    # was written — CONFIG builds its LOCAL TOOLS checkboxes from this dict,
    # so old files must still see the new tools (default: enabled)
    merged["local_tools"] = {**DEFAULTS["local_tools"], **(merged.get("local_tools") or {})}
    # same for voice: new provider keys (pocket_*) must reach old files
    merged["voice"] = {**DEFAULTS["voice"], **(merged.get("voice") or {})}
    merged["notify"] = {**DEFAULTS["notify"], **(merged.get("notify") or {})}
    merged["image_gen"] = {**DEFAULTS["image_gen"], **(merged.get("image_gen") or {})}
    merged["rag"] = {**DEFAULTS["rag"], **(merged.get("rag") or {})}
    merged["decide"] = {**DEFAULTS["decide"], **(merged.get("decide") or {})}
    merged["ivr"] = {**DEFAULTS["ivr"], **(merged.get("ivr") or {})}
    merged["skill_review"] = {**DEFAULTS["skill_review"], **(merged.get("skill_review") or {})}
    merged["staleness"] = {**DEFAULTS["staleness"], **(merged.get("staleness") or {})}
    merged["providers"] = _providers(merged)
    # top-level base_url/api_key/capabilities mirror the DEFAULT provider, so
    # every older call site (health, titles, file assist…) keeps working
    return effective(merged)


def private(key: str, env: str = "", default: str = "") -> str:
    """Deployment-specific values that must stay out of the PUBLIC repo
    (business hostnames, AWS zone ids): env var first, then settings.json
    "private" (data/ is gitignored), then `default`."""
    return (env and os.environ.get(env)) or str((load().get("private") or {}).get(key) or default)


def load() -> dict:
    with _lock:
        try:
            with open(SETTINGS_PATH) as f:
                merged = {**DEFAULTS, **json.load(f)}
        except FileNotFoundError:
            merged = dict(DEFAULTS)
        return _backfill(merged)


def save(settings: dict) -> dict:
    """Write settings. Top-level keys not in `settings` keep their ON-DISK
    value (2026-10-14: a one-key save used to fall back to DEFAULTS for every
    other key and wiped providers, models, MCP servers and fleet hosts)."""
    with _lock:
        try:
            with open(SETTINGS_PATH) as f:
                disk = json.load(f)
        except (FileNotFoundError, ValueError):
            disk = {}
        merged = _backfill({**DEFAULTS, **disk, **settings})
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(merged, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)
        return merged

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
        # numbers/dates/times → words before synthesis: pocket | all | off
        "tts_normalize": "pocket",  # benchmarked: 2 = 4 = 8 on this i9 (~4.5x realtime)
    },
    # Image generation / editing: Qwen-Image 2.1 GGUF on ComfyUI (server/comfy.py)
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


def _backfill(merged: dict) -> dict:
    # top-level merge can't introduce sub-keys added since a settings file
    # was written — CONFIG builds its LOCAL TOOLS checkboxes from this dict,
    # so old files must still see the new tools (default: enabled)
    merged["local_tools"] = {**DEFAULTS["local_tools"], **(merged.get("local_tools") or {})}
    # same for voice: new provider keys (pocket_*) must reach old files
    merged["voice"] = {**DEFAULTS["voice"], **(merged.get("voice") or {})}
    merged["notify"] = {**DEFAULTS["notify"], **(merged.get("notify") or {})}
    merged["image_gen"] = {**DEFAULTS["image_gen"], **(merged.get("image_gen") or {})}
    merged["skill_review"] = {**DEFAULTS["skill_review"], **(merged.get("skill_review") or {})}
    merged["providers"] = _providers(merged)
    # top-level base_url/api_key/capabilities mirror the DEFAULT provider, so
    # every older call site (health, titles, file assist…) keeps working
    return effective(merged)


def load() -> dict:
    with _lock:
        try:
            with open(SETTINGS_PATH) as f:
                merged = {**DEFAULTS, **json.load(f)}
        except FileNotFoundError:
            merged = dict(DEFAULTS)
        return _backfill(merged)


def save(settings: dict) -> dict:
    with _lock:
        merged = _backfill({**DEFAULTS, **settings})
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(merged, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)
        return merged

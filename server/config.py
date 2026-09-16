"""Persistent app settings (data/settings.json) with sane defaults."""
import json
import os
import threading

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
DB_PATH = os.path.join(DATA_DIR, "langbang.db")

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
    # Off by default: Qwen3-style hybrids answer silently unless asked, and
    # thinking adds a few hundred decode tokens per turn (~40-50 ms each on
    # a single Spark). Enable in CONFIG to stream reasoning into the
    # ◈ THINKING cards (the topbar REASONING button only shows/hides cards).
    "enable_thinking": False,
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
    },
}


def load() -> dict:
    with _lock:
        try:
            with open(SETTINGS_PATH) as f:
                merged = {**DEFAULTS, **json.load(f)}
        except FileNotFoundError:
            merged = dict(DEFAULTS)
        return merged


def save(settings: dict) -> dict:
    with _lock:
        merged = {**DEFAULTS, **settings}
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(merged, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)
        return merged

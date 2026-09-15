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
    "system_prompt": "You are LangBang, a concise, capable agent. Use tools when they help.",
    "mcp_servers": {
        # User's hybrid session-history RAG on spark-ee93 (systemd unit: rag-mcp).
        "rag-mcp": {
            "transport": "streamable_http",
            "url": os.environ.get("LANGBANG_RAG_MCP_URL", "http://spark-ee93:8004/mcp"),
        },
    },
    "max_react_iterations": 12,
    # Model input modalities. sglang/llama.cpp can't tell us, so the user
    # declares them; the UI only offers image paste/attach when vision=true.
    # RadixArk/Qwen3.8-Flash-Next-NVFP4 verified vision-capable on the Spark
    # (1x1 pixel color tests) -> on by default. Images re-prefill every turn,
    # so keep attachments small/few.
    "capabilities": {"vision": True},
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

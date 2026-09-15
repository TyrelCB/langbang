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
    "mcp_servers": {},
    "max_react_iterations": 12,
    "local_tools": {
        "run_bash": True,
        "read_file": True,
        "write_file": True,
        "list_dir": True,
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

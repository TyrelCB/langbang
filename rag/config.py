"""Node settings: data/rag/node.json (override dir with LANGBANG_RAG_DIR).

Separate from LangBang's settings.json on purpose — a fleet node may run the
rag package with no LangBang app beside it.
"""
import copy
import json
import os
import socket
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.expanduser(os.environ.get("LANGBANG_RAG_DIR") or os.path.join(
    os.path.dirname(HERE), "data", "rag"))
KEYS_DIR = os.path.join(os.path.dirname(DATA_DIR), "keys")
PATH = os.path.join(DATA_DIR, "node.json")
HOME = os.path.expanduser("~")

DEFAULTS = {
    "node": socket.gethostname().split(".")[0],
    "host": "0.0.0.0",
    "port": 8900,
    "model": "google/embeddinggemma-2",
    # identity of the vector space; peers whose fingerprint differs are refused
    # (sha256 prefix of model.safetensors — identical on fedora + Atlas)
    "model_fp": "eg2-197a32965d4b",
    "threads": 8,
    # bulk index embedding: "local" or a peer name from `peers` (GPU offload)
    "index_backend": "local",
    "roots": [HOME],
    "prune_dirs": [".git", "node_modules", ".venv", "venv", "site-packages", "__pycache__",
                   ".gradle", ".pub-cache", ".bun", ".unsloth", ".rustup", ".cargo",
                   ".next", ".npm", ".nuget", ".dotnet", ".pki", ".mozilla", ".docker",
                   ".java", ".m2", ".android", ".steam", ".local-browsers"],
    "skip": [HOME + "/.hermes", HOME + "/projects/embeddinggemma-2", HOME + "/llama.cpp",
             HOME + "/flutter", HOME + "/opencode", HOME + "/python", HOME + "/.cache"],
    # dot-dirs are skipped by the walk; these are indexed anyway
    "include_hidden": [HOME + "/.langbang/skills", HOME + "/.langbang/memory"],
    # path prefix -> source tag (longest prefix wins); everything else = home
    "sources": {
        HOME + "/Pictures/langbang": "output",
        HOME + "/projects/LangBang/data/uploads": "upload",
        HOME + "/.langbang/skills": "skill",
        HOME + "/.langbang/memory": "memory",
    },
    "workers": 4,
    "rescan_window": "01:00-08:00",  # timer rescans index only then; "" = any time
    "video_keyframes": 8,
    "peers": [],
    # fan-out budget per peer, seconds
    "peer_timeout": 3.0,
    "replicate_interval": 600,
}

_lock = threading.Lock()


def load():
    s = copy.deepcopy(DEFAULTS)
    try:
        with open(PATH) as f:
            s.update(json.load(f))
    except FileNotFoundError:
        pass
    return s


def save(patch):
    with _lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        cur = {}
        try:
            with open(PATH) as f:
                cur = json.load(f)
        except FileNotFoundError:
            pass
        cur.update(patch)
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cur, f, indent=2)
        os.replace(tmp, PATH)
    return load()


def read_key(name):
    """Secrets live only in data/keys/ (repo .gitignore'd)."""
    try:
        with open(os.path.join(KEYS_DIR, name)) as f:
            return f.read().strip()
    except OSError:
        return ""


def source_for(path, s):
    best, tag = -1, "home"
    for pre, t in s.get("sources", {}).items():
        if (path == pre or path.startswith(pre.rstrip("/") + "/")) and len(pre) > best:
            best, tag = len(pre), t
    return tag

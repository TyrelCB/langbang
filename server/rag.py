"""Client for the LangBang RAG node (rag/ package, langbang-rag.service :8900).

Ingest pushes are fire-and-forget on one background thread: a dead or slow
node must never delay or fail a chat run, an upload, or a memory write.
Search is the agent's semantic_search tool (local_tools.py).
"""
import concurrent.futures
import json
import logging
import os
import re
import urllib.parse
import urllib.request

from . import config

logger = logging.getLogger(__name__)
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag-push")
HOME = os.path.expanduser("~")
# files worth indexing (mirrors rag.indexer.category_for, without importing torch-land)
EXTS = re.compile(r"\.(py|js|ts|tsx|jsx|dart|java|kt|cs|cpp|cc|c|go|rs|rb|sh|html|css|scss|swift|md|txt|"
                  r"rst|csv|json|yaml|yml|toml|xml|pdf|docx|png|jpe?g|webp|gif|mp3|wav|m4a|flac|ogg|aac|"
                  r"mp4|mkv|mov|webm|avi)$", re.I)


def settings(s=None):
    s = s or config.load()
    r = {"enabled": True, "url": "http://127.0.0.1:8900", "auto_ingest": True, "index_threads": True,
         "scope": "fleet"}
    r.update(s.get("rag") or {})
    return r


def _req(method, path, body=None, timeout=10, s=None):
    r = settings(s)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(r["url"].rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _bg(method, path, body=None):
    def run():
        try:
            _req(method, path, body)
        except Exception as e:  # noqa: BLE001 - background push: log, never raise
            logger.warning("rag %s %s failed: %s: %s", method, path.split("?")[0], type(e).__name__, e)
    _pool.submit(run)


def _on(s=None):
    r = settings(s)
    return r["enabled"] and r["auto_ingest"]


def push_paths(paths, s=None):
    paths = [p for p in dict.fromkeys(paths or []) if p and EXTS.search(p)]
    if paths and _on(s):
        _bg("POST", "/index", {"paths": paths})


def push_docs(docs, s=None):
    if docs and _on(s):
        _bg("POST", "/index", {"docs": docs})


def drop(s=None, **q):
    if settings(s)["enabled"]:
        _bg("DELETE", "/index?" + urllib.parse.urlencode({k: v for k, v in q.items() if v}))


def drop_thread(tid):
    drop(tid=tid)


def search(query, *, category="", source="", scope="", top_k=8, image_path="", path_prefix="",
           timeout=20):
    r = settings()
    body = {"top_k": int(top_k), "scope": scope or r["scope"], "category": category or None,
            "source": source or None, "path_prefix": path_prefix or None}
    if image_path:
        body["image_path"] = os.path.abspath(os.path.expanduser(image_path))
    else:
        body["query"] = query
    return _req("POST", "/search", body, timeout=timeout)


def health(timeout=3):
    return _req("GET", "/healthz", timeout=timeout)


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return str(content or "")


def _written_paths(events):
    """Files this run wrote via the file tools. generate_image outputs push
    from comfy.generate itself; run_bash output is NOT mined (ls/find dumps
    would index half the disk)."""
    out = []
    for ev in events:
        t, name = ev.get("type"), ev.get("name")
        if t == "tool_start" and name in ("write_file", "edit_file"):
            inp = ev.get("input") or {}
            if isinstance(inp, dict):
                out.append(inp.get("file_path") or inp.get("path") or "")
    res = []
    for p in out:
        p = os.path.abspath(os.path.expanduser(str(p).rstrip(".,;:")))
        if p.startswith(HOME + "/") and os.path.isfile(p):
            res.append(p)
    return res


async def after_run(tid, events):
    """runs._produce hook: index what the run wrote + the exchange itself."""
    try:
        s = config.load()
        if not _on(s):
            return
        push_paths(_written_paths(events), s)
        if not settings(s)["index_threads"]:
            return
        if any(ev.get("type") in ("error", "shell_result") for ev in events):
            return  # failed/cancelled runs and !cmd shells aren't conversation
        from langchain_core.messages import AIMessage
        from . import agent, learning
        msgs = await agent._live_messages(tid)
        turn, human = learning._turn(msgs)
        if human is None:
            return
        answer = ""
        for m in reversed(turn):
            if isinstance(m, AIMessage) and not m.tool_calls and _text(m.content).strip():
                answer = _text(m.content).strip()
                break
        q = _text(human.content).strip()
        if not answer or not q:
            return
        title = ""
        try:
            row = await (await agent._db.execute("SELECT title FROM threads WHERE id=?", (tid,))).fetchone()
            title = (row[0] if row else "") or ""
        except Exception:  # noqa: BLE001
            pass
        turn_id = human.id or str(len(msgs))
        push_docs([{"path": f"lb-thread:{tid}/{turn_id}", "source": "thread",
                    "text": (f"THREAD: {title}\n" if title else "") + f"USER: {q[:6000]}\n\nLANGBANG: {answer[:20000]}",
                    "meta": {"tid": tid, "turn": turn_id, "title": title}}], s)
    except Exception as e:  # noqa: BLE001 - indexing must never disturb runs
        logger.warning("rag after_run %s: %s: %s", tid, type(e).__name__, e)

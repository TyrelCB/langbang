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


def _exchange(tid, title, human, turn):
    """One finished exchange as a RAG doc (None = nothing to index): the
    user's message + LangBang's final answer, id'd by the HumanMessage."""
    from langchain_core.messages import AIMessage
    answer = ""
    for m in reversed(turn):
        if isinstance(m, AIMessage) and not m.tool_calls and _text(m.content).strip():
            answer = _text(m.content).strip()
            break
    q = _text(human.content).strip()
    if not answer or not q or not human.id:
        return None
    return {"path": f"lb-thread:{tid}/{human.id}", "source": "thread",
            "text": (f"THREAD: {title}\n" if title else "") + f"USER: {q[:6000]}\n\nLANGBANG: {answer[:20000]}",
            "meta": {"tid": tid, "turn": human.id, "title": title}}


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
        from . import agent, learning
        if tid in await agent.ephemeral_tids():
            return  # ⌛ temp threads are throwaway tests: keep them out of search
        msgs = await agent._live_messages(tid)
        turn, human = learning._turn(msgs)
        if human is None:
            return
        title = ""
        try:
            row = await (await agent._db.execute("SELECT title FROM threads WHERE id=?", (tid,))).fetchone()
            title = (row[0] if row else "") or ""
        except Exception:  # noqa: BLE001
            pass
        doc = _exchange(tid, title, human, turn)
        if doc:
            push_docs([doc], s)
    except Exception as e:  # noqa: BLE001 - indexing must never disturb runs
        logger.warning("rag after_run %s: %s: %s", tid, type(e).__name__, e)


BACKFILL_V = "1"


async def backfill_threads(batch=8, pause=3.0):
    """Index every past exchange the node doesn't have yet (threads older
    than index_threads, or turns a down node missed), so ⌕ chat search's
    semantic half covers all threads. Throttled: `batch` docs per call,
    waiting for each to embed, then `pause` s — the embeddings run on the
    shared index backend (Atlas GPU), which bulk work slows for everyone.
    → docs pushed; raises if the node fails (rerun resumes: ids already on
    the node are skipped)."""
    import asyncio
    s = config.load()
    if not (_on(s) and settings(s)["index_threads"]):
        return 0
    from . import agent, runs
    try:
        have = set((await asyncio.to_thread(_req, "GET", "/index/paths?source=thread")).get("paths") or [])
    except Exception as e:  # node down / older node: the caller retries next start
        raise RuntimeError(f"can't list the node's indexed threads: {e}") from e
    eph = await agent.ephemeral_tids()
    rows = await (await agent._db.execute("SELECT id, title FROM threads")).fetchall()
    todo = []
    for tid, title in rows:
        if tid in eph or runs.get(tid):
            continue
        try:
            msgs = await agent._live_messages(tid)
        except Exception as e:  # noqa: BLE001
            logger.warning("rag backfill %s: %s", tid, e)
            continue
        starts = [i for i, m in enumerate(msgs) if agent._turn_start(m)]
        for j, i in enumerate(starts):
            doc = _exchange(tid, title or "", msgs[i], msgs[i:starts[j + 1] if j + 1 < len(starts) else None])
            if doc and doc["path"] not in have:
                todo.append(doc)
    logger.warning("rag backfill: %d exchanges to index", len(todo))
    n = 0
    for k in range(0, len(todo), batch):
        try:
            await asyncio.to_thread(_req, "POST", "/index", {"docs": todo[k:k + batch], "wait": True}, 900)
        except Exception as e:  # rerun skips what landed
            raise RuntimeError(f"stopped at {n}/{len(todo)}: {e}") from e
        n += len(todo[k:k + batch])
        await asyncio.sleep(pause)
    logger.warning("rag backfill: done, %d exchanges", n)
    return n

"""⌕ Chat search index: flattened transcripts in their own SQLite file.

The old /api/search re-read every thread's full history() (checkpoint
deserialization + archive replay) on every keystroke-debounced query —
3.2 s idle for 200 threads / 80 M chars, far worse while runs held the event
loop. Now each thread's searchable text lives in data/search.db, keyed by
its newest checkpoint_id: a query re-flattens only threads that changed
since the last one, then the substring scan is one SQLite LIKE (C speed).

Rows keep `pos` = the rendered-message index the UI's search-jump scrolls
to (same skip rule as renderHistory). Own file + own connection: a big
reindex never holds the main DB's WAL writer lock.
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiosqlite

from . import config

logger = logging.getLogger("langbang.searchidx")

PATH = os.path.join(config.DATA_DIR, "search.db")
MSG_CAP = 60_000  # chars per message kept searchable

_c: aiosqlite.Connection | None = None
_lock = asyncio.Lock()  # one refresher at a time (concurrent searches share it)


async def _conn() -> aiosqlite.Connection:
    global _c
    if _c is None:
        c = await aiosqlite.connect(PATH)
        await c.execute("PRAGMA busy_timeout=30000")
        await c.execute("PRAGMA journal_mode=WAL")
        await c.executescript(
            """
            CREATE TABLE IF NOT EXISTS docs(tid TEXT NOT NULL, pos INTEGER NOT NULL,
              role TEXT NOT NULL, text TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_docs_tid ON docs(tid, pos);
            CREATE TABLE IF NOT EXISTS meta(tid TEXT PRIMARY KEY, key TEXT);
            """)
        await c.commit()
        _c = c
    return _c


async def close() -> None:
    global _c
    if _c is not None:
        await _c.close()
        _c = None


async def _rows(tid: str) -> list[tuple[int, str, str]]:
    from . import agent
    out, pos = [], -1
    for m in await agent.history(tid):
        if m["role"] == "system":
            continue
        if (m["role"] == "ai" and not str(m.get("content") or "").strip()
                and not m.get("tool_calls") and not m.get("thinking")):
            continue  # renderHistory draws no node for these — keep pos aligned
        pos += 1  # counts rendered msgs only == client chat DOM child index
        text = (await agent.search_text(m))[:MSG_CAP]
        if text.strip():
            out.append((pos, m["role"], text))
    return out


async def refresh() -> int:
    """Re-flatten threads whose newest checkpoint moved; drop deleted ones.
    → number of threads reindexed."""
    from . import agent
    async with _lock:
        c = await _conn()
        cur = await agent._db.execute(
            "SELECT t.id, (SELECT MAX(checkpoint_id) FROM checkpoints k "
            " WHERE k.thread_id=t.id AND k.checkpoint_ns='') FROM threads t")
        want = {r[0]: r[1] or "" for r in await cur.fetchall()}
        have = dict(await (await c.execute("SELECT tid, key FROM meta")).fetchall())
        n = 0
        for tid in [t for t in have if t not in want]:
            await c.execute("DELETE FROM docs WHERE tid=?", (tid,))
            await c.execute("DELETE FROM meta WHERE tid=?", (tid,))
        for tid, key in want.items():
            if have.get(tid) == key:
                continue
            try:
                rows = await _rows(tid)
            except Exception as e:  # noqa: BLE001 - one bad thread never blocks search
                logger.warning("searchidx: %s: %r", tid, e)
                continue
            await c.execute("DELETE FROM docs WHERE tid=?", (tid,))
            await c.executemany("INSERT INTO docs(tid,pos,role,text) VALUES(?,?,?,?)",
                                [(tid, p, r, t) for p, r, t in rows])
            await c.execute("INSERT OR REPLACE INTO meta(tid,key) VALUES(?,?)", (tid, key))
            await c.commit()
            n += 1
        await c.commit()
        return n


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def query(q: str, tids: list[str], per_thread: int) -> dict[str, list[tuple[int, str, str]]]:
    """{tid: [(pos, role, text)…]} for threads in `tids` containing `q`
    (case-insensitive for ASCII, like the old scan for every query that
    matters), first `per_thread` hits per thread in transcript order."""
    if not tids:
        return {}
    await refresh()
    c = await _conn()
    out: dict[str, list] = {}
    for i in range(0, len(tids), 400):
        chunk = tids[i:i + 400]
        ph = ",".join("?" * len(chunk))
        cur = await c.execute(
            f"SELECT tid,pos,role,text FROM (SELECT tid,pos,role,text, ROW_NUMBER() OVER "
            f"(PARTITION BY tid ORDER BY pos) AS rn FROM docs WHERE tid IN ({ph}) "
            f"AND text LIKE ? ESCAPE '\\') WHERE rn<=?",
            (*chunk, _like(q), per_thread))
        for tid, pos, role, text in await cur.fetchall():
            out.setdefault(tid, []).append((pos, role, text))
    for v in out.values():
        v.sort()
    return out

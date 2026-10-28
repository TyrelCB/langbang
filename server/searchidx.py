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

Ranked search (rank()): an FTS5 index over the same rows (BM25, any query
word, prefix-matched, stopwords dropped) so "the automated browser thread"
works like a search engine, not one literal substring. main.py fuses it
with the RAG node's semantic hits over indexed exchanges.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

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
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
              text, content='docs', content_rowid='rowid', tokenize='unicode61');
            CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
              INSERT INTO docs_fts(rowid, text) VALUES (new.rowid, new.text); END;
            CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
              INSERT INTO docs_fts(docs_fts, rowid, text) VALUES ('delete', old.rowid, old.text); END;
            """)
        row = await (await c.execute("SELECT v FROM kv WHERE k='fts'")).fetchone()
        if not row:  # rows that predate the FTS table
            logger.warning("searchidx: building FTS index")
            await c.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
            await c.execute("INSERT OR REPLACE INTO kv VALUES('fts','1')")
        await c.commit()
        _c = c
    return _c


async def kv_get(k: str) -> str | None:
    row = await (await (await _conn()).execute("SELECT v FROM kv WHERE k=?", (k,))).fetchone()
    return row[0] if row else None


async def kv_set(k: str, v: str) -> None:
    c = await _conn()
    await c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, v))
    await c.commit()


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


STOP = set("""a an and are as at be but by can do does did for from had has have how i if in
into is it its just like me my of on or our so some than that the their them then there these
this to was we were what when where which who why will with you your thread threads chat chats
conversation looking look find found search searching about session one ones did done get got
want wanted need remember earlier last before ago was were used use using""".split())
TOKEN_RE = re.compile(r"[\w][\w.+-]*\w|\w", re.UNICODE)
ROLE_W = {"human": 1.6, "ai": 1.0, "tool": 0.45}  # what you asked > what it said > tool dumps


def terms(q: str) -> list[str]:
    """Query words worth matching: lowercased, stopwords dropped (all-stopword
    queries keep their words), FTS-safe (quoted per term below)."""
    ws = [w for w in TOKEN_RE.findall(q.lower()) if len(w) > 1 or w.isdigit()]
    keep = [w for w in ws if w not in STOP]
    return list(dict.fromkeys(keep or ws))[:12]


async def rank(q: str, tids: list[str], limit: int = 400) -> list[dict]:
    """Threads ranked by BM25 over their messages: [{tid, score, cover,
    pos, role, text}] best first. score = best row (role-weighted) plus a
    bonus per extra distinct query word the thread covers anywhere."""
    ts = terms(q)
    if not ts or not tids:
        return []
    await refresh()
    c = await _conn()
    match = " OR ".join(f'"{t}"*' for t in ts)
    keep = set(tids)
    try:
        cur = await c.execute(
            "SELECT d.tid, d.pos, d.role, d.text, bm25(docs_fts) FROM docs_fts "
            "JOIN docs d ON d.rowid = docs_fts.rowid WHERE docs_fts MATCH ? "
            "ORDER BY bm25(docs_fts) LIMIT ?", (match, limit * 4))
        rows = await cur.fetchall()
    except aiosqlite.OperationalError as e:  # odd input the tokenizer rejects
        logger.warning("searchidx rank %r: %s", q, e)
        return []
    ql = q.strip().lower()
    by: dict[str, dict] = {}
    for tid, pos, role, text, bm in rows:
        if tid not in keep:
            continue
        low = text.lower()
        sc = -bm * ROLE_W.get(role, 1.0) * (2.0 if len(ql) > 3 and ql in low else 1.0)
        t = by.setdefault(tid, {"tid": tid, "best": 0.0, "words": set(), "hits": 0})
        t["hits"] += 1
        t["words"].update(w for w in ts if w in low)
        if sc > t["best"]:
            t.update(best=sc, pos=pos, role=role, text=text)
    out = []
    for t in by.values():
        cover = len(t.pop("words"))
        t["cover"] = cover
        t["score"] = t.pop("best") * (1 + 0.6 * (cover - 1))
        out.append(t)
    out.sort(key=lambda t: -t["score"])
    return out[:limit]


async def pos_of(tid: str, user_text: str) -> int:
    """Rendered position of the user message that starts with `user_text`
    (a semantic hit names its exchange by the user's words), -1 if gone."""
    head = " ".join(user_text.split())[:80]
    if not head:
        return -1
    c = await _conn()
    cur = await c.execute("SELECT pos, text FROM docs WHERE tid=? AND role='human' ORDER BY pos", (tid,))
    for pos, text in await cur.fetchall():
        if " ".join(text.split()).startswith(head):
            return pos
    return -1

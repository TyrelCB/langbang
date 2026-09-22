"""Scheduled tasks: cron-driven agent turns that run without a client.

Each schedule owns a dedicated thread (created at task-creation time): every
firing posts the task's prompt there as a real user turn, so run history
accumulates in one place and the agent's own context carries over between
runs. run_chat() persists messages, trajectory rows and the sidebar bump on
its own — this module only decides WHEN to run it and keeps bookkeeping
(next_run, running, last_run) honest in SQLite.

Missed fires (server was down at fire time) are skipped, never replayed:
startup recomputes next_run from now.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from croniter import croniter

from . import agent, config

logger = logging.getLogger("langbang.schedule")

_db = None  # agent._db, bound in init() after agent.init()
_lock = asyncio.Lock()  # one scheduled run at a time (shared Spark serializes)


# ---- schema + loop lifecycle ----

async def init() -> None:
    global _db
    _db = agent._db
    await _db.executescript(
        """
        CREATE TABLE IF NOT EXISTS schedules(
          id TEXT PRIMARY KEY, title TEXT, prompt TEXT, cron TEXT,
          thread_id TEXT, enabled INTEGER DEFAULT 1,
          last_run REAL, next_run REAL, running INTEGER DEFAULT 0,
          created_at REAL);
        """
    )
    # fresh next_run for every enabled task: anything missed while the
    # server was down is skipped, not replayed
    now = time.time()
    cur = await _db.execute("SELECT id, cron FROM schedules WHERE enabled=1")
    for rid, cron in await cur.fetchall():
        await _db.execute("UPDATE schedules SET next_run=? WHERE id=?",
                          (_next_run(cron, now), rid))
    await _db.commit()


def start_loop() -> None:
    asyncio.get_running_loop().create_task(_loop())


async def _loop() -> None:
    while True:
        try:
            now = time.time()
            cur = await _db.execute(
                "SELECT id, title, prompt, cron, thread_id, next_run FROM schedules"
                " WHERE enabled=1 AND running=0 AND next_run<=? ORDER BY next_run",
                (now,),
            )
            for row in await cur.fetchall():
                await _fire(dict(zip(
                    ("id", "title", "prompt", "cron", "thread_id", "next_run"), row)))
            cur = await _db.execute(
                "SELECT MIN(next_run) FROM schedules WHERE enabled=1")
            nxt = (await cur.fetchone())[0]
            # long sleeps would sleep past a just-created schedule; clamp to
            # 30s so the CRUD endpoints take effect promptly anyway
            await asyncio.sleep(
                30.0 if nxt is None else min(30.0, max(1.0, nxt - time.time())))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must outlive any task
            logger.exception("schedule loop tick failed")
            await asyncio.sleep(5.0)


# ---- firing ----

def _next_run(cron: str, frm: float | None = None) -> float:
    return croniter(cron, frm or time.time()).get_next(float)


async def _fire(row: dict, manual: bool = False) -> None:
    """Drain run_chat as a headless consumer. run_chat logs its own errors
    into the thread trajectory; the try/except only protects the loop.
    The lock spans the whole run: cron fires and run-now clicks serialize."""
    rid = row["id"]
    async with _lock:
        cur = await _db.execute("SELECT running FROM schedules WHERE id=?", (rid,))
        r = await cur.fetchone()
        if not r or r[0]:
            return  # already in flight (loop vs run-now race)
        await _db.execute("UPDATE schedules SET running=1 WHERE id=?", (rid,))
        await _db.commit()
        logger.info("schedule %r firing in thread %s", row["title"], row["thread_id"])
        try:
            s = config.load()  # live config per run, same contract as /api/chat
            async for _ev in agent.run_chat(row["thread_id"], row["prompt"], s):
                pass
        except Exception:  # noqa: BLE001
            logger.exception("schedule %r run crashed", row["title"])
        finally:
            # a manual run must not shift the cron rhythm
            nxt = row["next_run"] if manual else _next_run(row["cron"])
            await _db.execute(
                "UPDATE schedules SET running=0, last_run=?, next_run=? WHERE id=?",
                (time.time(), nxt, rid))
            await _db.commit()


# ---- CRUD (used by main.py routes) ----

_COLS = ("id", "title", "prompt", "cron", "thread_id", "enabled",
         "last_run", "next_run", "running", "created_at")


def _rows(fetched) -> list[dict]:
    return [dict(zip(_COLS, r)) for r in fetched]


async def list_schedules() -> list[dict]:
    cur = await _db.execute(
        f"SELECT {', '.join(_COLS)} FROM schedules ORDER BY created_at DESC")
    return _rows(await cur.fetchall())


async def get(sid: str) -> dict | None:
    cur = await _db.execute(f"SELECT {', '.join(_COLS)} FROM schedules WHERE id=?",
                            (sid,))
    r = await cur.fetchone()
    return dict(zip(_COLS, r)) if r else None


async def create(title: str, prompt: str, cron: str) -> dict:
    if not croniter.is_valid(cron):
        raise ValueError(f"invalid cron expression: {cron!r}")
    sid = uuid.uuid4().hex[:12]
    now = time.time()
    thread = await agent.create_thread(title)  # the task's run-history notebook
    # column order: id,title,prompt,cron,thread_id,enabled,last_run,next_run,running,created_at
    await _db.execute(
        "INSERT INTO schedules VALUES(?,?,?,?,?,1,NULL,?,0,?)",
        (sid, title, prompt, cron, thread["id"], _next_run(cron, now), now))
    await _db.commit()
    return await get(sid)


async def update(sid: str, patch: dict) -> dict | None:
    cur = await _db.execute("SELECT cron FROM schedules WHERE id=?", (sid,))
    r = await cur.fetchone()
    if not r:
        return None
    cols, vals = [], []
    for k in ("title", "prompt", "cron"):
        if patch.get(k) is not None:
            if k == "cron" and not croniter.is_valid(patch[k]):
                raise ValueError(f"invalid cron expression: {patch[k]!r}")
            cols.append(f"{k}=?")
            vals.append(patch[k])
    if patch.get("cron") is not None:  # re-sync the timer to the new cadence
        cols.append("next_run=?")
        vals.append(_next_run(patch["cron"]))
    if patch.get("enabled") is not None:
        cols.append("enabled=?")
        vals.append(1 if patch["enabled"] else 0)
        if patch["enabled"]:
            cols.append("next_run=?")
            vals.append(_next_run(r[0]))  # waking up restarts from now
    if cols:
        vals.append(sid)
        await _db.execute(f"UPDATE schedules SET {', '.join(cols)} WHERE id=?", vals)
        await _db.commit()
    return await get(sid)


async def delete(sid: str) -> bool:
    cur = await _db.execute("SELECT thread_id FROM schedules WHERE id=?", (sid,))
    r = await cur.fetchone()
    if not r:
        return False
    await _db.execute("DELETE FROM schedules WHERE id=?", (sid,))
    await _db.commit()
    await agent.delete_thread(r[0])  # the notebook belongs to the task
    return True


async def run_now(sid: str) -> bool:
    row = await get(sid)
    if not row:
        return False
    asyncio.get_running_loop().create_task(_fire(row, manual=True))
    return True


# ---- display helpers (cron → human, for editor preview + panel rows) ----

_DAYS = {str(i): d for i, d in enumerate(
    ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"])}


def humanize(cron: str) -> str:
    """Common shapes only — anything exotic falls back to the raw string."""
    m, H, dom, mon, dow = (p.strip() for p in cron.split())
    try:
        hm = f"{int(H):02d}:{int(m):02d}"
    except ValueError:
        hm = None
    if cron == "* * * * *":
        return "every minute"
    if m.startswith("*/") and H == "*" and dom == "*" and dow == "*":
        return f"every {m[2:]} min"
    if hm and H == "*" and dom == "*" and dow == "*":
        return f"hourly at :{m.zfill(2)}"
    if hm and dom == "*" and mon == "*" and dow == "1-5":
        return f"weekdays at {hm}"
    if hm and dom == "*" and mon == "*" and dow == "*":
        return f"every day at {hm}"
    if hm and dom == "*" and mon == "*" and dow in _DAYS:
        return f"weekly on {_DAYS[dow]} at {hm}"
    if hm and dow == "*" and mon == "*" and dom.isdigit():
        return f"monthly on day {dom} at {hm}"
    return cron


def preview(cron: str) -> dict:
    """Live editor check: {ok, human, next} or {ok: False, error}."""
    if not croniter.is_valid(cron):
        return {"ok": False, "error": f"invalid cron expression: {cron!r}"}
    return {"ok": True, "human": humanize(cron), "next": _next_run(cron)}

"""Scheduled tasks: cron-driven agent turns that run without a client.

Each schedule owns a dedicated thread (created at task-creation time): every
firing posts the task's prompt there as a real user turn, so run history
accumulates in one place and the agent's own context carries over between
runs. run_chat() persists messages, trajectory rows and the sidebar bump on
its own — this module only decides WHEN to run it and keeps bookkeeping
(next_run, running, last_run) honest in SQLite.

Missed fires (server was down at fire time) are skipped, never replayed:
startup recomputes next_run from now.

One-off tasks (cron = '', run_at = epoch): fire once at run_at, then stay
in the list as DONE (enabled=0, done_at set) and age out of the table after
ONEOFF_KEEP_S — their thread (the result) stays in the sidebar. A one-off
missed while the server was down fires LATE on startup instead of being
skipped (skipping would mean it never runs at all).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime

from croniter import croniter

from . import agent, config, runs

logger = logging.getLogger("langbang.schedule")

_db = None  # agent._db, bound in init() after agent.init()
ONEOFF_KEEP_S = 24 * 3600  # a fired one-off stays visible (✓ DONE) this long
# Scheduled runs in flight at once. Was a Lock held across the WHOLE run
# (and awaited by the loop): one long run froze every other task — on
# 2026-10-04 Home Lab Health (hourly) fired 28-33 min late each hour and
# Image Gen (*/15) fired once in 6 h, all queued single file behind a
# Check Email run. 2 leaves sglang headroom (~4 streams) for live chat;
# runs.MAX_ACTIVE still caps the total.
MAX_SCHED = 2
_slots = asyncio.Semaphore(MAX_SCHED)
RETRY_S = 30.0  # runs.TooMany: retry the slot shortly instead of losing it


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
    cur = await _db.execute("PRAGMA table_info(schedules)")
    have = {r[1] for r in await cur.fetchall()}
    for col in ("run_at", "done_at"):  # one-off tasks (added 2026-10-02)
        if col not in have:
            await _db.execute(f"ALTER TABLE schedules ADD COLUMN {col} REAL")
    await _db.commit()
    # Stale bookkeeping from a dead process: a run that died mid-flight (or
    # whose finally-block UPDATE itself hit "database is locked") leaves
    # running=1 in the DB forever — the UI shows "RUNNING" hours after the
    # run is gone, and the due-scan below filters the task out of every tick.
    # After a process restart any running=1 is stale by definition.
    cur = await _db.execute("SELECT title FROM schedules WHERE running=1")
    stale = [r[0] for r in await cur.fetchall()]
    if stale:
        await _db.execute("UPDATE schedules SET running=0 WHERE running=1")
        await _db.commit()
        logger.warning("cleared stale running flags on startup: %s", stale)
    # fresh next_run for every enabled task: anything missed while the
    # server was down is skipped, not replayed
    now = time.time()
    cur = await _db.execute("SELECT id, cron, run_at FROM schedules WHERE enabled=1")
    for rid, cron, run_at in await cur.fetchall():
        await _db.execute("UPDATE schedules SET next_run=? WHERE id=?",
                          (_due(cron, run_at, now), rid))
    await _db.commit()


def start_loop() -> None:
    asyncio.get_running_loop().create_task(_loop())


async def _loop() -> None:
    while True:
        try:
            now = time.time()
            cur = await _db.execute(
                "SELECT id, title, prompt, cron, thread_id, next_run, run_at FROM schedules"
                " WHERE enabled=1 AND running=0 AND next_run<=? ORDER BY next_run",
                (now,),
            )
            for row in await cur.fetchall():
                row = dict(zip(
                    ("id", "title", "prompt", "cron", "thread_id", "next_run", "run_at"), row))
                # claim here (running=1) so the next tick can't double-fire;
                # the run itself is detached — the loop never waits on it
                if await _set("UPDATE schedules SET running=1 WHERE id=? AND running=0",
                              (row["id"],)):
                    agent._spawn(_fire(row, claimed=True))
            # fired one-offs age out of the list (their thread stays)
            await _set("DELETE FROM schedules WHERE done_at IS NOT NULL AND done_at<? AND running=0",
                       (time.time() - ONEOFF_KEEP_S,))
            cur = await _db.execute(
                "SELECT MIN(next_run) FROM schedules WHERE enabled=1 AND running=0")
            nxt = (await cur.fetchone())[0]
            # long sleeps would sleep past a just-created schedule; clamp to
            # 30s so the CRUD endpoints take effect promptly anyway
            await asyncio.sleep(
                30.0 if nxt is None else min(30.0, max(1.0, nxt - time.time())))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must outlive any task
            logger.exception("schedule loop tick failed")
            # a failed statement leaves its write txn (and the WAL writer
            # lock) OPEN on this connection — clear it or the next tick
            # inherits the poison, forever (see _set's docstring)
            try:
                await _db.rollback()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(5.0)


# ---- firing ----

def _due(cron: str, run_at: float | None, frm: float | None = None) -> float | None:
    """When a task should next fire: cron cadence, or its one-off time
    (a past run_at stays past → fires on the next tick, i.e. late)."""
    return run_at if not cron else _next_run(cron, frm)


def _next_run(cron: str, frm: float | None = None) -> float:
    # croniter matches fields against the clock of whatever base it's given:
    # a float epoch is read as UTC, so '0 9 * * *' would fire 09:00 UTC, not
    # 09:00 local (crontab semantics are local wall-clock). Hand it naive
    # local datetimes in and out; .timestamp() converts back to epoch.
    base = datetime.fromtimestamp(frm) if frm else datetime.now()
    return croniter(cron, base).get_next(datetime).timestamp()


async def _set(sql: str, args: tuple = (), tries: int = 5) -> int:
    """Bookkeeping write that SURVIVES transient 'database is locked'.

    The incident this guards: a failed write in aiosqlite does NOT auto-
    rollback — the connection keeps its half-open write transaction and its
    WAL write lock, so every later writer on any connection times out too
    (cascade). Roll back first to release any such poison, then retry. The
    finally-block flag-clear MUST land; if it doesn't, the UI shows a
    phantom RUNNING forever and the due-scan skips the task permanently."""
    last: Exception | None = None
    delay = 0.5
    for _ in range(tries):
        try:
            cur = await _db.execute(sql, args)
            await _db.commit()
            return cur.rowcount
        except Exception as e:  # noqa: BLE001
            last = e
            try:
                await _db.rollback()  # drop the failed txn, release its locks
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(delay)
            delay *= 2
    raise RuntimeError(f"schedule bookkeeping write failed: {last}") from last


async def _fire(row: dict, manual: bool = False, claimed: bool = False) -> None:
    """Drain run_chat as a headless consumer. run_chat logs its own errors
    into the thread trajectory; the try/except only protects the loop.
    At most MAX_SCHED of these run at once; extra due tasks wait for a slot
    (showing RUNNING — they're claimed) instead of blocking the loop."""
    rid = row["id"]
    # conditional single write (no read-then-write window); rowcount 0
    # means already in flight (loop vs run-now race)
    if not claimed and not await _set(
            "UPDATE schedules SET running=1 WHERE id=? AND running=0", (rid,)):
        return
    async with _slots:
        logger.info("schedule %r firing in thread %s", row["title"], row["thread_id"])
        deferred = False
        try:
            s = config.load()  # live config per run, same contract as /api/chat
            # stamp the run so its thread bubble shows WHEN (a schedule's
            # thread accumulates many runs; this says which output is which).
            # Server-owned hub task: the UI can watch it live (/api/runs)
            # and even STOP it; a dead viewer never kills it.
            hub = runs.start(
                row["thread_id"],
                agent.run_chat(
                    row["thread_id"], row["prompt"], s,
                    sched={"ts": time.time(), "manual": bool(manual)},
                ),
            )
            await hub.task  # _produce swallows run errors into the stream;
            # CancelledError (a user STOP on this run) propagates — the
            # finally below must still clear the flag (see _clear_flag)
        except runs.Busy:
            logger.warning("schedule %r skipped — thread %s already running",
                           row["title"], row["thread_id"])
        except runs.TooMany:
            logger.warning("schedule %r deferred %ds — run cap reached", row["title"], RETRY_S)
            deferred = True
        except Exception:  # noqa: BLE001
            logger.exception("schedule %r run crashed", row["title"])
        finally:
            # a manual run must not shift the cron rhythm
            if deferred:  # never started: same slot again shortly
                _clear_flag(rid, None, time.time() + RETRY_S, keep_last=True)
            elif manual:
                _clear_flag(rid, time.time(), row["next_run"])
            elif not row["cron"]:
                _clear_flag(rid, time.time(), None, done=True)  # one-off: fired → DONE
            else:
                _clear_flag(rid, time.time(), _next_run(row["cron"]))


def _clear_flag(rid, ts, nxt, done: bool = False, keep_last: bool = False) -> None:
    """Fire-and-forget the running=0 clear: when a user STOPs a scheduled
    run, _fire itself is being cancelled, and awaiting anything in that arm
    just re-raises CancelledError — which used to strand running=1 (the
    phantom-RUNNING bug). A spawned task runs on its own footing."""

    async def _go():
        try:
            if done:
                await _set(
                    "UPDATE schedules SET running=0, last_run=?, next_run=NULL,"
                    " enabled=0, done_at=? WHERE id=?", (ts, ts, rid))
            elif keep_last:  # deferred, never ran: don't stamp last_run
                await _set("UPDATE schedules SET running=0, next_run=? WHERE id=?", (nxt, rid))
            else:
                await _set(
                    "UPDATE schedules SET running=0, last_run=?, next_run=? WHERE id=?",
                    (ts, nxt, rid))
        except RuntimeError:
            logger.exception("schedule %r: running flag STUCK (restart to clear)", rid)

    agent._spawn(_go())


# ---- CRUD (used by main.py routes) ----

_COLS = ("id", "title", "prompt", "cron", "thread_id", "enabled",
         "last_run", "next_run", "running", "created_at", "run_at", "done_at")


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


def _check(cron: str, run_at: float | None) -> None:
    if bool(cron) == (run_at is not None):
        raise ValueError("give either a cron cadence or a one-off run_at time (not both)")
    if cron and not croniter.is_valid(cron):
        raise ValueError(f"invalid cron expression: {cron!r}")
    if run_at is not None and run_at < time.time() - 60:
        raise ValueError("one-off time is in the past")


async def create(title: str, prompt: str, cron: str = "", run_at: float | None = None) -> dict:
    cron = (cron or "").strip()
    _check(cron, run_at)
    sid = uuid.uuid4().hex[:12]
    now = time.time()
    thread = await agent.create_thread(title)  # the task's run-history notebook
    await _set(
        "INSERT INTO schedules(id,title,prompt,cron,thread_id,enabled,last_run,next_run,"
        "running,created_at,run_at,done_at) VALUES(?,?,?,?,?,1,NULL,?,0,?,?,NULL)",
        (sid, title, prompt, cron, thread["id"], _due(cron, run_at, now), now, run_at))
    return await get(sid)


async def update(sid: str, patch: dict) -> dict | None:
    cur = await _db.execute("SELECT cron, run_at FROM schedules WHERE id=?", (sid,))
    r = await cur.fetchone()
    if not r:
        return None
    cron, run_at = r[0] or "", r[1]
    cols, vals = [], []
    for k in ("title", "prompt"):
        if patch.get(k) is not None:
            cols.append(f"{k}=?")
            vals.append(patch[k])
    timing = patch.get("cron") is not None or "run_at" in patch and patch["run_at"] is not None
    if timing:
        # switching kinds is allowed: a cron clears run_at, a run_at clears cron
        if patch.get("cron"):
            cron, run_at = patch["cron"].strip(), None
        else:
            cron, run_at = "", patch["run_at"]
        _check(cron, run_at)
        # (re-)arming: a new time on a DONE one-off brings it back
        cols += ["cron=?", "run_at=?", "next_run=?", "enabled=1", "done_at=NULL"]
        vals += [cron, run_at, _due(cron, run_at)]
    if patch.get("enabled") is not None and not timing:
        if patch["enabled"] and not cron and (run_at or 0) < time.time():
            raise ValueError("this one-off's time has passed — give it a new time to re-arm it")
        cols.append("enabled=?")
        vals.append(1 if patch["enabled"] else 0)
        if patch["enabled"]:
            cols.append("next_run=?")
            vals.append(_due(cron, run_at))  # waking up restarts from now
    if cols:
        vals.append(sid)
        await _set(f"UPDATE schedules SET {', '.join(cols)} WHERE id=?", vals)
    return await get(sid)


async def delete(sid: str) -> bool:
    cur = await _db.execute("SELECT thread_id FROM schedules WHERE id=?", (sid,))
    r = await cur.fetchone()
    if not r:
        return False
    await _set("DELETE FROM schedules WHERE id=?", (sid,))
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


def when_text(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %b %-d %H:%M")


def describe(row: dict) -> str:
    """Panel/tool text for either kind of task."""
    if not row.get("cron"):
        if row.get("done_at"):
            return f"once — done {when_text(row['done_at'])}"
        return f"once at {when_text(row['run_at'])}" if row.get("run_at") else "once"
    return humanize(row["cron"])


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
    if m.isdigit() and H.startswith("*/") and dom == "*" and mon == "*" and dow == "*":
        # m is MINUTES past the hour — '26 */4' fires 00:26, 04:26, …
        return f"every {H[2:]} hours at :{m.zfill(2)}"
    if m.isdigit() and H == "*" and dom == "*" and dow == "*":
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

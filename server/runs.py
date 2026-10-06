"""Detached run hubs: a run belongs to the SERVER, not to a browser tab.

Before this module, /api/chat wrapped agent.run_chat in the HTTP response
generator, so a dropped connection — phone screen timing out, app switch, a
tab refresh, a proxy hiccup — cancelled the run mid-air (the trajectory's
"RUN CANCELLED — client disconnected" breadcrumbs). On top of that the UI
could only stream one run at a time, so a busy thread froze the SEND button
everywhere else.

Now a run is a plain task (HUBS), independent of any request, appending its
events to a ring buffer; any number of SSE followers can attach from any
offset (`follow`) and come and go freely. A hub lingers briefly after its
run so a late reopen can replay/finalize, then gets swept.

Cancellation is now EXPLICIT only (POST .../cancel → task.cancel()): the
producer's CancelledError arm cannot await anything (same rule as agent.py's
generator arm), so the terminal event + finish ride a spawned helper.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator

from . import agent, learning, notify

MAX_ACTIVE = 4       # concurrent runs across ALL threads (shared Spark)
GRACE = 300.0        # seconds a finished hub lingers for late reopen/replay
EVENT_CAP = 20000    # ring caps: a follower behind the window gets a "gap"
BYTES_CAP = 4_000_000
HEARTBEAT = 5.0      # SSE comment lines when idle — keeps phones/proxies alive


class Busy(Exception):
    pass


class TooMany(Exception):
    pass


class Hub:
    def __init__(self, tid: str) -> None:
        self.tid = tid
        self.events: list[dict] = []
        self.dropped = 0  # events evicted from the ring head (abs index base)
        self._bytes = 0
        self.done = False
        self.done_ts = 0.0
        self.t0 = time.time()
        self.cond = asyncio.Condition()
        self.task: asyncio.Task | None = None

    async def append(self, ev: dict) -> None:
        async with self.cond:
            self.events.append(ev)
            self._bytes += len(json.dumps(ev, ensure_ascii=False))
            while (len(self.events) > EVENT_CAP or self._bytes > BYTES_CAP) and len(self.events) > 1:
                self._bytes -= len(json.dumps(self.events[0], ensure_ascii=False))
                self.events.pop(0)
                self.dropped += 1
            self.cond.notify_all()

    async def finish(self) -> None:
        async with self.cond:
            self.done = True
            self.done_ts = time.time()
            self.cond.notify_all()


HUBS: dict[str, Hub] = {}


def sweep() -> None:
    now = time.time()
    for tid, h in list(HUBS.items()):
        if h.done and now - h.done_ts > GRACE:
            del HUBS[tid]


def get(tid: str) -> Hub | None:
    return HUBS.get(tid)


def active_map() -> dict:
    sweep()
    now = time.time()
    return {
        "runs": {h.tid: {"age": round(now - h.t0, 1)} for h in HUBS.values() if not h.done}
    }


def start(tid: str, events: AsyncIterator[dict]) -> Hub:
    """Own `events` (an async generator) in a detached task. Raises Busy if
    the thread already runs; TooMany past MAX_ACTIVE. The abandoned generator
    on a raise is closed by its finalizer — creating it had no side effects."""
    sweep()
    h = HUBS.get(tid)
    if h and not h.done:
        raise Busy(f"thread is already running (since {time.time() - h.t0:.0f}s) — STOP it first")
    if sum(1 for x in HUBS.values() if not x.done) >= MAX_ACTIVE:
        raise TooMany(f"{MAX_ACTIVE} concurrent runs already in flight — wait for one to finish")
    hub = Hub(tid)
    HUBS[tid] = hub
    hub.task = asyncio.get_running_loop().create_task(_produce(hub, events))
    return hub


def cancel(tid: str) -> bool:
    h = HUBS.get(tid)
    if not h or h.done:
        return False
    h.task.cancel()
    return True


async def _produce(hub: Hub, events: AsyncIterator[dict]) -> None:
    try:
        async for ev in events:
            await hub.append(ev)
    except (asyncio.CancelledError, GeneratorExit):
        # injected by cancel() (or task death at shutdown); awaiting here is
        # illegal, so the terminal event + finish ride a spawned helper
        agent._spawn(_on_cancel(hub))
        raise
    except Exception as e:  # noqa: BLE001 - the hub stream must still terminate
        try:
            await hub.append(
                {"type": "error", "message": f"RUN FAILED — {type(e).__name__}: {str(e)[:300]}"}
            )
        except Exception:  # noqa: BLE001
            pass
    # needs-input / done / failed → open tabs' feed + phone push when nobody
    # is watching this thread (a user STOP takes the cancel arm above: no event)
    agent._spawn(notify.run_finished(hub.tid, list(hub.events)))
    # substantial turn finished cleanly → background skill/memory review
    agent._spawn(learning.after_run(hub.tid, list(hub.events)))
    await hub.finish()
    # run's generator is exhausted → every checkpoint put has committed, and
    # this thread just left the active set → cheap moment to bound the tables
    agent.schedule_prune()


async def _on_cancel(hub: Hub) -> None:
    try:
        await hub.append({"type": "error", "message": "RUN CANCELLED — stop requested"})
        await hub.finish()
        agent.schedule_prune()
    except Exception:  # noqa: BLE001 - hub is going away anyway
        pass


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


async def follow(hub: Hub, since: int = 0) -> AsyncIterator[str]:
    """SSE frames for hub events from absolute offset `since`, live-following
    until the hub finishes. Indexes are absolute (list position + dropped);
    a follower that fell behind the ring gets one `gap` note and resumes at
    the window head. Heartbeats are SSE comments — invisible to the client's
    `data:` parser, but they keep Caddy and phone networks from idling us."""
    i = since
    # join header (does NOT advance the offset): a fresh follower that joins
    # a hub already DONE is racing a finished run — the client discards it
    # and repaints from /messages instead of double-painting the replay.
    yield _sse({"type": "join", "done": hub.done})
    while True:
        async with hub.cond:
            try:
                await asyncio.wait_for(
                    hub.cond.wait_for(lambda: i < hub.dropped + len(hub.events) or hub.done),
                    timeout=HEARTBEAT,
                )
            except asyncio.TimeoutError:
                pass
            dropped, events = hub.dropped, hub.events
            note = None
            if i < dropped:  # evicted under us (initial join or a stalled follower)
                note, i = _sse({"type": "gap"}), dropped
            batch = events[i - dropped:]
            i += len(batch)
            finished = hub.done and i >= dropped + len(events)
        if note:
            yield note
        for ev in batch:
            yield _sse(ev)
        if finished:
            return
        if not batch:
            yield ": ping\n\n"


def shell_events(tid: str, command: str) -> AsyncIterator[dict]:
    """Wrap a `!cmd` run in the same one-event-per-line contract as run_chat,
    so shells ride the hub machinery too (`! sleep 300` outlives a dead phone)."""

    async def _g() -> AsyncIterator[dict]:
        result = await agent.run_user_shell(tid, command)
        yield {"type": "shell_result", "result": result}
        yield {"type": "done", "seconds": result.get("dur") or 0}

    return _g()

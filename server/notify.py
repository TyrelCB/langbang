"""Notifications: tell the user when a run needs them, finished, or failed —
especially runs they aren't watching (another thread, a hidden tab, a
scheduled firing at 3 am).

Two outlets, one event source (runs._produce calls run_finished()):
- an in-memory EVENTS feed every open tab polls (GET /api/notify/events);
  the tab shows a toast + title badge + sound, and a desktop notification
  when the browser allows it (secure context only: https or localhost);
- phone push via ntfy (https://ntfy.sh or a self-hosted server) — sent only
  when no visible tab is looking at that thread right now (PRESENCE, fed by
  the 4 s /api/runs poll), so watching a run never buzzes your phone.

Kinds: "input" (ask_user / plan waiting — the run is paused), "done",
"failed". A user's own STOP is not an event.
"""
from __future__ import annotations

import collections
import logging
import secrets
import time

import httpx

from . import agent, config

logger = logging.getLogger("langbang.notify")

EVENTS: collections.deque = collections.deque(maxlen=200)
_seq = 0
PRESENCE: dict[str, dict] = {}  # tab id -> {tid, visible, ts}
PRESENCE_TTL = 15.0  # a tab silent this long (closed / asleep) isn't watching

KIND_TAG = {"input": "raising_hand", "done": "white_check_mark", "failed": "x"}
KIND_WORD = {"input": "needs your input", "done": "finished", "failed": "failed"}


def defaults() -> dict:
    return {
        "ntfy_enabled": False,
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",          # generated on first enable — the topic IS the secret
        "events": {"input": True, "done": True, "failed": True},
        "preview": True,           # include the first lines of the answer
        "click_base": "",          # e.g. http://192.168.6.185 — where a tap opens LangBang
    }


def settings() -> dict:
    n = config.load().get("notify") or {}
    return {**defaults(), **n, "events": {**defaults()["events"], **(n.get("events") or {})}}


def new_topic() -> str:
    return "langbang-" + secrets.token_hex(8)


# ---- presence ----

def touch(tab: str, tid: str | None, visible: bool) -> None:
    if not tab:
        return
    PRESENCE[tab[:64]] = {"tid": tid or None, "visible": bool(visible), "ts": time.time()}
    if len(PRESENCE) > 200:  # stale tabs
        cutoff = time.time() - PRESENCE_TTL
        for k in [k for k, p in PRESENCE.items() if p["ts"] < cutoff]:
            PRESENCE.pop(k, None)


def watching(tid: str) -> bool:
    now = time.time()
    return any(p["visible"] and p["tid"] == tid and now - p["ts"] < PRESENCE_TTL
               for p in PRESENCE.values())


# ---- events ----

def since(n: int) -> dict:
    return {"last": _seq, "events": [e for e in EVENTS if e["id"] > n]}


def _summarize(events: list[dict]) -> tuple[str | None, str]:
    """(kind, text) for a finished run from its hub event list."""
    segs, cur = [], []
    gate, err, shell = None, None, None
    for ev in events:
        t = ev.get("type")
        if t == "token":
            cur.append(ev.get("text") or "")
        elif t in ("tool_start", "tool_end", "done"):
            if cur:
                segs.append("".join(cur))
                cur = []
        elif t == "gate":
            gate = ev.get("value") or {}
        elif t == "error":
            err = str(ev.get("message") or "")
        elif t == "shell_result":
            shell = ev.get("result") or {}
    if cur:
        segs.append("".join(cur))
    if gate is not None:
        if gate.get("kind") == "plan":
            return "input", "A plan is ready for your approval."
        qs = [q.get("question", "") for q in gate.get("questions") or [] if isinstance(q, dict)]
        return "input", ("Question: " + " · ".join(qs)) if qs else "The agent is asking you something."
    if err is not None:
        if err.startswith("RUN CANCELLED"):
            return None, ""  # the user's own STOP
        return "failed", err
    if shell is not None:
        ok = shell.get("exit") == 0
        return ("done" if ok else "failed"), f"$ {shell.get('cmd', '')} → exit {shell.get('exit', '?')}"
    answer = next((s.strip() for s in reversed(segs) if s.strip()), "")
    return "done", answer


async def _title(tid: str) -> str:
    try:
        cur = await agent._db.execute("SELECT title FROM threads WHERE id=?", (tid,))
        r = await cur.fetchone()
        return (r[0] if r else "") or "LangBang"
    except Exception:  # noqa: BLE001 - a title is cosmetic
        return "LangBang"


async def run_finished(tid: str, events: list[dict]) -> None:
    """Called by runs._produce when a hub's run ends (any reason)."""
    global _seq
    kind, text = _summarize(events)
    if kind is None:
        return
    s = settings()
    if not s["events"].get(kind, True):
        return  # this kind is switched off in CONFIG → NOTIFICATIONS (toasts too)
    title = await _title(tid)
    _seq += 1
    ev = {"id": _seq, "ts": time.time(), "kind": kind, "tid": tid, "title": title,
          "text": " ".join(text.split())[:280]}
    EVENTS.append(ev)
    if s["ntfy_enabled"] and s["ntfy_topic"] and not watching(tid):
        agent._spawn(send_ntfy(ev, s))


def learned(tid: str, title: str, results: list[dict]) -> None:
    """Feed-only event (toast, no push): the post-run review changed a skill
    or memory. Shown even on the thread you're looking at — it's news."""
    global _seq
    _seq += 1
    text = "; ".join(f"{r['op']} {r['kind']} {r['name']}" for r in results)
    EVENTS.append({"id": _seq, "ts": time.time(), "kind": "learned", "tid": tid,
                   "title": title, "text": text[:280]})


async def send_ntfy(ev: dict, s: dict | None = None) -> dict:
    """POST one message to the ntfy topic. Returns {ok, status|error}."""
    s = s or settings()
    body = {
        "topic": s["ntfy_topic"],
        "title": f"{ev['title'][:80]} — {KIND_WORD.get(ev['kind'], ev['kind'])}",
        "message": (ev.get("text") if s.get("preview", True) else "") or KIND_WORD.get(ev["kind"], ""),
        "tags": [KIND_TAG.get(ev["kind"], "bell")],
        "priority": 4 if ev["kind"] in ("input", "failed") else 3,
    }
    if s.get("click_base") and ev.get("tid"):
        body["click"] = s["click_base"].rstrip("/") + "/?thread=" + ev["tid"]
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(s["ntfy_server"].rstrip("/") + "/", json=body)
        if r.status_code >= 300:
            logger.warning("ntfy push failed: HTTP %s %s", r.status_code, r.text[:200])
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return {"ok": True, "status": r.status_code}
    except Exception as e:  # noqa: BLE001 - never break a run over a push
        logger.warning("ntfy push failed: %s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

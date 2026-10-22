"""LangGraph agent + streaming runner + thread storage."""
import asyncio
import json
import logging
import math
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite
import openai
from langchain_core.callbacks.manager import adispatch_custom_event
from deepagents import (
    FilesystemPermission,
    HarnessProfile,
    backends,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.middleware.skills import SkillsMiddleware
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.config import get_config
from langgraph.errors import GraphBubbleUp, GraphInterrupt, GraphRecursionError
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command, interrupt
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from deepagents.middleware._utils import append_to_system_message

from . import config, learning, local_tools, mcp

# LangBang's tuning of the deepagents harness, registered for provider
# "openai" (our sglang backend is OpenAI-compatible; the lookup falls back to
# provider for pre-built model instances):
# - Drop deepagents' SummarizationMiddleware — our own compaction replaces it
#   and additionally archives originals to SQLite so the UI transcript stays
#   complete (theirs offloads to backend files our history() never reads).
# - Hide the built-in `execute` tool: LocalShellBackend would give the agent a
#   second, always-on shell while run_bash is the one the CONFIG toggles govern.
register_harness_profile(
    "openai",
    HarnessProfile(
        excluded_middleware=frozenset({"SummarizationMiddleware"}),
        excluded_tools=frozenset({"execute"}),
    ),
)

logger = logging.getLogger("langbang.agent")

_bg: set = set()  # fire-and-forget tasks kept referenced until done


def _spawn(coro):
    try:
        t = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:  # loop already gone (interpreter shutdown) — drop it
        coro.close()
        return None
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    return t

_checkpointer: AsyncSqliteSaver | None = None
_db: aiosqlite.Connection | None = None
_edb: aiosqlite.Connection | None = None  # event-log connection (events.db)
_cpdb: aiosqlite.Connection | None = None  # checkpointer-only connection (langbang.db)
# graph used ONLY to read state back (never invoked); see _read_agent()
_read_graph = None


async def init() -> None:
    global _checkpointer, _db, _edb, _cpdb
    _db = await aiosqlite.connect(config.DB_PATH)
    await _db.execute("PRAGMA busy_timeout=30000")
    # The checkpointer gets its OWN connection to the same WAL file. Statements
    # are FIFO-queued per connection and aput() is execute(INSERT)+commit as
    # TWO queued ops — so a _wt() UI commit/rollback could interleave between
    # them: worst case _wt's rollback discards the saver's already-executed
    # INSERT while the saver's queued commit() then no-ops "successfully", and
    # LangGraph advances with the checkpoint row SILENTLY LOST (2026-09-27
    # forensics). Transactions belong to connections, so a dedicated _cpdb
    # makes that interleave structurally impossible.
    _cpdb = await aiosqlite.connect(config.DB_PATH)
    await _cpdb.execute("PRAGMA busy_timeout=30000")
    _checkpointer = AsyncSqliteSaver(_cpdb)
    await _checkpointer.setup()
    # Trajectory rows live in their OWN FILE now: one INSERT+commit per run
    # event sharing the 900 MB checkpoint file's WAL writer lock was a
    # contention amplifier in the "database is locked" deaths. (One-way
    # migration below: old code versions can't see migrated trajectories.)
    _edb = await aiosqlite.connect(config.EVENTS_DB_PATH)
    await _edb.execute("PRAGMA busy_timeout=30000")
    await _edb.execute("PRAGMA journal_mode=WAL")
    await _db.executescript(
        """
        CREATE TABLE IF NOT EXISTS threads(
          id TEXT PRIMARY KEY, title TEXT, created_at REAL, updated_at REAL,
          orig TEXT);
        CREATE TABLE IF NOT EXISTS archived_messages(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, msg TEXT);
        CREATE TABLE IF NOT EXISTS btw(
          id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL,
          ts REAL NOT NULL, q TEXT NOT NULL, answer TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_btw_thread ON btw(thread_id, id);
        CREATE TABLE IF NOT EXISTS arch_base(
          tid TEXT PRIMARY KEY, base TEXT NOT NULL, n INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_arch_base ON arch_base(base);
        CREATE INDEX IF NOT EXISTS ix_archived_thread ON archived_messages(thread_id, seq);
        CREATE TABLE IF NOT EXISTS procs(   -- ⚙ background processes (procs.py)
          id TEXT PRIMARY KEY, thread_id TEXT, name TEXT NOT NULL, cmd TEXT NOT NULL,
          started REAL NOT NULL, ended REAL, exit INTEGER,
          state TEXT NOT NULL,              -- running | exited | stopped | lost
          wake TEXT NOT NULL,               -- exit | match | none
          wake_match TEXT, woke REAL, log_off INTEGER DEFAULT 0,
          host TEXT,                        -- ran via ssh on this host (NULL = here)
          warned REAL);                     -- early shell-error wake sent
        CREATE INDEX IF NOT EXISTS ix_procs_thread ON procs(thread_id, started);
        CREATE TABLE IF NOT EXISTS projects(   -- ◫ projects (projects.py)
          id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT,
          created_at REAL NOT NULL, auto INTEGER NOT NULL DEFAULT 0);
        """
    )
    # threads.orig = the seed title (schedule name / first-message prefix) that
    # ⟲ revert-title restores. Existing DBs predate the column.
    # threads.prompt = per-thread instructions appended to the system prompt
    # threads.model = JSON {provider, model} override of the global default
    # threads.parent = JSON {tid, title} of the thread this one was ⑂ forked from
    # threads.pinned = pin time (REAL) — pinned rows sort above the rest
    # threads.archived = archive time (REAL): read-only — no runs, no !cmd,
    #   nothing merged in; a ⤴ merge archives its branch (git merge + branch -d)
    # threads.project = projects.id (NULL = unfiled); threads.drift = JSON
    #   "this run worked in another repo" suggestion (projects.observe)
    for col, typ in (("orig", "TEXT"), ("prompt", "TEXT"), ("model", "TEXT"),
                     ("parent", "TEXT"), ("pinned", "REAL"), ("archived", "REAL"),
                     ("project", "TEXT"), ("drift", "TEXT")):
        try:
            await _db.execute(f"ALTER TABLE threads ADD COLUMN {col} {typ}")
        except aiosqlite.OperationalError:  # duplicate column — already migrated
            pass
    for col, typ in (("host", "TEXT"), ("warned", "REAL")):
        try:
            await _db.execute(f"ALTER TABLE procs ADD COLUMN {col} {typ}")
        except aiosqlite.OperationalError:
            pass
    await _db.execute("UPDATE threads SET orig=title WHERE orig IS NULL")
    await _db.commit()
    await _edb.executescript(
        """
        CREATE TABLE IF NOT EXISTS run_events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          thread_id TEXT NOT NULL,
          turn_id   TEXT NOT NULL,          -- one per /api/chat call
          ts        REAL NOT NULL,
          type      TEXT NOT NULL,          -- user | model | tool | error
          name      TEXT,                   -- tool name / model id
          dur       REAL,                   -- seconds (model/tool calls)
          tok_in    INTEGER, tok_out INTEGER, cache_read INTEGER, ttft REAL,
          meta      TEXT                    -- JSON previews (caps in _log callers)
        );
        CREATE INDEX IF NOT EXISTS ix_run_events_thread ON run_events(thread_id, seq);
        """
    )
    await _edb.commit()
    await _migrate_run_events()
    await _share_fork_archives()
    await _startup_maintenance()


async def shutdown() -> None:
    """Close every aiosqlite connection: each Connection IS a non-daemon
    thread parked on its work queue — interpreter shutdown joins them forever
    if they're left open (a graceful uvicorn stop would hang; the scripted
    fuser -k restart masked this). Closing also checkpoints their WALs, so
    what was written survives in the main files."""
    for c in (_cpdb, _db, _edb):
        if c is not None:
            try:
                await c.close()
            except Exception:  # noqa: BLE001 - best-effort; process is leaving
                pass


async def _migrate_run_events() -> None:
    """One-time: run_events rows from langbang.db into events.db, then DROP
    the legacy table. OR IGNORE makes a crash between copy and drop resumable
    (seq is the shared PK). If the copy can't be verified the legacy table
    stays untouched and next boot retries — new writes already go to
    events.db, so at worst old trajectory rows sit idle."""
    cur = await _db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_events' LIMIT 1")
    if not await cur.fetchone():
        return  # fresh DB — nothing to migrate
    cur = await _db.execute("SELECT COUNT(*) FROM run_events")
    n = (await cur.fetchone())[0]
    ok = n == 0
    if n:
        await _edb.execute("ATTACH DATABASE ? AS src", (config.DB_PATH,))
        try:
            await _edb.execute(
                "INSERT OR IGNORE INTO run_events(seq,thread_id,turn_id,ts,type,name,"
                "dur,tok_in,tok_out,cache_read,ttft,meta) "
                "SELECT seq,thread_id,turn_id,ts,type,name,dur,tok_in,tok_out,"
                "cache_read,ttft,meta FROM src.run_events")
            await _edb.commit()
            got = (await (await _edb.execute("SELECT COUNT(*) FROM run_events")).fetchone())[0]
            ok = got >= n
        except Exception as e:  # noqa: BLE001 — legacy rows are the fallback
            await _edb.rollback()
            logger.warning("run_events migration failed (%s) — legacy table kept, retry next boot", e)
        finally:
            try:
                await _edb.execute("DETACH DATABASE src")  # needs no open txn
            except Exception:  # noqa: BLE001
                pass
    if ok:
        await _db.execute("DROP TABLE run_events")  # takes its index with it
        await _db.commit()
        logger.warning("run_events: %d row(s) migrated to events.db, legacy table dropped", n)


async def _startup_maintenance() -> None:
    """First boot after this deploy: purge orphans + prune, then VACUUM the
    once-huge file back to size. Startup-only (no traffic yet, nothing can be
    mid-transaction); steady-state boots find 0 rows and skip the VACUUM."""
    deleted = await purge_orphan_checkpoints()
    deleted += await prune_checkpoints()
    if not deleted:
        return
    await _db.commit()
    await _cpdb.commit()
    before = (await (await _cpdb.execute("PRAGMA page_count")).fetchone())[0]
    t0 = time.time()
    await _cpdb.execute("VACUUM")
    after = (await (await _cpdb.execute("PRAGMA page_count")).fetchone())[0]
    logger.warning("db maintenance: removed %d checkpoint rows, %d -> %d pages (%.1fs)",
                   deleted, before, after, time.time() - t0)


async def purge_orphan_checkpoints() -> int:
    """Delete checkpoints/writes rows for thread_ids with no `threads` row.
    Every run _touch()es its thread row before its first put, so no thread row
    == garbage (deleted threads — delete_thread used to leak these by the
    hundred). Threads with a live hub are skipped: a cancelled-but-dying run
    can still be writing, and the next sweep gets it."""
    from . import runs  # lazy: runs imports agent at module level
    active = {h.tid for h in runs.HUBS.values() if not h.done}
    deleted = 0
    async with _checkpointer.lock:
        cur = await _cpdb.execute(
            "SELECT DISTINCT thread_id FROM checkpoints "
            "WHERE thread_id NOT IN (SELECT id FROM threads)")
        tids = [r[0] for r in await cur.fetchall() if r[0] not in active]
        for i in range(0, len(tids), 200):
            ph = ",".join("?" * len(tids[i:i + 200]))
            batch = tids[i:i + 200]
            c1 = await _cpdb.execute(f"DELETE FROM writes WHERE thread_id IN ({ph})", batch)
            c2 = await _cpdb.execute(f"DELETE FROM checkpoints WHERE thread_id IN ({ph})", batch)
            deleted += c1.rowcount + c2.rowcount
            await _cpdb.commit()
    if deleted:
        logger.warning("db maintenance: purged %d checkpoint/writes rows of %d orphan thread(s)",
                       deleted, len(tids))
    return deleted


async def prune_checkpoints(keep: int = 3) -> int:
    """Bound the checkpoint tables: per (thread_id, checkpoint_ns), keep
    everything >= floor where floor = min(newest seed row, the `keep`-th
    newest id) — at least `keep` rows AND never deleting past the newest
    _DeltaSnapshot seed.

    SAFETY: deep-mode `messages` is a DeltaChannel (snapshot ~every 50 steps);
    aget_state rebuilds it by walking the parent chain down to the newest seed
    row — past that, langgraph treats absence as 'start empty' = SILENT
    CONTEXT LOSS. A seed is exactly a row whose metadata has no
    counters_since_delta_snapshot.messages (counters are zeroed at a snapshot
    and only written when non-zero; pre-delta legacy rows also lack counters,
    counting as seeds = the safe direction). checkpoint_id is uuid6: string
    sort == chronological (verified on the live DB). Errors are always
    'skip this namespace', never 'delete'. Runs under the saver's own lock on
    _cpdb, so DELETEs can never interleave with a put's execute/commit pair."""
    from . import runs
    active = {h.tid for h in runs.HUBS.values() if not h.done}
    deleted = 0
    async with _checkpointer.lock:
        rows = await (await _cpdb.execute(
            "SELECT DISTINCT thread_id, checkpoint_ns FROM checkpoints")).fetchall()
        for tid, ns in rows:
            if tid in active:
                continue
            try:
                seed = await (await _cpdb.execute(
                    "SELECT checkpoint_id FROM checkpoints "
                    "WHERE thread_id=? AND checkpoint_ns=? AND json_extract(metadata,"
                    "'$.counters_since_delta_snapshot.messages') IS NULL "
                    "ORDER BY checkpoint_id DESC LIMIT 1", (tid, ns))).fetchone()
                third = await (await _cpdb.execute(
                    "SELECT checkpoint_id FROM checkpoints "
                    "WHERE thread_id=? AND checkpoint_ns=? "
                    "ORDER BY checkpoint_id DESC LIMIT 1 OFFSET ?", (tid, ns, keep - 1))).fetchone()
                if seed is None or third is None:
                    continue  # no anchor detectable, or fewer than `keep` rows — hands off
                floor = min(seed[0], third[0])  # keep >= keep rows AND the seed walk landing
                c1 = await _cpdb.execute(
                    "DELETE FROM writes WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id<?",
                    (tid, ns, floor))
                c2 = await _cpdb.execute(
                    "DELETE FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id<?",
                    (tid, ns, floor))
                deleted += c1.rowcount + c2.rowcount
                if deleted and deleted % 500 < 2:
                    await _cpdb.commit()  # bounded WAL growth; yields the FIFO to saver puts
            except Exception as e:  # noqa: BLE001 — skip, never over-delete on a surprise
                logger.warning("checkpoint prune skipped for %s/%s: %s", tid[:12], ns[:20], e)
        await _cpdb.commit()
    return deleted


_last_prune_ts = 0.0


def schedule_prune(force: bool = False) -> None:
    """Throttled fire-and-forget maintenance (hub finishes call it; a burst of
    endings costs one sweep)."""
    global _last_prune_ts
    if not force and time.time() - _last_prune_ts < 60:
        return
    _last_prune_ts = time.time()

    async def _go():
        try:
            n = await purge_orphan_checkpoints()
            n += await prune_checkpoints()
            if n:
                logger.warning("checkpoint prune: removed %d row(s)", n)
        except Exception as e:  # noqa: BLE001 — maintenance never breaks a run
            logger.warning("checkpoint prune failed: %s", e)

    _spawn(_go())


async def current_todos(thread_id: str):
    """The checkpointed `todos` channel value — what write_todos last
    COMMITTED. /messages can't always reconstruct the list (args-dropped tool
    calls, a lost checkpoint commit), so a reopening/second browser asks this
    when its tool-call scan comes up empty; the card then matches state."""
    tup = await _checkpointer.aget({"configurable": {"thread_id": thread_id}})
    if not tup:
        return None
    cv = tup.get("channel_values") if isinstance(tup, dict) else tup.channel_values
    return (cv or {}).get("todos") or None


# Chat-template control tokens written as TEXT (`<|im_start|>`, `<|im_end|>`,
# Llama's `<|eot_id|>` …) are tokenized by sglang as the REAL special token, so
# one in a tool result or a pasted log forges a message boundary mid-prompt.
# Thread e93c1bb448e4 (2026-10-13/14): reviewing IVR transcripts that held a
# leaked "<|im_start|>", every later turn ended after a few thinking tokens
# with an empty answer. Outgoing messages get a word joiner after the "<"
# (text unchanged to the eye, ordinary tokens to the model); tool-call args
# coming back have it removed, so files the agent edits keep the real string.
# Same for the reasoning tags (2026-10-17, thread 5fa2b2687abf): thinking
# about a heretic config's `response_prefix = "<think></think>"`, the model
# emitted the REAL </think> mid-thought — sglang's reasoning parser ended the
# thinking there and the rest of it became the "final answer", ending the run.
_CTRL_TOKEN = re.compile(r"<\|([A-Za-z0-9_]{1,40})\|>")
_THINK_TAG = re.compile(r"<(/?think)>")
_WJ = "\u2060"


def defang(text: str) -> str:
    if "<|" in text:
        text = _CTRL_TOKEN.sub(lambda m: f"<{_WJ}|{m.group(1)}|>", text)
    if "think>" in text:
        text = _THINK_TAG.sub(lambda m: f"<{_WJ}{m.group(1)}>", text)
    return text


def undefang(v):
    """Inverse of defang over a tool-call args structure."""
    if isinstance(v, str):
        return v.replace(f"<{_WJ}", "<") if _WJ in v else v
    if isinstance(v, dict):
        return {k: undefang(x) for k, x in v.items()}
    if isinstance(v, list):
        return [undefang(x) for x in v]
    return v


def defang_message(m: dict) -> None:
    """In place, on one chat-completions message dict."""
    c = m.get("content")
    if isinstance(c, str):
        m["content"] = defang(c)
    elif isinstance(c, list):
        for part in c:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                part["text"] = defang(part["text"])
    if isinstance(m.get("reasoning_content"), str):
        m["reasoning_content"] = defang(m["reasoning_content"])
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
            fn["arguments"] = defang(fn["arguments"])


class SGlangChatOpenAI(ChatOpenAI):
    """ChatOpenAI that recovers `reasoning_content` / `reasoning` from streamed deltas.

    langchain-openai >=1.x deliberately ignores non-spec delta fields, so
    sglang's/vLLM's separated thinking would be dropped on the floor; we
    re-attach it to additional_kwargs where _msg_dict/run_chat can see it.

    keep_reasoning ("off" | "turn" | "all") sends it BACK on assistant
    messages as `reasoning_content` — which langchain-openai never does, so
    every earlier step reached the Qwen3.8 template as an empty
    <think></think>: the model never saw why it made its previous calls.
    """

    keep_reasoning: str = "off"
    # the main agent's system prompt (build_agent): a request that starts with
    # it is the thread's own model view and is kept for /btw (_capture)
    lb_main_prompt: str = ""

    def _get_request_payload(self, input_, *, stop=None, **kwargs):  # noqa: ANN001
        payload = self._reasoning_payload(input_, stop=stop, **kwargs)
        for m in payload.get("messages") or []:
            defang_message(m)
        self._resume_payload(input_, payload)
        if self.lb_main_prompt:
            self._capture(input_, payload)
        return payload

    def _capture(self, input_, payload) -> None:
        """Keep the main agent's latest request per thread: /btw replays it
        with the side question appended, so it sees exactly what the agent
        sees AND shares its prefix in vLLM's cache. Sub-agents ride the same
        model object but carry their own system prompt, so they're skipped."""
        msgs = payload.get("messages") or []
        if not msgs or msgs[0].get("role") != "system" \
                or not _text_only(msgs[0].get("content") or "").startswith(self.lb_main_prompt):
            return
        try:
            tid = (get_config().get("configurable") or {}).get("thread_id")
        except RuntimeError:  # outside a graph run (btw's own fallback build)
            return
        if not tid:
            return
        last = next((m.id for m in reversed(self._convert_input(input_).to_messages()) if m.id), None)
        _VIEWS[tid] = {"payload": payload, "last_id": last, "client": self.root_async_client,
                       "ts": time.time()}
        while len(_VIEWS) > 8:
            _VIEWS.pop(next(iter(_VIEWS)))

    def _resume_payload(self, input_, payload) -> None:
        """A trailing AIMessage carrying `lb_resume` (see _EmptyAnswerRetry)
        becomes an OPEN assistant turn — "<think>\n" + the cut thinking —
        that the server continues (vLLM continue_final_message) instead of
        starting a new reply. Added after defang: this <think> is meant real."""
        msgs = payload.get("messages") or []
        last = self._convert_input(input_).to_messages()[-1:] if msgs else []
        carry = (last[0].additional_kwargs or {}).get("lb_resume") \
            if last and isinstance(last[0], AIMessage) else None
        if not carry or msgs[-1].get("role") != "assistant":
            return
        for k in ("reasoning_content", "tool_calls"):
            msgs[-1].pop(k, None)
        msgs[-1]["content"] = "<think>\n" + defang(carry)
        payload["extra_body"] = {**(payload.get("extra_body") or {}),
                                 "continue_final_message": True, "add_generation_prompt": False}

    def _reasoning_payload(self, input_, *, stop=None, **kwargs):  # noqa: ANN001
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if self.keep_reasoning not in ("turn", "all") or "messages" not in payload:
            return payload
        msgs = self._convert_input(input_).to_messages()
        if len(msgs) != len(payload["messages"]):
            return payload  # not the 1:1 chat-completions shape — leave it alone
        start = 0
        if self.keep_reasoning == "turn":  # only steps after the latest user message
            for i in range(len(msgs) - 1, -1, -1):
                if _turn_start(msgs[i]):
                    start = i
                    break
        for i in range(start, len(msgs)):
            rc = (msgs[i].additional_kwargs or {}).get("reasoning_content") \
                if isinstance(msgs[i], AIMessage) else None
            if rc and payload["messages"][i].get("role") == "assistant":
                payload["messages"][i]["reasoning_content"] = rc
        return payload

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ):
        gc = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if gc is None:
            return None
        choices = chunk.get("choices") or []
        delta = choices[0].get("delta") if choices else None
        # sglang streams `reasoning_content`; newer vLLM renamed it `reasoning`.
        # Either way it's stored as reasoning_content (history/UI/keep_reasoning).
        rc = (delta.get("reasoning_content") or delta.get("reasoning")) \
            if isinstance(delta, dict) else None
        if rc:
            gc.message.additional_kwargs["reasoning_content"] = rc
        return gc

    # optional_extra: extra_body is a best-effort thinking-off switch (see
    # no_think) — a provider that 400s on it gets the request again without
    # it, and is remembered so later calls skip the 400.
    optional_extra: bool = False

    def _drop_optional(self, e: Exception) -> bool:
        if not (self.optional_extra and self.extra_body and isinstance(e, openai.BadRequestError)
                and "chat_template_kwargs" in str(e)):
            return False
        _REJECTS_KWARGS.add(self.openai_api_base or "")
        logger.warning("%s rejected chat_template_kwargs; asking without it", self.openai_api_base)
        self.extra_body = None
        return True

    async def _agenerate(self, *a, **kw):  # noqa: ANN002, ANN003
        try:
            return await super()._agenerate(*a, **kw)
        except Exception as e:
            if not self._drop_optional(e):
                raise
            return await super()._agenerate(*a, **kw)

    async def _astream(self, *a, **kw):  # noqa: ANN002, ANN003
        started = False
        try:
            async for c in super()._astream(*a, **kw):
                started = True
                yield c
        except Exception as e:
            if started or not self._drop_optional(e):
                raise
            async for c in super()._astream(*a, **kw):
                yield c


_REJECTS_KWARGS: set[str] = set()  # base_urls that 400'd on chat_template_kwargs
_VIEWS: dict[str, dict] = {}  # thread → its agent's latest model request (/btw)


def no_think(s: dict) -> dict:
    """Constructor kwargs for background calls that must not think (titles,
    recap, compaction, file assist). A Qwen hybrid thinks by DEFAULT, and
    these calls have small budgets — titles 24 tokens, recap 700 — so a
    thinking model spends all of it reasoning and returns empty content
    (seen: blank recaps, a compaction summary of "."). So the switch is sent
    even when the provider's template_kwargs box is off; that box governs
    the chat's thinking toggle, and an empty background result is never
    what anyone wants. Providers that reject it are retried without."""
    if s.get("template_kwargs", True):
        return {"extra_body": extra_body(s, thinking=False)}
    if (s.get("base_url") or "") in _REJECTS_KWARGS:
        return {"extra_body": None}
    return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}, "optional_extra": True}


REASONING_EFFORTS = ("xhigh", "medium", "low")
COMPACTION_TAG = "lb_compaction"


def extra_body(s: dict, thinking: bool | None = None) -> dict | None:
    """chat_template_kwargs for providers that take them (sglang / vLLM:
    the Qwen thinking switch + reasoning effort). Others — OpenAI itself,
    llama-server, Ollama — get nothing extra: OpenAI 400s on unknown args."""
    if not s.get("template_kwargs", True):
        return None
    on = bool(s.get("enable_thinking")) if thinking is None else thinking
    kw = {"enable_thinking": on}
    if s.get("keep_reasoning") == "turn":
        # Qwen3.8 template: preserve_thinking unset = render <think> on EVERY
        # past assistant message (empty, since we don't send old reasoning);
        # False = only on this turn's steps, which is what "turn" sends
        kw["preserve_thinking"] = False
    # Qwen3.8 template: xhigh (its default when unset) | medium | low;
    # anything else raises in the template → only pass known values
    if on and s.get("reasoning_effort") in REASONING_EFFORTS:
        kw["reasoning_effort"] = s["reasoning_effort"]
    return {"chat_template_kwargs": kw}


def model(s: dict) -> ChatOpenAI:
    # Always state enable_thinking explicitly. Qwen3-style hybrids think by
    # *default* — omitting the kwarg would keep reasoning streaming even when
    # the user turned thinking off (the sglang/vllm template flips per flag).
    vision = bool((s.get("capabilities") or {}).get("vision"))
    return SGlangChatOpenAI(
        model=s["model"],
        base_url=s["base_url"],
        api_key=s["api_key"],
        temperature=s["temperature"],
        max_tokens=s["max_tokens"],
        streaming=True,
        # sglang only sends usage in a final stream chunk when asked; we need
        # prompt/completion counts (and timing) for the per-turn speed readout.
        stream_usage=True,
        # langchain-openai's default (120s) is too tight for a single Spark
        # that may be queued behind other GPU work — a long silent gap is
        # prefill, not a dead peer. Kills truly hung connections at 10 min.
        stream_chunk_timeout=600,
        extra_body=extra_body(s),
        keep_reasoning=(s.get("keep_reasoning") or "off") if s.get("template_kwargs", True) else "off",
        # deepagents' read_file returns media as base64 content blocks and
        # only scrubs block types the profile marks False (missing = assumed
        # supported). Unset, a read_file on a .wav/.mp4 shipped ~350k tokens
        # of base64 audio to sglang → a 400 context overflow on EVERY later
        # turn of that thread. The scrub runs at model-call time over the
        # whole history, so declaring this also heals already-poisoned threads.
        profile={
            "image_inputs": vision,
            "image_tool_message": vision,
            "audio_inputs": False,
            "video_inputs": False,
            "pdf_inputs": False,
        },
    )


def summarizer(s: dict) -> ChatOpenAI:
    """Cheap non-streaming call used to compress old context (no thinking)."""
    return SGlangChatOpenAI(
        model=s["model"],
        base_url=s["base_url"],
        api_key=s["api_key"],
        temperature=0.3,
        max_tokens=int(s.get("compact_summary_tokens", 800)),
        streaming=False,
        **no_think(s),
    )


_SUMMARY_INSTRUCTION = (
    "You compress chat history. Write a dense, factual summary of the "
    "conversation below. Preserve: the user's goals and constraints, key facts "
    "and decisions, commands run with their results, file paths, open "
    "questions, and anything the user explicitly asked to remember. "
    "No preamble, no commentary — just the summary."
)


def _text_only(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "image_url":
                    parts.append("[image]")  # never re-send base64 to summarizer
        return "\n".join(parts)
    return str(content)


def _transcript(msgs: list) -> str:
    """Flatten messages to plain text for the summarizer (bounded size)."""
    lines = []
    for m in msgs:
        who = {"human": "USER", "ai": "ASSISTANT", "tool": "TOOL", "system": "SYSTEM"}.get(
            m.type, m.type
        )
        text = _text_only(m.content).strip()
        if isinstance(m, AIMessage) and m.tool_calls:
            calls = "; ".join(
                f"{tc['name']}({json.dumps(tc['args'], ensure_ascii=False)[:300]})"
                for tc in m.tool_calls
            )
            text = (text + "\n" if text else "") + f"[called {calls}]"
        if isinstance(m, ToolMessage):
            text, who = text[:1500], f"TOOL[{m.name}]"
        lines.append(f"{who}: {text[:1200]}")
    return "\n\n".join(lines)


def _ctx_tokens(msgs: list) -> int:
    """Real-ish size of the next prompt.

    count_tokens_approximately (chars/4) ran ~2.05x LOW on tool-heavy threads
    (emails, HTML, JSON — thread 28360d6bf583: estimated 111k while sglang
    reported 228k), so the 120k compaction trigger never fired and the run
    died at the 262k context limit. sglang's real prompt count rides on every
    AIMessage (usage_metadata). Model: real ≈ overhead (system prompt + tool
    schemas, ~13k here) + ratio × approx(messages). Overhead comes from the
    first trusted reply, ratio from the latest; the estimate is the latest
    real count plus ratio × what was appended since. Kept-after-compaction
    messages have their (stale) usage stripped by the hook, so only replies
    made after the last compaction are ever trusted."""
    return _ctx_model(msgs)[0]


def _ctx_model(msgs: list) -> tuple[int, float]:
    """(_ctx_tokens estimate, ratio real-tokens-per-approx-token for message
    bodies — system prompt/tool-schema overhead excluded)."""
    approx = count_tokens_approximately(msgs)
    trusted = [(i, m.usage_metadata) for i, m in enumerate(msgs)
               if isinstance(m, AIMessage) and (getattr(m, "usage_metadata", None) or {}).get("input_tokens")]
    if not trusted:
        return approx, 1.0
    i0, u0 = trusted[0]
    overhead = max(0, int(u0["input_tokens"]) - count_tokens_approximately(msgs[:i0]))
    i, u = trusted[-1]
    real = int(u["input_tokens"]) + int(u.get("output_tokens") or 0)
    body = count_tokens_approximately(msgs[: i + 1])
    ratio = min(3.0, max(1.0, (real - overhead) / max(1, body)))
    return max(approx, int(real + ratio * count_tokens_approximately(msgs[i + 1:]))), ratio


def _compaction_hook(s: dict):
    """pre_model_hook: fold old prefix into one summary when context gets long.

    LangGraph replays the whole thread into every model call — on a single
    Spark (~2k tok/s prefill) that means seconds of dead air per extra 10k
    tokens, forever. This node runs before each model call; past the trigger
    it archives the head to SQLite (UI still sees full transcript), and
    rewrites graph state to [summary] + recent tail via REMOVE_ALL_MESSAGES.
    """

    async def pre_model_hook(state: dict):
        msgs = state["messages"]
        trigger = int(s.get("compact_trigger_tokens", 40000))
        if trigger <= 0 or _ctx_tokens(msgs) < trigger:
            return None
        keep = max(4, int(s.get("compact_keep_messages", 20)))
        split = max(0, len(msgs) - keep)
        # Never orphan tool results: cut right BEFORE a model step (AIMessage)
        # or a user turn — a ToolMessage always follows its AIMessage, so
        # either boundary keeps every call/result pair on one side. (The old
        # rule allowed only user-message boundaries, so one long agentic turn
        # could never be folded: 'nothing safe to fold away yet', forever.)
        while split > 0 and not isinstance(msgs[split], (HumanMessage, AIMessage)):
            split -= 1
        head, tail = msgs[:split], msgs[split:]
        if not head:
            return None
        # cutting INSIDE the current turn folds away the user's own request —
        # carry it verbatim so the agent can't lose its task
        current = next((m for m in reversed(msgs[: split + 1]) if _turn_start(m)
                        and not (m.additional_kwargs or {}).get("lb_compacted")), None)
        in_turn = current is not None and current in head and not any(
            _turn_start(m) for m in tail)
        before, body_ratio = _ctx_model(msgs)
        t_sum = time.time()
        # tagged so run_chat doesn't log/stream it as an ordinary model step
        # (it used to show up as an unlabelled ASSISTANT row + usage line)
        summary = await summarizer(s).with_config(tags=[COMPACTION_TAG]).ainvoke(
            [
                SystemMessage(content=_SUMMARY_INSTRUCTION),
                HumanMessage(content=_transcript(head)),
            ]
        )
        t_sum = time.time() - t_sum
        text = _text_only(summary.content).strip()
        if len(text) < 40:
            # an empty/near-empty summary would replace the archived messages
            # with nothing — keep the full context; the next model step retries
            logger.warning("compaction summary came back empty (%r); not compacting", text)
            return None
        tid = (get_config() or {}).get("configurable", {}).get("thread_id", "")
        async with _wt(_db):
            for m in head:
                await _db.execute(
                    "INSERT INTO archived_messages(thread_id,msg) VALUES(?,?)",
                    (tid, json.dumps(_msg_dict(m), ensure_ascii=False)),
                )
        if in_turn:
            text += ("\n\n[CURRENT REQUEST — still in progress, quoted verbatim]\n"
                     + _text_only(current.content).strip()[:8000])
        note = HumanMessage(
            content=text,
            additional_kwargs={"lb_compacted": {"count": len(head)}},
        )
        # the kept tail's replies carry PRE-compaction prompt counts — strip
        # them, or _ctx_tokens would keep seeing the old size and re-fire
        tail = [m.model_copy(update={"usage_metadata": None}) if isinstance(m, AIMessage) else m
                for m in tail]
        um = getattr(summary, "usage_metadata", None) or {}
        # after = before − what the folded messages really cost (body ratio:
        # tool output tokenizes ~2x denser than chars/4) + the summary (prose,
        # ~1x). Scaling the whole remainder by before/approx inflated it with
        # the system-prompt overhead: a 1-msg fold read "14.9k → 20.1k".
        after = max(0, int(before - body_ratio * count_tokens_approximately(head)
                           + count_tokens_approximately([note])))
        note.additional_kwargs["lb_compacted"].update(
            {"before": before, "after": after, "kept": len(tail), "in_turn": in_turn})
        try:  # → run_chat: trajectory COMPACT row + live ⟲ card in the chat
            await adispatch_custom_event("lb_compacted", {
                "count": len(head), "kept": len(tail), "before": before,
                "after": after, "in_turn": in_turn,
                "dur": round(t_sum, 3), "tok_in": um.get("input_tokens"),
                "tok_out": um.get("output_tokens"), "summary": text[:4000],
            })
        except Exception:  # noqa: BLE001 - outside a traced run (offline tests)
            pass
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), note, *tail]}

    return pre_model_hook


class _CompactionMiddleware(AgentMiddleware):
    """Deep-agent twin of `_compaction_hook`: create_agent-based graphs
    (deepagents) take middleware instead of pre_model_hook."""

    def __init__(self, s: dict):
        super().__init__()
        self._hook = _compaction_hook(s)

    async def abefore_model(self, state, runtime):  # noqa: ANN001, ARG002
        return await self._hook(state)


# ---- steering: messages typed while a run is going ----
# POST /api/threads/{tid}/steer queues text here; _Steer delivers it as a real
# user message right before the run's next model call (after the current tool
# finishes), and run_chat gives it one more round if it lands after the
# model's last step. Never a hidden note: it is the user talking, saved in the
# thread where it landed.
_STEERS: dict[str, list[str]] = {}


def steer_put(thread_id: str, text: str) -> int:
    q = _STEERS.setdefault(thread_id, [])
    q.append(text)
    return len(q)


def steer_take(thread_id: str) -> list[str]:
    return _STEERS.pop(thread_id, None) or []


def _steer_msg(texts: list[str]) -> HumanMessage:
    return HumanMessage(content="\n\n".join(texts), additional_kwargs={"lb_steer": True})


def _turn_start(m) -> bool:
    """A HumanMessage that begins a user turn — a mid-run steer doesn't
    (it belongs to the turn it was typed into)."""
    return isinstance(m, HumanMessage) and not (m.additional_kwargs or {}).get("lb_steer")


class _Steer(AgentMiddleware):
    """Delivers queued steers before each main-agent model call."""

    async def abefore_model(self, state, runtime):  # noqa: ANN001, ARG002
        tid = ((get_config() or {}).get("configurable") or {}).get("thread_id")
        texts = steer_take(tid) if tid else []
        if not texts:
            return None
        m = _steer_msg(texts)
        try:  # → run_chat: trajectory USER row + the bubble where it landed
            await adispatch_custom_event("lb_steer", {"text": m.content})
        except Exception:  # noqa: BLE001 - outside a traced run (offline tests)
            pass
        return {"messages": [m]}


def _final_ai(resp):
    """The AIMessage a model-call wrapper's handler produced (ModelResponse |
    AIMessage | the factory's composed envelope), or None."""
    mr = resp if getattr(resp, "result", None) is not None else getattr(
        resp, "model_response", None
    )
    msgs = getattr(mr, "result", None)
    if msgs is None and isinstance(resp, AIMessage):
        msgs = [resp]
    return next((m for m in reversed(msgs or []) if isinstance(m, AIMessage)), None)


class _EmptyAnswerRetry(AgentMiddleware):
    """Re-ask a model step that came back with neither text nor tool calls.

    Qwen-family models write chat control tokens as the REAL token when they
    talk about them: thinking "ChatML starts with <|im_start|>…<|im_end|>"
    emits the end-of-message token mid-thought, sglang stops there, and the
    unclosed reasoning yields an empty answer (thread e93c1bb448e4, an IVR
    review about a leaked "<|im_start|>": three turns in a row died this
    way; 2 of 3 bare repros returned nothing). logit_bias can't ban the token
    — sglang refuses it under speculative decoding — so the step is retried
    with a call-local USER note (never persisted), at most twice per run.
    Innermost in the chain so the other middlewares see the good answer."""

    MAX = 2
    # A cut thought is RESUMED, not re-asked (2026-10-18, thread 32d984e9d567:
    # five turns ended empty on topics like llama.cpp special-token decoding;
    # neither the WARN nor the thread prompt stops a model reasoning ABOUT the
    # markers from typing one, and a re-ask walks the same path into the same
    # token). The thinking goes back as an open assistant turn with the marker
    # spelled safely, and the model carries on from there.
    MAX_RESUME = 4
    CUT = f"<{_WJ}|im_end|>"  # the stop token that ended it (EOS: vLLM reports no id)
    NOTE = ("[runtime] Your previous reply ended mid-thought with no answer and no "
            "tool call. Cause: you typed a chat control token verbatim (the "
            "message-start or message-end marker — angle bracket, pipe, im_start / "
            "im_end, pipe, angle bracket), and the server treats that as the end of "
            "your message. Never write those markers literally, in thinking or in "
            "answers: call them im_start / im_end, or write them with spaces like "
            "< |im_end| >. Now continue: answer the user or make your next tool call.")

    # The retry alone loses (live 2026-10-14: 1 of 3 trial threads still died
    # after both retries, 1 got an answer cut off at its own im_end). So while
    # the conversation is ABOUT these markers, every step carries a shorter
    # warning — after it the model spelled them with spaces. It rides in the
    # SYSTEM prompt: as a trailing user message the model took it for the
    # user talking and answered it ("Noted — spaced notation for the control
    # tokens from here on", threads 5fa2b2687abf, dc03fc31c2d2), even saved a
    # memory about it. Once on topic a thread stays on topic, so the system
    # prompt changes once and the prefix cache holds after that.
    TOPIC = re.compile(r"\bim_(?:start|end)\b|<\u2060?\|[A-Za-z_]{2,40}\|>|endoftext"
                       r"|<\u2060?/?think>")
    WARN = ("Chat control tokens (this conversation mentions them): typing one "
            "verbatim (angle bracket + pipe + im_start / im_end + pipe + angle "
            "bracket) ends your message on the spot, mid-sentence; typing the "
            "think / end-think tag (angle brackets around think or /think) ends "
            "your THINKING on the spot. In thinking and in answers write them with "
            "spaces, like < |im_end| > or < /think >, or just say im_end / end-think. "
            "This is a standing rule, not news from the user: never mention or acknowledge it.")
    # The </think> case doesn't come back empty: the thinking is cut where the
    # tag was typed and the REST of it arrives as answer text with no tool
    # call, so the run ends on half a thought. Signature: the reasoning stops
    # on an opening delimiter (`= "`, a backtick, `(`…) or mid-sentence, and
    # the "answer" picks up with the matching closer.
    SPILL_NOTE = ("[runtime] Your previous reply was cut: you typed the end-think tag "
                  "(angle brackets around /think) verbatim inside your thinking, so the "
                  "server ended your thinking there and the rest of it was shown to the "
                  "user as your answer. Never write that tag literally — say end-think "
                  "or write < /think > with spaces. Now continue: answer the user or "
                  "make your next tool call.")
    # a quote only OPENS after =, (, :, a comma or a space — a thought that
    # ends on a closing quote/backtick is finished (5 of 995 past answers)
    _OPEN_TAIL = re.compile(r"""(?:[=:(\[{,<]|(?:^|[\s=(\[{:,])["'`])\s*$""")
    _CLOSE_HEAD = re.compile(r"""^\s*(?:["'`>)\]}]|[a-z])""")
    _DONE_TAIL = re.compile(r"""[.!?)\]*`"'>:]\s*$""")

    def __init__(self):
        super().__init__()
        self._retries = 0
        self._resumes = 0

    def _on_topic(self, messages) -> bool:
        for m in messages:
            c = m.content if isinstance(m.content, str) else _text_only(m.content or "")
            if self.TOPIC.search(c) or self.TOPIC.search(
                    str((m.additional_kwargs or {}).get("reasoning_content") or "")):
                return True
        return False

    @classmethod
    def spilled(cls, ai) -> bool:
        """A final answer that is really the tail of a thinking block cut by a
        verbatim </think> (see SPILL_NOTE)."""
        if ai is None or ai.tool_calls:
            return False
        think = str((ai.additional_kwargs or {}).get("reasoning_content") or "").rstrip()
        text = _text_only(ai.content or "")
        if not think or not text.strip():
            return False
        if cls._OPEN_TAIL.search(think):
            return True
        return bool(cls._CLOSE_HEAD.search(text)) and not cls._DONE_TAIL.search(think)

    async def awrap_model_call(self, request, handler):  # noqa: ANN001
        if self._on_topic(request.messages):
            request = request.override(
                system_message=append_to_system_message(request.system_message, self.WARN))
        resp = await handler(request)
        while True:
            ai = _final_ai(resp)
            if ai is None or ai.tool_calls:
                return resp
            think = str((ai.additional_kwargs or {}).get("reasoning_content") or "")
            empty = not _text_only(ai.content or "").strip()
            if empty and think.strip() and self._resumes < self.MAX_RESUME:
                self._resumes += 1
                carry = think + self.CUT
                logger.warning("empty-answer: thinking cut by a control token, resuming %d/%d",
                               self._resumes, self.MAX_RESUME)
                resp = await handler(request.override(messages=[
                    *request.messages, AIMessage(content="", additional_kwargs={"lb_resume": carry})]))
                ai2 = _final_ai(resp)
                if ai2 is not None:
                    more = str((ai2.additional_kwargs or {}).get("reasoning_content") or "")
                    ai2.additional_kwargs["reasoning_content"] = carry + more
                continue
            if self._retries >= self.MAX:
                return resp
            if self.spilled(ai):
                note, why = self.SPILL_NOTE, "thinking spilled into the answer (verbatim </think>)"
            elif empty:
                note, why = self.NOTE, "model step returned nothing"
            else:
                return resp
            self._retries += 1
            logger.warning("empty-answer: %s, retry %d/%d", why, self._retries, self.MAX)
            resp = await handler(request.override(
                messages=[*request.messages, HumanMessage(content=note)]))


class _TodoReconcile(AgentMiddleware):
    """Deterministic finish-line gate for the todo list.

    Prompt advice (our DEEP_NOTE and the harness's own WRITE_TODOS_SYSTEM_
    PROMPT) proved advisory in live runs: Qwen3.8-flash did work and answered
    without ever reconciling write_todos, leaving the card stuck on a mid-run
    snapshot. This middleware wraps every model call and, when the model
    produces a FINAL answer (AIMessage without tool_calls) after doing real
    tool work this turn while the todo list still has open items, re-runs the
    call once with a hard reconciliation order appended. At most one retry
    per run — middleware is constructed per build_agent() call, i.e. per
    turn, so `_nudged` is per-run. A model that still refuses, or a retry
    that errors, gets the original answer plus the UI's ⚠ NOT UPDATED THIS
    RUN badge.

    Two live-test-earned constraints: the nudge rides as a USER message
    (sglang's Qwen template rejects mid-conversation system roles — 400
    "System message must be at the beginning"), and only non-write_todos
    tool work counts (a plan-only turn legitimately ends with everything
    open — that's what planning is).

    The intercepted answer is HELD, not regenerated (2026-10-01): the old
    retry discarded it and made the model answer again after write_todos —
    the user saw two full answers (the first had already streamed), voice
    auto-read raced between them, and Qwen spent ~50 s re-deriving facts
    for the copy. Now the nudge asks for ONLY the write_todos call; when
    that tool result comes back, the next model call is short-circuited and
    returns the held answer verbatim (persisted after the todo update, so
    history reads: todo card → answer). If the model instead starts other
    tool work, the hold is dropped and it answers again itself; if it
    ignores the order and answers, the ORIGINAL answer is delivered. Safe
    because amodel_node persists only what the wrap chain RETURNS."""

    def __init__(self):
        super().__init__()
        self._nudged = False
        self._held = None  # intercepted final answer, delivered after write_todos
        self._init_stage = 0  # 0=watching, 1=nudge #1 sent, 2=done (escalated or list exists)
        self._stale_nudges = 0  # staleness gate: max 2 per run, never a spiral

    @staticmethod
    def _real_tools(messages) -> int:
        """Count of real (non-write_todos) tool results after this turn's
        last human message. Scanning backwards stops at that human boundary,
        so earlier turns don't count."""
        n = 0
        for m in reversed(messages):
            if isinstance(m, ToolMessage):
                if (getattr(m, "name", None) or "") != "write_todos":
                    n += 1
            elif _turn_start(m):
                break
        return n

    @classmethod
    def _did_work(cls, messages) -> bool:
        return cls._real_tools(messages) > 0

    def _init_nudge(self, request):
        """PLANNING gate (the finish-line gate above can't fire for a run
        that never CREATED a list — the user's "won't make a todo list
        unless I prompt it"). Once the turn has 2+ real tool results and the
        list is still empty, append a USER-role nudge to the NEXT model call;
        if the model answers that call by KEEPING TOOLS (last message is a
        real ToolMessage again), escalate once — then stop either way (two
        nudges max, no spiral). Sub-agent-proof twice over: this middleware
        is only ever wired into the MAIN graph (build_agent's mw), and where
        the wrapper can see the tool list, write_todos must be in it. (A
        'todos' KEY in state proves the list was written — LangGraph omits
        never-written channels, so key presence proves too little and proves
        nothing here; an empty list is the real signal.) A model that answers
        anyway (genuinely near-done) is accepted: an answer is always legal."""
        tools = getattr(request, "tools", None)
        if tools:
            names = {getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
                     for t in tools}
            if "write_todos" not in names:
                return None
        if (request.state or {}).get("todos"):
            self._init_stage = 2  # list exists (model complied) — gate is done
            return None
        if self._init_stage >= 2:
            return None
        # First-person, hard-ordered phrasing (the tone the finish-line gate
        # earned live compliance with): the old "if 3+ steps left... answer
        # directly" wording let Qwen3.8 talk itself into "too small" on every
        # multi-echo test and keep answering list-free (seen 2026-09-27).
        if self._init_stage == 0:
            n = self._real_tools(request.messages)
            if n < 2:
                return None
            self._init_stage = 1
            logger.warning(
                "todo-init: %d tool calls with an empty list, forcing planning round", n
            )  # warning on purpose: no logging handler configured (lastResort)
            nudge = HumanMessage(content=(
                "[todo-enforcer] You have run " + str(n) + " tool calls in this task "
                "without ever calling write_todos. Unless the task is COMPLETE or a "
                "single obvious action, your very next tool call must be write_todos "
                "listing the remaining steps with accurate statuses — the user's "
                "progress bar depends on it. Then continue the work. Do not answer "
                "without either writing the list or finishing."
            ))
            return request.override(messages=[*request.messages, nudge])
        # stage 1: the model was warned. If its answer to that was ANOTHER
        # real tool call (not write_todos, not a final answer), escalate once.
        # (The stage-0 nudge itself isn't persisted — middleware message
        # overrides are call-local — so the tail here is the model's
        # post-warning tool_call + result.)
        last = request.messages[-1] if request.messages else None
        if isinstance(last, ToolMessage) and (getattr(last, "name", None) or "") != "write_todos":
            self._init_stage = 2
            logger.warning("todo-init: warning ignored, escalating (final round)")
            nudge = HumanMessage(content=(
                "[todo-enforcer] FINAL WARNING: you kept working after being told to "
                "maintain a todo list. Your next tool call must be write_todos with "
                "the remaining plan and statuses. Only if the task is fully complete "
                "may you answer instead."
            ))
            return request.override(messages=[*request.messages, nudge])
        return None

    @staticmethod
    def _tools_since_last_wt(messages) -> int:
        """Real tool results since the last write_todos result (scanning
        back; stops at that write_todos or at this turn's human boundary)."""
        n = 0
        for m in reversed(messages):
            if isinstance(m, ToolMessage):
                if (getattr(m, "name", None) or "") == "write_todos":
                    break
                n += 1
            elif _turn_start(m):
                break
        return n

    STALE_AFTER = 8  # real tool calls tolerated with an open list before nudging

    def _stale_nudge(self, request):
        """FRESHNESS gate (user report 2026-09-27: the card moved at run start
        and end only). A list EXISTS and has open items, but the model has
        done >= STALE_AFTER real tool calls without touching write_todos --
        the UI is showing a stale snapshot. Nudge it to batch write_todos in
        with its next tool calls. Bounded at 2 per run; a model that still
        ignores it is accepted (same no-spiral staged philosophy as the init
        gate; the finish-line gate is the backstop). Counting is relative to
        the last write_todos result, so compliance resets the gate naturally."""
        todos = (request.state or {}).get("todos") or []
        if not todos or self._stale_nudges >= 2:
            return None
        open_items = [t for t in todos if t.get("status") != "completed"]
        if not open_items:
            return None
        n = self._tools_since_last_wt(request.messages)
        if n < self.STALE_AFTER:
            return None
        self._stale_nudges += 1
        logger.warning(
            "todo-stale: %d tool calls since the last write_todos, nudge %d/2 "
            "(%d items open)", n, self._stale_nudges, len(open_items)
        )  # warning on purpose (no logging handler; lastResort = WARNING+)
        nudge = HumanMessage(content=(
            "[todo-enforcer] Your todo list is STALE: " + str(n) + " tool calls "
            "since your last write_todos while " + str(len(open_items)) + " item(s) "
            "are still open. Your next tool-call message MUST include write_todos "
            "(batch it with your other tool calls) with statuses matching reality: "
            "completed for every item you actually finished, in_progress for the "
            "one you are on. The user is watching this progress bar."
        ))
        return request.override(messages=[*request.messages, nudge])

    async def awrap_model_call(self, request, handler):  # noqa: ANN001
        if _gate_flags()[0]:
            # plan mode: read-only investigation IS the job and the plan goes
            # to exit_plan_mode — "make a todo list" nudges would fight that
            return await handler(request)
        if self._held is not None:
            held, self._held = self._held, None
            last = request.messages[-1] if request.messages else None
            if isinstance(last, ToolMessage) and (getattr(last, "name", None) or "") == "write_todos":
                logger.warning("todo-reconcile: list reconciled; delivering the held answer as-is")
                return held  # no model call: the answer already streamed once
            # anything else came back (other tools ran): the model will answer itself
        req = self._init_nudge(request)  # pre-handler: sees the pristine request
        if req is not None:
            request = req
        req = self._stale_nudge(request)  # freshness gate (list exists but stale)
        if req is not None:
            request = req
        resp = await handler(request)
        if self._nudged:
            return resp
        # handler results arrive as ModelResponse | AIMessage |
        # ExtendedModelResponse — or the factory's internal composed envelope,
        # _ComposedExtendedModelResponse (.model_response). Unwrap duck-tight.
        mr = resp if getattr(resp, "result", None) is not None else getattr(
            resp, "model_response", None
        )
        msgs = getattr(mr, "result", None)
        if msgs is None and isinstance(resp, AIMessage):  # bare-AIMessage return
            msgs = [resp]
        ai = next((m for m in reversed(msgs or []) if isinstance(m, AIMessage)), None)
        if ai is None or ai.tool_calls:
            return resp  # not a final answer — tool work still in flight
        if not self._did_work(request.messages):
            return resp  # nothing done this turn the list could reflect
        open_items = [
            t for t in (request.state or {}).get("todos") or []
            if t.get("status") != "completed"
        ]
        if not open_items:
            return resp
        self._nudged = True
        listed = "; ".join(
            f"[{t.get('status')}] {t.get('content')}" for t in open_items[:15]
        )
        nudge = HumanMessage(content=(
            "[todo-enforcer] Your answer above is final and will be delivered "
            "to the user exactly as written — do NOT repeat, re-check or "
            f"rewrite it. Your todo list still has {len(open_items)} open "
            f"item(s): {listed}. Reply with ONLY a write_todos call (no text) "
            "carrying the FULL list updated to match reality — completed for "
            "what this run actually finished, in_progress for anything "
            "mid-way, pending only for genuinely remaining work."
        ))
        logger.warning(
            "todo-reconcile: final answer intercepted, forcing one reconciliation (%d open items)",
            len(open_items),
        )  # warning-level on purpose: no logging handler is configured,
        # so INFO would sink to nowhere (logging lastResort = WARNING+)
        try:
            retry = await handler(request.override(messages=[*request.messages, nudge]))
        except Exception as e:  # noqa: BLE001 - never lose the user's answer
            logger.warning(
                "todo-reconcile: forced retry failed (%s); delivering original answer",
                str(e)[:200],
            )
            return resp
        rmr = retry if getattr(retry, "result", None) is not None else getattr(
            retry, "model_response", None)
        rmsgs = getattr(rmr, "result", None)
        if rmsgs is None and isinstance(retry, AIMessage):
            rmsgs = [retry]
        rai = next((m for m in reversed(rmsgs or []) if isinstance(m, AIMessage)), None)
        names = [tc.get("name") for tc in (rai.tool_calls if rai else [])]
        if names and all(n == "write_todos" for n in names):
            self._held = resp  # delivered on the call after the tool result
            return retry
        if names:
            return retry  # it wants more real work — it will answer again itself
        logger.warning("todo-reconcile: retry answered instead of updating; delivering original answer")
        return resp


# Provided by the deepagents harness itself in deep mode (on the real FS, with
# richer descriptions) — our same-named tools would collide on bind.
DEEP_REPLACED_TOOLS = {"read_file", "write_file"}


class _FileArgAlias(AgentMiddleware):
    """Rename drifted file-tool args BEFORE validation.

    The harness file tools take `file_path`; a code-agent-trained model
    sometimes reaches for Claude-Code's `path` instead (observed live:
    write_file({'path': …}) → ToolNode answers 'file_path: Field required'
    without the tool ever running). Renaming is safe only on the
    file_path-family tools — ls/glob/grep legitimately take `path`."""

    TOOLS = {"write_file", "read_file", "edit_file", "delete_file", "delete"}
    ALIAS = ("path", "filename", "file")

    # write_todos is the one tool whose only argument is a list of objects,
    # and sglang's qwen3_coder parser (Qwen's <parameter=todos>…</parameter>
    # XML) mangles it ~10 % of the time on long thinking-on runs (21 of ~200
    # calls 2026-09-28..10-03): 19× the args arrive as {} (nothing to
    # recover), 2× the whole list lands in the KEY with a trailing
    # '</parameter' — that shape is repaired exactly below.
    RESEND = ("Your write_todos call reached the server with NO arguments — the "
              "todo list was lost in transit (a tool-call parsing glitch, not your "
              "mistake). Re-send write_todos now with the full `todos` list "
              "(short items, each with content + status), then continue.")

    @staticmethod
    def _repair_todos(args: dict) -> dict | None:
        if not isinstance(args, dict) or "todos" in args:
            return None
        for k, v in args.items():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                return {"todos": v}  # right list, wrong key (todo / items / …)
            raw = k.strip()
            if raw.startswith("["):
                raw = re.sub(r"\s*</?parameter[^\]]*$", "", raw)
                try:
                    todos = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(todos, list):
                    return {"todos": todos}
        return None

    @staticmethod
    def _coerce_json_strings(tool, args: dict) -> dict | None:
        """sglang's qwen3_coder parser falls back to a STRING when it can't
        convert a parameter (ask_user arrived as questions='[{"question": …}]'
        → pydantic 'Input should be a valid list'). If the tool's schema
        says array/object and the string parses as JSON of that shape, hand
        the tool the parsed value."""
        props = getattr(tool, "args", None) or {}
        if not isinstance(args, dict) or not isinstance(props, dict):
            return None
        fixed, hit = dict(args), False
        for k, v in args.items():
            want = (props.get(k) or {}).get("type")
            if want not in ("array", "object") or not isinstance(v, str):
                continue
            try:
                val = json.loads(v.strip())
            except ValueError:
                continue
            if isinstance(val, list if want == "array" else dict):
                fixed[k], hit = val, True
        return fixed if hit else None

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001
        call = request.tool_call
        args = call.get("args")
        clean = undefang(args)
        if clean != args:  # model echoed defanged control tokens: real text for the tool
            call, args = {**call, "args": clean}, clean
            request = request.override(tool_call=call)
        coerced = self._coerce_json_strings(getattr(request, "tool", None), args)
        if coerced is not None:
            logger.warning("arg-repair: %s JSON-string args parsed (%s)", call.get("name"),
                           [k for k in coerced if coerced[k] is not args.get(k)])
            call = {**call, "args": coerced}
            args = coerced
            request = request.override(tool_call=call)
        if call.get("name") == "write_todos":
            fixed = self._repair_todos(args)
            if fixed is not None:
                logger.warning("arg-repair: write_todos list recovered from mangled args (%d items)",
                               len(fixed["todos"]))
                request = request.override(tool_call={**call, "args": fixed})
                return await handler(request)
        if (
            call.get("name") in self.TOOLS
            and isinstance(args, dict)
            and any(k in args for k in self.ALIAS)
        ):
            # rename only when the canonical key is absent; an alias present
            # ALONGSIDE file_path is a strict-schema extra -> just drop it.
            fixed = dict(args)
            for k in self.ALIAS:
                if k in fixed:
                    fixed.setdefault("file_path", fixed.pop(k))
            logger.warning(
                "arg-repair: %s %s -> file_path",
                call.get("name"),
                [k for k in self.ALIAS if k in args],
            )
            request = request.override(tool_call={**call, "args": fixed})
        try:
            res = await handler(request)
        except GraphBubbleUp:
            raise  # ask_user / plan gates pause the graph this way — not an error
        except Exception as e:  # noqa: BLE001
            # A tool that RAISES (vs. returning an error) used to kill the
            # whole run: an MCP call to a server the agent had itself just
            # stopped surfaced as 'ExceptionGroup: unhandled errors in a
            # TaskGroup', cancelled the parallel run_bash and ended the turn
            # (thread dc17b8aa6519). Hand the agent the ROOT cause instead
            # so it can retry, reconnect or tell the user.
            root = e
            while isinstance(root, BaseExceptionGroup) and root.exceptions:
                root = root.exceptions[0]
            while root.__cause__ is not None and not str(root):
                root = root.__cause__
            msg = f"{type(root).__name__}: {str(root)[:400]}" or type(root).__name__
            logger.warning("tool-raise: %s — %s", call.get("name"), msg)
            return ToolMessage(
                content=(f"ERROR: the {call.get('name')} tool crashed — {msg}. If it's an MCP "
                         "tool, its server may be down or restarting (MCP connections are "
                         "made at the start of a run: a server moved/restarted mid-run only "
                         "comes back on the next turn). Don't retry blindly — check, or "
                         "tell the user."),
                name=call.get("name"), tool_call_id=call.get("id"), status="error")
        # ToolNode converts invocation/validation failures into error
        # ToolMessages instead of raising; breadcrumb them (the astream
        # loop separately surfaces on_tool_error events to UI+trajectory).
        if (isinstance(res, ToolMessage) and getattr(res, "status", None) == "error"
                and call.get("name") == "write_todos" and not (args or {}).get("todos")):
            # the pydantic dump ('Field required [type=missing, input_value=
            # {'runtime': ToolRuntime(…)}…') told the model nothing useful
            logger.warning("tool-error: write_todos arrived with empty args — asking for a resend")
            return res.model_copy(update={"content": self.RESEND})
        if isinstance(res, ToolMessage) and getattr(res, "status", None) == "error":
            logger.warning(
                "tool-error: %s kwargs=%.120s — %.200s",
                request.tool_call.get("name"),
                str(request.tool_call.get("args") or {}),
                res.content,
            )
        if (call.get("name") in ("write_file", "edit_file") and isinstance(res, ToolMessage)
                and getattr(res, "status", None) != "error"):
            # an agent-written SKILL.md with broken frontmatter silently
            # doesn't load (gmail-inbox-triage, 2026-10-04: unquoted ': ' in
            # the description) — say so while the agent can still fix it
            warn = learning.check_written_skill(str((request.tool_call.get("args") or {}).get("file_path") or ""))
            if warn:
                logger.warning("skill-check: %s", warn)
                res = res.model_copy(update={"content": f"{_text_only(res.content)}\n\n{warn}"})
        return res

def _render_stale(m):
    """Model-view copy of a HumanMessage with its ⏱ get-current note IN FRONT
    (it frames the request: read the situation, then the ask)."""
    st = (m.additional_kwargs or {}).get("lb_stale")
    if not st or not st.get("note"):
        return m
    c = m.content
    head = st["note"] + "\n\n---\n"
    if isinstance(c, str):
        c = head + c
    elif isinstance(c, list):
        c = [{"type": "text", "text": head}, *c]
    return m.model_copy(update={"content": c})


class _PrefetchNote(AgentMiddleware):
    """Show the model each human turn's auto-search hits (server/prefetch.py)
    and its ⏱ get-current note (projects.staleness). They live in the
    message's `lb_prefetch` / `lb_stale` kwargs and are rendered into the
    call-local copy only, so the stored message and its chat bubble stay as
    typed — and since every past turn renders identically on every call, the
    prompt prefix cache survives."""

    async def awrap_model_call(self, request, handler):  # noqa: ANN001
        from . import prefetch
        msgs, hit = [], False
        for m in request.messages:
            kw = (m.additional_kwargs or {}) if isinstance(m, HumanMessage) else {}
            if kw.get("lb_prefetch") or kw.get("lb_stale"):
                m, hit = _render_stale(prefetch.render(m)), True
            msgs.append(m)
        return await handler(request.override(messages=msgs) if hit else request)


class _MediaReadGuard(AgentMiddleware):
    """Keep binary media that read_file returns out of the model's context.

    deepagents' read_file answers media with a base64 content block, but
    ToolNode's msg_content_output only passes TOOL_MESSAGE_BLOCK_TYPES
    through as blocks — `audio`/`video` aren't on that list, so they get
    json.dumps'd into one giant STRING. The profile-driven scrub (see
    model()) only replaces blocks, so the string sailed through: a 260 KB
    .wav became ~300k tokens of text and every later turn of that thread
    400'd on context length (observed live, thread 0f455e549925).

    Tool side: swap such results for a short note before they're stored.
    Model side: the same swap over history, which heals threads
    that were poisoned before this guard existed."""

    @staticmethod
    def _note(m: ToolMessage) -> str | None:
        ak = m.additional_kwargs or {}
        mime = ak.get("read_file_media_type")
        if not mime or mime.startswith("image/"):
            return None  # images: real blocks, profile scrub handles vision-off
        return (
            f"[read_file: {ak.get('read_file_path', 'file')} is {mime} media — "
            "NOT attached (this model can't take audio/video input). To show "
            "it to the user, cite its absolute path in your reply (the UI "
            "renders an inline player); to inspect it, use run_bash "
            "(ffprobe / ffmpeg frame grabs).]"
        )

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001
        res = await handler(request)
        if isinstance(res, ToolMessage) and (note := self._note(res)):
            res = res.model_copy(update={"content": note})
        return res

    async def awrap_model_call(self, request, handler):  # noqa: ANN001
        msgs, hit = [], False
        for m in request.messages:
            if isinstance(m, ToolMessage) and (note := self._note(m)) and m.content != note:
                m, hit = m.model_copy(update={"content": note}), True
            msgs.append(m)
        return await handler(request.override(messages=msgs) if hit else request)


# ---- human gates: ask_user + plan mode ----
# Both tools pause the graph with langgraph `interrupt()`: the run ends with a
# `gate` event (run_chat checks the checkpoint for pending interrupts after the
# stream), the UI renders a question / plan-review card, and the answer comes
# back as Command(resume=...) through /api/threads/{tid}/resume — which may be
# hours later, after a restart: the pause lives in the SQLite checkpoint, not
# in memory. On resume LangGraph re-runs the tool and interrupt() returns the
# answer. Main agent only: create_deep_agent never hands custom middleware
# (or its tools) to `task` sub-agents, so a sub-agent can't block on a human.
#
# Per-run flags ride the graph config: lb_plan (plan mode: read-only +
# exit_plan_mode) and lb_nogate (scheduled runs — nobody is there to answer).

class _Question(BaseModel):
    question: str = Field(description="One clear question.")
    options: list[str] = Field(
        default_factory=list,
        description="2-5 short concrete choices (recommended first). The user "
        "can always type their own answer instead.")
    multi_select: bool = Field(
        default=False, description="True if several options may be picked.")


def _gate_flags() -> tuple[bool, bool]:
    try:
        c = (get_config() or {}).get("configurable") or {}
    except RuntimeError:  # outside a graph run
        c = {}
    return bool(c.get("lb_plan")), bool(c.get("lb_nogate"))


def _fmt_answers(questions: list[dict], ans) -> str:
    if not isinstance(ans, dict):
        ans = {"text": str(ans)}
    if ans.get("text") and not ans.get("answers"):
        return ("The user answered in free text (not per question):\n"
                + str(ans["text"]).strip())
    lines = ["The user answered:"]
    got = ans.get("answers") or []
    for i, q in enumerate(questions):
        a = got[i] if i < len(got) and isinstance(got[i], dict) else {}
        picked = [str(x) for x in (a.get("selected") or [])]
        extra = str(a.get("text") or "").strip()
        parts = picked + ([extra] if extra else [])
        lines.append(f"Q{i + 1}. {q.get('question')}\n   A: "
                     + ("; ".join(parts) if parts else "(skipped — use your judgment)"))
    return "\n".join(lines)


@tool
def ask_user(questions: list[_Question | str]) -> str:
    """Ask the user 1-4 questions and WAIT for the answers (a human gate:
    the run pauses until they reply). Use it when you are blocked on a
    decision only the user can make — an ambiguous requirement, a
    preference between real alternatives, anything destructive or
    irreversible — instead of guessing or ending your reply with a question.
    Batch related questions into ONE call and give concrete options. Never
    ask what you can find out yourself with tools."""
    # bare strings tolerated: local models often drop the object wrapper
    qs = [q.model_dump() if isinstance(q, BaseModel)
          else {"question": q, "options": [], "multi_select": False} if isinstance(q, str)
          else dict(q) for q in questions][:4]
    ans = interrupt({"kind": "ask", "questions": qs})
    return _fmt_answers(qs, ans)


@tool
def exit_plan_mode(plan: str) -> str:
    """PLAN MODE ONLY. Present your finished plan (Markdown: goal, ordered
    steps, files/services touched, risks, how you'll verify) for the user's
    approval and WAIT. The user either approves — plan mode ends and you
    implement it right away — or sends revision notes."""
    ans = interrupt({"kind": "plan", "plan": plan})
    if not isinstance(ans, dict):
        ans = {"text": str(ans)}
    fb = str(ans.get("feedback") or ans.get("text") or "").strip()
    if ans.get("approved"):
        return ("The user APPROVED the plan" + (f", adding: {fb}" if fb else "")
                + ". Plan mode is OFF — implement it now: write_todos from the "
                "plan's steps, then execute and verify each one.")
    return ("NOT approved — the user wants changes:\n" + (fb or "(no details given)")
            + "\nYou are still in plan mode (read-only). Revise the plan — "
            "ask_user if anything is unclear — and call exit_plan_mode again.")


GATE_TOOLS = {"ask_user", "exit_plan_mode"}
PLAN_BLOCKED = {
    "write_file", "edit_file", "delete_file", "delete",
    "create_scheduled_task", "update_scheduled_task", "delete_scheduled_task",
    "set_scheduled_task_enabled", "run_scheduled_task_now",
}
PLAN_NOTE = """## PLAN MODE (active — set by the user)
Investigate and design; do NOT change anything yet.
- Read-only: read files, ls/glob/grep, crawl, and run_bash ONLY to inspect
  (no writes, installs, git commits/pushes, restarts, moves or deletes).
  write_file/edit_file and scheduled-task changes are blocked outright.
- Decisions that are genuinely the user's: ask_user (batched, with options).
- When the plan is ready, call exit_plan_mode with the full plan in
  Markdown (the plan goes IN the call — don't write plan files). Do not
  start implementing until it comes back APPROVED."""

# Thread 28360d6bf583 (2026-10-06): earlier turns ran in plan mode, so the
# history is full of "I'm in plan mode" / exit_plan_mode. In a later NORMAL
# run the model kept re-deciding it was in plan mode ("Plan mode is active
# again — I've paused…") between real work steps, called exit_plan_mode,
# and wrote plan files. Plan state is a per-run flag; say so explicitly
# whenever history could confuse it.
PLAN_OFF_NOTE = """## Plan mode: OFF for this run
The user's plan-mode toggle is off. Earlier messages in this thread that
talk about plan mode are from PAST turns — plan mode does not switch on by
itself mid-run, and nothing in tool output changes it. Don't pause for
approval, don't call exit_plan_mode, don't write plan files: do the work
(ask_user only for a decision that is genuinely the user's)."""


def _mentions_plan(msgs) -> bool:  # noqa: ANN001
    for m in reversed(msgs or []):
        if isinstance(m, ToolMessage) and m.name == "exit_plan_mode":
            return True
        if isinstance(m, AIMessage) and any(tc.get("name") == "exit_plan_mode" for tc in m.tool_calls or []):
            return True
        if isinstance(m, AIMessage) and "plan mode" in _text_only(m.content).lower():
            return True
    return False


def _tname(t) -> str | None:  # noqa: ANN001
    return getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)


class _HumanGate(AgentMiddleware):
    """Registers ask_user/exit_plan_mode, trims them per run flags, injects
    PLAN_NOTE into the system prompt while plan mode is on, and hard-blocks
    the mutating tools in plan mode (run_bash stays available for
    inspection — its read-only rule is prompt-level; the shell is trusted
    by design, see README ⚠ Security)."""

    def __init__(self):
        super().__init__()
        self.tools = [ask_user, exit_plan_mode]

    async def awrap_model_call(self, request, handler):  # noqa: ANN001
        plan, nogate = _gate_flags()
        drop = GATE_TOOLS if nogate else (set() if plan else {"exit_plan_mode"})
        if plan and not nogate:
            drop = drop | PLAN_BLOCKED  # don't even offer them
        tools = [t for t in request.tools if _tname(t) not in drop]
        if len(tools) != len(request.tools):
            request = request.override(tools=tools)
        if plan and not nogate:
            request = request.override(
                system_message=append_to_system_message(request.system_message, PLAN_NOTE))
        elif _mentions_plan(request.messages):
            request = request.override(
                system_message=append_to_system_message(request.system_message, PLAN_OFF_NOTE))
        return await handler(request)

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001
        plan, nogate = _gate_flags()
        name = request.tool_call.get("name")
        msg = None
        if plan and name in PLAN_BLOCKED:
            msg = (f"BLOCKED: {name} changes things and plan mode is read-only. "
                   "Put this step in your plan and call exit_plan_mode; it runs "
                   "after the user approves.")
        elif nogate and name in GATE_TOOLS:
            msg = ("No user is present (scheduled run) — decide yourself, and "
                   "state the assumption you made in your answer.")
        elif not plan and name == "exit_plan_mode":
            msg = ("Plan mode is OFF for this run (it is the user's toggle and never "
                   "turns on by itself). Nothing is waiting for approval — continue "
                   "executing the task.")
        if msg:
            return ToolMessage(content=msg, name=name, status="error",
                               tool_call_id=request.tool_call["id"])
        return await handler(request)


async def pending_gates(thread_id: str) -> list[dict]:
    """Interrupts waiting in the thread's checkpoint (a question / plan the
    user hasn't answered). Survives restarts — it's checkpoint state."""
    a = await _read_agent()
    snap = await a.aget_state({"configurable": {"thread_id": thread_id}})
    return [{"id": i.id, "value": i.value} for i in (snap.interrupts or ())]


# Nudge the planner to use the concurrency the harness already supports:
# ToolNode runs multiple tool calls from one message in parallel, and
# several `task` sub-agents dispatched together crawl/research concurrently.
DEEP_NOTE = (
    "\n\nOpening move: a task that will take 3+ tool calls gets write_todos "
    "with the plan BEFORE the first real action — the list is the user's "
    "progress bar; small one-shot questions need none."
    "\n\nParallelism: independent work goes in ONE message — several `task` "
    "sub-agents for independent research streams, and multiple tool calls "
    "when one result doesn't feed the next; they run concurrently. Batch "
    "write_todos status changes with the calls they describe. Finish line: "
    "an answer is not done until the todo list matches reality — before the "
    "final reply call write_todos (no items left pending/in_progress that "
    "are actually finished; after resuming an interrupted run, reconcile the "
    "list first)."
    "\n\nLarge writes: a write_file whose content is many KB can lose its "
    "arguments in transit (a failed call with empty args is a transport "
    "loss, not a bug — retry smaller). For files over ~100 lines, write a "
    "skeleton with write_file, then append sections via run_bash heredocs "
    "(cat >> path <<'EOF'). Same applies to write_todos: keep each todo item "
    "terse (a few words) — the WHOLE list rides on every call's args, and "
    "oversized args are what gets dropped, losing the update."
)


# Compact rewrite of the harness's SKILLS_SYSTEM_PROMPT: theirs is ~3x the
# size (roughly 0.2s of Spark prefill per model call, every call) and talks
# about "Deepagents"/"Agents" sources we don't have. SkillsMiddleware requires
# all three {slots}. The Hermes-read-only / LangBang-writable split is the
# contract _skill_sources + the FilesystemPermission deny-rule in build_agent
# actually enforce.
SKILLS_NOTE = """## Skills

{skills_locations}{skills_load_warnings}

**Available Skills:**

{skills_list}

Progressive disclosure: the list shows name + description only. When a task
matches a skill, `read_file` the path shown under it (`limit=1000`) and
follow it; use absolute paths for any helper files.

Sources labeled **Hermes** belong to the `hermes` CLI's skill tree and are
READ-ONLY for you — never write there; installing/updating them is the
user's `hermes` job. The **Langbang** source is yours: when you work out a
reusable multi-step procedure, save it as `<langbang dir>/<name>/SKILL.md`
(folder name == frontmatter `name`; YAML frontmatter with `name` plus a
`description` naming its trigger conditions), keep skills accurate — fix
one when you catch it wrong, and improve an existing skill rather than
duplicating it."""


def _skill_sources(s: dict) -> list[tuple[str, str]]:
    """Layered skill sources for SkillsMiddleware, last one wins: Hermes's
    tree first, LangBang's own dir last (so a LangBang skill can override a
    Hermes one by name).

    The middleware scans ONE level (`<source>/<skill>/SKILL.md`), but Hermes
    nests by category (`skills/<category>/<skill>/SKILL.md`), so every
    immediate subdir becomes its own source; category-less dirs and dirs
    without skill children are skipped silently by design. Dotted dirs
    (.hub, .curator_backups) are Hermes bookkeeping — skipped, or stale
    hub/backup copies would surface as ghost skills. All Hermes sources share
    the "Hermes" label (see _FreshSkillsLocations collapse); the LangBang dir
    is created eagerly — the prompt invites the agent to author into it."""
    hermes = os.path.expanduser(s.get("skills_hermes_dir") or "~/.hermes/skills")
    lb_dir = os.path.expanduser(s.get("skills_dir") or "~/.langbang/skills")
    os.makedirs(lb_dir, exist_ok=True)
    sources: list[tuple[str, str]] = []
    if os.path.isdir(hermes):
        sources.append((hermes, "Hermes"))
        sources += [
            (os.path.join(hermes, name), "Hermes")
            for name in sorted(os.listdir(hermes))
            if not name.startswith(".")
            and os.path.isdir(os.path.join(hermes, name))
        ]
    sources.append((lb_dir, "Langbang"))
    return sources


def _uncached_skill_state(state: dict) -> dict:
    """State copy without the skills cache keys — SkillsMiddleware skips its
    scan whenever `skills_metadata` is present ('once per session'), but
    LangBang threads persist for weeks while Hermes updates skills out of
    band, so we re-scan every turn (a few dozen stats, noise next to the
    model call it precedes)."""
    return {k: v for k, v in state.items()
            if k not in ("skills_metadata", "skills_load_errors")}


def _skill_update(update):  # noqa: ANN202
    """Parent returns skills_load_errors only when non-empty; force both keys
    so the replace-on-write channels can't keep a stale list from an
    earlier turn."""
    update = dict(update or {})
    update.setdefault("skills_metadata", [])
    update.setdefault("skills_load_errors", [])
    return update


class _FreshSkillsMiddleware(SkillsMiddleware):
    """SkillsMiddleware but the per-session metadata cache is defeated (see
    _uncached_skill_state). v1 deliberately covers the main agent only —
    `task` sub-agents get the vanilla harness, which here means no skills;
    the `skills=` kwarg on create_deep_agent can't be used instead, it would
    inject a second, vanilla SkillsMiddleware alongside this one."""

    def _format_skills_locations(self) -> str:
        """Collapse same-label sources to one line: the Hermes tree arrives
        as ~26 category-dir sources, and listing every path would spend
        ~400 prompt tokens on every single model call to say "look under
        ~/.hermes/skills". Parent's convention kept: one `**Label
        Skills**: `path`` line per label, "(higher priority)" on the last."""
        order: list[str] = []
        first: dict[str, str] = {}
        count: dict[str, int] = {}
        for path, label in zip(self.sources, self.source_labels, strict=True):
            if label not in first:
                first[label] = path
                order.append(label)
            count[label] = count.get(label, 0) + 1
        lines = [
            f"**{label} Skills**: `{first[label]}`"
            + (f" (+{count[label] - 1} category dirs)" if count[label] > 1 else "")
            + (" (higher priority)" if label == order[-1] else "")
            for label in order
        ]
        return "\n".join(lines)

    def before_agent(self, state, runtime, config):  # noqa: ANN001, ARG002
        return _skill_update(
            super().before_agent(_uncached_skill_state(state), runtime, config)
        )

    async def abefore_agent(self, state, runtime, config):  # noqa: ANN001, ARG002
        return _skill_update(
            await super().abefore_agent(_uncached_skill_state(state), runtime, config)
        )


def _fs_backend():
    """Real filesystem for the deep-agent file tools. No jail: the README
    already treats this server as an unsandboxed personal LAN tool (run_bash
    is plain `bash -lc`), and `virtual_mode=False` lets absolute paths work;
    relative paths resolve from $HOME like run_bash's cwd. Plain
    FilesystemBackend (not LocalShellBackend): `execute` is excluded via the
    harness profile, run_bash owns the shell."""
    return backends.FilesystemBackend(root_dir=os.path.expanduser("~"), virtual_mode=False)


async def build_agent(s: dict, checkpointer=None, thread_prompt: str = "", project: dict | None = None):
    enabled = s.get("local_tools") or {}
    deep = bool(s.get("deep_agent", True))
    skip = DEEP_REPLACED_TOOLS if deep else set()
    tools = [
        t
        for t in local_tools.LOCAL_TOOLS
        if enabled.get(t.name, True) and t.name not in skip
    ] + await mcp.get_tools(s.get("mcp_servers") or {})
    if s.get("memory_enabled", True):
        tools += learning.MEMORY_TOOLS
    from . import procs  # imports agent: deferred
    bg = [t for t in procs.TOOLS if enabled.get(t.name, True)]
    tools += bg
    cp = checkpointer or _checkpointer
    # built per run: memory edits and thread-prompt changes apply next turn
    # (also keeps the system prompt stable — and prefix-cached — mid-run)
    prompt = (s["system_prompt"] + learning.thread_note(thread_prompt)
              + local_tools.TOOLS_NOTE + (procs.NOTE if bg else "")
              + learning.memory_note(s, project["name"] if project else ""))
    if deep:
        mw = [TodoListMiddleware()]  # write_todos planning tool
        if s.get("compact_enabled", True):
            mw.append(_CompactionMiddleware(s))
        mw.append(_Steer())  # messages typed mid-run → before the next model call
        mw.append(_TodoReconcile())  # deterministic finish-line gate
        mw.append(_FileArgAlias())  # repair path->file_path arg drift pre-validation
        mw.append(_MediaReadGuard())  # audio/video base64 never reaches the model
        mw.append(_PrefetchNote())  # auto-search hints on human turns (prefetch.py)
        mw.append(_HumanGate())  # ask_user / plan mode (interrupt-based gates)
        perms = []
        if s.get("skills_enabled", True):
            mw.append(_FreshSkillsMiddleware(
                backend=_fs_backend(),
                sources=_skill_sources(s),
                system_prompt=SKILLS_NOTE,
            ))
            # make the prompt's "Hermes is read-only" promise real for the
            # harness file tools. Advisory next to run_bash (the agent could
            # overwrite skills via shell) — this fences honest mistakes, not
            # the model's own shell, which is the trust posture anyway.
            hermes = os.path.expanduser(s.get("skills_hermes_dir") or "~/.hermes/skills")
            perms = [FilesystemPermission(
                operations=["write"], paths=[hermes + "/**"], mode="deny")]
        mw.append(_EmptyAnswerRetry())  # last = innermost, right around the model
        main = model(s)
        main.lb_main_prompt = prompt  # → _capture (/btw replays this view)
        return create_deep_agent(
            main,
            tools,
            system_prompt=prompt + DEEP_NOTE,
            middleware=mw,
            backend=_fs_backend(),
            checkpointer=cp,
            permissions=perms,
        )
    return create_react_agent(
        model(s),
        tools,
        checkpointer=cp,
        prompt=SystemMessage(content=prompt),
        pre_model_hook=_compaction_hook(s) if s.get("compact_enabled", True) else None,
    )


_overview_cache: tuple = (0.0, None, None)


async def overview() -> dict:
    """NEW CHAT status panel (Hermes-banner style): what the agent would
    actually get on its next run. Tool names come from a real build_agent
    graph (its ToolNode), so harness tools, CONFIG toggles and MCP health
    are all reflected rather than re-derived. Cached 20 s per settings."""
    global _overview_cache
    s = config.load()
    key = json.dumps(s, sort_keys=True, default=str)
    if _overview_cache[1] == key and time.time() - _overview_cache[0] < 20:
        return _overview_cache[2]
    g = await build_agent(s)
    names = set(getattr(g.nodes["tools"].bound, "tools_by_name", {}) or {}) - {"execute"}  # hidden: see register_harness_profile
    servers = s.get("mcp_servers") or {}
    mcp_names = {n for v in mcp.by_server.values() for n in v}
    local_all = [t.name for t in local_tools.LOCAL_TOOLS]
    enabled = s.get("local_tools") or {}
    memory = {t.name for t in learning.MEMORY_TOOLS}
    tools = [
        {"group": "local", "on": [n for n in local_all if n in names],
         "off": [n for n in local_all if not enabled.get(n, True)]},
        {"group": "agent harness", "on": sorted(names - set(local_all) - mcp_names - memory), "off": []},
        {"group": "memory", "on": sorted(names & memory), "off": []},
    ]
    mcps = []
    for name, cfg in servers.items():
        st = ("off" if cfg.get("disabled") else "error" if name in mcp.last_errors
              else "ok" if name in mcp.by_server else "unloaded")
        mcps.append({"name": name, "state": st, "transport": cfg.get("transport") or "",
                     "tools": mcp.by_server.get(name, []) if st == "ok" else [],
                     "error": mcp.last_errors.get(name)})
    skills: dict[str, dict] = {}
    if s.get("skills_enabled", True):
        hermes_root = os.path.realpath(os.path.expanduser(s.get("skills_hermes_dir") or "~/.hermes/skills"))
        for src, label in _skill_sources(s):
            cat = ("langbang" if label == "Langbang"
                   else "hermes" if os.path.realpath(src) == hermes_root
                   else "hermes/" + os.path.basename(src))
            try:
                dirs = sorted(os.listdir(src))
            except OSError:
                continue
            for d in dirs:
                p = os.path.join(src, d, "SKILL.md")
                if d.startswith(".") or not os.path.isfile(p):
                    continue
                try:
                    meta = learning.parse_frontmatter(open(p, encoding="utf-8").read())[0] or {}
                except OSError:
                    meta = {}
                # same name later = override (last source wins, as in the middleware)
                skills[d] = {"name": d, "cat": cat, "desc": str(meta.get("description") or "")[:240]}
    cats: dict[str, list] = {}
    for sk in skills.values():
        cats.setdefault(sk["cat"], []).append({"name": sk["name"], "desc": sk["desc"]})
    out = {
        "model": s.get("model"), "provider": s.get("provider"),
        "deep_agent": bool(s.get("deep_agent", True)),
        "tools": tools, "tool_count": len(names),
        "mcp": mcps,
        "skills": [{"cat": c, "items": sorted(v, key=lambda x: x["name"])}
                   for c, v in sorted(cats.items(), key=lambda kv: (kv[0] != "langbang", kv[0]))],
        "skill_count": len(skills),
        "memories": len(learning.list_memories(s)) if s.get("memory_enabled", True) else None,
    }
    _overview_cache = (time.time(), key, out)
    return out


def _msg_dict(m) -> dict:
    d = {"role": m.type, "content": m.content}
    if isinstance(m, AIMessage) and m.tool_calls:
        d["tool_calls"] = [
            {"name": tc["name"], "args": tc["args"]} for tc in m.tool_calls
        ]
    if isinstance(m, ToolMessage):
        d["tool_name"] = m.name
        if getattr(m, "status", None) == "error":
            d["status"] = "error"  # chat labels it as a failure, not a result/answer
    reasoning = (m.additional_kwargs or {}).get("reasoning_content")
    if reasoning:
        d["thinking"] = reasoning
    compacted = (m.additional_kwargs or {}).get("lb_compacted")
    if compacted:
        d["compacted"] = compacted  # UI renders it as a ⟲ CONTEXT COMPACTED card
    shell = (m.additional_kwargs or {}).get("lb_shell")
    if shell:
        d["shell"] = shell  # UI renders it as a $ command card (see run_user_shell)
    sched = (m.additional_kwargs or {}).get("lb_sched")
    if sched:
        d["sched"] = sched  # UI stamps the bubble ⏰ SCHEDULED RUN (see schedule._fire)
    if (m.additional_kwargs or {}).get("lb_steer"):
        d["steer"] = True
    wk = (m.additional_kwargs or {}).get("lb_wake")
    if wk:
        d["wake"] = wk  # UI stamps the bubble ⚙ WOKEN BY (see procs._wake)
    merge = (m.additional_kwargs or {}).get("lb_merge")
    if merge:
        d["merge"] = merge  # UI stamps the bubble ⤴ MERGED FROM ⑂ (see merge_deliver)
    pf = (m.additional_kwargs or {}).get("lb_prefetch")
    if pf:
        d["prefetch"] = {k: v for k, v in pf.items() if k != "note"}  # ⌕ AUTO-SEARCH strip
    st = (m.additional_kwargs or {}).get("lb_stale")
    if st:
        d["stale"] = st  # ⏱ RESUMED strip (projects.staleness)
    return d


async def _read_agent():
    """Compiled deep-agent graph used only to load state — never invoked.

    deepagents' DeepAgentState declares `messages` as a DeltaChannel (deltas
    in the write log, full snapshots only every ~50 steps), so a raw
    checkpointer blob may have no `messages` key at all. Only Pregel's own
    state load (aget_state) replays the write log back into the channel.
    Any compiled graph whose messages channel is a DeltaChannel can read any
    thread: delta threads replay writes; plain threads seed from their
    materialized blob value. Built lazily with empty tools (no MCP, no
    local-tool wiring) since none of that affects the state schema.
    """
    global _read_graph
    if _read_graph is None:
        s = config.load()
        _read_graph = create_deep_agent(
            model(s), [], backend=_fs_backend(), checkpointer=_checkpointer
        )
    return _read_graph


async def _live_messages(thread_id: str) -> list:
    """Messages in live graph state (post-compaction), reconstructing the
    delta-channel case. Returns [] for unknown threads."""
    cfg = {"configurable": {"thread_id": thread_id}}
    tup = await _checkpointer.aget(cfg)
    if not tup:
        return []
    # Newer checkpointer returns a CheckpointTuple; older returns dict.
    cv = tup.get("channel_values") if isinstance(tup, dict) else tup.channel_values
    msgs = (cv or {}).get("messages")
    if isinstance(msgs, _DeltaSnapshot):
        msgs = None  # materialized snapshot blob, not a message list — replay it
    if msgs is None:
        try:
            g = await _read_agent()
            snap = await g.aget_state(cfg)
            msgs = (snap.values if snap else {}).get("messages")
        except Exception:  # never let a corrupt thread blank the whole UI
            logger.exception("delta-channel state load failed for %s", thread_id)
            msgs = None
    return list(msgs or [])


_MEDIA_BLOCKS = {"image", "audio", "video", "file"}


def _slim(d: dict) -> dict:
    """Strip binary payloads from a TOOL result before it goes to the UI.

    read_file on an image returns the whole base64 file as a content block;
    the chat card only ever renders text (blocks showed as empty), yet the
    UI downloaded every byte on each thread open — 57 MB for one thread of
    screenshot checks, 99.6 % of its transcript. Each block becomes a short
    note plus a `media` entry ({path, mime}) the card can show lazily via
    /api/media. Human messages keep their images (the user's own pastes)."""
    if d.get("role") != "tool" or not isinstance(d.get("content"), list):
        return d
    path = d.pop("read_file_path", None)
    content, media = [], []
    for b in d["content"]:
        if isinstance(b, dict) and b.get("type") in _MEDIA_BLOCKS:
            mime = b.get("mime_type") or b.get("type")
            n = len(b.get("base64") or b.get("data") or "") * 3 // 4
            content.append({"type": "text",
                            "text": f"[{b['type']} {mime}, {n / 1e6:.1f} MB"
                                    + (f": {path}" if path else "") + " — shown to the model]"})
            if path:
                media.append({"path": path, "mime": mime})
        elif isinstance(b, dict) and b.get("type") == "image_url":
            content.append({"type": "text", "text": "[image — shown to the model]"})
        else:
            content.append(b)
    out = {**d, "content": content}
    if media:
        out["media"] = media
    return out


# ---- archived (compacted-away) messages, shared across ⑂ forks ----
# A fork doesn't copy its source's archive: arch_base says "my archive starts
# with the first n rows of <base>'s" and archived_messages holds only the
# rows compacted in the fork itself. Archives only ever grow at the end, so
# a prefix never changes under a fork. A deleted thread whose rows a fork
# still uses keeps them (and its own arch_base row) until the last user goes.

async def _arch_raw(tid: str, n: int | None = None, _depth: int = 0) -> list[str]:
    """A thread's archived message JSON, oldest first (base prefix + own rows),
    or just its first n."""
    out: list[str] = []
    b = await (await _db.execute("SELECT base,n FROM arch_base WHERE tid=?", (tid,))).fetchone()
    if b and _depth < 64:
        out = await _arch_raw(b[0], b[1], _depth + 1)
    if n is not None and len(out) >= n:
        return out[:n]
    sql = "SELECT msg FROM archived_messages WHERE thread_id=? ORDER BY seq"
    args: tuple = (tid,)
    if n is not None:
        sql += " LIMIT ?"
        args = (tid, n - len(out))
    out += [r[0] for r in await (await _db.execute(sql, args)).fetchall()]
    return out


async def _arch_flatten(tid: str, n: int) -> tuple[str, int]:
    """Point at the thread that actually holds rows: a prefix lying wholly
    inside tid's own base belongs to that base (short chains, and a middle
    fork can be deleted without pinning anything)."""
    for _ in range(64):
        b = await (await _db.execute("SELECT base,n FROM arch_base WHERE tid=?", (tid,))).fetchone()
        if not b or n > b[1]:
            break
        tid = b[0]
    return tid, n


async def _arch_gc(tid: str) -> None:
    """Drop a deleted thread's archive unless a fork still reads it, then
    walk up its bases: a ghost (deleted, kept for forks) with no users left
    goes too. Runs inside the caller's _wt transaction."""
    for _ in range(64):
        if await (await _db.execute("SELECT 1 FROM threads WHERE id=?", (tid,))).fetchone():
            return  # still a live thread
        if await (await _db.execute("SELECT 1 FROM arch_base WHERE base=? LIMIT 1", (tid,))).fetchone():
            return  # a fork reads its rows: keep them as a ghost
        b = await (await _db.execute("SELECT base FROM arch_base WHERE tid=?", (tid,))).fetchone()
        await _db.execute("DELETE FROM archived_messages WHERE thread_id=?", (tid,))
        await _db.execute("DELETE FROM arch_base WHERE tid=?", (tid,))
        if not b:
            return
        tid = b[0]


async def _share_fork_archives() -> None:
    """One-time per fork (forks made before arch_base copied the archive):
    the leading rows identical to the source's archive become a shared
    prefix and the copies are deleted. Freed pages are reused by SQLite,
    the file itself doesn't shrink without a VACUUM."""
    cur = await _db.execute(
        "SELECT id, parent FROM threads WHERE parent IS NOT NULL "
        "AND id NOT IN (SELECT tid FROM arch_base)")
    for tid, parent in await cur.fetchall():
        try:
            src = json.loads(parent)["tid"]
        except (ValueError, KeyError, TypeError):
            continue
        if not await (await _db.execute("SELECT 1 FROM threads WHERE id=?", (src,))).fetchone():
            continue
        own = await (await _db.execute(
            "SELECT seq, msg FROM archived_messages WHERE thread_id=? ORDER BY seq", (tid,))).fetchall()
        if not own:
            continue
        theirs = await _arch_raw(src, len(own))
        k = 0
        while k < len(own) and k < len(theirs) and own[k][1] == theirs[k]:
            k += 1
        if not k:
            continue
        base, n = await _arch_flatten(src, k)
        async with _wt(_db):
            await _db.execute("INSERT INTO arch_base(tid,base,n) VALUES(?,?,?)", (tid, base, n))
            await _db.execute("DELETE FROM archived_messages WHERE thread_id=? AND seq<=?",
                              (tid, own[k - 1][0]))
        logger.warning("fork %s now shares %d archived rows with %s", tid, k, base)


async def history(thread_id: str) -> list:
    live = []
    for m in await _live_messages(thread_id):
        d = _msg_dict(m)
        if isinstance(m, ToolMessage) and (m.additional_kwargs or {}).get("read_file_path"):
            d["read_file_path"] = m.additional_kwargs["read_file_path"]
        live.append(_slim(d))
    # Pre-compaction originals were dropped from graph state; replay them so
    # the UI still shows the full transcript while the model sees the summary.
    archived = [_slim(json.loads(r)) for r in await _arch_raw(thread_id)]
    out = archived + live
    s = config.load()
    for d in out:  # 📘/🧠 chips on replayed tool cards (learning.consulted)
        for tc in d.get("tool_calls") or ():
            c = learning.consulted(tc.get("name") or "", tc.get("args"), s)
            if c:
                tc["consult"] = c
    return out


# ---- run trajectory (agentic visibility) ----

async def _prior_user_text(thread_id: str) -> str:
    """The thread's previous user message (from the trajectory log: one
    indexed row, no checkpoint replay). '' when none or on any error."""
    try:
        cur = await _edb.execute(
            "SELECT meta FROM run_events WHERE thread_id=? AND type='user' "
            "ORDER BY seq DESC LIMIT 1", (thread_id,))
        row = await cur.fetchone()
        await cur.close()
        m = json.loads(row[0]) if row and row[0] else {}
        return "" if m.get("gate") else str(m.get("text") or "")
    except Exception:  # noqa: BLE001
        return ""


async def _log(
    thread_id: str,
    turn_id: str,
    etype: str,
    name: str | None = None,
    dur: float | None = None,
    meta: dict | None = None,
    tok_in: int | None = None,
    tok_out: int | None = None,
    cache_read: int | None = None,
    ttft: float | None = None,
) -> None:
    """Append one trajectory row. Bookkeeping must never break the chat
    stream, so every failure is swallowed (the transcript in `messages`
    remains the source of truth)."""
    try:
        await _edb.execute(
            "INSERT INTO run_events(thread_id,turn_id,ts,type,name,dur,"
            "tok_in,tok_out,cache_read,ttft,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                thread_id, turn_id, time.time(), etype, name, dur,
                tok_in, tok_out, cache_read, ttft,
                json.dumps(meta, ensure_ascii=False, default=str) if meta is not None else None,
            ),
        )
        await _edb.commit()
    except Exception:  # noqa: BLE001
        # the swallow MUST roll back: a failed INSERT leaves the implicit
        # write txn (and the WAL writer lock) open on _edb — every later
        # writer on _db/`_edb` then times out "database is locked" (the
        # 2026-09-25 cascade started exactly here)
        try:
            await _edb.rollback()
        except Exception:  # noqa: BLE001
            pass


async def _release_tx() -> None:
    """Roll ALL connections back to a clean slate (the checkpointer's too: a
    task.cancel() can land inside an aput after the INSERT completed but
    before its queued commit — that half-open txn on _cpdb would hold the WAL
    writer lock, the exact wedge class below).

    WHY this exists: a failed write (any sqlite error, e.g. "database is
    locked") does NOT auto-rollback — the half-open write transaction stays
    open and keeps holding the WAL writer lock, so every later writer on
    every connection then times out too (the 2026-09-25 incident: one
    timeout cascaded into a self-sustaining wedge for HOURS). Call this in
    the run's error/cancel arms: a failed write there otherwise poisons the
    DB for every subsequent writer.

    _cpdb is rolled back UNDER the saver's own lock: _checkpointer/_cpdb are a
    shared singleton, so a concurrent hub could be mid-aput on it right now —
    an unsynchronized rollback would discard that healthy run's INSERT (the
    same corruption class this whole split exists to kill). The lock is the
    one place guaranteed to have no in-flight saver statement."""
    for c in (_edb, _db):
        try:
            await c.rollback()
        except Exception:  # noqa: BLE001 — last-resort cleanup
            pass
    if _cpdb is not None and _checkpointer is not None:
        async with _checkpointer.lock:
            try:
                await _cpdb.rollback()
            except Exception:  # noqa: BLE001
                pass


@asynccontextmanager
async def _wt(c: aiosqlite.Connection):
    """Write transaction that can never linger half-open.

    A failed sqlite statement does NOT auto-rollback (see _release_tx): the
    implicit write txn keeps holding the WAL writer lock and every later
    writer on every connection times out behind it. Run every multi-statement
    write through this — the endpoint still 500s on failure, but the wedge
    dies with the request instead of poisoning the DB for hours. Single
    statement + commit sites could equally use _set()-style rollback-swallow;
    this covers the helpers whose failure is what the 2026-09-26 cascade
    actually showed (touch/rename/compaction writes under concurrent runs).
    """
    try:
        yield
        await c.commit()
    except Exception:
        try:
            await c.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise


def _cap(x, n: int) -> str:
    """Stringify + truncate for trajectory meta previews (kept small so the
    event table stays bounded; full text lives in the chat transcript)."""
    t = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str)
    return t[:n]


async def trajectory(thread_id: str) -> dict:
    """Trajectory rows + thread totals for the Trajectory tab / stats bar.

    Events are the most recent window; totals aggregate the thread's FULL
    history so the display cap never skews the stats bar. `tool_s` excludes
    the `task` row — its dur already contains the sub-agent's inner model +
    tool rows (counting both would double-count). Both llm_s and tool_s are
    cumulative busy-times, not wall time.
    """
    cur = await _edb.execute(
        "SELECT turn_id,seq,ts,type,name,dur,tok_in,tok_out,cache_read,ttft,meta"
        " FROM run_events WHERE thread_id=? ORDER BY seq DESC LIMIT 4000",
        (thread_id,),
    )
    events = []
    for r in reversed(await cur.fetchall()):
        d = dict(
            zip(
                ("turn_id", "seq", "ts", "type", "name", "dur",
                 "tok_in", "tok_out", "cache_read", "ttft"),
                r[:10],
            )
        )
        try:
            d["meta"] = json.loads(r[10]) if r[10] else None
        except (TypeError, ValueError):
            d["meta"] = None
        events.append(d)
    t = await (
        await _edb.execute(
            "SELECT COUNT(DISTINCT turn_id),"
            " COALESCE(SUM(type IN ('model','tool')),0),"
            " COALESCE(SUM(CASE WHEN type IN ('model','compact') THEN dur END),0),"
            " COALESCE(SUM(CASE WHEN type='tool' AND name!='task' THEN dur END),0),"
            " COALESCE(SUM(tok_in),0), COALESCE(SUM(tok_out),0), COALESCE(SUM(cache_read),0),"
            " AVG(CASE WHEN type='model' THEN ttft END),"
            " COALESCE(SUM(type='model'),0)"
            " FROM run_events WHERE thread_id=?",
            (thread_id,),
        )
    ).fetchone()
    return {
        "events": events,
        "totals": {
            "turns": t[0],
            "steps": t[1],
            "llm_s": round(t[2], 1),
            "tool_s": round(t[3], 1),
            "tok_in": t[4],
            "tok_out": t[5],
            "cache_read": t[6],
            "ttft_avg": round(t[7], 2) if t[7] is not None else None,
            "model_calls": t[8],
        },
    }


# ---- thread bookkeeping ----

async def create_thread(title: str = "New chat", project: str | None = None) -> dict:
    tid = uuid.uuid4().hex[:12]
    now = time.time()
    if project and not await (await _db.execute("SELECT 1 FROM projects WHERE id=?", (project,))).fetchone():
        project = None  # a project deleted under the picker: file it nowhere
    # orig = seed title: renames (manual/auto) never touch it; ⟲ restores it
    async with _wt(_db):
        await _db.execute(
            "INSERT INTO threads(id,title,created_at,updated_at,orig,project) VALUES(?,?,?,?,?,?)",
            (tid, title, now, now, title, project),
        )
    return {"id": tid, "title": title, "created_at": now, "updated_at": now, "project": project}


def prompt_overhead_tokens() -> int:
    """Approx size of what every model call carries on top of thread history:
    the system prompt + tools note (the tool schemas themselves add a bit
    more, uncounted here). Same estimator as compaction, so the UI's CTX chip
    and the compaction trigger speak the same units."""
    s = config.load()
    p = s["system_prompt"] + local_tools.TOOLS_NOTE
    if s.get("deep_agent", True):
        p += DEEP_NOTE
    return count_tokens_approximately([SystemMessage(content=p)])


# tid -> (latest checkpoint_id, ctx tokens w/o prompt overhead, n_msgs, chars)
_thread_stats: dict[str, tuple] = {}


async def list_threads() -> list:
    """Sidebar rows. The per-thread numbers need the LIVE graph state
    (_live_messages = aget_state + delta replay) — ~1.2 s for all 68 threads,
    and the UI polls this every 4 s per open tab, which kept the event loop
    and DB busy exactly when a thread switch needed /messages. Now cached
    per thread, keyed by its newest checkpoint_id (every superstep / compaction
    writes a new one), so only threads that actually changed are recomputed."""
    cur = await _db.execute("SELECT id,title,created_at,updated_at,orig,pinned,parent,archived,"
                            "project,drift FROM threads "
                            "ORDER BY pinned IS NULL, updated_at DESC")
    rows = await cur.fetchall()
    cur = await _db.execute(
        "SELECT thread_id, MAX(checkpoint_id) FROM checkpoints "
        "WHERE checkpoint_ns='' GROUP BY thread_id")
    latest = dict(await cur.fetchall())
    base = prompt_overhead_tokens()
    out = []
    for r in rows:
        key = latest.get(r[0])
        hit = _thread_stats.get(r[0])
        if not hit or hit[0] != key:
            # What the *next* model call would replay: live graph state (post-
            # compaction), not the full archived transcript the UI shows.
            msgs = await _live_messages(r[0])
            hit = (key, _ctx_tokens(msgs), len(msgs),
                   sum(len(str(getattr(m, "content", "") or "")) for m in msgs))
            _thread_stats[r[0]] = hit
        out.append(
            {"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3],
             "context_tokens": base + hit[1], "orig": r[4],
             # cheap content signal the sidebar uses to decide whether the ✕
             # needs a confirm click (see web/app.js refreshThreads)
             "n_msgs": hit[2], "chars": hit[3],
             "pinned": r[5] is not None, "parent": json.loads(r[6]) if r[6] else None,
             "archived": r[7], "project": r[8], "drift": json.loads(r[9]) if r[9] else None}
        )
    return out


async def archived_why(tid: str) -> str:
    """'' when the thread may run; else the user-facing reason it can't."""
    row = await (await _db.execute("SELECT archived,parent FROM threads WHERE id=?", (tid,))).fetchone()
    if not row or row[0] is None:
        return ""
    try:
        p = json.loads(row[1]) if row[1] else {}
    except ValueError:
        p = {}
    into = f" — it was ⤴ merged into “{p.get('title')}”" if p.get("merged") else ""
    return f"this thread is archived (read-only){into}. ↺ Unarchive it to continue here."


async def archive_thread(tid: str, on: bool) -> bool:
    """Archive / unarchive. False = no such thread."""
    async with _wt(_db):
        cur = await _db.execute("UPDATE threads SET archived=? WHERE id=?",
                                (time.time() if on else None, tid))
    return cur.rowcount > 0


async def pin_thread(tid: str, pinned: bool) -> bool:
    """★ pin/unpin. Pinned rows sort first (still newest-first among
    themselves); False = no such thread."""
    async with _wt(_db):
        cur = await _db.execute("UPDATE threads SET pinned=? WHERE id=?",
                                (time.time() if pinned else None, tid))
    return cur.rowcount > 0


def _dict_transcript(ds: list, budget: int = 60000) -> str:
    """_transcript for archived message DICTS (the originals are gone), newest
    kept when over budget — the fork continues from the end, so the end is
    what the model needs most."""
    lines = []
    for d in ds:
        role = d.get("role")
        if role == "system" or d.get("compacted"):
            continue
        who = {"human": "USER", "ai": "ASSISTANT", "tool": f"TOOL[{d.get('tool_name')}]"}.get(role, role)
        text = _text_only(d.get("content", "")).strip()
        calls = "; ".join(f"{tc['name']}({json.dumps(tc.get('args'), ensure_ascii=False)[:300]})"
                          for tc in d.get("tool_calls") or [])
        if calls:
            text = (text + "\n" if text else "") + f"[called {calls}]"
        lines.append(f"{who}: {text[:1200]}")
    out, n = [], 0
    for ln in reversed(lines):
        n += len(ln) + 2
        if n > budget:
            out.append(f"[… {len(lines) - len(out)} earlier messages omitted]")
            break
        out.append(ln)
    return "\n\n".join(reversed(out))


def _ui_text(content) -> str:
    """The text a chat bubble shows (web/app.js textOf) — what a live
    bubble's ⑂ sends back to be matched."""
    if isinstance(content, list):
        return "".join(b if isinstance(b, str) else (b.get("text") or "") if isinstance(b, dict) else ""
                       for b in content)
    return content if isinstance(content, str) else json.dumps(content)


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


async def fork_thread(tid: str, at: int | None = None, role: str = "", text: str = "") -> dict:
    """⑂ Fork: a NEW thread holding this one's conversation up to a point;
    the source is never touched.

    The point is a history() index (`at`, from a bubble rendered out of
    /messages) or — for a bubble streamed live, which has none — its role +
    text, matched from the end. On an assistant answer the fork keeps that
    message (plus its tool results, so no tool call dangles); on a user
    message it keeps everything BEFORE it and hands the text back for the
    composer (edit-and-resend).

    State is SEEDED, not copied: raw checkpoint rows are delta-chained and
    pruned (prune_checkpoints), so the fork gets the live messages as fresh
    graph input — the same as_node="__start__" append run_user_shell uses.
    A point inside the compacted region (originals only survive as lossy
    dicts in archived_messages) seeds a ⟲ summary note built from them."""
    cur = await _db.execute("SELECT title,prompt,model,project FROM threads WHERE id=?", (tid,))
    row = await cur.fetchone()
    if row is None:
        raise LookupError("no such thread")
    arch_raw = await _arch_raw(tid)
    arch = [json.loads(m) for m in arch_raw]
    live = await _live_messages(tid)
    hist = arch + [_msg_dict(m) for m in live]  # index-aligned with history()

    want = "human" if role == "user" else role
    def hit(i):
        d = hist[i]
        return (d.get("role") == want and not d.get("compacted") and not d.get("shell")
                and _norm(_ui_text(d.get("content", ""))) == _norm(text))
    if at is not None and 0 <= at < len(hist) and (not text or hit(at)):
        i = at
    elif text and want in ("human", "ai"):
        i = next((j for j in range(len(hist) - 1, -1, -1) if hit(j)), None)
        if i is None:
            raise ValueError("couldn't find that message in the thread any more")
    else:
        raise ValueError("bad fork point")
    kind = hist[i].get("role")
    if kind == "human":
        cut, carry = i, _ui_text(hist[i].get("content", ""))
    elif kind == "ai":
        cut, carry = i + 1, ""
        while cut < len(hist) and hist[cut].get("role") == "tool":
            cut += 1
    else:
        raise ValueError("fork from a user message or an answer")

    na = len(arch)
    if cut > na:
        keep_arch, seed = arch_raw, live[: cut - na]
    else:
        keep_arch = arch_raw[:cut]
        seed = [HumanMessage(
            content="Summary of the conversation so far (forked from an earlier thread; "
                    "the full transcript stays visible above):\n\n" + _dict_transcript(arch[:cut]),
            additional_kwargs={"lb_compacted": {"count": cut, "forked": True}})] if cut else []
    # todos AT the fork point: the last write_todos inside the kept span
    # (the channel itself only holds the newest list)
    todos = None
    if cut == len(hist):
        todos = await current_todos(tid)
    else:
        for m in reversed(seed):
            for tc in reversed(getattr(m, "tool_calls", None) or []):
                if tc["name"] == "write_todos" and isinstance(tc.get("args", {}).get("todos"), list):
                    todos = tc["args"]["todos"]
                    break
            if todos is not None:
                break

    new = uuid.uuid4().hex[:12]
    title = ("⑂ " + (row[0] or "chat").removeprefix("⑂ "))[:80]
    now = time.time()
    async with _wt(_db):
        await _db.execute(
            "INSERT INTO threads(id,title,created_at,updated_at,orig,prompt,model,parent,project) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (new, title, now, now, title, row[1], row[2],
             json.dumps({"tid": tid, "title": row[0], "at": i, "kept": cut}, ensure_ascii=False),
             row[3]))  # a fork starts in its source's project (projects.observe may move it on)
        if keep_arch:  # shared with the source, not copied (_arch_raw)
            base, n = await _arch_flatten(tid, len(keep_arch))
            await _db.execute("INSERT INTO arch_base(tid,base,n) VALUES(?,?,?)", (new, base, n))
    if seed or todos:
        upd = {"messages": seed}
        if todos:
            upd["todos"] = todos
        g = await _read_agent()
        await g.aupdate_state({"configurable": {"thread_id": new}}, upd, as_node="__start__")
    return {"id": new, "title": title, "created_at": now, "updated_at": now,
            "draft": carry, "kept": cut, "of": len(hist), "project": row[3]}


async def search_text(m: dict) -> str:
    """Flatten a history() message dict to searchable text."""
    t = _text_only(m.get("content", ""))
    if m.get("thinking"):
        t += "\n" + str(m["thinking"])
    for tc in m.get("tool_calls") or []:
        t += "\n" + tc["name"] + " " + json.dumps(tc["args"], ensure_ascii=False)
    return t


async def delete_thread(tid: str) -> None:
    async with _wt(_db):
        await _db.execute("DELETE FROM threads WHERE id=?", (tid,))
        await _db.execute("DELETE FROM btw WHERE thread_id=?", (tid,))
        await _db.execute("UPDATE procs SET thread_id=NULL, wake='none' WHERE thread_id=?", (tid,))
        await _arch_gc(tid)
    async with _wt(_edb):
        await _edb.execute("DELETE FROM run_events WHERE thread_id=?", (tid,))
    # took the checkpoint tables with it (300+ leaked threads before this)
    await _checkpointer.adelete_thread(tid)


async def _touch(thread_id: str, first_text: str) -> None:
    async with _wt(_db):
        cur = await _db.execute("SELECT title FROM threads WHERE id=?", (thread_id,))
        row = await cur.fetchone()
        if row is None:
            title = first_text[:60]
            now = time.time()
            await _db.execute(
                "INSERT INTO threads(id,title,created_at,updated_at,orig) VALUES(?,?,?,?,?)",
                (thread_id, title, now, now, title),
            )
        else:
            title = row[0]
            if title == "New chat" or title == "":
                # first message seeds BOTH title and orig (⟲ reverts here)
                await _db.execute(
                    "UPDATE threads SET title=?, orig=?, updated_at=? WHERE id=?",
                    (first_text[:60], first_text[:60], time.time(), thread_id))
            else:
                await _db.execute("UPDATE threads SET updated_at=? WHERE id=?", (time.time(), thread_id))


async def touch_thread(thread_id: str) -> None:
    """Resume-bump: opening a thread counts as current, so it sorts to the
    top of the sidebar. Unknown/deleted ids are a no-op — never resurrect."""
    async with _wt(_db):
        await _db.execute("UPDATE threads SET updated_at=? WHERE id=?", (time.time(), thread_id))


# ---- empty-thread sweep ----

async def _sweep_once() -> int:
    """Drop abandoned 'New chat' rows: default title, untouched for an hour,
    and empty on top of that (a real thread renamed to 'New chat' with
    messages survives). Lazy-create means new ones only appear from old
    clients or direct POSTs; _touch re-inserts the row on first chat, so a
    swept thread that gets used again resurrects itself."""
    cur = await _db.execute(
        "SELECT id FROM threads WHERE title IN ('New chat','') AND updated_at<?",
        (time.time() - 3600,))
    n = 0
    for (tid,) in await cur.fetchall():
        if not await _live_messages(tid):
            await delete_thread(tid)
            n += 1
    if n:
        logger.warning("swept %d empty New chat thread(s)", n)
    return n


def start_sweeper() -> None:
    """Hourly cleanup; first pass immediately after startup."""

    async def loop():
        while True:
            try:
                await _sweep_once()
            except Exception as e:
                logger.warning("thread sweep failed: %s", e)
            # checkpoint tables ride the same hourly beat (idle servers where
            # no hub ever finishes still get pruned; throttle lives inside)
            schedule_prune(force=True)
            await asyncio.sleep(3600)

    asyncio.get_running_loop().create_task(loop())


# ---- titles (✎ manual / ⚡ auto / ⟲ initial) + recap ----

def _one_shot(s: dict, max_tokens: int, temperature: float) -> SGlangChatOpenAI:
    """Non-streaming single completion, same construction as summarizer()."""
    return SGlangChatOpenAI(
        model=s["model"], base_url=s["base_url"], api_key=s["api_key"],
        temperature=temperature, max_tokens=max_tokens, streaming=False,
        **no_think(s),
    )


def _flat_transcript(msgs: list[dict], head: int, tail: int, cap: int = 350,
                     args: int = 0) -> str:
    """history()-style dicts → compact text for one-shot LLM calls. First
    `head` + last `tail` messages; tool noise folded to the tool name (plus
    the first `args` chars of its arguments when args > 0)."""
    sel = msgs if len(msgs) <= head + tail else msgs[:head] + msgs[-tail:]
    lines = []
    for m in sel:
        role = m.get("role")
        if role == "tool":
            lines.append(f"TOOL[{m.get('tool_name')}]: {m.get('content') or ''}"[:cap])
            continue
        if role == "system":
            continue
        text = _text_only(m.get("content") or "").strip()[:cap]
        calls = ", ".join(tc["name"] + (" " + json.dumps(tc.get("args"), ensure_ascii=False)[:args]
                                        if args else "") for tc in (m.get("tool_calls") or []))
        if calls:
            text = (text + " " if text else "") + f"[called: {calls}]"
        lines.append(f"{role.upper()}: {text}")
    return "\n".join(lines)


_TITLE_INSTRUCTION = (
    "You title conversations. From the transcript excerpt, write a label of at "
    "most 6 words that names what the conversation is ABOUT (its topic or "
    "outcome, not 'Chat' or 'Conversation'). Same language as the "
    "conversation. Reply with ONLY the title — no quotes, no period."
)


async def rename_thread(tid: str, title: str) -> str | None:
    """Manual rename (✎). Never touches orig — that's ⟲'s anchor."""
    title = (title or "").strip()
    if not title:
        raise ValueError("title must be non-empty")
    async with _wt(_db):
        cur = await _db.execute("UPDATE threads SET title=? WHERE id=?", (title[:200], tid))
        hit = cur.rowcount
    return title[:200] if hit else None


async def get_thread_model(tid: str) -> dict:
    """This thread's {provider, model} override ({} = use the global default)."""
    cur = await _db.execute("SELECT model FROM threads WHERE id=?", (tid,))
    r = await cur.fetchone()
    try:
        ov = json.loads(r[0]) if r and r[0] else {}
    except ValueError:
        ov = {}
    return {k: str(ov[k]) for k in ("provider", "model", "keep_reasoning") if ov.get(k)}


async def set_thread_model(tid: str, ov: dict | None) -> bool:
    ov = {k: str((ov or {}).get(k) or "").strip() for k in ("provider", "model", "keep_reasoning")}
    if ov["keep_reasoning"] not in config.KEEP_REASONING:
        ov["keep_reasoning"] = ""
    ov = {k: v for k, v in ov.items() if v}
    async with _wt(_db):
        cur = await _db.execute("UPDATE threads SET model=? WHERE id=?",
                                (json.dumps(ov) if ov else None, tid))
        hit = cur.rowcount
    return bool(hit)


async def set_thread_prompt(tid: str, text: str) -> bool:
    """Per-thread instructions (applied from the thread's next run)."""
    text = (text or "").strip()[: learning.THREAD_PROMPT_CAP]
    async with _wt(_db):
        cur = await _db.execute("UPDATE threads SET prompt=? WHERE id=?", (text or None, tid))
        hit = cur.rowcount
    return bool(hit)


async def auto_title(tid: str) -> str | None:
    """⚡: one cheap LLM call names the thread from a transcript slice."""
    msgs = await history(tid)
    if not any(m.get("role") in ("human", "ai") and _text_only(m.get("content") or "").strip()
               for m in msgs):
        raise ValueError("nothing to title yet")
    raw = await _one_shot(config.load(), 24, 0.4).ainvoke([
        SystemMessage(content=_TITLE_INSTRUCTION),
        HumanMessage(content=_flat_transcript(msgs, head=2, tail=6)),
    ])
    title = str(_text_only(raw.content)).strip().strip("\"'`")
    title = title.removeprefix("Title:").strip(" \t-–—:•")
    title = title.rstrip(".。").strip()[:80]
    if not title:
        raise RuntimeError("model returned an empty title")
    async with _wt(_db):
        cur = await _db.execute("UPDATE threads SET title=? WHERE id=?", (title, tid))
        hit = cur.rowcount
    return title if hit else None


async def revert_title(tid: str) -> str | None:
    """⟲: back to the seed title stored in orig."""
    cur = await _db.execute("SELECT orig FROM threads WHERE id=?", (tid,))
    row = await cur.fetchone()
    if not row:
        return None
    return await rename_thread(tid, row[0] or "New chat")


_RECAP_INSTRUCTION = (
    "You recap working sessions so someone can resume them cold. From the "
    "transcript write terse markdown with exactly these sections: "
    "**Goal** / **Established facts** / **Current state** / **Open items**. "
    "Keep specifics (paths, hosts, commands, numbers). No preamble."
)


async def recap_thread(tid: str) -> dict | None:
    """✦ RECAP: one-shot summary of the thread (archived + live)."""
    cur = await _db.execute("SELECT title FROM threads WHERE id=?", (tid,))
    row = await cur.fetchone()
    if not row:
        return None
    msgs = await history(tid)
    summary = await _one_shot(config.load(), 700, 0.3).ainvoke([
        SystemMessage(content=_RECAP_INSTRUCTION),
        HumanMessage(content=_flat_transcript(msgs, head=2, tail=40)),
    ])
    text = _text_only(summary.content).strip()
    if not text:
        raise RuntimeError("the model returned no text (finish: %s)"
                           % (summary.response_metadata or {}).get("finish_reason"))
    return {"title": row[0], "summary": text, "ts": time.time()}


_BTW_INSTRUCTION = (
    "[/btw — SIDE QUESTION, not a new task] The user is asking you a side "
    "question about this thread while the work goes on. Answer it briefly and "
    "concretely from everything above, plus the excerpts below from the "
    "thread's compacted (summarized-away) history. Do NOT call tools, do not "
    "continue or change the task, and don't propose new work unless asked — "
    "this exchange is not added to the thread. If the thread really doesn't "
    "say, say so plainly."
)
_BTW_PRIOR = 5  # earlier side exchanges fed into a new /btw
_BTW_STOP = set("""the and for are was were that this with from what which when where who how why
does did have has had not but you your our its it's can could would should will about into than then
them they there their been being any all just like also only some more most very much many such
used use using last first one two out yes no""".split())


def _btw_terms(q: str) -> list[str]:
    """Search terms from a side question: words of 3+ letters, plus short
    tokens holding a digit (v3, 32, t27 — exactly what these threads hinge on)."""
    out = []
    for w in re.findall(r"[a-z0-9][a-z0-9_.\-]*", q.lower()):
        w = w.strip(".-")
        if w and w not in _BTW_STOP and w not in out and (len(w) >= 3 or re.search(r"\d", w)):
            out.append(w)
    return out[:24]


def _btw_excerpts(q: str, rows: list[tuple[int, str]], budget: int = 12000) -> str:
    """Compacted-history passages for a side question: archived messages
    matching 2+ question terms (whole words), scored by summed idf (digit
    terms count 1.5x — v3/32/t27 are what these threads hinge on) times the
    share of terms hit; best few shown as a window around their rarest
    hit, in thread order. Ties go to the later message."""
    terms = _btw_terms(q)
    if not terms or not rows:
        return ""
    low = [t.lower() for _, t in rows]
    pat = {w: re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])") for w in terms}
    df = {w: sum(1 for t in low if pat[w].search(t)) for w in terms}
    idf = {w: math.log((len(rows) + 1) / (df[w] + 0.5)) * (1.5 if re.search(r"\d", w) else 1)
           for w in terms if df[w]}
    need = min(2, len(idf))
    scored = []
    for k, t in enumerate(low):
        hit = [w for w in idf if pat[w].search(t)]
        if hit and len(hit) >= need:
            scored.append((sum(idf[w] for w in hit) * len(hit) / len(idf), k,
                           min(hit, key=lambda w: df[w])))
    picked, used = [], 0
    for _, k, rare in sorted(scored, reverse=True)[:12]:
        text = rows[k][1]
        at = pat[rare].search(text.lower()).start()
        lo = max(0, at - 700)
        snip = ("…" if lo else "") + text[lo:lo + 1600] + ("…" if lo + 1600 < len(text) else "")
        if used + len(snip) > budget:
            break
        picked.append((k, snip))
        used += len(snip)
    return "\n\n".join(f"[earlier message {rows[k][0]}] {snip}" for k, snip in sorted(picked))


async def _btw_turns(q: str, tid: str) -> str:
    """Semantic hits among this thread's indexed turns (rag source=thread:
    user message + final answer), its ⑂ ancestors' too — a fork's early
    history was indexed under the thread it came from. Best effort."""
    from . import rag
    tids, cur_t = [tid], tid
    for _ in range(3):
        row = await (await _db.execute("SELECT parent FROM threads WHERE id=?", (cur_t,))).fetchone()
        try:
            cur_t = json.loads(row[0])["tid"] if row and row[0] else None
        except (ValueError, KeyError, TypeError):
            cur_t = None
        if not cur_t:
            break
        tids.append(cur_t)
    hits = []
    for t in tids:
        try:
            r = await asyncio.to_thread(rag.search, q, source="thread", scope="local", top_k=3,
                                        path_prefix=f"lb-thread:{t}/", timeout=6)
            hits += (r or {}).get("results") or []
        except Exception as e:  # noqa: BLE001 - a hint, never a failure
            logger.warning("btw turn search %s: %s", t, e)
            break
    hits.sort(key=lambda h: -(h.get("score") or 0))
    return "\n\n".join(f"[earlier turn] {(h.get('text') or '')[:1500]}" for h in hits[:3])


def _archive_rows(raw: list[str]) -> list[tuple[int, str]]:
    rows = []
    for n, r in enumerate(raw):
        try:
            m = json.loads(r)
        except ValueError:
            continue
        role = m.get("role") or m.get("type") or "?"
        text = _text_only(m.get("content") or "")
        calls = " ".join(json.dumps(tc.get("args"), ensure_ascii=False)[:600]
                         for tc in (m.get("tool_calls") or []))
        body = (text + (" [called] " + calls if calls else "")).strip()
        if body:
            rows.append((n, f"{str(role).upper()}: {body}"))
    return rows


def _btw_text_view(lm: list, vision: bool) -> list:
    """Live messages as a model request when there's no captured view
    (thread hasn't run since the server started): media blocks become a
    placeholder unless they're images on a vision model."""
    out = []
    for m in lm:
        c = m.content
        if isinstance(c, list):
            keep = []
            for b in c:
                if not isinstance(b, dict) or b.get("type") == "text":
                    keep.append(b)
                elif vision and b.get("type") in ("image", "image_url"):
                    keep.append(b)
                else:
                    keep.append({"type": "text", "text": f"[{b.get('type')} omitted]"})
            m = m.model_copy(update={"content": keep})
        out.append(m)
    return out


async def _ask_view(tid: str, ask: str, max_tokens: int) -> tuple[str, dict]:
    """One answer from the thread's own model view, with `ask` appended as a
    user message — nothing is written to the thread (/btw, ⤴ merge).

    The view is the agent's latest request (captured by _capture): same
    system prompt, tools and messages, so vLLM's prefix cache already holds
    nearly all of it, plus any messages since. With no capture (server
    restarted since the thread last ran) it's rebuilt from live state."""
    s = config.effective(config.load(), await get_thread_model(tid))
    live = await _live_messages(tid)
    view = _VIEWS.get(tid)
    m = model(s)
    if view:
        payload = {**view["payload"], "messages": list(view["payload"]["messages"])}
        ids = [x.id for x in live]
        if view["last_id"] in ids:  # tool results etc. since that model call
            newer = live[ids.index(view["last_id"]) + 1:]
            if newer:
                payload["messages"] += m._get_request_payload(newer)["messages"]
        client = view["client"]
    else:
        prompt = s["system_prompt"] + learning.thread_note(await learning.get_thread_prompt(_db, tid))
        payload = m._get_request_payload(
            [SystemMessage(content=prompt)]
            + _btw_text_view(live, bool((s.get("capabilities") or {}).get("vision"))))
        client = m.root_async_client
    payload["messages"].append({"role": "user", "content": ask})
    payload.pop("stream_options", None)
    payload.update(stream=False, temperature=0.3)
    payload.pop("max_completion_tokens", None)
    payload["max_tokens"] = max_tokens
    if payload.get("tools"):
        payload["tool_choice"] = "none"  # tools stay listed: they're part of the cached prefix
    eb = dict(payload.pop("extra_body", None) or {})
    if eb.pop("continue_final_message", None):  # captured mid-resume: drop the open turn
        eb.pop("add_generation_prompt", None)
        payload["messages"].pop(-2)
    kw = dict(eb.get("chat_template_kwargs") or {})
    if kw.get("enable_thinking"):
        kw["reasoning_effort"] = "low"  # an aside, not a work step
        eb["chat_template_kwargs"] = kw
    t0 = time.time()
    resp = await client.chat.completions.create(**payload, extra_body=eb or None, timeout=170)
    ch = resp.choices[0]
    text = _text_only(ch.message.content or "")
    text = text.split("</think>")[-1].strip()  # a provider without a reasoning parser
    if not text:
        raise RuntimeError("the model returned no text (finish: %s)" % ch.finish_reason)
    u = resp.usage
    det = getattr(u, "prompt_tokens_details", None) if u else None
    return text, {"ctx": u.prompt_tokens if u else None,
                  "cached": getattr(det, "cached_tokens", None) if det else None,
                  "secs": round(time.time() - t0, 1), "view": "agent" if view else "rebuilt"}


async def btw(tid: str, question: str) -> dict | None:
    """/btw: a side question answered from the thread's own model view
    (_ask_view), without touching the thread — the running agent never sees
    it. What compaction summarized away is searched for the question's terms
    and the best passages ride along. The exchange is banked in the `btw`
    table and the last few are included, so a follow-up can build on them."""
    cur = await _db.execute("SELECT title FROM threads WHERE id=?", (tid,))
    if not await cur.fetchone():
        return None
    excerpts = _btw_excerpts(question, _archive_rows(await _arch_raw(tid)))
    turns = await _btw_turns(question, tid)
    prior = (await btw_list(tid))[-_BTW_PRIOR:]
    ask = _BTW_INSTRUCTION
    if excerpts:
        ask += "\n\nEXCERPTS FROM THE COMPACTED HISTORY (matched on the question's terms):\n" + excerpts
    if turns:
        ask += "\n\nEARLIER TURNS OF THIS THREAD (semantic match; may repeat what's above):\n" + turns
    for b in prior:
        ask += f"\n\nEARLIER SIDE QUESTION: {b['q']}\nYOUR ANSWER: {b['answer'][:1500]}"
    ask += "\n\nSIDE QUESTION: " + question
    text, meta = await _ask_view(tid, ask, 4000)
    ts = time.time()
    async with _wt(_db):
        cur = await _db.execute("INSERT INTO btw(thread_id,ts,q,answer) VALUES(?,?,?,?)",
                                (tid, ts, question, text))
        bid = cur.lastrowid
    return {"id": bid, "q": question, "answer": text, "ts": ts, **meta,
            "excerpts": excerpts.count("[earlier message ") + turns.count("[earlier turn]")}


# ---- ⤴ merge: a ⑂ branch hands what it learned back to its parent ----
_MERGE_INSTRUCTION = (
    "[⤴ MERGE — write a hand-off note, not a new task] This thread is a ⑂ branch "
    "of the thread “{ptitle}”. The branch's own work {since} begins at the user "
    "message quoted below; everything before that point the parent already has. "
    "Write a hand-off note for the parent thread's agent about ONLY the branch's "
    "own work: what was found or decided (with the evidence/numbers), what changed "
    "on disk (exact paths), commands or services that matter and their state, "
    "and what is still open. Dense markdown, no preamble, no sign-off. Do NOT "
    "call tools.\n\nTHE BRANCH'S OWN WORK STARTS AT:\n{start}"
)


async def _branch_info(tid: str) -> tuple[dict, str]:
    row = await (await _db.execute("SELECT title,parent FROM threads WHERE id=?", (tid,))).fetchone()
    if row is None:
        raise LookupError("no such thread")
    try:
        p = json.loads(row[1]) if row[1] else None
    except ValueError:
        p = None
    if not p or not p.get("tid"):
        raise ValueError("this thread isn't a ⑂ fork — there is no parent to merge into")
    if not await (await _db.execute("SELECT 1 FROM threads WHERE id=?", (p["tid"],))).fetchone():
        raise ValueError(f"the parent thread “{p.get('title')}” was deleted")
    if await archived_why(p["tid"]):
        raise ValueError(f"the parent thread “{p.get('title')}” is archived — unarchive it first")
    return p, row[0]


async def merge_preview(tid: str) -> dict:
    """Draft the hand-off note (the branch's own model view, _ask_view) for
    the user to read/edit before merge_deliver sends it."""
    p, title = await _branch_info(tid)
    hist = await history(tid)
    merged = p.get("merged_upto")
    start = merged if merged is not None else p.get("kept", p.get("at", -1) + 1)
    own = hist[start:]
    if not any(d.get("role") == "ai" and _ui_text(d.get("content", "")).strip() for d in own):
        raise ValueError("nothing new on this branch since the "
                         + ("last merge" if merged is not None else "fork"))
    first = next((d for d in own if d.get("role") == "human" and not d.get("shell")
                  and not d.get("compacted")), None)
    quote = _ui_text(first.get("content", ""))[:1500] if first else "(the first message after the fork)"
    ask = _MERGE_INSTRUCTION.format(
        ptitle=p.get("title") or "parent",
        since="since its last merge" if merged is not None else "since the fork", start=quote)
    note, meta = await _ask_view(tid, ask, 2500)
    return {"note": note, "parent": {"tid": p["tid"], "title": p.get("title")},
            "upto": len(hist), "again": merged is not None, **meta}


async def merge_deliver(tid: str, note: str, upto: int, parent_running: bool) -> dict:
    """Send the (possibly edited) hand-off to the parent: mid-run it lands as
    a steer before the parent's next step, otherwise it's appended to the
    parent's state like a !cmd card (the agent reads it next turn). The
    branch remembers how far it has merged, so a later merge only covers
    what came after."""
    p, title = await _branch_info(tid)
    note = note.strip()
    if not note:
        raise ValueError("empty hand-off note")
    ptid = p["tid"]
    text = f"⤴ Merged from ⑂ branch “{title}”:\n\n{note}"
    turn_id = uuid.uuid4().hex[:12]
    await _log(ptid, turn_id, "user", meta={"text": _cap(text, 800), "merge": tid})
    if parent_running:
        steer_put(ptid, text)
        how = "steer"
    else:
        g = await _read_agent()
        await g.aupdate_state(
            {"configurable": {"thread_id": ptid}},
            {"messages": [HumanMessage(content=text, additional_kwargs={
                "lb_merge": {"tid": tid, "title": title, "ts": time.time()}})]},
            as_node="__start__")
        how = "appended"
    p = {**p, "merged": time.time(), "merged_upto": int(upto)}
    async with _wt(_db):
        # merged = done: the branch goes read-only so it can't be resumed by
        # accident (↺ unarchive brings it back deliberately)
        await _db.execute("UPDATE threads SET parent=?, archived=? WHERE id=?",
                          (json.dumps(p, ensure_ascii=False), time.time(), tid))
        await _db.execute("UPDATE threads SET updated_at=? WHERE id=?", (time.time(), ptid))
    return {"parent": {"tid": ptid, "title": p.get("title")}, "how": how}


async def btw_list(tid: str) -> list[dict]:
    """The thread's banked side questions, oldest first."""
    cur = await _db.execute(
        "SELECT id, ts, q, answer FROM btw WHERE thread_id=? ORDER BY id", (tid,))
    return [{"id": r[0], "ts": r[1], "q": r[2], "answer": r[3]} for r in await cur.fetchall()]


async def btw_delete(tid: str, bid: int) -> bool:
    async with _wt(_db):
        cur = await _db.execute("DELETE FROM btw WHERE thread_id=? AND id=?", (tid, bid))
        return cur.rowcount > 0


# ---- streaming runner ----

async def run_chat(
    thread_id: str,
    user_text: str,
    s: dict,
    images: list[str] | None = None,
    sched: dict | None = None,
    resume: dict | None = None,
    plan: bool = False,
    wake: dict | None = None,
) -> AsyncIterator[dict]:
    """Yield SSE-ready dicts: token | thinking | tool_start | tool_end |
    todos | sub | usage | gate | done | error. `resume` = {id, value}
    answers a pending gate (ask_user / exit_plan_mode interrupt) instead of
    sending a new message; `plan` runs in plan mode. Trajectory rows are persisted to
    `run_events` as the run progresses (persist-before-yield, so the
    post-done refresh always sees what the client was already shown)."""
    turn_id = uuid.uuid4().hex[:12]
    t_run0 = time.time()
    why = await archived_why(thread_id)
    if why:  # the authority for every caller (chat, wake, schedule, resume)
        yield {"type": "error", "message": "⛔ " + why}
        return
    steer_take(thread_id)  # nothing queued for a run that is over carries into this one
    try:
        await _touch(thread_id, user_text or "[image]")
        from . import projects
        proj = await projects.thread_project(thread_id)
        # ⏱ BEFORE this turn's own trajectory row: it measures the gap since
        # the thread's last activity. Cron runs repeat on purpose — no note.
        stale, t_st = None, time.time()
        if not resume and not sched:
            try:
                stale = await projects.staleness(thread_id, s)
            except Exception as e:  # noqa: BLE001 - a reminder, never a failure
                logger.warning("staleness %s: %r", thread_id, e)
        t_st = round(time.time() - t_st, 3)
        pf_task = None
        if not resume and user_text and bool(s.get("deep_agent", True)):
            from . import prefetch
            # started before build_agent (MCP tool load) so its ~0.3-0.7 s
            # usually costs nothing; read before this turn's own user row.
            # Other projects' threads never ride along as hints.
            pf_task = asyncio.ensure_future(prefetch.gather(
                user_text, await _prior_user_text(thread_id), s,
                drop_tids=await projects.others_tids(proj and proj["id"])))
        await _log(
            thread_id, turn_id, "user",
            meta={"text": _cap(user_text or "[image]", 800),
                  **({"gate": True} if resume else {}), **({"plan": True} if plan else {})},
        )
        # this thread's provider/model override (scheduled tasks too: each
        # task owns a thread) — resolved per run, so a change applies next turn
        s = config.effective(s, await get_thread_model(thread_id))
        agent = await build_agent(s, thread_prompt=await learning.get_thread_prompt(_db, thread_id),
                                  project=proj)
        deep = bool(s.get("deep_agent", True))
        # Deep mode grants headroom for write_todos bookkeeping rounds (each
        # todo update is a full model+tool round that isn't "real" iteration).
        cfg = {
            "configurable": {
                "thread_id": thread_id,
                "lb_plan": bool(plan),
                "lb_nogate": sched is not None,  # nobody's there to answer
                "lb_project": proj["name"] if proj else "",  # remember(project_only=…)
            },
            "recursion_limit": 2 * int(s.get("max_react_iterations", 12)) + 2 + (16 if deep else 0),
        }
        if images:
            content = [
                {"type": "image_url", "image_url": {"url": du}} for du in images
            ] + [{"type": "text", "text": user_text or "Describe the image."}]
        else:
            content = user_text
        h_kwargs = {"lb_sched": sched} if sched else {}
        if wake:
            h_kwargs["lb_wake"] = {**wake, "ts": time.time()}  # ⚙ procs._wake
        if stale:
            h_kwargs["lb_stale"] = stale
            await _log(thread_id, turn_id, "tool", name="get_current", dur=t_st,
                       meta={"in": "", "out": _cap(stale["note"], 2000), "sub": None})
            yield {"type": "stale", **stale}
        if pf_task is not None:
            pf = None
            try:
                pf = await pf_task
            except Exception as e:  # noqa: BLE001 - a hint, never a failure
                logger.warning("prefetch failed: %r", e)
            if pf:
                from . import prefetch
                summ = prefetch.summary(pf)
                h_kwargs["lb_prefetch"] = {"note": pf["note"], **summ}
                await _log(thread_id, turn_id, "tool", name="auto_search", dur=pf["ms"] / 1000,
                           meta={"in": _cap({"why": pf["why"], "gate": pf.get("gate"),
                                             "queries": pf["queries"]}, 2000),
                                 "out": _cap(pf["hits"], 2000), "sub": None})
                yield {"type": "prefetch", **summ}
        # Per model-call timing, for the usage/speed line streamed to the UI.
        # A turn can contain several model calls (one per ReAct round, plus the
        # compaction summarizer); each gets its own usage event.
        t0: float | None = None
        t_first: float | None = None
        # Sub-agents launched via the `task` tool run their own model/tool
        # calls on the same event bus. While a task run is live, suppress the
        # model events whose ancestor chain contains it — otherwise a
        # sub-agent's draft stream would render as the main answer and its
        # per-call timings would corrupt ours. (`task` cards and the sub's
        # tool cards still come through.)
        sub_runs: set[str] = set()
        # Trajectory bookkeeping: tool timings paired by run_id (also fixes
        # interleaved parallel tools client-side — they're keyed, not tracked
        # by a single pointer), plus start times of sub-agent model calls so
        # their LLM seconds get persisted even though their SSE is suppressed.
        tool_t0: dict[str, tuple[float, str | None, object]] = {}
        sub_m_t0: dict[str, float] = {}
        calls: list[tuple[str, dict]] = []  # every tool call's args → projects.observe
        graph_in = (
            Command(resume={resume["id"]: resume["value"]}) if resume else {
                "messages": [
                    HumanMessage(
                        content=content,
                        # scheduled-run stamp {ts, manual} and auto-search
                        # hits: survive the messages reducer/checkpoint,
                        # surfaced by _msg_dict
                        additional_kwargs=h_kwargs,
                    )
                ]
            }
        )
        while True:
            async for ev in agent.astream_events(graph_in, cfg, version="v2"):
                kind = ev["event"]
                if kind == "on_tool_start" and ev["name"] == "task":
                    sub_runs.add(str(ev["run_id"]))
                elif kind == "on_tool_end" and str(ev["run_id"]) in sub_runs:
                    sub_runs.discard(str(ev["run_id"]))
                in_sub = bool(sub_runs & {str(p) for p in ev.get("parent_ids") or ()})
                # Innermost live `task` this event belongs to. parent_ids is
                # ordered root -> immediate parent, so scan reversed for the
                # innermost match. The `task` tool's OWN events don't carry its
                # run_id in parent_ids, so they correctly resolve to None (or to
                # the enclosing task for a nested task) — the UI renders those
                # as the sub card itself.
                sub_id = next(
                    (
                        p
                        for p in reversed([str(x) for x in (ev.get("parent_ids") or ())])
                        if p in sub_runs
                    ),
                    None,
                )
                if kind.startswith("on_chat_model") and COMPACTION_TAG in (ev.get("tags") or ()):
                    continue  # the summarizer: reported once, via lb_compacted below
                if kind == "on_custom_event" and ev.get("name") == "lb_steer" and not in_sub:
                    txt = (ev.get("data") or {}).get("text") or ""
                    await _log(thread_id, turn_id, "user", meta={"text": _cap(txt, 800), "steer": True})
                    yield {"type": "steer", "text": txt}
                    continue
                if kind == "on_custom_event" and ev.get("name") == "lb_compacted" and not in_sub:
                    d = ev.get("data") or {}
                    await _log(
                        thread_id, turn_id, "compact", name=s.get("model"), dur=d.get("dur"),
                        tok_in=d.get("tok_in"), tok_out=d.get("tok_out"),
                        meta={k: d.get(k) for k in ("count", "kept", "before", "after", "in_turn")}
                        | {"text": _cap(d.get("summary") or "", 500)},
                    )
                    yield {"type": "compacted", **{k: d.get(k) for k in
                           ("count", "kept", "before", "after", "in_turn", "dur", "summary")}}
                    continue
                if kind == "on_chat_model_start":
                    if not in_sub:
                        t0, t_first = time.time(), None
                    else:
                        sub_m_t0[str(ev["run_id"])] = time.time()
                elif kind == "on_chat_model_stream":
                    if in_sub:
                        continue
                    if t0 is not None and t_first is None:
                        t_first = time.time()  # includes prefill + first-token latency
                    chunk = ev["data"]["chunk"]
                    text = chunk.content
                    # reasoning BEFORE content: vLLM's parser can put the last
                    # reasoning "\n" and the first answer text in ONE delta —
                    # token-first opened a stray empty ◈ THINKING card under the answer
                    reasoning = (chunk.additional_kwargs or {}).get("reasoning_content")
                    if reasoning:
                        # Belt & braces: with thinking off we explicitly asked the
                        # backend not to emit any; if one ignores that, keep the
                        # text in the visible stream rather than a card the
                        # REASONING toggle would strand.
                        yield (
                            {"type": "thinking", "text": reasoning}
                            if s.get("enable_thinking")
                            else {"type": "token", "text": reasoning}
                        )
                    if isinstance(text, str) and text:
                        yield {"type": "token", "text": text}
                elif kind == "on_chat_model_end":
                    # stream_usage=True makes sglang append a final usage chunk;
                    # langchain merges it into the assembled message's usage_metadata.
                    um = getattr(ev["data"].get("output"), "usage_metadata", None) or {}
                    inp = int(um.get("input_tokens") or 0)
                    outp = int(um.get("output_tokens") or 0)
                    # sglang only reports cache_read with --enable-cache-report;
                    # absent/0 until then, and the UI hides the chip accordingly.
                    cache = (um.get("input_token_details") or {}).get("cache_read")
                    out_text = _cap(
                        _text_only(getattr(ev["data"].get("output"), "content", "")), 500
                    )
                    if in_sub:
                        # Persisted (so LLM seconds include sub-agent thinking),
                        # but its stream stays suppressed from the UI.
                        st = sub_m_t0.pop(str(ev["run_id"]), None)
                        await _log(
                            thread_id, turn_id, "model", name=s.get("model"),
                            dur=round(time.time() - st, 3) if st else None,
                            tok_in=inp or None, tok_out=outp or None, cache_read=cache,
                            meta={"text": out_text, "sub": sub_id},
                        )
                        continue
                    if t0 is not None:
                        now = time.time()
                        ttft = (t_first if t_first else now) - t0
                        decode = max(now - (t_first if t_first else now), 1e-3)
                        await _log(
                            thread_id, turn_id, "model", name=s.get("model"),
                            dur=round(now - t0, 3),
                            tok_in=inp or None, tok_out=outp or None,
                            cache_read=cache, ttft=round(ttft, 3),
                            meta={"text": out_text, "sub": None},
                        )
                        if inp or outp:
                            yield {
                                "type": "usage",
                                "input": inp,
                                "output": outp,
                                "ttft": round(ttft, 2),
                                "prefill_tps": round(inp / max(ttft, 1e-3)),
                                "decode_tps": round(outp / decode, 1),
                                "seconds": round(now - t0, 1),
                            }
                    t0, t_first = None, None
                elif kind == "on_tool_start":
                    rid = str(ev["run_id"])
                    tool_t0[rid] = (time.time(), sub_id, ev["data"].get("input"))
                    consult = learning.consulted(ev["name"], ev["data"].get("input"), s)
                    calls.append((ev["name"], ev["data"].get("input")))
                    yield {
                        "type": "tool_start",
                        "name": ev["name"],
                        "input": _safe(ev["data"].get("input")),
                        "run_id": rid,
                        "sub": sub_id,
                        **({"consult": consult} if consult else {}),
                    }
                    if ev["name"] == "write_todos":
                        todos = (ev["data"].get("input") or {}).get("todos")
                        if isinstance(todos, list):
                            # UI renders the TO-DOS panel from this; the raw
                            # tool card still streams too.
                            yield {"type": "todos", "todos": todos}
                    if ev["name"] == "task":
                        d = ev["data"].get("input") or {}
                        yield {
                            "type": "sub",
                            "state": "start",
                            "sub_id": rid,
                            "desc": str(d.get("description") or "")[:120],
                            "subagent_type": d.get("subagent_type"),
                        }
                elif kind == "on_tool_end":
                    rid = str(ev["run_id"])
                    out_text = _tool_text(ev["data"].get("output"))
                    st0, sub0, inp0 = tool_t0.pop(rid, (None, sub_id, None))
                    dur = round(time.time() - st0, 3) if st0 else None
                    meta = {"in": _cap(inp0, 2000), "out": _cap(out_text, 2000), "sub": sub0}
                    if ev["name"] == "task":
                        # The task row IS the sub-agent span: dur = whole
                        # delegation, meta carries what was delegated.
                        d = inp0 if isinstance(inp0, dict) else {}
                        meta["desc"] = str(d.get("description") or "")[:120]
                        meta["subagent_type"] = d.get("subagent_type")
                    await _log(thread_id, turn_id, "tool", name=ev["name"], dur=dur, meta=meta)
                    yield {
                        "type": "tool_end",
                        "name": ev["name"],
                        "output": out_text,
                        "run_id": rid,
                        "sub": sub0,
                        "dur": dur,
                    }
                    if ev["name"] == "task":
                        yield {"type": "sub", "state": "end", "sub_id": rid, "dur": dur}
                elif kind == "on_tool_error" and isinstance(ev["data"].get("error"), GraphInterrupt):
                    # a human gate pausing (ask_user / exit_plan_mode) is not a
                    # failure: drop the timing entry; the `gate` event after the
                    # stream renders it, and the resumed run re-emits the tool
                    tool_t0.pop(str(ev["run_id"]), None)
                elif kind == "on_tool_error":
                    # Validation/invocation failure (e.g. the model emitted a
                    # tool call with empty or wrong-schema args — sglang drops
                    # giant tool-call JSON now and then). The tool never ran:
                    # without this branch the card sat "running" forever and
                    # the trajectory had no row for the attempt at all.
                    rid = str(ev["run_id"])
                    sub_runs.discard(rid)
                    st0, sub0, inp0 = tool_t0.pop(rid, (None, sub_id, None))
                    dur = round(time.time() - st0, 3) if st0 else None
                    err = str(ev["data"].get("error") or "tool error")[:500]
                    await _log(
                        thread_id, turn_id, "tool", name=ev["name"], dur=dur,
                        meta={"in": _cap(inp0, 2000), "err": err, "sub": sub0},
                    )
                    yield {
                        "type": "tool_error",
                        "name": ev["name"],
                        "error": err,
                        "run_id": rid,
                        "sub": sub0,
                        "dur": dur,
                    }
            # a steer that landed after the model's last step: the graph has
            # ended without seeing it — one more round in this same run
            if await pending_gates(thread_id):
                break
            left = steer_take(thread_id)
            if not left:
                break
            m_st = _steer_msg(left)
            await _log(thread_id, turn_id, "user", meta={"text": _cap(m_st.content, 800), "steer": True})
            yield {"type": "steer", "text": m_st.content}
            graph_in = {"messages": [m_st]}
        try:  # ◫ auto-file an unfiled thread / flag a drifted one
            pj = await projects.observe(thread_id, calls)
            if pj:
                yield {"type": "project", **pj}
        except Exception as e:  # noqa: BLE001 - bookkeeping, never a failed run
            logger.warning("projects.observe %s: %r", thread_id, e)
        # an interrupt() ends the stream quietly — the pause lives in the
        # checkpoint; surface it so the UI can render the question / plan
        for g in await pending_gates(thread_id):
            await _log(thread_id, turn_id, "gate", meta={"value": g["value"]})
            yield {"type": "gate", "id": g["id"], "value": _safe(g["value"])}
        yield {"type": "done", "seconds": round(time.time() - t_run0, 1)}
    except GraphRecursionError:
        # LangGraph's own text ("set the recursion_limit config key", docs URL)
        # is misleading here: the user-facing knob is MAX AGENT ITERATIONS, and
        # crucially the checkpoint survives — a new message resumes for free.
        # a checkpoint write that died mid-flight must not leave its txn
        # (and the WAL writer lock) open for the next victim
        await _release_tx()
        await _log(
            thread_id,
            turn_id,
            "error",
            meta={"message": f"GraphRecursionError: limit {cfg['recursion_limit']} reached"},
        )
        yield {
            "type": "error",
            "message": (
                f"AGENT BUDGET EXHAUSTED — {cfg['recursion_limit']} graph steps "
                f"(max agent iterations: {s.get('max_react_iterations', 12)}) "
                "without a stop. Work so far is saved: send 'continue' to "
                "resume from where it stopped (fresh budget), or raise MAX "
                "AGENT ITERATIONS in CONFIG."
            ),
        }
    except Exception as e:  # noqa: BLE001 - stream errors to the UI
        await _release_tx()  # same reason as above: failed writes don't self-rollback
        await _log(thread_id, turn_id, "error", meta={"message": str(e)[:500]})
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
    except (asyncio.CancelledError, GeneratorExit):
        # Runs are server-owned now (runs.Hub): this generator is consumed by
        # a detached task, so it only ever gets cancelled by an EXPLICIT STOP
        # (hub.task.cancel()) or a server shutdown — a dropped browser tab
        # can no longer reach here. Both inherit BaseException, so the arm
        # above never saw them; breadcrumb the trajectory fire-and-forget,
        # because awaiting anything here either re-raises (CancelledError) or
        # is illegal (GeneratorExit inside a closing generator). A mid-write
        # cancellation is the MOST likely way to strand an open txn holding
        # the DB lock, so the cleanup releases it before the breadcrumb.
        async def _cancel_cleanup() -> None:
            await _release_tx()
            await _log(thread_id, turn_id, "error", meta={
                "message": "RUN CANCELLED — stop requested (or server shutdown)",
            })
        _spawn(_cancel_cleanup())
        raise


SHELL_TIMEOUT = 300  # wall clock for a user-typed `!cmd`, same cap as run_bash


async def run_user_shell(thread_id: str, command: str) -> dict:
    """Claude-Code-style `!cmd`: run the command in the same unsandboxed shell
    as run_bash, WITHOUT invoking the model. The exchange is appended to the
    thread's checkpoint state, so the agent reads the output on its next turn
    (content carries the full text; the UI renders the structured `shell`
    kwarg instead of a plain bubble). Returns {cmd, out, exit, dur} for the
    client's card."""
    turn_id = uuid.uuid4().hex[:12]
    await _touch(thread_id, f"! {command}")
    await _log(thread_id, turn_id, "user", meta={"text": _cap("! " + command, 800)})
    t0 = time.time()
    # same code path/format as the agent's own tool call (incl. the MAX_OUT
    # trim, so model context cost of a `!cmd` equals the agent running it)
    out = await asyncio.to_thread(
        local_tools.bash, command, SHELL_TIMEOUT
    )
    dur = round(time.time() - t0, 3)
    m = re.match(r"exit=(-?\d+)", out)
    exit_code = int(m.group(1)) if m else None  # None on TIMEOUT/ERROR strings
    await _log(
        thread_id,
        turn_id,
        "tool",
        name="shell",
        dur=dur,
        meta={"in": _cap(command, 2000), "out": _cap(out, 2000), "sub": None},
    )
    msg = HumanMessage(
        # leading `$ ` line = what the agent sees first; body = run_bash format
        content=f"$ {command}\n{out}",
        additional_kwargs={
            "lb_shell": {"cmd": command, "out": out, "exit": exit_code, "dur": dur}
        },
    )
    # Append without invoking, as if the exchange had arrived as graph input:
    # as_node="__start__" routes through the messages reducer (DeltaChannel
    # included) and leaves next=<entry>, so the next real turn simply follows.
    g = await _read_agent()
    await g.aupdate_state(
        {"configurable": {"thread_id": thread_id}}, {"messages": [msg]}, as_node="__start__"
    )
    return {"cmd": command, "out": out, "exit": exit_code, "dur": dur}


def _safe(x):
    try:
        json.dumps(x)
        return x
    except (TypeError, ValueError):
        return str(x)


def _tool_text(out):
    """Flatten ToolMessage / MCP result to plain text for the UI."""
    if hasattr(out, "content"):
        out = out.content
    if isinstance(out, list):  # content blocks
        out = "\n".join(
            (b.get("text", "") or (f"[{b.get('type')} — shown to the model]"
                                    if b.get("type") in _MEDIA_BLOCKS else ""))
            if isinstance(b, dict) else str(b) for b in out
        )
    return _safe(out)

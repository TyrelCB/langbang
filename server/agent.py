"""LangGraph agent + streaming runner + thread storage."""
import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite
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
from langgraph.errors import GraphInterrupt, GraphRecursionError
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command, interrupt
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from deepagents.middleware._utils import append_to_system_message

from . import config, local_tools, mcp

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
        """
    )
    # threads.orig = the seed title (schedule name / first-message prefix) that
    # ⟲ revert-title restores. Existing DBs predate the column.
    try:
        await _db.execute("ALTER TABLE threads ADD COLUMN orig TEXT")
    except aiosqlite.OperationalError:  # duplicate column — already migrated
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


class SGlangChatOpenAI(ChatOpenAI):
    """ChatOpenAI that recovers `reasoning_content` from streamed deltas.

    langchain-openai >=1.x deliberately ignores non-spec delta fields, so
    sglang's/vLLM's separated thinking would be dropped on the floor; we
    re-attach it to additional_kwargs where _msg_dict/run_chat can see it.
    """

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
        rc = delta.get("reasoning_content") if isinstance(delta, dict) else None
        if rc:
            gc.message.additional_kwargs["reasoning_content"] = rc
        return gc


def model(s: dict) -> ChatOpenAI:
    # Always state enable_thinking explicitly. Qwen3-style hybrids think by
    # *default* — omitting the kwarg would keep reasoning streaming even when
    # the user turned thinking off (the sglang/vllm template flips per flag).
    extra_body = {
        "chat_template_kwargs": {"enable_thinking": bool(s.get("enable_thinking"))}
    }
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
        extra_body=extra_body,
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
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
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
        if trigger <= 0 or count_tokens_approximately(msgs) < trigger:
            return None
        keep = max(4, int(s.get("compact_keep_messages", 20)))
        split = max(0, len(msgs) - keep)
        # Never cut inside a tool round: walk back to a HumanMessage boundary
        # (orphan ToolMessages without their AIMessage tool_calls 400 the API).
        while split > 0 and not isinstance(msgs[split], HumanMessage):
            split -= 1
        head, tail = msgs[:split], msgs[split:]
        if not head:  # one giant turn — nothing safe to fold away yet
            return None
        summary = await summarizer(s).ainvoke(
            [
                SystemMessage(content=_SUMMARY_INSTRUCTION),
                HumanMessage(content=_transcript(head)),
            ]
        )
        tid = (get_config() or {}).get("configurable", {}).get("thread_id", "")
        async with _wt(_db):
            for m in head:
                await _db.execute(
                    "INSERT INTO archived_messages(thread_id,msg) VALUES(?,?)",
                    (tid, json.dumps(_msg_dict(m), ensure_ascii=False)),
                )
        note = HumanMessage(
            content=str(summary.content).strip(),
            additional_kwargs={"lb_compacted": {"count": len(head)}},
        )
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
            elif isinstance(m, HumanMessage):
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
            elif isinstance(m, HumanMessage):
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

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001
        call = request.tool_call
        args = call.get("args")
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
        res = await handler(request)
        # ToolNode converts invocation/validation failures into error
        # ToolMessages instead of raising; breadcrumb them (the astream
        # loop separately surfaces on_tool_error events to UI+trajectory).
        if isinstance(res, ToolMessage) and getattr(res, "status", None) == "error":
            logger.warning(
                "tool-error: %s kwargs=%.120s — %.200s",
                request.tool_call.get("name"),
                str(request.tool_call.get("args") or {}),
                res.content,
            )
        return res

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
  Markdown. Do not start implementing until it comes back APPROVED."""


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
            msg = "Plan mode is not active — just do the work."
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


async def build_agent(s: dict, checkpointer=None):
    enabled = s.get("local_tools") or {}
    deep = bool(s.get("deep_agent", True))
    skip = DEEP_REPLACED_TOOLS if deep else set()
    tools = [
        t
        for t in local_tools.LOCAL_TOOLS
        if enabled.get(t.name, True) and t.name not in skip
    ] + await mcp.get_tools(s.get("mcp_servers") or {})
    cp = checkpointer or _checkpointer
    prompt = s["system_prompt"] + local_tools.TOOLS_NOTE
    if deep:
        mw = [TodoListMiddleware()]  # write_todos planning tool
        if s.get("compact_enabled", True):
            mw.append(_CompactionMiddleware(s))
        mw.append(_TodoReconcile())  # deterministic finish-line gate
        mw.append(_FileArgAlias())  # repair path->file_path arg drift pre-validation
        mw.append(_MediaReadGuard())  # audio/video base64 never reaches the model
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
        return create_deep_agent(
            model(s),
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


def _msg_dict(m) -> dict:
    d = {"role": m.type, "content": m.content}
    if isinstance(m, AIMessage) and m.tool_calls:
        d["tool_calls"] = [
            {"name": tc["name"], "args": tc["args"]} for tc in m.tool_calls
        ]
    if isinstance(m, ToolMessage):
        d["tool_name"] = m.name
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


async def history(thread_id: str) -> list:
    live = []
    for m in await _live_messages(thread_id):
        d = _msg_dict(m)
        if isinstance(m, ToolMessage) and (m.additional_kwargs or {}).get("read_file_path"):
            d["read_file_path"] = m.additional_kwargs["read_file_path"]
        live.append(_slim(d))
    # Pre-compaction originals were dropped from graph state; replay them so
    # the UI still shows the full transcript while the model sees the summary.
    cur = await _db.execute(
        "SELECT msg FROM archived_messages WHERE thread_id=? ORDER BY seq",
        (thread_id,),
    )
    archived = [_slim(json.loads(r[0])) for r in await cur.fetchall()]
    return archived + live


# ---- run trajectory (agentic visibility) ----

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
            " COALESCE(SUM(CASE WHEN type='model' THEN dur END),0),"
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

async def create_thread(title: str = "New chat") -> dict:
    tid = uuid.uuid4().hex[:12]
    now = time.time()
    # orig = seed title: renames (manual/auto) never touch it; ⟲ restores it
    async with _wt(_db):
        await _db.execute(
            "INSERT INTO threads(id,title,created_at,updated_at,orig) VALUES(?,?,?,?,?)",
            (tid, title, now, now, title),
        )
    return {"id": tid, "title": title, "created_at": now, "updated_at": now}


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
    cur = await _db.execute("SELECT id,title,created_at,updated_at,orig FROM threads ORDER BY updated_at DESC")
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
            hit = (key, count_tokens_approximately(msgs), len(msgs),
                   sum(len(str(getattr(m, "content", "") or "")) for m in msgs))
            _thread_stats[r[0]] = hit
        out.append(
            {"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3],
             "context_tokens": base + hit[1], "orig": r[4],
             # cheap content signal the sidebar uses to decide whether the ✕
             # needs a confirm click (see web/app.js refreshThreads)
             "n_msgs": hit[2], "chars": hit[3]}
        )
    return out


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
        await _db.execute("DELETE FROM archived_messages WHERE thread_id=?", (tid,))
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
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )


def _flat_transcript(msgs: list[dict], head: int, tail: int, cap: int = 350) -> str:
    """history()-style dicts → compact text for one-shot LLM calls. First
    `head` + last `tail` messages; tool noise folded to the tool name."""
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
        calls = ", ".join(tc["name"] for tc in (m.get("tool_calls") or []))
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
    return {"title": row[0], "summary": _text_only(summary.content).strip(),
            "ts": time.time()}


# ---- streaming runner ----

async def run_chat(
    thread_id: str,
    user_text: str,
    s: dict,
    images: list[str] | None = None,
    sched: dict | None = None,
    resume: dict | None = None,
    plan: bool = False,
) -> AsyncIterator[dict]:
    """Yield SSE-ready dicts: token | thinking | tool_start | tool_end |
    todos | sub | usage | gate | done | error. `resume` = {id, value}
    answers a pending gate (ask_user / exit_plan_mode interrupt) instead of
    sending a new message; `plan` runs in plan mode. Trajectory rows are persisted to
    `run_events` as the run progresses (persist-before-yield, so the
    post-done refresh always sees what the client was already shown)."""
    turn_id = uuid.uuid4().hex[:12]
    t_run0 = time.time()
    try:
        await _touch(thread_id, user_text or "[image]")
        await _log(
            thread_id, turn_id, "user",
            meta={"text": _cap(user_text or "[image]", 800),
                  **({"gate": True} if resume else {}), **({"plan": True} if plan else {})},
        )
        agent = await build_agent(s)
        deep = bool(s.get("deep_agent", True))
        # Deep mode grants headroom for write_todos bookkeeping rounds (each
        # todo update is a full model+tool round that isn't "real" iteration).
        cfg = {
            "configurable": {
                "thread_id": thread_id,
                "lb_plan": bool(plan),
                "lb_nogate": sched is not None,  # nobody's there to answer
            },
            "recursion_limit": 2 * int(s.get("max_react_iterations", 12)) + 2 + (16 if deep else 0),
        }
        if images:
            content = [
                {"type": "image_url", "image_url": {"url": du}} for du in images
            ] + [{"type": "text", "text": user_text or "Describe the image."}]
        else:
            content = user_text
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
        graph_in = (
            Command(resume={resume["id"]: resume["value"]}) if resume else {
                "messages": [
                    HumanMessage(
                        content=content,
                        # scheduled-run stamp {ts, manual}: survives the
                        # messages reducer/checkpoint, surfaced by _msg_dict
                        additional_kwargs={"lb_sched": sched} if sched else {},
                    )
                ]
            }
        )
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
                if isinstance(text, str) and text:
                    yield {"type": "token", "text": text}
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
                yield {
                    "type": "tool_start",
                    "name": ev["name"],
                    "input": _safe(ev["data"].get("input")),
                    "run_id": rid,
                    "sub": sub_id,
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
        local_tools.run_bash.invoke, {"command": command, "timeout": SHELL_TIMEOUT}
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

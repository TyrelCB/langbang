"""LangGraph agent + streaming runner + thread storage."""
import json
import logging
import os
import time
import uuid
from typing import AsyncIterator

import aiosqlite
from deepagents import (
    HarnessProfile,
    backends,
    create_deep_agent,
    register_harness_profile,
)
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
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.config import get_config
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.prebuilt import create_react_agent

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

_checkpointer: AsyncSqliteSaver | None = None
_db: aiosqlite.Connection | None = None
_edb: aiosqlite.Connection | None = None  # event-log connection (trajectory rows)
# graph used ONLY to read state back (never invoked); see _read_agent()
_read_graph = None


async def init() -> None:
    global _checkpointer, _db, _edb
    _db = await aiosqlite.connect(config.DB_PATH)
    await _db.execute("PRAGMA busy_timeout=10000")
    # Trajectory rows get their OWN connection: run_chat's generator commits
    # per event, and sharing the checkpointer's connection could commit a
    # half-written checkpoint transaction that lands between the saver's
    # inserts and its own commit. The DB is WAL, so writers serialize cleanly.
    _edb = await aiosqlite.connect(config.DB_PATH)
    await _edb.execute("PRAGMA busy_timeout=10000")
    _checkpointer = AsyncSqliteSaver(_db)
    await _checkpointer.setup()
    await _db.executescript(
        """
        CREATE TABLE IF NOT EXISTS threads(
          id TEXT PRIMARY KEY, title TEXT, created_at REAL, updated_at REAL);
        CREATE TABLE IF NOT EXISTS archived_messages(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, msg TEXT);
        """
    )
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
        for m in head:
            await _db.execute(
                "INSERT INTO archived_messages(thread_id,msg) VALUES(?,?)",
                (tid, json.dumps(_msg_dict(m), ensure_ascii=False)),
            )
        await _db.commit()
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


# Provided by the deepagents harness itself in deep mode (on the real FS, with
# richer descriptions) — our same-named tools would collide on bind.
DEEP_REPLACED_TOOLS = {"read_file", "write_file"}


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
        return create_deep_agent(
            model(s),
            tools,
            system_prompt=prompt,
            middleware=mw,
            backend=_fs_backend(),
            checkpointer=cp,
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
    if msgs is None:
        try:
            g = await _read_agent()
            snap = await g.aget_state(cfg)
            msgs = (snap.values if snap else {}).get("messages")
        except Exception:  # never let a corrupt thread blank the whole UI
            logger.exception("delta-channel state load failed for %s", thread_id)
            msgs = None
    return list(msgs or [])


async def history(thread_id: str) -> list:
    live = [_msg_dict(m) for m in await _live_messages(thread_id)]
    # Pre-compaction originals were dropped from graph state; replay them so
    # the UI still shows the full transcript while the model sees the summary.
    cur = await _db.execute(
        "SELECT msg FROM archived_messages WHERE thread_id=? ORDER BY seq",
        (thread_id,),
    )
    archived = [json.loads(r[0]) for r in await cur.fetchall()]
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
        pass


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
    await _db.execute("INSERT INTO threads VALUES(?,?,?,?)", (tid, title, now, now))
    await _db.commit()
    return {"id": tid, "title": title, "created_at": now, "updated_at": now}


def prompt_overhead_tokens() -> int:
    """Approx size of what every model call carries on top of thread history:
    the system prompt + tools note (the tool schemas themselves add a bit
    more, uncounted here). Same estimator as compaction, so the UI's CTX chip
    and the compaction trigger speak the same units."""
    s = config.load()
    return count_tokens_approximately(
        [SystemMessage(content=s["system_prompt"] + local_tools.TOOLS_NOTE)]
    )


async def list_threads() -> list:
    cur = await _db.execute("SELECT id,title,created_at,updated_at FROM threads ORDER BY updated_at DESC")
    rows = await cur.fetchall()
    base = prompt_overhead_tokens()
    out = []
    for r in rows:
        # What the *next* model call would replay: live graph state (post-
        # compaction), not the full archived transcript the UI shows.
        # _live_messages also reconstructs delta-stored messages (deep mode).
        ctx = base + count_tokens_approximately(await _live_messages(r[0]))
        out.append(
            {"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3],
             "context_tokens": ctx}
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
    await _db.execute("DELETE FROM threads WHERE id=?", (tid,))
    await _db.execute("DELETE FROM archived_messages WHERE thread_id=?", (tid,))
    await _db.commit()
    await _edb.execute("DELETE FROM run_events WHERE thread_id=?", (tid,))
    await _edb.commit()


async def _touch(thread_id: str, first_text: str) -> None:
    cur = await _db.execute("SELECT title FROM threads WHERE id=?", (thread_id,))
    row = await cur.fetchone()
    if row is None:
        title = first_text[:60]
        now = time.time()
        await _db.execute("INSERT INTO threads VALUES(?,?,?,?)", (thread_id, title, now, now))
    else:
        title = row[0]
        if title == "New chat" or title == "":
            await _db.execute("UPDATE threads SET title=?, updated_at=? WHERE id=?",
                              (first_text[:60], time.time(), thread_id))
        else:
            await _db.execute("UPDATE threads SET updated_at=? WHERE id=?", (time.time(), thread_id))
    await _db.commit()


# ---- streaming runner ----

async def run_chat(
    thread_id: str, user_text: str, s: dict, images: list[str] | None = None
) -> AsyncIterator[dict]:
    """Yield SSE-ready dicts: token | thinking | tool_start | tool_end |
    todos | sub | usage | done | error. Trajectory rows are persisted to
    `run_events` as the run progresses (persist-before-yield, so the
    post-done refresh always sees what the client was already shown)."""
    turn_id = uuid.uuid4().hex[:12]
    t_run0 = time.time()
    try:
        await _touch(thread_id, user_text or "[image]")
        await _log(
            thread_id, turn_id, "user", meta={"text": _cap(user_text or "[image]", 800)}
        )
        agent = await build_agent(s)
        deep = bool(s.get("deep_agent", True))
        # Deep mode grants headroom for write_todos bookkeeping rounds (each
        # todo update is a full model+tool round that isn't "real" iteration).
        cfg = {
            "configurable": {"thread_id": thread_id},
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
        async for ev in agent.astream_events(
            {"messages": [HumanMessage(content=content)]}, cfg, version="v2"
        ):
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
        yield {"type": "done", "seconds": round(time.time() - t_run0, 1)}
    except Exception as e:  # noqa: BLE001 - stream errors to the UI
        await _log(thread_id, turn_id, "error", meta={"message": str(e)[:500]})
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}


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
            b.get("text", "") if isinstance(b, dict) else str(b) for b in out
        )
    return _safe(out)

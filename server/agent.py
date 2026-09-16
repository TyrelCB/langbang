"""LangGraph agent + streaming runner + thread storage."""
import json
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

_checkpointer: AsyncSqliteSaver | None = None
_db: aiosqlite.Connection | None = None


async def init() -> None:
    global _checkpointer, _db
    _db = await aiosqlite.connect(config.DB_PATH)
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


async def history(thread_id: str) -> list:
    tup = await _checkpointer.aget({"configurable": {"thread_id": thread_id}})
    live = []
    if tup:
        # Newer checkpointer returns a CheckpointTuple; older returns dict.
        cv = tup.get("channel_values") if isinstance(tup, dict) else tup.channel_values
        live = [_msg_dict(m) for m in (cv or {}).get("messages", [])]
    # Pre-compaction originals were dropped from graph state; replay them so
    # the UI still shows the full transcript while the model sees the summary.
    cur = await _db.execute(
        "SELECT msg FROM archived_messages WHERE thread_id=? ORDER BY seq",
        (thread_id,),
    )
    archived = [json.loads(r[0]) for r in await cur.fetchall()]
    return archived + live


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
        ctx = base
        tup = await _checkpointer.aget({"configurable": {"thread_id": r[0]}})
        if tup:
            cv = tup.get("channel_values") if isinstance(tup, dict) else tup.channel_values
            ctx += count_tokens_approximately((cv or {}).get("messages", []))
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
    """Yield SSE-ready dicts: token | thinking | tool_start | tool_end | done | error."""
    try:
        await _touch(thread_id, user_text or "[image]")
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
        async for ev in agent.astream_events(
            {"messages": [HumanMessage(content=content)]}, cfg, version="v2"
        ):
            kind = ev["event"]
            if kind == "on_tool_start" and ev["name"] == "task":
                sub_runs.add(str(ev["run_id"]))
            elif kind == "on_tool_end" and str(ev["run_id"]) in sub_runs:
                sub_runs.discard(str(ev["run_id"]))
            in_sub = bool(sub_runs & {str(p) for p in ev.get("parent_ids") or ()})
            if kind == "on_chat_model_start":
                if not in_sub:
                    t0, t_first = time.time(), None
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
                if in_sub:
                    continue
                # stream_usage=True makes sglang append a final usage chunk;
                # langchain merges it into the assembled message's usage_metadata.
                um = getattr(ev["data"].get("output"), "usage_metadata", None) or {}
                inp = int(um.get("input_tokens") or 0)
                outp = int(um.get("output_tokens") or 0)
                if (inp or outp) and t0 is not None:
                    now = time.time()
                    ttft = (t_first if t_first else now) - t0
                    decode = max(now - (t_first if t_first else now), 1e-3)
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
                yield {
                    "type": "tool_start",
                    "name": ev["name"],
                    "input": _safe(ev["data"].get("input")),
                }
            elif kind == "on_tool_end":
                yield {"type": "tool_end", "name": ev["name"], "output": _tool_text(ev["data"].get("output"))}
        yield {"type": "done"}
    except Exception as e:  # noqa: BLE001 - stream errors to the UI
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

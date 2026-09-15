"""LangGraph agent + streaming runner + thread storage."""
import json
import time
import uuid
from typing import AsyncIterator

import aiosqlite
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.prebuilt import create_react_agent

from . import config, mcp

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
        """
    )
    await _db.commit()


def model(s: dict) -> ChatOpenAI:
    return ChatOpenAI(
        model=s["model"],
        base_url=s["base_url"],
        api_key=s["api_key"],
        temperature=s["temperature"],
        max_tokens=s["max_tokens"],
        streaming=True,
        # sglang doesn't do usage on every stream chunk; keep defaults lean.
    )


async def build_agent(s: dict, checkpointer=None):
    tools = await mcp.get_tools(s.get("mcp_servers") or {})
    return create_react_agent(
        model(s),
        tools,
        checkpointer=checkpointer or _checkpointer,
        prompt=SystemMessage(content=s["system_prompt"]),
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
    return d


async def history(thread_id: str) -> list:
    tup = await _checkpointer.aget({"configurable": {"thread_id": thread_id}})
    if not tup:
        return []
    return [_msg_dict(m) for m in tup.channel_values.get("messages", [])]


# ---- thread bookkeeping ----

async def create_thread(title: str = "New chat") -> dict:
    tid = uuid.uuid4().hex[:12]
    now = time.time()
    await _db.execute("INSERT INTO threads VALUES(?,?,?,?)", (tid, title, now, now))
    await _db.commit()
    return {"id": tid, "title": title, "created_at": now, "updated_at": now}


async def list_threads() -> list:
    cur = await _db.execute("SELECT id,title,created_at,updated_at FROM threads ORDER BY updated_at DESC")
    rows = await cur.fetchall()
    return [
        {"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3]} for r in rows
    ]


async def delete_thread(tid: str) -> None:
    await _db.execute("DELETE FROM threads WHERE id=?", (tid,))
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

async def run_chat(thread_id: str, user_text: str, s: dict) -> AsyncIterator[dict]:
    """Yield SSE-ready dicts: token | thinking | tool_start | tool_end | done | error."""
    try:
        await _touch(thread_id, user_text)
        agent = await build_agent(s)
        cfg = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 2 * int(s.get("max_react_iterations", 12)) + 2,
        }
        async for ev in agent.astream_events(
            {"messages": [HumanMessage(content=user_text)]}, cfg, version="v2"
        ):
            kind = ev["event"]
            if kind == "on_chat_model_stream":
                chunk = ev["data"]["chunk"]
                text = chunk.content
                if isinstance(text, str) and text:
                    yield {"type": "token", "text": text}
                reasoning = (chunk.additional_kwargs or {}).get("reasoning_content")
                if reasoning:
                    yield {"type": "thinking", "text": reasoning}
            elif kind == "on_tool_start":
                yield {
                    "type": "tool_start",
                    "name": ev["name"],
                    "input": _safe(ev["data"].get("input")),
                }
            elif kind == "on_tool_end":
                out = ev["data"].get("output")
                yield {"type": "tool_end", "name": ev["name"], "output": _safe(out)}
        yield {"type": "done"}
    except Exception as e:  # noqa: BLE001 - stream errors to the UI
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}


def _safe(x):
    try:
        json.dumps(x)
        return x
    except (TypeError, ValueError):
        return str(x)

"""✎ FILES assistant: ask about — or edit — the ONE file open in the editor.

Deliberately not the agent: a single streamed model call with NO tools, so
it physically cannot touch anything but the text it's shown, and it answers
in seconds instead of planning. It sees the editor's CURRENT buffer (unsaved
edits included), the user's selection, and the last few exchanges about this
file. Changes come back as search/replace blocks the editor shows as a diff
and applies only when the user says so (then SAVE is theirs) — a full-file
rewrite would cost ~30 tok/s of decode per line of file on this box.
"""
from __future__ import annotations

from typing import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from . import agent

MAX_FILE_CHARS = 120_000   # ~30k tokens: fine for sglang's 262k ctx and ~1 s of prefill
MAX_HISTORY = 6

SYSTEM = """You are LangBang's file assistant. You work on exactly ONE file — the one shown to you — and you have no tools: you cannot run commands, read other files or touch anything else.

If the user asks a question about the file, answer briefly in Markdown. Do not output edit blocks.

If the user asks for a change, write ONE short sentence saying what you changed, then one or more edit blocks, each exactly in this form:

<<<<<<< SEARCH
exact lines copied verbatim from the CURRENT file (same whitespace and indentation), just enough to be unique
=======
the replacement lines
>>>>>>> REPLACE

Rules for edit blocks:
- SEARCH must match the current file exactly; never paraphrase or abbreviate it, never add line numbers.
- Keep blocks small: only the lines that change plus a little context. Several blocks are fine.
- To insert new lines, SEARCH a neighbouring line and repeat it in the replacement together with the new lines.
- To delete, leave the replacement empty.
- If the file is empty, use an empty SEARCH section and put the whole content in the replacement.
- Do not wrap edit blocks in ``` fences. Do not reprint the whole file."""


def _numbered_hint(content: str, sel: dict | None) -> str:
    if not sel or not sel.get("text"):
        return ""
    return (f"\n\nThe user has selected lines {sel.get('from_line')}–{sel.get('to_line')}:\n"
            f"<<<SELECTION\n{sel['text'][:20000]}\nSELECTION>>>")


async def stream(path: str, lang: str | None, content: str, instruction: str,
                 selection: dict | None, history: list[dict], s: dict) -> AsyncIterator[dict]:
    if len(content) > MAX_FILE_CHARS:
        yield {"type": "error", "message": f"file is too large for the assistant ({len(content):,} chars; max {MAX_FILE_CHARS:,})"}
        return
    file_block = (f"File: {path}" + (f" ({lang})" if lang else "") +
                  f"\n<<<FILE\n{content}\nFILE>>>")
    msgs = [SystemMessage(content=SYSTEM)]
    # history turns re-anchor on the CURRENT file text (it may have changed
    # since, through applied edits or typing) — only the newest turn carries it
    for h in history[-MAX_HISTORY:]:
        text = str(h.get("text") or "")[:8000]
        if h.get("role") == "user":
            msgs.append(HumanMessage(content=text))
        elif h.get("role") == "assistant":
            msgs.append(AIMessage(content=text))
    msgs.append(HumanMessage(content=file_block + _numbered_hint(content, selection) +
                             "\n\nRequest: " + instruction.strip()))
    model = agent.SGlangChatOpenAI(
        model=s["model"], base_url=s["base_url"], api_key=s["api_key"],
        temperature=0.2, max_tokens=4096, streaming=True,
        extra_body=agent.extra_body(s, thinking=False),
        stream_chunk_timeout=600,
    )
    try:
        async for chunk in model.astream(msgs):
            if isinstance(chunk.content, str) and chunk.content:
                yield {"type": "token", "text": chunk.content}
        yield {"type": "done"}
    except Exception as e:  # noqa: BLE001 - surfaced in the assist panel
        yield {"type": "error", "message": f"{type(e).__name__}: {str(e)[:300]}"}

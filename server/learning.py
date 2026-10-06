"""Per-thread prompts, LangBang memory, and the automatic skill/memory review.

Three pieces, one module because they share one idea — what the agent knows
beyond the current thread:

* Thread prompt — free text stored per thread (threads.prompt), appended to
  the global system prompt on every run in that thread (scheduled runs too).
* Memory — one markdown file per fact under `memory_dir` (default
  ~/.langbang/memory), YAML frontmatter {name, description, type}. The index
  injected into every run's system prompt is built FROM THE FILES (no
  separate index to drift); the agent reads a memory's body on demand.
  Tools: remember / forget. The user can edit the files in FILES.
* Review — after a substantial interactive run (>= min_tool_calls tool calls,
  finished normally), a background no-tool model call reads the turn's
  transcript and may create/patch a LangBang skill and/or save memories.
  Every change is validated (skill frontmatter must parse — the 10-04
  gmail-inbox-triage YAML break went unnoticed for a day), the previous
  version is kept under ~/.langbang/skill-history/, and the change is logged
  (data/learning.jsonl) + announced in the notification feed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import yaml
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from . import config

logger = logging.getLogger("langbang.learning")

LOG_PATH = os.path.join(config.DATA_DIR, "learning.jsonl")
HISTORY_DIR = "~/.langbang/skill-history"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MEM_TYPES = ("user", "feedback", "project", "reference")
INDEX_CAP = 6000          # chars of memory index in the system prompt
THREAD_PROMPT_CAP = 8000  # chars of per-thread instructions
TRANSCRIPT_CAP = 60_000   # chars of turn transcript handed to the reviewer
SKILL_BODY_CAP = 20_000   # chars of an existing skill shown for patching


def defaults() -> dict:
    return {"enabled": True, "min_tool_calls": 6, "scheduled": False}


def review_settings(s: dict | None = None) -> dict:
    s = s or config.load()
    return {**defaults(), **(s.get("skill_review") or {})}


def _skills_dir(s: dict) -> str:
    return os.path.expanduser(s.get("skills_dir") or "~/.langbang/skills")


def memory_dir(s: dict | None = None) -> str:
    s = s or config.load()
    d = os.path.expanduser(s.get("memory_dir") or "~/.langbang/memory")
    os.makedirs(d, exist_ok=True)
    return d


# ---- frontmatter ----

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict | None, str, str | None]:
    """(meta, body, error). Same delimiters/loader as deepagents' skill parser."""
    m = _FM.match(text or "")
    if not m:
        return None, text or "", "no YAML frontmatter (file must start with a '---' block)"
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        msg = str(e).split("\n")[0]
        return None, text[m.end():], (f"invalid YAML in frontmatter: {msg} — quote values that "
                                      "contain ': ' or start with a special character")
    if not isinstance(meta, dict):
        return None, text[m.end():], "frontmatter is not a key: value mapping"
    return meta, text[m.end():], None


def validate_skill(text: str, dirname: str) -> str | None:
    """None if SKILL.md content would load; else a fix-it message."""
    meta, body, err = parse_frontmatter(text)
    if err:
        return err
    name = str(meta.get("name") or "").strip()
    desc = str(meta.get("description") or "").strip()
    if not name or not desc:
        return "frontmatter needs both `name` and `description`"
    if name != dirname:
        return f"frontmatter name '{name}' must equal the folder name '{dirname}'"
    if not NAME_RE.match(name) or len(name) > 64:
        return f"name '{name}' must be lowercase letters/digits/hyphens (max 64)"
    if len(desc) > 1024:
        return f"description is {len(desc)} chars (max 1024)"
    if not body.strip():
        return "skill body is empty"
    return None


def _dump_frontmatter(meta: dict) -> str:
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True,
                                     width=10_000).strip() + "\n---\n"


def check_written_skill(path: str, s: dict | None = None) -> str | None:
    """Hook for file-writing tools: if `path` is a LangBang SKILL.md, validate
    it from disk and return a warning for the model (None = fine/not a skill)."""
    s = s or config.load()
    root = os.path.realpath(_skills_dir(s))
    p = os.path.realpath(os.path.expanduser(path))
    if os.path.basename(p) != "SKILL.md" or os.path.dirname(os.path.dirname(p)) != root:
        return None
    try:
        text = open(p, encoding="utf-8").read()
    except OSError:
        return None
    err = validate_skill(text, os.path.basename(os.path.dirname(p)))
    return None if err is None else (
        f"⚠ SKILL CHECK FAILED for {p}: {err}. The skill will NOT load until this is "
        "fixed — fix the file now.")


# ---- thread prompt ----

async def get_thread_prompt(db, tid: str) -> str:
    cur = await db.execute("SELECT prompt FROM threads WHERE id=?", (tid,))
    r = await cur.fetchone()
    return (r[0] if r else "") or ""


# ---- memory ----

def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:64]


def list_memories(s: dict | None = None) -> list[dict]:
    d = memory_dir(s)
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".md") or fn.startswith("."):
            continue
        p = os.path.join(d, fn)
        try:
            text = open(p, encoding="utf-8").read()
        except OSError:
            continue
        meta, body, err = parse_frontmatter(text)
        meta = meta or {}
        out.append({
            "name": str(meta.get("name") or fn[:-3]),
            "description": str(meta.get("description") or body.strip().split("\n")[0][:150]),
            "type": str(meta.get("type") or ""),
            "path": p, "mtime": os.path.getmtime(p), "chars": len(body),
            **({"error": err} if err else {}),
        })
    return out


def write_memory(name: str, description: str, content: str, type_: str = "project",
                 s: dict | None = None) -> dict:
    slug = _slug(name)
    if not slug:
        raise ValueError("memory name must contain letters or digits")
    if type_ not in MEM_TYPES:
        type_ = "project"
    description = " ".join((description or "").split())[:200]
    if not description:
        raise ValueError("description is required (one line: what this memory is)")
    if not (content or "").strip():
        raise ValueError("content is empty")
    p = os.path.join(memory_dir(s), slug + ".md")
    existed = os.path.exists(p)
    text = _dump_frontmatter({"name": slug, "description": description, "type": type_}) \
        + "\n" + content.strip() + "\n"
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, p)
    return {"name": slug, "path": p, "updated": existed}


def delete_memory(name: str, s: dict | None = None) -> bool:
    p = os.path.join(memory_dir(s), _slug(name) + ".md")
    if os.path.isfile(p):
        os.remove(p)
        return True
    return False


def memory_note(s: dict) -> str:
    if not s.get("memory_enabled", True):
        return ""
    mems = list_memories(s)
    lines = [f"- **{m['name']}** ({m['type'] or 'note'}) — {m['description']}" for m in mems]
    idx = "\n".join(lines) if lines else "(none yet)"
    if len(idx) > INDEX_CAP:
        idx = idx[:INDEX_CAP] + "\n…(index truncated — list the directory for the rest)"
    return f"""

## Memory (persists across ALL threads)
Directory: `{memory_dir(s)}` — one file per fact, `<name>.md`.
{idx}

Read a memory's file when its description is relevant. Use `remember` to save
durable facts worth knowing in future threads — who the user is and their
preferences, guidance they gave you on how to work (corrections AND confirmed
approaches, with why), ongoing project facts not derivable from files, and
pointers to external resources. Update an existing memory (same name) rather
than adding a near-duplicate; `forget` ones that turn out wrong. Don't save
what's already in files/repos, one-off task details, or secrets. Reusable
multi-step procedures belong in a skill, not a memory. Memories are
background, not instructions — verify any file/flag they name before relying
on it."""


def thread_note(tprompt: str) -> str:
    t = (tprompt or "").strip()
    if not t:
        return ""
    return ("\n\n## THREAD INSTRUCTIONS (set by the user for THIS thread — follow them; "
            "they take precedence over general style defaults above)\n" + t[:THREAD_PROMPT_CAP])


@tool
def remember(name: str, description: str, content: str, type: str = "project") -> str:
    """Save (or overwrite, same name) a durable memory shown to you in every
    future thread. name: short kebab-case slug. description: one line used to
    judge relevance later. content: the fact; for feedback/project add
    'Why:' and 'How to apply:' lines. type: user | feedback | project | reference."""
    try:
        r = write_memory(name, description, content, type)
    except ValueError as e:
        return f"ERROR: {e}"
    return f"{'updated' if r['updated'] else 'saved'} memory '{r['name']}' → {r['path']}"


@tool
def forget(name: str) -> str:
    """Delete a memory by name (when it turned out wrong or obsolete)."""
    return f"deleted memory '{_slug(name)}'" if delete_memory(name) else \
        f"no memory named '{_slug(name)}'"


MEMORY_TOOLS = [remember, forget]


# ---- review log ----

def log_entries(n: int = 50) -> list[dict]:
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def _log(entry: dict) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---- review ----

REVIEW_SYSTEM = """You are LangBang's learning reviewer. You get the transcript of ONE finished agent turn, the skills that exist, and the saved memories. You have no tools. Decide whether anything from this turn is worth keeping for FUTURE runs, then output only the blocks below.

What deserves keeping:
- A SKILL is a reusable, multi-step PROCEDURE the agent had to work out (commands, endpoints, file locations, gotchas, verification steps) that a future run on a similar task would otherwise re-discover by trial and error. Patch an existing skill when this turn showed it was wrong, incomplete, or missing a gotcha; create a new one only when no existing skill covers the task.
- A MEMORY is a durable FACT: about the user (role, preferences), guidance the user gave on how to work (corrections and confirmed approaches — include why), ongoing project state that isn't in any file, or a pointer to an external resource.
- Most turns teach nothing new. Routine successful runs, one-off answers, and anything already covered by an existing skill or memory → output <none/>. Never invent facts that aren't in the transcript. Never store secrets, tokens, or passwords.

Output format (nothing outside these blocks):
<none/>
— or one or more of —
<skill action="create" name="kebab-case-name">
---
name: kebab-case-name
description: "When to use it: the trigger conditions, in one or two sentences."
---
# Title
...procedure, commands, gotchas, how to verify...
</skill>
<skill action="patch" name="existing-name">
<<<<<<< SEARCH
exact existing lines
=======
replacement lines
>>>>>>> REPLACE
</skill>
<memory name="kebab-case-name" type="user|feedback|project|reference" description="one line">
the fact (feedback/project: add Why: and How to apply: lines)
</memory>
<reason>one or two sentences: what you kept and why (or why nothing)</reason>

Rules: patch only skills whose FULL TEXT is shown to you (SEARCH must match that text exactly); always quote the description value in frontmatter; the folder name equals `name`; a memory with an existing name overwrites it — use that to update rather than duplicate."""


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return str(content or "")


def _turn(msgs: list) -> tuple[list, HumanMessage | None]:
    """Messages of the last user turn (from the last real HumanMessage). A
    turn compacted mid-way has no real HumanMessage left — its request lives
    in the compaction note (quoted as CURRENT REQUEST), so start there."""
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if isinstance(m, HumanMessage):
            return msgs[i:], m
    return msgs, None


def transcript(turn: list) -> tuple[str, int, list[str]]:
    """(compact text, tool-call count, paths read via read_file)."""
    parts, n_tools, reads = [], 0, []
    for m in turn:
        if isinstance(m, HumanMessage):
            parts.append("USER: " + _text(m.content)[:4000])
        elif isinstance(m, AIMessage):
            t = _text(m.content).strip()
            if t:
                parts.append("AGENT: " + t[:3000])
            for tc in m.tool_calls or []:
                n_tools += 1
                args = json.dumps(tc.get("args"), ensure_ascii=False)
                parts.append(f"CALL {tc.get('name')}: {args[:1200]}")
                if tc.get("name") == "read_file":
                    p = (tc.get("args") or {}).get("file_path") or (tc.get("args") or {}).get("path")
                    if p:
                        reads.append(str(p))
        elif isinstance(m, ToolMessage):
            status = " (ERROR)" if getattr(m, "status", None) == "error" else ""
            parts.append(f"RESULT {m.name}{status}: {_text(m.content)[:900]}")
    text = "\n".join(parts)
    if len(text) > TRANSCRIPT_CAP:  # keep the start (task) and the end (outcome)
        half = TRANSCRIPT_CAP // 2
        text = text[:half] + "\n…[middle of turn omitted]…\n" + text[-half:]
    return text, n_tools, reads


def _skill_catalog(s: dict, agent_mod) -> list[dict]:
    """Every skill both sources expose: name, description, path, own(=LangBang)."""
    out = {}
    lb = os.path.realpath(_skills_dir(s))
    for src, _label in agent_mod._skill_sources(s):
        try:
            names = sorted(os.listdir(src))
        except OSError:
            continue
        for d in names:
            p = os.path.join(src, d, "SKILL.md")
            if d.startswith(".") or not os.path.isfile(p):
                continue
            try:
                meta, _, _ = parse_frontmatter(open(p, encoding="utf-8").read())
            except OSError:
                continue
            meta = meta or {}
            out[d] = {"name": d, "description": str(meta.get("description") or "")[:300],
                      "path": p, "own": os.path.realpath(src) == lb}
    return list(out.values())


_BLOCK = re.compile(r"<(skill|memory)\b([^>]*)>\n?(.*?)\n?</\1>", re.DOTALL)
_ATTR = re.compile(r'(\w+)="([^"]*)"')
_EDIT = re.compile(r"<{7} SEARCH\n(.*?)\n?={7}\n(.*?)\n?>{7} REPLACE", re.DOTALL)


def parse_review(text: str) -> tuple[list[dict], str]:
    acts = []
    for kind, attrs, body in _BLOCK.findall(text or ""):
        a = dict(_ATTR.findall(attrs))
        acts.append({"kind": kind, **a, "body": body})
    m = re.search(r"<reason>(.*?)</reason>", text or "", re.DOTALL)
    return acts, (m.group(1).strip() if m else "")


def apply_patch(original: str, body: str) -> str:
    edits = _EDIT.findall(body)
    if not edits:
        raise ValueError("patch has no SEARCH/REPLACE block")
    out = original
    for search, repl in edits:
        if search not in out:
            raise ValueError("SEARCH text not found in the current skill")
        out = out.replace(search, repl, 1)
    return out


def _backup(name: str, path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    d = os.path.join(os.path.expanduser(HISTORY_DIR), name)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + ".md")
    with open(path, encoding="utf-8") as src, open(dst, "w", encoding="utf-8") as fh:
        fh.write(src.read())
    return dst


def apply_actions(acts: list[dict], s: dict, catalog: list[dict], shown: set[str]) -> list[dict]:
    """Validate + apply reviewer actions. Returns per-action results."""
    lb = _skills_dir(s)
    by_name = {c["name"]: c for c in catalog}
    results = []
    for a in acts:
        name = _slug(a.get("name", ""))
        try:
            if a["kind"] == "memory":
                r = write_memory(name, a.get("description", ""), a["body"], a.get("type", "project"), s)
                results.append({"kind": "memory", "name": r["name"], "path": r["path"],
                                "op": "updated" if r["updated"] else "saved"})
                continue
            act = a.get("action", "")
            cur = by_name.get(name)
            if act == "create":
                if cur and not cur["own"]:
                    raise ValueError("a Hermes skill has that name (read-only) — not overriding it")
                if cur and cur["own"]:
                    raise ValueError("skill exists — the reviewer must patch it, not recreate it")
                new = a["body"].strip() + "\n"
            elif act == "patch":
                if not cur or not cur["own"]:
                    raise ValueError("only existing LangBang skills can be patched")
                if name not in shown:
                    raise ValueError("skill text wasn't shown to the reviewer")
                new = apply_patch(open(cur["path"], encoding="utf-8").read(), a["body"])
            else:
                raise ValueError(f"unknown skill action '{act}'")
            err = validate_skill(new, name)
            if err:
                raise ValueError(f"result would not load: {err}")
            path = os.path.join(lb, name, "SKILL.md")
            backup = _backup(name, path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                fh.write(new)
            os.replace(path + ".tmp", path)
            results.append({"kind": "skill", "name": name, "path": path,
                            "op": "created" if act == "create" else "patched",
                            **({"backup": backup} if backup else {})})
        except (ValueError, KeyError, OSError) as e:
            results.append({"kind": a.get("kind"), "name": name, "op": "rejected", "error": str(e)})
    return results


_lock = asyncio.Lock()
_pending: set[str] = set()


def _qualifies(events: list[dict], s: dict) -> int:
    """Tool-call count if this finished run should be reviewed, else 0."""
    rs = review_settings(s)
    if not rs["enabled"]:
        return 0
    n = 0
    for ev in events:
        t = ev.get("type")
        if t in ("error", "gate", "shell_result"):
            return 0  # failed / cancelled / waiting on the user / a !cmd
        if t == "tool_start":
            n += 1
    return n if n >= int(rs["min_tool_calls"]) else 0


async def after_run(tid: str, events: list[dict]) -> None:
    """runs._produce hook: maybe review the turn that just finished."""
    s = config.load()
    if not _qualifies(events, s) or tid in _pending:
        return
    try:
        await review(tid, s, auto=True)
    except Exception:  # noqa: BLE001 - learning must never disturb runs
        logger.exception("skill review failed for %s", tid)


async def review(tid: str, s: dict | None = None, auto: bool = False) -> dict:
    """Review the last turn of `tid`. Returns the log entry."""
    from . import agent, notify  # cycle-safe

    s = s or config.load()
    _pending.add(tid)
    try:
        async with _lock:  # one review at a time: it's an extra sglang stream
            msgs = await agent._live_messages(tid)
            turn, human = _turn(msgs)
            if human is None:
                return {"skipped": "no user turn"}
            sched = (human.additional_kwargs or {}).get("lb_sched")
            if auto and sched and not review_settings(s)["scheduled"]:
                return {"skipped": "scheduled run"}
            text, n_tools, reads = transcript(turn)
            catalog = _skill_catalog(s, agent)
            real_reads = {os.path.realpath(os.path.expanduser(p)) for p in reads}
            shown = {c["name"] for c in catalog
                     if c["own"] and os.path.realpath(c["path"]) in real_reads}
            listing = "\n".join(
                f"- {c['name']} [{'LangBang' if c['own'] else 'Hermes, read-only'}]: {c['description']}"
                for c in catalog) or "(none)"
            full = "".join(
                f"\n\n=== FULL TEXT of LangBang skill '{c['name']}' ===\n"
                + open(c["path"], encoding="utf-8").read()[:SKILL_BODY_CAP]
                for c in catalog if c["name"] in shown)
            mems = "\n".join(f"- {m['name']} ({m['type']}): {m['description']}"
                             for m in list_memories(s)) or "(none)"
            prompt = (f"## Existing skills\n{listing}{full}\n\n## Saved memories\n{mems}\n\n"
                      f"## The turn ({n_tools} tool calls)\n{text}")
            llm = agent.summarizer({**s, "compact_summary_tokens": 4000})
            t0 = time.time()
            resp = await llm.ainvoke([SystemMessage(content=REVIEW_SYSTEM),
                                      HumanMessage(content=prompt)])
            out = _text(resp.content)
            acts, reason = parse_review(out)
            results = apply_actions(acts, s, catalog, shown)
            title = await notify._title(tid)
            entry = {"ts": time.time(), "tid": tid, "title": title, "auto": auto,
                     "tool_calls": n_tools, "seconds": round(time.time() - t0, 1),
                     "results": results, "reason": reason[:600]}
            _log(entry)
            done = [r for r in results if r["op"] != "rejected"]
            logger.info("review %s: %s", tid, [(r["kind"], r["name"], r["op"]) for r in results])
            if done:
                notify.learned(tid, title, done)
            return entry
    finally:
        _pending.discard(tid)

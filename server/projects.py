"""◫ Projects: threads grouped by what they work on.

A project is a name plus an optional root directory (usually a git repo).
Threads carry `threads.project` (a project id, NULL = unfiled); new chats
inherit the project picked in the sidebar, ⑂ forks inherit their source's.

What it scopes: the sidebar filter, chat search, auto-search thread hits
(prefetch — a thread never gets "hints" from another project's threads),
project-tagged memories, and the ⏱ "since you were last here" check, which
reads git history under the root (agent.staleness).

Auto-filing: every run's tool calls are scanned for paths (file tools'
paths, absolute paths and `cd` targets in run_bash) and mapped to their git
root. At the end of a run:
  * an UNFILED thread whose run worked mostly in one repo is filed into the
    project with that root — created (auto=1, named after the folder) if
    there is none;
  * a FILED thread whose run worked mostly somewhere else gets a `drift`
    suggestion (never moved silently): the UI offers to move it to that
    repo's project or a new one. That's how a fork that went off to become
    its own idea graduates instead of polluting its parent's project.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import time
import uuid
from collections import Counter
from functools import lru_cache

HOME = os.path.expanduser("~")
AUTO_MIN_HITS = 3   # tool-call paths in ONE run before a repo files / drifts a thread
_FILE_KEYS = ("file_path", "path", "dir", "directory", "cwd")
_ABS = re.compile(r"""(?<![\w.])(?:~|/)[^\s'"`;|&<>()$]+""")


def _db():
    from . import agent  # agent owns the connection (and imports us lazily)
    return agent._db


def _wt():
    from . import agent
    return agent._wt(agent._db)


def _norm_root(root: str) -> str:
    r = os.path.realpath(os.path.expanduser((root or "").strip())) if (root or "").strip() else ""
    return r.rstrip("/") or ("/" if r else "")


def _row(r) -> dict:
    return {"id": r[0], "name": r[1], "root": r[2] or "", "created_at": r[3], "auto": bool(r[4])}


# ---- CRUD ----

async def list_projects() -> list[dict]:
    cur = await _db().execute(
        "SELECT p.id,p.name,p.root,p.created_at,p.auto,"
        " (SELECT COUNT(*) FROM threads t WHERE t.project=p.id) FROM projects p ORDER BY lower(p.name)")
    return [{**_row(r), "threads": r[5]} for r in await cur.fetchall()]


async def get(pid: str | None) -> dict | None:
    if not pid:
        return None
    r = await (await _db().execute(
        "SELECT id,name,root,created_at,auto FROM projects WHERE id=?", (pid,))).fetchone()
    return _row(r) if r else None


async def thread_project(tid: str) -> dict | None:
    r = await (await _db().execute("SELECT project FROM threads WHERE id=?", (tid,))).fetchone()
    return await get(r[0]) if r else None


async def by_root(root: str) -> dict | None:
    root = _norm_root(root)
    if not root:
        return None
    r = await (await _db().execute(
        "SELECT id,name,root,created_at,auto FROM projects WHERE root=?", (root,))).fetchone()
    return _row(r) if r else None


async def create(name: str, root: str = "", auto: bool = False) -> dict:
    name = " ".join((name or "").split())[:60]
    root = _norm_root(root)
    if not name and root:
        name = os.path.basename(root) or root
    if not name:
        raise ValueError("a project needs a name")
    if root and not os.path.isdir(root):
        raise ValueError(f"root is not a directory: {root}")
    if await (await _db().execute("SELECT 1 FROM projects WHERE lower(name)=lower(?)", (name,))).fetchone():
        if auto:  # auto-created names never collide: suffix the parent dir
            name = f"{name} ({os.path.basename(os.path.dirname(root)) or '/'})"[:60]
        else:
            raise ValueError(f"there is already a project named “{name}”")
    if root and await by_root(root):
        raise ValueError(f"“{(await by_root(root))['name']}” already has the root {root}")
    pid = uuid.uuid4().hex[:10]
    async with _wt():
        await _db().execute("INSERT INTO projects(id,name,root,created_at,auto) VALUES(?,?,?,?,?)",
                            (pid, name, root or None, time.time(), 1 if auto else 0))
    return await get(pid)


async def update(pid: str, name: str | None = None, root: str | None = None) -> dict:
    p = await get(pid)
    if not p:
        raise LookupError("no such project")
    if name is not None:
        name = " ".join(name.split())[:60]
        if not name:
            raise ValueError("a project needs a name")
        if await (await _db().execute("SELECT 1 FROM projects WHERE lower(name)=lower(?) AND id!=?",
                                      (name, pid))).fetchone():
            raise ValueError(f"there is already a project named “{name}”")
    if root is not None:
        root = _norm_root(root)
        if root and not os.path.isdir(root):
            raise ValueError(f"root is not a directory: {root}")
        other = await by_root(root) if root else None
        if other and other["id"] != pid:
            raise ValueError(f"“{other['name']}” already has the root {root}")
    async with _wt():
        await _db().execute(
            "UPDATE projects SET name=COALESCE(?,name), root=CASE WHEN ? THEN ? ELSE root END, auto=0 WHERE id=?",
            (name, root is not None, root or None, pid))
    if name is not None and name != p["name"]:
        from . import learning  # memories are scoped by project NAME (readable in the files)
        learning.retag_memories(p["name"], name)
    return await get(pid)


async def delete(pid: str) -> bool:
    """Threads are kept, just unfiled; its scoped memories become global."""
    p = await get(pid)
    if p:
        from . import learning
        learning.retag_memories(p["name"], "")
    async with _wt():
        await _db().execute("UPDATE threads SET project=NULL WHERE project=?", (pid,))
        cur = await _db().execute("DELETE FROM projects WHERE id=?", (pid,))
    return cur.rowcount > 0


async def set_thread(tid: str, pid: str | None) -> bool:
    """File a thread (None = unfiled). Clears any drift suggestion."""
    if pid and not await get(pid):
        raise LookupError("no such project")
    async with _wt():
        cur = await _db().execute("UPDATE threads SET project=?, drift=NULL WHERE id=?", (pid, tid))
    return cur.rowcount > 0


async def dismiss_drift(tid: str) -> None:
    async with _wt():
        await _db().execute("UPDATE threads SET drift=NULL WHERE id=?", (tid,))


async def others_tids(pid: str | None) -> set[str]:
    """Threads filed under a DIFFERENT project than `pid` — whose content
    must not leak into a `pid` thread as cross-thread hints. Unfiled threads
    (and every thread, when `pid` is None) stay visible."""
    if not pid:
        return set()
    cur = await _db().execute("SELECT id FROM threads WHERE project IS NOT NULL AND project!=?", (pid,))
    return {r[0] for r in await cur.fetchall()}


# ---- auto-filing ----

@lru_cache(maxsize=4096)
def git_root(path: str) -> str:
    """Nearest ancestor holding .git, '' when none below $HOME (or outside it).
    Meta trees (~/.hermes, ~/.langbang, ~/.claude, any dot-dir straight under
    $HOME) never count: reading a skill isn't working in its repo."""
    p = os.path.realpath(os.path.expanduser(path))
    if not p.startswith(HOME + "/"):
        return ""
    rel = p[len(HOME) + 1:]
    if rel.startswith("."):
        return ""
    d = p if os.path.isdir(p) else os.path.dirname(p)
    while d.startswith(HOME + "/"):
        if os.path.exists(os.path.join(d, ".git")):
            return d
        d = os.path.dirname(d)
    return ""


def paths_in(tool: str, args) -> list[str]:
    """Paths a tool call works on: file tools' path args, plus absolute paths
    and `cd` targets inside run_bash commands."""
    if not isinstance(args, dict):
        return []
    out = [str(args[k]) for k in _FILE_KEYS if isinstance(args.get(k), str) and args[k].strip()]
    cmd = args.get("command") if tool in ("run_bash", "bash", "bg_start") else None
    if isinstance(cmd, str):
        try:
            toks = shlex.split(cmd, posix=True)
        except ValueError:
            toks = cmd.split()
        for i, t in enumerate(toks):
            if t == "cd" and i + 1 < len(toks):
                out.append(toks[i + 1])
        out += _ABS.findall(cmd)
    return [p for p in out if p.startswith(("/", "~"))]


def tally(tool_calls: list[tuple[str, dict]]) -> Counter:
    c: Counter = Counter()
    for name, args in tool_calls:
        roots = {git_root(p) for p in paths_in(name, args)} - {""}
        c.update(roots)  # once per call, however many paths it named
    return c


def _under(root: str, base: str) -> bool:
    return bool(base) and (root == base or root.startswith(base.rstrip("/") + "/"))


async def observe(tid: str, tool_calls: list[tuple[str, dict]]) -> dict | None:
    """End-of-run auto-filing (see module doc). → {"filed": project} |
    {"drift": {...}} | None."""
    c = tally(tool_calls)
    if not c:
        return None
    root, n = c.most_common(1)[0]
    if n < AUTO_MIN_HITS:
        return None
    r = await (await _db().execute("SELECT project, archived FROM threads WHERE id=?", (tid,))).fetchone()
    if not r or r[1] is not None:
        return None
    cur = await get(r[0])
    match = await by_root(root)
    if match is None:  # a sub-folder repo of a project's root belongs to it
        for p in await list_projects():
            if p["root"] and _under(root, p["root"]):
                match = p
                break
    if cur is None:
        if match is None:
            match = await create(os.path.basename(root), root, auto=True)
        await set_thread(tid, match["id"])
        return {"filed": match}
    if cur["root"] and (_under(root, cur["root"]) or c.get(cur["root"], 0) * 2 >= n):
        return None  # still (mostly) home
    if not cur["root"] and (match is None or match["id"] == cur["id"]):
        return None  # a root-less project can't be drifted from by an unknown repo
    if match and match["id"] == cur["id"]:
        return None
    d = {"root": root, "hits": n, "ts": time.time(),
         **({"project": {"id": match["id"], "name": match["name"]}} if match else {})}
    async with _wt():
        await _db().execute("UPDATE threads SET drift=? WHERE id=?", (json.dumps(d), tid))
    return {"drift": d}


def short(path: str) -> str:
    return "~" + path[len(HOME):] if path.startswith(HOME) else path



# ---- ⏱ get current: what changed since this thread last ran ----

GIT_LOG_N = 8
DIRTY_N = 15


def _ago(sec: float) -> str:
    sec = max(0, int(sec))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if sec >= n:
            big, rest = divmod(sec, n)
            sub = {"d": 3600, "h": 60}.get(unit)
            return f"{big}{unit}" + (f" {rest // sub}{'h' if unit == 'd' else 'm'}" if sub and rest // sub else "")
    return f"{sec}s"


def _git(root: str, *args: str) -> str:
    import subprocess
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - not a repo / git missing / slow FS
        return ""


def _repo_since(root: str, since: float) -> dict:
    at = f"@{int(since)}"
    log = _git(root, "log", f"--since={at}", "--format=%h %ar · %s", "-n", str(GIT_LOG_N + 1))
    commits = [ln for ln in log.splitlines() if ln.strip()]
    n = len(commits)
    if n > GIT_LOG_N:
        n = int(_git(root, "rev-list", "--count", f"--since={at}", "HEAD") or n)
    st = _git(root, "status", "--short", "--branch").splitlines()
    branch = st[0][3:] if st and st[0].startswith("## ") else ""
    dirty = [ln for ln in st[1:] if ln.strip()]
    return {"commits": n, "log": commits[:GIT_LOG_N], "branch": branch,
            "dirty": len(dirty), "dirty_list": dirty[:DIRTY_N]}


async def _last_active(tid: str) -> float | None:
    """Newest trajectory row of this thread — real activity, unlike
    threads.updated_at, which merely OPENING a thread bumps. A fork with no
    turns of its own yet inherits its source's last activity."""
    from . import agent
    r = await (await agent._edb.execute(
        "SELECT MAX(ts) FROM run_events WHERE thread_id=?", (tid,))).fetchone()
    if r and r[0]:
        return float(r[0])
    row = await (await _db().execute("SELECT parent FROM threads WHERE id=?", (tid,))).fetchone()
    try:
        p = json.loads(row[0]) if row and row[0] else None
    except ValueError:
        p = None
    return await _last_active(p["tid"]) if p and p.get("tid") and p["tid"] != tid else None


async def staleness(tid: str, s: dict) -> dict | None:
    """→ {note, ago_s, last, project?, root?, commits, dirty, others} or
    None when the thread is current enough to need no reminder."""
    import asyncio
    cfg = s.get("staleness") or {}
    if not cfg.get("enabled", True):
        return None
    last = await _last_active(tid)
    if not last:
        return None
    gap = time.time() - last
    if gap < float(cfg.get("min_gap_min", 10)) * 60:
        return None
    proj = await thread_project(tid)
    repo = await asyncio.to_thread(_repo_since, proj["root"], last) if proj and proj["root"] else None
    others = []
    if proj:
        from . import agent
        cur = await _db().execute("SELECT id,title FROM threads WHERE project=? AND id!=?", (proj["id"], tid))
        titles = dict(await cur.fetchall())
        ids = list(titles)[:500]
        if ids:
            cur = await agent._edb.execute(
                f"SELECT thread_id, MAX(ts) FROM run_events WHERE thread_id IN ({','.join('?' * len(ids))}) "
                "GROUP BY thread_id", ids)
            for oid, ts in await cur.fetchall():
                if ts and ts > last:
                    others.append({"tid": oid, "title": titles[oid] or "", "ago_s": round(time.time() - ts)})
        others.sort(key=lambda o: o["ago_s"])
    long_gap = gap >= float(cfg.get("after_h", 6)) * 3600
    if not (long_gap or (repo and repo["commits"]) or others):
        return None
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last))
    lines = [f"⏱ GET CURRENT — this thread was last active {_ago(gap)} ago ({when}); "
             f"it is now {time.strftime('%Y-%m-%d %H:%M')}. Everything earlier in this conversation "
             "about files, processes, services, branches and todos describes THEN, not now."]
    if proj:
        lines.append(f"◫ Project “{proj['name']}”" + (f" — {short(proj['root'])}" if proj["root"] else ""))
    if repo:
        if repo["commits"]:
            lines.append(f"Since then: {repo['commits']} commit(s)"
                         + (f" on {repo['branch']}" if repo["branch"] else "") + ":")
            lines += ["  " + c for c in repo["log"]]
        else:
            lines.append("No commits since then" + (f" (on {repo['branch']})" if repo["branch"] else "") + ".")
        if repo["dirty"]:
            lines.append(f"Uncommitted now: {repo['dirty']} path(s):")
            lines += ["  " + d for d in repo["dirty_list"]]
    if others:
        lines.append("Other threads in this project ran since — they may have changed the same files:")
        lines += [f"  “{o['title'][:70]}” ({_ago(o['ago_s'])} ago)" for o in others[:5]]
    lines.append("Re-check current state before acting on earlier conclusions: re-read files before "
                 "editing them, and don't assume earlier tool output still holds.")
    return {"note": "\n".join(lines), "ago_s": round(gap), "last": last,
            **({"project": proj["name"]} if proj else {}),
            **({"root": short(proj["root"])} if proj and proj["root"] else {}),
            "commits": repo["commits"] if repo else None, "dirty": repo["dirty"] if repo else None,
            "others": others[:5], "long": long_gap}

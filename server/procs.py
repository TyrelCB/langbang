"""⚙ Background processes the agent starts and LangBang tracks.

The agent used to wait on long work by sleep-polling inside run_bash
(`sleep 290; ssh … check`, ~44 h of runs parked that way by 10-20), each poll
a full model call, or by `nohup … &`, leaving untracked orphans in
langbang.service's cgroup. bg_start instead launches the command as its OWN
transient unit under the user's systemd manager (linger is on):

- detached from langbang.service — a LangBang restart or stop leaves it running;
- one cgroup per process — bg_stop (or STOP in the UI) kills the whole tree;
- output appends to data/procs/<id>/log, the exit code lands in .../exit
  (the unit is --collect'ed, so the file is the only record of it).

A 5 s poller (start_poller) notices exits, trims runaway logs, and WAKES the
owning thread: on exit (or on the first log line matching `wake_match`) it
starts a run there with the outcome and the log tail, so the agent can end its
turn instead of sleeping. Mid-run, the wake arrives as a steer instead. After
a LangBang restart the poller picks up where it left off from the procs table.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from langchain_core.tools import tool
from langgraph.config import get_config

from . import agent, config, runs

logger = logging.getLogger("langbang.procs")

DIR = Path(config.DATA_DIR) / "procs"
UNIT = "lb-proc-"
LOG_CAP = 64 * 1024 * 1024  # a log past this keeps only its last LOG_KEEP
LOG_KEEP = 8 * 1024 * 1024
POLL_S = 5
WAKE_TAIL = 40  # log lines handed to the woken agent
HOST = os.uname().nodename
# A watcher the agent hand-rolls usually breaks the same few ways: checks run
# on the wrong machine (no such container / port), nested ssh quoting, a typo
# like $(out##*) for ${out##*}. Those print shell errors from the first loop
# iteration on — and then loop silently for 20 min. Lines like these in the
# first EARLY_S seconds wake the thread at once (the process keeps running).
EARLY_S = 180
SHELL_ERR = re.compile(
    r"command not found|syntax error|unexpected EOF|bad substitution|unbound variable"
    r"|No such container|Could not resolve hostname|Permission denied \(publickey", re.I)
_HOST_OK = re.compile(r"^[A-Za-z0-9_.@-]{1,100}$")


def _env() -> dict:
    run = f"/run/user/{os.getuid()}"
    return {**os.environ, "XDG_RUNTIME_DIR": run, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={run}/bus"}


def _systemctl(*args: str, timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], env=_env(), capture_output=True,
                          text=True, timeout=timeout)


def _paths(pid: str) -> tuple[Path, Path]:
    d = DIR / pid
    return d / "log", d / "exit"


def tail(pid: str, lines: int = 40, max_bytes: int = 64 * 1024) -> str:
    log, _ = _paths(pid)
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode(errors="replace")
    except FileNotFoundError:
        return ""
    out = data.splitlines()[-lines:]
    return "\n".join(out)


def _row(r) -> dict:
    keys = ("id", "thread_id", "name", "cmd", "started", "ended", "exit", "state",
            "wake", "wake_match", "woke", "host", "warned")
    d = dict(zip(keys, r))
    d["secs"] = round((d["ended"] or time.time()) - d["started"])
    return d


_COLS = "id,thread_id,name,cmd,started,ended,exit,state,wake,wake_match,woke,host,warned"


async def list_procs(thread_id: str | None = None, limit: int = 50) -> list[dict]:
    if thread_id:
        cur = await agent._db.execute(
            f"SELECT {_COLS} FROM procs WHERE thread_id=? ORDER BY started DESC LIMIT ?",
            (thread_id, limit))
    else:
        cur = await agent._db.execute(
            f"SELECT {_COLS} FROM procs ORDER BY (state='running') DESC, started DESC LIMIT ?",
            (limit,))
    out = []
    for r in await cur.fetchall():
        d = _row(r)
        d["last"] = tail(d["id"], 1)[-200:]
        out.append(d)
    return out


async def get(pid: str) -> dict | None:
    r = await (await agent._db.execute(f"SELECT {_COLS} FROM procs WHERE id=?", (pid,))).fetchone()
    return _row(r) if r else None


def _syntax_err(command: str) -> str:
    """bash -n: parse without running → "" or bash's complaint."""
    r = subprocess.run(["bash", "-n", "-c", command], capture_output=True, text=True, timeout=10)
    return "" if r.returncode == 0 else (r.stderr.strip() or "bash -n failed")


def _ssh_wrap(host: str, command: str) -> str:
    """Run `command` under bash -l on `host`. One shlex.quote per shell that
    parses it (the local bash, then the remote login shell), so the agent's
    command goes over verbatim — no hand-nested quotes. -tt: the remote side
    runs on a pty, so when bg_stop kills the local ssh the remote command gets
    SIGHUP and dies too (without it, the remote loop runs on orphaned);
    stty -onlcr keeps the log free of \r. BatchMode: never hang on a password
    prompt; keepalives: a dead link ends the unit (exit 255) instead of
    hanging forever."""
    remote = "stty -onlcr -echo 2>/dev/null; exec bash -lc " + shlex.quote(command)
    return ("ssh -q -tt -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30 "
            f"-o ServerAliveCountMax=6 {shlex.quote(host)} {shlex.quote(remote)} < /dev/null")


async def start(thread_id: str | None, command: str, name: str, wake: str = "exit",
                wake_match: str = "", max_hours: float = 24, host: str = "") -> dict:
    host = (host or "").strip()
    if host in ("localhost", "127.0.0.1", HOST):
        host = ""
    if host and not _HOST_OK.match(host):
        raise ValueError(f"bad host {host!r} (an ssh alias or user@host)")
    err = await asyncio.to_thread(_syntax_err, command)
    if err:
        raise ValueError(f"bash syntax error — nothing started:\n{err}")
    if wake not in ("exit", "match", "none"):
        raise ValueError("wake must be exit, match or none")
    if wake == "match":
        if not wake_match:
            raise ValueError("wake='match' needs wake_match (a regex)")
        re.compile(wake_match)  # bad regex → error now, not in the poller
    pid = uuid.uuid4().hex[:10]
    log, exitf = _paths(pid)
    log.parent.mkdir(parents=True, exist_ok=True)
    (log.parent / "cmd").write_text(command)
    run_cmd = _ssh_wrap(host, command) if host else command
    secs = max(60, int(min(float(max_hours), 24 * 7) * 3600))
    # bash -lc: the user's login env (the user manager's own env is bare);
    # >> appends (O_APPEND), so the poller's in-place trim keeps working
    wrapper = 'bash -lc "$0" >> "$1" 2>&1; echo $? > "$2"'
    r = await asyncio.to_thread(subprocess.run, [
        "systemd-run", "--user", "--unit", UNIT + pid, "--collect", "--quiet",
        "-p", f"RuntimeMaxSec={secs}", "--working-directory", str(Path.home()),
        "--description", f"LangBang bg: {name[:60]}",
        "/usr/bin/bash", "-c", wrapper, run_cmd, str(log), str(exitf),
    ], env=_env(), capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "systemd-run failed")
    now = time.time()
    async with agent._wt(agent._db):
        await agent._db.execute(
            "INSERT INTO procs(id,thread_id,name,cmd,started,state,wake,wake_match,host) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (pid, thread_id, name[:120], command, now, "running", wake, wake_match or None,
             host or None))
    return await get(pid)


async def stop(pid: str) -> dict | None:
    p = await get(pid)
    if not p:
        return None
    if p["state"] == "running":
        await asyncio.to_thread(_systemctl, "stop", UNIT + pid + ".service", timeout=30)
        async with agent._wt(agent._db):
            # woke stamped: a process the user stopped wakes nobody
            await agent._db.execute(
                "UPDATE procs SET state='stopped', ended=?, woke=COALESCE(woke,?) WHERE id=?",
                (time.time(), time.time(), pid))
    return await get(pid)


def _active_units() -> set[str]:
    r = _systemctl("list-units", UNIT + "*", "--no-legend", "--plain", "--all")
    out = set()
    for line in r.stdout.splitlines():
        parts = line.split()
        if parts and parts[0].startswith(UNIT) and len(parts) > 3 and parts[2] in ("active", "activating", "deactivating"):
            out.add(parts[0].removeprefix(UNIT).removesuffix(".service"))
    return out


def _trim_log(log: Path) -> int:
    """Keep a runaway log's tail (the 11 GB log of 10-18 must not repeat
    here). Returns how many bytes were cut off the front."""
    try:
        size = log.stat().st_size
        if size <= LOG_CAP:
            return 0
        with open(log, "r+b") as f:
            f.seek(size - LOG_KEEP)
            keep = f.read()
            f.seek(0)
            f.truncate(0)
            f.write(b"[... earlier output trimmed by LangBang ...]\n" + keep)
        return size - LOG_KEEP
    except OSError:
        return 0


def _new_text(log: Path, off: int) -> tuple[str, int]:
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            end = f.tell()
            if end < off:  # trimmed under us: start over from the top
                off = 0
            f.seek(max(off, end - 1024 * 1024))
            return f.read().decode(errors="replace"), end
    except FileNotFoundError:
        return "", off


def _fmt_dur(s: float) -> str:
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


async def _wake(p: dict, what: str, advice: str = "") -> bool:
    """Hand the outcome to the owning thread. False = try again next tick
    (run cap reached)."""
    tid = p["thread_id"]
    if not tid or not await (await agent._db.execute(
            "SELECT 1 FROM threads WHERE id=?", (tid,))).fetchone():
        return True
    if await agent.archived_why(tid):
        return True  # read-only now (merged/archived): the exit is in the PROCESSES bar
    where = f" on {p['host']}" if p.get("host") else f" on {HOST} (the LangBang host)"
    hint = ""
    if p.get("host") and p.get("exit") == 255:
        hint = (f"\nExit 255 is ssh's own code: the connection to {p['host']} failed or "
                "dropped — or something killed the remote shell (e.g. `pkill -f` matching "
                "its own command line). It says nothing about the command's result.")
    text = (f"⚙ Background process “{p['name']}” ({p['id']}){where} {what} after "
            f"{_fmt_dur(p['secs'])}. Last output:\n```\n{tail(p['id'], WAKE_TAIL)[-6000:]}\n```\n"
            f"(command: {p['cmd'][:300]}){hint}{advice}")
    info = {"id": p["id"], "name": p["name"], "what": what, "exit": p.get("exit")}
    try:
        runs.start(tid, agent.run_chat(tid, text, config.load(), wake=info))
    except runs.Busy:
        agent.steer_put(tid, text)  # mid-run: lands before the agent's next step
    except runs.TooMany:
        return False
    logger.warning("bg %s (%s) %s — woke thread %s", p["id"], p["name"], what, tid)
    return True


async def _tick() -> None:
    cur = await agent._db.execute(
        f"SELECT {_COLS}, log_off FROM procs WHERE state='running' "
        "OR (woke IS NULL AND wake!='none' AND state!='running')")
    rows = await cur.fetchall()
    if not rows:
        return
    active = await asyncio.to_thread(_active_units)
    for r in rows:
        p, off = _row(r[:-1]), r[-1] or 0
        log, exitf = _paths(p["id"])
        if p["state"] == "running":
            await asyncio.to_thread(_trim_log, log)
            if (p["thread_id"] and not p["warned"] and p["wake"] != "none"
                    and time.time() - p["started"] < EARLY_S):
                m = SHELL_ERR.search(await asyncio.to_thread(tail, p["id"], 60))
                if m and await _wake(p, f"started printing shell errors (“{m.group(0)}”)", (
                        "\nThe command itself is probably broken (wrong machine? quoting? "
                        "typo?) and it is STILL RUNNING: bg_stop it, run its check once with "
                        "run_bash, then bg_start the fixed version (host= for remote checks).")):
                    async with agent._wt(agent._db):
                        await agent._db.execute("UPDATE procs SET warned=? WHERE id=?",
                                                (time.time(), p["id"]))
            if p["wake"] == "match" and not p["woke"] and p["wake_match"]:
                text, end = await asyncio.to_thread(_new_text, log, off)
                m = re.search(p["wake_match"], text, re.M)
                ok = True
                if m:
                    ok = await _wake(p, f"printed a line matching /{p['wake_match']}/ "
                                        f"(“{m.group(0)[:200]}”) — still running")
                    if ok:
                        await agent._db.execute("UPDATE procs SET woke=? WHERE id=?", (time.time(), p["id"]))
                if ok:  # a deferred wake re-reads the same text next tick
                    await agent._db.execute("UPDATE procs SET log_off=? WHERE id=?", (end, p["id"]))
                await agent._db.commit()
            if p["id"] in active:
                continue
            code = None
            try:
                code = int(exitf.read_text().strip())
                state = "exited"
            except (OSError, ValueError):
                state = "lost"  # killed (RuntimeMaxSec, OOM, reboot) before it could record a code
            async with agent._wt(agent._db):
                await agent._db.execute(
                    "UPDATE procs SET state=?, exit=?, ended=? WHERE id=?",
                    (state, code, time.time(), p["id"]))
            p.update(state=state, exit=code, ended=time.time())
            p["secs"] = round(p["ended"] - p["started"])
        if p["wake"] == "exit" and not p["woke"] and p["state"] != "running":
            what = (f"exited with code {p['exit']}" if p["state"] == "exited"
                    else "was killed before it could record an exit code (timeout, OOM or reboot)")
            if await _wake(p, what):
                async with agent._wt(agent._db):
                    await agent._db.execute("UPDATE procs SET woke=? WHERE id=?", (time.time(), p["id"]))
        elif p["wake"] == "match" and p["state"] != "running" and not p["woke"]:
            # the match never came; nobody is waiting on an exit — just close it
            await agent._db.execute("UPDATE procs SET woke=? WHERE id=?", (time.time(), p["id"]))
            await agent._db.commit()


_poller: asyncio.Task | None = None


def start_poller() -> None:
    global _poller

    async def loop():
        while True:
            try:
                await _tick()
            except Exception:  # noqa: BLE001 - the poller must outlive one bad row
                logger.exception("bg poller tick failed")
            await asyncio.sleep(POLL_S)

    if _poller is None or _poller.done():
        _poller = asyncio.get_running_loop().create_task(loop())


# ---- agent tools ----

def _thread() -> str | None:
    try:
        return (get_config().get("configurable") or {}).get("thread_id")
    except RuntimeError:
        return None


def _fmt(p: dict, lines: int = 0) -> str:
    st = p["state"] + (f" (exit {p['exit']})" if p["exit"] is not None else "")
    s = (f"{p['id']} “{p['name']}” on {p.get('host') or HOST} — {st}, {_fmt_dur(p['secs'])}"
         f"{'' if p['wake'] == 'none' else ', wakes this thread on ' + p['wake']}"
         f"{' (woken)' if p.get('woke') and p['state'] == 'running' else ''}")
    if lines:
        s += f"\nlast {lines} lines of output:\n{tail(p['id'], lines)}"
    return s


@tool
async def bg_start(command: str, name: str, wake: str = "exit", wake_match: str = "",
                   max_hours: float = 24, host: str = "") -> str:
    """Start a long-running shell command in the background, tracked by LangBang
    and shown to the user. Use it for anything that runs or waits longer than a
    couple of minutes (training, evals, uploads, builds, remote jobs, watchers)
    instead of sleep-polling with run_bash.

    It survives LangBang restarts, runs with cwd=home and the user's login env,
    and its output goes to a log (bg_status reads it). Then END YOUR TURN —
    you are woken in this thread with the outcome and the log tail:
    - wake="exit" (default): when the command exits.
    - wake="match": the first time an output line matches the regex wake_match
      (the process keeps running), e.g. to watch a remote log for "DONE|Error".
      A watcher can also simply be a loop that exits once its condition holds.
    - wake="none": nobody is woken (servers, tunnels).
    max_hours: hard runtime cap (default 24, max 168), then it is killed.
    name: short label the user sees, e.g. "t26 stats eval".

    host: WHERE it runs. Default "" = the LangBang machine itself — NOT the
    box your run_bash commands ssh into. If the job or the thing you check
    (a container, a port, a log file) lives on another machine, pass its ssh
    name, e.g. host="spark-ee93", and write the command exactly as you would
    in a shell ON that machine: LangBang adds the ssh and all quoting. Do not
    wrap it in ssh '...' yourself. Exit 255 with host= means ssh failed.

    Before starting a watcher loop, run its check body ONCE with run_bash
    (on the same host) and confirm it prints what you expect. Never use
    `pkill -f PATTERN` inside a watcher: the pattern also matches the
    watcher's own command line, so it kills itself. A command that prints
    shell errors (command not found, No such container, …) in its first
    minutes wakes you right away so you can fix it."""
    try:
        p = await start(_thread(), command, name, wake, wake_match, max_hours, host)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    return ("started " + _fmt(p) + ". End your turn now unless there is other work; "
            "you'll be woken with the result" if p["wake"] != "none" else "started " + _fmt(p))


@tool
async def bg_status(id: str = "", lines: int = 30) -> str:
    """Status and recent output of a background process started with bg_start;
    with no id, lists this thread's processes."""
    if id:
        p = await get(id)
        return _fmt(p, max(1, min(int(lines), 400))) if p else f"no process {id}"
    ps = await list_procs(_thread(), 20)
    return "\n".join(_fmt(p) for p in ps) or "no background processes in this thread"


@tool
async def bg_stop(id: str) -> str:
    """Stop a background process (kills its whole process tree)."""
    p = await stop(id)
    return _fmt(p, 10) if p else f"no process {id}"


TOOLS = [bg_start, bg_status, bg_stop]
NOTE = (
    "\n\nBackground work: for anything that runs or must be waited on for more "
    "than ~2 minutes (training, evals, uploads, remote jobs, 'tell me when X'), "
    "do NOT sleep-poll in run_bash (`sleep 290; check`) and do not `nohup … &`. "
    "Start it with bg_start (or a small watcher loop that exits when the "
    "condition holds), tell the user what you started, and end your turn — "
    "remember bg_start runs on THIS machine unless you pass host=. "
    "LangBang wakes this thread with the result. The user sees these in the "
    "⚙ PROCESSES bar."
)

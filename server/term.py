"""▮ TERM: browser terminals on this box + SSH into the fleet.

Every session is a tmux session on a private server (`tmux -L langbang`, own
config in data/term/tmux.conf — the user's ~/.tmux.conf is never read). A
browser tab is just one more tmux client: each WebSocket gets its own PTY
running `tmux attach`, so closing the tab, a second device, or a LangBang
restart (the unit's KillMode=process leaves the tmux server alive) never
kills the shell — only ✕ or `exit` does.

Same trust posture as /api/shell (README ⚠ Security): an unsandboxed shell
for whoever reaches the app. The WebSocket route checks Origin, because a
browser lets ANY site open a WebSocket to the LAN — unlike a JSON POST, which
CORS preflight blocks.

Fleet hosts live in settings `fleet.hosts` [{name, ssh, note}] — `ssh` is an
~/.ssh/config alias or user@host[:port]; probes and run-on-many use
BatchMode (key auth only, never a password prompt).
"""
import asyncio
import fcntl
import os
import re
import secrets
import shlex
import shutil
import struct
import subprocess
import termios
import time

from . import config

SOCK = "langbang"
CONF = os.path.join(config.DATA_DIR, "term", "tmux.conf")
TMUX_CONF = """\
# LangBang ▮ TERM — written by server/term.py on every start (edits are lost)
set -g status off
set -g mouse on
set -g history-limit 50000
set -g escape-time 0
set -g default-terminal "tmux-256color"
set -ga terminal-overrides ",xterm-256color:Tc"
set -g window-size latest
set -g aggressive-resize on
set -g set-titles on
set -g set-titles-string "#{pane_current_command} · #{pane_current_path}"
set -g allow-passthrough on
set -g set-clipboard on
"""
SID_RE = re.compile(r"^lb-[0-9a-f]{8}$")
FMT = "#{session_name}\t#{@lb_host}\t#{@lb_title}\t#{session_created}\t#{session_attached}\t#{pane_current_command}\t#{pane_current_path}"


def available() -> bool:
    return shutil.which("tmux") is not None


def _tmux(*args, check=True, timeout=5) -> subprocess.CompletedProcess:
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    if not os.path.exists(CONF) or open(CONF).read() != TMUX_CONF:
        with open(CONF, "w") as f:
            f.write(TMUX_CONF)
    return subprocess.run(["tmux", "-L", SOCK, "-f", CONF, *args], capture_output=True,
                          text=True, check=check, timeout=timeout)


# ---- fleet hosts ------------------------------------------------------------

def hosts(s: dict | None = None) -> list[dict]:
    s = s or config.load()
    return [h for h in ((s.get("fleet") or {}).get("hosts") or [])
            if isinstance(h, dict) and h.get("name") and h.get("ssh")]


def host(name: str) -> dict | None:
    return next((h for h in hosts() if h["name"] == name), None)


def ssh_argv(h: dict, *extra: str) -> list[str]:
    """`ssh` target: an alias from ~/.ssh/config, or user@host[:port]."""
    target, port = h["ssh"].strip(), None
    m = re.match(r"^([^:\s]+):(\d+)$", target)  # aliases never hold a ':'
    if m:
        target, port = m.group(1), m.group(2)
    argv = ["ssh", "-o", "ServerAliveInterval=30", *extra]
    if port:
        argv += ["-p", port]
    return argv + ["--", target]


GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "ssh.github.com", "codeberg.org"}


def ssh_config_hosts(path: str = "~/.ssh/config") -> list[dict]:
    """Concrete `Host` aliases in ~/.ssh/config (no wildcards), deduped by
    HostName so `spark` + `spark-ee93` pointing at one box list once."""
    out, seen = [], {}
    try:
        lines = open(os.path.expanduser(path)).read().splitlines()
    except OSError:
        return []
    cur = None
    for ln in lines:
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        k, _, v = ln.partition(" ")
        k, v = k.lower(), v.strip().strip('"')
        if k == "host":
            cur = []
            for a in v.split():
                if not any(c in a for c in "*?!"):
                    e = {"name": a, "ssh": a, "hostname": a, "user": ""}
                    out.append(e)
                    cur.append(e)
        elif cur and k == "hostname":
            for e in cur:
                e["hostname"] = v
        elif cur and k == "user":
            for e in cur:
                e["user"] = v
    res = []
    for e in out:
        if e["hostname"].lower() in GIT_HOSTS or e["name"].lower() in GIT_HOSTS:
            continue  # git remotes, not shells
        key = (e["hostname"].split(".")[0].lower(), e["user"])
        if key in seen:
            continue
        seen[key] = e
        ip = re.match(r"^[\d.]+$|:", e["name"])
        res.append({"name": e["name"] if ip else e["name"].split(".")[0], "ssh": e["name"],
                    "note": e["hostname"] if e["hostname"] != e["name"] else ""})
    return res


PROBE = r"""h=$(hostname -s 2>/dev/null || hostname); u=$(cut -d' ' -f1 /proc/uptime 2>/dev/null)
l=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null); n=$(nproc 2>/dev/null)
m=$(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{if(t)printf "%d %d",(t-a)/1024,t/1024}' /proc/meminfo 2>/dev/null)
d=$(df -P / 2>/dev/null | awk 'NR==2{print $5}')
g=$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)
if [ -z "$u" ]; then  # macOS / BSD
  b=$(sysctl -n kern.boottime 2>/dev/null | awk -F'[ ,]+' '{print $4}'); [ -n "$b" ] && u=$(( $(date +%s) - b ))
  l=$(sysctl -n vm.loadavg 2>/dev/null | tr -d '{}' | awk '{print $1,$2,$3}'); n=$(sysctl -n hw.ncpu 2>/dev/null)
  t=$(sysctl -n hw.memsize 2>/dev/null)
  [ -n "$t" ] && m=$(vm_stat | awk -v t=$t '/page size of/{p=$8}/Pages (free|speculative|inactive)/{gsub(/\./,"",$NF);f+=$NF}END{printf "%d %d",(t-f*p)/1048576,t/1048576}')
fi
echo "LBP|$h|$u|$l|$n|$m|$d|$g|$(uname -sm)"
"""


async def _run(argv: list[str], timeout: float, stdin: bytes | None = None) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(p.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(p.pid, 9)
        except ProcessLookupError:
            pass
        await p.wait()
        return 124, "", f"timed out after {timeout:g} s"
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


_BATCH = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new")


async def probe(h: dict) -> dict:
    t0 = time.monotonic()
    code, out, err = await _run(ssh_argv(h, *_BATCH, "-T") + ["sh", "-s"], 15, PROBE.encode())
    ms = round((time.monotonic() - t0) * 1000)
    line = next((x for x in out.splitlines() if x.startswith("LBP|")), None)
    if code != 0 or not line:
        msg = (err.strip().splitlines() or [f"exit {code}"])[-1]
        return {"name": h["name"], "ok": False, "ms": ms, "error": msg[:300]}
    _, hn, up, load, ncpu, mem, disk, gpu, uname = (line.split("|") + [""] * 9)[:9]
    r = {"name": h["name"], "ok": True, "ms": ms, "hostname": hn, "os": uname,
         "load": load, "cpus": int(ncpu) if ncpu.isdigit() else None, "disk": disk}
    try:
        r["uptime_s"] = int(float(up))
    except ValueError:
        pass
    if mem.strip():
        u, t = mem.split()
        r["mem_mb"] = [int(u), int(t)]
    if gpu.strip():
        g = [x.strip() for x in gpu.split(",")]
        r["gpu"] = {"util": g[0], "mem_used": g[1] if len(g) > 1 else "",
                    "mem_total": g[2] if len(g) > 2 else ""}
    return r


async def probe_all(names: list[str] | None = None) -> list[dict]:
    hs = [h for h in hosts() if not names or h["name"] in names]
    return list(await asyncio.gather(*(probe(h) for h in hs)))


async def run_many(names: list[str], command: str, timeout: float = 60) -> list[dict]:
    """Non-interactive `command` on each host at once (no TTY, key auth)."""
    hs = [h for h in hosts() if h["name"] in names]
    timeout = max(1.0, min(float(timeout), 600.0))

    async def one(h):
        t0 = time.monotonic()
        code, out, err = await _run(ssh_argv(h, *_BATCH, "-T") + [command], timeout)
        return {"name": h["name"], "code": code, "out": out[-200_000:], "err": err[-20_000:],
                "ms": round((time.monotonic() - t0) * 1000)}
    return list(await asyncio.gather(*(one(h) for h in hs)))


# ---- sessions ---------------------------------------------------------------

def sessions() -> list[dict]:
    if not available():
        return []
    r = _tmux("list-sessions", "-F", FMT, check=False)
    out = []
    for ln in r.stdout.splitlines():
        p = (ln.split("\t") + [""] * 7)[:7]
        if not SID_RE.match(p[0]):
            continue
        out.append({"id": p[0], "host": p[1] or "local", "title": p[2] or p[1] or "local",
                    "created": int(p[3] or 0), "attached": int(p[4] or 0),
                    "command": p[5], "cwd": p[6]})
    return sorted(out, key=lambda x: x["created"])


def create(host_name: str = "local", cols: int = 120, rows: int = 32, cwd: str = "") -> dict:
    if not available():
        raise RuntimeError("tmux is not installed on the server (dnf install tmux)")
    sid = "lb-" + secrets.token_hex(4)
    cols, rows = max(20, min(int(cols), 500)), max(5, min(int(rows), 200))
    if host_name in ("", "local"):
        cmd, title = [], "local"
        start = os.path.expanduser(cwd or "~")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
    else:
        h = host(host_name)
        if not h:
            raise KeyError(host_name)
        # keep the pane after ssh ends so the reason (auth, refused…) stays visible
        ssh = shlex.join(ssh_argv(h))
        cmd = [f"{ssh}; c=$?; printf '\\n\\033[33m[ssh exited %s — Enter closes this tab]\\033[0m' $c; read -r _"]
        title, start = h["name"], os.path.expanduser("~")
    _tmux("new-session", "-d", "-s", sid, "-x", str(cols), "-y", str(rows), "-c", start, *cmd)
    _tmux("set-option", "-t", sid, "@lb_host", host_name or "local")
    _tmux("set-option", "-t", sid, "@lb_title", title)
    return next((x for x in sessions() if x["id"] == sid), {"id": sid, "host": host_name, "title": title})


def rename(sid: str, title: str) -> bool:
    if not SID_RE.match(sid) or not exists(sid):
        return False
    _tmux("set-option", "-t", sid, "@lb_title", title.strip()[:60] or "term")
    return True


def kill(sid: str) -> bool:
    if not SID_RE.match(sid):
        return False
    return _tmux("kill-session", "-t", "=" + sid, check=False).returncode == 0


def exists(sid: str) -> bool:
    return bool(SID_RE.match(sid)) and _tmux("has-session", "-t", "=" + sid, check=False).returncode == 0


class Client:
    """One attached tmux client on its own PTY (= one browser tab)."""

    def __init__(self, sid: str, cols: int, rows: int):
        self.master, slave = os.openpty()
        self.resize(cols, rows)
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
        env.pop("TMUX", None)
        # setsid -c: own session + this PTY as the controlling tty (job control,
        # ^C, window-size signals) without a preexec_fn in a threaded server
        self.proc = subprocess.Popen(
            ["setsid", "-c", "tmux", "-L", SOCK, "-f", CONF, "attach-session", "-t", "=" + sid],
            stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
        os.close(slave)
        os.set_blocking(self.master, False)

    def resize(self, cols: int, rows: int):
        cols, rows = max(2, min(int(cols), 1000)), max(2, min(int(rows), 500))
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def write(self, data: bytes):
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.master, view)
            except BlockingIOError:
                time.sleep(0.005)
                continue
            view = view[n:]

    def close(self):
        try:
            self.proc.terminate()  # detaches this client only; the session lives on
        except ProcessLookupError:
            pass
        try:
            os.close(self.master)
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()

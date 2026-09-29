"""Built-in local tools: shell + filesystem + LangBang's own scheduler.
Runs on THIS machine — the box serving LangBang. Deliberately unrestricted
(personal LAN tool, bypass-perms philosophy); see README before exposing this
server beyond localhost/LAN."""
import concurrent.futures
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from langchain_core.tools import tool

from . import schedule  # cycle-safe: only used at call time

HOME = Path.home()
MAX_OUT = 12_000  # chars — keep tool output off Spark's prefill budget
CRAWL_API = os.environ.get("LANGBANG_CRAWL4AI_URL", "http://spark-ee93:8088")
# hard wall-clock cap for one crawl; dead sources should cost seconds, not
# minutes (180s socket timeouts let wedged upstreams stall whole runs)
CRAWL_TIMEOUT = int(os.environ.get("LANGBANG_CRAWL_TIMEOUT", "45"))


def _trim(s: str) -> str:
    return s if len(s) <= MAX_OUT else s[:MAX_OUT] + "\n…[truncated]"


def _resolve(path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else (HOME / p)


@tool
def run_bash(command: str, timeout: int = 60) -> str:
    """Execute a bash command in the user's home directory. Returns exit code,
    stdout and stderr. Non-interactive commands only; hard cap 300s."""
    try:
        r = subprocess.run(
            ["bash", "-lc", command],
            cwd=HOME,
            capture_output=True,
            text=True,
            timeout=min(int(timeout), 300),
        )
        return _trim(
            f"exit={r.returncode}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
        )
    except subprocess.TimeoutExpired:
        return f"TIMEOUT after {timeout}s: {command}"
    except Exception as e:  # noqa: BLE001 - report to model, not crash
        return f"ERROR: {type(e).__name__}: {e}"


@tool
def read_file(path: str, max_lines: int = 500) -> str:
    """Read a text file as UTF-8. Relative paths resolve against the home directory."""
    try:
        lines = _resolve(path).read_text(errors="replace").splitlines()
        out = "\n".join(lines[: int(max_lines)])
        if len(lines) > int(max_lines):
            out += f"\n…[{len(lines) - int(max_lines)} more lines]"
        return _trim(out)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {e}"


@tool
def write_file(path: str, content: str) -> str:
    """Create or overwrite a text file (parent directories are created).
    Relative paths resolve against the home directory."""
    try:
        p = _resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content.encode())} bytes to {p}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {e}"


@tool
def list_dir(path: str = ".") -> str:
    """List a directory, one entry per line, dirs suffixed with '/'.
    Relative paths resolve against the home directory."""
    try:
        p = _resolve(path)
        return _trim(
            "\n".join(
                sorted(f"{e.name}{'/' if e.is_dir() else ''}" for e in p.iterdir())
            )
        )
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {e}"


def _crawl_once(url: str) -> dict:
    req = urllib.request.Request(
        CRAWL_API.rstrip("/") + "/api/crawl",
        data=json.dumps({"url": url, "fit_markdown": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # urllib's timeout is PER SOCKET OP — a wedged upstream that trickles
    # bytes can still hang far past it, so crawl_url caps wall clock too.
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


@tool
def crawl_url(url: str) -> str:
    """Crawl a public web page and return its readable Markdown, using the
    Crawl4AI workbench running on spark-ee93. Good for docs/articles; blocks
    private-network targets. Slow/dead sources fail within ~45s — crawl
    independent URLs as parallel calls in ONE message, not turn by turn."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        d = ex.submit(_crawl_once, url).result(timeout=CRAWL_TIMEOUT)
    except concurrent.futures.TimeoutError:
        return (
            f"ERROR: crawl timed out after {CRAWL_TIMEOUT}s — {url} is slow or "
            "dead; move on or try another source (run_bash curl also works)"
        )
    except Exception as e:  # noqa: BLE001 - report to model, not crash
        return f"ERROR: {type(e).__name__}: {e} (workbench at {CRAWL_API})"
    finally:
        ex.shutdown(wait=False)  # orphan thread dies at its own socket timeout
    if not d.get("success"):
        return f"CRAWL FAILED for {url}: {d.get('error', d)}"
    return _trim(
        f"# {d.get('title') or url}\n"
        f"[{d.get('word_count', '?')} words, {d.get('elapsed_seconds', '?')}s, "
        f"saved: {d.get('output_file', '-')}, "
        f"links: {d.get('internal_links', '?')} in / {d.get('external_links', '?')} out]\n\n"
        + (d.get("markdown") or "")
    )


def _fmt(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


# ---- LangBang's own scheduler (server/schedule.py) -------------------------
# These exist so "schedule X every N hours" lands on the app's scheduler.
# Without them the model improvised with run_bash + OS schedulers (it once
# registered a Windows schtask over ssh for exactly this request).
# Async is fine: the graph runs async, same path as the async MCP tools.

@tool
async def create_scheduled_task(title: str, prompt: str, cron: str) -> str:
    """Create a LangBang scheduled task: `prompt` runs as a headless agent
    turn on a 5-field cron cadence (minute hour dom month dow —
    '0 */4 * * *' every 4 hours, '30 8 * * 1-5' weekdays 08:30). Each task
    owns a dedicated chat thread where every run's output lands and runs
    share that thread's context, so `prompt` must be self-contained (future
    runs see no live conversation). ALWAYS use this tool — never crontab,
    systemd timers or Windows schtasks — for scheduling the user asks for."""
    try:
        row = await schedule.create(title, prompt, cron)
    except ValueError:
        return (f"ERROR: invalid cron {cron!r} — need 5 fields "
                "'minute hour dom month dow', e.g. '0 */4 * * *' (every 4h)")
    return (f"created {row['id']}: {title!r} — {schedule.humanize(cron)}; "
            f"next run {_fmt(row['next_run'])}")


@tool
async def list_scheduled_tasks() -> str:
    """List LangBang scheduled tasks: id | title | cron (human) |
    on/off | next run | last run."""
    rows = await schedule.list_schedules()
    if not rows:
        return "no scheduled tasks"
    return _trim("\n".join(
        f"{r['id']} | {r['title']} | {r['cron']} ({schedule.humanize(r['cron'])}) | "
        f"{'ON ' if r['enabled'] else 'OFF'} next {_fmt(r['next_run'])} last {_fmt(r['last_run'])}"
        for r in rows))


@tool
async def update_scheduled_task(id: str, title: str = "", prompt: str = "",
                                cron: str = "") -> str:
    """Update a LangBang scheduled task; pass "" for fields to keep. A cron
    change re-syncs the timer to the new cadence."""
    patch = {k: v for k, v in (("title", title), ("prompt", prompt), ("cron", cron)) if v}
    try:
        row = await (schedule.update(id, patch) if patch else schedule.get(id))
    except ValueError:
        return f"ERROR: invalid cron {cron!r}"
    if not row:
        return f"ERROR: no scheduled task {id!r} (see list_scheduled_tasks)"
    return (f"updated {id}: {row['title']!r} — {schedule.humanize(row['cron'])}; "
            f"next {_fmt(row['next_run'])}")


@tool
async def set_scheduled_task_enabled(id: str, enabled: bool) -> str:
    """Pause (enabled=false) or resume a LangBang scheduled task. Resuming
    restarts the cadence from now."""
    row = await schedule.update(id, {"enabled": enabled})
    return (f"{'resumed' if enabled else 'paused'} {id}: {row['title']!r}"
            if row else f"ERROR: no scheduled task {id!r}")


@tool
async def run_scheduled_task_now(id: str) -> str:
    """Fire one run of a LangBang scheduled task immediately, into its own
    thread; does not shift its cron rhythm."""
    return ("run started for " + id) if await schedule.run_now(id) \
        else f"ERROR: no scheduled task {id!r}"


@tool
async def delete_scheduled_task(id: str) -> str:
    """Delete a LangBang scheduled task AND its run-history thread."""
    return (f"deleted {id}" if await schedule.delete(id)
            else f"ERROR: no scheduled task {id!r}")


LOCAL_TOOLS = [run_bash, read_file, write_file, list_dir, crawl_url,
               create_scheduled_task, list_scheduled_tasks, update_scheduled_task,
               set_scheduled_task_enabled, run_scheduled_task_now, delete_scheduled_task]

TOOLS_NOTE = (
    "\n\nLocal tools available: run_bash (shell, cwd=home), read_file, "
    "write_file, list_dir, crawl_url (fetch any web page as Markdown via "
    "Crawl4AI on spark-ee93). Prefer them over asking the user to run things. "
    "Scheduling: any recurring/scheduled work the user asks for goes to "
    "create_scheduled_task (5-field cron) — LangBang's own scheduler; do NOT "
    "use run_bash with crontab, systemd timers, at, or Windows schtasks for "
    "it. list/update/set_enabled/run_now/delete siblings manage tasks. "
    "Parallelize: independent tool calls (several URLs, files, commands) go "
    "in ONE message as multiple calls — they run concurrently; never spend a "
    "separate turn on a call that didn't depend on the last one's result. "
    "Showing media: the chat UI renders an inline image/audio/video player "
    "for every absolute file path (.png/.jpg/.gif/.webp/.mp4/.webm/.mov/"
    ".mp3/.wav/.ogg/.m4a) written in your reply text — to show the user a "
    "file, just cite its full path in the answer (plain or `inline code`, "
    "not inside a fenced block). read_file on media only lets YOU look at it "
    "(images when vision is on); it never displays anything to the user."
)

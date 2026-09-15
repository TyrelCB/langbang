"""Built-in local tools: shell + filesystem. Runs on THIS machine — the box
serving LangBang. Deliberately unrestricted (personal LAN tool, bypass-perms
philosophy); see README before exposing this server beyond localhost/LAN."""
import json
import os
import subprocess
import urllib.request
from pathlib import Path

from langchain_core.tools import tool

HOME = Path.home()
MAX_OUT = 12_000  # chars — keep tool output off Spark's prefill budget
CRAWL_API = os.environ.get("LANGBANG_CRAWL4AI_URL", "http://spark-ee93:8088")


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


@tool
def crawl_url(url: str) -> str:
    """Crawl a public web page and return its readable Markdown, using the
    Crawl4AI workbench running on spark-ee93. Good for docs/articles; blocks
    private-network targets. May take ~10-60s."""
    try:
        req = urllib.request.Request(
            CRAWL_API.rstrip("/") + "/api/crawl",
            data=json.dumps({"url": url, "fit_markdown": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.load(r)
        if not d.get("success"):
            return f"CRAWL FAILED for {url}: {d.get('error', d)}"
        return _trim(
            f"# {d.get('title') or url}\n"
            f"[{d.get('word_count', '?')} words, {d.get('elapsed_seconds', '?')}s, "
            f"saved: {d.get('output_file', '-')}, "
            f"links: {d.get('internal_links', '?')} in / {d.get('external_links', '?')} out]\n\n"
            + (d.get("markdown") or "")
        )
    except Exception as e:  # noqa: BLE001 - report to model, not crash
        return f"ERROR: {type(e).__name__}: {e} (workbench at {CRAWL_API})"


LOCAL_TOOLS = [run_bash, read_file, write_file, list_dir, crawl_url]

TOOLS_NOTE = (
    "\n\nLocal tools available: run_bash (shell, cwd=home), read_file, "
    "write_file, list_dir, crawl_url (fetch any web page as Markdown via "
    "Crawl4AI on spark-ee93). Prefer them over asking the user to run things."
)

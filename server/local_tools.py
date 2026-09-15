"""Built-in local tools: shell + filesystem. Runs on THIS machine — the box
serving LangBang. Deliberately unrestricted (personal LAN tool, bypass-perms
philosophy); see README before exposing this server beyond localhost/LAN."""
import subprocess
from pathlib import Path

from langchain_core.tools import tool

HOME = Path.home()
MAX_OUT = 12_000  # chars — keep tool output off Spark's prefill budget


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


LOCAL_TOOLS = [run_bash, read_file, write_file, list_dir]

TOOLS_NOTE = (
    "\n\nLocal tools available: run_bash (shell, cwd=home), read_file, "
    "write_file, list_dir. Prefer them over asking the user to run things."
)

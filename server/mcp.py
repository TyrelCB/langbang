"""MCP server connections -> LangChain tools, cached with manual reload.

Resilience (2026-10-03, after an agent stopped the Gmail MCP it was about to
call): a dead or slow server must not take the agent down with it.
- Tools load PER SERVER with a timeout; a server that fails is skipped (and
  logged) instead of failing every run's tool load. While any server is
  failing the cache isn't kept, so it's picked up again once it's back.
- Every MCP tool call has a wall-clock cap (`call_timeout` per server,
  default CALL_TIMEOUT s): a server that dies MID-call left the client
  waiting forever (measured: >10 min, never returned). The timeout raises,
  and agent.py's tool guard turns it into an error result the model reads.
"""
import asyncio
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger("langbang.mcp")

LOAD_TIMEOUT = 15.0     # connect + list tools, per server
CALL_TIMEOUT = 300.0    # one tool call; override per server: "call_timeout": 1800
LANGBANG_KEYS = ("disabled", "call_timeout", "oauth_file")  # ours — never passed to the client


class FileBearerAuth(httpx.Auth):
    """Bearer auth from an OAuth token JSON file, refreshed on expiry (401-safe).

    Config: "oauth_file": "~/.path/token.json" — the file must carry
    access_token (+ optionally refresh_token, expires_at, client_id,
    token_endpoint, resource). The refreshed token is written back.
    """

    _locks: dict[str, threading.Lock] = {}

    def __init__(self, path: str):
        self.path = Path(path).expanduser()
        self._lock = self._locks.setdefault(str(self.path), threading.Lock())

    def _refresh(self, d: dict) -> dict:
        data = {"grant_type": "refresh_token", "refresh_token": d["refresh_token"],
                "client_id": d.get("client_id", "")}
        if d.get("scope"):
            data["scope"] = d["scope"]
        if d.get("resource"):
            data["resource"] = d["resource"]
        req = urllib.request.Request(
            d["token_endpoint"], data=urllib.parse.urlencode(data).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "Mozilla/5.0"})  # CF blocks the urllib UA
        with urllib.request.urlopen(req, timeout=30) as r:
            new = json.load(r)
        d = {**d, **new,
             "expires_at": time.time() + int(new.get("expires_in", 3600)) - 60}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        tmp.chmod(0o600)
        tmp.replace(self.path)
        logger.info("MCP oauth: refreshed token for %s", self.path.name)
        return d

    def token(self) -> str:
        with self._lock:
            d = json.loads(self.path.read_text())
            if time.time() >= float(d.get("expires_at", 0)) and d.get("refresh_token"):
                d = self._refresh(d)
            return d["access_token"]

    async def async_auth_flow(self, request):
        token = await asyncio.to_thread(self.token)
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            with self._lock:  # force one refresh, then retry once
                d = json.loads(self.path.read_text())
                d = self._refresh(d) if d.get("refresh_token") else d
                token = d["access_token"]
            request.headers["Authorization"] = f"Bearer {token}"
            yield request

_client: MultiServerMCPClient | None = None
_tools: list = []
_tools_lock = asyncio.Lock()
_fingerprint = None
last_errors: dict[str, str] = {}  # server -> why its tools are missing right now
by_server: dict[str, list[str]] = {}  # server -> tool names from the last load (NEW CHAT overview)
# tool name -> the server's MCP readOnlyHint (True/False), when it declared one
readonly_hint: dict[str, bool] = {}


def _fingerprint_of(servers: dict):
    return sorted((name, sorted(cfg.items())) for name, cfg in servers.items())


def _active(servers: dict) -> dict:
    """Drop per-server entries flagged {"disabled": true}."""
    return {name: dict(cfg) for name, cfg in (servers or {}).items() if not cfg.get("disabled")}


def _client_cfg(cfg: dict) -> dict:
    out = {k: v for k, v in cfg.items() if k not in LANGBANG_KEYS}
    if cfg.get("oauth_file"):
        out["auth"] = FileBearerAuth(cfg["oauth_file"])
    return out


def _root(e: BaseException) -> str:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def _with_timeout(tool, server: str, secs: float):
    orig = tool.coroutine
    if orig is None:
        return tool

    async def run(*a, **k):
        try:
            return await asyncio.wait_for(orig(*a, **k), secs)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"MCP server '{server}' didn't answer within {secs:.0f}s "
                "(server down/restarted mid-call, or a long job — raise call_timeout)") from None

    tool.coroutine = run
    return tool


async def get_tools(servers: dict) -> list:
    """LangChain tools for the configured MCP servers (cached while healthy)."""
    global _client, _tools, _fingerprint
    servers = _active(servers)
    async with _tools_lock:
        fp = _fingerprint_of(servers)
        if _client is not None and fp == _fingerprint:
            return _tools
        tools: list = []
        last_errors.clear()
        by_server.clear()
        readonly_hint.clear()
        client = MultiServerMCPClient({n: _client_cfg(c) for n, c in servers.items()}) if servers else None
        for name, cfg in servers.items():
            try:
                got = await asyncio.wait_for(client.get_tools(server_name=name), LOAD_TIMEOUT)
            except Exception as e:  # noqa: BLE001 - one server must not sink the rest
                last_errors[name] = (f"timed out after {LOAD_TIMEOUT:.0f}s"
                                     if isinstance(e, asyncio.TimeoutError) else _root(e))
                logger.warning("MCP server %r skipped this run: %s", name, last_errors[name])
                continue
            by_server[name] = [t.name for t in got]
            for t in got:
                h = (t.metadata or {}).get("readOnlyHint")
                if isinstance(h, bool):
                    readonly_hint[t.name] = h
            secs = float(cfg.get("call_timeout") or CALL_TIMEOUT)
            tools += [_with_timeout(t, name, secs) for t in got]
        _tools = tools
        # only a fully healthy set is cached; otherwise retry next run
        _client = client if not last_errors else None
        _fingerprint = fp if not last_errors else None
        return _tools


async def probe_server(cfg: dict, timeout: float = 15.0) -> dict:
    """One-off connect + tool listing against a single server config, for the
    CONFIG UI's Test connection. Throwaway client — never touches the cache,
    so testing a broken server can't poison live chat tool loads."""
    client = MultiServerMCPClient({"__probe__": _client_cfg(cfg or {})})
    try:
        tools = await asyncio.wait_for(
            client.get_tools(server_name="__probe__"), timeout)
        return {"ok": True, "tools": [t.name for t in tools]}
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"connection timed out ({timeout:.0f}s)"}
    except Exception as e:  # noqa: BLE001 - the error text IS the feature
        # anyio TaskGroups wrap the real failure one level down — "unhandled
        # errors in a TaskGroup" alone is useless in the UI, unwrap it.
        return {"ok": False, "error": _root(e)}

"""MCP server connections -> LangChain tools, cached with manual reload."""
import asyncio

from langchain_mcp_adapters.client import MultiServerMCPClient

_client: MultiServerMCPClient | None = None
_tools: list = []
_tools_lock = asyncio.Lock()
_fingerprint = None


def _fingerprint_of(servers: dict):
    return sorted((name, sorted(cfg.items())) for name, cfg in servers.items())


def _active(servers: dict) -> dict:
    """Drop per-server entries flagged {"disabled": true}; strip the flag."""
    return {
        name: {k: v for k, v in cfg.items() if k != "disabled"}
        for name, cfg in (servers or {}).items()
        if not cfg.get("disabled")
    }


async def get_tools(servers: dict) -> list:
    """Return LangChain tools for the configured MCP servers (cached)."""
    global _client, _tools, _fingerprint
    servers = _active(servers)
    async with _tools_lock:
        fp = _fingerprint_of(servers)
        if _client is not None and fp == _fingerprint:
            return _tools
        _tools, _client = [], None
        if servers:
            client = MultiServerMCPClient(servers)
            try:
                _tools = await client.get_tools()
                _client = client
            except Exception as e:  # noqa: BLE001 - surface config errors to the caller
                raise RuntimeError(f"MCP tool load failed: {e}") from e
        _fingerprint = fp
        return _tools

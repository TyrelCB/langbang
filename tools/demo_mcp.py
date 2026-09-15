"""Tiny demo MCP server (stdio) for LangBang — time + calculator tools."""
import datetime
import math

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool()
def now_utc() -> str:
    """Current UTC time as ISO-8601."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@mcp.tool()
def calculate(expression: str) -> str:
    """Evaluate a math expression (numbers, + - * / ** % and math.* functions)."""
    allowed = {n: getattr(math, n) for n in dir(math) if not n.startswith("_")}
    return str(eval(expression, {"__builtins__": {}}, allowed))  # noqa: S307


if __name__ == "__main__":
    mcp.run()

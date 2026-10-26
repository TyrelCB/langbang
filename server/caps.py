"""Model capability detection — vision, per (base_url, model).

Providers used to carry a hand-set "vision" checkbox, which went stale per
MODEL: one llama-server router serves Nemotron (mmproj) and text-only GLM
behind the same base_url. A provider's vision is now "auto" (default) or a
forced "on" / "off"; auto asks the backend, cheapest signal first:

1. /v1/models with per-model launch args (llama-server router): --mmproj in
   the args. Read from the listing, so an unloaded model is never loaded.
2. /props (single llama-server): modalities.vision.
3. /api/show (Ollama): "vision" in capabilities.
4. otherwise (vLLM, sglang, hosted APIs): one 1x1-PNG chat request with
   max_tokens 1 — 200 = vision, a 400 = not.

Results are cached per (base_url, model); an unknown (backend down) is
retried after a minute and treated as no vision meanwhile.
"""

import asyncio
import logging
import time

import httpx

logger = logging.getLogger("langbang.caps")

TTL = 6 * 3600     # a model's modalities don't change while it's served
RETRY = 60         # backend unreachable → ask again after this
TIMEOUT = 8.0
# 1x1 transparent PNG
PIXEL = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
         "nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg==")

_cache: dict[tuple[str, str], tuple[float, bool | None, str]] = {}
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _root(base_url: str) -> str:
    u = (base_url or "").rstrip("/")
    return u[:-3] if u.endswith("/v1") else u


def mode(p: dict) -> str:
    """A provider's vision setting: auto | on | off. Pre-2026-10-26 settings
    stored a hand-set bool — those become auto (they were guesses)."""
    v = (p or {}).get("vision")
    return v if v in ("on", "off") else "auto"


def cached(base_url: str, model: str) -> tuple[bool | None, str]:
    hit = _cache.get((base_url, model))
    return (hit[1], hit[2]) if hit else (None, "")


async def _detect(c: httpx.AsyncClient, base_url: str, model: str, key: str) -> tuple[bool | None, str]:
    root, hdr = _root(base_url), {"Authorization": f"Bearer {key or 'none'}"}
    try:
        r = await c.get(base_url.rstrip("/") + "/models", headers=hdr)
        if r.status_code == 200:
            for m in (r.json().get("data") or []):
                args = (m.get("status") or {}).get("args")
                if m.get("id") == model and isinstance(args, list):
                    return "--mmproj" in args, "router args"
    except (httpx.HTTPError, ValueError):
        return None, "unreachable"
    try:
        r = await c.get(root + "/props", headers=hdr)
        mods = r.json().get("modalities") if r.status_code == 200 else None
        if isinstance(mods, dict):
            return bool(mods.get("vision")), "llama-server /props"
    except (httpx.HTTPError, ValueError):
        pass
    try:
        r = await c.post(root + "/api/show", json={"model": model}, headers=hdr)
        cap = r.json().get("capabilities") if r.status_code == 200 else None
        if isinstance(cap, list):
            return "vision" in cap, "ollama /api/show"
    except (httpx.HTTPError, ValueError):
        pass
    body = {"model": model, "max_tokens": 1, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": PIXEL}},
                {"type": "text", "text": "ok"}]}]}
    try:
        r = await c.post(base_url.rstrip("/") + "/chat/completions", json=body, headers=hdr,
                         timeout=60)
        if r.status_code == 400 and "chat_template_kwargs" in r.text:  # strict API: retry bare
            body.pop("chat_template_kwargs")
            r = await c.post(base_url.rstrip("/") + "/chat/completions", json=body, headers=hdr,
                             timeout=60)
    except httpx.HTTPError:
        return None, "unreachable"
    if r.status_code == 200:
        return True, "image probe"
    if r.status_code in (400, 404, 415, 422):
        return False, "image probe"
    return None, f"image probe HTTP {r.status_code}"


async def vision(base_url: str, model: str, key: str = "") -> tuple[bool | None, str]:
    """(supports images?, how we know). None = couldn't tell (backend down)."""
    k = (base_url, model)
    hit = _cache.get(k)
    if hit and time.time() - hit[0] < (TTL if hit[1] is not None else RETRY):
        return hit[1], hit[2]
    async with _locks.setdefault(k, asyncio.Lock()):
        hit = _cache.get(k)  # a concurrent caller may have just filled it
        if hit and time.time() - hit[0] < (TTL if hit[1] is not None else RETRY):
            return hit[1], hit[2]
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            v, how = await _detect(c, base_url, model, key)
        _cache[k] = (time.time(), v, how)
        logger.warning("vision %s %s → %s (%s)", base_url, model, v, how)
        return v, how


async def resolve(s: dict) -> dict:
    """`s` (already config.effective) with capabilities.vision settled:
    forced on/off as set, auto → detected (unknown = off)."""
    m = s.get("vision_mode") or "auto"
    if m == "auto":
        v, how = await vision(s.get("base_url") or "", s.get("model") or "", s.get("api_key") or "")
        on = bool(v)
    else:
        on, how = m == "on", "forced in CONFIG"
    return {**s, "capabilities": {**(s.get("capabilities") or {}), "vision": on},
            "vision_how": how}

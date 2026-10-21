"""Auto-search: a semantic prefetch before the agent's first model call.

The agent reaches for `run_bash find/grep` almost every time it has to find
something (1,711 shell searches vs 7 semantic_search calls in 390 turns,
10-12..10-17), and each search round is a full model round trip. When a
message refers to something that already exists, the RAG node's hits ride
along with it as a hint, so the first model call can go straight to the file.

Gate (cheap → slower, both well under a model round trip):
  1. REFERENCE regex — the phrasings people use for existing things ("find",
     "where's", "the image we made", "a while ago we", "last time") → search
     at once, no Laya wait.
  2. otherwise Laya (decide backends, System 1 only — never the LLM
     fallback) answers three yes/no questions in one pass, run in parallel
     with the searches; any of them >= threshold passes.

Searches (parallel): the message itself; the message plus the previous user
message when there is one (follow-ups like "apply batch 2" name nothing);
media only (image/video/audio), so "the reference image of that neon ninja
to make a new video episode" still surfaces the IMAGE the instruction words
would otherwise bury. Hits merge by path, best first.

A/B on 18 find questions (2026-10-17, real agent + tools): median time to
first touch the right file 16.8 s → 9.7 s, tool calls 2 → 1. Every failure
(node down, Laya down, budget blown) silently means no hint.
"""
import asyncio
import logging
import re
import time

from . import config, decide, local_tools, rag

logger = logging.getLogger("langbang.prefetch")

DEFAULTS = {"enabled": True, "threshold": 0.5, "top_k": 6, "media_k": 3, "budget_s": 1.5}

REFERENCE = re.compile(r"""
    \b(?:we|i|you)\s+(?:made|created|generated|built|wrote|did|worked|used|set\s+up|talked|discussed|saved|rendered)\b
  | \ba\s+while\s+(?:ago|back)\b | \blast\s+(?:time|week|night|month)\b | \bremember\s+(?:when|that|the)\b
  | \bthe\s+other\s+day\b
  | \b(?:where(?:'s|\s+is|\s+are|\s+did|\s+was|\s+do)|find|locate|look\s+up|dig\s+up|pull\s+up)\b
  | \b(?:that|those|our|the\s+last|like\s+the)\b[^.?!\n]{0,40}?\b(?:image|picture|photo|pic|render|video|film|movie
      |episode|clip|song|track|audio|recording|script|file|note|doc|document|project|skill|thread|chat|diagram|poster
      |report|config|repo)s?\b
""", re.I | re.X)

GATE_QUESTIONS = {
    "where": {"type": "noul", "instructions": "Is the user asking where something is, or referring to an existing "
              "file, script, note, image or past project that must be found first?"},
    "past": {"type": "noul", "instructions": "Does the message refer back to earlier work, a past project or "
             "conversation, or something previously created or saved?"},
    "made": {"type": "noul", "instructions": "Does the message mention a specific thing that was made, saved, "
             "generated or worked on before (a file, image, video, song, script, project or chat)?"},
}
LAYA_CAP = 1500  # chars of state (Laya's checkpoint reads 512 tokens)
NOTE_HEAD = ("[LangBang auto-search: indexed files that may be relevant to this message "
             "(semantic score, kind, path). Verify before relying on them; ignore any that are unrelated.]")


def settings(s=None):
    s = s or config.load()
    r = dict(DEFAULTS)
    r.update(((s.get("rag") or {}).get("prefetch")) or {})
    return r


def _clean(text):
    # attachment stamps carry upload paths, not meaning
    text = re.sub(r"\[attached (?:image|file) \d+: [^\]]*\]", "", text or "").strip()
    return "" if text == "[image]" else text


async def _laya(s, text):
    """max P(yes) over GATE_QUESTIONS from the first System 1 backend that
    answers; None when none does (the regex gate still stands)."""
    cfg = s.get("decide") or {}
    if not cfg.get("enabled", True):
        return None
    body = {"state": text[:LAYA_CAP], "questions": GATE_QUESTIONS}
    for b in cfg.get("backends") or []:
        if not (b.get("url") and b.get("enabled", True)):
            continue
        try:
            r = await decide._systemone(b, body, timeout=3)
            return max(float(a.get("noul", 0)) for a in (r.get("answers") or {}).values())
        except Exception as e:  # noqa: BLE001 - next backend, else no gate
            logger.warning("prefetch: decide backend %s failed: %r", b.get("name"), e)
    return None


def _search(q, category=""):
    return rag.search(q, category=category, top_k=8, timeout=4).get("results") or []


async def gather(text, prior="", s=None, drop_tids=()):
    """→ {note, hits, gate, why, ms, queries} or None (no hint this turn).
    `drop_tids`: threads whose turns must not be hinted (other projects)."""
    s = s or config.load()
    p = settings(s)
    if not p["enabled"] or not rag.settings(s)["enabled"]:
        return None
    text = _clean(text)
    if len(text) < 6:
        return None
    t0 = time.perf_counter()
    prior = _clean(prior)[-600:]
    th = asyncio.to_thread
    jobs = {"now": th(_search, text), "media": th(_search, text, "image,video,audio")}
    if prior:
        jobs["ctx"] = th(_search, prior + "\n" + text)
    ref = bool(REFERENCE.search(text))
    tasks = {k: asyncio.ensure_future(v) for k, v in jobs.items()}
    gate, why = None, "phrase" if ref else ""
    try:
        if not ref:
            gate = await asyncio.wait_for(_laya(s, (prior + "\n" if prior else "") + text), p["budget_s"])
            if gate is None or gate < float(p["threshold"]):
                return None
            why = f"laya {gate:.2f}"
        left = max(0.2, p["budget_s"] - (time.perf_counter() - t0))
        done, _ = await asyncio.wait(tasks.values(), timeout=left)
    except asyncio.TimeoutError:
        return None
    finally:
        for t in tasks.values():
            if not t.done():
                t.cancel()
    res = {}
    for k, t in tasks.items():
        if t in done and not t.cancelled() and t.exception() is None:
            res[k] = [r for r in t.result() if not _dropped(r, drop_tids)]
        elif t in done and not t.cancelled():
            logger.warning("prefetch: %s search failed: %r", k, t.exception())
    if not res:
        return None
    k = int(p["top_k"])
    # message hits and context hits interleave (each found files the other
    # missed on the real-turn replay), media hits top up the list
    picked, seen = [], set()

    def take(rows, n):
        for r in rows:
            key = (r.get("origin"), r.get("path"))
            if n and key not in seen:
                seen.add(key); picked.append(r); n -= 1
    for a, b in zip(res.get("now", []) + [None] * k, res.get("ctx", []) + [None] * k):
        if len(picked) >= k:
            break
        take([x for x in (a, b) if x], 2)
    picked = picked[:k]
    take(res.get("media", []), int(p["media_k"]))
    if not picked:
        return None
    hits = local_tools._fmt_hits({"results": picked})
    return {"note": NOTE_HEAD + "\n" + hits, "hits": hits, "gate": gate, "why": why,
            "ms": round((time.perf_counter() - t0) * 1000), "queries": sorted(res)}


def _dropped(r, tids):
    """A thread-turn hit (path lb-thread:<tid>/<turn>) from a dropped thread."""
    p = str(r.get("path") or "")
    return bool(tids) and p.startswith("lb-thread:") and p[10:].split("/", 1)[0] in tids


def render(m):
    """Model-view copy of a HumanMessage with its prefetch note appended
    (the stored message — and the chat bubble — stay as the user typed)."""
    pf = (m.additional_kwargs or {}).get("lb_prefetch")
    if not pf or not pf.get("note"):
        return m
    note = "\n\n" + pf["note"]
    c = m.content
    if isinstance(c, str):
        c = c + note
    elif isinstance(c, list):
        c = [*c, {"type": "text", "text": note}]
    return m.model_copy(update={"content": c})


def summary(pf):
    """Small dict for the UI / history (no note body duplication)."""
    return {"hits": pf["hits"], "why": pf["why"], "ms": pf["ms"],
            **({"gate": round(pf["gate"], 3)} if pf.get("gate") is not None else {})}


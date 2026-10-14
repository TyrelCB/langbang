"""Decision endpoint (POST /v1/systemone): fast typed judgments.

A "System 1" decision model reads a state (text, email, ticket, JSON) plus
typed questions and returns answers with probabilities in one forward pass —
no generated text, so nothing to parse and no invented labels. Question types
(the TypeSafe Jev wire protocol, which Laya's laya-serve also speaks):

  choice  pick one key of `criteria` ({key: description} or [key, ...])
  score   ordinal over `criteria` ([label0, label1, ...]); score = E[index]
  noul    probability the answer is yes

Backends are tried in order (settings `decide.backends`, each anything that
speaks POST /v1/systemone: local Laya on :8901, a hosted Jev, a fleet box).
When every one is down — or a request names `x_backend: "llm"` — the chat
model answers the same questions through a JSON schema whose enums are the
option keys (System 2 standing in: slower, and its probabilities are its own
estimate, flagged `x_calibrated: false`). `escalate_below` re-asks only the
System 1 answers whose confidence fell under it.
"""
import json
import logging
import os
import time

import httpx

from . import config

logger = logging.getLogger("langbang.decide")

TYPES = ("choice", "score", "noul")
STATE_CAP = 12000  # chars of state shown to the LLM fallback


def _key(b: dict) -> str:
    """Bearer token for a backend: `key_file` names a file under data/keys/."""
    name = os.path.basename(b.get("key_file") or "")
    if not name:
        return ""
    try:
        with open(os.path.join(config.DATA_DIR, "keys", name)) as f:
            return f.read().strip()
    except OSError:
        logger.warning("decide backend %s: key file %s unreadable", b.get("name"), name)
        return ""


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"} if key and key != "none" else {}


def validate(questions) -> dict:
    """Shape check before any backend sees it; ValueError text is client-safe."""
    if not isinstance(questions, dict) or not questions:
        raise ValueError("'questions' must be a non-empty object {id: question}")
    for qid, q in questions.items():
        if not isinstance(q, dict):
            raise ValueError(f"question {qid!r} must be an object")
        if q.get("type") not in TYPES:
            raise ValueError(f"question {qid!r}: type must be one of {', '.join(TYPES)}")
        if q["type"] in ("choice", "score") and not _options(q):
            raise ValueError(f"question {qid!r}: {q['type']} needs non-empty 'criteria'")
    return questions


def _options(q: dict) -> list[tuple[str, str]]:
    """[(key, description)] for choice/score criteria, dict or list form."""
    c = q.get("criteria")
    if isinstance(c, dict):
        return [(str(k), str(v or "")) for k, v in c.items()]
    if isinstance(c, list):
        return [(str(k), "") for k in c]
    return []


# ---- System 1 backends ----

async def _systemone(b: dict, body: dict, timeout: float) -> dict:
    url = b["url"].rstrip("/") + "/v1/systemone"
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json=body, headers=_headers(_key(b)))
    if r.status_code in (400, 422):  # the request is wrong, not the backend
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:300]
        raise ValueError(f"{b.get('name')}: {detail}")
    r.raise_for_status()
    return r.json()


async def health(b: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(b["url"].rstrip("/") + "/health", headers=_headers(_key(b)))
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return {"name": b.get("name"), "url": b["url"], "ok": r.status_code == 200,
                "loaded": j.get("loaded"), "device": j.get("device")}
    except (httpx.HTTPError, ValueError) as e:
        return {"name": b.get("name"), "url": b["url"], "ok": False, "error": type(e).__name__}


# ---- System 2 stand-in: the chat model through a JSON schema ----

def _conn(s: dict, cfg: dict) -> dict:
    pname = cfg.get("llm_provider") or ""
    if pname and pname in (s.get("providers") or {}):
        return config.effective(s, {"provider": pname, "model": cfg.get("llm_model") or ""})
    if cfg.get("llm_model"):
        return config.effective(s, {"model": cfg["llm_model"]})
    return s


def _schema(questions: dict) -> dict:
    props = {}
    for qid, q in questions.items():
        conf = {"type": "number", "minimum": 0, "maximum": 1}
        if q["type"] == "noul":
            props[qid] = {"type": "object", "properties": {"yes_probability": conf},
                          "required": ["yes_probability"], "additionalProperties": False}
        else:
            keys = [k for k, _ in _options(q)]
            pick = ({"enum": keys} if q["type"] == "choice"
                    else {"type": "integer", "enum": list(range(len(keys)))})
            props[qid] = {"type": "object", "properties": {"answer": pick, "confidence": conf},
                          "required": ["answer", "confidence"], "additionalProperties": False}
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


def _prompt(state, questions: dict) -> str:
    st = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False, indent=1)
    lines = [f"STATE:\n{st[:STATE_CAP]}", "", "QUESTIONS:"]
    for qid, q in questions.items():
        lines.append(f"- {qid} ({q['type']}): {q.get('instructions') or ''}")
        if q["type"] == "noul":
            lines.append("    answer with yes_probability = P(yes)")
        elif q["type"] == "choice":
            for k, d in _options(q):
                lines.append(f"    {k}" + (f": {d}" if d else ""))
        else:
            for i, (k, _) in enumerate(_options(q)):
                lines.append(f"    {i} = {k}")
    return "\n".join(lines)


SYSTEM = ("You are a decision function. Judge only from the STATE. For every question "
          "give the answer and your honest probability that it is right. Output only "
          "the JSON object the schema describes.")


def _parse(text: str) -> dict:
    text = (text or "").strip()
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("model returned no JSON object")
    return json.loads(text[i:j + 1])


def _shape(questions: dict, raw: dict) -> dict:
    """LLM JSON → laya/Jev answer objects. The probability mass the model did
    not give its pick is spread evenly over the rest (it never saw them
    scored), so only the pick's probability carries meaning."""
    out = {}
    for qid, q in questions.items():
        a = raw.get(qid)
        if not isinstance(a, dict):
            raise ValueError(f"model skipped question {qid!r}")
        if q["type"] == "noul":
            p = min(1.0, max(0.0, float(a["yes_probability"])))
            out[qid] = {"type": "noul", "noul": round(p, 4), "confidence": round(max(p, 1 - p), 4),
                        "answer_confidence": round(max(p, 1 - p), 4), "x_calibrated": False}
            continue
        keys = [k for k, _ in _options(q)]
        p = min(1.0, max(0.0, float(a["confidence"])))
        rest = (1 - p) / (len(keys) - 1) if len(keys) > 1 else 0.0
        if q["type"] == "choice":
            pick = str(a["answer"])
            if pick not in keys:
                raise ValueError(f"model answered {qid!r} with unknown option {pick!r}")
            probs = {k: round(p if k == pick else rest, 4) for k in keys}
            out[qid] = {"type": "choice", "choice": pick, "probabilities": probs,
                        "confidence": round(p, 4), "answer_confidence": round(p, 4),
                        "x_calibrated": False}
        else:
            idx = int(a["answer"])
            if not 0 <= idx < len(keys):
                raise ValueError(f"model answered {qid!r} with out-of-range score {idx}")
            probs = {str(i): (p if i == idx else rest) for i in range(len(keys))}
            score = sum(i * v for i, v in enumerate(probs.values()))
            out[qid] = {"type": "score", "score": round(score, 3),
                        "legend": {str(i): k for i, k in enumerate(keys)},
                        "probabilities": {k: round(v, 4) for k, v in probs.items()},
                        "confidence": round(p, 4), "answer_confidence": round(p, 4),
                        "x_calibrated": False}
    return out


async def _llm(s: dict, cfg: dict, state, questions: dict, timeout: float) -> dict:
    conn = _conn(s, cfg)
    body = {"model": conn["model"], "temperature": 0, "max_tokens": 200 + 60 * len(questions),
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": _prompt(state, questions)}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "decision", "schema": _schema(questions), "strict": True}}}
    if conn.get("template_kwargs", True):
        body["chat_template_kwargs"] = {"enable_thinking": False}
    url = conn["base_url"].rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json=body, headers=_headers(conn.get("api_key")))
        if r.status_code == 400:  # no structured output / template kwargs: plain JSON ask
            logger.warning("decide: %s refused the schema request (%s); asking without it",
                           conn["base_url"], r.text[:200])
            body.pop("response_format", None)
            body.pop("chat_template_kwargs", None)
            r = await c.post(url, json=body, headers=_headers(conn.get("api_key")))
    r.raise_for_status()
    j = r.json()
    msg = j["choices"][0]["message"]
    answers = _shape(questions, _parse(msg.get("content") or ""))
    u = j.get("usage") or {}
    return {"model": conn["model"], "answers": answers,
            "usage": {"input_tokens": u.get("prompt_tokens", 0),
                      "output_tokens": u.get("completion_tokens", 0)}}


# ---- entry point ----

async def decide(body: dict) -> dict:
    """One /v1/systemone request. Extra request fields (all optional):
    x_backend — a backend name, or "llm" for the chat model only;
    x_escalate_below — overrides settings escalate_below for this call."""
    s = config.load()
    cfg = s.get("decide") or {}
    if not cfg.get("enabled", True):
        raise PermissionError("decision endpoint is disabled (CONFIG → decide.enabled)")
    questions = validate(body.get("questions"))
    state = body.get("state")
    timeout = float(cfg.get("timeout_s") or 10)
    want = body.get("x_backend") or ""
    backends = [b for b in (cfg.get("backends") or []) if b.get("url") and b.get("enabled", True)]
    if want and want != "llm":
        backends = [b for b in backends if b.get("name") == want]
        if not backends:
            raise ValueError(f"no enabled decision backend named {want!r}")
    fwd = {k: v for k, v in body.items() if not k.startswith("x_")}
    t0 = time.perf_counter()
    meta = {"failed": []}
    result = None
    if want != "llm":
        for b in backends:
            try:
                result = await _systemone(b, fwd, timeout)
                meta["backend"] = b.get("name")
                break
            except ValueError:
                raise
            except (httpx.HTTPError, KeyError) as e:
                logger.warning("decide backend %s failed: %r", b.get("name"), e)
                meta["failed"].append({"backend": b.get("name"), "error": type(e).__name__})
    if result is None:
        if want != "llm" and not cfg.get("llm_fallback", True):
            raise ConnectionError("every System 1 backend failed and llm_fallback is off")
        result = await _llm(s, cfg, state, questions, max(timeout, 30))
        meta["backend"] = "llm"
    else:
        floor = body.get("x_escalate_below", cfg.get("escalate_below") or 0)
        low = {q: questions[q] for q, a in (result.get("answers") or {}).items()
               if q in questions and float(a.get("confidence", 1)) < float(floor or 0)}
        if low:
            try:
                fix = await _llm(s, cfg, state, low, max(timeout, 30))
                for q, a in fix["answers"].items():
                    a["x_system1"] = result["answers"][q]
                    result["answers"][q] = a
                meta["escalated"] = sorted(low)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                logger.warning("decide: escalation failed, keeping System 1 answers: %r", e)
                meta["escalation_error"] = type(e).__name__
    meta["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    if not meta["failed"]:
        meta.pop("failed")
    result["x_langbang"] = meta
    return result


async def status() -> dict:
    s = config.load()
    cfg = s.get("decide") or {}
    conn = _conn(s, cfg)
    return {"enabled": cfg.get("enabled", True),
            "backends": [await health(b) for b in (cfg.get("backends") or []) if b.get("url")],
            "llm_fallback": cfg.get("llm_fallback", True),
            "llm": {"provider": conn.get("provider"), "model": conn.get("model")},
            "escalate_below": cfg.get("escalate_below") or 0}

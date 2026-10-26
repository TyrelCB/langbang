"""▤ BENCH — model/provider evaluations, starting with GPQA Diamond.

GPQA Diamond: 198 graduate-level 4-choice science questions (Rein et al.).
The dataset is gated on Hugging Face and its authors ask that it never be
published in plain text — this repo is public, so it is downloaded at
runtime into DATA_DIR/bench/ (gitignored) with the user's HF token and
never committed. Same for model outputs (DATA_DIR/bench.db).

Each run = one provider/model + sampling params over the first `n`
questions of a FIXED seeded order (so a 20-question quick run is the same
20 questions for every model) with seeded answer shuffles. Every answer is
persisted as it lands (running totals, and a stopped / restarted run
resumes where it left off). Requests go straight to the provider's
/chat/completions with LangBang's thinking controls (agent.extra_body:
enable_thinking, reasoning effort, llama-server thinking budget).
"""

import asyncio
import csv
import hashlib
import json
import logging
import os
import re
import time
import uuid

import aiosqlite
import httpx

from . import config

logger = logging.getLogger("langbang.bench")

BENCH_DIR = os.path.join(config.DATA_DIR, "bench")
DB_PATH = os.path.join(config.DATA_DIR, "bench.db")
GPQA_CSV = os.path.join(BENCH_DIR, "gpqa_diamond.csv")
GPQA_URL = "https://huggingface.co/datasets/Idavidrein/gpqa/resolve/main/gpqa_diamond.csv"
BENCHES = {"gpqa_diamond": "GPQA Diamond"}
LETTERS = "ABCD"
# simple-evals' GPQA zero-shot CoT prompt
PROMPT = ("Answer the following multiple choice question. The last line of your response "
          "should be of the following format: 'Answer: $LETTER' (without quotes) where LETTER "
          "is one of ABCD. Think step by step before answering.\n\n{q}\n\n"
          "A) {A}\nB) {B}\nC) {C}\nD) {D}")
ANSWER_RE = re.compile(r"(?i)answer\s*(?:is)?\s*[:：]?\s*\**\s*\(?([ABCD])\)?\b")
BOXED_RE = re.compile(r"\\boxed\{\s*\(?([ABCD])\)?\s*\}")
DEFAULTS = {"n": 198, "concurrency": 1, "max_tokens": 32768, "thinking": True,
            "budget": 0, "temperature": 0.6}  # GPQA needs ≥32k CoT+answer room
TEXT_CAP = 6000  # chars of answer text kept per item (reasoning: length only)

_db: aiosqlite.Connection | None = None
_tasks: dict[str, asyncio.Task] = {}
_items: list[dict] | None = None


# ---- dataset ----

def _hf_token() -> str:
    tok = os.environ.get("HF_TOKEN") or config.private("hf_token")
    if tok:
        return tok.strip()
    try:
        return open(os.path.expanduser("~/.cache/huggingface/token"), encoding="utf-8").read().strip()
    except OSError:
        return ""


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def items() -> list[dict]:
    """GPQA Diamond in the fixed benchmark order, choices shuffled per
    question by a seed from its Record ID. [] when not downloaded."""
    global _items
    if _items is not None:
        return _items
    try:
        rows = list(csv.DictReader(open(GPQA_CSV, encoding="utf-8")))
    except OSError:
        return []
    out = []
    for r in rows:
        rid = r.get("Record ID") or _h(r["Question"])[:16]
        ch = [r["Correct Answer"], r["Incorrect Answer 1"], r["Incorrect Answer 2"], r["Incorrect Answer 3"]]
        order = sorted(range(4), key=lambda i: _h(f"{rid}:{i}"))
        opts = [ch[i].strip() for i in order]
        out.append({"qid": rid, "domain": r.get("High-level domain") or "",
                    "gold": LETTERS[order.index(0)],
                    "prompt": PROMPT.format(q=r["Question"].strip(), **dict(zip(LETTERS, opts)))})
    out.sort(key=lambda x: _h("order:" + x["qid"]))
    _items = out
    return out


def dataset_status() -> dict:
    n = len(items())
    return {"ok": n > 0, "n": n, "has_token": bool(_hf_token())}


async def download() -> dict:
    global _items
    tok = _hf_token()
    if not tok:
        raise RuntimeError("no Hugging Face token (HF_TOKEN, settings private.hf_token, "
                           "or `huggingface-cli login`)")
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(GPQA_URL, headers={"Authorization": f"Bearer {tok}"})
    if r.status_code in (401, 403):
        raise RuntimeError("HF refused: accept the dataset terms at "
                           "huggingface.co/datasets/Idavidrein/gpqa with this token's account")
    r.raise_for_status()
    os.makedirs(BENCH_DIR, exist_ok=True)
    tmp = GPQA_CSV + ".tmp"
    open(tmp, "wb").write(r.content)
    os.replace(tmp, GPQA_CSV)
    _items = None
    return dataset_status()


# ---- scoring ----

def extract(text: str) -> str:
    """The model's letter: last 'Answer: X', else last \\boxed{X}, else ''."""
    m = ANSWER_RE.findall(text or "") or BOXED_RE.findall(text or "")
    return m[-1].upper() if m else ""


# ---- storage ----

async def init() -> None:
    global _db
    os.makedirs(config.DATA_DIR, exist_ok=True)
    _db = await aiosqlite.connect(DB_PATH)
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA busy_timeout=30000")
    await _db.executescript("""
        CREATE TABLE IF NOT EXISTS runs(
          id TEXT PRIMARY KEY, bench TEXT NOT NULL, provider TEXT, model TEXT NOT NULL,
          params TEXT NOT NULL, n INTEGER NOT NULL, created REAL NOT NULL,
          ended REAL, state TEXT NOT NULL,   -- running | done | stopped | lost | error
          error TEXT);
        CREATE TABLE IF NOT EXISTS items(
          run_id TEXT NOT NULL, idx INTEGER NOT NULL, qid TEXT, gold TEXT, pred TEXT,
          correct INTEGER, out_tokens INTEGER, reasoning_chars INTEGER, secs REAL,
          finish TEXT, error TEXT, text TEXT, PRIMARY KEY(run_id, idx));
    """)
    # a restart killed whatever was running: resumable, answers kept
    await _db.execute("UPDATE runs SET state='lost' WHERE state='running'")
    await _db.commit()


async def _write(sql: str, args: tuple) -> None:
    try:
        await _db.execute(sql, args)
        await _db.commit()
    except Exception:
        await _db.rollback()  # never leave the WAL writer lock held
        raise


def _row(r) -> dict:  # noqa: ANN001
    return {"id": r[0], "bench": r[1], "provider": r[2], "model": r[3],
            "params": json.loads(r[4]), "n": r[5], "created": r[6], "ended": r[7],
            "state": r[8], "error": r[9]}


async def _stats(rid: str) -> dict:
    cur = await _db.execute(
        "SELECT COUNT(*), SUM(correct), SUM(pred=''), SUM(error IS NOT NULL), "
        "SUM(finish='length'), AVG(out_tokens), SUM(secs) FROM items WHERE run_id=?", (rid,))
    done, ok, nopred, err, cut, tok, secs = await cur.fetchone()
    scored = (done or 0) - (err or 0)
    return {"done": done or 0, "correct": ok or 0, "scored": scored,
            "acc": round(100 * (ok or 0) / scored, 1) if scored else None,
            "no_answer": nopred or 0, "errors": err or 0, "truncated": cut or 0,
            "avg_tokens": round(tok) if tok else None, "secs": round(secs or 0)}


async def list_runs() -> list[dict]:
    cur = await _db.execute("SELECT * FROM runs ORDER BY created DESC")
    return [{**_row(r), **(await _stats(r[0]))} for r in await cur.fetchall()]


async def run_items(rid: str, full: bool = False) -> list[dict]:
    cur = await _db.execute(
        "SELECT idx,qid,gold,pred,correct,out_tokens,reasoning_chars,secs,finish,error"
        + (",text" if full else "") + " FROM items WHERE run_id=? ORDER BY idx", (rid,))
    keys = ["idx", "qid", "gold", "pred", "correct", "out_tokens", "reasoning_chars",
            "secs", "finish", "error"] + (["text"] if full else [])
    return [dict(zip(keys, r)) for r in await cur.fetchall()]


async def item(rid: str, idx: int) -> dict | None:
    got = [x for x in await run_items(rid, full=True) if x["idx"] == idx]
    if not got:
        return None
    qs = items()
    return {**got[0], "prompt": qs[idx]["prompt"] if idx < len(qs) else ""}


# ---- running ----

def _settings(prov: str, model: str, p: dict) -> dict:
    s = config.effective(config.load(), {"provider": prov, "model": model})
    return {**s, "enable_thinking": bool(p["thinking"]), "thinking_budget": int(p["budget"] or 0),
            "max_tokens": int(p["max_tokens"]), "reasoning_effort": p.get("effort") or s.get("reasoning_effort")}


async def _ask(c: httpx.AsyncClient, s: dict, prompt: str, temperature: float) -> dict:
    from . import agent  # imports bench-free modules only; deferred to keep startup lean
    body = {"model": s["model"], "max_tokens": s["max_tokens"], "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}], **(agent.extra_body(s) or {})}
    url = s["base_url"].rstrip("/") + "/chat/completions"
    hdr = {"Authorization": f"Bearer {s.get('api_key') or 'none'}"}
    for attempt in range(6):
        r = await c.post(url, json=body, headers=hdr)
        if r.status_code == 400:  # a field this backend refuses: drop it (like _drop_optional)
            bad = [k for k in ("chat_template_kwargs", "reasoning_budget_tokens",
                               "reasoning_budget_message") if k in body and k in r.text]
            if bad:
                for k in bad:
                    body.pop(k)
                continue
        if r.status_code in (429, 502, 503) and attempt < 5:  # busy backend (sglang caps ~4 streams)
            await asyncio.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


async def _one(c: httpx.AsyncClient, rid: str, idx: int, q: dict, s: dict, p: dict) -> None:
    t0 = time.time()
    try:
        j = await _ask(c, s, q["prompt"], float(p["temperature"]))
        ch = j["choices"][0]
        msg = ch.get("message") or {}
        text = msg.get("content") or ""
        rc = msg.get("reasoning_content") or msg.get("reasoning") or ""
        pred = extract(text)
        row = (rid, idx, q["qid"], q["gold"], pred, int(pred == q["gold"]),
               (j.get("usage") or {}).get("completion_tokens"), len(rc),
               round(time.time() - t0, 1), ch.get("finish_reason"), None, text[-TEXT_CAP:])
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - one failed question never kills the run
        row = (rid, idx, q["qid"], q["gold"], "", 0, None, None, round(time.time() - t0, 1),
               None, f"{type(e).__name__}: {e}"[:400], "")
    await _write("INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row)


async def _run(rid: str) -> None:
    cur = await _db.execute("SELECT * FROM runs WHERE id=?", (rid,))
    run = _row(await cur.fetchone())
    p = {**DEFAULTS, **run["params"]}
    qs = items()[: run["n"]]
    cur = await _db.execute("SELECT idx FROM items WHERE run_id=? AND error IS NULL", (rid,))
    have = {r[0] for r in await cur.fetchall()}
    todo = [i for i in range(len(qs)) if i not in have]
    s = _settings(run["provider"], run["model"], p)
    sem = asyncio.Semaphore(max(1, min(8, int(p["concurrency"]))))
    try:
        # read timeout: a 32k-token thinking answer at ~40 tok/s is ~14 min
        async with httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=15)) as c:
            async def go(i):
                async with sem:
                    await _one(c, rid, i, qs[i], s, p)
            await asyncio.gather(*(go(i) for i in todo))
        await _write("UPDATE runs SET state='done', ended=? WHERE id=?", (time.time(), rid))
    except asyncio.CancelledError:
        await _write("UPDATE runs SET state='stopped', ended=? WHERE id=?", (time.time(), rid))
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("bench run %s failed", rid)
        await _write("UPDATE runs SET state='error', error=?, ended=? WHERE id=?",
                     (str(e)[:400], time.time(), rid))
    finally:
        _tasks.pop(rid, None)


def _spawn(rid: str) -> None:
    _tasks[rid] = asyncio.get_running_loop().create_task(_run(rid))


async def start(bench: str, provider: str, model: str, params: dict) -> dict:
    if bench not in BENCHES:
        raise ValueError(f"unknown benchmark {bench!r}")
    qs = items()
    if not qs:
        raise ValueError("dataset not downloaded")
    s = config.load()
    if provider not in s["providers"]:
        raise ValueError(f"unknown provider {provider!r}")
    model = model or config.effective(s, {"provider": provider})["model"]
    p = {**DEFAULTS, **{k: v for k, v in (params or {}).items() if k in DEFAULTS or k == "effort"}}
    n = max(1, min(len(qs), int(p.pop("n"))))
    rid = uuid.uuid4().hex[:12]
    await _write("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (rid, bench, provider, model, json.dumps(p), n, time.time(), None, "running", None))
    _spawn(rid)
    return {"id": rid}


async def stop(rid: str) -> bool:
    t = _tasks.get(rid)
    if not t:
        return False
    t.cancel()
    return True


async def resume(rid: str) -> bool:
    if rid in _tasks:
        return False
    await _write("UPDATE runs SET state='running', ended=NULL, error=NULL WHERE id=?", (rid,))
    _spawn(rid)
    return True


async def delete(rid: str) -> None:
    await stop(rid)
    await _write("DELETE FROM items WHERE run_id=?", (rid,))
    await _write("DELETE FROM runs WHERE id=?", (rid,))

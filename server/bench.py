"""▤ BENCH — model/provider evaluations: GPQA Diamond + Terminal-Bench 2.0.

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

Terminal-Bench 2.0 (89 agentic shell tasks, each in its own container):
run through Harbor (`uv tool install harbor`) with its reference
terminus-2 agent pointed at the provider's OpenAI-compatible endpoint —
one Harbor job PER TASK, so progress/resume are per task and each task's
image (0.5-1.5 GB, prebuilt) is removed after its trial unless it was
already on the box. Reward 1.0 = the task's tests pass.
"""

import asyncio
import csv
import glob
import shutil
import signal
import tomllib
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
TB_DIR = os.path.join(BENCH_DIR, "terminal-bench-2.0")
JOBS_DIR = os.path.join(BENCH_DIR, "jobs")
TB_DATASET = "terminal-bench@2.0"
MIN_FREE_GB = 8  # a task image is ≤ ~1.5 GB; stop starting tasks below this
BENCHES = {"gpqa_diamond": "GPQA Diamond", "terminal_bench": "Terminal-Bench 2.0"}
LETTERS = "ABCD"
# simple-evals' GPQA zero-shot CoT prompt
PROMPT = ("Answer the following multiple choice question. The last line of your response "
          "should be of the following format: 'Answer: $LETTER' (without quotes) where LETTER "
          "is one of ABCD. Think step by step before answering.\n\n{q}\n\n"
          "A) {A}\nB) {B}\nC) {C}\nD) {D}")
ANSWER_RE = re.compile(r"(?i)answer\s*(?:is)?\s*[:：]?\s*\**\s*\(?([ABCD])\)?\b")
BOXED_RE = re.compile(r"\\boxed\{\s*\(?([ABCD])\)?\s*\}")
DEFAULTS = {"n": 198, "concurrency": 1, "max_tokens": 32768, "thinking": True,
            "budget": 0, "temperature": 0.6,  # GPQA needs ≥32k CoT+answer room
            "context": 131072}  # Terminal-Bench: terminus-2's context window (summarizes near it)
TEXT_CAP = 6000  # chars of answer text kept per item (reasoning: length only)
ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*\x07|[()][A-Za-z0-9])|\r")  # tmux pane captures

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


def harbor_bin() -> str:
    return shutil.which("harbor") or os.path.expanduser("~/.local/bin/harbor")


_tb: list[dict] | None = None


def tb_tasks() -> list[dict]:
    """Terminal-Bench tasks in the fixed benchmark order ([] = not downloaded)."""
    global _tb
    if _tb is not None:
        return _tb
    out = []
    for p in glob.glob(os.path.join(TB_DIR, "**", "task.toml"), recursive=True):
        try:
            d = tomllib.load(open(p, "rb"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        m, env = d.get("metadata") or {}, d.get("environment") or {}
        out.append({"qid": os.path.basename(os.path.dirname(p)), "gold": m.get("difficulty") or "",
                    "domain": m.get("category") or "", "image": env.get("docker_image") or "",
                    "timeout": float((d.get("agent") or {}).get("timeout_sec") or 900)})
    out.sort(key=lambda x: _h("order:" + x["qid"]))
    _tb = out if out else None
    return out


def bench_items(bench: str) -> list[dict]:
    return items() if bench == "gpqa_diamond" else tb_tasks()


def dataset_status() -> dict:
    hb = os.path.exists(harbor_bin())
    return {
        "gpqa_diamond": {"ok": bool(items()), "n": len(items()), "has_token": bool(_hf_token())},
        "terminal_bench": {"ok": bool(tb_tasks()) and hb and bool(shutil.which("docker")),
                           "n": len(tb_tasks()), "harbor": hb, "docker": bool(shutil.which("docker")),
                           "free_gb": round(shutil.disk_usage(config.DATA_DIR).free / 2**30, 1)},
    }


async def download(bench: str = "gpqa_diamond") -> dict:
    if bench == "terminal_bench":
        return await _download_tb()
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


async def _download_tb() -> dict:
    global _tb
    if not os.path.exists(harbor_bin()):
        raise RuntimeError("Harbor isn't installed: run `uv tool install harbor` (it drives the task containers)")
    os.makedirs(BENCH_DIR, exist_ok=True)
    tmp = TB_DIR + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    p = await asyncio.create_subprocess_exec(
        harbor_bin(), "dataset", "download", TB_DATASET, "-o", tmp,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env={**os.environ, "COLUMNS": "120"})
    out, _ = await asyncio.wait_for(p.communicate(), 900)
    if p.returncode != 0:
        raise RuntimeError("harbor dataset download failed: " + out.decode(errors="replace")[-400:])
    shutil.rmtree(TB_DIR, ignore_errors=True)
    os.replace(tmp, TB_DIR)
    _tb = None
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
    # a restart killed whatever was running: answers are kept. GPQA runs
    # carry straight on (only the in-flight questions are re-asked);
    # Terminal-Bench ones wait for a manual RESUME — langbang.service is
    # KillMode=process, so the cut-off task's harbor child may still be
    # finishing, and a resume would run the same task twice.
    cur = await _db.execute("SELECT id, bench FROM runs WHERE state='running'")
    cut = await cur.fetchall()
    await _db.execute("UPDATE runs SET state='lost' WHERE state='running'")
    await _db.commit()
    for rid, b in cut:
        if b == "gpqa_diamond":
            logger.warning("bench: resuming %s after restart", rid)
            await resume(rid)


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
    cur = await _db.execute("SELECT bench FROM runs WHERE id=?", (rid,))
    b = (await cur.fetchone() or ["gpqa_diamond"])[0]
    qs = bench_items(b)
    q = qs[idx] if idx < len(qs) else {}
    prompt = q.get("prompt") or ""
    if b == "terminal_bench" and q:
        ins = glob.glob(os.path.join(TB_DIR, "**", q["qid"], "instruction.md"), recursive=True)
        prompt = open(ins[0], encoding="utf-8").read() if ins else q["qid"]
    out = {**got[0], "prompt": prompt}
    if b == "terminal_bench":
        out["text"] = ANSI_RE.sub("", out.get("text") or "")
    return out


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
    qs = bench_items(run["bench"])[: run["n"]]
    cur = await _db.execute("SELECT idx FROM items WHERE run_id=? AND error IS NULL", (rid,))
    have = {r[0] for r in await cur.fetchall()}
    todo = [i for i in range(len(qs)) if i not in have]
    s = _settings(run["provider"], run["model"], p)
    sem = asyncio.Semaphore(max(1, min(8, int(p["concurrency"]))))
    try:
        # read timeout: a 32k-token thinking answer at ~40 tok/s is ~14 min
        async with httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=15)) as c:
            one = _one_tb if run["bench"] == "terminal_bench" else _one

            async def go(i):
                async with sem:
                    await one(c, rid, i, qs[i], s, p)
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


# ---- Terminal-Bench via Harbor ----

async def _docker(*args: str) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec("docker", *args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return p.returncode, out.decode(errors="replace")


async def _cleanup_trial(job_dir: str) -> None:
    """Remove containers of this job's trial(s) — a STOP / timeout kill can
    leave Harbor's compose project (named <trial>__env) running."""
    trials = [os.path.basename(d) for d in glob.glob(os.path.join(job_dir, "*__*")) if os.path.isdir(d)]
    for t in trials:
        _, ids = await _docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={t.lower()}__env")
        if ids.split():
            await _docker("rm", "-f", *ids.split())


def _tb_job(rid: str, q: dict, s: dict, p: dict) -> tuple[str, dict]:
    from . import agent
    job_dir = os.path.join(JOBS_DIR, rid)
    kw = {"temperature": float(p["temperature"]),
          "model_info": {"max_input_tokens": int(p.get("context") or 131072),
                         "max_output_tokens": int(p["max_tokens"]),
                         "input_cost_per_token": 0, "output_cost_per_token": 0},
          "llm_call_kwargs": {"max_tokens": int(p["max_tokens"]),
                              **({"extra_body": eb} if (eb := agent.extra_body(s)) else {})}}
    cfg = {"jobs_dir": job_dir, "job_name": q["qid"], "n_concurrent_trials": 1,
           "agents": [{"name": "terminus-2", "model_name": "openai/" + s["model"],
                       "base_url": s["base_url"].rstrip("/"), "kwargs": kw}],
           "tasks": [{"path": os.path.dirname(glob.glob(os.path.join(TB_DIR, "**", q["qid"], "task.toml"),
                                                        recursive=True)[0])}]}
    return os.path.join(job_dir, q["qid"]), cfg


async def _one_tb(c, rid: str, idx: int, q: dict, s: dict, p: dict) -> None:  # noqa: ANN001
    t0 = time.time()
    job_dir, cfg = _tb_job(rid, q, s, p)
    shutil.rmtree(job_dir, ignore_errors=True)  # a resumed task starts clean
    os.makedirs(os.path.dirname(job_dir), exist_ok=True)
    cfg_path = job_dir + ".json"
    open(cfg_path, "w").write(json.dumps(cfg))
    had_image = bool(q["image"]) and (await _docker("image", "inspect", q["image"]))[0] == 0
    row = None
    proc = None
    try:
        free = shutil.disk_usage(config.DATA_DIR).free / 2**30
        if free < MIN_FREE_GB:
            raise RuntimeError(f"only {free:.1f} GB free (need {MIN_FREE_GB}) — task not started")
        proc = await asyncio.create_subprocess_exec(
            harbor_bin(), "run", "-c", cfg_path, "-q", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "OPENAI_API_KEY": s.get("api_key") or "none", "COLUMNS": "120"})
        # task timeout (agent + verifier + image pull) with headroom
        out, _ = await asyncio.wait_for(proc.communicate(), q["timeout"] * 2.5 + 900)
        res = glob.glob(os.path.join(job_dir, "*__*", "result.json"))
        if not res:
            raise RuntimeError("harbor produced no trial result: " + out.decode(errors="replace")[-300:])
        t = json.load(open(res[0]))
        reward = ((t.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        ar = t.get("agent_result") or {}
        exc = t.get("exception_info") or None
        pane = glob.glob(os.path.join(os.path.dirname(res[0]), "agent", "*.pane"))
        text = open(pane[0], encoding="utf-8", errors="replace").read()[-TEXT_CAP:] if pane else ""
        ok = reward is not None and float(reward) >= 1.0
        etype = (exc or {}).get("exception_type") if isinstance(exc, dict) else (str(exc)[:60] if exc else None)
        row = (rid, idx, q["qid"], q["gold"], "pass" if ok else "fail", int(ok),
               ar.get("n_output_tokens"), ar.get("n_input_tokens"), round(time.time() - t0, 1),
               etype or "done", None, text)
    except asyncio.CancelledError:
        if proc and proc.returncode is None:
            os.killpg(proc.pid, signal.SIGINT)  # let Harbor tear its compose project down
            try:
                await asyncio.wait_for(proc.wait(), 30)
            except asyncio.TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
        await _cleanup_trial(job_dir)
        raise
    except Exception as e:  # noqa: BLE001 - one broken task never kills the run
        if proc and proc.returncode is None:
            os.killpg(proc.pid, signal.SIGKILL)
        await _cleanup_trial(job_dir)
        row = (rid, idx, q["qid"], q["gold"], "", 0, None, None, round(time.time() - t0, 1),
               None, f"{type(e).__name__}: {e}"[:400], "")
    finally:
        if q["image"] and not had_image:  # disk: drop images this run pulled
            await _docker("rmi", "-f", q["image"])
    await _write("INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row)


def _spawn(rid: str) -> None:
    _tasks[rid] = asyncio.get_running_loop().create_task(_run(rid))


async def start(bench: str, provider: str, model: str, params: dict) -> dict:
    if bench not in BENCHES:
        raise ValueError(f"unknown benchmark {bench!r}")
    qs = bench_items(bench)
    if not qs:
        raise ValueError("dataset not downloaded")
    if bench == "terminal_bench" and not dataset_status()["terminal_bench"]["ok"]:
        raise ValueError("Terminal-Bench needs Harbor + Docker on this box")
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

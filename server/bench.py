"""▤ BENCH — model/provider evaluations: GPQA Diamond, IFEval, Terminal-Bench 2.0,
SWE-bench Verified.

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

IFEval (541 prompts with verifiable formatting instructions, google/IFEval):
scored by Google's checkers, vendored from lm-eval in server/ifeval/.
Headline = prompt-level strict accuracy (every instruction in the prompt
followed); instruction-level and "loose" (lenient on markdown/first/last
line) are kept per answer in items.extra.

Terminal-Bench 2.0 (89 agentic shell tasks, each in its own container):
run through Harbor (`uv tool install harbor`) with its reference
terminus-2 agent pointed at the provider's OpenAI-compatible endpoint —
one Harbor job PER TASK, so progress/resume are per task and each task's
image (0.5-1.5 GB, prebuilt) is removed after its trial unless it was
already on the box. Reward 1.0 = the task's tests pass.

SWE-bench Verified (500 human-validated GitHub issues from 12 Python repos):
the same Harbor path with its swebench-verified adapter — the agent edits
the repo in the task's container, then the hidden FAIL_TO_PASS/PASS_TO_PASS
tests run. Default agent mini-swe-agent (the SWE-bench "bash only"
leaderboard harness); terminus-2 also works. The images are x86_64 and
1-3 GB each, so a Harbor run can go to another box: params.host = an SSH
host with Docker + Harbor (~/.local/bin/harbor). LangBang then copies the
job config over, runs `harbor run` there (task definitions downloaded into
~/langbang-bench/ on first use), and rsyncs the trial dir back for scoring.
"""

import asyncio
import csv
import functools
import glob
import shutil
import signal
import tomllib
import hashlib
import json
import logging
import os
import platform
import re
import shlex
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
IFE_PATH = os.path.join(BENCH_DIR, "ifeval_input_data.jsonl")
IFE_URL = "https://huggingface.co/datasets/google/IFEval/resolve/main/ifeval_input_data.jsonl"
NLTK_DIR = os.path.join(BENCH_DIR, "nltk_data")  # punkt_tab for the IFEval checkers
THINK_RE = re.compile(r"(?s)^.*?</think>\s*")  # reasoning leaked into content (reasoning_format none)
TB_DIR = os.path.join(BENCH_DIR, "terminal-bench-2.0")
JOBS_DIR = os.path.join(BENCH_DIR, "jobs")
TB_DATASET = "terminal-bench@2.0"
SWE_DIR = os.path.join(BENCH_DIR, "swebench-verified")
MIN_FREE_GB = 8  # a task image is ≤ ~3 GB; stop starting tasks below this
BENCHES = {"gpqa_diamond": "GPQA Diamond", "ifeval": "IFEval", "terminal_bench": "Terminal-Bench 2.0",
           "swebench_verified": "SWE-bench Verified"}
# Harbor-run benchmarks: local task dir, registry dataset, default agent
HARBOR = {"terminal_bench": (TB_DIR, TB_DATASET, "terminus-2"),
          "swebench_verified": (SWE_DIR, "swebench-verified@1.0", "mini-swe-agent")}
AGENTS = ("terminus-2", "mini-swe-agent")
REMOTE_ROOT = "langbang-bench"  # under the SSH host's home
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")
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
            "context": 131072,  # Terminal-Bench: terminus-2's context window (summarizes near it)
            "agent": "", "host": ""}  # Harbor benches: "" = the bench's default agent / this box
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


_hb: dict[str, list[dict]] = {}
FROM_RE = re.compile(r"(?im)^\s*FROM\s+(\S+)")


def harbor_tasks(bench: str) -> list[dict]:
    """A Harbor benchmark's tasks in the fixed order ([] = not downloaded).
    `image` = the prebuilt image, else the Dockerfile's FROM (removed after
    the trial for disk; Harbor itself drops the image it builds on top)."""
    if bench in _hb:
        return _hb[bench]
    root, out = HARBOR[bench][0], []
    for p in glob.glob(os.path.join(root, "**", "task.toml"), recursive=True):
        try:
            d = tomllib.load(open(p, "rb"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        m, env = d.get("metadata") or {}, d.get("environment") or {}
        image = env.get("docker_image") or ""
        if not image:
            try:
                f = FROM_RE.search(open(os.path.join(os.path.dirname(p), "environment", "Dockerfile")).read())
                image = f.group(1) if f else ""
            except OSError:
                pass
        out.append({"qid": os.path.basename(os.path.dirname(p)), "gold": m.get("difficulty") or "",
                    "domain": m.get("category") or "", "image": image,
                    "dir": os.path.relpath(os.path.dirname(p), root),
                    "timeout": float((d.get("agent") or {}).get("timeout_sec") or 900)})
    out.sort(key=lambda x: _h("order:" + x["qid"]))
    if out:
        _hb[bench] = out
    return out


_ife: list[dict] | None = None


def ife_items() -> list[dict]:
    """IFEval prompts in the fixed benchmark order ([] = not downloaded)."""
    global _ife
    if _ife is not None:
        return _ife
    try:
        docs = [json.loads(line) for line in open(IFE_PATH, encoding="utf-8") if line.strip()]
    except (OSError, ValueError):
        return []
    out = [{"qid": str(d["key"]), "gold": ",".join(i.split(":")[-1] for i in d["instruction_id_list"]),
            "domain": "", "prompt": d["prompt"], "doc": d} for d in docs]
    out.sort(key=lambda x: _h("order:" + x["qid"]))
    _ife = out or None
    return out


def bench_items(bench: str) -> list[dict]:
    if bench in HARBOR:
        return harbor_tasks(bench)
    return {"gpqa_diamond": items, "ifeval": ife_items}[bench]()


def dataset_status() -> dict:
    hb, dk = os.path.exists(harbor_bin()), bool(shutil.which("docker"))
    free = round(shutil.disk_usage(config.DATA_DIR).free / 2**30, 1)
    return {
        "gpqa_diamond": {"ok": bool(items()), "n": len(items()), "has_token": bool(_hf_token())},
        "ifeval": {"ok": bool(ife_items()) and os.path.isdir(os.path.join(NLTK_DIR, "tokenizers", "punkt_tab")),
                   "n": len(ife_items())},
        # ok = task definitions are here; Harbor + Docker may live on params.host instead
        **{b: {"ok": bool(harbor_tasks(b)), "n": len(harbor_tasks(b)), "harbor": hb, "docker": dk,
               "free_gb": free, "agent": HARBOR[b][2]} for b in HARBOR},
    }


async def download(bench: str = "gpqa_diamond") -> dict:
    if bench in HARBOR:
        return await _download_harbor(bench)
    if bench == "ifeval":
        return await _download_ife()
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


async def _download_ife() -> dict:
    global _ife
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(IFE_URL)
    r.raise_for_status()
    os.makedirs(BENCH_DIR, exist_ok=True)
    open(IFE_PATH + ".tmp", "wb").write(r.content)
    os.replace(IFE_PATH + ".tmp", IFE_PATH)
    from .ifeval import instructions_util  # nltk punkt_tab, into our data dir
    await asyncio.to_thread(instructions_util.download_nltk_resources, NLTK_DIR)
    _ife = None
    return dataset_status()


async def _download_harbor(bench: str) -> dict:
    root, ds, _ = HARBOR[bench]
    if not os.path.exists(harbor_bin()):
        raise RuntimeError("Harbor isn't installed: run `uv tool install harbor` (it drives the task containers)")
    os.makedirs(BENCH_DIR, exist_ok=True)
    tmp = root + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    p = await asyncio.create_subprocess_exec(
        harbor_bin(), "dataset", "download", ds, "-o", tmp,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env={**os.environ, "COLUMNS": "120"})
    out, _ = await asyncio.wait_for(p.communicate(), 900)
    if p.returncode != 0:
        raise RuntimeError("harbor dataset download failed: " + out.decode(errors="replace")[-400:])
    shutil.rmtree(root, ignore_errors=True)
    os.replace(tmp, root)
    _hb.pop(bench, None)
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
    try:  # IFEval per-instruction results (JSON); older DBs predate it
        await _db.execute("ALTER TABLE items ADD COLUMN extra TEXT")
    except aiosqlite.OperationalError:
        pass
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
    sub = {}
    cur = await _db.execute("SELECT extra FROM items WHERE run_id=? AND extra IS NOT NULL", (rid,))
    ex = [json.loads(r[0]) for r in await cur.fetchall()]
    if ex:  # IFEval: instruction-level + loose (lm-eval's metric definitions)
        ns = sum(len(e["strict"]) for e in ex)
        sub = {"inst_strict": round(100 * sum(sum(e["strict"]) for e in ex) / ns, 1) if ns else None,
               "inst_loose": round(100 * sum(sum(e["loose"]) for e in ex) / ns, 1) if ns else None,
               "prompt_loose": round(100 * sum(e["prompt_loose"] for e in ex) / len(ex), 1)}
    return {**sub, "done": done or 0, "correct": ok or 0, "scored": scored,
            "acc": round(100 * (ok or 0) / scored, 1) if scored else None,
            "no_answer": nopred or 0, "errors": err or 0, "truncated": cut or 0,
            "avg_tokens": round(tok) if tok else None, "secs": round(secs or 0)}


async def list_runs() -> list[dict]:
    cur = await _db.execute("SELECT * FROM runs ORDER BY created DESC")
    return [{**_row(r), **(await _stats(r[0]))} for r in await cur.fetchall()]


async def run_items(rid: str, full: bool = False) -> list[dict]:
    cur = await _db.execute(
        "SELECT idx,qid,gold,pred,correct,out_tokens,reasoning_chars,secs,finish,error,extra"
        + (",text" if full else "") + " FROM items WHERE run_id=? ORDER BY idx", (rid,))
    keys = ["idx", "qid", "gold", "pred", "correct", "out_tokens", "reasoning_chars",
            "secs", "finish", "error", "extra"] + (["text"] if full else [])
    out = [dict(zip(keys, r)) for r in await cur.fetchall()]
    for o in out:
        o["extra"] = json.loads(o["extra"]) if o["extra"] else None
    return out


async def item(rid: str, idx: int) -> dict | None:
    got = [x for x in await run_items(rid, full=True) if x["idx"] == idx]
    if not got:
        return None
    cur = await _db.execute("SELECT bench FROM runs WHERE id=?", (rid,))
    b = (await cur.fetchone() or ["gpqa_diamond"])[0]
    qs = bench_items(b)
    q = qs[idx] if idx < len(qs) else {}
    prompt = q.get("prompt") or ""
    if b in HARBOR and q:
        ins = os.path.join(HARBOR[b][0], q["dir"], "instruction.md")
        prompt = open(ins, encoding="utf-8").read() if os.path.exists(ins) else q["qid"]
    out = {**got[0], "prompt": prompt}
    if b in HARBOR:
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
    await _write("INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)", row)


def _ife_score(doc: dict, text: str) -> dict:
    from .ifeval import instructions_util, utils as ife
    instructions_util.download_nltk_resources(NLTK_DIR)  # path only; data is there
    return ife.process_results(doc, [text])


async def _one_ife(c: httpx.AsyncClient, rid: str, idx: int, q: dict, s: dict, p: dict) -> None:
    t0 = time.time()
    extra = None
    try:
        j = await _ask(c, s, q["prompt"], float(p["temperature"]))
        ch = j["choices"][0]
        msg = ch.get("message") or {}
        text = msg.get("content") or ""
        rc = msg.get("reasoning_content") or msg.get("reasoning") or ""
        if "</think>" in text:
            text = THINK_RE.sub("", text, count=1)
        r = await asyncio.to_thread(_ife_score, q["doc"], text)
        strict, loose = r["inst_level_strict_acc"], r["inst_level_loose_acc"]
        extra = json.dumps({"ids": q["doc"]["instruction_id_list"], "strict": strict, "loose": loose,
                            "prompt_loose": bool(r["prompt_level_loose_acc"])})
        row = (rid, idx, q["qid"], q["gold"], f"{sum(strict)}/{len(strict)}",
               int(bool(r["prompt_level_strict_acc"])), (j.get("usage") or {}).get("completion_tokens"),
               len(rc), round(time.time() - t0, 1), ch.get("finish_reason"), None, text[-TEXT_CAP:])
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        row = (rid, idx, q["qid"], q["gold"], "", 0, None, None, round(time.time() - t0, 1),
               None, f"{type(e).__name__}: {e}"[:400], "")
    await _write("INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (*row, extra))


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
            one = {"ifeval": _one_ife}.get(run["bench"], _one)
            if run["bench"] in HARBOR:
                if p.get("host"):
                    await _host_prep(p["host"], run["bench"])
                one = functools.partial(_one_harbor, bench=run["bench"])

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


# ---- Harbor benchmarks (Terminal-Bench, SWE-bench): on this box or an SSH host ----

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30"]
PIPE, STDOUT = asyncio.subprocess.PIPE, asyncio.subprocess.STDOUT


async def _sh(host: str, *argv: str, script: str = "", stdin: bytes | None = None,
              timeout: float = 600) -> tuple[int, str]:
    """argv here, or on `host` over ssh (argv quoted, or a raw shell `script`)."""
    cmd = list(argv) if not host else ["ssh", *SSH_OPTS, host, script or shlex.join(argv)]
    p = await asyncio.create_subprocess_exec(*cmd, stdin=PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                                             stdout=PIPE, stderr=STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "timed out"
    return p.returncode, out.decode(errors="replace")


async def _docker(host: str, *args: str) -> tuple[int, str]:
    return await _sh(host, "docker", *args)


async def _free_gb(host: str) -> float:
    if not host:
        return shutil.disk_usage(config.DATA_DIR).free / 2**30
    rc, out = await _sh(host, script="df -B1G --output=avail ~ | tail -1")
    return float(out.strip()) if rc == 0 and out.strip().isdigit() else 0.0


async def _host_prep(host: str, bench: str) -> None:
    """An SSH runner needs Harbor + Docker (+ x86_64 for SWE-bench) and the
    task definitions, which are downloaded there on first use."""
    root, ds, _ = HARBOR[bench]
    rdir = f"{REMOTE_ROOT}/{os.path.basename(root)}"
    rc, out = await _sh(host, timeout=900, script=(
        "test -x ~/.local/bin/harbor || { echo 'no ~/.local/bin/harbor (uv tool install harbor)'; exit 3; }; "
        "docker info >/dev/null 2>&1 || { echo 'docker not usable by this user'; exit 4; }; "
        "uname -m; "
        f"test -d {rdir} || COLUMNS=120 ~/.local/bin/harbor dataset download {ds} -o {rdir} >/dev/null 2>&1 "
        "|| { echo 'harbor dataset download failed'; exit 5; }"))
    if rc != 0:
        raise RuntimeError(f"runner {host}: {out.strip()[-300:]}")
    if bench == "swebench_verified" and out.split()[0] != "x86_64":
        raise RuntimeError(f"runner {host} is {out.split()[0]}: SWE-bench images are x86_64 only")


async def _cleanup_trial(host: str, job_dir: str) -> None:
    """Remove containers of this job's trial(s): a STOP / timeout kill can
    leave Harbor's compose project (named <trial>__env) running, and the
    image it built for the task (<trial>__env-main, GBs) behind."""
    _, ls = await _sh(host, "ls", job_dir)
    for t in [x for x in ls.split() if "__" in x]:
        _, ids = await _docker(host, "ps", "-aq", "--filter", f"label=com.docker.compose.project={t.lower()}__env")
        if ids.split():
            await _docker(host, "rm", "-f", *ids.split())
        await _docker(host, "rmi", "-f", f"{t.lower()}__env-main")


def _agent_cfg(name: str, s: dict, p: dict) -> dict:
    """Harbor agent entry for the provider's OpenAI-compatible endpoint with
    LangBang's sampling + thinking controls."""
    from . import agent
    url, eb, mt = s["base_url"].rstrip("/"), agent.extra_body(s), int(p["max_tokens"])
    if name == "mini-swe-agent":  # an inline config layers over mini.yaml; api_base goes inside it
        # _skip_mcp_handler: litellm ≥1.10x imports its MCP bridge (needs the `mcp`
        # package, which Harbor's install doesn't add) on any call with tools
        mk = {"api_base": url, "temperature": float(p["temperature"]), "_skip_mcp_handler": True,
              **({"extra_body": eb} if eb else {})}
        return {"name": name, "model_name": "openai/" + s["model"],
                "kwargs": {"max_tokens": mt, "config": {"model": {"model_kwargs": mk}}}}
    kw = {"temperature": float(p["temperature"]),
          "model_info": {"max_input_tokens": int(p.get("context") or 131072), "max_output_tokens": mt,
                         "input_cost_per_token": 0, "output_cost_per_token": 0},
          "llm_call_kwargs": {"max_tokens": mt, **({"extra_body": eb} if eb else {})}}
    return {"name": name, "model_name": "openai/" + s["model"], "base_url": url, "kwargs": kw}


PYTEST_SUM_RE = re.compile(r"(?m)^=+ .*\b(?:passed|failed|error)\b.* =+$")


def _trial_text(trial: str) -> str:
    """What the answer view shows: terminus-2's last terminal screen, or
    mini-swe-agent's closing message + the verdict lines of the test run.
    (Harbor scores the repo state, so there's no submitted patch to show.)"""
    pane = glob.glob(os.path.join(trial, "agent", "*.pane"))
    if pane:
        return open(pane[0], encoding="utf-8", errors="replace").read()[-TEXT_CAP:]
    out = ""
    try:
        t = json.load(open(os.path.join(trial, "agent", "mini-swe-agent.trajectory.json")))
        msgs = t.get("messages") or []
        last = next((m.get("content") for m in reversed(msgs) if m.get("role") == "assistant" and m.get("content")), "")
        steps = sum(m.get("role") == "assistant" for m in msgs)
        out = f"exit: {(t.get('info') or {}).get('exit_status') or '?'} · {steps} steps\n\n{str(last)[:3000]}"
    except (OSError, ValueError):
        pass
    tests = glob.glob(os.path.join(trial, "verifier", "test-stdout.txt"))
    if tests:
        log = open(tests[0], encoding="utf-8", errors="replace").read()
        verdict = PYTEST_SUM_RE.findall(log)[-1:] + re.findall(r"(?s)SWEBench results starts here\s*(.*?)\s*SWEBench results ends here", log)[-1:]
        out += "\n\n---- tests ----\n" + ("\n".join(verdict) or log[-1500:])
    return out[-TEXT_CAP:]


async def _one_harbor(c, rid: str, idx: int, q: dict, s: dict, p: dict, bench: str) -> None:  # noqa: ANN001
    """One task = one Harbor job, here or on p.host. Remote: config (and the
    API key, via stdin) go over ssh, the trial dir is rsynced back after."""
    t0 = time.time()
    host, root = p.get("host") or "", HARBOR[bench][0]
    job_dir = os.path.join(JOBS_DIR, rid, q["qid"])  # local copy, parsed below
    if host:
        rjob = f"{REMOTE_ROOT}/jobs/{rid}/{q['qid']}"
        task = f"{REMOTE_ROOT}/{os.path.basename(root)}/{q['dir']}"
    else:
        rjob, task = job_dir, os.path.join(root, q["dir"])
    cfg = {"jobs_dir": os.path.dirname(rjob), "job_name": q["qid"], "n_concurrent_trials": 1,
           "agents": [_agent_cfg(p.get("agent") or HARBOR[bench][2], s, p)], "tasks": [{"path": task}]}
    shutil.rmtree(job_dir, ignore_errors=True)  # a resumed task starts clean
    os.makedirs(os.path.dirname(job_dir), exist_ok=True)
    had_image = bool(q["image"]) and (await _docker(host, "image", "inspect", q["image"]))[0] == 0
    key = s.get("api_key") or "none"
    row = proc = None
    try:
        free = await _free_gb(host)
        if free < MIN_FREE_GB:
            raise RuntimeError(f"only {free:.1f} GB free{' on ' + host if host else ''} (need {MIN_FREE_GB}) — task not started")
        env = {**os.environ, "COLUMNS": "120"}
        if host:
            argv, stdin = ["ssh", *SSH_OPTS, host, (
                f"cd ~ && rm -rf {rjob} && mkdir -p {os.path.dirname(rjob)} && read -r OPENAI_API_KEY && "
                f"export OPENAI_API_KEY COLUMNS=120 && cat > {rjob}.json && "
                f"{{ setsid ~/.local/bin/harbor run -c {rjob}.json -q </dev/null >{rjob}.log 2>&1 & "
                f"echo $! > {rjob}.pid; wait $!; }}; tail -c 400 {rjob}.log")], (key + "\n" + json.dumps(cfg)).encode()
        else:
            open(job_dir + ".json", "w").write(json.dumps(cfg))
            argv, stdin = [harbor_bin(), "run", "-c", job_dir + ".json", "-q"], None
            env["OPENAI_API_KEY"] = key
        proc = await asyncio.create_subprocess_exec(*argv, stdin=PIPE if stdin else None, stdout=PIPE,
                                                    stderr=STDOUT, start_new_session=True, env=env)
        # task timeout (agent + verifier + image pull) with headroom
        out, _ = await asyncio.wait_for(proc.communicate(stdin), q["timeout"] * 2.5 + 900)
        if host:
            rc, msg = await _sh("", "rsync", "-a", "-e", shlex.join(["ssh", *SSH_OPTS]),
                                f"{host}:{rjob}/", job_dir + "/", timeout=300)
            if rc != 0:
                raise RuntimeError(f"rsync from {host} failed: {msg[-200:]}")
        res = glob.glob(os.path.join(job_dir, "*__*", "result.json"))
        if not res:
            raise RuntimeError("harbor produced no trial result: " + out.decode(errors="replace")[-300:])
        t = json.load(open(res[0]))
        reward = ((t.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        ar = t.get("agent_result") or {}
        exc = t.get("exception_info") or None
        ok = reward is not None and float(reward) >= 1.0
        etype = (exc or {}).get("exception_type") if isinstance(exc, dict) else (str(exc)[:60] if exc else None)
        row = (rid, idx, q["qid"], q["gold"], "pass" if ok else "fail", int(ok),
               ar.get("n_output_tokens"), ar.get("n_input_tokens"), round(time.time() - t0, 1),
               etype or "done", None, _trial_text(os.path.dirname(res[0])))
    except asyncio.CancelledError:
        if proc and proc.returncode is None:
            if host:  # the remote harbor is its own process group (setsid)
                await _sh(host, script=f"kill -INT -- -$(cat {rjob}.pid) 2>/dev/null", timeout=30)
            else:
                os.killpg(proc.pid, signal.SIGINT)  # let Harbor tear its compose project down
            try:
                await asyncio.wait_for(proc.wait(), 30)
            except asyncio.TimeoutError:
                if host:
                    await _sh(host, script=f"kill -KILL -- -$(cat {rjob}.pid) 2>/dev/null", timeout=30)
                os.killpg(proc.pid, signal.SIGKILL)
        await _cleanup_trial(host, rjob)
        raise
    except Exception as e:  # noqa: BLE001 - one broken task never kills the run
        if proc and proc.returncode is None:
            if host:
                await _sh(host, script=f"kill -KILL -- -$(cat {rjob}.pid) 2>/dev/null", timeout=30)
            os.killpg(proc.pid, signal.SIGKILL)
        await _cleanup_trial(host, rjob)
        row = (rid, idx, q["qid"], q["gold"], "", 0, None, None, round(time.time() - t0, 1),
               None, f"{type(e).__name__}: {e}"[:400], "")
    finally:
        if q["image"] and not had_image:  # disk: drop images this run pulled
            await _docker(host, "rmi", "-f", q["image"])
        if host:
            await _sh(host, script=f"rm -rf {rjob} {rjob}.json {rjob}.pid {rjob}.log; "
                                   f"rmdir {os.path.dirname(rjob)} 2>/dev/null", timeout=60)
    await _write("INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)", row)


def _spawn(rid: str) -> None:
    _tasks[rid] = asyncio.get_running_loop().create_task(_run(rid))


async def start(bench: str, provider: str, model: str, params: dict) -> dict:
    if bench not in BENCHES:
        raise ValueError(f"unknown benchmark {bench!r}")
    qs = bench_items(bench)
    if not qs:
        raise ValueError("dataset not downloaded")
    p = {**DEFAULTS, **{k: v for k, v in (params or {}).items() if k in DEFAULTS or k == "effort"}}
    if bench in HARBOR:
        p["host"] = str(p.get("host") or "").strip()
        if p["host"] and not HOST_RE.match(p["host"]):
            raise ValueError(f"bad runner host {p['host']!r}")
        p["agent"] = p.get("agent") or HARBOR[bench][2]
        if p["agent"] not in AGENTS:
            raise ValueError(f"unknown agent {p['agent']!r} (have: {', '.join(AGENTS)})")
        ds = dataset_status()[bench]
        if not p["host"] and not (ds["harbor"] and ds["docker"]):
            raise ValueError(f"{BENCHES[bench]} needs Harbor + Docker on this box, or a runner host")
        if not p["host"] and bench == "swebench_verified" and platform.machine() != "x86_64":
            raise ValueError("SWE-bench images are x86_64: pick an x86_64 runner host")
    else:
        p.pop("agent", None), p.pop("host", None)
    s = config.load()
    if provider not in s["providers"]:
        raise ValueError(f"unknown provider {provider!r}")
    model = model or config.effective(s, {"provider": provider})["model"]
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

"""A/B: keep_reasoning off vs turn on 4 read-only multi-step tasks (thinking on)."""
import asyncio, json, re, sqlite3, sys, time, copy
import httpx
sys.path.insert(0, "/home/tyrel/projects/LangBang")
B = "http://127.0.0.1:8123/api"
REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 4
PAR = 2
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/ab_results.json"
SUFFIX = ("\n\nWork it out with tools — don't guess or estimate, and don't modify any files. "
          "End your reply with one line that starts with `ANSWER:` in exactly the format given.")
TASKS = {
 "routes": ("In /home/tyrel/projects/LangBang/server/main.py, find every FastAPI route decorator "
            "(@app.get/@app.post/@app.put/@app.patch/@app.delete at the start of a line) whose path starts with "
            "/api/threads. Count them in total and per HTTP method.\n"
            "Format: ANSWER: total=<n> GET=<n> POST=<n> PUT=<n> PATCH=<n> DELETE=<n>",
            {"total=20": r"total\s*=\s*20\b", "GET=8": r"GET\s*=\s*8\b", "POST=8": r"POST\s*=\s*8\b",
             "PUT=2": r"PUT\s*=\s*2\b", "PATCH=1": r"PATCH\s*=\s*1\b", "DELETE=1": r"DELETE\s*=\s*1\b"}),
 "filestats": ("For /home/tyrel/projects/LangBang: (a) how many lines does web/app.js have? (b) which three .py files "
               "directly in server/ have the most lines, and how many each? (c) divide (a) by the sum of the three "
               "counts in (b), rounded to 2 decimals.\n"
               "Format: ANSWER: app.js=<n> | <file>=<n>, <file>=<n>, <file>=<n> | ratio=<x.xx>",
               {"app.js": r"app\.js\s*=\s*4682\b", "agent.py": r"agent\.py\s*=\s*2379\b",
                "main.py": r"main\.py\s*=\s*1261\b", "learning.py": r"learning\.py\s*=\s*549\b",
                "ratio": r"ratio\s*=\s*1\.12\b"}),
 "git": ("In the git repo /home/tyrel/projects/LangBang, find the commit that first ADDED the file server/ttsnorm.py. "
         "Report its short hash (7 chars), its commit date (YYYY-MM-DD), how many lines the file had in that commit, "
         "and how many lines it has now in the working tree.\n"
         "Format: ANSWER: commit=<hash> date=<YYYY-MM-DD> lines_then=<n> lines_now=<n>",
         {"hash": r"commit\s*=\s*7f0ce07", "date": r"date\s*=\s*2026-10-06", "then": r"lines_then\s*=\s*356\b",
          "now": r"lines_now\s*=\s*449\b"}),
 "churn": ("In the git repo /home/tyrel/projects/LangBang, consider the commits made between 2026-10-01 00:00 and "
           "2026-10-06 23:59 local time (America/Denver). How many commits are there? Which file was changed in the "
           "most of those commits, in how many, and how many lines were inserted and deleted in that file across them?\n"
           "Format: ANSWER: commits=<n> top=<path> touched=<n> ins=<n> del=<n>",
           {"commits": r"commits\s*=\s*26\b", "top": r"top\s*=\s*\S*web/app\.js", "touched": r"touched\s*=\s*15\b",
            "ins": r"ins\s*=\s*1204\b", "del": r"del\s*=\s*65\b"}),
 "hosts": ("On the two DGX Sparks spark-da36 and spark-ee93 (use ssh -o BatchMode=yes), find each host's total RAM in "
           "GiB as `free -g` reports it, its kernel release (uname -r), and how many docker containers are running. "
           "Then give the total number of running containers across both.\n"
           "Format: ANSWER: da36 ram=<n> kernel=<k> containers=<n>; ee93 ram=<n> kernel=<k> containers=<n>; total=<n>",
           None),  # graded against a live re-check (state can drift)
}

def live_hosts():
    import subprocess
    g = {}
    for h in ("spark-da36", "spark-ee93"):
        o = subprocess.run(["ssh", "-o", "BatchMode=yes", h, "echo $(free -g | awk '/Mem:/{print $2}') $(uname -r) $(docker ps -q | wc -l)"],
                           capture_output=True, text=True, timeout=30).stdout.split()
        g[h] = o
    return g

async def run_one(c, task, arm, rep, results):
    prompt, _ = TASKS[task]
    t = (await c.post(B + "/threads", json={"title": f"lbtest ab {task} {arm} {rep}"})).json()["id"]
    await c.put(f"{B}/threads/{t}/model", json={"keep_reasoning": arm})
    t0 = time.time(); err = None
    for attempt in range(30):  # TooMany (run cap) / transient → retry
        try:
            async with c.stream("POST", B + "/chat", json={"thread_id": t, "text": prompt + SUFFIX}) as r:
                if r.status_code == 429 or r.status_code == 409:
                    await asyncio.sleep(20); continue
                async for _ in r.aiter_lines():
                    pass
            break
        except Exception as e:  # noqa: BLE001
            err = repr(e); await asyncio.sleep(10)
    # wait for the detached hub to finish (stream end == done, but be safe)
    for _ in range(600):
        runs = (await c.get(B + "/runs")).json()["runs"]
        if t not in runs: break
        await asyncio.sleep(2)
    results.append({"task": task, "arm": arm, "rep": rep, "tid": t, "wall": round(time.time() - t0, 1), "client_err": err})
    print(f"done {task:9} {arm:4} #{rep} {time.time()-t0:6.0f}s", flush=True)

async def main():
    async with httpx.AsyncClient(timeout=1800) as c:
        jobs = [(task, arm, rep) for rep in range(REPS) for task in TASKS for arm in ("off", "turn")]
        results, sem = [], asyncio.Semaphore(PAR)
        async def guarded(j):
            async with sem:
                await run_one(c, *j, results)
        await asyncio.gather(*(guarded(j) for j in jobs))
    json.dump(results, open(OUT, "w"), indent=1)
    print("runs written:", OUT)

asyncio.run(main())

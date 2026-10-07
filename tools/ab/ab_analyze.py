import asyncio, json, re, sqlite3, sys, statistics as st, collections
sys.path.insert(0, "/home/tyrel/projects/LangBang")
from langchain_core.messages import AIMessage, ToolMessage, HumanMessage
from server import agent
TASKS = eval(open("/home/tyrel/projects/LangBang/tools/ab/ab_reasoning.py").read().split("TASKS = ")[1].split("\n}\n")[0] + "\n}")
files = sys.argv[1:-1] if len(sys.argv) > 2 else [sys.argv[1]]
runs = sum((json.load(open(f)) for f in files), [])
hosts = None
def grade(task, text):
    m = re.findall(r"ANSWER:\s*(.+)", text or "")
    ans = m[-1] if m else ""
    checks = TASKS[task][1]
    if checks is None:
        global hosts
        if hosts is None:
            import subprocess
            hosts = {}
            for h in ("spark-da36", "spark-ee93"):
                hosts[h] = subprocess.run(["ssh","-o","BatchMode=yes",h,"echo $(free -g | awk '/Mem:/{print $2}') $(uname -r) $(docker ps -q | wc -l)"],capture_output=True,text=True,timeout=30).stdout.split()
        d, e = hosts["spark-da36"], hosts["spark-ee93"]
        checks = {"da36": rf"da36\s+ram\s*=\s*{d[0]}\b.*?kernel\s*=\s*{re.escape(d[1])}.*?containers\s*=\s*{d[2]}\b",
                  "ee93": rf"ee93\s+ram\s*=\s*{e[0]}\b.*?kernel\s*=\s*{re.escape(e[1])}.*?containers\s*=\s*{e[2]}\b",
                  "total": rf"total\s*=\s*{int(d[2]) + int(e[2])}\b"}
    ok = {k: bool(re.search(p, ans)) for k, p in checks.items()}
    return sum(ok.values()) / len(ok), ok, ans[:160]
async def main():
    await agent.init()
    db = sqlite3.connect("/home/tyrel/projects/LangBang/data/events.db")
    rows = []
    for r in runs:
        ev = db.execute("select type, tok_in, tok_out, dur, ts, meta from run_events where thread_id=?", (r["tid"],)).fetchall()
        models = [e for e in ev if e[0] == "model"]; tools = [e for e in ev if e[0] == "tool"]
        errs = [e for e in ev if e[0] == "error"]
        msgs = await agent._live_messages(r["tid"])
        final = next((m for m in reversed(msgs) if isinstance(m, AIMessage) and not m.tool_calls), None)
        text = agent._text_only(final.content) if final else ""
        score, ok, ans = grade(r["task"], text)
        calls = [(tc["name"], json.dumps(tc["args"], sort_keys=True)) for m in msgs if isinstance(m, AIMessage) for tc in (m.tool_calls or [])]
        dups = sum(n - 1 for n in collections.Counter(calls).values() if n > 1)
        terr = sum(1 for m in msgs if isinstance(m, ToolMessage) and getattr(m, "status", None) == "error")
        tin = [e[1] for e in models if e[1]]
        rows.append({**r, "score": score, "ok": ok, "answer": ans, "model_calls": len(models), "tool_calls": len(calls),
                     "tool_errors": terr, "run_errors": len(errs), "dup_calls": dups, "prompt_tok_total": sum(tin),
                     "prompt_tok_max": max(tin) if tin else 0, "out_tok": sum(e[2] or 0 for e in models),
                     "llm_s": round(sum(e[3] or 0 for e in models), 1)})
    await agent.shutdown()
    json.dump(rows, open(sys.argv[-1], "w"), indent=1)
    def agg(sel, k): 
        v = [x[k] for x in sel]; return st.mean(v) if v else float("nan")
    print(f"{'task':10} {'arm':5} {'n':>2} {'score':>6} {'perfect':>7} {'steps':>6} {'tools':>6} {'t-err':>6} {'dups':>5} {'maxctx':>7} {'Σprompt':>8} {'out':>6} {'wall':>6}")
    for task in list(TASKS) + ["ALL"]:
        for arm in ("off", "turn"):
            sel = [x for x in rows if x["arm"] == arm and (task == "ALL" or x["task"] == task)]
            if not sel: continue
            print(f"{task:10} {arm:5} {len(sel):>2} {agg(sel,'score'):6.2f} {sum(x['score']==1 for x in sel):>4}/{len(sel):<2} {agg(sel,'model_calls'):6.1f} {agg(sel,'tool_calls'):6.1f} {agg(sel,'tool_errors'):6.2f} {agg(sel,'dup_calls'):5.2f} {agg(sel,'prompt_tok_max'):7.0f} {agg(sel,'prompt_tok_total'):8.0f} {agg(sel,'out_tok'):6.0f} {agg(sel,'wall'):6.0f}")
    print("live hosts at grading:", hosts)
asyncio.run(main())

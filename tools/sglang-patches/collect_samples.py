import glob, json, os, re
D = os.path.expanduser("~/.local/share/sglang-qwen38-da36/requests")
CALL = re.compile(r"<tool_call>(.*?)(?:</tool_call>|$)", re.S)
bad, ok = [], []
for f in sorted(glob.glob(D + "/*.log*")):
    with open(f, "rb") as fh:
        for raw in fh:
            if b'"request.finished"' not in raw or b"<tool_call>" not in raw: continue
            line = raw.decode("utf-8", "replace"); j = json.loads(line[line.index("{"):])
            post = ((j.get("out") or {}).get("text") or "").rsplit("</think>", 1)[-1]
            for body in CALL.findall(post):
                m = re.match(r"\s*<function=([^>\n]*)>", body)
                if not m: continue
                name = m.group(1); inner = body[m.end():]
                lead = inner.split("<parameter=", 1)[0].strip()
                rec = {"ts": j["timestamp"], "tool": name, "raw": "<tool_call>" + body + "</tool_call>"}
                if name == "write_todos" and (lead or "<parameter=[" in inner): bad.append(rec)
                elif name in ("write_todos", "ask_user", "run_bash", "read_file") and len([x for x in ok if x["tool"] == name]) < 15: ok.append(rec)
json.dump({"bad": bad, "ok": ok}, open("/tmp/toolcall_samples.json", "w"))
print("bad", len(bad), "ok", {t: sum(1 for x in ok if x["tool"] == t) for t in ("write_todos", "ask_user", "run_bash", "read_file")})

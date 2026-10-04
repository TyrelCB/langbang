import importlib.util, json, random
from sglang.srt.entrypoints.openai.protocol import Tool, Function
def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
ORIG = load("/tmp/qwen3_coder_detector.orig.py", "det_orig").Qwen3CoderDetector
PATCH = load("/tmp/qwen3_coder_detector.patched.py", "det_patch").Qwen3CoderDetector
item = {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string"}}}
TOOLS = [Tool(type="function", function=Function(name=n, parameters=p)) for n, p in [
    ("write_todos", {"type": "object", "properties": {"todos": {"type": "array", "items": item}}, "required": ["todos"]}),
    ("ask_user", {"type": "object", "properties": {"questions": {"type": "array"}}, "required": ["questions"]}),
    ("run_bash", {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}}),
    ("read_file", {"type": "object", "properties": {"file_path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}}),
]]
def stream(cls, text, sizes):
    d = cls(); calls = {}; i = 0
    while i < len(text):
        n = sizes(); r = d.parse_streaming_increment(text[i:i + n], TOOLS); i += n
        for c in r.calls:
            e = calls.setdefault(c.tool_index, {"name": None, "args": ""})
            if c.name: e["name"] = c.name
            e["args"] += c.parameters or ""
    return [(e["name"], json.loads(e["args"] or "{}")) for _, e in sorted(calls.items())]
def oneshot(cls, text):
    return [(c.name, json.loads(c.parameters)) for c in cls().detect_and_parse(text, TOOLS).calls]
S = json.load(open("/tmp/toolcall_samples.json")); rnd = random.Random(7); fails = []
print("== MALFORMED (stock → patched)")
for s in S["bad"]:
    t = s["raw"]; so, po = oneshot(ORIG, t), oneshot(PATCH, t)
    ss = stream(ORIG, t, lambda: rnd.randint(1, 12)); ps = [stream(PATCH, t, lambda: rnd.randint(1, 12)) for _ in range(20)] + [stream(PATCH, t, lambda: len(t))]
    good = all(p and p[0][0] == "write_todos" and isinstance(p[0][1].get("todos"), list) and p[0][1]["todos"] for p in ps + [po])
    if not good: fails.append(("bad", s["ts"]))
    print(f"  {s['ts']}  stock stream={ss[0][1] if ss else None!s:12.12}  stock oneshot={so[0][1] if so else None!s:12.12}  patched → todos×{len(ps[0][0][1].get('todos', [])) if ps[0] else 0}  all-21-chunkings-ok={good}")
print("== WELL-FORMED (patched must equal stock, streaming + oneshot)")
for s in S["ok"]:
    t = s["raw"]; ref_s = stream(ORIG, t, lambda: len(t)); ref_o = oneshot(ORIG, t)
    same = oneshot(PATCH, t) == ref_o and all(stream(PATCH, t, lambda: rnd.randint(1, 12)) == ref_s for _ in range(20))
    if not same: fails.append(("ok", s["tool"], s["ts"]))
print(f"  {len(S['ok'])} samples ({', '.join(sorted(set(x['tool'] for x in S['ok'])))}): identical={len(S['ok']) - sum(1 for f in fails if f[0] == 'ok')}/{len(S['ok'])}")
print("FAILS:", fails or "none")

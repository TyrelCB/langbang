"""Scan sglang request logs: classify every raw tool call the model emitted."""
import collections, glob, json, os, re, sys
D = os.path.expanduser("~/.local/share/sglang-qwen38-da36/requests")
CALL = re.compile(r"<tool_call>(.*?)(?:</tool_call>|$)", re.S)
FUNC = re.compile(r"^\s*<function=([^>\n]*)>(.*?)(?:</function>\s*)?$", re.S)
PARAM = re.compile(r"<parameter=([^>\n]*)>\n?(.*?)\n?</parameter>", re.S)
def classify(body):
    m = FUNC.match(body)
    if not m: return "?", "NO_FUNCTION_TAG", {}
    name, inner = m.group(1).strip(), m.group(2)
    opens = re.findall(r"<parameter=([^>\n]*)>", inner)
    closes = inner.count("</parameter>")
    lead = inner.split("<parameter=", 1)[0].strip()
    params = {k: v for k, v in PARAM.findall(inner)}
    if any(o.strip()[:1] in "[{" for o in opens):              kind = "VALUE_IN_PARAM_NAME"
    elif lead and closes > len(opens):                         kind = "MISSING_PARAM_OPEN_TAG"
    elif lead and not opens:                                   kind = "BARE_VALUE_NO_TAGS"
    elif closes != len(opens):                                 kind = "UNBALANCED_PARAM_TAGS"
    elif lead:                                                 kind = "STRAY_TEXT_BEFORE_PARAMS"
    else:                                                      kind = "OK"
    return name, kind, params
stats = collections.Counter(); by_tool = collections.defaultdict(collections.Counter)
samples = collections.defaultdict(list); ok_samples = []
for f in sorted(glob.glob(D + "/*.log*")):
    with open(f, "rb") as fh:
        for raw in fh:
            if b'"request.finished"' not in raw or b"<tool_call>" not in raw: continue
            line = raw.decode("utf-8", "replace")
            try: j = json.loads(line[line.index("{"):])
            except Exception: stats["unparsable_line"] += 1; continue
            text = ((j.get("out") or {}).get("text")) or ""
            post = text.rsplit("</think>", 1)[-1]
            for body in CALL.findall(post):
                name, kind, params = classify(body)
                stats[kind] += 1; by_tool[name][kind] += 1
                if kind != "OK" and len(samples[kind]) < 6:
                    samples[kind].append({"ts": j.get("timestamp"), "tool": name, "raw": body[:600]})
                if kind == "OK" and name in ("write_todos", "ask_user") and len(ok_samples) < 40:
                    ok_samples.append({"ts": j.get("timestamp"), "tool": name, "raw": "<tool_call>" + body + "</tool_call>"})
            # thinking-side tool calls (inside <think>) never reach the parser's tool path
            pre = text.rsplit("</think>", 1)[0] if "</think>" in text else ""
            if "<tool_call>" in pre: stats["tool_call_inside_think"] += 1
json.dump({"stats": stats, "by_tool": by_tool, "samples": samples}, sys.stdout, indent=1, default=dict)
json.dump(ok_samples, open("/tmp/ok_toolcall_samples.json", "w"))

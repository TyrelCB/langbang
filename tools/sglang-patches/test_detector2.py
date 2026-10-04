exec(open("/tmp/test_detector.py").read().split("S = json.load")[0])
import random; rnd = random.Random(3)
cases = {
 "value-in-param-name (write_todos)": '<tool_call>\n<function=write_todos>\n<parameter=[{"content": "Pull BBC World headlines", "status": "completed"}, {"content": "Retry Reuters", "status": "in_progress"}]\n</parameter>\n</function>\n</tool_call>',
 "bare value, no </parameter> (write_todos)": '<tool_call>\n<function=write_todos>\n[{"content": "a", "status": "pending"}]\n</function>\n</tool_call>',
 "bare value on MULTI-param tool (run_bash) — must NOT guess": '<tool_call>\n<function=run_bash>\n{"command": "ls"}\n</parameter>\n</function>\n</tool_call>',
}
for label, t in cases.items():
    so = oneshot(ORIG, t); po = oneshot(PATCH, t)
    ps = {json.dumps(stream(PATCH, t, lambda: rnd.randint(1, 9))) for _ in range(20)}
    print(f"{label}\n   stock={so}\n   patched oneshot={po}\n   patched streaming ({len(ps)} distinct over 20 chunkings)={list(ps)[0][:160]}")

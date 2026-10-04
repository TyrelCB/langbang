# sglang qwen3_coder parser patch — tag-less single-parameter values

**Problem (proven 2026-10-04 from sglang request logs, level 3):** Qwen3.8-Flash-Next
sometimes skips the parameter tag when a tool takes exactly ONE parameter:

    <function=write_todos>
    [{"content": "…", "status": "in_progress"}, …]
    </parameter>
    </function>

(rarer: `<parameter=[{…}]\n</parameter>`). The stock `Qwen3CoderDetector` discards the
bare value as stray text inside the tool call and emits `{}`, so LangBang saw
`write_todos {}` → pydantic "todos Field required". Across the logged window:
write_todos 5/32 malformed, every other tool 0/364; LangBang's recorded failures
matched those 5 timestamps 1:1 — no well-formed call was ever lost (no parser bug in
the normal path).

**Patch:** when the function's schema has exactly one parameter and none was parsed
yet, a bare JSON array/object (or a `<parameter=` whose "name" is the value) becomes
that parameter. Streaming + non-streaming. Multi-parameter tools are never guessed.

**Verified inside the container (separate process, live server untouched):** the 5
real failures recover their full lists under 21 random chunkings each; 47 well-formed
samples (write_todos / ask_user / run_bash / read_file) give byte-identical results
to the stock parser; edge cases in `test_detector2.py`.

Files: `qwen3_coder_detector.{orig,patched}.py`, `qwen3_coder_tagless_param.patch`,
`scan_toolcalls.py` (classify raw tool calls in the request logs),
`collect_samples.py` + `test_detector.py` / `test_detector2.py` (replay test).

**Deploy** (restarts sglang — interrupts every client for the model load):

    scp qwen3_coder_detector.patched.py spark-da36:/tmp/
    ssh spark-da36 'docker cp /tmp/qwen3_coder_detector.patched.py \
      sglang-qwen38-da36:/sgl-workspace/sglang/python/sglang/srt/function_call/qwen3_coder_detector.py \
      && docker restart sglang-qwen38-da36'

Lives in the container's writable layer: survives restarts, NOT a container
re-create (re-apply, or bind-mount the file in the `docker run`). Revert: same
command with `qwen3_coder_detector.orig.py`. The patch logs
`[langbang patch] recovered tag-less …` when it fires.

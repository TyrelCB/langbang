#!/usr/bin/env python3
"""ivr_report.py — email a finished call: recording + transcript.

Runs after ivr_loop.py returns (invoked by ivr_answer.py). Takes the call's
recording (m4a, caller left / agent right), pulls that call's turn events
from LangBang's data/ivr/calls.jsonl on fedora, builds a plain-text
transcript, uploads the audio to fedora, and sends everything to Tyrel via
gmail-mcp (which reads attachments from fedora's disk).

Hub mode (IVR_REPORT_HUB set, e.g. on the Mac mini, which has no ssh to
fedora): upload the three files to LangBang's /api/ivr/node/recording and ask
/api/ivr/node/report to build the same transcript and send the email there.

Usage: ivr_report.py <call_id> <audio_path>
Env: IVR_REPORT_TO (default tyrelcb@gmail.com; ssh mode — the hub has its own),
     IVR_REPORT_HUB (LangBang base URL: hub mode), IVR_TOKEN_FILE, IVR_NODE_NAME
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

TO = os.environ.get("IVR_REPORT_TO", "tyrelcb@gmail.com")
HUB = os.environ.get("IVR_REPORT_HUB", "").rstrip("/")
TOKEN_FILE = os.environ.get("IVR_TOKEN_FILE",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ivr_token"))
REMOTE_DIR = "/home/tyrel/ivr-recordings"


def ssh(cmd: str, timeout: int = 60) -> str:
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "fedora", cmd],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"ssh failed ({r.returncode}): {r.stderr[:200]}")
    return r.stdout


def turns_for(call_id: str) -> list:
    """Turn events for this call from calls.jsonl (newest file tail)."""
    out = ssh(f"grep -F '{call_id}' {REMOTE_DIR}/../projects/LangBang/data/ivr/calls.jsonl "
              f"2>/dev/null | tail -80")
    ev = []
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("call_id") == call_id and d.get("event") == "turn":
            ev.append(d)
    return ev


def stt_words(remote_wav: str) -> list:
    """Whisper word timings for a full-leg wav already on fedora, via
    /api/stt (stt_provider=whisper gives [{w,s,e}] on the file's own
    timeline). Empty list if the provider degrades to sr."""
    out = ssh(f"curl -s --max-time 120 -X POST http://127.0.0.1:8123/api/stt "
              f"--data-binary @{remote_wav}")
    try:
        return json.loads(out).get("words") or []
    except ValueError:
        return []


def build_transcript(call_id: str, ev: list, audio_path: str,
                     agent_words: list) -> str:
    lines = [f"IVR call report — call {call_id}",
             f"when: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
             f"recording: {os.path.basename(audio_path)} "
             f"(stereo: caller L / agent R; both legs also attached as mono wavs)", "",
             "--- conversation (caller words are utterance-relative) ---"]
    for d in ev:
        if d.get("transcript"):
            w = d.get("words") or []
            span = f" [{w[0]['s']:.1f}-{w[-1]['e']:.1f}s]" if w else ""
            lines.append(f"CALLER{span}: {d['transcript']}")
            if w:
                lines.append("    words: " + " ".join(
                    f"{x['w']}@{x['s']:.1f}" for x in w[:40]))
        if d.get("reply"):
            lines.append(f"AGENT (intended):  {d['reply']}")
        if d.get("tools"):
            lines.append(f"TOOLS:  {', '.join(d['tools'])}")
    if len(lines) <= 5:
        lines.append("(no transcribed turns — STT returned nothing this call)")
    if agent_words:
        # What the caller ACTUALLY heard: whisper over the recorded agent leg,
        # absolute call time. An intended sentence missing here = the reply
        # was cut off (barge-in or TTS truncation).
        lines.append("")
        lines.append("--- agent speech as recorded (whisper, call-time offsets) ---")
        chunk, last_e = [], 0.0
        for x in agent_words:
            if chunk and (x["s"] - last_e > 0.8 or len(chunk) >= 12):
                lines.append(f"AGENT [{chunk[0]['s']:6.1f}-{last_e:6.1f}s] "
                             + " ".join(c["w"] for c in chunk))
                chunk = []
            chunk.append(x)
            last_e = x["e"]
        if chunk:
            lines.append(f"AGENT [{chunk[0]['s']:6.1f}-{last_e:6.1f}s] "
                         + " ".join(c["w"] for c in chunk))
    return "\n".join(lines)


def send_email(subject: str, body: str, remote_files: list) -> str:
    args = {"to": TO, "subject": subject, "body": body,
            "attachment_paths": remote_files}
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "send_email_with_attachments",
                                     "arguments": args}})
    q = urllib.parse.quote(payload, safe="")
    # run curl ON fedora: the MCP server is 127.0.0.1:4000 there and reads
    # attachment paths from fedora's disk. Pass the JSON via a temp file to
    # dodge quoting.
    ssh(f"cat > /tmp/ivr_mail.json <<'EOF'\n{payload}\nEOF")
    out = ssh("curl -s --max-time 90 -X POST http://127.0.0.1:4000/mcp "
              "-H 'Content-Type: application/json' "
              "-H 'Accept: application/json, text/event-stream' "
              "--data @/tmp/ivr_mail.json")
    return out[:300]


def via_hub(cid: str, audio: str) -> int:
    """Upload the recordings to the hub and have it send the report."""
    tok = open(TOKEN_FILE).read().strip()

    def post(path: str, data: bytes, ctype: str, timeout: int = 180) -> dict:
        req = urllib.request.Request(HUB + path, data=data, headers={
            "Authorization": "Bearer " + tok, "Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{cid}.m4a"
    try:
        for leg, path in (("mix", audio), ("caller", f"{audio}.caller.wav"), ("agent", f"{audio}.agent.wav")):
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    post(f"/api/ivr/node/recording?stem={urllib.parse.quote(stem)}&leg={leg}",
                         fh.read(), "application/octet-stream")
        time.sleep(3)  # the node pushes its last call-log rows within a second or two
        resp = post("/api/ivr/node/report", json.dumps(
            {"call_id": cid, "stem": stem, "node": os.environ.get("IVR_NODE_NAME", "")}).encode(),
            "application/json")
    except Exception as e:  # noqa: BLE001
        print(f"report failed: {e.__class__.__name__}: {e}")
        return 1
    print("email resp:", resp.get("resp"))
    return 0 if resp.get("ok") else 1


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: ivr_report.py <call_id> <audio_path>")
        return 2
    cid, audio = sys.argv[1], sys.argv[2]
    if HUB:
        return via_hub(cid, audio)
    ev = turns_for(cid)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{cid}.m4a"
    remote = f"{REMOTE_DIR}/{name}"
    # three attachments: stereo mix + each leg alone (mono wavs the
    # Recorder left beside the m4a). Missing legs degrade to fewer files
    # rather than a failed report.
    files = [(audio, remote)]
    for leg in ("caller", "agent"):
        part = f"{audio}.{leg}.wav"
        if os.path.exists(part):
            files.append((part, f"{REMOTE_DIR}/{name}.{leg}.wav"))
    try:
        ssh(f"mkdir -p {REMOTE_DIR}")
        for local, rem in files:
            subprocess.run(["scp", "-o", "BatchMode=yes", local, f"fedora:{rem}"],
                           check=True, capture_output=True, timeout=120)
        # whisper the recorded agent leg server-side: shows what was ACTUALLY
        # spoken (cutoffs visible); never fails the report if it dies
        agent_words = []
        agent_remote = next((r for l, r in files if l.endswith(".agent.wav")), None)
        if agent_remote:
            try:
                agent_words = stt_words(agent_remote)
            except Exception:
                pass
        body = build_transcript(cid, ev, audio, agent_words)
        resp = send_email(f"IVR call recording: {cid}", body,
                          [rem for _, rem in files])
        print("email resp:", resp)
        if "Message ID" in resp:
            return 0
        return 1
    except Exception as e:
        print(f"report failed: {e.__class__.__name__}: {e}")
        # keep the evidence locally even if email died
        return 1


if __name__ == "__main__":
    sys.exit(main())

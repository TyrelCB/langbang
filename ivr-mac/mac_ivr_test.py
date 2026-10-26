#!/usr/bin/env python3
"""mac_ivr_test.py — end-to-end IVR test on the Mac, no browser or phone:
ivr_loop.py (mac audio backend) against the live LangBang, with
mac_fake_caller.py playing the caller over BlackHole.

Run from the desktop session (macOS only lets it capture audio there):
  launchctl kickstart gui/501/com.tyrel.ivr-audio-test
with the job's ProgramArguments pointing here. Output: mac_ivr_test.log.
"""
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
CLIPS = [os.path.join(HERE, "bench", "clips", c) for c in ("hello.wav", "info.wav", "phone_call.wav")]


def relay(p, tag):
    for line in p.stdout:
        print(f"{tag}{line}", end="", flush=True)


def main():
    env = {**os.environ, "IVR_NO_GV": "1", "IVR_NO_REPORT": "1", "PYTHONUNBUFFERED": "1"}
    # IVR_TEST_INBOUND=1: the inbound path real calls take (ivr_answer's
    # --greet-first): greet once the line is quiet, then converse
    inbound = os.environ.get("IVR_TEST_INBOUND") == "1"
    loop = subprocess.Popen([sys.executable, os.path.join(HERE, "ivr_loop.py"),
                             "--answer-secs", "40", "--watch-secs", "8", "--max-turns", "6",
                             *(["--greet-first"] if inbound else [])],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    started = threading.Event()

    def watch():
        for line in loop.stdout:
            print("loop|", line, end="", flush=True)
            if "tapping" in line:
                started.set()
    threading.Thread(target=watch, daemon=True).start()
    if not started.wait(30):
        print("ivr_loop never started listening", flush=True)
        loop.kill()
        return 1
    clips = CLIPS[1:] if inbound else CLIPS          # inbound: we greet first, no "hello" to answer
    caller = subprocess.Popen([sys.executable, os.path.join(HERE, "mac_fake_caller.py"), *clips,
                               "--linger", "30"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    threading.Thread(target=relay, args=(caller, ""), daemon=True).start()
    caller.wait()
    try:
        loop.wait(30)
    except subprocess.TimeoutExpired:
        print("ivr_loop still running after the caller finished — stopping it", flush=True)
        loop.send_signal(2)
        loop.wait(20)
    print(f"done: ivr_loop exit {loop.returncode}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

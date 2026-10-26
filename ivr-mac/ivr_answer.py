#!/usr/bin/env python3
"""ivr_answer.py — answering service for the IVR rig (inbound calls).

Hard-coded answer sequence (GV's own flow, per user):
caller -> GV plays "who's calling" -> our leg rings -> we click ANSWER ->
GV plays the menu ("to accept this call press 1 ...") on the downlink ->
we open the call keypad and press 1 -> call is live -> ivr_loop.py greets
and converses (its teardown hangs up the GV leg).

Detection is pure DOM via CDP on the agent-browser GV tab. The authoritative
"call is live" signal is the call-duration timer inside the call panel: GV
shows Hold/Mute while RINGING and in the press-1 menu state too, but the mm:ss
timer only appears once media is connected.

Usage: ivr_answer.py [--secs N] [--no-loop]   (default 600 s watch window)
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


HG = _load("ivr_hangup", os.path.join(HERE, "ivr_hangup.py"))
LOOP = _load("ivr_loop", os.path.join(HERE, "ivr_loop.py"))

PORT_FILE = HG.PORT_FILE
# a stale duplicate GV tab opened via the ?pli=1 redirect link; watching it
# while the live ring renders in the other tab is a missed-call bug
STALE_TAB = "https://voice.google.com/u/0/calls?pli=1"

# once per call: DOM chains of the hang-up button and every '1' key, so the
# log shows which keypad is the in-call one (report-only)
WHERE_JS = r"""
(() => {
  const chain = el => { const c = []; for (let p = el, i = 0; p && i < 9; p = p.parentElement, i++)
    c.push(p.tagName.toLowerCase() + (p.getAttribute('aria-label') ? '[' + p.getAttribute('aria-label').slice(0, 24) + ']' : ''));
    return c.join('<'); };
  const bs = [...document.querySelectorAll('button,[role=button]')];
  const lab = b => (b.getAttribute('aria-label') || '').trim();
  const out = bs.filter(b => lab(b) === 'Hang up call' || lab(b) === '1' || (b.textContent || '').trim() === '1')
    .map(b => lab(b) + ' vis=' + (b.getBoundingClientRect().width > 0) + ' ' + chain(b));
  return out.join(' || ');
})()
"""

# runs in the page: what does the call UI look like right now? (report-only)
STATE_JS = r"""
(() => {
  const bs = [...document.querySelectorAll('button,[role=button]')];
  const L = bs.map(b => (b.getAttribute('aria-label') || '').trim());
  const has = s => L.includes(s);
  const panel = has('Hold call') || has('Mute call') || has('Hang up call');
  // Handoff signal (2026-10-12): GV now KEEPS an 'Answer call' button in the
  // DOM while the call is live, so 'panel && !answer' alone can wedge the
  // menu phase forever (pressing 1 over and over on an accepted call). The
  // in-call 'Elapsed time' block only exists once media is connected — treat
  // that as authoritative for 'live'.
  const inc = document.querySelector('gv-in-call, gv-in-call-button-panel');
  const live = !!inc && /Elapsed time/i.test(inc.textContent || '');
  return JSON.stringify({answer: has('Answer call'), panel, live});
})()
"""

# click Answer to connect our leg (GV then plays its press-1 menu on the call)
ANSWER_JS = """
(() => {
  const b = [...document.querySelectorAll('button,[role=button]')]
    .find(x => (x.getAttribute('aria-label') || '').trim() === 'Answer call');
  if (b) { b.click(); return 'clicked-answer'; }
  return 'no-answer-button';
})()
"""

# press DTMF 1 on the call keypad; if the pad is closed, open it (any button
# mentioning keypad/dialpad); on failure report the real labels so the log
# tells us what GV actually renders this week.
KEY1_JS = """
(() => {
  // ONLY a '1' inside the in-call panel sends DTMF. The Calls page always
  // shows the outgoing-call <gv-dialpad> too, and its '1' (the first match in
  // DOM order) just types into the dial box — clicking it is how the accept
  // "press" silently never happened. The call panel = nearest ancestor of the
  // "Hang up call" button that also holds the keypad (or its toggle).
  const all = [...document.querySelectorAll('button,[role=button]')];
  const label = b => (b.getAttribute('aria-label') || '').trim();
  const hang = all.find(b => label(b) === 'Hang up call');
  if (!hang) return 'no-call-panel';
  // the outgoing-call pad lives in <gv-make-call-panel>: never press that one
  const isOne = b => (label(b) === '1' || label(b) === "'1'" || (b.textContent || '').trim() === '1')
                     && !b.closest('gv-make-call-panel');
  let panel = hang.parentElement;
  for (let i = 0; panel && i < 12; i++, panel = panel.parentElement) {
    const btns = [...panel.querySelectorAll('button,[role=button]')];
    const one = btns.find(b => isOne(b) && b.getBoundingClientRect().width > 0);
    if (one) { one.click(); return 'pressed-1'; }
    const toggle = btns.find(b => b !== hang && !b.closest('gv-make-call-panel')
                             && /keypad|dialpad|dial pad/i.test(label(b) + ' ' + (b.textContent || '')));
    if (toggle) { toggle.click(); return 'opened-pad'; }
    if (panel.tagName === 'GV-DIALPAD' || panel === document.body) break;
  }
  return 'no-pad | ' + all.map(label).filter(l => l).slice(0, 24).join(' ; ');
})()
"""


# seconds after the Answer click to press 1 (repeats cover GV's prompt)
PRESS_AT = (1.5, 3.0, 4.5, 6.0)


def handoff_ready(st: dict, presses: int) -> bool:
    """Accepted: hand the call to ivr_loop.py? After Answer+press-1 the
    Answer button is gone and the panel stays up. NOT "live + one press":
    call 28a38a02a121 (10-26) showed live at press #1 (+1.8 s), handed off
    at +2.0 s into GV's press-1 prompt, and the loop answered GV's own voice
    as the caller. 'live' is true during the menu; only the full schedule
    is safe. (tests/test_logic.py pins this.)"""
    return bool(st.get("panel")) and presses >= len(PRESS_AT) and bool(st.get("live") or not st.get("answer"))


# tests/run_suite.py's audio layer drives ivr_loop over the same BlackHole
# devices a real call uses: while this file is fresh, rings are left to
# voicemail instead of being answered into a test
SUITE_LOCK = os.path.join(HERE, ".suite_running")
SUITE_LOCK_MAX_S = 30 * 60   # a crashed suite can't block calls longer than this


def suite_running() -> bool:
    try:
        return time.time() - os.path.getmtime(SUITE_LOCK) < SUITE_LOCK_MAX_S
    except OSError:
        return False


# same panel search as KEY1_JS, but only makes sure the keypad is showing
OPEN_PAD_JS = KEY1_JS.replace("if (one) { one.click(); return 'pressed-1'; }",
                              "if (one) return 'pad-open';")
assert OPEN_PAD_JS != KEY1_JS


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def cdp_eval(expr):
    """Runtime.evaluate on the GV tab; returns the string value or None."""
    import urllib.request as U
    try:
        port = int(open(PORT_FILE).read().split("\n")[0])
        targets = json.load(U.urlopen(f"http://127.0.0.1:{port}/json", timeout=3))
    except Exception:
        return None
    pages = [t for t in targets if t.get("type") == "page"
             and t.get("url", "").startswith("https://voice.google.com")
             and t.get("webSocketDebuggerUrl")]
    # prefer the ACTIVE tab: a stale duplicate GV tab would be watched dead.
    # Never grab the SMS watcher tab (it lives on /messages?watcher=sms and
    # belongs to ivr_sms.py).
    pages = [t for t in pages if "watcher=sms" not in t.get("url", "")]
    page = next((t for t in pages if t.get("url") != STALE_TAB), pages[0] if pages else None)
    if not page:
        return None
    try:
        s = HG.ws_connect(page["webSocketDebuggerUrl"])
        HG.ws_send(s, json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "returnByValue": True}}).encode())
        msg = HG.ws_recv_text(s, 1)
        return (((msg or {}).get("result") or {}).get("result") or {}).get("value")
    except Exception as e:
        log("cdp warn:", e.__class__.__name__)
        return None


def page_state():
    try:
        return json.loads(cdp_eval(STATE_JS))
    except Exception:
        return {}


_last_tab_recreate = 0.0


def ensure_calls_tab():
    """If something (e.g. a cron LinkedIn run) closed or navigated away our
    GV Calls tab, open a fresh one through the browser-level CDP endpoint.
    Rate-limited: when the GV session is dead the tab bounces to a sign-in
    page, the existence check never passes, and an unthrottled loop spawns
    a new tab every poll (tab flood, memory growth)."""
    global _last_tab_recreate
    if time.time() - _last_tab_recreate < 120:
        # throttle only applies while the browser answers CDP: after a
        # restart the first recreate must go through immediately (an
        # inbound call in the throttle window would be missed)
        try:
            import urllib.request
            port = int(open(PORT_FILE).read().split("\n")[0])
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        except Exception:
            _last_tab_recreate = 0.0
    if time.time() - _last_tab_recreate < 120:
        return False
    try:
        import urllib.request as U
        port = int(open(PORT_FILE).read().split("\n")[0])
        targets = json.load(U.urlopen(f"http://127.0.0.1:{port}/json", timeout=3))
        if any(t.get("type") == "page"
               and t.get("url", "").startswith("https://voice.google.com/u/0/calls")
               for t in targets):
            return True
        _last_tab_recreate = time.time()
        bws = json.load(U.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3))["webSocketDebuggerUrl"]
        s = HG.ws_connect(bws)
        HG.ws_send(s, json.dumps({"id": 1, "method": "Target.createTarget",
                                  "params": {"url": "https://voice.google.com/u/0/calls"}}).encode())
        HG.ws_recv_text(s, 1)
        s.close()
        log("recreated the GV Calls tab")
        time.sleep(6)
        return True
    except Exception as e:
        log("tab-ensure warn:", e.__class__.__name__)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=int, default=600)
    ap.add_argument("--no-loop", action="store_true", help="detect+accept only, don't run the conversation loop")
    args = ap.parse_args()
    end = time.time() + args.secs

    # phases: wait -> (ring seen, Answer not yet clicked)
    #         menu -> (Answer clicked, trying keypad 1 until timer/live or timeout)
    phase, t_phase, last_try, last_dump, dumped, presses, pad_open = "wait", 0.0, 0.0, 0.0, False, 0, False
    suite_noted = False
    # the loop is started at the Answer click, not after the accept: its
    # startup, /api/ivr/start and the downlink tap (0-3 s) overlap GV's
    # press-1 menu. It blocks on stdin until we say "go" (accepted) or
    # anything else (the call went away: it frees the session and exits).
    loop = None

    def release(cmd):
        nonlocal loop
        if loop is None:
            return
        try:
            loop.stdin.write(cmd + "\n")
            loop.stdin.close()
        except (BrokenPipeError, OSError):
            pass                                # loop died at startup: its log says why
        loop.wait()
        loop = None
    log("watching Google Voice for an incoming call"
        f" (window {args.secs}s, replies from {LOOP.BASE})")

    while time.time() < end:
        st = page_state()
        if not st and not ensure_calls_tab():
            time.sleep(5)
            continue
        now = time.time()

        if phase == "wait" and st.get("answer") and not st.get("live"):
            # 'live' guard: while a call is already up, GV keeps an 'Answer
            # call' (join) button on the banner — don't treat it as a ring.
            if suite_running():
                if not suite_noted:
                    log("INCOMING while the test suite has the audio devices — leaving it to voicemail")
                    suite_noted = True
                time.sleep(0.5)
                continue
            suite_noted = False
            log("INCOMING: GV is ringing — clicking Answer")
            out = cdp_eval(ANSWER_JS) or ""
            # presses is per CALL: left over from the previous call it skipped
            # the press-1 accept, so GV's accept prompt looped on the line
            t_phase, last_try, dumped, presses, pad_open = now, 0.0, False, 0, False
            phase = "menu" if out == "clicked-answer" else "wait"
            log("answer click:", out)
            if phase == "menu":
                if not args.no_loop:
                    loop = subprocess.Popen([sys.executable, os.path.join(HERE, "ivr_loop.py"),
                                             "--greet-first", "--await-go",
                                             "--max-turns", "100000", "--watch-secs", "10"],
                                            stdin=subprocess.PIPE, text=True)
                time.sleep(0.8)                 # let the call panel render
                log("keys:", cdp_eval(WHERE_JS))

        elif phase == "menu":
            # the loop's greet-first path waits for a quiet line before
            # speaking, so a still-playing GV prompt is fine
            if handoff_ready(st, presses):
                log(f"ACCEPTED at +{now - t_phase:.1f}s: handing off to ivr_loop.py")
                phase = "wait"
                if args.no_loop:
                    return
                release("go")
                log("loop finished; back to watching")
                time.sleep(2)
                continue
            # the accept DTMF: open the in-call keypad right away, then press 1
            # on a schedule that spans GV's "call from X, press 1 to accept"
            # prompt. One press 1 s after Answer was lost (media not up yet)
            # and the call timed out; extra 1s after acceptance are harmless.
            due = presses < len(PRESS_AT) and now - t_phase >= PRESS_AT[presses]
            if now - last_try > 0.4 and (due or not pad_open):
                last_try = now
                out = cdp_eval(KEY1_JS if due else OPEN_PAD_JS) or ""
                if out.startswith("pressed-1"):
                    presses += 1
                    pad_open = True
                    log(f"pressed keypad 1 (accept) #{presses} at +{now - t_phase:.1f}s")
                elif out in ("opened-pad", "pad-open"):
                    if not pad_open:
                        log("in-call keypad open")
                    pad_open = True
                elif out.startswith("no-pad") and not dumped:
                    log("keypad UI:", out)          # real labels, once
                    dumped = True
            if not dumped and now - t_phase > 4 and now - last_dump > 4:
                last_dump = now
                log("menu state:", json.dumps(st))  # periodic diagnostics
            if not st.get("panel"):
                log("call went away before we accepted; resuming watch")
                release("abort")
                phase = "wait"
            elif now - t_phase > 20:
                # menu never went live: bail so we don't sit in someone's
                # voicemail; hangup keeps GV clean for the next call
                log("menu state stuck 20s — hanging up and resuming")
                release("abort")
                subprocess.run([sys.executable, os.path.join(HERE, "ivr_hangup.py")])
                phase = "wait"

        time.sleep(0.5 if phase == "wait" else 0.1)


if __name__ == "__main__":
    main()

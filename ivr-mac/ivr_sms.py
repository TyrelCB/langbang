#!/usr/bin/env python3
"""ivr_sms.py — SMS answering loop for Google Voice (runs alongside ivr_answer.py).

Owns ONE dedicated GV tab (URL carries ?watcher=sms so it is never confused
with the calls tab the call rig drives). Polls the Messages list over CDP; when
a thread gets NEW inbound activity it reads the last message, sends it as text
to the LangBang IVR agent (POST /api/ivr/turn, same call session per sender),
and texts the agent's reply back — no audio, no barge-in, no mic.

Coexistence: the call rig (ivr_answer.py/ivr_loop.py) only ever touches the
Calls tab; this file only ever touches the watcher=sms tab. GV treats SMS and
calls as independent channels, so both loops can sit up at once.

Usage: ivr_sms.py [--secs N] [--interval S] [--numbers D+,D+ | --all]
  --numbers  digit-only sender numbers to auto-reply to (default: $IVR_SMS_NUMBERS)
  --all      auto-reply to new inbound texts from ANY sender
Env: IVR_DEVTOOLS_PORT (agent-browser DevToolsActivePort), IVR_BASE, IVR_TOKEN_FILE
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ivr_hangup", os.path.join(HERE, "ivr_hangup.py"))
HG = importlib.util.module_from_spec(spec)
spec.loader.exec_module(HG)

PORT_FILE = HG.PORT_FILE
BASE = os.environ.get("IVR_BASE", "https://ai.tyrel.cloud")
TOKEN_FILE = os.environ.get("IVR_TOKEN_FILE", os.path.join(HERE, ".ivr_token"))
AUTH = "Bear" + "er "  # built at runtime: file tools mangle this literal
WATCH_URL = "https://voice.google.com/u/0/messages?watcher=sms"

ROWS_JS = r"""
(() => {
  const rows = [...document.querySelectorAll('gv-thread-list-item')];
  return JSON.stringify(rows.map(r => r.textContent.replace(/\s+/g, ' ').trim()));
})
"""

def row_digits(label: str) -> str:
    """Phone digits of a thread row's aria-label, ignoring the spaced
    keypad echo the label repeats after the number."""
    m = re.search(r"[(\s]*(\d{3})[)-]*\s*(\d{3})\D*(\d{4})", re.sub(r"[\u202a\u202c]", " ", label))
    if m:
        return "".join(m.groups())
    return re.sub(r"\D", "", label)[:10]

# open the thread whose digits match N; returns 'opened' / 'not-found'
OPEN_JS = r"""
(digits => {
  const rows = [...document.querySelectorAll('gv-thread-list-item')];
  const hit = rows.find(r => r.textContent.replace(/\D/g, '').startsWith(digits.replace(/\D/g, '')));
  if (!hit) return 'not-found';
  const btn = hit.querySelector('[role=button]') || hit;
  btn.click();
  return 'opened';
})
"""

# last message bubble: [senderDigitsOrYou, text] — sender comes from
# "Message from 5 5 5 0 1 0 0 1 2 3, <text>, <weekday>, <date>" lines
READ_JS = r"""
(() => {
  const t = document.body.innerText;
  const re = /Message from ([^,]+?), (.*?), (?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day, .*?\d{4}|Today|Yesterday), \d{1,2}:\d{2}\s*[AP]M/g;
  let m, last = null;
  while ((m = re.exec(t)) !== null) last = m;
  if (!last) return '[]';
  const who = /\d/.test(last[1]) ? last[1].replace(/\D/g, '') : 'you';
  return JSON.stringify([who, last[2]]);
})
"""

SEND_JS = r"""
(text => {
  const ta = [...document.querySelectorAll('textarea')].find(t => t.placeholder === 'Type a message');
  if (!ta) return 'no-textarea';
  const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  ta.focus();
  set.call(ta, text);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  const b = [...document.querySelectorAll('button,[role=button]')]
    .find(x => (x.getAttribute('aria-label') || '') === 'Send message');
  if (!b) return 'no-send';
  setTimeout(() => b.click(), 400);
  return 'sent';
})
"""


def token() -> str:
    return open(TOKEN_FILE).read().strip()


def cdp_port() -> int:
    return int(open(PORT_FILE).read().split("\n")[0])


def list_pages(port: int) -> list:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as r:
        return [t for t in json.load(r)
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]


def tab_ws(port: int, target_id: str) -> str | None:
    return next((t["webSocketDebuggerUrl"] for t in list_pages(port)
                 if t["id"] == target_id), None)


def watcher_tab(port: int) -> dict | None:
    pages = list_pages(port)
    return next((t for t in pages
                 if t.get("url", "").startswith("https://voice.google.com")
                 and "watcher=sms" in t.get("url", "")), None)


def make_watcher_tab(port: int) -> str | None:
    """Open the watcher tab through the browser-level CDP socket; returns its
    targetId. The tab must be tracked by ID afterwards: clicking a thread
    rewrites its URL and the watcher=sms marker is gone."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
        bws = json.load(r)["webSocketDebuggerUrl"]
    s = HG.ws_connect(bws)
    HG.ws_send(s, json.dumps({
        "id": 1, "method": "Target.createTarget",
        "params": {"url": WATCH_URL},
    }).encode())
    msg = HG.ws_recv_text(s, 1)
    s.close()
    return (((msg or {}).get("result") or {}) or {}).get("targetId")


def back_to_list(ws: str) -> None:
    """Thread clicks mutate the tab URL; put it back on the list view."""
    evaluate(ws, f"(() => {{ location.href = {json.dumps(WATCH_URL)}; return 'nav'; }})")


def evaluate(tab_ws: str, expr: str, arg_json: str = "") -> object:
    """Runtime.evaluate; arg_json is appended as the IIFE call argument."""
    s = HG.ws_connect(tab_ws)
    HG.ws_send(s, json.dumps({
        "id": 1, "method": "Runtime.evaluate",
        "params": {"expression": expr + "(" + arg_json + ")", "returnByValue": True},
    }).encode())
    msg = HG.ws_recv_text(s, 1)
    s.close()
    return (((msg or {}).get("result") or {}).get("result") or {}).get("value")


def lb_session(call_ids: dict, digits: str) -> str:
    if digits not in call_ids:
        req = urllib.request.Request(
            f"{BASE}/api/ivr/start?caller=%2B1{digits}", method="POST",
            headers={"authorization": AUTH + token()})
        with urllib.request.urlopen(req, timeout=30) as r:
            call_ids[digits] = json.load(r).get("call_id", "")
        save_sessions(call_ids)
    return call_ids[digits]


# --- persistent SMS session map -------------------------------------------
# The digits->call_id map used to live only in RAM: every loop restart made
# the next text open a brand-new agent session (the "no context" symptom).
# Reset from the PHONE: text the number RESET_SESSION_WORD and the mapping
# is dropped, so the next message tests a fresh session; any other text
# continues the existing one.
SESSIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "sms_sessions.json")
RESET_SESSION_WORD = "reset session"


def load_sessions() -> dict:
    try:
        with open(SESSIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_sessions(call_ids: dict) -> None:
    tmp = SESSIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(call_ids, f, indent=1)
    os.replace(tmp, SESSIONS_FILE)


def lb_reply(digits: str, text: str, call_ids: dict) -> str:
    cid = lb_session(call_ids, digits)
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/ivr/turn?call_id={cid}&caller=%2B1{digits}&out=wav&rate=8000",
        data=body, method="POST",
        headers={"authorization": AUTH + token(), "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("reply", "")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def authed(ws: str) -> bool:
    """True if the tab is on a rendered, signed-in GV shell (the sidebar
    'Send new message' button is only in an authenticated page). An empty
    inbox and a logged-out tab both show zero rows; this tells them apart
    (2026-10-14: threads were archived, 'all caught up' looked like a dead
    session and the watcher crash-looped)."""
    t = str(evaluate(ws, '(() => document.body ? document.body.innerText : "")'))
    return "Send new message" in t or "Send new Message" in t


def wait_rows(ws: str, timeout: float = 40.0) -> list:
    """Poll until the Messages list actually renders (GV SPA boot takes a
    while); returns the row labels. Empty list on timeout."""
    t0 = time.time()
    while time.time() < t0 + timeout:
        try:
            labels = json.loads(evaluate(ws, ROWS_JS) or "[]")
        except Exception:
            labels = []
        if labels:
            return labels
        time.sleep(2)
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=int, default=3600)
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--numbers", default=os.environ.get("IVR_SMS_NUMBERS", ""))
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    allow = {re.sub(r"\D", "", n) for n in args.numbers.split(",") if n.strip()}

    port = cdp_port()
    tab = watcher_tab(port)
    tid = tab["id"] if tab else None
    if not tid:
        log("creating watcher tab")
        tid = make_watcher_tab(port)
        time.sleep(8)
    if not tid:
        log("FATAL: could not create the GV messages watcher tab")
        return 1
    ws = tab_ws(port, tid)
    if not ws:
        log("FATAL: watcher tab vanished")
        return 1

    call_ids: dict[str, str] = load_sessions()   # survives restarts now
    if call_ids:
        log(f"restored {len(call_ids)} persisted SMS session(s)")
    seen: dict[str, str] = {}          # digits -> last row text we acted on
    answered: set[tuple] = set()       # (digits, inbound text) already replied to
    t_end = time.time() + args.secs
    snap = wait_rows(ws)
    if not snap and not authed(ws):
        log("FATAL: messages list never rendered (session expired? re-auth the watcher tab)")
        return 1
    if not snap:
        log("watching an EMPTY inbox (authenticated, 0 threads)")
    for lab in snap:
        seen[row_digits(lab)] = lab  # baseline: existing state is not "new"
    log(f"watching; baseline snapshot {len(seen)} threads; "
        f"allow={sorted(allow) if not args.all else 'ALL'}")

    while time.time() < t_end:
        time.sleep(args.interval)
        ws = tab_ws(port, tid)
        if not ws:  # tab was closed under us: make a new one
            tid = make_watcher_tab(port)
            time.sleep(8)
            ws = tab_ws(port, tid) if tid else None
            if not ws:
                log("FATAL: cannot recover the watcher tab")
                return 1
            snap = wait_rows(ws)
            for lab in snap:
                seen[row_digits(lab)] = lab
            continue
        try:
            labels = json.loads(evaluate(ws, ROWS_JS) or "[]")
        except Exception as e:
            log(f"poll failed ({e.__class__.__name__}); reloading watcher tab")
            back_to_list(ws)
            continue
        if not labels:  # list is empty (all threads archived) or we got
            # navigated into a thread view. If a re-render still shows zero
            # rows we are NOT in a thread: re-baseline so a genuinely emptied
            # inbox isn't a nav loop, and a logged-out tab (renders chrome
            # but no rows) still surfaces as FATAL instead of spinning.
            # (2026-10-14: inbox went empty at 04:48, the old branch navigated
            # forever, and an inbound during that window would be missed.)
            if "itemId=" in str(evaluate(ws, "(() => location.href)")):
                back_to_list(ws)  # genuinely inside a thread view
                continue
            snap = wait_rows(ws, timeout=20.0)
            if not snap and not authed(ws):
                log("FATAL: messages list never rendered (session expired? re-auth the watcher tab)")
                return 1
            for lab in snap:
                seen[row_digits(lab)] = lab
            continue
        for lab in labels:
            digits = row_digits(lab)
            old = seen.get(digits)
            if old == lab:
                continue  # unchanged
            if not args.all and digits not in allow:
                seen[digits] = lab
                continue
            # only mark seen once we've actually handled this row state:
            # the thread view can render one beat late, and marking early
            # drops the inbound forever (2026-10-13 'Hey' bug). A row whose
            # preview starts 'You:' genuinely ends with our own reply.
            if " You:" in lab or lab.startswith("You:"):
                seen[digits] = lab
                continue
            log(f"new activity in {digits}, opening thread")
            opened = evaluate(ws, OPEN_JS, json.dumps(digits))
            if opened != "opened":
                log(f"  could not open thread: {opened}")
                continue
            time.sleep(3)
            last = json.loads(evaluate(ws, READ_JS) or "[]")
            if not last or last[0] == "you":
                # thread view may still be rendering: only accept 'ours' once
                # the row preview itself shows our reply, else retry next poll
                if " You:" in lab:
                    log("  last bubble is ours; nothing to do")
                    seen[digits] = lab
                else:
                    log("  read shows no inbound yet; will retry next poll")
                back_to_list(ws)
                continue
            seen[digits] = lab   # handled: inbound read, about to reply
            if (digits, last[1]) in answered:
                log("  already answered this exact inbound text; skipping")
                back_to_list(ws)
                continue
            log(f"  inbound: {last[1][:120]}")
            if last[1].strip().lower() == RESET_SESSION_WORD:
                call_ids.pop(digits, None)
                save_sessions(call_ids)
                res = evaluate(ws, SEND_JS, json.dumps(
                    "Session reset — next message starts fresh."))
                log(f"  session reset ({res})")
                answered.add((digits, last[1]))
                time.sleep(4)
                back_to_list(ws)
                continue
            try:
                reply = lb_reply(digits, last[1], call_ids)
            except Exception as e:
                log(f"  IVR error: {e.__class__.__name__}: {e}")
                back_to_list(ws)
                continue
            if not reply:
                log("  IVR returned no text; skipping send")
                back_to_list(ws)
                continue
            res = evaluate(ws, SEND_JS, json.dumps(reply))
            log(f"  replied ({res}): {reply[:120]}")
            answered.add((digits, last[1]))
            time.sleep(4)  # let our own bubble render before navigating away
            back_to_list(ws)
    log("watch window over")
    return 0


if __name__ == "__main__":
    sys.exit(main())

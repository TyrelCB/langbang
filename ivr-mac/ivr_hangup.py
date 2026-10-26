#!/usr/bin/env python3
"""ivr_hangup.py — end the Google Voice leg of a call in the agent browser.

The IVR loop's teardown releases the LangBang session, but the phone-side
call only dies when GV's "Hang up call" button is clicked. This clicks it
over the agent browser's DevTools socket with stdlib only (no websockets
package needed), so the loop can fire it unconditionally at teardown.

Finds the debug port in DevToolsActivePort, picks the voice.google.com tab,
and Runtime.evaluate-clicks the hang-up button. Always exits 0: no port
file, no GV tab, or no live call are all silent no-ops (the loop must never
die from teardown).

Env: IVR_DEVTOOLS_PORT (default ~/.agent-browser/profile/DevToolsActivePort)
"""
import base64
import json
import os
import socket
import struct
import sys
import urllib.request

PORT_FILE = os.environ.get(
    "IVR_DEVTOOLS_PORT",
    os.path.expanduser("~/.agent-browser/profile/DevToolsActivePort"),
)

JS = (
    "(() => {"
    "const b=[...document.querySelectorAll('button,[role=button]')]"
    ".find(x=>/hang up call|end call/i.test(x.getAttribute('aria-label')||''));"
    "if(b){b.click();return 'hung-up';} return 'no-call';})()"
)

# the SMS watcher tab never carries a live call; never hang up through it
SKIP_URL_MARK = "watcher=sms"


def ws_connect(ws_url: str, timeout: float = 5.0) -> socket.socket:
    """Minimal client: ws://host/path -> connected, handshaked socket."""
    assert ws_url.startswith("ws://")
    hostport, _, path = ws_url[len("ws://"):].partition("/")
    host, _, port = hostport.partition(":")
    s = socket.create_connection((host, int(port or 80)), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(
        (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            raise OSError("ws handshake closed")
        resp += chunk
    if b"101" not in resp.split(b"\r\n", 1)[0]:
        raise OSError(f"ws handshake refused: {resp.split(b'\\r\\n', 1)[0]!r}")
    return s


def ws_send(s: socket.socket, data: bytes) -> None:
    # client frames MUST be masked (RFC 6455); payload fits one frame here
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    n = len(data)
    hdr = bytes([0x81])  # FIN + text
    if n < 126:
        hdr += bytes([0x80 | n])
    elif n < 65536:
        hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
    s.sendall(hdr + mask + masked)


def ws_recv_text(s: socket.socket, want_id: int, timeout: float = 5.0) -> dict | None:
    """Read unmasked server frames until a text response with matching id."""
    import time as _t
    deadline = _t.time() + timeout
    buf = b""

    def recv_more() -> None:
        nonlocal buf
        chunk = s.recv(65536)
        if not chunk:
            raise OSError("ws closed")
        buf += chunk

    while _t.time() < deadline:
        s.settimeout(max(0.1, deadline - _t.time()))
        while len(buf) < 2:
            recv_more()
        first, ln = buf[0], buf[1]
        ln &= 0x7F
        ext = 2 if ln == 126 else 8 if ln == 127 else 0
        want = 2 + ext + ln
        while len(buf) < want:
            recv_more()
        if ext:
            ln = struct.unpack(">H" if ext == 2 else ">Q", buf[2:2 + ext])[0]
        payload = buf[2 + ext:2 + ext + ln]
        buf = buf[2 + ext + ln:]
        if first & 0x0F != 0x1:  # not text (pong/close/ping)
            continue
        try:
            msg = json.loads(payload)
        except Exception:
            continue
        if msg.get("id") == want_id:
            return msg
    return None


def main() -> int:
    try:
        port = int(open(PORT_FILE).read().split("\n")[0])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as r:
            targets = json.load(r)
    except Exception as e:
        print(f"no CDP ({e.__class__.__name__}) — GV leg left as-is")
        return 0
    page = next(
        (t for t in targets
         if t.get("type") == "page"
         and t.get("url", "").startswith("https://voice.google.com")
         and SKIP_URL_MARK not in t.get("url", "")
         and t.get("webSocketDebuggerUrl")),
        None,
    )
    if not page:
        print("no GV tab — nothing to hang up")
        return 0
    try:
        s = ws_connect(page["webSocketDebuggerUrl"])
        req = json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": JS, "returnByValue": True},
        }).encode()
        ws_send(s, req)
        msg = ws_recv_text(s, 1)
        val = (((msg or {}).get("result") or {}).get("result") or {}).get("value")
        print(f"GV teardown: {val or 'no response'}")
    except Exception as e:
        print(f"GV hangup warn: {e.__class__.__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

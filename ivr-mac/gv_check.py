"""Read-only look at the Mac's Google Voice tab over CDP."""
import json, sys, urllib.request
sys.path.insert(0, "/Users/tyrel/projects/ivr-mac")
import ivr_hangup as H

JS = r"""
(async () => {
  const out = {url: location.href, title: document.title};
  const acct = document.querySelector('[aria-label^="Google Account"]');
  out.account = acct ? acct.getAttribute('aria-label').slice(0, 80) : null;
  try { out.mic_permission = (await navigator.permissions.query({name: 'microphone'})).state; } catch (e) { out.mic_permission = 'err ' + e; }
  const devs = await navigator.mediaDevices.enumerateDevices();
  out.devices = devs.map(d => d.kind + ': ' + (d.label || '(no label)') + (d.deviceId === 'default' ? ' [default]' : ''));
  const ls = {};
  for (let i = 0; i < localStorage.length; i++) { if (!/device/.test(localStorage.key(i))) continue;
    const k = localStorage.key(i);
    ls[k] = (localStorage.getItem(k) || '').slice(0, 200);
  }
  out.localStorage = ls;
  out.web_calls_hint = [...document.querySelectorAll('[aria-label]')].map(e => e.getAttribute('aria-label'))
      .filter(l => /make and receive|calls on this|web/i.test(l)).slice(0, 5);
  return JSON.stringify(out);
})()
"""
targets = json.load(urllib.request.urlopen("http://127.0.0.1:44001/json", timeout=3))
pages = [t for t in targets if t.get("type") == "page"]
for t in pages:
    print("tab:", t["url"][:100])
page = next(t for t in pages if t["url"].startswith("https://voice.google.com"))
s = H.ws_connect(page["webSocketDebuggerUrl"])
H.ws_send(s, json.dumps({"id": 1, "method": "Runtime.evaluate",
                         "params": {"expression": JS, "returnByValue": True, "awaitPromise": True}}).encode())
msg = H.ws_recv_text(s, 1, timeout=10)
val = (((msg or {}).get("result") or {}).get("result") or {}).get("value")
print(json.dumps(json.loads(val), indent=1) if val else msg)

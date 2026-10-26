"""Pin GV's speaker + ringer to BlackHole 2ch (the keys GV's own settings
page writes; mirrors the Linux tab), then reload the tab."""
import json, sys, urllib.request
sys.path.insert(0, "/Users/tyrel/projects/ivr-mac")
import ivr_hangup as H

JS = r"""
(async () => {
  const devs = await navigator.mediaDevices.enumerateDevices();
  const d = devs.find(x => x.kind === 'audiooutput' && x.label.startsWith('BlackHole 2ch'));
  if (!d) return 'BlackHole 2ch output not found';
  const pre = Object.keys(localStorage).find(k => k.endsWith('::audio-input-device-id')).replace('audio-input-device-id', '');
  for (const k of ['audio-output-device', 'audio-output-ring-device']) {
    localStorage.setItem(pre + k + '-id', d.deviceId);
    localStorage.setItem(pre + k + '-label', d.label);
  }
  return 'set ' + d.label + ' ' + d.deviceId.slice(0, 12);
})()
"""
def ev(s, i, expr, await_=False):
    H.ws_send(s, json.dumps({"id": i, "method": "Runtime.evaluate",
                             "params": {"expression": expr, "returnByValue": True, "awaitPromise": await_}}).encode())
    return H.ws_recv_text(s, i, timeout=10)
t = next(t for t in json.load(urllib.request.urlopen("http://127.0.0.1:44001/json", timeout=3))
         if t.get("type") == "page" and t["url"].startswith("https://voice.google.com"))
s = H.ws_connect(t["webSocketDebuggerUrl"])
print(ev(s, 1, JS, True)["result"]["result"]["value"])
H.ws_send(s, json.dumps({"id": 2, "method": "Page.reload", "params": {}}).encode())
print("reloaded")

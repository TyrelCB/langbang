// LangBang frontend — vanilla, no build step, dark HUD by default.
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined) n.textContent = txt;
  return n;
};

let threadId = null;
let threads = [];
let streaming = false;

// ---------- API ----------
const api = {
  async settings() { return (await fetch("/api/settings")).json(); },
  async saveSettings(patch) {
    return (await fetch("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patch }),
    })).json();
  },
  async health() { return (await fetch("/api/health")).json(); },
  async threads() { return (await fetch("/api/threads")).json(); },
  async newThread(title) {
    return (await fetch("/api/threads", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    })).json();
  },
  async delThread(id) { await fetch("/api/threads/" + id, { method: "DELETE" }); },
  async messages(id) { return (await fetch(`/api/threads/${id}/messages`)).json(); },
};

// ---------- rendering ----------
function addMsg(role, text) {
  const m = el("div", "msg " + role, text);
  $("#chat").appendChild(m);
  scrollBottom();
  return m;
}

function addBlock(kind, label) {
  const d = el("details", "block " + kind);
  const s = el("summary", null, label);
  d.appendChild(s);
  d.appendChild(el("pre"));
  $("#chat").appendChild(d);
  scrollBottom();
  return d;
}

function scrollBottom() {
  const c = $("#chat");
  c.scrollTop = c.scrollHeight;
}

function renderHistory(msgs) {
  $("#chat").innerHTML = "";
  for (const m of msgs) {
    if (m.role === "system") continue;
    if (m.role === "human") addMsg("user", m.content);
    else if (m.role === "ai") {
      if (m.thinking) {
        const b = addBlock("thinking", "◈ THINKING");
        b.querySelector("pre").textContent = m.thinking;
      }
      if (m.content) addMsg("assistant", typeof m.content === "string" ? m.content : JSON.stringify(m.content));
      for (const tc of m.tool_calls || []) {
        const b = addBlock("tool", `⚙ ${tc.name}`);
        b.querySelector("pre").textContent = "→ " + JSON.stringify(tc.args, null, 2);
      }
    } else if (m.role === "tool") {
      const b = addBlock("tool", `⚙ ${m.tool_name} result`);
      b.querySelector("pre").textContent = String(m.content).slice(0, 20000);
    }
  }
}

// ---------- chat ----------
async function send() {
  const text = $("#input").value.trim();
  if (!text || streaming) return;
  if (!threadId) await newThread();
  $("#input").value = "";
  addMsg("user", text);
  SFX.play("message_sent");
  streaming = true;
  setBusy(true);

  const asstMsg = addMsg("assistant", "");
  asstMsg.classList.add("cursor");
  let thinkingBlock = null, currentTool = null;

  try {
    const res = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: threadId, text }),
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
        if (!raw.startsWith("data: ")) continue;
        handleEvent(JSON.parse(raw.slice(6)));
      }
    }
  } catch (e) {
    addMsg("error", "CONNECTION LOST: " + e);
    SFX.play("error");
  }
  asstMsg.classList.remove("cursor");
  streaming = false;
  setBusy(false);
  await refreshThreads();

  function handleEvent(ev) {
    if (ev.type === "token") asstMsg.textContent += ev.text;
    else if (ev.type === "thinking") {
      if (!thinkingBlock) { thinkingBlock = addBlock("thinking", "◈ THINKING"); }
      thinkingBlock.querySelector("pre").textContent += ev.text;
    } else if (ev.type === "tool_start") {
      SFX.play("tool_start");
      currentTool = addBlock("tool", `⚙ ${ev.name} …`);
      currentTool.open = true;
      currentTool.querySelector("pre").textContent = "→ " + JSON.stringify(ev.input, null, 2);
      asstMsg = addMsg("assistant", "");
      asstMsg.classList.add("cursor");
    } else if (ev.type === "tool_end") {
      SFX.play("tool_end");
      if (currentTool) {
        currentTool.querySelector("summary").textContent = `⚙ ${ev.name}`;
        currentTool.querySelector("pre").textContent += "\n← " + JSON.stringify(ev.output, null, 2);
        currentTool.open = false;
        currentTool = null;
      }
    } else if (ev.type === "error") {
      SFX.play("error");
      addMsg("error", ev.message);
    } else if (ev.type === "done") {
      SFX.play("message_received");
    }
    scrollBottom();
  }
}

function setBusy(b) {
  $("#btn-send").disabled = b;
  $("#btn-send").textContent = b ? "…" : "SEND ▶";
}

// ---------- threads ----------
async function refreshThreads() {
  threads = await api.threads();
  const box = $("#threads");
  box.innerHTML = "";
  for (const t of threads) {
    const d = el("div", "thread" + (t.id === threadId ? " active" : ""), t.title);
    const x = el("span", "x", "✕");
    x.onclick = async (e) => {
      e.stopPropagation();
      await api.delThread(t.id);
      if (t.id === threadId) { threadId = null; $("#chat").innerHTML = ""; }
      refreshThreads();
    };
    d.appendChild(x);
    d.onclick = () => openThread(t);
    box.appendChild(d);
  }
}

async function openThread(t) {
  SFX.play("click");
  threadId = t.id;
  $("#chat-title").textContent = t.title.toUpperCase();
  renderHistory(await api.messages(t.id));
  refreshThreads();
}

async function newThread() {
  const t = await api.newThread("New chat");
  threadId = t.id;
  $("#chat").innerHTML = "";
  $("#chat-title").textContent = "NEW CHAT";
  SFX.play("thread_new");
  refreshThreads();
}

// ---------- settings ----------
async function openSettings() {
  const s = await api.settings();
  for (const k of ["base_url", "model", "temperature", "max_tokens", "system_prompt"])
    $("#set-" + k).value = s[k];
  $("#set-mcp").value = JSON.stringify(s.mcp_servers || {}, null, 2);
  $("#settings-panel").classList.remove("hidden");
  SFX.play("click");
}

async function saveSettings() {
  const err = $("#settings-error");
  err.classList.add("hidden");
  let mcp;
  try { mcp = JSON.parse($("#set-mcp").value); }
  catch { err.textContent = "MCP JSON is invalid."; err.classList.remove("hidden"); return; }
  await api.saveSettings({
    base_url: $("#set-base_url").value,
    model: $("#set-model").value,
    temperature: parseFloat($("#set-temperature").value),
    max_tokens: parseInt($("#set-max_tokens").value),
    system_prompt: $("#set-system_prompt").value,
    mcp_servers: mcp,
  });
  $("#settings-panel").classList.add("hidden");
  SFX.play("settings_saved");
  checkHealth();
}

// ---------- boot ----------
async function checkHealth() {
  const h = await api.health();
  const el2 = $("#health");
  el2.textContent = "LINK: " + (h.backend_up ? "ONLINE" : "OFFLINE");
  el2.className = "health " + (h.backend_up ? "up" : "down");
  $("#model-tag").textContent = h.model;
}

$("#btn-send").onclick = () => { SFX.play("click"); send(); };
$("#btn-new").onclick = () => { SFX.play("click"); newThread(); };
$("#btn-settings").onclick = openSettings;
$("#btn-save").onclick = saveSettings;
$("#btn-cancel").onclick = () => $("#settings-panel").classList.add("hidden");
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("#btn-sound").onclick = () => {
  const on = localStorage.getItem("lb-sound") === "on";
  localStorage.setItem("lb-sound", on ? "off" : "on");
  $("#btn-sound").textContent = "♪ SOUND: " + (on ? "OFF" : "ON");
};
if (localStorage.getItem("lb-sound") === "on")
  $("#btn-sound").textContent = "♪ SOUND: ON";

checkHealth();
setInterval(checkHealth, 15000);
refreshThreads();

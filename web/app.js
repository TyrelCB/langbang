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
let aborter = null;
let supportsVision = false;
let pendingImages = []; // data URLs awaiting send
const MAX_IMAGES = 4;
const MAX_IMG_BYTES = 6 * 1024 * 1024;

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

// ---------- markdown ----------
marked.use({ breaks: true, gfm: true });
function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(String(text || "")));
}
function setMarkdown(node, raw) {
  node.innerHTML = renderMarkdown(raw);
  enhanceCodeBlocks(node);
}

// ---------- code blocks: highlight + copy/save toolbar ----------
const LANG_EXT = {
  python: "py", js: "js", javascript: "js", typescript: "ts", json: "json",
  bash: "sh", sh: "sh", shell: "sh", zsh: "sh", sql: "sql", yaml: "yml",
  yml: "yml", toml: "toml", html: "html", css: "css", c: "c", cpp: "cpp",
  go: "go", rust: "rs", java: "java", md: "md", diff: "diff", text: "txt",
  plaintext: "txt",
};
function codeLang(code) {
  const c = [...code.classList].find((x) => x.startsWith("language-"));
  return c ? c.slice(9) : "";
}
async function copyText(t) {
  try { await navigator.clipboard.writeText(t); return true; }
  catch {
    const ta = el("textarea"); ta.value = t;
    ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    let ok = false; try { ok = document.execCommand("copy"); } catch {}
    ta.remove(); return ok;
  }
}
function enhanceCodeBlocks(root) {
  root.querySelectorAll("pre > code").forEach((code) => {
    const lang = codeLang(code);
    if (lang && hljs.getLanguage(lang))
      code.innerHTML = hljs.highlight(code.textContent, { language: lang, ignoreIllegals: true }).value;
    else if (!lang)
      code.innerHTML = hljs.highlightAuto(code.textContent).value;
    code.classList.add("hljs");

    const wrap = el("div", "codeblock");
    const bar = el("div", "cb-bar");
    bar.appendChild(el("span", "cb-lang", (lang || "text").toUpperCase()));
    const copy = el("button", "cb-btn", "⧉ COPY");
    copy.onclick = () =>
      copyText(code.textContent).then((ok) => {
        copy.textContent = ok ? "✓ COPIED" : "✕ FAIL";
        setTimeout(() => (copy.textContent = "⧉ COPY"), 1200);
      });
    const dl = el("button", "cb-btn", "⬇ SAVE");
    dl.onclick = () => {
      const ext = LANG_EXT[(lang || "").toLowerCase()] || "txt";
      const url = URL.createObjectURL(new Blob([code.textContent], { type: "text/plain" }));
      const a = el("a"); a.href = url; a.download = `langbang-${Date.now()}.${ext}`;
      a.click(); setTimeout(() => URL.revokeObjectURL(url), 5000);
    };
    bar.append(copy, dl);
    const pre = code.parentElement;
    pre.replaceWith(wrap);
    wrap.append(bar, pre);
  });
}

// ---------- images: paste / attach ----------
function addImage(file) {
  if (!file || !supportsVision) return;
  if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) return;
  if (pendingImages.length >= MAX_IMAGES) return;
  if (file.size > MAX_IMG_BYTES) return;
  const fr = new FileReader();
  fr.onload = () => {
    pendingImages.push(fr.result);
    renderAttachStrip();
  };
  fr.readAsDataURL(file);
}

function renderAttachStrip() {
  const strip = $("#attach-strip");
  strip.innerHTML = "";
  pendingImages.forEach((url, i) => {
    const chip = el("div", "attach");
    const img = el("img");
    img.src = url;
    const x = el("span", "x", "✕");
    x.onclick = () => { pendingImages.splice(i, 1); renderAttachStrip(); };
    chip.append(img, x);
    strip.appendChild(chip);
  });
}

function imagesOf(content) {
  const arr = Array.isArray(content) ? content : [content];
  return arr
    .filter((b) => b && b.type === "image_url")
    .map((b) => (typeof b.image_url === "string" ? b.image_url : b.image_url?.url))
    .filter(Boolean);
}

// ---------- rendering ----------
function addMsg(role, text, images) {
  const m = el("div", "msg " + role);
  setMarkdown(m, text);
  for (const url of images || []) {
    const img = el("img", "msg-img");
    img.src = url;
    m.insertBefore(img, m.firstChild);
  }
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

function textOf(content) {
  if (Array.isArray(content))
    return content.map((b) => (typeof b === "string" ? b : b.text || "")).join("");
  return typeof content === "string" ? content : JSON.stringify(content);
}

function renderHistory(msgs) {
  $("#chat").innerHTML = "";
  for (const m of msgs) {
    if (m.role === "system") continue;
    if (m.role === "human") addMsg("user", textOf(m.content), imagesOf(m.content));
    else if (m.role === "ai") {
      if (m.thinking) {
        const b = addBlock("thinking", "◈ THINKING");
        b.querySelector("pre").textContent = m.thinking;
      }
      if (m.content) addMsg("assistant", textOf(m.content));
      for (const tc of m.tool_calls || []) {
        const b = addBlock("tool", `⚙ ${tc.name}`);
        b.querySelector("pre").textContent = "→ " + JSON.stringify(tc.args, null, 2);
      }
    } else if (m.role === "tool") {
      const b = addBlock("tool", `⚙ ${m.tool_name} result`);
      b.querySelector("pre").textContent = textOf(m.content).slice(0, 20000);
    }
  }
}

// ---------- chat ----------
async function send() {
  const text = $("#input").value.trim();
  const images = pendingImages;
  if ((!text && !images.length) || streaming) return;
  if (!threadId) await newThread();
  $("#input").value = "";
  pendingImages = [];
  renderAttachStrip();
  addMsg("user", text, images);
  SFX.play("message_sent");
  streaming = true;
  aborter = new AbortController();
  setBusy(true);

  let asstMsg = addMsg("assistant", "");
  let asstRaw = "";
  asstMsg.classList.add("cursor");
  let thinkingBlock = null, currentTool = null;
  let renderTimer = null;
  const scheduleRender = () => {
    if (renderTimer) return;
    renderTimer = setTimeout(() => {
      renderTimer = null;
      setMarkdown(asstMsg, asstRaw);
      scrollBottom();
    }, 80);
  };

  try {
    const res = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: threadId, text, images }),
      signal: aborter.signal,
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
    if (e.name === "AbortError") {
      addMsg("error", "TRANSMISSION ABORTED");
    } else {
      addMsg("error", "CONNECTION LOST: " + e);
      SFX.play("error");
    }
  }
  asstMsg.classList.remove("cursor");
  streaming = false;
  setBusy(false);
  await refreshThreads();

  function handleEvent(ev) {
    if (ev.type === "token") { asstRaw += ev.text; scheduleRender(); }
    else if (ev.type === "thinking") {
      if (!thinkingBlock) { thinkingBlock = addBlock("thinking", "◈ THINKING"); }
      thinkingBlock.querySelector("pre").textContent += ev.text;
    } else if (ev.type === "tool_start") {
      SFX.play("tool_start");
      currentTool = addBlock("tool", `⚙ ${ev.name} …`);
      currentTool.open = true;
      currentTool.querySelector("pre").textContent = "→ " + JSON.stringify(ev.input, null, 2);
      asstMsg = addMsg("assistant", "");
      asstRaw = "";
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
  $("#btn-send").textContent = b ? "■ STOP" : "SEND ▶";
  $("#btn-send").classList.toggle("stop", b);
}

function stopGeneration() {
  if (aborter) aborter.abort();
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
  for (const k of ["base_url", "model", "temperature", "max_tokens", "max_react_iterations", "system_prompt"])
    $("#set-" + k).value = s[k];
  const tb = $("#set-tools");
  tb.innerHTML = "";
  for (const [name, on] of Object.entries(s.local_tools || {})) {
    const lab = el("label", "chk");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = !!on; cb.dataset.tool = name;
    lab.append(cb, document.createTextNode(" " + name));
    tb.appendChild(lab);
  }
  $("#set-mcp").value = JSON.stringify(s.mcp_servers || {}, null, 2);
  $("#set-vision").checked = !!(s.capabilities || {}).vision;
  $("#set-thinking").checked = !!s.enable_thinking;
  $("#settings-panel").classList.remove("hidden");
  SFX.play("click");
}

async function saveSettings() {
  const err = $("#settings-error");
  err.classList.add("hidden");
  let mcp;
  try { mcp = JSON.parse($("#set-mcp").value); }
  catch { err.textContent = "MCP JSON is invalid."; err.classList.remove("hidden"); return; }
  const local_tools = {};
  document.querySelectorAll("#set-tools input").forEach((cb) => {
    local_tools[cb.dataset.tool] = cb.checked;
  });
  await api.saveSettings({
    base_url: $("#set-base_url").value,
    model: $("#set-model").value,
    temperature: parseFloat($("#set-temperature").value),
    max_tokens: parseInt($("#set-max_tokens").value),
    max_react_iterations: parseInt($("#set-max_react_iterations").value),
    system_prompt: $("#set-system_prompt").value,
    capabilities: { vision: $("#set-vision").checked },
    enable_thinking: $("#set-thinking").checked,
    local_tools,
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
  supportsVision = !!h.supports_vision;
  $("#btn-attach").classList.toggle("hidden", !supportsVision);
  if (!supportsVision && pendingImages.length) { pendingImages = []; renderAttachStrip(); }
}

$("#btn-send").onclick = () => {
  if (streaming) { stopGeneration(); return; }
  SFX.play("click");
  send();
};
$("#btn-new").onclick = () => { SFX.play("click"); newThread(); };
$("#btn-settings").onclick = openSettings;
$("#btn-save").onclick = saveSettings;
$("#btn-cancel").onclick = () => $("#settings-panel").classList.add("hidden");
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
// image paste / attach (only meaningful when supportsVision; server still enforces)
$("#input").addEventListener("paste", (e) => {
  if (!supportsVision) return;
  const files = [...(e.clipboardData?.items || [])]
    .filter((it) => it.kind === "file" && it.type.startsWith("image/"))
    .map((it) => it.getAsFile());
  if (files.some(Boolean)) {
    e.preventDefault();
    files.forEach((f) => f && addImage(f));
  }
});
$("#btn-attach").onclick = () => { SFX.play("click"); $("#file-img").click(); };
$("#file-img").onchange = (e) => {
  [...e.target.files].forEach(addImage);
  e.target.value = ""; // allow re-selecting the same file
};
$("#btn-sound").onclick = () => {
  const on = localStorage.getItem("lb-sound") === "on";
  localStorage.setItem("lb-sound", on ? "off" : "on");
  $("#btn-sound").textContent = "♪ SOUND: " + (on ? "OFF" : "ON");
};
if (localStorage.getItem("lb-sound") === "on")
  $("#btn-sound").textContent = "♪ SOUND: ON";

// reasoning-card visibility (per browser; server decides whether the model thinks)
function applyReasoningVis() {
  const on = localStorage.getItem("lb-reasoning") !== "off";
  document.body.classList.toggle("hide-reasoning", !on);
  $("#btn-reasoning").textContent = "◈ REASONING: " + (on ? "ON" : "OFF");
  $("#btn-reasoning").classList.toggle("on", on);
}
$("#btn-reasoning").onclick = () => {
  SFX.play("click");
  localStorage.setItem("lb-reasoning",
    localStorage.getItem("lb-reasoning") === "off" ? "on" : "off");
  applyReasoningVis();
};
applyReasoningVis();

checkHealth();
setInterval(checkHealth, 15000);
refreshThreads().then(() => {
  const t = new URLSearchParams(location.search).get("thread");
  if (t) { const th = threads.find((x) => x.id === t); if (th) openThread(th); }
});

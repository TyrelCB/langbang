// LangBang frontend — vanilla, no build step, dark HUD by default.
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined) n.textContent = txt;
  return n;
};

let threadId = null;
const TAB_ID = (() => {
  try {
    let t = sessionStorage.getItem("lb-tab");
    if (!t) { t = Math.random().toString(36).slice(2, 12); sessionStorage.setItem("lb-tab", t); }
    return t;
  } catch { return Math.random().toString(36).slice(2, 12); }
})();
let threads = [];
let supportsVision = false;
let pendingImages = []; // data URLs awaiting send
let pendingFiles = []; // {file} non-image attachments, uploaded at send time
let uploading = false; // blocks re-Enter while attachment bytes are in flight
const MAX_IMAGES = 4;
const MAX_IMG_BYTES = 6 * 1024 * 1024;
// non-image attachments upload to data/uploads/ on send; caps mirror main.py
const MAX_FILES = 6;
const MAX_FILE_BYTES = 20 * 1024 * 1024;
const IMG_TYPE_RE = new RegExp("^image/(png|jpeg|webp|gif)$");

// agentic-visibility state
// Runs are SERVER-owned now (server/runs.py): every stream we watch — one we
// started, one a scheduler fired, one a reload interrupted — is a follower of
// a hub. So run state is per-THREAD, not global: RUNS maps tid -> bundle,
// and many can live at once (only one is on screen; the rest park off-DOM).
const RUNS = new Map();
let serverRuns = new Set(); // active hub tids per GET /api/runs (scheduler/other tabs)
let trajCache = null;  // trajectory rows/totals for the current thread
let trajVisible = false;

// read-aloud state (which provider speaks is the server's CONFIG; we just
// queue mp3 clips from /api/tts and play one at a time)
// off | speak (read answers aloud) | talk (speak + hands-free mic loop).
// TALK is never restored on load: opening the mic needs a user gesture.
let voiceMode = localStorage.getItem("lb-voice") === "speak" ? "speak" : "off";

// ---------- API ----------
// fetch + parse with a READABLE failure: FastAPI's 500 body is plain text,
// so a bare .json() died with "unexpected character at line 1 column 1" and
// hid the real cause (e.g. "database is locked") from the UI entirely.
async function J(res) {
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    let m = body.slice(0, 200);
    try { m = JSON.parse(body).detail || m; } catch { /* keep text */ }
    throw new Error(`HTTP ${res.status}: ${m || res.statusText}`);
  }
  return res.json();
}
const api = {
  async settings() { return J(await fetch("/api/settings")); },
  async saveSettings(patch) {
    return J(await fetch("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patch }),
    }));
  },
  async mcpTest(config) {
    return J(await fetch("/api/mcp/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    }));
  },
  async soundsSlots() { return J(await fetch("/api/sounds/slots")); },
  async sfxRegen(slot) {
    return J(await fetch("/api/sounds/regen", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slot }),
    }));
  },
  async sfxRegenStatus(slot) { return J(await fetch("/api/sounds/regen/" + slot)); },
  async health() { return J(await fetch("/api/health")); },
  async threads() { return J(await fetch("/api/threads")); },
  async newThread(title) {
    return J(await fetch("/api/threads", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }));
  },
  async delThread(id) { await fetch("/api/threads/" + id, { method: "DELETE" }); },
  async renameThread(id, title) {
    return J(await fetch("/api/threads/" + id, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }));
  },
  async autoTitle(id) {
    return J(await fetch(`/api/threads/${id}/retitle`, { method: "POST" }));
  },
  async revertTitle(id) {
    return J(await fetch(`/api/threads/${id}/revert-title`, { method: "POST" }));
  },
  async recap(id) {
    return J(await fetch(`/api/threads/${id}/recap`, { method: "POST" }));
  },
  async touchThread(id) { await fetch("/api/threads/" + id + "/touch", { method: "POST" }); },
  async messages(id) { return J(await fetch(`/api/threads/${id}/messages`)); },
  async todos(id) { return J(await fetch(`/api/threads/${id}/todos`)); },
  async trajectory(id) { return J(await fetch(`/api/threads/${id}/trajectory`)); },
  async search(q) { return J(await fetch("/api/search?q=" + encodeURIComponent(q))); },
  async schedules() { return J(await fetch("/api/schedules")); },
  async newSchedule(t) {
    return J(await fetch("/api/schedules", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(t),
    }));
  },
  async putSchedule(id, patch) {
    return J(await fetch("/api/schedules/" + id, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch),
    }));
  },
  async delSchedule(id) { await fetch("/api/schedules/" + id, { method: "DELETE" }); },
  async runSchedule(id) {
    return J(await fetch("/api/schedules/" + id + "/run", { method: "POST" }));
  },
  async cronNext(cron) {
    return J(await fetch("/api/schedules/next?cron=" + encodeURIComponent(cron)));
  },
  // presence rides the runs poll: notify.watching() skips phone pushes for
  // a thread a visible tab is showing (see server/notify.py)
  async runs() {
    return J(await fetch(`/api/runs?tab=${TAB_ID}&view=${encodeURIComponent(threadId || "")}` +
                         `&vis=${document.hidden ? 0 : 1}`));
  },
  async cancelThread(id) {
    return J(await fetch("/api/threads/" + id + "/cancel", { method: "POST" }));
  },
};

// token counts are approximate everywhere (chars/4 estimator, same as the
// server's compaction trigger) — compact 12.3k display
const fmtTok = (n) =>
  n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
// "78m 08s" for prominent timers, "78m08s"/"4.2s" for inline chips
const fmtClock = (s) => `${Math.floor(s / 60)}m ${String(Math.floor(s) % 60).padStart(2, "0")}s`;
const fmtShort = (s) =>
  s >= 60 ? `${Math.floor(s / 60)}m${String(Math.floor(s) % 60).padStart(2, "0")}s` : `${Math.round(s * 10) / 10}s`;

// ---------- markdown ----------
marked.use({ breaks: true, gfm: true });
function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(String(text || "")));
}
function setMarkdown(node, raw) {
  node.innerHTML = renderMarkdown(raw);
  enhanceCodeBlocks(node);
}
// escape + wrap first case-insensitive occurrence of q in <mark> (result hl)
function hl(text, q) {
  const esc = (s) =>
    s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const i = text.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return esc(text);
  return (
    esc(text.slice(0, i)) +
    "<mark>" + esc(text.slice(i, i + q.length)) + "</mark>" +
    esc(text.slice(i + q.length))
  );
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

const fmtBytes = (n) =>
  n >= 1048576 ? (n / 1048576).toFixed(1) + " MB"
    : n >= 1024 ? Math.round(n / 1024) + " kB" : n + " B";

// ---------- images: paste / attach ----------
function addImage(file) {
  if (!file || !supportsVision) return;
  if (!IMG_TYPE_RE.test(file.type)) return;
  if (pendingImages.length >= MAX_IMAGES) return;
  if (file.size > MAX_IMG_BYTES) return;
  const fr = new FileReader();
  fr.onload = () => {
    pendingImages.push(fr.result);
    renderAttachStrip();
  };
  fr.readAsDataURL(file);
}

// ---------- non-image attachments: upload on send, agent reads from disk ---
function addFile(file) {
  if (!file) return;
  if (pendingFiles.length >= MAX_FILES) {
    addMsg("error", `⚠ max ${MAX_FILES} files per message — ${file.name} dropped`);
    return;
  }
  if (file.size > MAX_FILE_BYTES) {
    addMsg("error", `⚠ ${file.name} too large (max ${MAX_FILE_BYTES / 1048576} MB) — dropped`);
    return;
  }
  pendingFiles.push({ file });
  renderAttachStrip();
}

// picker routing: recognized images ride the base64 vision path (when the
// model has vision); everything else — and images without vision — becomes a
// file attachment the agent opens from disk with its tools
function ingestFiles(list) {
  for (const f of list || []) {
    if (IMG_TYPE_RE.test(f.type) && supportsVision) addImage(f);
    else addFile(f);
  }
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
  pendingFiles.forEach((p, i) => {
    const chip = el("div", "attach file");
    chip.append(
      el("span", "fname", "📄 " + p.file.name),
      el("span", "fsize", fmtBytes(p.file.size)),
    );
    const x = el("span", "x", "✕");
    x.onclick = () => { pendingFiles.splice(i, 1); renderAttachStrip(); };
    chip.append(x);
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
function addMsg(role, text, images, host) {
  const m = el("div", "msg " + role);
  setMarkdown(m, text);
  for (const url of images || []) {
    const img = el("img", "msg-img");
    img.src = url;
    m.insertBefore(img, m.firstChild);
  }
  (host || $("#chat")).appendChild(m);
  scrollBottom();
  return m;
}

function addMsgRaw(cls, text, host) {
  // plain-text row (no markdown round-trip) — usage readouts etc.
  const m = el("div", cls, text);
  (host || $("#chat")).appendChild(m);
  scrollBottom();
  return m;
}

function buildToolCard(label, input) {
  // same chrome as addBlock("tool", …) but DETACHED — caller chooses the host
  // (plain #chat, or nested inside a sub-agent's card body for inner tools)
  const d = el("details", "block tool");
  d.appendChild(el("summary", null, label));
  if (input !== null) d.appendChild(el("pre", null, "→ " + JSON.stringify(input, null, 2)));
  return d;
}

function addBlock(kind, label, host) {
  const d = el("details", "block " + kind);
  const s = el("summary", null, label);
  d.appendChild(s);
  d.appendChild(el("pre"));
  (host || $("#chat")).appendChild(d);
  scrollBottom();
  return d;
}

// renderHistory sets this: every addMsg/addBlock calls scrollBottom(), and
// each call reads scrollHeight = a forced synchronous layout of the whole
// growing #chat — O(n) layouts per thread open (~650 ms of a 1 s switch on a
// 100-message thread). Bulk renders scroll ONCE at the end instead.
let bulkRender = false;
function scrollBottom() {
  if (bulkRender) return;
  // sticky: stream appends only follow while the user is parked at the
  // bottom — scrolling up pins the view (and raises the jump pill)
  const c = $("#chat");
  if (c._pinned) c.scrollTop = c.scrollHeight;
  updateJumpPill();
}

// ---------- jump to bottom ----------
// one pill serves both scroll panes (#chat / #traj-body, whichever tab shows)
const JUMP_NEAR = 140; // px from bottom still counted as "at bottom"
function activePane() {
  return trajVisible ? $("#traj-body") : $("#chat");
}
function updateJumpPill() {
  const c = activePane();
  const far =
    c.scrollHeight - c.scrollTop - c.clientHeight >
    Math.max(320, 0.6 * c.clientHeight);
  $("#jump-bottom").classList.toggle("hidden", !far);
}
function jumpBottom() {
  const c = activePane();
  c.scrollTop = c.scrollHeight;
  c._pinned = true; // jumping down = "follow again"
  updateJumpPill();
}
for (const sel of ["#chat", "#traj-body"]) {
  const c = $(sel);
  c._pinned = true;
  c.addEventListener("scroll", () => {
    c._pinned = c.scrollHeight - c.scrollTop - c.clientHeight < JUMP_NEAR;
    updateJumpPill();
  });
}
$("#jump-bottom").onclick = () => {
  SFX.play("click");
  jumpBottom();
};
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.key === "End") {
    e.preventDefault();
    SFX.play("click");
    jumpBottom();
  }
});

function textOf(content) {
  if (Array.isArray(content))
    return content.map((b) => (typeof b === "string" ? b : b.text || "")).join("");
  return typeof content === "string" ? content : JSON.stringify(content);
}

function schedStrip(s) {
  // run stamp for a scheduled turn (server attaches lb_sched kwargs to the
  // HumanMessage — see schedule._fire). The strip lives INSIDE the user
  // bubble: #chat child indices must stay 1:1 with rendered messages or the
  // search-jump pos mapping (server counts, client children[pos]) drifts.
  const t = new Date(s.ts * 1000), p = (n) => String(n).padStart(2, "0");
  return el("div", "sched-strip", "⏰ SCHEDULED RUN" + (s.manual ? " · MANUAL" : "") +
    " · " + t.getFullYear() + "-" + p(t.getMonth() + 1) + "-" + p(t.getDate()) +
    " " + p(t.getHours()) + ":" + p(t.getMinutes()) + ":" + p(t.getSeconds()));
}

function runChip(s) {
  // tiny provenance tag on an AI bubble produced by a scheduled run — the
  // full ⏰ strip lives on the run's prompt far above, but by the time you're
  // reading output mid-run that strip is scrolled away (see schedStrip).
  const t = new Date(s.ts * 1000), p = (n) => String(n).padStart(2, "0");
  return el("div", "run-chip", `⏰ ${p(t.getMonth() + 1)}-${p(t.getDate())} ${p(t.getHours())}:${p(t.getMinutes())}` +
    (s.manual ? " · MANUAL" : ""));
}

function renderHistory(msgs) {
  bulkRender = true;
  try { renderHistoryInner(msgs); } finally { bulkRender = false; }
  scrollBottom();
}

function renderHistoryInner(msgs) {
  $("#chat").innerHTML = "";
  $("#chat")._pinned = true; // opening a thread always shows its newest message
  // run provenance: a scheduled firing's segment = the stamped prompt up to
  // (exclusive) the next human message. A human WITHOUT a stamp is manual
  // chat (even inside a schedule's thread) and ends the segment. Survives
  // compaction — archived heads keep their sched stamps in their JSON.
  let curSched = null;
  for (const m of msgs) {
    // one malformed step must never truncate the rest of the transcript —
    // a string where an array was expected used to throw here and the whole
    // chat after it silently disappeared (thread dc17b8aa6519)
    try {
      curSched = renderHistoryMsg(m, curSched);
    } catch (e) {
      console.error("renderHistory: skipped a message", e, m);
      const b = addBlock("tool errored", "⚠ couldn't render this step");
      b.querySelector("pre").textContent = String(e) + "\n" + JSON.stringify(m).slice(0, 4000);
    }
  }
}

// ask_user args as the model sent them — a list, or (parser fallback) a JSON string
function gateQuestions(q) {
  if (typeof q === "string") { try { q = JSON.parse(q); } catch { return [q]; } }
  return Array.isArray(q) ? q : q ? [q] : [];
}

function compactLabel(c) {
  const k = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n));
  return `⟲ CONTEXT COMPACTED — ${c.count} EARLIER MSGS SUMMARIZED` +
    (c.before ? ` · ~${k(c.before)} → ~${k(c.after)} tokens` : "") +
    (c.in_turn ? " · mid-task (request kept verbatim)" : "");
}

function renderHistoryMsg(m, curSched) {
  {
    if (m.role === "system") return curSched;
    if (m.compacted) {
      const b = addBlock("compact", compactLabel(m.compacted));
      b.querySelector("pre").textContent = textOf(m.content);
      return curSched; // notes sit between runs; never touch curSched
    }
    if (m.shell) {
      const b = shellBlock(m.shell.cmd);
      shellSeal(b, m.shell);
      return curSched;
    }
    if (m.role === "human") {
      const b = addMsg("user", textOf(m.content), imagesOf(m.content));
      if (m.sched) b.prepend(schedStrip(m.sched));
      curSched = m.sched || null;
    } else if (m.role === "ai") {
      if (m.thinking) {
        const b = addBlock("thinking", "◈ THINKING");
        b.querySelector("pre").textContent = m.thinking;
        attachBlockSpeak(b, m.thinking);
      }
      if (textOf(m.content).trim()) {
        const b = addMsg("assistant", textOf(m.content));
        if (curSched) b.prepend(runChip(curSched));
        attachSpeak(b, textOf(m.content));
      }
      for (const tc of m.tool_calls || []) {
        if (tc.name === "ask_user") {
          const b = addBlock("tool gate-hist", "◆ AGENT ASKED YOU");
          b.querySelector("pre").textContent = gateQuestions(tc.args?.questions)
            .map((q, i) => `Q${i + 1}. ${typeof q === "string" ? q : q.question}` +
              (Array.isArray(q?.options) && q.options.length ? `\n    [${q.options.join(" | ")}]` : "")).join("\n");
          continue;
        }
        if (tc.name === "exit_plan_mode") {
          const b = addBlock("tool gate-hist", "◆ PLAN PROPOSED");
          b.querySelector("pre").textContent = tc.args?.plan || "";
          continue;
        }
        const b = addBlock("tool", `⚙ ${tc.name}`);
        b.querySelector("pre").textContent = "→ " + JSON.stringify(tc.args, null, 2);
        edLink(b, tc.args);
      }
    } else if (m.role === "tool") {
      const failed = m.status === "error";
      const gate = GATE_TOOLS.has(m.tool_name) && !failed;
      const b = addBlock(gate ? "tool gate-hist" : failed ? "tool errored" : "tool",
        gate ? "◆ YOUR ANSWER" : failed ? `✕ ${m.tool_name} failed` : `⚙ ${m.tool_name} result`);
      b.querySelector("pre").textContent = textOf(m.content).slice(0, 20000);
      if (gate) b.open = true;
      // what the agent LOOKED at (server strips the base64 — see _slim):
      // lazy, and a closed <details> never fetches it until opened
      for (const md of m.media || []) {
        if (!/^image\//.test(md.mime || "")) continue;
        const img = el("img", "tool-media");
        img.loading = "lazy";
        img.src = "/api/media?path=" + encodeURIComponent(md.path);
        img.title = md.path;
        b.appendChild(img);
      }
    }
  }
  return curSched;
}

// ---------- chat ----------
async function send() {
  const text = $("#input").value.trim();
  const images = pendingImages;
  if ((!text && !images.length && !pendingFiles.length) || uploading) return;
  // per-thread gate: only a run IN THIS THREAD blocks sending here — other
  // threads run concurrently (the SEND button reads STOP only for this one)
  if (threadId && RUNS.has(threadId)) return;
  // `!cmd` = shell mode (Claude Code style): run on the server, no model call
  if (!images.length && !pendingFiles.length && text.startsWith("!")) {
    const cmd = text.slice(1).trim();
    if (!cmd) return;
    $("#input").value = "";
    await runShell(cmd);
    return;
  }
  stopSpeaking(); // a new run interrupts whatever was being read aloud
  if (!threadId) {
    // lazy row creation can fail (server wedged?) — say so INSTEAD of
    // dropping the message into an unhandled rejection
    try { await createThreadNow(); }
    catch (e) { SFX.play("error"); addMsg("error", "COULD NOT START THREAD: " + e.message); return; }
  }
  // upload attachments first; their server-side paths go INTO the message
  // body (that's what the agent — and the persisted history — sees). On
  // failure the draft survives untouched: fix the network, press SEND again.
  let msgText = text;
  if (pendingFiles.length) {
    uploading = true;
    setBusy(true); // button reads busy while bytes are in flight
    try {
      const fd = new FormData();
      pendingFiles.forEach((p) => fd.append("files", p.file));
      const r = await fetch("/api/upload", { method: "POST", body: fd });
      if (!r.ok) {
        throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status);
      }
      const lines = (await r.json()).files.map(
        (f) => `[attached file] ${f.name} (${fmtBytes(f.size)}) — read it from: ${f.path}`,
      );
      msgText = [text, ...lines].filter(Boolean).join("\n\n");
    } catch (e) {
      addMsg("error", "⚠ attachment upload failed: " + e.message);
      setBusy(false);
      uploading = false;
      return;
    }
    setBusy(false);
    uploading = false;
  }
  $("#input").value = "";
  pendingImages = [];
  pendingFiles = [];
  renderAttachStrip();
  // the server routes this text into the pending gate (free-text answer /
  // plan revision notes) — retire the open card so it can't be answered twice
  for (const c of document.querySelectorAll("#chat .gate:not(.sealed)"))
    gateSeal(c, "answered in chat ↓");
  syncComposerHint();
  $("#chat")._pinned = true; // sending always reveals your own message
  const userBubble = addMsg("user", msgText, images);
  SFX.play("message_sent");
  // live-run bundle (newBundle): timers recomputed from t0 on every tick
  // (background tabs throttle intervals — never accumulate elapsed by
  // counting ticks). Thread ownership lives in the bundle: this run's DOM
  // belongs to ITS thread, and the user can be anywhere else while it
  // streams — nodes park in the detached .off host (parkRun/resumeRun).
  const run = newBundle(threadId);
  run.nodes.push(userBubble); // run's top-level #chat children, in creation order
  // resumeRun() slices server history right before this message — the run's
  // parked nodes ARE the tail from here on (later identical texts win:
  // scanning from the end finds OUR occurrence)
  run.match = (m) => m.role === "human" && !m.shell && textOf(m.content) === msgText;
  RUNS.set(threadId, run);
  run.iv = setInterval(() => tickRun(run), 250);
  syncSendBtn();
  const pipe = runPipeline(run);
  if (run.viewing) {
    $("#sb-live").classList.remove("hidden");
    // In-pane liveness card: on a big context the first token can take 30s+
    // (prefill), and a chat pane that shows nothing reads as "broken". Lives
    // until the first stream event of any kind (handleEvent).
    const ctxTok = (threads.find((x) => x.id === threadId) || {}).context_tokens || 0;
    run.waitLabel = "⏳ AWAITING MODEL" + (ctxTok ? ` — CTX ~${fmtTok(ctxTok)}` : "");
    run.waiting = pipe.put(addBlock("waiting", run.waitLabel + " …", pipe.CH()));
    run.waiting.open = true;
    tickRun(run);
  }
  // POST starts the SERVER-SIDE run; the response is merely our follower
  // stream. Losing it (refresh, phone sleep) now only detaches the view —
  // pollRuns()/openThread reattach via GET /api/threads/{tid}/stream.
  try {
    const res = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: run.tid, text: msgText, images, plan: planOn() }),
    });
    if (!res.ok) {
      // rejected before the run started (busy thread / concurrency cap):
      // nothing was persisted — retract the echoed bubble, don't leave a lie
      let why = "";
      try { await J(res); } catch (e) { why = e.message; }
      userBubble.remove();
      run.nodes = [];
      SFX.play("error");
      pipe.put(addMsg("error", "RUN REJECTED — " + why, null, pipe.CH()));
      endRun(run);
      return;
    }
    await pipeSSE(res, pipe.handleEvent);
    await afterStream(run, pipe);
  } catch (e) {
    // stream death without a terminal event ≠ run death: keep the bundle
    // parked, pollRuns() reattaches from run.seen. If the hub is really gone
    // (server restart), pollRuns sees it missing and repairs the view.
    detachedNote(run, e);
  }
}

// ---------- run lifecycle (server-owned hubs, per-thread followers) ----------
// A run's DOM + counters live in one bundle keyed by thread; MANY can be
// alive at once (only one viewed, the rest parked off-DOM). Streams are
// disposable: send()/attachRun()/rejoinRun() all feed the SAME pipeline, and
// a lost stream just marks the bundle a zombie until pollRuns() reattaches.
function newBundle(tid) {
  return {
    tid,
    t0: Date.now(),
    iv: null, // setInterval(() => tickRun(bundle), 250)
    tools: new Map(), // run_id -> card el; pairs start/end even when parallel
    subs: new Map(),  // task run_id -> {card, body, t0}
    stats: { steps: 0, llm_s: 0, tool_s: 0, in: 0, out: 0, ttfts: 0, ttft_n: 0 },
    todosTouched: false, // did THIS run write the list? drives the stale badge
    seen: 0,        // events consumed from the server hub (absolute index)
    terminal: false,// saw done/error: the run is over, teardown is real
    zombie: false,  // stream dropped WITHOUT terminal — awaiting reattach
    off: el("div"), // detached host for nodes while the thread is off-screen
    nodes: [],      // run's top-level #chat children, in creation order
    viewing: tid === threadId,
    match: null,    // resumeRun's history-slice predicate (send sets it)
    dropEvents: false, // join header landed on a done hub → discard replay
    waiting: null,
    waitLabel: "",
  };
}

// the send()-shaped event pipeline, parameterized by bundle: EVERY follower
// (fresh send, reload replay, phone-wake rejoin) builds one of these and
// pipes SSE frames into handleEvent. Lexical capture of `run` is deliberate —
// concurrent streams must never cross-talk through a shared global.
function runPipeline(run) {
  const CH = () => (run.viewing ? $("#chat") : run.off);
  const put = (n) => { CH().appendChild(n); run.nodes.push(n); return n; };

  let asstMsg = null; // created lazily on first visible token — no empty cursor boxes
  let asstRaw = "";
  let thinkingBlock = null;
  let renderTimer = null;
  const ensureAsst = () => {
    if (!asstMsg) {
      asstMsg = put(addMsg("assistant", "", null, CH()));
      asstRaw = "";
      asstMsg.classList.add("cursor");
    }
  };
  const scheduleRender = () => {
    if (renderTimer) return;
    renderTimer = setTimeout(() => {
      renderTimer = null;
      if (asstMsg) setMarkdown(asstMsg, asstRaw);
      if (run.viewing) scrollBottom();
    }, 80);
  };
  // A ◈ THINKING card is complete once the model moves on — first answer
  // token, a tool call, or the end of the run. Each step then gets its own
  // card in order (the shape renderHistory paints from the checkpoint),
  // instead of every step's reasoning piling into the first card.
  // Auto-read only for runs this tab started: an attach replays the whole
  // ring buffer in a burst, which would re-read reasoning already past.
  const sealThinking = (speak) => {
    if (!thinkingBlock) return;
    const b = thinkingBlock;
    thinkingBlock = null;
    attachBlockSpeak(b, b.querySelector("pre").textContent);
    if (speak && !run.attached && voiceMode !== "off" && loadedVoice.speak_reasoning === "on")
      speakRaw(b._raw || "", b);
  };
  // MUST run before any finalize-then-append (attachSpeak): a pending render
  // would fire after the append and setMarkdown() would wipe the new child
  const flushRender = () => {
    if (!renderTimer) return;
    clearTimeout(renderTimer);
    renderTimer = null;
    if (asstMsg) setMarkdown(asstMsg, asstRaw);
  };

  function handleEvent(ev) {
    // join/gap are FOLLOWER-transport frames, not model output: they must
    // NOT count as the "first sign of life" that retires the AWAITING MODEL
    // card — the join header arrives milliseconds after SEND, and the card's
    // live WAITED timer is exactly what long prefill (big CTX) needs shown.
    if (ev.type === "join") {
      // a FRESH join (seen=0) landing on an already-done hub means the full
      // transcript is in /messages — dropping the replay and repainting
      // beats double-painting it
      if (ev.done && run.seen === 0) run.dropEvents = true;
      return;
    }
    if (run.dropEvents) return;
    if (ev.type === "gap") {
      // our since= fell behind the server ring buffer: whatever we're about
      // to show skips a middle — the FINAL answer still arrives whole
      put(addMsgRaw("usage", "⚠ LIVE BUFFER ROLLED OVER — older output evicted; awaiting final answer", CH()));
      if (run.viewing) scrollBottom();
      return; // not a hub event — seen counts server events only
    }
    if (run.waiting) { run.waiting.remove(); run.waiting = null; } // first REAL event
    run.seen++;
    if (ev.type === "done" || ev.type === "error") run.terminal = true;
    if (ev.type === "token") {
      sealThinking(true);
      if (ev.text.trim()) ensureAsst(); // whitespace-only content never opens a bubble
      if (asstMsg) { asstRaw += ev.text; scheduleRender(); }
    }
    else if (ev.type === "thinking") {
      // whitespace never opens a card (same rule as answer bubbles): a stray
      // trailing "\n" of reasoning would be an empty card under the answer
      if (!thinkingBlock && !ev.text.trim()) return;
      if (!thinkingBlock) { thinkingBlock = put(addBlock("thinking", "◈ THINKING", CH())); }
      thinkingBlock.querySelector("pre").textContent += ev.text;
    } else if (ev.type === "tool_start") {
      SFX.play("tool_start");
      sealThinking(true);
      // close the current answer bubble; the next one opens on its first token
      if (asstMsg) {
        flushRender();
        asstMsg.classList.remove("cursor");
        attachSpeak(asstMsg, asstRaw);
        if (asstRaw.trim()) run.lastBubble = { msg: asstMsg, raw: asstRaw };
        asstMsg = null;
      }
      if (GATE_TOOLS.has(ev.name) && !ev.sub) return; // the `gate` event renders it
      let card, host = null;
      if (ev.name === "task") {
        // sub-agent run: the card IS the live activity container — inner
        // tool cards nest into its body instead of into #chat
        card = buildToolCard(
          `◈ DEEP DIVING${ev.input?.subagent_type ? " — " + ev.input.subagent_type : ""} …`, null);
        const body = el("div", "sub-body");
        card.appendChild(body);
        run.subs.set(ev.run_id, { card, body, t0: Date.now() });
      } else {
        card = buildToolCard(`⚙ ${ev.name} …`, ev.input);
        edLink(card, ev.input);
        if (ev.sub && run.subs.get(ev.sub)) host = run.subs.get(ev.sub).body;
      }
      card.open = true;
      if (host) host.appendChild(card);
      else put(card); // top-level: parked with the run when its thread is off-screen
      run.tools.set(ev.run_id, card);
    } else if (ev.type === "tool_end") {
      SFX.play("tool_end");
      run.stats.steps++;
      if (ev.name !== "task" && typeof ev.dur === "number") run.stats.tool_s += ev.dur;
      const card = run.tools.get(ev.run_id);
      run.tools.delete(ev.run_id);
      if (!card && GATE_TOOLS.has(ev.name)) {
        // resumed gate tool finished: its card was sealed at answer time
      } else if (!card) {
        console.debug("tool_end for unknown run_id", ev.run_id); // e.g. run teardown raced it
      } else if (ev.name === "task") {
        card.querySelector("summary").textContent = `✓ DEEP DIVING — ${fmtClock(ev.dur || 0)}`;
        card.open = false;
        run.subs.delete(ev.run_id);
      } else {
        card.querySelector("summary").textContent =
          `⚙ ${ev.name}${typeof ev.dur === "number" ? " · " + fmtShort(ev.dur) : ""}`;
        card.querySelector("pre").textContent += "\n← " + JSON.stringify(ev.output, null, 2);
        card.open = false;
      }
    } else if (ev.type === "tool_error") {
      // The tool never ran (bad/lost args). Close the card as FAILED instead
      // of leaving a forever-"…" spinner — a visible red error beats a silent
      // stall that looks like the write happened.
      SFX.play("error");
      const card = run.tools.get(ev.run_id);
      run.tools.delete(ev.run_id);
      if (card && lostTodos(ev.name, ev.error)) {
        // sglang's tool-call parser dropped the arguments; the server already
        // told the model to resend (agent.py _FileArgAlias.RESEND) — a retry,
        // not a failure worth a red alarm
        card.querySelector("summary").textContent = "⟳ write_todos lost its arguments in transit — resend requested";
        card.classList.add("retried");
        card.open = false;
        run.subs.delete(ev.run_id);
      } else if (card) {
        card.querySelector("summary").textContent =
          `✕ ${ev.name} FAILED${typeof ev.dur === "number" ? " · " + fmtShort(ev.dur) : ""}`;
        const pre = card.querySelector(":scope > pre");
        if (pre) pre.textContent += "\n← " + (ev.error || "tool error");
        else card.appendChild(el("pre", null, "← " + (ev.error || "tool error")));
        card.open = true;
        card.classList.add("errored");
        run.subs.delete(ev.run_id);
      }
    } else if (ev.type === "gate") {
      // the run paused on a human gate (ask_user / exit_plan_mode) — its
      // `done` follows; the answer starts a NEW hub run via /resume
      if (!document.querySelector(`[data-gate-id="${ev.id}"]`)) {
        put(gateCard(run.tid, ev));
        SFX.play("message_received");
      }
      if (run.viewing) syncComposerHint();
    } else if (ev.type === "todos") {
      run.todosTouched = true;
      // the to-do panel shows the OPEN thread's list — a parked run must not
      // paint over another thread's; replayTodos() redraws ours on switch-back
      if (run.viewing) renderTodos(ev.todos);
    } else if (ev.type === "compacted") {
      const b = put(addBlock("compact", compactLabel(ev), CH()));
      b.querySelector("pre").textContent = ev.summary || "";
      SFX.play("message_received");
    } else if (ev.type === "usage") {
      // one line per model call (ReAct rounds; the compaction summarizer reports via "compacted")
      // each report their own); prefill_tps is ttft-inclusive, so it's a
      // lower bound on real prefill speed — label kept honest as "~".
      run.stats.steps++;
      run.stats.in += ev.input || 0;
      run.stats.out += ev.output || 0;
      run.stats.llm_s += ev.seconds || 0;
      run.stats.ttfts += ev.ttft || 0;
      run.stats.ttft_n++;
      put(addMsgRaw(
        "usage",
        `⚡ IN ${fmtTok(ev.input)} → OUT ${fmtTok(ev.output)} · TTFT ${ev.ttft}s · ` +
          `PREFILL ~${fmtTok(ev.prefill_tps)}/s · DECODE ${ev.decode_tps}/s · ${ev.seconds}s`,
        CH()
      ));
    } else if (ev.type === "error") {
      SFX.play("error");
      put(addMsg("error", ev.message, null, CH()));
    } else if (ev.type === "shell_result") {
      // a shell hub replayed through attach/rejoin (runShell seals its own
      // card inline via its bespoke onEvent; this covers the replay paths)
      const c = run.nodes.find((n) => n.classList?.contains("shell"));
      if (c) shellSeal(c, ev.result);
    } else if (ev.type === "done") {
      SFX.play("message_received");
      sealThinking(true); // queues ahead of the answer below
      // run ended without ever writing the list while items sit open → the
      // card shows a mid-run snapshot; say so (model finished, bookkeeping
      // didn't follow — e.g. a resumed run that dove straight back to work)
      if (run.viewing && !run.todosTouched) markTodosStale();
      // read the FINAL answer bubble only — mid-run "let me check…" bubbles
      // keep their manual 🔊 (auto-reading play-by-play is filler audio)
      // The todo gate can deliver the answer that streamed BEFORE its
      // write_todos card (server holds it, no re-generation) — then the
      // final segment is empty and the answer is the last sealed bubble.
      if (voiceMode !== "off") {
        if (asstMsg && asstRaw.trim()) speakRaw(asstRaw, asstMsg);
        else if (!asstMsg && run.lastBubble) speakRaw(run.lastBubble.raw, run.lastBubble.msg);
      }
    }
    if (run.viewing) scrollBottom();
  }

  // waiting-card removal, bubble sealing, and the ✕ stamp on cards whose
  // tool_end never arrived (aborted/errored runs — never a forever-spinner)
  function finalize() {
    if (run.waiting) { run.waiting.remove(); run.waiting = null; }
    sealThinking(false); // STOP / error mid-thought: keep the 🔊, don't read it
    if (asstMsg) {
      flushRender();
      asstMsg.classList.remove("cursor");
      attachSpeak(asstMsg, asstRaw); // final answer bubble (partial on error — speakable anyway)
    }
    for (const card of run.tools.values()) {
      const s = card.querySelector("summary");
      s.textContent = "✕ " + s.textContent.replace(/ …$/, "");
      card.open = false;
    }
  }

  return { handleEvent, finalize, put, CH };
}

// parse SSE `data:` frames from a fetch response body into handleEvent
async function pipeSSE(res, onEvent) {
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
      if (!raw.startsWith("data: ")) continue; // ": ping" heartbeats pass here
      onEvent(JSON.parse(raw.slice(6)));
    }
  }
}

// a follower stream ended: only a terminal event means the RUN ended
async function afterStream(run, pipe) {
  if (!run.terminal) { detachedNote(run); return; }
  pipe.finalize();
  await endRun(run);
}

// keep the bundle alive (parked nodes and all) and say what happened; the
// 4s poller reattaches from run.seen while the hub is still alive
function detachedNote(run, err) {
  run.zombie = true;
  const host = run.viewing ? $("#chat") : run.off;
  run.nodes.push(addMsgRaw(
    "usage",
    "⚠ CONNECTION TO RUN LOST" + (err ? `: ${err}` : "") +
    " — the run continues on the server; reconnecting…",
    host
  ));
  SFX.play("error");
}

// forget a bundle; repair=true repaints from the server (used when a run
// vanished without a terminal event — e.g. server restart mid-run)
async function endRun(run, repair = false) {
  RUNS.delete(run.tid);
  if (run.iv) clearInterval(run.iv);
  if (run.tid === threadId) {
    syncSendBtn();
    $("#sb-live").classList.add("hidden");
    if (repair) {
      const seq = openSeq;
      try {
        const ms = await api.messages(run.tid);
        if (seq === openSeq && threadId === run.tid) {
          renderHistory(ms);
          ensureTodosCard(run.tid, ms); // dead run's card -> the committed truth
        }
      } catch (e) { /* transient — a reopen will try again */ }
    }
  }
  await refreshThreads();
  refreshStats();
}

// watch a hub we did NOT start: reload reattach (since=0 replay), a run the
// scheduler fired, or a second tab. Replay reconstructs the tail through the
// same pipeline; a hub already DONE returns done-first and we bail to a
// clean history render instead of double-painting under it.
async function attachRun(tid, since) {
  const run = newBundle(tid);
  run.seen = since;
  run.attached = true; // replayed, not started here — no reasoning auto-read
  RUNS.set(tid, run);
  syncSendBtn();
  run.iv = setInterval(() => tickRun(run), 250);
  const pipe = runPipeline(run);
  if (run.viewing) {
    $("#sb-live").classList.remove("hidden");
    run.waitLabel = "⏳ RUN IN PROGRESS";
    run.waiting = pipe.put(addBlock("waiting", run.waitLabel + " — REPLAYING …", pipe.CH()));
    run.waiting.open = true;
    tickRun(run);
  }
  try {
    const res = await fetch(`/api/threads/${encodeURIComponent(tid)}/stream?since=${since}`);
    if (!res.ok) { await endRun(run, true); return; } // hub vanished between /api/runs and here
    await pipeSSE(res, pipe.handleEvent);
    if (run.dropEvents) { await endRun(run, true); return; } // finished between the runs-poll and the join header
    await afterStream(run, pipe); // seal + teardown, or park as zombie
  } catch (e) {
    detachedNote(run, e);
  }
}

// rejoin a zombie bundle after the network came back (phone wake, tab freeze)
async function rejoinRun(run) {
  run.zombie = false;
  const pipe = runPipeline(run); // fresh closure: continuation text opens a new bubble
  if (run.waiting === null && run.viewing && !run.terminal) {
    run.waitLabel = "⏳ RUN IN PROGRESS";
    run.waiting = pipe.put(addBlock("waiting", run.waitLabel + " — RECONNECTED …", pipe.CH()));
    run.waiting.open = true;
  }
  try {
    const res = await fetch(`/api/threads/${encodeURIComponent(run.tid)}/stream?since=${run.seen}`);
    if (!res.ok) { await endRun(run, true); return; }
    await pipeSSE(res, pipe.handleEvent);
    await afterStream(run, pipe);
  } catch (e) {
    detachedNote(run, e);
  }
}

// SEND/STOP follows the VIEWED thread only — concurrent runs elsewhere leave
// this thread's button a normal SEND
function syncSendBtn() {
  setBusy(!!(threadId && RUNS.get(threadId)) || uploading);
}

// the glue between the server's run registry and the sidebar/UI: which
// threads are busy, and (re)attaching the viewed one. Cheap: /api/runs is an
// in-memory dict — no SQL, no model.
async function pollRuns() {
  let act;
  try {
    act = Object.keys((await api.runs()).runs || {});
  } catch (e) {
    return; // server restarting under us — next beat will find it
  }
  serverRuns = new Set(act);
  const cur = threadId ? RUNS.get(threadId) : null;
  if (cur) {
    // A LIVE (non-zombie) follower is never touched here: the hub can leave
    // the active map the instant the run completes, and our own stream
    // delivers done/error a beat later — only zombies need this poller.
    if (cur.zombie && serverRuns.has(threadId)) rejoinRun(cur);
    else if (cur.zombie) await endRun(cur, true); // hub gone (restart / done while offline) → clean repaint
  } else if (threadId && serverRuns.has(threadId)) {
    attachRun(threadId, 0); // scheduler run / other tab / post-reload
  }
  refreshThreads(); // repaint the ◉ running dots
  pollNotify();
}

let chargeT = null; // X-buster: STOP charges while a run is live
function setBusy(b) {
  const btn = $("#btn-send");
  btn.textContent = b ? "■ STOP" : "SEND ▶";
  btn.classList.toggle("stop", b);
  clearTimeout(chargeT);
  btn.classList.remove("charged");
  // a run still streaming after 4s = full charge: glow goes steady-hot
  if (b) chargeT = setTimeout(() => btn.classList.add("charged"), 4000);
}

function stopGeneration() {
  // STOP is a server-side cancel now (the old abort() killed the SSE, which
  // used to kill the run — exactly the phone-timeout disaster). The hub
  // ends its stream with a cancelled error event; teardown happens there.
  const r = threadId && RUNS.get(threadId);
  if (r) api.cancelThread(r.tid).catch(() => {});
}

// ---------- shell mode (`!cmd`): run on the server, no model call ----------
function shellBlock(cmd, host) {
  const b = addBlock("shell", `$ ${cmd} …`, host);
  b.open = true; // user ran it to see the result — never hide the output
  return b;
}

function shellSeal(b, { cmd, out, exit, dur }) {
  b.querySelector("summary").textContent =
    `$ ${cmd} · exit=${exit ?? "?"} · ${fmtShort(dur || 0)}`;
  b.querySelector("pre").textContent = out;
}

async function runShell(cmd) {
  if (!threadId) {
    // lazy row creation can fail (server wedged?) — say so INSTEAD of
    // dropping the message into an unhandled rejection
    try { await createThreadNow(); }
    catch (e) { SFX.play("error"); addMsg("error", "COULD NOT START THREAD: " + e.message); return; }
  }
  if (RUNS.has(threadId)) return; // this thread is already running (button reads STOP)
  // hub-backed (SSE like chat): `! sleep 300` now SURVIVES a dead tab —
  // the command runs on the server regardless; we just watch. DOM
  // ownership is the same bundle scheme as send(), so switching threads
  // mid-await parks the card off-screen instead of leaking it.
  const run = newBundle(threadId);
  run.match = (m) => m.shell && m.shell.cmd === cmd;
  RUNS.set(threadId, run);
  run.iv = setInterval(() => tickRun(run), 250);
  syncSendBtn();
  SFX.play("tool_start");
  $("#chat")._pinned = true; // same reveal rule as send()
  const CH = () => (run.viewing ? $("#chat") : run.off);
  const put = (n) => { CH().appendChild(n); run.nodes.push(n); return n; };
  const card = put(shellBlock(cmd, CH()));
  let sealed = false;
  const onEvent = (ev) => {
    if (ev.type === "join" || ev.type === "gap") return; // headers/notes, not hub events
    run.seen++;
    if (ev.type === "done" || ev.type === "error") run.terminal = true;
    if (ev.type === "shell_result") {
      shellSeal(card, ev.result);
      SFX.play("tool_end");
      sealed = true;
    } else if (ev.type === "error") {
      SFX.play("error");
      card.remove();
      put(addMsg("error", "SHELL FAILED: " + ev.message, null, CH()));
    }
  };
  try {
    const res = await fetch("/api/shell", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: run.tid, command: cmd }),
    });
    if (!res.ok) {
      let why = "";
      try { await J(res); } catch (e) { why = e.message; }
      card.remove();
      SFX.play("error");
      put(addMsg("error", "SHELL REJECTED — " + why, null, CH()));
      await endRun(run);
      return;
    }
    await pipeSSE(res, onEvent);
    if (!run.terminal) { detachedNote(run); return; } // command lives on; card shows on reopen
  } catch (e) {
    detachedNote(run, e);
    return;
  }
  await endRun(run); // context grew — CTX chip and thread order may move (refreshThreads inside)
  if (sealed) refreshStats();
}

// ---------- agentic visibility: live run bar, to-dos, totals, trajectory ----------
function tickRun(run) {
  // the live bar reflects the VIEWED thread's run; other runs keep ticking
  // their own timers invisibly (sub-chip clocks and traj refresh only matter
  // while you're looking at them)
  if (!run || run.tid !== threadId) return;
  const bar = $("#sb-live");
  const s = run.stats;
  const parts = [`⏱ RUN ${fmtClock((Date.now() - run.t0) / 1000)}`, `${s.steps} STEPS`];
  if (s.llm_s) parts.push(`LLM ${fmtShort(s.llm_s)}`);
  if (s.tool_s) parts.push(`TOOL ${fmtShort(s.tool_s)}`);
  if (s.ttft_n) parts.push(`TTFT ${Math.round((1000 * s.ttfts) / s.ttft_n)}ms`);
  if (s.in || s.out) parts.push(`IN ${fmtTok(s.in)} · OUT ${fmtTok(s.out)}`);
  bar.textContent = parts.join("  │  "); // wipes stale chips too
  // waiting card carries the visible clock until the model speaks
  if (run.waiting?.isConnected)
    run.waiting.querySelector("summary").textContent =
      `${run.waitLabel} · WAITED ${fmtClock((Date.now() - run.t0) / 1000)} …`;
  // one live timer chip per active sub-agent (reads as ×N when parallel)
  for (const sub of run.subs.values())
    bar.appendChild(
      el("span", "sub-chip", `◈ DEEP DIVING ${fmtClock((Date.now() - sub.t0) / 1000)}`)
    );
  // trajectory rows commit per-event server-side, so re-reading every few
  // seconds keeps the tab live during the run (not just after it)
  if (trajVisible && Date.now() - (run._statsAt || run.t0) > 3000) {
    run._statsAt = Date.now();
    refreshStats();
  }
}

function renderTodos(todos) {
  if (!Array.isArray(todos) || !todos.length) return hideTodos();
  const head = (n, s) => n + " " + s;
  const done = todos.filter((t) => t.status === "completed").length;
  const prog = todos.filter((t) => t.status === "in_progress").length;
  $("#todo-label").textContent =
    `☰ TO-DOS — ${head(done, "COMPLETED")} · ${head(prog, "IN PROGRESS")} · ` +
    `${head(todos.length - done - prog, "PENDING")}`;
  const list = $("#todo-list");
  list.innerHTML = "";
  for (const t of todos) {
    const row = el("div", "todo-item" + (t.status === "completed" ? " done" : ""));
    const ic = el("span", "todo-ic");
    if (t.status === "completed") ic.textContent = "✓";
    else if (t.status === "in_progress") ic.classList.add("spin");
    else ic.textContent = "○";
    row.append(ic, el("span", "todo-txt", String(t.content ?? t.text ?? "")));
    list.appendChild(row); // textContent-built: model-authored content stays XSS-safe
  }
  $("#todo-panel").classList.remove("hidden");
  applyTodosCollapsed();
}

function hideTodos() {
  $("#todo-panel").classList.add("hidden");
  $("#todo-list").innerHTML = "";
}

// A run finished without touching write_todos while the visible list still
// has open items → stamp the header; it's a true statement regardless of
// whether the model forgot (resumed runs) or the turn was off-list entirely.
function markTodosStale() {
  const list = $("#todo-list");
  if ($("#todo-panel").classList.contains("hidden")) return;
  if (!list.querySelector(".todo-item:not(.done)")) return; // all closed already
  const label = $("#todo-label");
  if (label.querySelector(".todo-stale")) return; // already stamped
  label.appendChild(el("span", "todo-stale", " · ⚠ NOT UPDATED THIS RUN"));
}

function applyTodosCollapsed() {
  $("#todo-list").classList.toggle("hidden", localStorage.getItem("lb-todos-collapsed") === "on");
}

function resetTrajView() {
  hideTodos();
  trajCache = null;
  $("#sb-totals").textContent = "";
  if (trajVisible) renderTraj();
}

// replay from persisted messages — write_todos calls live in tool_call args,
// so this survives compaction (archived messages keep their args).
// Returns whether a list was found (see ensureTodosCard).
function replayTodos(msgs) {
  for (let i = msgs.length - 1; i >= 0; i--)
    for (const tc of msgs[i].tool_calls || [])
      if (tc.name === "write_todos" && Array.isArray(tc.args?.todos)) {
        renderTodos(tc.args.todos);
        return true;
      }
  hideTodos();
  return false;
}

// The message replay CAN miss the list: the serving stack drops oversized
// tool-call args (the call then has no todos to scan), or the super-step
// that committed it never landed (a checkpoint write lost to a DB failure).
// The list itself is plain checkpointed state — /todos asks the checkpointer
// directly, so a reopened tab or a second browser still sees the card the
// initiating tab got from live SSE. Skipped while a run is live: SSE owns
// the card then, and the checkpoint read lags a super-step behind.
async function ensureTodosCard(tid, msgs) {
  if (replayTodos(msgs) || RUNS.has(tid)) return;
  const seq = openSeq;
  let todos = null;
  try {
    todos = (await api.todos(tid)).todos;
  } catch (e) {
    return; // transient — the next reopen will try again
  }
  // guarded paint: a thread switch mid-fetch must not draw the old thread's
  // list, and a run that started meanwhile owns the card again (SSE)
  if (seq !== openSeq || threadId !== tid || RUNS.has(tid)) return;
  if (todos) renderTodos(todos);
}

async function refreshStats() {
  const id = threadId;
  if (!id) {
    trajCache = null;
    $("#sb-totals").textContent = "";
    if (trajVisible) renderTraj();
    return;
  }
  let data;
  try {
    data = await api.trajectory(id);
  } catch (e) {
    return; // transient — a periodic refresh will pick the rows up next beat
  }
  if (id !== threadId) return; // thread switched mid-fetch — stale read
  trajCache = data;
  const T = data.totals || {};
  const groups = [];
  const G = (...xs) => groups.push(xs.filter(Boolean).join(" · "));
  if (T.turns) {
    G(`${T.turns} TURNS`, `${T.steps} STEPS`);
    G(`LLM ${fmtShort(T.llm_s || 0)}`, `TOOL ${fmtShort(T.tool_s || 0)}`);
    const perf = [];
    if (T.ttft_avg != null) perf.push(`TTFT ${Math.round(1000 * T.ttft_avg)}ms`);
    if (T.llm_s > 0 && T.tok_out) perf.push(`${fmtTok(Math.round(T.tok_out / T.llm_s))} OUT/s`);
    G(...perf);
    if (T.cache_read > 0 && T.tok_in) G(`CACHE ${Math.round((100 * T.cache_read) / T.tok_in)}%`);
    G(`IN ${fmtTok(T.tok_in || 0)}`, `OUT ${fmtTok(T.tok_out || 0)}`);
  }
  $("#sb-totals").textContent = groups.join("  │  ");
  if (trajVisible) renderTraj();
}

function showTab(which) {
  trajVisible = which === "traj";
  $("#chat").classList.toggle("hidden", trajVisible);
  $("#trajectory").classList.toggle("hidden", !trajVisible);
  $("#tab-chat").classList.toggle("on", !trajVisible);
  $("#tab-traj").classList.toggle("on", trajVisible);
  updateJumpPill(); // pill follows whichever pane is now visible
  if (!trajVisible) return;
  // while a run streams, the cache is by definition stale — tickRun also
  // re-reads every ~3s once this tab is visible
  if (trajCache && !(threadId && RUNS.has(threadId))) renderTraj();
  else refreshStats();
}

// ---- trajectory helpers: failure detection + copy ----
// A tool row whose meta carries `err` never ran (bad/lost args — e.g. a
// write_todos whose args arrived as {}): it's logged by on_tool_error.
const trajFailed = (e) => e.type === "tool" && !!(e.meta && e.meta.err);
// write_todos whose args sglang's qwen3_coder parser dropped ({} → pydantic
// "todos Field required"): the model is asked to resend — label it as such
const lostTodos = (name, err) => name === "write_todos" && /todos\s+Field required/.test(String(err || ""));
function gateSummary(v) {
  if (!v || typeof v !== "object") return String(v ?? "");
  if (v.kind === "plan") return "◆ plan proposed for approval";
  return "◆ asked: " + (v.questions || []).map((q) => q.question).join(" | ");
}
// Human-readable text for one row (what ⧉ copies). Note: the trajectory
// log stores tool I/O capped at ~2 KB and model text at ~500 chars — the
// full values live in the chat transcript.
function trajRowText(e) {
  const m = e.meta || {};
  const when = new Date(e.ts * 1000).toLocaleTimeString();
  const dur = e.dur != null ? " · " + fmtShort(e.dur) : "";
  const fmtv = (v) => (typeof v === "string" ? v : JSON.stringify(v, null, 2));
  if (e.type === "tool") {
    const lines = [`[${when}] TOOL ${e.name || ""}${dur}${trajFailed(e) ? " — FAILED" : ""}` +
      (m.sub ? " (sub-agent)" : "")];
    if (m.desc) lines.push("task: " + m.desc);
    if (m.in != null && m.in !== "") lines.push("→ " + fmtv(m.in));
    if (m.err) lines.push("✕ " + m.err);
    else if (m.out != null && m.out !== "") lines.push("← " + fmtv(m.out));
    return lines.join("\n");
  }
  if (e.type === "model") {
    const tok = e.tok_in != null ? ` · in ${e.tok_in} → out ${e.tok_out}` : "";
    return `[${when}] ASSISTANT ${e.name || ""}${dur}${tok}\n${m.text || ""}`.trimEnd();
  }
  if (e.type === "user") return `[${when}] USER\n${m.text || ""}`;
  if (e.type === "error") return `[${when}] ERROR\n${m.message || ""}`;
  if (e.type === "gate") return `[${when}] GATE\n${fmtv(m.value)}`;
  if (e.type === "compact") return `[${when}] COMPACT${dur} · ${m.count} msgs summarized, ${m.kept} kept · ctx ~${m.before} → ~${m.after} tokens\n${m.text || ""}`;
  return `[${when}] ${e.type}\n${fmtv(m)}`;
}
function trajCopyBtn(getText, tip) {
  const b = el("button", "traj-copy", "⧉");
  b.dataset.label = "⧉";
  b.title = tip;
  b.onclick = async (ev) => {
    ev.stopPropagation(); // the row's own click toggles the JSON panel
    const ok = await copyText(getText());
    b.textContent = ok ? "✓" : "✕";
    b.classList.toggle("done", ok);
    setTimeout(() => { b.textContent = b.dataset.label; b.classList.remove("done"); }, 1200);
  };
  return b;
}

function renderTraj() {
  const body = $("#traj-body");
  const keepScroll = body.scrollTop; // live refresh rebuilds the DOM — hold the view
  body.innerHTML = "";
  const q = $("#traj-search").value.trim().toLowerCase();
  const events = (trajCache && trajCache.events) || [];
  if (!events.length) {
    body.appendChild(el("div", "no-results", "// NO TELEMETRY YET"));
    return;
  }
  const groups = []; // contiguous runs share a turn_id
  for (const e of events) {
    const g = groups[groups.length - 1];
    if (g && g.turn_id === e.turn_id) g.rows.push(e);
    else groups.push({ turn_id: e.turn_id, rows: [e] });
  }
  const BADGE = { user: "USER", model: "ASSISTANT", tool: "TOOL", error: "ERROR", gate: "GATE", compact: "COMPACT" };
  // Rows land in seq order, but tool rows are logged at span END — a task's
  // children would render above it. Sort by effective START (end - dur); the
  // sort is stable, so ties keep log order.
  const st = (e) => e.ts - (typeof e.dur === "number" ? e.dur : 0);
  for (const g of groups) g.rows.sort((a, b) => st(a) - st(b));
  groups.forEach((g, gi) => {
    const t0 = Math.min(...g.rows.map(st));
    const span = Math.max(Math.max(...g.rows.map((e) => e.ts)) - t0, 0.001);
    const card = el("div", "traj-card");
    const resent = g.rows.filter((e) => trajFailed(e) && lostTodos(e.name, e.meta.err)).length;
    const fails = g.rows.filter(trajFailed).length - resent;
    const head = el("div", "traj-head",
      `RUN ${gi + 1} · ${new Date(t0 * 1000).toLocaleTimeString()} · ${fmtShort(span)}`);
    if (fails) head.appendChild(el("span", "traj-fails", ` · ✕ ${fails} FAILED`));
    if (resent) head.appendChild(el("span", "traj-resent", ` · ⟳ ${resent} RESENT`));
    head.appendChild(trajCopyBtn(() => g.rows.map(trajRowText).join("\n\n"), "copy this whole run as text"));
    card.appendChild(head);
    const strip = el("div", "traj-strip");
    strip.appendChild(el("i", "traj-tick input")); // input sits at 0 (CSS left:0)
    for (const e of g.rows) {
      if (e.type !== "model" && e.type !== "tool" && e.type !== "compact") continue;
      const tk = el("i", "traj-tick " + e.type + (trajFailed(e) ? " err" : ""));
      tk.style.left = (100 * (st(e) - t0)) / span + "%";
      tk.title = `${e.name || e.type}${e.dur != null ? " · " + fmtShort(e.dur) : ""}`;
      strip.appendChild(tk);
    }
    card.appendChild(strip);
    for (const e of g.rows) {
      const m = e.meta || {};
      const label = BADGE[e.type] || e.type;
      const failed = trajFailed(e);
      const lost = failed && lostTodos(e.name, m.err);
      const preview =
        failed && lostTodos(e.name, m.err) ? "⟳ arrived with no arguments (tool-call parser glitch) — resend requested"
        : failed ? "✕ " + String(m.err).split("\n")[0] + "  ← " + String(m.in || "")
        : e.type === "tool" ? (m.desc ? "◈ " + m.desc : String(m.in || ""))
        : e.type === "error" ? String(m.message || "")
        : e.type === "gate" ? gateSummary(m.value)
        : e.type === "compact" ? `⟲ ${m.count} msgs summarized, ${m.kept} kept · context ~${fmtTok(m.before)} → ~${fmtTok(m.after)}${m.in_turn ? " · mid-task" : ""}`
        : String(m.text || "");
      const row = el("div", "traj-row" + (m.sub ? " sub" : "") + (failed && !lost ? " err" : ""));
      row.appendChild(el("span", "traj-badge " + (lost ? "gate" : failed ? "error" : e.type),
                         lost ? "TOOL ⟳" : failed ? "TOOL ✕" : label));
      row.appendChild(el("span", "traj-name", e.name || (e.dur != null ? fmtShort(e.dur) : "")));
      row.appendChild(el("span", "traj-pre", preview));
      row.appendChild(trajCopyBtn(() => trajRowText(e), "copy this step as text"));
      if (q && !(label + " " + (e.name || "") + " " + preview).toLowerCase().includes(q))
        row.classList.add("hidden");
      row.onclick = () => {
        if (row._meta) { row._meta.remove(); row._meta = null; return; }
        // meta pre is a SIBLING of the row — keep a direct ref to toggle it
        const json = JSON.stringify({ ...e, meta: m }, null, 2);
        row._meta = el("div", "traj-meta-wrap");
        const b = trajCopyBtn(() => json, "copy the raw JSON");
        b.textContent = "⧉ JSON";
        b.dataset.label = "⧉ JSON";
        row._meta.append(b, el("pre", "traj-meta", json));
        row.after(row._meta);
      };
      card.appendChild(row);
    }
    body.appendChild(card);
  });
  body.scrollTop = keepScroll;
  updateJumpPill(); // rebuild changed content height — pill may need to appear
}

// ---------- threads ----------
// Two-click delete confirm lives on a module-scoped tid, NOT the ✕ element:
// the ◉ dot sync repaints the sidebar every few seconds (pollRuns), which
// would otherwise wipe an armed row mid-confirm.
let armedTid = null;
let armT = 0;
// An open ✎ rename editor must survive the sidebar refresh: pollRuns calls
// refreshThreads every 4 s and the rebuild below used to replace the input
// mid-typing (the user saw the editor "time out" and lose its text). While
// renaming, only the data and the ◉ running dots are refreshed in place.
let renamingTid = null;

async function refreshThreads() {
  threads = await api.threads();
  const box = $("#threads");
  if (renamingTid && box.querySelector(".t-edit")) {
    for (const row of box.querySelectorAll(".thread")) {
      const id = row.dataset.tid;
      row.classList.toggle("running", RUNS.has(id) || serverRuns.has(id));
    }
    return;
  }
  renamingTid = null; // the editor is gone (thread deleted / view rebuilt)
  box.innerHTML = "";
  for (const t of threads) {
    // busy = OUR client has a run in it, or the server says one is in flight
    // (another tab, a phone, or a cron-scheduled run we know nothing about)
    const busy = RUNS.has(t.id) || serverRuns.has(t.id);
    const d = el("div", "thread" + (t.id === threadId ? " active" : "")
                 + (busy ? " running" : ""));
    d.dataset.tid = t.id;
    d.appendChild(el("span", "ctx", t.context_tokens != null ? fmtTok(t.context_tokens) : ""));
    const name = el("span", "t-name", t.title);
    name.title = t.title;
    d.appendChild(name);
    // one-click title ops; ✎ (manual) is handled as inline edit below
    const act = (txt, tip, fn) => {
      const s = el("span", "a", txt);
      s.title = tip;
      s.onclick = async (e) => {
        e.stopPropagation();
        SFX.play("click");
        s.textContent = "…";
        s.title = "…";
        let r = {};
        try { r = await fn(); } catch (err) { r = { detail: String(err) }; }
        s.textContent = txt;
        if (r && r.title) {
          if (t.id === threadId) $("#chat-title").textContent = r.title.toUpperCase();
          refreshThreads();
        } else {
          s.classList.add("fail");
          s.title = "Failed: " + ((r && r.detail) || "error");
          SFX.play("error");
        }
      };
      d.appendChild(s);
    };
    act("⟲", "Restore the initial title (first message / task name)", () => api.revertTitle(t.id));
    act("⚡", "Auto-title from the conversation (one LLM call)", () => api.autoTitle(t.id));
    const pen = el("span", "a", "✎");
    pen.title = "Rename — Enter saves, Esc cancels";
    pen.onclick = (e) => { e.stopPropagation(); SFX.play("click"); startInlineRename(t, d, name); };
    d.appendChild(pen);
    const x = el("span", "x", "✕");
    x.title = "Delete thread";
    if (armedTid === t.id) {  // repaint mid-confirm: re-apply the armed look
      x.classList.add("arm");
      x.textContent = "⚠";
      x.title = `${t.n_msgs} messages — click again to DELETE`;
    }
    x.onclick = async (e) => {
      e.stopPropagation();
      // Mis-click guard: rows that hold real content need a second,
      // confirming click; empty/thin rows (abandoned new chats, test
      // probes) die in one click so junk stays quick to sweep.
      const trivial = (t.n_msgs ?? 99) < 3 && (t.chars ?? 1e9) < 100;
      if (armedTid !== t.id && !trivial) {
        armedTid = t.id;
        x.classList.add("arm");
        x.textContent = "⚠";
        x.title = `${t.n_msgs} messages — click again to DELETE`;
        clearTimeout(armT);
        // repaint (not element mutation): the live row may be a fresh one
        armT = setTimeout(() => { armedTid = null; refreshThreads(); }, 4000);
        return;
      }
      armedTid = null;
      clearTimeout(armT);
      // server cancels an in-flight hub for us (del_thread → runs.cancel)
      await api.delThread(t.id);
      // park BEFORE the wipe: a run streaming in this very thread keeps its
      // nodes off-screen until its stream tears itself down with the cancelled
      // terminal event (deleted thread can never be re-opened, so the parked
      // host is simply dropped — RUNS.delete happens in that teardown)
      if (t.id === threadId) { parkRun(RUNS.get(t.id)); threadId = null; syncDraft(); $("#chat").innerHTML = ""; resetTrajView(); }
      refreshThreads();
    };
    d.appendChild(x);
    d.onclick = () => openThread(t);
    box.appendChild(d);
  }
  $("#btn-recap").disabled = !threadId; // recap needs an open thread
  $("#btn-tprompt").disabled = !threadId; // a draft has no row to hang it on
  updateCtxTag();
}

function startInlineRename(t, row, name) {
  const inp = el("input", "t-edit");
  inp.value = t.title === "New chat" ? "" : t.title;
  inp.placeholder = t.title || "title…";
  row.replaceChild(inp, name);
  renamingTid = t.id;
  inp.focus();
  inp.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    renamingTid = null;
    const v = inp.value.trim();
    if (save && v && v !== t.title) {
      const r = await api.renameThread(t.id, v);
      if (r && r.title && t.id === threadId) $("#chat-title").textContent = r.title.toUpperCase();
    }
    refreshThreads();
  };
  inp.onclick = (e) => e.stopPropagation();
  inp.onkeydown = (e) => {
    e.stopPropagation();
    if (e.key === "Enter") finish(true);
    else if (e.key === "Escape") finish(false);
  };
  inp.onblur = () => finish(true);
}

// what the next model call will actually re-prefill for the open thread
function updateCtxTag() {
  const t = threads.find((x) => x.id === threadId);
  $("#ctx-tag").textContent = t ? "CTX ~" + fmtTok(t.context_tokens) : "";
}

// ---------- live-run thread ownership ----------
// send()/runShell() mark their DOM as belonging to their own thread. If the
// user switches threads mid-run, parkRun() moves the run's top-level nodes
// into a detached host (later events append there directly via CH());
// resumeRun() re-attaches them IN ORDER and draws history only up to the
// run's prompt — re-rendering the full server history would double-paint the
// very tool cards/text that are still streaming into the parked nodes.
function parkRun(r) {
  if (!r) return;
  r.viewing = false;
  for (const n of r.nodes) if (n.parentNode === $("#chat")) r.off.appendChild(n);
}

function parkAll() {
  for (const r of RUNS.values()) parkRun(r);
}

function resumeRun(r, msgs) {
  if (!r) return false;
  let idx = -1;
  for (let i = msgs.length - 1; i >= 0; i--)
    if (r.match && r.match(msgs[i])) { idx = i; break; } // null match (reattach): no slice
  // The run's own turn only reaches graph state when its superstep CHECKPOINT
  // commits — measured: mid-tool-run the raw checkpoint can still show just
  // the PREVIOUS turns (a shell turn never lands until it commits, and
  // compaction can eat the prompt entirely). No match therefore means the
  // whole list is the committed prefix that the parked tail continues.
  const head = idx < 0 ? msgs : msgs.slice(0, idx);
  for (const n of r.nodes) if (n.parentNode === $("#chat")) r.off.appendChild(n);
  r.viewing = true;
  renderHistory(head);
  for (const n of r.nodes) if (n.parentNode === r.off) $("#chat").appendChild(n);
  scrollBottom();
  return true;
}

// phone drawer: on ≤720px the sidebar is off-canvas (☰ + scrim); closing is a
// no-op on desktop, so every "you have a thread now" path can call it freely
function closeNav() { $("#app").classList.remove("nav-open"); }

// fast thread-switch race: an earlier openThread's messages fetch can land
// AFTER a later one's — without this token the stale response paints thread
// B's bubbles under A's header (render is the async part; title/threadId aren't)
let openSeq = 0;

async function openThread(t) {
  const seq = ++openSeq;
  SFX.play("click");
  closeNav();
  if (recapAbort) recapAbort.abort();
  closeRecap(); // one thread's recap must not follow you to another
  parkAll(); // every run's output leaves with its thread (ours re-attaches below)
  threadId = t.id;
  syncDraft();
  $("#chat-title").textContent = t.title.toUpperCase();
  syncSendBtn(); // a busy thread opens to STOP, an idle one to SEND — even
                 // while other threads' runs stream on (per-thread gating)
  // resuming = current: sorts it to the top. Best-effort: a 500 (DB busy
  // during a heavy run) must not reject after an early `seq` return or after
  // the render already succeeded — worst case the row just won't re-sort.
  const bumped = api.touchThread(t.id).catch(() => {});
  syncTPrompt(t.id);
  draftModel = null; // a pick on an abandoned draft doesn't follow you
  syncTModel(t.id);
  const msgs = await api.messages(t.id);
  if (seq !== openSeq) return; // superseded by a newer open/newThread — don't paint
  // returning to the thread that is mid-run: re-attach its parked nodes and
  // let them keep streaming (resumeRun draws the history above them);
  // any other case redraws the whole committed transcript. No local bundle
  // but the server says busy → this is a reload/second-tab return: attach.
  const r = RUNS.get(t.id);
  if (r) { resumeRun(r, msgs); tickRun(r); }
  else renderHistory(msgs);
  if (r) $("#sb-live").classList.remove("hidden");
  else $("#sb-live").classList.add("hidden");
  if (!r && serverRuns.has(t.id)) attachRun(t.id, 0);
  else if (!r) showPendingGates(t.id, seq);
  syncComposerHint();
  ensureTodosCard(t.id, msgs); // last write_todos call re-draws the panel on
                               // switch/reload; /todos covers dropped args /
                               // lost commits the messages scan can't see
  refreshStats();
  await bumped; // order may already have moved — refresh AFTER the bump lands
  refreshThreads();
}

async function newThread() {
  // Lazy: NO server row yet — an abandoned "New chat" used to leave a
  // permanent sidebar ghost. createThreadNow() materializes on first send.
  openSeq++; // invalidate any in-flight openThread render (see openSeq)
  closeNav();
  if (recapAbort) recapAbort.abort();
  closeRecap();
  parkAll(); // switch to a draft: mid-run threads' output stays behind (runs go on)
  threadId = null;
  syncDraft();
  syncTPrompt(null);
  syncTModel(null);
  $("#chat").innerHTML = "";
  $("#chat-title").textContent = "NEW CHAT";
  $("#sb-live").classList.add("hidden");
  resetTrajView();
  syncSendBtn(); // a run streaming elsewhere must NOT leave STOP in the draft
  syncComposerHint();
  SFX.play("thread_new");
  refreshThreads();
}

async function createThreadNow() {
  const t = await api.newThread("New chat");
  const draftPlan = planOn(null);
  threadId = t.id;
  syncDraft();
  if (draftPlan) { setPlan(true, t.id); setPlan(false, null); } // plan toggled on the draft carries over
  if (draftModel) { // a model picked on the draft lands on the new thread BEFORE its first run
    const ov = draftModel; draftModel = null;
    try {
      await J(await fetch(`/api/threads/${t.id}/model`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(ov),
      }));
    } catch {}
    syncTModel(t.id);
  }
  refreshThreads();
}

// ---------- chat search ----------
let searchTimer = null;

function showThreadList() {
  $("#search-results").classList.add("hidden");
  $("#threads").classList.remove("hidden");
}

async function runSearch() {
  const q = $("#search").value.trim();
  if (q.length < 2) { showThreadList(); return; }
  const rs = await api.search(q);
  if ($("#search").value.trim() !== q) return runSearch(); // stale — query moved meanwhile
  const box = $("#search-results");
  box.innerHTML = "";
  $("#threads").classList.add("hidden");
  box.classList.remove("hidden");
  if (!rs.length) {
    box.appendChild(el("div", "no-results", "// NO MATCHES"));
    return;
  }
  box.appendChild(
    el("div", "no-results", `// ${rs.length} MATCH${rs.length > 1 ? "ES" : ""}` +
      (rs.length >= 50 ? " (SHOWN, CAP REACHED)" : ""))
  );
  for (const r of rs) {
    const d = el("div", "thread result");
    const name = el("div", "t-name");
    name.innerHTML = DOMPurify.sanitize(
      hl(r.title, q) + (r.role === "title" ? "  ⌕ TITLE" : "")
    );
    d.appendChild(name);
    if (r.pos >= 0) {
      const snip = el("div", "r-snip");
      snip.innerHTML = DOMPurify.sanitize(hl(r.snippet, q));
      d.appendChild(snip);
    }
    d.onclick = async () => {
      await openThread({ id: r.thread_id, title: r.title });
      if (r.pos < 0) return;
      // server's pos counts exactly the nodes renderHistory draws
      const n = $("#chat").children[r.pos];
      if (!n) return;
      n.scrollIntoView({ block: "center" });
      n.classList.add("flash");
      setTimeout(() => n.classList.remove("flash"), 2500);
    };
    box.appendChild(d);
  }
}

$("#search").addEventListener("input", () => {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 250);
});
$("#search").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("#search").value = ""; showThreadList(); }
});

// ---------- voice (read-aloud) ----------
// Synthesis happens server-side (/api/tts -> one clip per request; the server
// disk-caches by text+voice, so even a page reload never re-synthesizes).
// Per BUBBLE we keep our own Audio + object URL forever: first 🔊 fetches
// once, every replay/pause/seek afterwards is local. The auto-read queue
// holds bubbles, not URLs. One voice at a time — starting one stops another.
let speakQ = [];        // auto-read queue (voice mode) — bubbles awaiting play
let voiceCur = null;    // bubble whose audio is playing right now
let speakOwner = null;  // bubble whose 🔊 shows ■ STOP (playing or synthesizing)
let synthing = false;   // a /api/tts fetch is in flight
let playSeq = 0;        // bump cancels an in-flight playBubble (STOP mid-synth)

const fmtMS = (t) =>
  !isFinite(t)
    ? "0:00"
    : `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;

function markSpeaking(msg) {
  if (speakOwner && speakOwner._speakBtn) {
    speakOwner._speakBtn.textContent = "🔊";
    speakOwner._speakBtn.classList.remove("on");
  }
  speakOwner = msg;
  if (msg && msg._speakBtn) {
    msg._speakBtn.textContent = "■ STOP";
    msg._speakBtn.classList.add("on");
  }
}

function syncPlayer(msg) {
  const a = msg._aud;
  if (!a || !msg._ptime) return;
  msg._pp.textContent = a.paused ? "▶" : "⏸";
  msg._ptime.textContent = fmtMS(a.currentTime) + " / " + fmtMS(a.duration);
  msg._pfill.style.width =
    isFinite(a.duration) && a.duration > 0 ? (100 * a.currentTime) / a.duration + "%" : "0%";
}

function wireAudio(msg) {
  const a = msg._aud;
  a.onplay = () => { voiceCur = msg; markSpeaking(msg); syncPlayer(msg); };
  a.onpause = () => {
    if (voiceCur === msg) { voiceCur = null; markSpeaking(null); }
    syncPlayer(msg);
  };
  a.onended = () => {
    if (voiceCur === msg) { voiceCur = null; markSpeaking(null); }
    syncPlayer(msg);
    pumpSpeak();
  };
  a.ontimeupdate = () => syncPlayer(msg);
  a.onerror = () => { if (voiceCur === msg) { voiceCur = null; markSpeaking(null); } pumpSpeak(); };
}

// mini player INSIDE the bubble (pos invariant) — survives the clip, so the
// bubble stays seekable/pausable for the whole session
function buildPlayer(msg) {
  if (msg.querySelector(".tts-player") || !msg.appendChild) return;
  const p = el("div", "tts-player");
  const pp = el("button", "cb-btn", "⏸");
  pp.title = "pause / resume";
  const back = el("button", "cb-btn", "−5");
  back.title = "back 5 s";
  const time = el("span", "tts-time", "0:00 / 0:00");
  const fwd = el("button", "cb-btn", "+5");
  fwd.title = "forward 5 s";
  const bar = el("div", "tts-bar");
  const fill = el("div", "tts-fill");
  bar.appendChild(fill);
  bar.title = "click to seek";
  const seek = (t) => {
    const a = msg._aud, d = isFinite(a.duration) ? a.duration : 0;
    if (!d) return;
    a.currentTime = Math.max(0, Math.min(t, d - 0.05));
  };
  pp.onclick = (e) => {
    e.stopPropagation();
    if (msg._stale) { stopSpeaking(); playBubble(msg); return; } // stream was released
    const a = msg._aud;
    if (a.paused) a.play().catch(() => {}); else a.pause();
  };
  back.onclick = (e) => { e.stopPropagation(); seek(msg._aud.currentTime - 5); };
  fwd.onclick = (e) => { e.stopPropagation(); seek(msg._aud.currentTime + 5); };
  bar.onpointerdown = (e) => {
    e.stopPropagation();
    const a = msg._aud;
    if (!isFinite(a.duration) || !a.duration) return;
    const r = bar.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    a.currentTime = f * (a.duration - 0.05);
  };
  p.append(pp, back, time, fwd, bar);
  msg._pp = pp; msg._ptime = time; msg._pfill = fill;
  (msg._playerHost || msg).appendChild(p);
}

async function synth(msg) {
  if (msg._aud && !msg._stale) return true; // one fetch per bubble (until released)
  let res;
  try {
    res = await fetch("/api/tts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: msg._raw }),
    });
  } catch (e) {
    return voiceFail("VOICE: server unreachable — " + e);
  }
  if (!res.ok) {
    let m = "HTTP " + res.status;
    try { m = (await res.json()).detail || m; } catch {}
    return voiceFail("VOICE: " + m);
  }
  if ((res.headers.get("content-type") || "").includes("application/json")) {
    // streaming provider (Pocket TTS): the server hands back a clip URL the
    // <audio> element plays progressively while it's synthesized — speech
    // starts in ~0.1 s. Duration reads Infinity until the stream ends (the
    // player shows 0:00 and seek no-ops until then); a later replay or a
    // reload gets the finished file from the cache, fully seekable.
    const out = await res.json();
    msg._aud = new Audio(out.stream);
    msg._stale = false;
    voiceBubbles.add(msg);
  } else {
    msg._blob = URL.createObjectURL(await res.blob());
    msg._aud = new Audio(msg._blob);
  }
  wireAudio(msg);
  buildPlayer(msg);
  return true;
}

// A Pocket clip that is still streaming keeps its HTTP connection — and its
// server-side synthesis — alive even while paused (browsers keep buffering
// a paused element). Synthesis is one-at-a-time, so a clip you moved away
// from would make the one you asked for wait until it finished. Starting
// a clip therefore releases every OTHER bubble's unfinished stream; that
// bubble re-requests (cache hit if it completed) when played again.
const voiceBubbles = new Set();
function releaseStreams(except) {
  for (const b of voiceBubbles) {
    // "still streaming": Chrome reports duration=Infinity until the stream
    // ends; Firefox reports a GROWING finite estimate instead (measured:
    // 4.3 → 10.3 → … 73.3 s) — but networkState stays LOADING (2) until the
    // last byte, then IDLE (1). Firefox also keeps downloading a PAUSED
    // element to the end, so a duration-only check never released it.
    const a = b._aud;
    if (b === except || !a || (a.networkState !== 2 && isFinite(a.duration))) continue;
    a.pause();
    a.removeAttribute("src");
    a.load(); // closes the connection → server cancels within ~1 s
    b._stale = true;
  }
}

async function playBubble(msg) {
  releaseStreams(msg);
  const gen = ++playSeq;
  markSpeaking(msg); // immediate ■ STOP feedback — first synthesis may take seconds
  synthing = true;
  const ok = await synth(msg);
  synthing = false;
  if (!ok || gen !== playSeq) return; // STOP (or newer play) landed mid-synth
  if (voiceCur && voiceCur !== msg) voiceCur._aud.pause(); // onpause clears voiceCur
  await msg._aud.play().catch((e) => {
    // AbortError = WE interrupted it (another 🔊 / STOP paused this clip
    // while it was still buffering — Firefox words it "fetching process …
    // aborted by the user agent"). Expected, not a failure; the newer
    // action owns the voice now, so leave speakOwner alone.
    if (e && e.name === "AbortError") return;
    if (gen !== playSeq) return;
    markSpeaking(null);
    voiceFail("VOICE: playback failed — " + e);
  });
}

// auto-read entry point: queue the bubble, pump when the voice is free
function speakRaw(raw, owner) {
  const text = raw.trim();
  if (!text || !owner) return;
  owner._raw = text;
  speakQ.push(owner);
  pumpSpeak();
}

function pumpSpeak() {
  if (voiceCur || synthing || !speakQ.length) return;
  playBubble(speakQ.shift());
}

function stopSpeaking() {
  playSeq++; // also cancels a mid-synth playBubble (fetch may finish; we won't play it)
  speakQ = [];
  if (voiceCur) voiceCur._aud.pause(); // currentTime survives → resume is free
  markSpeaking(null);
}

let lastVoiceFail = 0;
function voiceFail(msg) {
  const now = Date.now();
  if (now - lastVoiceFail < 3000) return; // one bubble per burst, not per retry
  lastVoiceFail = now;
  addMsg("error", msg);
  SFX.play("error");
}

// ---------- inline media ----------
// Absolute file paths to images/video/audio that appear in a FINAL answer get
// players — agents love citing "saved: /tmp/render.mp4" and switching apps to
// check is the worst. Finalization-only (attachSpeak's contract: mid-stream
// these are half-typed tokens), and never inside <pre>/<a> or mixed <code> (command
// echoes and existing links must not fake players). Files that don't exist
// collapse to a dim chip: agent prose cites paths that aren't there yet.
const MEDIA_RE = /(?<![\w/])\/(?:[^/\s]+\/)*[^/\s]*\.(?:png|jpe?g|gif|webp|mp4|webm|mov|m4v|mp3|wav|ogg|opus|m4a)\b/gi;
const MEDIA_KIND = (p) =>
  /\.(png|jpe?g|gif|webp)$/i.test(p) ? "img"
  : /\.(mp4|webm|mov|m4v)$/i.test(p) ? "video" : "audio";

function mediafy(msg) {
  if (msg.querySelector(":scope > .media")) return; // idempotent
  const seen = new Set();
  const paths = [];
  const w = document.createTreeWalker(msg, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => {
      if (n.parentElement.closest("pre,a,.media")) return NodeFilter.FILTER_REJECT;
      // inline `code` is how models cite paths — accept it only when the
      // span IS the path (a `cmd /x.png` echo stays inert)
      const c = n.parentElement.closest("code");
      if (c && !/^\/\S+$/.test(c.textContent.trim())) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  for (let n; (n = w.nextNode());)
    for (const m of n.nodeValue.match(MEDIA_RE) || [])
      if (!seen.has(m)) { seen.add(m); paths.push(m); }
  if (!paths.length) return;
  const host = el("div", "media");
  for (const p of paths) {
    const src = "/api/media?path=" + encodeURIComponent(p);
    const kind = MEDIA_KIND(p);
    const item = el("div", "media-item");
    const node = document.createElement(kind);
    node.src = src;
    if (kind !== "img") { node.controls = true; node.preload = "metadata"; }
    if (kind === "video") node.setAttribute("playsinline", "");
    node.addEventListener("error", () =>
      item.replaceChildren(el("div", "media-missing", "⚠ " + p + " (missing)")));
    const cap = el("div", "media-cap");
    const link = el("a", "", p.split("/").pop());
    link.href = src;
    link.target = "_blank";
    cap.append(link);
    item.append(node, cap);
    host.append(item);
  }
  msg.append(host);
}

// 🔊 rides INSIDE the bubble (.msg) — never as a bare #chat child (the
// search-jump feature keys off #chat child indices). setMarkdown() rewrites
// innerHTML while streaming, so callers attach only at FINALIZATION.
function attachSpeak(msg, raw) {
  mediafy(msg); // same finalization-only contract (self-guards idempotently)
  if (!raw || !raw.trim() || msg.querySelector(".speak-btn")) return;
  msg._raw = raw.trim();
  const b = el("button", "cb-btn speak-btn", "🔊");
  b.title = "Read this reply aloud (replays are instant, no re-synthesis)";
  b.onclick = async (e) => {
    e.stopPropagation();
    // ■ STOP shows only while THIS bubble owns the voice (playing or mid-fetch)
    if (speakOwner === msg) { stopSpeaking(); return; } // pause; resume via 🔊/▶ stays free
    stopSpeaking(); // take the voice from any other bubble, drop pending queue
    await playBubble(msg); // resumes from pause — zero network for known clips
  };
  msg._speakBtn = b;
  msg.appendChild(b);
}

// 🔊 for a ◈ THINKING card: button + mini player ride in the <summary> so
// they work while the card is collapsed. A capture-phase preventDefault on
// their wrapper keeps clicks from toggling the card open/closed.
function attachBlockSpeak(block, raw) {
  let text = (raw || "").trim();
  if (!text || block._speakBtn) return;
  if (text.length > 19500) { // /api/tts caps at 20k chars
    const cut = text.lastIndexOf(".", 19500);
    text = text.slice(0, cut > 9750 ? cut + 1 : 19500);
  }
  block._raw = text;
  const wrap = el("span", "block-voice");
  wrap.addEventListener("click", (e) => e.preventDefault(), true);
  const b = el("button", "cb-btn speak-btn", "🔊");
  b.title = "Read this reasoning aloud";
  b.onclick = async (e) => {
    e.stopPropagation();
    if (speakOwner === block) { stopSpeaking(); return; }
    stopSpeaking();
    await playBubble(block);
  };
  wrap.appendChild(b);
  block._speakBtn = b;
  block._playerHost = wrap;
  block.querySelector("summary").appendChild(wrap);
}

// ---------- settings ----------
// config.save() merges the TOP level only — nested dicts must be sent whole,
// hence the spread-from-loaded base in saveSettings.
const DEFAULT_VOICE = {
  tts_provider: "gtts", tts_lang: "en", tts_tld: "com",
  stt_provider: "sr", stt_lang: "en-US",
  gcloud_key_file: "", gcloud_tts_lang: "en-US", gcloud_tts_voice: "en-US-Wavenet-J",
  pocket_voice: "alba", pocket_language: "english", pocket_threads: 2,
  tts_normalize: "pocket",
  speak_reasoning: "off",
};
let loadedVoice = { ...DEFAULT_VOICE };

// ---- Pocket voice picker: presets / my (cloned) voices / ＋ clone ----
const PV_CLONE = "__clone__";
let pvPrev = "alba";      // selection to fall back to when the clone form closes
let pvAudio = null;       // the ▶ preview player (one at a time)

function pvValue() {
  const v = $("#set-voice-pocket_voice").value;
  return v === PV_CLONE ? pvPrev : v;
}

async function pvLoad(selected) {
  let lib = { presets: [], saved: [] };
  try { lib = await (await fetch("/api/voices")).json(); } catch {}
  pvLib = lib;
  const sel = $("#set-voice-pocket_voice");
  sel.replaceChildren();
  const group = (label, items) => {
    const g = el("optgroup");
    g.label = label;
    for (const [value, text] of items) {
      const o = el("option", null, text);
      o.value = value;
      g.appendChild(o);
    }
    sel.appendChild(g);
  };
  if (lib.saved.length)
    group("MY VOICES", lib.saved.map((v) => [v.name, v.name + (v.language && v.language !== "english" ? ` (${v.language})` : "")]));
  group("PRESETS", lib.presets.map((n) => [n, n]));
  // a legacy free-text value (absolute path to a clip) stays selectable
  if (selected && ![...sel.options].some((o) => o.value === selected))
    group("CUSTOM", [[selected, "custom: " + selected.split("/").pop()]]);
  const add = el("option", null, "＋ Clone a new voice…");
  add.value = PV_CLONE;
  sel.appendChild(add);
  sel.value = selected || "alba";
  pvPrev = sel.value;
  pvSync();
}
let pvLib = { presets: [], saved: [] };

function pvSync() {
  const v = $("#set-voice-pocket_voice").value;
  const cloning = v === PV_CLONE;
  $("#pv-clone").classList.toggle("hidden", !cloning);
  $("#btn-pv-preview").disabled = cloning;
  $("#btn-pv-delete").classList.toggle("hidden", !pvLib.saved.some((x) => x.name === v));
  if (cloning) {
    $("#pv-status").textContent = pvLib.cloning_ready ? "" :
      "⚠ no Hugging Face login on this box yet — cloning will fail until you accept the terms + hf auth login";
    $("#pv-status").className = "pv-status" + (pvLib.cloning_ready ? "" : " warn");
    $("#pv-name").focus();
  } else pvPrev = v;
}

async function pvPreview() {
  const voice = pvValue();
  if (pvAudio) { pvAudio.pause(); pvAudio = null; }
  $("#btn-pv-preview").textContent = "…";
  try {
    const r = await fetch("/api/tts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: `Hi, I'm ${voice.replace(/_/g, " ")}. This is how I'd sound reading your LangBang replies.`,
        voice, language: $("#set-voice-pocket_language").value,
      }),
    });
    const out = await r.json().catch(() => ({}));
    if (!r.ok || !out.stream) throw new Error(out.detail || "HTTP " + r.status);
    pvAudio = new Audio(out.stream);
    pvAudio.onended = () => { $("#btn-pv-preview").textContent = "▶"; };
    await pvAudio.play();
    $("#btn-pv-preview").textContent = "■";
  } catch (e) {
    $("#btn-pv-preview").textContent = "▶";
    $("#pv-status").textContent = "✕ preview failed: " + e.message;
    $("#pv-status").className = "pv-status warn";
    SFX.play("error");
  }
}

async function pvClone() {
  const name = $("#pv-name").value.trim();
  const file = $("#pv-file").files[0];
  const st = $("#pv-status");
  const fail = (m) => { st.textContent = "✕ " + m; st.className = "pv-status warn"; SFX.play("error"); };
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/.test(name)) return fail("name: letters, digits, _ or - (max 40)");
  if (!file) return fail("pick an audio file");
  if (!$("#pv-consent").checked) return fail("confirm you have permission to clone this voice");
  const fd = new FormData();
  fd.append("name", name);
  fd.append("language", $("#set-voice-pocket_language").value);
  fd.append("consent", "true");
  fd.append("file", file);
  $("#btn-pv-clone").disabled = true;
  st.textContent = "⏳ cloning… (loads the cloning model once, ~10 s)";
  st.className = "pv-status";
  try {
    const r = await fetch("/api/voices", { method: "POST", body: fd });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) return fail(out.detail || "HTTP " + r.status);
    await pvLoad(out.name);        // selected — SAVE makes it the reading voice
    cfgDirtyCompute();
    $("#pv-name").value = ""; $("#pv-file").value = ""; $("#pv-consent").checked = false;
    st.textContent = `✓ saved “${out.name}” — ▶ to hear it, SAVE to use it`;
    st.className = "pv-status ok";
    SFX.play("message_received");
  } catch (e) {
    fail(e.message);
  } finally {
    $("#btn-pv-clone").disabled = false;
  }
}

async function pvDelete() {
  const name = $("#set-voice-pocket_voice").value;
  const b = $("#btn-pv-delete");
  if (!b.classList.contains("arm")) { // two-click confirm (no confirm() in this app)
    b.classList.add("arm"); b.textContent = "✕ sure?";
    setTimeout(() => { b.classList.remove("arm"); b.textContent = "✕"; }, 3000);
    return;
  }
  b.classList.remove("arm"); b.textContent = "✕";
  const r = await fetch("/api/voices/" + encodeURIComponent(name), { method: "DELETE" });
  if (!r.ok) { SFX.play("error"); return; }
  if (loadedVoice.pocket_voice === name) loadedVoice.pocket_voice = "alba"; // server reset it too
  await pvLoad(pvPrev === name ? "alba" : pvPrev);
  cfgDirtyCompute();
}

$("#set-voice-pocket_voice").addEventListener("change", pvSync);
$("#btn-pv-preview").onclick = () => {
  if (pvAudio && !pvAudio.paused) { pvAudio.pause(); $("#btn-pv-preview").textContent = "▶"; return; }
  pvPreview();
};
$("#btn-pv-clone").onclick = pvClone;
$("#btn-pv-cancel").onclick = () => { $("#set-voice-pocket_voice").value = pvPrev; pvSync(); cfgDirtyCompute(); };
$("#btn-pv-delete").onclick = pvDelete;

async function openSettings() {
  const s = await api.settings();
  provLoad(s);
  for (const k of ["model", "temperature", "max_tokens", "max_react_iterations", "system_prompt",
                   "compact_trigger_tokens", "compact_keep_messages", "compact_summary_tokens"])
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
  mcpDraft = JSON.parse(JSON.stringify(s.mcp_servers || {}));
  mcpEditing = null;
  for (const k of Object.keys(mcpTestState)) delete mcpTestState[k];
  mcpShowEditor(false);
  mcpRenderTest(null, $("#mcp-test-result"));
  $("#mcp-import-msg").classList.add("hidden");
  mcpRenderList();
  renderSoundboard(); // empty board while the slot list is in flight
  try { sndSlots = await api.soundsSlots(); } catch { sndSlots = []; }
  renderSoundboard();
  sndStatusSweep();
  $("#set-vision").checked = !!(s.capabilities || {}).vision;
  $("#set-splash_image").value = s.splash_image || "";
  $("#set-thinking").checked = !!s.enable_thinking;
  $("#set-reasoning_effort").value = s.reasoning_effort || "xhigh";
  $("#set-keep_reasoning").value = s.keep_reasoning || "off";
  $("#set-reasoning_effort").disabled = !s.enable_thinking;
  $("#set-compact").checked = !!s.compact_enabled;
  $("#set-deep_agent").checked = !!s.deep_agent;
  $("#set-skills_enabled").checked = !!s.skills_enabled;
  const rv = { enabled: true, min_tool_calls: 6, scheduled: false, ...(s.skill_review || {}) };
  $("#set-memory_enabled").checked = s.memory_enabled !== false;
  $("#set-review-enabled").checked = !!rv.enabled;
  $("#set-review-scheduled").checked = !!rv.scheduled;
  $("#set-review-min").value = rv.min_tool_calls;
  $("#btn-review-now").disabled = !threadId;
  $("#review-msg").classList.add("hidden");
  refreshMemoryUI(true);
  loadedVoice = { ...DEFAULT_VOICE, ...(s.voice || {}) };
  await pvLoad(loadedVoice.pocket_voice); // options must exist before .value is set
  notifyLoadForm(s.notify || {});
  const ig = s.image_gen || {};
  $("#set-img-comfy_url").value = ig.comfy_url || "http://spark-ee93:8188";
  $("#set-img-out_dir").value = ig.out_dir || "~/Pictures/langbang";
  $("#set-img-steps").value = ig.steps || 25;
  for (const k of Object.keys(DEFAULT_VOICE))
    $("#set-voice-" + k).value = loadedVoice[k];
  $("#cfg-confirm").classList.add("hidden");
  cfgSnapshot(); // after every field is populated: this is the unsaved-baseline
  $("#settings-panel").classList.remove("hidden");
  SFX.play("click");
}

async function saveSettings() {
  const err = $("#settings-error");
  err.classList.add("hidden");
  // mid-editor edits live only in mcpDraft fields — don't silently drop them
  if (!$("#mcp-editor").classList.contains("hidden")) {
    err.textContent = "MCP editor is open — SAVE SERVER or CANCEL EDIT first.";
    err.classList.remove("hidden");
    return;
  }
  const provs = provForm();
  const badProv = Object.entries(provs).find(([, p]) => !/^https?:\/\//.test(p.base_url));
  if (!Object.keys(provs).length || badProv) {
    err.textContent = badProv ? `provider "${badProv[0]}" needs a base URL (http://…/v1)` : "at least one provider is required";
    err.classList.remove("hidden");
    return;
  }
  const local_tools = {};
  document.querySelectorAll("#set-tools input").forEach((cb) => {
    local_tools[cb.dataset.tool] = cb.checked;
  });
  await api.saveSettings({
    providers: provForm(),
    provider: $("#set-provider").value,
    model: $("#set-model").value.trim(),
    temperature: parseFloat($("#set-temperature").value),
    max_tokens: parseInt($("#set-max_tokens").value),
    max_react_iterations: parseInt($("#set-max_react_iterations").value),
    system_prompt: $("#set-system_prompt").value,
    capabilities: { vision: $("#set-vision").checked },
    splash_image: $("#set-splash_image").value.trim(),
    enable_thinking: $("#set-thinking").checked,
    reasoning_effort: $("#set-reasoning_effort").value || "xhigh",
    keep_reasoning: $("#set-keep_reasoning").value || "off",
    compact_enabled: $("#set-compact").checked,
    deep_agent: $("#set-deep_agent").checked,
    skills_enabled: $("#set-skills_enabled").checked,
    memory_enabled: $("#set-memory_enabled").checked,
    skill_review: { // complete dict (top-level merge)
      enabled: $("#set-review-enabled").checked,
      scheduled: $("#set-review-scheduled").checked,
      min_tool_calls: Math.max(1, Math.min(200, parseInt($("#set-review-min").value, 10) || 6)),
    },
    compact_trigger_tokens: parseInt($("#set-compact_trigger_tokens").value) || 0,
    compact_keep_messages: parseInt($("#set-compact_keep_messages").value) || 20,
    compact_summary_tokens: parseInt($("#set-compact_summary_tokens").value) || 800,
    local_tools,
    mcp_servers: mcpDraft,
    // complete dict — the server shallow-merges top-level keys only
    voice: {
      ...loadedVoice,
      tts_provider: $("#set-voice-tts_provider").value,
      tts_lang: $("#set-voice-tts_lang").value.trim() || "en",
      tts_tld: $("#set-voice-tts_tld").value.trim() || "com",
      stt_provider: $("#set-voice-stt_provider").value,
      stt_lang: $("#set-voice-stt_lang").value.trim() || "en-US",
      gcloud_key_file: $("#set-voice-gcloud_key_file").value.trim(),
      gcloud_tts_lang: $("#set-voice-gcloud_tts_lang").value.trim() || "en-US",
      gcloud_tts_voice: $("#set-voice-gcloud_tts_voice").value.trim() || "en-US-Wavenet-J",
      pocket_voice: pvValue() || "alba",
      pocket_language: $("#set-voice-pocket_language").value || "english",
      pocket_threads: Math.max(1, Math.min(8, parseInt($("#set-voice-pocket_threads").value, 10) || 2)),
      tts_normalize: $("#set-voice-tts_normalize").value || "pocket",
      speak_reasoning: $("#set-voice-speak_reasoning").value || "off",
    },
    notify: notifyForm(),
    image_gen: {
      comfy_url: $("#set-img-comfy_url").value.trim() || "http://spark-ee93:8188",
      out_dir: $("#set-img-out_dir").value.trim() || "~/Pictures/langbang",
      steps: Math.max(4, Math.min(60, parseInt($("#set-img-steps").value, 10) || 25)),
    },
  });
  applySplash($("#set-splash_image").value.trim());
  // runs read speak_reasoning from loadedVoice — take the saved value now
  loadedVoice = { ...loadedVoice, speak_reasoning: $("#set-voice-speak_reasoning").value || "off" };
  $("#settings-panel").classList.add("hidden");
  cfgBase = null;  // the save closed the draft; next openSettings re-snapshots
  $("#cfg-dirty").classList.add("hidden");
  $("#cfg-confirm").classList.add("hidden");
  SFX.play("settings_saved");
  checkHealth();
}

// ---------- CONFIG dirty tracking / close paths ----------
// openSettings refetches and rebuilds everything on every open, so DISCARD
// is just "close" — the snapshot taken here defines unsaved. Covers static
// fields, the JS-built tool checkboxes, the voice selects, and the MCP draft
// (whose edits live only in mcpDraft, so its JSON rides the same Map).
let cfgBase = null;
// #mcp-list/#mcp-editor/#soundboard-list rebuild their DOM wholesale on
// every action — their inputs can't be identity-keyed snapshot members (the
// MCP state rides the __mcp JSON instead; the soundboard saves per-cue)
// #pv-clone is a one-shot form (clone saves itself) — not a setting
const cfgMember = (el0) => !el0.closest("#mcp-list,#mcp-editor,#soundboard-list,#pv-clone,#prov-list");
function cfgDirtyCompute() {
  if (!cfgBase) return;
  let n = 0;
  for (const el0 of document.querySelectorAll(
      "#settings-panel input,#settings-panel textarea,#settings-panel select")) {
    if (!cfgMember(el0)) continue;
    const now = el0.type === "checkbox" ? el0.checked : el0.value;
    const diff = !cfgBase.has(el0) || cfgBase.get(el0) !== now;
    const host = el0.closest("label") || el0; // .chk labels: color the whole row
    el0.classList.toggle("changed", diff);
    host.classList.toggle("changed", diff);
    if (diff) n++;
  }
  if (cfgBase.get("__mcp") !== JSON.stringify(mcpDraft)) n++;
  if (cfgBase.get("__prov") !== JSON.stringify(provForm())) n++;
  const bar = $("#cfg-dirty");
  bar.classList.toggle("hidden", n === 0);
  if (n) bar.textContent = "● " + n + " UNSAVED";
}
function cfgSnapshot() {
  cfgBase = new Map();
  for (const el0 of document.querySelectorAll(
      "#settings-panel input,#settings-panel textarea,#settings-panel select"))
    if (cfgMember(el0)) cfgBase.set(el0, el0.type === "checkbox" ? el0.checked : el0.value);
  cfgBase.set("__mcp", JSON.stringify(mcpDraft));
  cfgBase.set("__prov", JSON.stringify(provForm()));
  cfgDirtyCompute();  // fresh baseline => clean; also clears a stale N-UNSAVED span
}
// ESC / CANCEL / backdrop all route here: clean closes are instant; dirty
// ones reveal the save|discard bar instead of silently eating the edits
// (no window.confirm — inline bar matches the app idiom).
function closeSettings(force) {
  const hidden = $("#settings-error").classList.contains("hidden");
  if (!force && !hidden) { return; } // server rejected a value — stay open
  if (!force && !$("#cfg-dirty").classList.contains("hidden")) {
    $("#cfg-confirm").classList.remove("hidden");
    $("#cfg-confirm-n").textContent = $("#cfg-dirty").textContent.match(/\d+/)[0];
    return;
  }
  $("#cfg-confirm").classList.add("hidden");
  $("#settings-panel").classList.add("hidden");
}

// ---------- mcp server manager ----------
// settings.mcp_servers is {name: cfg} — the dict KEY is the server identity,
// "disabled": true parks one (server mcp._active strips the flag). mcpDraft
// is a staged deep copy living while CONFIG is open; rows/editor/import all
// mutate it and nothing reaches the server until CONFIG SAVE, which must send
// the COMPLETE dict (config.save shallow-merges top-level keys only).
let mcpDraft = {};
let mcpEditing = null;    // original key under edit; null = editor closed/adding
let mcpArgs = [];         // stdio argument strings while the editor is open
const mcpTestState = {};  // name -> {ok, tools|error}; display-only, not persisted

const mcpTarget = (cfg) =>
  cfg.transport === "stdio"
    ? [cfg.command || "", ...(Array.isArray(cfg.args) ? cfg.args : [])].join(" ")
    : cfg.url || "";

function mcpShowEditor(on) {
  $("#mcp-editor").classList.toggle("hidden", !on);
  $("#btn-mcp-editor-save").classList.toggle("hidden", !on);
  $("#btn-mcp-editor-cancel").classList.toggle("hidden", !on);
  $("#btn-mcp-add").classList.toggle("hidden", on);
  $("#btn-mcp-import").classList.toggle("hidden", on);
}

function mcpRenderTest(res, into) {
  if (!res) {
    into.textContent = "";
    into.className = "mcp-test hidden";
    return;
  }
  into.classList.remove("hidden");
  if (res.ok) {
    const shown = res.tools.slice(0, 8).join(", ");
    into.textContent = "✓ " + res.tools.length + " tools: " + shown +
      (res.tools.length > 8 ? " +" + (res.tools.length - 8) + " more" : "");
    into.className = "mcp-test ok";
  } else {
    into.textContent = "✕ " + res.error;
    into.className = "mcp-test bad";
  }
}

async function mcpRunTest(cfg, btn, into) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "… TESTING";
  mcpRenderTest(null, into);
  let res;
  try { res = await api.mcpTest(cfg); }
  catch (e) { res = { ok: false, error: "request failed: " + e.message }; }
  btn.disabled = false;
  btn.textContent = label;
  mcpRenderTest(res, into);
  return res;
}

function mcpRenderArgs() {
  const box = $("#mcp-args");
  box.innerHTML = "";
  mcpArgs.forEach((a, i) => {
    const row = el("div", "mcp-arg-row");
    const inp = el("input");
    inp.type = "text"; inp.value = a; inp.spellcheck = false;
    inp.oninput = () => { mcpArgs[i] = inp.value; };
    const del = el("button", "btn ghost sm", "✕");
    del.onclick = () => { SFX.play("click"); mcpArgs.splice(i, 1); mcpRenderArgs(); };
    row.append(inp, del);
    box.appendChild(row);
  });
}

function mcpRenderList() {
  const box = $("#mcp-list");
  box.innerHTML = "";
  const names = Object.keys(mcpDraft);
  if (!names.length) {
    box.appendChild(el("div", "mcp-empty", "No MCP servers. ADD SERVER below."));
    cfgDirtyCompute();
    return;
  }
  const mkBtn = (txt, title, fn) => {
    const b = el("button", "btn ghost sm", txt);
    b.title = title;
    b.onclick = () => { SFX.play("click"); fn(b); };
    return b;
  };
  for (const name of names) {
    const cfg = mcpDraft[name];
    const off = !!cfg.disabled;
    const row = el("div", "mcp-row" + (off ? " off" : ""));
    const head = el("div", "mcp-head");
    const lab = el("label", "chk");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !off;
    cb.title = "enabled";
    cb.onchange = () => {
      if (cb.checked) delete mcpDraft[name].disabled;
      else mcpDraft[name].disabled = true;
      mcpRenderList();
    };
    lab.append(cb, el("span", "mcp-name", name));
    head.appendChild(lab);
    const sub = el("div", "mcp-sub" + (off ? " dim" : ""), mcpTarget(cfg) || "—");
    const testLine = el("div", "mcp-test hidden");
    mcpRenderTest(mcpTestState[name], testLine);
    const acts = el("div", "mcp-actions");
    acts.append(
      mkBtn("↻", "test connection", (b) =>
        mcpRunTest(cfg, b, testLine).then((res) => { mcpTestState[name] = res; })),
      mkBtn("✎ EDIT", "edit", () => mcpOpenEditor(name)),
      mkBtn("🗑 DELETE", "delete", () => {
        delete mcpDraft[name];
        delete mcpTestState[name];
        mcpRenderList();
      }),
    );
    row.append(head, sub, testLine, acts);
    box.appendChild(row);
  }
  cfgDirtyCompute(); // every MCP mutation funnels through a re-render
}

function mcpSyncTransportRows() {
  const stdio = $("#mcp-transport").value === "stdio";
  $("#mcp-url-row").classList.toggle("hidden", stdio);
  $("#mcp-stdio-rows").classList.toggle("hidden", !stdio);
}

function mcpFormCfg() {
  // transport-specific fields only; commit() merges these over a passthrough
  // copy of the original config, so unknown keys (env, headers…) survive
  const transport = $("#mcp-transport").value;
  if (transport === "stdio") {
    return {
      transport,
      command: $("#mcp-command").value.trim(),
      args: mcpArgs.map((s) => s.trim()).filter((s) => s),
    };
  }
  return { transport, url: $("#mcp-url").value.trim() };
}

function mcpNormalizeCfg(c) {
  let transport = c.transport || "";
  if (!transport) {
    transport = c.type === "http" || c.type === "streamable_http" ? "streamable_http"
      : c.type === "sse" ? "sse" : c.command ? "stdio" : "streamable_http";
  }
  const out = { transport };
  if (transport === "stdio") {
    out.command = c.command || "";
    if (Array.isArray(c.args)) out.args = c.args;
    if (c.env) out.env = c.env;
    if (c.cwd) out.cwd = c.cwd;
  } else {
    out.url = c.url || "";
    if (c.headers) out.headers = c.headers;
    if (c.timeout !== undefined) out.timeout = c.timeout;
  }
  if (c.disabled) out.disabled = true;
  return out;
}

function mcpOpenEditor(key) {
  mcpEditing = key;
  const cfg = key ? mcpDraft[key] : { transport: "streamable_http" };
  $("#mcp-error").classList.add("hidden");
  $("#mcp-name").value = key || "";
  $("#mcp-transport").value = cfg.transport === "sse" || cfg.transport === "stdio" ? cfg.transport : "streamable_http";
  $("#mcp-url").value = cfg.url || "";
  $("#mcp-command").value = cfg.command || "";
  mcpArgs = Array.isArray(cfg.args) ? [...cfg.args] : [];
  mcpRenderArgs();
  mcpSyncTransportRows();
  mcpRenderTest(key ? mcpTestState[key] : null, $("#mcp-test-result"));
  mcpShowEditor(true);
  $("#btn-mcp-editor-save").textContent = key ? "SAVE SERVER" : "ADD SERVER";
  $("#mcp-name").focus();
  $("#mcp-editor").scrollIntoView({ block: "nearest" });
}

function mcpCommit() {
  const err = $("#mcp-error");
  err.classList.add("hidden");
  const fail = (msg) => { err.textContent = msg; err.classList.remove("hidden"); };
  const name = $("#mcp-name").value.trim();
  if (!name) return fail("Name is required.");
  const allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._- ";
  if ([...name].some((c) => !allowed.includes(c)))
    return fail("Name: letters, digits, space, . _ - only.");
  if (name !== $("#mcp-name").value) return fail("Name can't start or end with a space.");
  if (name !== mcpEditing && mcpDraft[name])
    return fail('A server named "' + name + '" already exists.');
  const form = mcpFormCfg();
  if (form.transport === "stdio" ? !form.command : !form.url)
    return fail(form.transport === "stdio" ? "Command is required." : "URL is required.");
  const out = { ...(mcpEditing ? mcpDraft[mcpEditing] : {}) };
  delete out.url;
  delete out.command;
  delete out.args;
  Object.assign(out, form);
  if (mcpEditing && mcpEditing !== name) {
    delete mcpDraft[mcpEditing];
    delete mcpTestState[mcpEditing]; // a stale ✓ must not follow the config
  }
  mcpDraft[name] = out;
  mcpEditing = null;
  mcpShowEditor(false);
  mcpRenderList();
}

$("#btn-mcp-add").onclick = () => { SFX.play("click"); mcpOpenEditor(null); };
$("#btn-mcp-editor-save").onclick = () => { SFX.play("click"); mcpCommit(); };
$("#btn-mcp-editor-cancel").onclick = () => {
  SFX.play("click");
  mcpEditing = null;
  mcpShowEditor(false);
};
$("#btn-mcp-arg").onclick = () => { SFX.play("click"); mcpArgs.push(""); mcpRenderArgs(); };
$("#mcp-transport").onchange = () => {
  mcpSyncTransportRows();
  mcpRenderTest(null, $("#mcp-test-result"));
};
$("#btn-mcp-test").onclick = async () => {
  SFX.play("click");
  const form = mcpFormCfg();
  if (form.transport === "stdio" ? !form.command : !form.url) {
    mcpRenderTest({
      ok: false,
      error: "fill in " + (form.transport === "stdio" ? "the command" : "the URL") + " first",
    }, $("#mcp-test-result"));
    return;
  }
  const res = await mcpRunTest(form, $("#btn-mcp-test"), $("#mcp-test-result"));
  if (mcpEditing) mcpTestState[mcpEditing] = res;
};
$("#btn-mcp-import").onclick = () => { SFX.play("click"); $("#mcp-import-file").click(); };
$("#mcp-import-file").onchange = (e) => {
  const f = e.target.files[0];
  e.target.value = ""; // allow re-selecting the same file
  if (!f) return;
  const msg = $("#mcp-import-msg");
  const show = (txt, good) => {
    msg.textContent = txt;
    msg.className = "mcp-test " + (good ? "ok" : "bad");
  };
  const rd = new FileReader();
  rd.onload = () => {
    let obj;
    try { obj = JSON.parse(rd.result); }
    catch { show("✕ import: not valid JSON", false); return; }
    // Claude-Desktop style {mcpServers:{...}} or a bare name→config dict
    const map = obj && obj.mcpServers && typeof obj.mcpServers === "object"
      ? obj.mcpServers : obj;
    const bad = !map || typeof map !== "object" || Array.isArray(map) ||
      !Object.keys(map).length ||
      Object.values(map).some(
        (c) => !c || typeof c !== "object" || !(c.transport || c.type || c.command || c.url));
    if (bad) {
      show('✕ import: expected { "mcpServers": { … } } or a bare name→config dict', false);
      return;
    }
    const names = Object.keys(map);
    const over = names.filter((n) => mcpDraft[n]);
    // Claude Desktop uses type:"stdio"|"http"; the adapter needs "transport"
    // and REJECTS unknown keys — normalize + whitelist instead of verbatim copy
    for (const n of names) mcpDraft[n] = mcpNormalizeCfg(map[n]);
    names.forEach((n) => { delete mcpTestState[n]; });
    show("✓ imported " + names.length + " server" + (names.length > 1 ? "s" : "") +
      ": " + names.join(", ") + (over.length ? " (" + over.length + " overwritten)" : "") +
      " — press SAVE to apply", true);
    mcpRenderList();
  };
  rd.readAsText(f);
};

// ---------- soundboard (CONFIG → SOUNDBOARD) ----------
// One row per cue in sfxgen.CUES (GET /api/sounds/slots). ▶ PLAY auditions a
// cue with ♪ SOUND off (SFX.play force flag); ↻ REGEN POSTs a fire-and-poll
// re-render (committed prompt, fresh seed). The render is a background task
// server-side — it can sit minutes in the all-media queue — so the client
// polls status every 2s instead of holding the POST open. On success the live
// sfx.js buffer is swapped (SFX.reload), so the next play anywhere hears the
// new take without a page reload.
const SND_GEN_MSG = "… GENERATING — queued behind other media jobs (~1 min)";
let sndSlots = [];   // slots_view() rows; refreshed every CONFIG open
const sndRows = {};  // slot -> {row, sub, line, btn}; rebuilt per render
const sndTimers = {};// slot -> status-poll interval, while a regen runs

function sndSub(s) { return [s.when || s.slot, s.file, fmtBytes(s.bytes)].join(" · "); }

function sndLine(slot, kind, text) {
  // kind: "" clear | "gen" neutral | "ok" | "bad"
  const r = sndRows[slot];
  if (!r) return;
  if (!kind) { r.line.textContent = ""; r.line.className = "mcp-test hidden"; return; }
  r.line.textContent = text;
  r.line.className = "mcp-test" + (kind === "gen" ? "" : " " + kind);
}

function sndBusy(slot, on) {
  const r = sndRows[slot];
  if (!r) return;
  r.btn.disabled = on;
  r.btn.textContent = on ? "… REGEN" : "↻ REGEN";
}

function renderSoundboard() {
  const box = $("#soundboard-list");
  box.innerHTML = "";
  for (const k of Object.keys(sndRows)) delete sndRows[k];
  if (!sndSlots.length) {
    box.appendChild(el("div", "mcp-empty", "No cues loaded."));
    return;
  }
  const mk = (txt, title, fn, clickSfx) => {
    const b = el("button", "btn ghost sm", txt);
    b.title = title;
    b.onclick = () => { if (clickSfx) SFX.play("click"); fn(b); };
    return b;
  };
  for (const s of sndSlots) {
    const row = el("div", "mcp-row" + (s.exists ? "" : " off"));
    row.title = s.prompt; // the exact generation brief this slot re-renders from
    const head = el("div", "mcp-head");
    head.appendChild(el("span", "mcp-name", s.slot));
    const sub = el("div", "mcp-sub" + (s.exists ? "" : " dim"), sndSub(s));
    const line = el("div", "mcp-test hidden");
    const acts = el("div", "mcp-actions");
    const btn = mk("↻ REGEN",
      "re-render this cue on the all-media server (committed prompt, fresh seed)",
      () => sndRegen(s.slot), true);
    acts.append(
      mk("▶ PLAY", "audition this cue — works even with ♪ SOUND off",
         () => SFX.play(s.slot, true), false), // no click cue on top of the audition
      btn,
    );
    row.append(head, sub, line, acts);
    box.appendChild(row);
    sndRows[s.slot] = { row, sub, line, btn };
  }
  // this tab is mid-regen on some slots (CONFIG closed and reopened): re-attach
  // busy visuals to the fresh DOM; the pollers already run and will update them
  for (const slot of Object.keys(sndTimers)) { sndBusy(slot, true); sndLine(slot, "gen", SND_GEN_MSG); }
}

function sndPoll(slot, ours) {
  if (sndTimers[slot]) return; // one poller per slot (sweep may start it too)
  sndTimers[slot] = setInterval(async () => {
    let st;
    try { st = await api.sfxRegenStatus(slot); } catch { return; } // transient; keep polling
    if (st && st.running) {
      // re-assert visuals each tick: keeps the row honest even when the poller
      // was started by the open-sweep or an "already regenerating" POST
      sndBusy(slot, true);
      sndLine(slot, "gen", SND_GEN_MSG);
      return;
    }
    clearInterval(sndTimers[slot]);
    delete sndTimers[slot];
    sndBusy(slot, false);
    if (!st) return;
    if (st.ok) {
      sndLine(slot, "ok", "✓ new take — seed " + st.seed + ", " + st.duration_s +
        "s (" + fmtBytes(st.bytes) + ")");
      SFX.reload(slot); // swap the live buffer: next play = new take, no reload needed
      if (ours) SFX.play(slot, true); // auto-audition only when we pressed REGEN
      const s = sndSlots.find((x) => x.slot === slot);
      if (s) { s.bytes = st.bytes; s.exists = true; }
      const r = sndRows[slot];
      if (s && r) { r.sub.textContent = sndSub(s); r.row.classList.remove("off"); }
    } else if (st.error) sndLine(slot, "bad", "✕ " + st.error);
    else sndLine(slot, "", "");
  }, 2000);
}

async function sndRegen(slot) {
  sndBusy(slot, true);
  sndLine(slot, "gen", SND_GEN_MSG);
  let res;
  try { res = await api.sfxRegen(slot); }
  catch (e) { res = { ok: false, error: "request failed: " + e.message }; }
  if (!res.ok) {
    sndBusy(slot, false);
    sndLine(slot, "bad", "✕ " + res.error);
    // "already regenerating" means a job IS running — let the poller prove it
  }
  sndPoll(slot, res.ok);
}

// CONFIG just opened: ask the server about every slot so rows resume a running
// regen started earlier (or from another tab), and show the last take's result.
function sndStatusSweep() {
  for (const s of sndSlots) {
    api.sfxRegenStatus(s.slot).then((st) => {
      const r = sndRows[s.slot];
      if (!r) return;
      if (st.running) {
        sndBusy(s.slot, true);
        sndLine(s.slot, "gen", SND_GEN_MSG);
        sndPoll(s.slot, false);
      } else if (st.ok) {
        sndLine(s.slot, "ok", "✓ new take — seed " + st.seed + ", " +
          st.duration_s + "s (" + fmtBytes(st.bytes) + ")");
      } else if (st.error) sndLine(s.slot, "bad", "✕ " + st.error);
    }).catch(() => {});
  }
}

// ---------- scheduled tasks ----------
const SCHED_PRESETS = [
  ["*/15 * * * *", "15 min"], ["0 * * * *", "hourly"], ["0 9 * * *", "daily 9:00"],
  ["0 9 * * 1-5", "weekdays 9:00"], ["0 9 * * 1", "mondays 9:00"],
];
let schedEditing = null; // null = creating; id = editing that task
let schedKind = "repeat"; // "repeat" (cron) | "once" (run_at)
// <input type=datetime-local> speaks local "YYYY-MM-DDTHH:MM"
const toLocalInput = (ts) => {
  const d = new Date(ts * 1000), p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
};
const fromLocalInput = (v) => (v ? new Date(v).getTime() / 1000 : null);
function schedSetKind(kind) {
  schedKind = kind;
  $("#sched-kind-repeat").classList.toggle("on", kind === "repeat");
  $("#sched-kind-once").classList.toggle("on", kind === "once");
  $("#sched-repeat-rows").classList.toggle("hidden", kind !== "repeat");
  $("#sched-once-rows").classList.toggle("hidden", kind !== "once");
  if (kind === "once" && !$("#sched-at").value)
    $("#sched-at").value = toLocalInput(Math.ceil(Date.now() / 3600000) * 3600 + 3600); // next full hour +1h
  schedPreviewNow();
}
let schedTimer = null; // panel-open poll: RUNNING badge / next_run stay live
let schedPrevT = null;

const schedWhen = (ts) =>
  ts ? new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

async function openSchedules() {
  SFX.play("click");
  schedEditing = null;
  schedShowEditor(false);
  await refreshSchedules();
  $("#sched-panel").classList.remove("hidden");
  clearInterval(schedTimer);
  schedTimer = setInterval(refreshSchedules, 5000);
}
function closeSchedules() {
  $("#sched-panel").classList.add("hidden");
  clearInterval(schedTimer);
}

async function refreshSchedules() {
  if ($("#sched-panel").classList.contains("hidden")) return;
  const list = await api.schedules();
  const box = $("#sched-list");
  box.innerHTML = "";
  if (!list.length) {
    box.appendChild(el("div", "sched-empty", "No tasks yet. + NEW TASK below."));
    return;
  }
  for (const s of list) {
    const card = el("div", "sched-card" + (s.running ? " running" : "") + (s.enabled ? "" : " off"));

    const head = el("div", "sched-head");
    const title = el("button", "sched-title", s.title);
    title.title = "Open this task's run-history thread";
    title.onclick = () => {
      closeSchedules();
      openThread(threads.find((x) => x.id === s.thread_id) || { id: s.thread_id, title: s.title });
    };
    head.appendChild(title);
    if (s.done_at) {
      head.appendChild(el("span", "sched-done", "✓ DONE"));
    } else {
      const enLab = el("label", "sched-en");
      const en = document.createElement("input");
      en.type = "checkbox";
      en.checked = !!s.enabled;
      en.onchange = async () => { await api.putSchedule(s.id, { enabled: en.checked }); refreshSchedules(); };
      enLab.append(en, document.createTextNode(" enabled"));
      head.appendChild(enLab);
    }
    card.appendChild(head);

    card.appendChild(el("div", "sched-sub", (s.cron ? "⏰ " : "① ") + s.human + (s.cron ? "  ·  " + s.cron : "")));
    card.appendChild(
      el("div", "sched-sub dim",
         (s.running ? "◉ RUNNING · " : "") +
         (s.done_at ? "ran " + schedWhen(s.last_run) + " · leaves this list " + schedWhen(s.expires_at)
          : (s.enabled ? "next " + schedWhen(s.next_run) : "paused") + " · last " + schedWhen(s.last_run))));

    const act = el("div", "sched-actions");
    const run = el("button", "btn ghost sm", "▶ RUN");
    run.title = "Fire one run now (does not shift the cron rhythm)";
    // optimistic .running: the server flips the flag asynchronously and a fast
  // run can finish between refresh ticks — without this the card may never
  // visibly say RUNNING. The 1.2s refresh (then 5s ticks) takes over truth.
  run.onclick = async () => {
    SFX.play("click");
    await api.runSchedule(s.id);
    card.classList.add("running");
    setTimeout(refreshSchedules, 1200);
  };
    const edit = el("button", "btn ghost sm", "EDIT");
    edit.onclick = () => { SFX.play("click"); schedEdit(s); };
    const del = el("button", "btn ghost sm", "✕ DELETE");
    del.title = "Delete the task and its run-history thread";
    del.onclick = async () => { SFX.play("click"); await api.delSchedule(s.id); refreshSchedules(); };
    act.append(run, edit, del);
    card.appendChild(act);
    box.appendChild(card);
  }
}

function schedShowEditor(on) {
  $("#sched-editor").classList.toggle("hidden", !on);
  $("#btn-sched-save").classList.toggle("hidden", !on);
}

function schedEdit(s) {
  schedEditing = s.id;
  $("#sched-editor-title").textContent = "EDIT TASK";
  $("#sched-title").value = s.title;
  $("#sched-prompt").value = s.prompt;
  $("#sched-cron").value = s.cron || "";
  // a DONE one-off opens with a fresh time: saving re-arms it
  $("#sched-at").value = s.run_at && !s.done_at ? toLocalInput(s.run_at) : "";
  schedShowEditor(true);
  schedSetKind(s.cron ? "repeat" : "once");
  $("#sched-editor").scrollIntoView({ block: "nearest" });
}

function schedNew() {
  SFX.play("click");
  schedEditing = null;
  $("#sched-editor-title").textContent = "NEW TASK";
  $("#sched-title").value = "";
  $("#sched-prompt").value = "";
  $("#sched-cron").value = "";
  $("#sched-at").value = "";
  $("#sched-preview").textContent = "";
  schedSetKind("repeat");
  schedShowEditor(true);
  $("#sched-title").focus();
}

async function saveScheduleTask() {
  const err = $("#sched-error");
  err.classList.add("hidden");
  const body = { title: $("#sched-title").value.trim(), prompt: $("#sched-prompt").value.trim() };
  if (schedKind === "once") body.run_at = fromLocalInput($("#sched-at").value);
  else body.cron = $("#sched-cron").value.trim();
  if (!body.title || !body.prompt || !(body.cron || body.run_at)) {
    err.textContent = "Title, prompt and " + (schedKind === "once" ? "a date/time" : "a cron cadence") + " are all required.";
    err.classList.remove("hidden");
    return;
  }
  const res = schedEditing ? await api.putSchedule(schedEditing, body) : await api.newSchedule(body);
  if (!res || res.detail || !res.id) {
    err.textContent = (res && res.detail) || "save failed";
    err.classList.remove("hidden");
    return;
  }
  schedEditing = null;
  schedShowEditor(false);
  SFX.play("settings_saved");
  refreshSchedules();
}

async function schedPreviewNow() {
  const cron = $("#sched-cron").value.trim();
  const pv = $("#sched-preview");
  clearTimeout(schedPrevT);
  if (schedKind === "once") {
    const at = fromLocalInput($("#sched-at").value);
    if (!at) { pv.textContent = ""; pv.classList.remove("bad"); return; }
    const mins = Math.round((at - Date.now() / 1000) / 60);
    const bad = mins < -1;
    const rel = mins < 60 ? `${mins} min` : mins < 2880 ? `${Math.floor(mins / 60)} h ${mins % 60} min` : `${Math.round(mins / 1440)} days`;
    pv.textContent = bad ? "✕ that time is in the past" : `☑ once at ${schedWhen(at)} · in ${rel}`;
    pv.classList.toggle("bad", bad);
    return;
  }
  if (!cron) { pv.textContent = ""; pv.classList.remove("bad"); return; }
  const r = await api.cronNext(cron);
  if (!r.ok) {
    pv.textContent = "✕ " + r.error;
    pv.classList.add("bad");
  } else {
    pv.textContent = "☑ " + r.human + " · next " + schedWhen(r.next);
    pv.classList.remove("bad");
  }
}

for (const [cron, label] of SCHED_PRESETS) {
  const b = el("button", "chip", label);
  b.title = cron;
  b.onclick = () => {
    SFX.play("click");
    $("#sched-cron").value = cron;
    schedPreviewNow();
  };
  $("#sched-chips").appendChild(b);
}
$("#sched-kind-repeat").onclick = () => { SFX.play("click"); schedSetKind("repeat"); };
$("#sched-kind-once").onclick = () => { SFX.play("click"); schedSetKind("once"); };
$("#sched-at").addEventListener("input", schedPreviewNow);
for (const [mins, label] of [[30, "in 30 min"], [60, "in 1 h"], [180, "in 3 h"], ["tm9", "tomorrow 9:00"]]) {
  const b = el("button", "chip", label);
  b.onclick = () => {
    SFX.play("click");
    let ts;
    if (mins === "tm9") { const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(9, 0, 0, 0); ts = d.getTime() / 1000; }
    else ts = Date.now() / 1000 + mins * 60;
    $("#sched-at").value = toLocalInput(ts);
    schedPreviewNow();
  };
  $("#sched-once-chips").appendChild(b);
}
$("#sched-cron").addEventListener("input", () => {
  clearTimeout(schedPrevT);
  schedPrevT = setTimeout(schedPreviewNow, 350);
});
$("#btn-schedules").onclick = () => { closeNav(); openSchedules(); };
$("#btn-sched-close").onclick = () => { SFX.play("click"); closeSchedules(); };
$("#btn-sched-new").onclick = schedNew;
$("#btn-sched-save").onclick = saveScheduleTask;

// ---------- boot ----------
// ---------- notifications: toasts, (n) title badge, desktop popups ----------
// The server records needs-input / done / failed for EVERY run (chat,
// scheduled, !cmd — server/notify.py) and pushes to ntfy when no visible tab
// shows that thread. Each tab polls the same feed with the 4 s runs poll and
// alerts for whatever you're not looking at.
let notifyCursor = -1; // -1 = fresh tab: take the cursor, don't replay history
let unseenNotes = 0;
const BASE_TITLE = document.title;
const NOTE_ICON = { input: "◆", done: "✓", failed: "✕", learned: "📘" };
const NOTE_WORD = { input: "NEEDS YOUR INPUT", done: "FINISHED", failed: "FAILED", learned: "LEARNED" };

async function pollNotify() {
  let r;
  try { r = await J(await fetch(`/api/notify/events?since=${notifyCursor}`)); } catch { return; }
  const fresh = notifyCursor >= 0 ? r.events || [] : [];
  notifyCursor = r.last;
  for (const ev of fresh) notifyShow(ev);
}

function notifyOpen(ev) {
  if (!ev.tid) return;
  openThread(threads.find((x) => x.id === ev.tid) || { id: ev.tid, title: ev.title });
}

function notifyShow(ev) {
  // you're looking at it — except "learned" (skill/memory changed): that's news
  if (ev.tid === threadId && !document.hidden && ev.kind !== "learned") return;
  const t = el("div", "toast " + ev.kind);
  t.append(el("div", "toast-head", `${NOTE_ICON[ev.kind] || "•"} ${NOTE_WORD[ev.kind] || ev.kind}`),
           el("div", "toast-title", ev.title || "LangBang"));
  if (ev.text) t.appendChild(el("div", "toast-text", ev.text));
  const x = el("button", "toast-x", "✕");
  x.onclick = (e) => { e.stopPropagation(); t.remove(); };
  t.appendChild(x);
  t.onclick = () => { t.remove(); notifyOpen(ev); };
  $("#toasts").appendChild(t);
  // needs-input waits for you; the rest fade on their own
  if (ev.kind !== "input") setTimeout(() => t.remove(), ev.kind === "failed" ? 30000 : 15000);
  while ($("#toasts").children.length > 4) $("#toasts").firstChild.remove();
  if (ev.kind === "learned") { refreshMemoryUI(); return; } // quiet: no sound/badge/popup
  SFX.play(ev.kind === "failed" ? "error" : "message_received");
  if (document.hidden) { unseenNotes++; document.title = `(${unseenNotes}) ${BASE_TITLE}`; }
  desktopNotify(ev);
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && unseenNotes) { unseenNotes = 0; document.title = BASE_TITLE; }
});

// desktop popups: per browser, and only in a secure context (https or
// http://localhost) — plain-http LAN/tailnet origins can't have them
const deskSupported = () => "Notification" in window && window.isSecureContext;
const deskOn = () => {
  try { return deskSupported() && Notification.permission === "granted" && localStorage.getItem("lb-desk-notify") === "on"; }
  catch { return false; }
};
function desktopNotify(ev) {
  if (!deskOn() || (!document.hidden && document.hasFocus())) return; // toast covers a focused tab
  try {
    const n = new Notification(`${ev.title || "LangBang"} — ${(NOTE_WORD[ev.kind] || ev.kind).toLowerCase()}`, {
      body: ev.text || "", tag: "lb-" + ev.id, requireInteraction: ev.kind === "input",
    });
    n.onclick = () => { window.focus(); notifyOpen(ev); n.close(); };
  } catch {}
}
function deskSync() {
  const b = $("#btn-desk-notify"), st = $("#desk-status");
  if (!deskSupported()) {
    b.disabled = true;
    st.textContent = `unavailable here — browsers only allow popups on https or http://localhost (this is ${location.origin}); toasts + phone push still work`;
    st.className = "pv-status warn";
    return;
  }
  b.disabled = false;
  if (Notification.permission === "denied") {
    b.disabled = true;
    st.textContent = "blocked in this browser's site settings for " + location.origin;
    st.className = "pv-status warn";
  } else if (deskOn()) {
    b.textContent = "DISABLE";
    st.textContent = "✓ on in this browser";
    st.className = "pv-status ok";
  } else {
    b.textContent = "ENABLE";
    st.textContent = "off in this browser";
    st.className = "pv-status";
  }
}
$("#btn-desk-notify").onclick = async () => {
  SFX.play("click");
  if (deskOn()) { localStorage.setItem("lb-desk-notify", "off"); deskSync(); return; }
  const p = await Notification.requestPermission(); // needs this click (user gesture)
  if (p === "granted") {
    localStorage.setItem("lb-desk-notify", "on");
    new Notification("LangBang", { body: "Desktop notifications are on for this browser." });
  }
  deskSync();
};

// CONFIG → NOTIFICATIONS form
function notifyLoadForm(n) {
  const ev = n.events || {};
  $("#set-notify-ev-input").checked = ev.input !== false;
  $("#set-notify-ev-done").checked = ev.done !== false;
  $("#set-notify-ev-failed").checked = ev.failed !== false;
  $("#set-notify-ntfy_enabled").checked = !!n.ntfy_enabled;
  $("#set-notify-ntfy_server").value = n.ntfy_server || "https://ntfy.sh";
  $("#set-notify-ntfy_topic").value = n.ntfy_topic || "";
  $("#set-notify-click_base").value = n.click_base || "";
  $("#set-notify-preview").checked = n.preview !== false;
  $("#ntfy-status").textContent = "";
  ntfyHowto();
  deskSync();
}
function notifyForm() {
  return {
    ntfy_enabled: $("#set-notify-ntfy_enabled").checked,
    ntfy_server: $("#set-notify-ntfy_server").value.trim() || "https://ntfy.sh",
    ntfy_topic: $("#set-notify-ntfy_topic").value.trim(),
    click_base: $("#set-notify-click_base").value.trim(),
    preview: $("#set-notify-preview").checked,
    events: {
      input: $("#set-notify-ev-input").checked,
      done: $("#set-notify-ev-done").checked,
      failed: $("#set-notify-ev-failed").checked,
    },
  };
}
function ntfyHowto() {
  const topic = $("#set-notify-ntfy_topic").value.trim();
  const server = $("#set-notify-ntfy_server").value.trim() || "https://ntfy.sh";
  $("#ntfy-howto").textContent = topic
    ? `Phone: install ntfy → ＋ Subscribe → topic “${topic}”` +
      (server.replace(/\/$/, "") === "https://ntfy.sh" ? "" : ` (use another server: ${server})`) + ". Then ▶ SEND TEST PUSH."
    : "Tick “send pushes” to get a private random topic.";
}
async function ntfyNewTopic() {
  try { $("#set-notify-ntfy_topic").value = (await J(await fetch("/api/notify/topic"))).topic; } catch {}
  ntfyHowto();
  cfgDirtyCompute();
}
$("#set-notify-ntfy_enabled").addEventListener("change", async () => {
  if (!$("#set-notify-ntfy_enabled").checked) return;
  if (!$("#set-notify-ntfy_topic").value.trim()) await ntfyNewTopic();
  // a tap on the push should open THIS LangBang — default to how this tab reaches it
  if (!$("#set-notify-click_base").value.trim()) $("#set-notify-click_base").value = location.origin;
  cfgDirtyCompute();
});
$("#set-notify-ntfy_topic").addEventListener("input", ntfyHowto);
$("#set-notify-ntfy_server").addEventListener("input", ntfyHowto);
$("#btn-ntfy-topic").onclick = () => { SFX.play("click"); ntfyNewTopic(); };
$("#btn-ntfy-test").onclick = async () => {
  SFX.play("click");
  const st = $("#ntfy-status");
  st.textContent = "sending…"; st.className = "pv-status";
  try {
    const r = await fetch("/api/notify/test", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(notifyForm()),
    });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.detail || "HTTP " + r.status);
    st.textContent = "✓ sent — check your phone"; st.className = "pv-status ok";
  } catch (e) {
    st.textContent = "✕ " + e.message; st.className = "pv-status warn";
  }
};

// ---------- stale-tab guard: offer a reload when the frontend changed ----------
// The first health reply pins the version this tab booted with; a later
// different one means web/ changed under an open tab (which otherwise keeps
// running the old JS forever — that's how inline media "didn't work" on a
// tab opened before the fix). LATER hides it until the NEXT change.
let bootUiVersion = null;
let dismissedUiVersion = null;
function checkUiVersion(v) {
  if (!v) return;
  if (bootUiVersion === null) { bootUiVersion = v; return; }
  $("#update-bar").classList.toggle("hidden", v === bootUiVersion || v === dismissedUiVersion);
  $("#update-bar").dataset.version = v;
}
function reloadForUpdate() {
  // carry the unsent draft + open thread across the reload
  try {
    sessionStorage.setItem("lb-draft", JSON.stringify({ tid: threadId, text: $("#input").value }));
  } catch {}
  const u = new URL(location.href);
  if (threadId) u.searchParams.set("thread", threadId);
  else u.searchParams.delete("thread");
  location.replace(u.toString());
}
$("#btn-update-reload").onclick = () => { SFX.play("click"); reloadForUpdate(); };
$("#btn-update-later").onclick = () => {
  SFX.play("click");
  dismissedUiVersion = $("#update-bar").dataset.version;
  $("#update-bar").classList.add("hidden");
};
(() => {
  // restore a draft stashed by reloadForUpdate (only into the same thread)
  let d = null;
  try { d = JSON.parse(sessionStorage.getItem("lb-draft") || "null"); sessionStorage.removeItem("lb-draft"); } catch {}
  const urlTid = new URLSearchParams(location.search).get("thread");
  if (d && d.text && (d.tid || null) === (urlTid || null)) $("#input").value = d.text;
})();

async function checkHealth() {
  const h = await api.health();
  const el2 = $("#health");
  el2.textContent = "LINK: " + (h.backend_up ? "ONLINE" : "OFFLINE");
  el2.className = "health " + (h.backend_up ? "up" : "down");
  defaultModel = { model: h.model, vision: !!h.supports_vision };
  if (!threadId) syncTModel(null); // a thread's tag/vision come from its override
  checkUiVersion(h.ui_version);
  // attach stays offered without vision: non-image files (and images as
  // plain files) ride the upload path — the agent opens them from disk
  if (!supportsVision && pendingImages.length) { pendingImages = []; renderAttachStrip(); }
}

$("#btn-send").onclick = () => {
  // STOP only while the VIEWED thread runs — other threads' runs are none of
  // this button's business (per-thread gating; RUNS.get is the single source)
  if (threadId && RUNS.get(threadId)) { stopGeneration(); return; }
  SFX.play("click");
  send();
};
$("#btn-new").onclick = () => { SFX.play("click"); newThread(); };
$("#btn-settings").onclick = () => { closeNav(); openSettings(); };
// phone drawer: ☰ toggles, scrim click closes (openThread/newThread also close)
$("#btn-nav").onclick = () => { SFX.play("click"); $("#app").classList.toggle("nav-open"); };
$("#nav-scrim").onclick = closeNav;
$("#btn-save").onclick = saveSettings;
$("#btn-cancel").onclick = () => { SFX.play("click"); closeSettings(false); };
// backdrop + ESC join CANCEL through closeSettings(false), which routes a
// dirty draft through the confirm bar instead of silently trashing it
$("#settings-panel").onclick = (e) => { if (e.target.id === "settings-panel") closeSettings(false); };
$("#cfg-confirm-save").onclick = () => { SFX.play("click"); saveSettings(); };
$("#cfg-confirm-discard").onclick = () => { SFX.play("click"); closeSettings(true); };
$("#cfg-confirm-keep").onclick = () => { SFX.play("click"); $("#cfg-confirm").classList.add("hidden"); };
// one delegated pair catches static fields AND the JS-built tool checkboxes
// (mcp/soundboard subtrees are excluded inside cfgDirtyCompute)
for (const t of ["input", "change"]) $("#settings-panel").addEventListener(t, cfgDirtyCompute);
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
  ingestFiles(e.target.files);
  e.target.value = ""; // allow re-selecting the same file
};
$("#tab-chat").onclick = () => { SFX.play("click"); showTab("chat"); };
$("#tab-traj").onclick = () => { SFX.play("click"); showTab("traj"); };

// ---------- recap (✦): LLM cold-resume summary of the open thread ----------
function closeRecap() {
  $("#recap-panel").classList.add("hidden");
}
let recapAbort = null;

async function openRecap() {
  if (!threadId) return;
  const tid = threadId;
  SFX.play("click");
  const body = $("#recap-body");
  $("#recap-head").textContent =
    "RECAP — " + $("#chat-title").textContent.replace(/^—|—$/g, "").trim();
  $("#recap-ts").textContent = "";
  body.innerHTML = '<p>⏳ SYNTHESIZING…</p>';
  $("#recap-panel").classList.remove("hidden");
  if (recapAbort) recapAbort.abort();
  recapAbort = new AbortController();
  const timer = setTimeout(() => recapAbort.abort(), 90000); // Spark may queue behind chat traffic
  try {
    const r = await fetch(`/api/threads/${tid}/recap`,
      { method: "POST", signal: recapAbort.signal });
    if (threadId !== tid) return; // switched mid-flight — result belongs to a dead view
    if (!r.ok) {
      let msg = "HTTP " + r.status;
      try { msg = (await r.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    const out = await r.json();
    if (threadId !== tid) return;
    setMarkdown(body, out.summary);
    $("#recap-ts").textContent =
      "GENERATED " + new Date(out.ts * 1000).toLocaleTimeString();
    SFX.play("message_received");
  } catch (e) {
    if (threadId !== tid) return;
    body.innerHTML = "";
    setMarkdown(body, (e.name === "AbortError"
      ? "✕ recap timed out (90 s) — the model may be busy; try again."
      : "✕ recap failed: " + e.message));
    SFX.play("error");
  } finally {
    clearTimeout(timer);
  }
}

// ---------- providers (CONFIG → MODEL) + per-thread model override ----------
// A provider = one OpenAI-compatible endpoint. CONFIG holds the list + the
// global default {provider, model}; any thread can override either (stored
// server-side in threads.model) — scheduled tasks inherit their thread's.
let defaultModel = { model: "", vision: false };
const shortModel = (m) => (m || "").split("/").pop();
function provRow(name = "", p = {}) {
  const row = el("div", "prov-row");
  const f = (label, cls, val, type = "text", ph = "") => {
    const lab = el("label", "", label + " ");
    const i = document.createElement("input");
    i.type = type; i.className = cls; i.value = val || ""; i.placeholder = ph;
    i.autocomplete = "off"; i.spellcheck = false;
    lab.appendChild(i); row.appendChild(lab); return i;
  };
  f("Name", "p-name", name, "text", "openrouter");
  f("Base URL", "p-url", p.base_url, "text", "http://host:8000/v1");
  f("API key", "p-key", p.api_key && p.api_key !== "none" ? p.api_key : "", "password", "none");
  f("Default model", "p-model", p.model, "text", "(global model)");
  const flags = el("div", "prov-flags");
  const chk = (label, cls, on, title) => {
    const lab = el("label", "chk"); lab.title = title;
    const c = document.createElement("input"); c.type = "checkbox"; c.className = cls; c.checked = !!on;
    lab.append(c, document.createTextNode(" " + label)); flags.appendChild(lab);
  };
  chk("vision", "p-vision", p.vision, "model accepts images");
  chk("sglang/vLLM template kwargs", "p-tk", p.template_kwargs !== false,
      "send chat_template_kwargs (Qwen thinking on/off + reasoning effort). Turn OFF for OpenAI, llama-server, Ollama — OpenAI rejects unknown args");
  const test = el("button", "btn ghost", "↻ TEST"); test.type = "button";
  const out = el("span", "prov-test", "");
  test.onclick = async () => {
    out.className = "prov-test busy"; out.textContent = "testing…";
    test.disabled = true;
    const r = await probeModels({ base_url: row.querySelector(".p-url").value.trim(),
                                  api_key: row.querySelector(".p-key").value.trim() });
    test.disabled = false;
    out.className = "prov-test " + (r.ok ? "ok" : "bad");
    out.textContent = r.ok ? "✓ " : "✕ ";
    out.textContent += r.ok ? `reachable — ${r.models.length} model(s): ${r.models.slice(0, 4).map(shortModel).join(", ")}${r.models.length > 4 ? "…" : ""}` : r.error;
    if (r.ok) { // offer them in this row's Default model field
      let dl = row.querySelector("datalist");
      if (!dl) { dl = document.createElement("datalist"); dl.id = "dl-prov-" + Math.random().toString(36).slice(2); row.appendChild(dl); }
      dl.innerHTML = ""; for (const m of r.models) dl.appendChild(new Option(m, m));
      row.querySelector(".p-model").setAttribute("list", dl.id);
    }
  };
  const x = el("button", "btn ghost", "✕"); x.type = "button"; x.title = "remove provider";
  x.onclick = () => { row.remove(); provSyncSelect(); cfgDirtyCompute(); };
  flags.append(test, out, x);
  row.appendChild(flags);
  row.addEventListener("input", (e) => { if (e.target.classList.contains("p-name")) provSyncSelect(); });
  return row;
}
function provForm() {
  const out = {};
  for (const r of document.querySelectorAll("#prov-list .prov-row")) {
    const name = r.querySelector(".p-name").value.trim();
    if (!name) continue;
    out[name] = {
      base_url: r.querySelector(".p-url").value.trim(),
      api_key: r.querySelector(".p-key").value.trim() || "none",
      model: r.querySelector(".p-model").value.trim(),
      vision: r.querySelector(".p-vision").checked,
      template_kwargs: r.querySelector(".p-tk").checked,
    };
  }
  return out;
}
function provSyncSelect(want) {
  const sel = $("#set-provider"), cur = want ?? sel.value;
  sel.innerHTML = "";
  for (const n of Object.keys(provForm())) sel.appendChild(new Option(n, n));
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
}
async function probeModels(body) {
  try { return await J(await fetch("/api/models", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })); }
  catch (e) { return { ok: false, error: e.message, models: [] }; }
}
async function fillModels(dl, body) {
  const r = await probeModels(body);
  dl.innerHTML = "";
  for (const m of r.models || []) dl.appendChild(new Option(m, m));
  return r;
}
function provLoad(s) {
  const box = $("#prov-list");
  box.innerHTML = "";
  for (const [n, p] of Object.entries(s.providers || {})) box.appendChild(provRow(n, p));
  provSyncSelect(s.provider);
  fillModels($("#dl-default-models"), { provider: s.provider });
}
$("#btn-prov-add").onclick = () => {
  $("#prov-list").appendChild(provRow("", { template_kwargs: false }));
  $("#prov-list .prov-row:last-child .p-name").focus();
  cfgDirtyCompute();
};
$("#set-provider").addEventListener("change", () => {
  const p = provForm()[$("#set-provider").value] || {};
  fillModels($("#dl-default-models"), { base_url: p.base_url, api_key: p.api_key });
});

let tmodelSeq = 0, tmodelView = null;
let draftModel = null; // {provider, model} chosen on a "New chat" before it has a row
async function syncTModel(tid) {
  const b = $("#model-tag"), seq = ++tmodelSeq;
  const paint = (model, over, title, vision) => {
    b.textContent = (over ? "◇ " : "") + shortModel(model) + " ▾";
    b.title = title; b.classList.toggle("override", over);
    supportsVision = !!vision;
    if (!supportsVision && pendingImages.length) { pendingImages = []; renderAttachStrip(); }
  };
  if (!tid) {
    if (draftModel) { // show the draft's pick; the server resolves it once the thread exists
      tmodelView = { override: draftModel, draft: true };
      paint(draftModel.model || draftModel.provider || defaultModel.model, true, `${draftModel.provider || "(default provider)"} · ${draftModel.model || "(provider default)"}  (applies to this new chat)`, defaultModel.vision);
    } else {
      tmodelView = { override: {}, draft: true };
      paint(defaultModel.model, false, "global default model — click to pick another for this new chat", defaultModel.vision);
    }
    return;
  }
  let v;
  try { v = await J(await fetch(`/api/threads/${tid}/model`)); } catch { return; }
  if (seq !== tmodelSeq || tid !== threadId) return;
  tmodelView = v;
  const over = !!(v.override.provider || v.override.model || v.override.keep_reasoning);
  paint(v.model, over, `${v.provider} · ${v.model} · keep reasoning: ${v.keep_reasoning}` + (over ? "  (this thread has overrides — click to change)" : "  (global defaults — click to override for this thread)"), v.vision);
}
async function openTModel() {
  if (!tmodelView) return;
  SFX.play("click");
  const s = await api.settings();
  const sel = $("#tmodel-provider");
  sel.innerHTML = "";
  sel.appendChild(new Option(`(default) ${s.provider}`, ""));
  for (const n of Object.keys(s.providers || {})) sel.appendChild(new Option(n, n));
  sel.value = tmodelView.override.provider || "";
  $("#tmodel-model").value = tmodelView.override.model || "";
  const KR = { off: "off", turn: "current turn", all: "all turns" };
  $("#tmodel-keep-default").textContent = `(default: ${KR[s.keep_reasoning || "off"]})`;
  $("#tmodel-keep").value = tmodelView.override.keep_reasoning || "";
  $("#tmodel-thread").textContent = threadId ? $("#chat-title").textContent : "NEW CHAT — applies when you send the first message";
  const refresh = async () => {
    const pn = sel.value || s.provider;
    $("#tmodel-model").placeholder = `(${sel.value ? (s.providers[pn]?.model || s.model) : s.model})`;
    $("#tmodel-msg").textContent = "loading models…";
    const r = await fillModels($("#dl-tmodel-models"), { provider: pn });
    $("#tmodel-msg").textContent = r.ok ? `${r.models.length} model(s) on ${pn}` : `${pn}: ${r.error}`;
  };
  sel.onchange = refresh;
  refresh();
  $("#tmodel-panel").classList.remove("hidden");
}
const closeTModel = () => $("#tmodel-panel").classList.add("hidden");
async function saveTModel(ov) {
  const tid = threadId;
  if (!tid) { // draft: keep it client-side until createThreadNow
    draftModel = ov.provider || ov.model || ov.keep_reasoning ? ov : null;
    closeTModel(); SFX.play("settings_saved"); syncTModel(null); return;
  }
  try {
    await J(await fetch(`/api/threads/${tid}/model`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(ov),
    }));
  } catch (e) { $("#tmodel-msg").textContent = "save failed: " + e.message; return; }
  closeTModel();
  SFX.play("settings_saved");
  syncTModel(tid);
}
$("#model-tag").onclick = openTModel;
$("#btn-tmodel-save").onclick = () => saveTModel({ provider: $("#tmodel-provider").value, model: $("#tmodel-model").value.trim(), keep_reasoning: $("#tmodel-keep").value });
$("#btn-tmodel-default").onclick = () => saveTModel({ provider: "", model: "", keep_reasoning: "" });
$("#btn-tmodel-cancel").onclick = () => { SFX.play("click"); closeTModel(); };
$("#tmodel-panel").onclick = (e) => { if (e.target.id === "tmodel-panel") closeTModel(); };

// ---------- per-thread prompt ----------
// Stored server-side per thread (threads.prompt) and appended to the system
// prompt on every run in that thread; ✎ PROMPT lights up while one is set.
let tpromptSeq = 0;
async function syncTPrompt(tid) {
  const b = $("#btn-tprompt"), seq = ++tpromptSeq;
  b.classList.remove("on"); b.dataset.prompt = "";
  b.textContent = "✎ PROMPT";
  b.disabled = !tid;
  if (!tid) return;
  let r;
  try { r = await J(await fetch(`/api/threads/${tid}/prompt`)); } catch { return; }
  if (seq !== tpromptSeq || tid !== threadId) return; // switched away meanwhile
  b.dataset.prompt = r.prompt || "";
  b.classList.toggle("on", !!r.prompt);
  b.textContent = r.prompt ? "✎ PROMPT: ON" : "✎ PROMPT";
}
function tpromptCount() {
  const n = $("#tprompt-text").value.length;
  $("#tprompt-count").textContent = `${n} / 8000 chars`;
}
function openTPrompt() {
  if (!threadId) return;
  SFX.play("click");
  $("#tprompt-thread").textContent = $("#chat-title").textContent;
  $("#tprompt-text").value = $("#btn-tprompt").dataset.prompt || "";
  tpromptCount();
  $("#tprompt-panel").classList.remove("hidden");
  $("#tprompt-text").focus();
}
const closeTPrompt = () => $("#tprompt-panel").classList.add("hidden");
async function saveTPrompt(text) {
  const tid = threadId;
  try {
    await J(await fetch(`/api/threads/${tid}/prompt`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: text }),
    }));
  } catch (e) { $("#tprompt-count").textContent = "save failed: " + e.message; return; }
  closeTPrompt();
  SFX.play("settings_saved");
  syncTPrompt(tid);
}
$("#btn-tprompt").onclick = openTPrompt;
$("#tprompt-text").oninput = tpromptCount;
$("#btn-tprompt-save").onclick = () => saveTPrompt($("#tprompt-text").value);
$("#btn-tprompt-clear").onclick = () => saveTPrompt("");
$("#btn-tprompt-cancel").onclick = () => { SFX.play("click"); closeTPrompt(); };
$("#tprompt-panel").onclick = (e) => { if (e.target.id === "tprompt-panel") closeTPrompt(); };
$("#tprompt-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("#btn-tprompt-save").click(); }
});

// ---------- memory & learning (CONFIG) ----------
const fmtWhen = (ts) => new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
function learnOpenFile(path) {
  closeSettings(false);
  if (!$("#settings-panel").classList.contains("hidden")) return; // unsaved CONFIG: its bar asks first
  openEditor(path);
}
async function refreshMemoryUI(force = false) {
  // feed events refresh only an OPEN CONFIG; openSettings forces (it unhides last)
  if (!force && $("#settings-panel").classList.contains("hidden")) return;
  let mem, log;
  try { [mem, log] = await Promise.all([J(await fetch("/api/memory")), J(await fetch("/api/learning?n=20"))]); }
  catch { return; }
  $("#mem-dir").textContent = mem.dir;
  $("#mem-count").textContent = `(${mem.memories.length})`;
  const ml = $("#mem-list");
  ml.innerHTML = "";
  if (!mem.memories.length) ml.appendChild(el("div", "learn-empty", "none yet — the agent saves them with remember, or the review does"));
  for (const m of mem.memories) {
    const row = el("div", "learn-row");
    const main = el("div", "lr-main");
    const nm = el("span", "lr-name", m.name);
    nm.title = "open in FILES";
    nm.onclick = () => learnOpenFile(m.path);
    main.append(nm, el("span", "lr-meta", ` · ${m.type || "note"} — `), document.createTextNode(m.description));
    if (m.error) main.appendChild(el("div", "lr-bad", "⚠ " + m.error));
    const x = el("button", "btn ghost", "✕");
    x.type = "button"; x.title = "forget this memory";
    x.onclick = async () => {
      if (x.dataset.armed !== "1") { x.dataset.armed = "1"; x.textContent = "SURE?"; return; }
      try { await J(await fetch("/api/memory/" + encodeURIComponent(m.name), { method: "DELETE" })); } catch {}
      refreshMemoryUI();
    };
    row.append(main, x);
    ml.appendChild(row);
  }
  const ll = $("#learn-list");
  ll.innerHTML = "";
  const entries = (log.entries || []).filter((e) => !e.skipped);
  if (!entries.length) ll.appendChild(el("div", "learn-empty", "no reviews yet"));
  for (const e of entries) {
    const row = el("div", "learn-row");
    const main = el("div", "lr-main");
    main.appendChild(el("span", "lr-meta", `${fmtWhen(e.ts)} · ${e.title || e.tid} · ${e.tool_calls} calls${e.auto ? "" : " · manual"} — `));
    const res = e.results || [];
    if (!res.length) main.appendChild(document.createTextNode("nothing new"));
    res.forEach((r, i) => {
      if (i) main.appendChild(document.createTextNode(", "));
      if (r.op === "rejected") { main.appendChild(el("span", "lr-bad", `rejected ${r.kind} ${r.name}: ${r.error}`)); return; }
      main.appendChild(document.createTextNode(`${r.op} ${r.kind} `));
      const nm = el("span", "lr-name", r.name);
      nm.title = r.path + (r.backup ? `\nprevious version: ${r.backup}` : "");
      nm.onclick = () => learnOpenFile(r.path);
      main.appendChild(nm);
    });
    if (e.reason) { const why = el("div", "lr-meta", e.reason); main.appendChild(why); }
    row.appendChild(main);
    ll.appendChild(row);
  }
}
$("#btn-review-now").onclick = async () => {
  const b = $("#btn-review-now"), msg = $("#review-msg");
  if (!threadId) return;
  b.disabled = true;
  msg.textContent = "reviewing the last turn… (one model call, ~10–60 s)";
  msg.classList.remove("hidden");
  try {
    const r = await J(await fetch(`/api/threads/${threadId}/review`, { method: "POST" }));
    const n = (r.results || []).filter((x) => x.op !== "rejected").length;
    msg.textContent = r.skipped ? "skipped: " + r.skipped
      : n ? `done — ${n} change(s), see below` : "done — nothing worth keeping from that turn";
  } catch (e) { msg.textContent = "review failed: " + e.message; }
  b.disabled = !threadId;
  refreshMemoryUI();
};

// effort only means something while thinking is on
$("#set-thinking").addEventListener("change", (e) => { $("#set-reasoning_effort").disabled = !e.target.checked; });  // keep_reasoning stays editable: a thread can enable thinking later

$("#btn-recap").onclick = openRecap;
$("#btn-recap-close").onclick = () => { SFX.play("click"); closeRecap(); };
$("#recap-panel").onclick = (e) => { if (e.target.id === "recap-panel") closeRecap(); };
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  // settings above recap: it can sit on top of it and is the top-most panel
  if (!$("#settings-panel").classList.contains("hidden")) closeSettings(false);
  else if (!$("#tprompt-panel").classList.contains("hidden")) closeTPrompt();
  else if (!$("#tmodel-panel").classList.contains("hidden")) closeTModel();
  else if (!$("#editor-panel").classList.contains("hidden")) edClose();
  else if (!$("#recap-panel").classList.contains("hidden")) closeRecap();
});

// ---------- human gates: ask_user questions + plan mode ----------
// The agent pauses on a langgraph interrupt; the run ends with a `gate`
// event and this card collects the answer, which POSTs /resume — a NEW
// hub-backed run of the same thread. The pause lives in the server
// checkpoint, so a reopened tab / phone finds it again via GET .../gate.
const GATE_TOOLS = new Set(["ask_user", "exit_plan_mode"]);

// plan mode is per thread (a draft "New chat" keeps its own until created)
const planKey = (tid) => "lb-plan:" + (tid || "draft");
function planOn(tid = threadId) {
  try { return localStorage.getItem(planKey(tid)) === "on"; } catch { return false; }
}
function setPlan(on, tid = threadId) {
  try {
    if (on) localStorage.setItem(planKey(tid), "on");
    else localStorage.removeItem(planKey(tid));
  } catch {}
  if (tid === threadId) syncComposerHint();
}

const INPUT_HINT = $("#input").placeholder;
function syncComposerHint() {
  const on = planOn();
  $("#btn-plan").classList.toggle("on", on);
  $("#btn-plan").textContent = on ? "◆ PLAN: ON" : "◇ PLAN";
  $("#composer").classList.toggle("plan-mode", on);
  const waiting = !!document.querySelector("#chat .gate:not(.sealed)");
  $("#input").placeholder = waiting
    ? "The agent is waiting on you — answer in the card above, or type a reply here…"
    : on ? "PLAN MODE — the agent investigates read-only, asks what it needs, and proposes a plan for your approval (Shift+Tab toggles)…"
    : INPUT_HINT;
}

function gateSeal(card, summary) {
  card.classList.add("sealed");
  for (const n of card.querySelectorAll("button, input, textarea")) n.disabled = true;
  const act = card.querySelector(".gate-actions");
  if (act) act.replaceChildren(el("div", "gate-done", "✓ " + summary));
}

function gateCard(tid, g) {
  const v = g.value || {};
  const card = el("div", "msg gate");
  card.dataset.gateId = g.id;
  const act = el("div", "gate-actions");
  if (v.kind === "plan") {
    card.appendChild(el("div", "gate-head", "◆ PLAN READY FOR REVIEW"));
    const body = el("div", "gate-plan");
    setMarkdown(body, v.plan || "");
    card.appendChild(body);
    const fb = el("textarea", "gate-text");
    fb.rows = 2;
    fb.placeholder = "Notes — required for REVISE, optional with APPROVE…";
    card.appendChild(fb);
    const ok = el("button", "btn accent", "✓ APPROVE & BUILD");
    const rev = el("button", "btn ghost", "↺ REVISE");
    ok.onclick = () => {
      const notes = fb.value.trim();
      gateSeal(card, "APPROVED" + (notes ? " — " + notes : "") + " · plan mode off");
      setPlan(false, tid); // the agent implements now; next turns run normally
      resumeGate(tid, g.id, { approved: true, feedback: notes }, card);
    };
    rev.onclick = () => {
      const notes = fb.value.trim();
      if (!notes) { fb.focus(); fb.classList.add("need"); return; }
      gateSeal(card, "REVISION REQUESTED — " + notes);
      resumeGate(tid, g.id, { approved: false, feedback: notes }, card);
    };
    act.append(ok, rev);
  } else {
    card.appendChild(el("div", "gate-head", "◆ THE AGENT NEEDS YOUR INPUT"));
    const qs = v.questions || [];
    const rows = qs.map((q, i) => {
      const box = el("div", "gate-q");
      box.appendChild(el("div", "gate-qt", `${qs.length > 1 ? i + 1 + ". " : ""}${q.question}`));
      const picked = new Set();
      if (q.options?.length) {
        const chips = el("div", "gate-opts");
        for (const o of q.options) {
          const c = el("button", "gate-opt", o);
          c.onclick = () => {
            if (!q.multi_select) {
              picked.clear();
              for (const x of chips.children) x.classList.remove("on");
            }
            if (picked.has(o)) { picked.delete(o); c.classList.remove("on"); }
            else { picked.add(o); c.classList.add("on"); }
          };
          chips.appendChild(c);
        }
        box.appendChild(chips);
      }
      const t = el("input", "gate-text");
      t.type = "text";
      t.placeholder = q.options?.length ? "…or your own answer / details" : "Your answer";
      t.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit.click(); } });
      box.appendChild(t);
      card.appendChild(box);
      return { q, picked, t };
    });
    const submit = el("button", "btn accent", "SUBMIT ▶");
    submit.onclick = () => {
      const answers = rows.map((r) => ({ selected: [...r.picked], text: r.t.value.trim() }));
      gateSeal(card, answers.map((a, i) =>
        (qs.length > 1 ? i + 1 + ". " : "") + ([...a.selected, a.text].filter(Boolean).join("; ") || "(skipped)")
      ).join("  ·  "));
      resumeGate(tid, g.id, { answers }, card);
    };
    act.appendChild(submit);
  }
  card.appendChild(act);
  return card;
}

// answer → /resume: same bundle/pipeline dance as send(), minus the bubble
async function resumeGate(tid, gid, value, card) {
  if (RUNS.has(tid)) return;
  syncComposerHint();
  SFX.play("message_sent");
  const run = newBundle(tid);
  if (card && card.parentNode === $("#chat")) run.nodes.push(card); // parks with the run
  RUNS.set(tid, run);
  run.iv = setInterval(() => tickRun(run), 250);
  syncSendBtn();
  const pipe = runPipeline(run);
  if (run.viewing) {
    $("#chat")._pinned = true;
    $("#sb-live").classList.remove("hidden");
    run.waitLabel = "⏳ RESUMING WITH YOUR ANSWER";
    run.waiting = pipe.put(addBlock("waiting", run.waitLabel + " …", pipe.CH()));
    run.waiting.open = true;
    tickRun(run);
  }
  try {
    const res = await fetch(`/api/threads/${encodeURIComponent(tid)}/resume`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: gid, value, plan: planOn(tid) }),
    });
    if (!res.ok) {
      let why = "";
      try { await J(res); } catch (e) { why = e.message; }
      SFX.play("error");
      pipe.put(addMsg("error", "ANSWER REJECTED — " + why, null, pipe.CH()));
      endRun(run, true); // repaint from the server's truth (gate may be gone)
      return;
    }
    await pipeSSE(res, pipe.handleEvent);
    await afterStream(run, pipe);
  } catch (e) {
    detachedNote(run, e);
  }
}

async function showPendingGates(tid, seq) {
  let gates = [];
  try { gates = (await (await fetch(`/api/threads/${encodeURIComponent(tid)}/gate`)).json()).gates || []; }
  catch { return; }
  if (seq !== openSeq || threadId !== tid || RUNS.has(tid)) return;
  for (const g of gates)
    if (!document.querySelector(`[data-gate-id="${g.id}"]`)) $("#chat").appendChild(gateCard(tid, g));
  if (gates.length) scrollBottom();
  syncComposerHint();
}

$("#btn-plan").onclick = () => { SFX.play("click"); setPlan(!planOn()); };
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Tab" && e.shiftKey) { e.preventDefault(); SFX.play("click"); setPlan(!planOn()); }
});
syncComposerHint();

// ---------- ✎ FILES: browse + edit text files on the server ----------
// One buffer at a time. Saves carry the content hash the buffer was loaded at, so
// an agent write landing while you edit is a 409 resolved in #ed-confirm
// (reload / overwrite) — never a silent clobber either way. Every path that
// would drop unsaved edits (close, open another file, ESC, backdrop) goes
// through edGuard's SAVE / DISCARD / KEEP bar (the viewer has no confirm()).
const ED = { path: null, ver: null, eol: "lf", orig: "", dir: null, lines: 0, indent: "    " };
const edDirty = () => ED.path !== null && !ED.viewer && $("#ed-text").value !== ED.orig;

function edStatus(msg, cls) {
  const s = $("#ed-status");
  s.textContent = msg || "";
  s.className = cls || "";
}

// ---- assist bar: ask about / edit THIS file (server/fileassist.py) ----
// One tool-less model call per request: it sees the current buffer (unsaved
// edits included) + selection + the last exchanges about this file, and
// proposes search/replace blocks that are applied only on APPLY — via
// execCommand so Ctrl+Z still works — and saved only by SAVE.
ED.assist = { path: null, turns: [], abort: null };

function edAssistReset(path) {
  if (ED.assist.abort) ED.assist.abort.abort();
  ED.assist = { path, turns: [], abort: null };
  $("#ed-assist-log").replaceChildren();
  $("#ed-ask").value = "";
}

function edParseBlocks(text) {
  const re = /<{7} SEARCH\n([\s\S]*?)\n?={7}\n([\s\S]*?)\n?>{7} REPLACE/g;
  const blocks = [];
  let m;
  while ((m = re.exec(text))) blocks.push({ search: m[1], replace: m[2] });
  const first = text.search(/<{7} SEARCH/);
  const summary = (first >= 0 ? text.slice(0, first) : text).trim();
  return { blocks, summary };
}

// exact match first; then a line-wise match ignoring trailing whitespace
function edFind(hay, needle) {
  const i = hay.indexOf(needle);
  if (i >= 0) return [i, i + needle.length];
  const H = hay.split("\n"), N = needle.split("\n").map((l) => l.trimEnd());
  for (let a = 0; a + N.length <= H.length; a++) {
    if (N.every((l, k) => H[a + k].trimEnd() === l)) {
      const start = H.slice(0, a).join("\n").length + (a ? 1 : 0);
      const end = start + H.slice(a, a + N.length).join("\n").length;
      return [start, end];
    }
  }
  return null;
}

function edApplyBlocks(text, blocks) {
  let out = text;
  const failed = [];
  blocks.forEach((b, i) => {
    if (!b.search) {
      if (!out.trim()) out = b.replace; else failed.push(i + 1);
      return;
    }
    const at = edFind(out, b.search);
    if (!at) { failed.push(i + 1); return; }
    out = out.slice(0, at[0]) + b.replace + out.slice(at[1]);
  });
  return { out, failed };
}

function edDiffView(blocks) {
  const box = el("div", "ed-diff");
  blocks.forEach((b, i) => {
    const pre = el("pre", "ed-diff-block");
    pre.appendChild(el("div", "ed-diff-h", `edit ${i + 1}`));
    for (const l of b.search ? b.search.split("\n") : []) pre.appendChild(el("div", "del", "− " + l));
    for (const l of b.replace ? b.replace.split("\n") : []) pre.appendChild(el("div", "add", "+ " + l));
    box.appendChild(pre);
  });
  return box;
}

function edSelection() {
  const ta = $("#ed-text");
  const [a, b] = [ta.selectionStart, ta.selectionEnd];
  if (a === b) return null;
  const before = ta.value.slice(0, a);
  return { text: ta.value.slice(a, b), from_line: before.split("\n").length,
           to_line: before.split("\n").length + ta.value.slice(a, b).split("\n").length - 1 };
}

async function edAsk() {
  const ask = $("#ed-ask");
  const q = ask.value.trim();
  if (ED.assist.abort) { ED.assist.abort.abort(); return; } // ASK button doubles as STOP
  if (!q || ED.path === null || ED.viewer) return;
  ask.value = "";
  const path = ED.path;
  const log = $("#ed-assist-log");
  log.appendChild(el("div", "ed-q", "› " + q));
  const ans = el("div", "ed-a msg");
  ans.textContent = "…";
  log.appendChild(ans);
  log.scrollTop = log.scrollHeight;
  const ctl = new AbortController();
  ED.assist.abort = ctl;
  $("#btn-ed-ask").textContent = "■ STOP";
  let raw = "";
  try {
    const res = await fetch("/api/fs/assist", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: ctl.signal,
      body: JSON.stringify({ path, lang: ED.lang, content: $("#ed-text").value, instruction: q,
                             selection: edSelection(), history: ED.assist.turns }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "HTTP " + res.status);
    await pipeSSE(res, (ev) => {
      if (ev.type === "token") { raw += ev.text; ans.textContent = raw; log.scrollTop = log.scrollHeight; }
      else if (ev.type === "error") throw new Error(ev.message);
    });
  } catch (e) {
    if (e.name !== "AbortError") { ans.textContent = "✕ " + e.message; ans.classList.add("err"); }
    else ans.textContent = raw + " …(stopped)";
    ED.assist.abort = null;
    $("#btn-ed-ask").textContent = "ASK ▶";
    return;
  }
  ED.assist.abort = null;
  $("#btn-ed-ask").textContent = "ASK ▶";
  if (ED.path !== path) return; // switched files mid-answer
  ED.assist.turns.push({ role: "user", text: q }, { role: "assistant", text: raw });
  const { blocks, summary } = edParseBlocks(raw);
  ans.replaceChildren();
  if (!blocks.length) { setMarkdown(ans, raw); log.scrollTop = log.scrollHeight; return; }
  if (summary) ans.appendChild(el("div", "ed-a-sum", summary));
  ans.appendChild(edDiffView(blocks));
  const act = el("div", "ed-a-act");
  const apply = el("button", "btn accent sm", "✓ APPLY");
  const drop = el("button", "btn ghost sm", "DISCARD");
  apply.onclick = () => {
    const ta = $("#ed-text");
    const { out, failed } = edApplyBlocks(ta.value, blocks);
    if (failed.length === blocks.length) {
      act.replaceChildren(el("span", "pv-status warn", "✕ none of the edits match the current text — ask again"));
      return;
    }
    // execCommand keeps Ctrl+Z, but only works on a focusable, VISIBLE
    // textarea — in PREVIEW mode it's display:none, focus() is a no-op and
    // the insert silently went nowhere while this said "applied" (a real
    // edit was lost that way). Use it when it can work, then VERIFY, and
    // fall back to setting the text directly.
    let undoable = false;
    if (ta.offsetParent !== null) {
      ta.focus();
      ta.select();
      document.execCommand("insertText", false, out);
      undoable = ta.value === out;
    }
    if (ta.value !== out) ta.value = out;
    if (ta.value !== out) {
      act.replaceChildren(el("span", "pv-status warn", "✕ couldn't update the buffer — nothing changed"));
      return;
    }
    edGutter();
    edSchedulePreview(true); // PREVIEW / SPLIT show the change right away
    edRefreshChrome();
    act.replaceChildren(el("span", "pv-status ok",
      (failed.length ? `✓ applied — edit ${failed.join(", ")} didn't match and was skipped`
                     : "✓ applied to the buffer") +
      (undoable ? " · Ctrl+Z undoes" : " · REVERT undoes") + " · SAVE to keep"));
  };
  drop.onclick = () => act.replaceChildren(el("span", "pv-status", "discarded"));
  act.append(apply, drop);
  ans.appendChild(act);
  log.scrollTop = log.scrollHeight;
}
$("#btn-ed-ask").onclick = () => edAsk();
$("#ed-ask").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); edAsk(); }
});

function edRefreshChrome() {
  const dirty = edDirty();
  $("#ed-file").classList.toggle("dirty", dirty);
  $("#btn-ed-save").disabled = ED.path === null || ED.viewer || (!dirty && ED.ver !== null);
  $("#btn-ed-revert").disabled = !dirty;
}

function edGutter() {
  const ta = $("#ed-text");
  const n = ta.value.split("\n").length;
  if (n !== ED.lines) {
    ED.lines = n;
    let s = "";
    for (let i = 1; i <= n; i++) s += i + "\n";
    $("#ed-gutter").textContent = s;
  }
  edPaint();
  edSchedulePreview();
  edSyncScroll();
}

// ---- syntax highlighting: #ed-hl mirrors the textarea underneath it ----
// Every input repaints SYNCHRONOUSLY (the textarea's own glyphs are
// transparent — a deferred paint would make typing invisible). Big files
// paint plain text now and colors a beat later; huge ones stay plain.
const ED_EXT_LANG = {
  py: "python", pyw: "python", js: "javascript", mjs: "javascript", cjs: "javascript",
  jsx: "javascript", ts: "typescript", tsx: "typescript", json: "json", jsonl: "json",
  sh: "bash", bash: "bash", zsh: "bash", yml: "yaml", yaml: "yaml", toml: "toml",
  ini: "ini", cfg: "ini", conf: "ini", service: "ini", timer: "ini", md: "markdown",
  markdown: "markdown", html: "xml", htm: "xml", xml: "xml", svg: "xml", css: "css",
  scss: "scss", less: "less", c: "c", h: "c", cpp: "cpp", cc: "cpp", hpp: "cpp",
  cs: "csharp", go: "go", rs: "rust", java: "java", kt: "kotlin", swift: "swift",
  rb: "ruby", php: "php", pl: "perl", r: "r", lua: "lua", sql: "sql", diff: "diff",
  patch: "diff", graphql: "graphql", gql: "graphql", vb: "vbnet", m: "objectivec",
  mk: "makefile",
};
const ED_NAME_LANG = { makefile: "makefile", gnumakefile: "makefile", caddyfile: "ini",
  ".bashrc": "bash", ".zshrc": "bash", ".profile": "bash", ".bash_profile": "bash" };
const ED_HL_SYNC = 60_000;   // chars: highlight inline on every keystroke
const ED_HL_MAX = 400_000;   // beyond: plain text, no colors
function edLangFor(path) {
  const name = (path || "").split("/").pop().toLowerCase();
  const lang = ED_NAME_LANG[name] ||
    (name.includes(".") ? ED_EXT_LANG[name.split(".").pop()] : null);
  return lang && typeof hljs !== "undefined" && hljs.getLanguage(lang) ? lang : null;
}
const edEsc = (t) => t.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
let edHlTimer = null;
function edPaint() {
  const text = $("#ed-text").value;
  const code = $("#ed-hl code");
  const plain = !ED.lang || text.length > ED_HL_MAX;
  $(".ed-wrap").classList.toggle("plain", plain);
  clearTimeout(edHlTimer);
  if (plain) { code.textContent = ""; return; }
  // trailing " ": a <pre> ignores a final newline the textarea still shows
  // as an empty last line — one space keeps both heights equal (a "\n "
  // here made the layer a line TALLER; the suite's height check caught it)
  const hl = () => hljs.highlight(text, { language: ED.lang, ignoreIllegals: true }).value + " ";
  if (text.length <= ED_HL_SYNC) { code.innerHTML = hl(); return; }
  code.innerHTML = edEsc(text) + " ";
  edHlTimer = setTimeout(() => {
    if ($("#ed-text").value === text) code.innerHTML = hl();
  }, 180);
}
function edSyncScroll() {
  const ta = $("#ed-text");
  $("#ed-gutter").scrollTop = ta.scrollTop;
  const hlPre = $("#ed-hl");
  hlPre.scrollTop = ta.scrollTop;
  hlPre.scrollLeft = ta.scrollLeft;
  // split view: preview follows proportionally (markdown ≠ line-aligned)
  const pv = $("#ed-preview");
  if (ED.md && ED.mode === "split") {
    const r = ta.scrollTop / Math.max(1, ta.scrollHeight - ta.clientHeight);
    pv.scrollTop = r * (pv.scrollHeight - pv.clientHeight);
  }
}

// ---- markdown preview (EDIT / SPLIT / PREVIEW for .md files) ----
// Same renderer as chat bubbles (marked + DOMPurify + code-block toolbar).
// Relative image srcs resolve against the file's folder through /api/media;
// relative links to local files open in this editor instead of 404ing.
let edPvTimer = null;
function edSchedulePreview(now) {
  if (!ED.md || ED.mode === "edit") return;
  clearTimeout(edPvTimer);
  edPvTimer = setTimeout(edRenderPreview, now ? 0 : 200);
}
function edResolve(ref) {
  if (/^[a-z][a-z0-9+.-]*:/i.test(ref) || ref.startsWith("#") || ref.startsWith("//")) return null;
  const clean = decodeURIComponent(ref.split("#")[0].split("?")[0]);
  if (!clean) return null;
  const base = clean.startsWith("/") ? [] : ED.path.split("/").slice(0, -1);
  for (const seg of clean.split("/")) {
    if (seg === "..") base.pop();
    else if (seg && seg !== ".") base.push(seg);
  }
  return "/" + base.filter(Boolean).join("/");
}
function edRenderPreview() {
  const pv = $("#ed-preview");
  const keep = pv.scrollTop;
  setMarkdown(pv, $("#ed-text").value);
  for (const img of pv.querySelectorAll("img[src]")) {
    const p = edResolve(img.getAttribute("src"));
    if (p) img.src = "/api/media?path=" + encodeURIComponent(p);
  }
  for (const a of pv.querySelectorAll("a[href]")) {
    const p = edResolve(a.getAttribute("href"));
    if (p) {
      a.href = "#";
      a.title = p;
      a.onclick = (e) => { e.preventDefault(); edGuard(() => edLoad(p)); };
    } else a.target = "_blank";
  }
  pv.scrollTop = keep;
}
function edSetMode(mode) {
  ED.mode = mode;
  try { localStorage.setItem("lb-ed-mdmode", mode); } catch {}
  edApplyMode();
}
function edApplyMode() {
  const wrap = $(".ed-wrap");
  const mode = ED.md ? ED.mode : "edit";
  wrap.classList.remove("mode-edit", "mode-split", "mode-preview");
  wrap.classList.add("mode-" + mode);
  $("#ed-preview").classList.toggle("hidden", mode === "edit");
  $("#ed-modes").classList.toggle("hidden", !ED.md);
  for (const b of $("#ed-modes").querySelectorAll("button"))
    b.classList.toggle("on", b.dataset.mode === mode);
  if (mode !== "edit") edSchedulePreview(true);
  if (mode === "preview") $("#ed-preview").focus?.();
}
ED.mode = (() => { try { return localStorage.getItem("lb-ed-mdmode") || "split"; } catch { return "split"; } })();
for (const b of $("#ed-modes").querySelectorAll("button"))
  b.onclick = () => { SFX.play("click"); edSetMode(b.dataset.mode); };

async function edFetch(url, opts) {
  const r = await fetch(url, opts);
  let body = null;
  try { body = await r.json(); } catch {}
  if (!r.ok) {
    const err = new Error((body && body.detail) || "HTTP " + r.status);
    err.status = r.status;
    throw err;
  }
  return body;
}

function edConfirm(msg, buttons) {
  const bar = $("#ed-confirm");
  bar.replaceChildren(el("span", null, msg));
  for (const [label, cls, fn] of buttons) {
    const b = el("button", "btn " + cls, label);
    b.onclick = () => { bar.classList.add("hidden"); fn(); };
    bar.appendChild(b);
  }
  bar.classList.remove("hidden");
}

// run `fn` now, or after the user settles unsaved edits
function edGuard(fn) {
  if (!edDirty()) { fn(); return; }
  edConfirm(`● UNSAVED CHANGES TO ${ED.path.split("/").pop()} —`, [
    ["SAVE", "accent", async () => { if (await edSave()) fn(); }],
    ["DISCARD", "ghost", fn],
    ["KEEP EDITING", "ghost", () => $("#ed-text").focus()],
  ]);
}

async function edBrowse(dir) {
  const tree = $("#ed-tree");
  let out;
  try {
    out = await edFetch("/api/fs/list?path=" + encodeURIComponent(dir));
  } catch (e) {
    edStatus("✕ " + dir + ": " + e.message, "err");
    return false;
  }
  ED.dir = out.path;
  try { localStorage.setItem("lb-ed-dir", out.path); } catch {}
  const showHidden = $("#ed-hidden").checked;
  tree.replaceChildren();
  const row = (cls, label, onclick, size) => {
    const r = el("div", "ed-row " + cls, label);
    if (size != null) r.appendChild(el("span", "sz", fmtBytes(size)));
    r.onclick = onclick;
    tree.appendChild(r);
    return r;
  };
  tree.appendChild(el("div", "ed-note", out.path));
  if (out.parent) row("dir", "↰ ..", () => edBrowse(out.parent));
  for (const e of out.entries) {
    if (!showHidden && e.name.startsWith(".")) continue;
    const full = (out.path === "/" ? "" : out.path) + "/" + e.name;
    if (e.dir) row("dir", "▸ " + e.name, () => edBrowse(full));
    else {
      const r = row("file", e.name, () => edGuard(() => edLoad(full)), e.size);
      r.dataset.path = full;
      if (full === ED.path) r.classList.add("on");
    }
  }
  if (out.truncated) tree.appendChild(el("div", "ed-note", "… listing truncated"));
  return true;
}

function edMarkTree() {
  for (const r of document.querySelectorAll("#ed-tree .ed-row.file"))
    r.classList.toggle("on", r.dataset.path === ED.path);
}

function edSetBuffer(path, content, ver, eol, note) {
  edViewerOff();
  if (ED.assist.path !== path) edAssistReset(path);
  $("#ed-assist").classList.remove("hidden");
  ED.path = path;
  ED.ver = ver;
  ED.lang = edLangFor(path);
  ED.md = ED.lang === "markdown";
  $("#ed-lang").textContent = (ED.lang || "plain text").toUpperCase();
  ED.eol = eol || "lf";
  ED.orig = content;
  ED.lines = 0;
  ED.indent = /^\t/m.test(content) ? "\t" : "    ";
  const ta = $("#ed-text");
  ta.disabled = false;
  ta.value = content;
  ta.scrollTop = 0;
  ta.scrollLeft = 0;
  $("#ed-file").textContent = path;
  $("#ed-path").value = path;
  $("#ed-confirm").classList.add("hidden");
  edGutter();
  edApplyMode();
  edRefreshChrome();
  edMarkTree();
  edStatus(note);
}

// ---- media viewer: images / audio / video / PDF open in place of the editor ----
const ED_MEDIA = {
  img: /\.(png|jpe?g|gif|webp|bmp|ico|avif|svg)$/i,
  audio: /\.(mp3|wav|ogg|oga|opus|m4a|flac|aac)$/i,
  video: /\.(mp4|webm|mov|m4v|mkv|ogv)$/i,
  pdf: /\.pdf$/i,
};
const edMediaKind = (p) => Object.keys(ED_MEDIA).find((k) => ED_MEDIA[k].test(p)) || null;

function edViewerOff() {
  if (!ED.viewer) return;
  ED.viewer = false;
  for (const m of $("#ed-viewer").querySelectorAll("audio,video")) m.pause();
  $("#ed-viewer").replaceChildren();
  $("#ed-viewer").classList.add("hidden");
  $(".ed-wrap").classList.remove("viewer");
}

async function edShowMedia(path, kind) {
  edViewerOff();
  const src = "/api/media?path=" + encodeURIComponent(path);
  ED.path = path; ED.ver = null; ED.orig = ""; ED.viewer = true; ED.md = false; ED.lang = null;
  const ta = $("#ed-text");
  ta.value = ""; ta.disabled = true;
  $(".ed-wrap").classList.add("viewer");
  $("#ed-preview").classList.add("hidden");
  $("#ed-modes").classList.add("hidden");
  $("#ed-assist").classList.add("hidden");
  $("#ed-confirm").classList.add("hidden");
  $("#ed-file").textContent = path;
  $("#ed-path").value = path;
  $("#ed-lang").textContent = kind === "img" ? "IMAGE" : kind.toUpperCase();
  const v = $("#ed-viewer");
  v.classList.remove("hidden");
  let node;
  if (kind === "pdf") {
    node = el("iframe", "ed-pdf");
    node.src = src;
    node.title = path;
  } else {
    node = document.createElement(kind);
    node.src = src;
    if (kind !== "img") { node.controls = true; node.preload = "metadata"; }
  }
  const size = (document.querySelector(`#ed-tree .ed-row[data-path="${CSS.escape(path)}"] .sz`) || {}).textContent || "";
  const info = (extra) => edStatus([$("#ed-lang").textContent, extra, size].filter(Boolean).join(" · "));
  node.addEventListener(kind === "img" ? "load" : "loadedmetadata", () => {
    if (kind === "img") info(`${node.naturalWidth}×${node.naturalHeight}`);
    else if (kind === "video") info(`${node.videoWidth}×${node.videoHeight} · ${fmtMS(node.duration)}`);
    else info(fmtMS(node.duration));
  });
  node.addEventListener("error", () => edStatus("✕ the browser can't display this file", "err"));
  v.appendChild(node);
  const open = el("a", "btn ghost sm", "↗ OPEN RAW");
  open.href = src; open.target = "_blank";
  const bar = el("div", "ed-viewer-bar");
  bar.appendChild(open);
  if (/\.svg$/i.test(path)) { // SVG is text too: offer the editor
    const src2 = el("button", "btn ghost sm", "✎ EDIT SOURCE");
    src2.onclick = () => edLoad(path, true);
    bar.appendChild(src2);
  } else if (kind === "img") { // raster: Qwen-Image 2.1 edit on ComfyUI
    const ai = el("button", "btn ghost sm", "✨ EDIT WITH AI");
    ai.onclick = () => { form.classList.toggle("hidden"); form.querySelector("input").focus(); };
    bar.appendChild(ai);
  }
  v.appendChild(bar);
  // ✨ edit form: the result is a NEW file (~/Pictures/langbang/), never an overwrite
  const form = el("div", "ed-imgedit hidden");
  const pr = el("input");
  pr.type = "text";
  pr.placeholder = "Describe the change — e.g. “make it night, add snow” (this image is image_1)";
  const go = el("button", "btn accent sm", "GENERATE ▶");
  const st = el("span", "pv-status");
  form.append(pr, go, st);
  v.appendChild(form);
  const run = async () => {
    const prompt = pr.value.trim();
    if (!prompt || go.disabled) return;
    go.disabled = true;
    const t0 = Date.now();
    const tick = setInterval(() => { st.textContent = `⏳ generating on ComfyUI… ${Math.round((Date.now() - t0) / 1000)} s (≈ 1 min)`; }, 500);
    st.className = "pv-status";
    try {
      const r = await fetch("/api/image", { method: "POST", headers: { "Content-Type": "application/json" },
                                            body: JSON.stringify({ prompt, input_images: [path] }) });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(out.detail || "HTTP " + r.status);
      clearInterval(tick);
      SFX.play("message_received");
      if (ED.path === path) await edLoad(out.paths[0]); // show the result (its folder opens in the tree)
    } catch (e) {
      clearInterval(tick);
      st.textContent = "✕ " + e.message;
      st.className = "pv-status warn";
      go.disabled = false;
    }
  };
  go.onclick = run;
  pr.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); run(); } });
  info("");
  edRefreshChrome();
  edMarkTree();
  const dir = path.slice(0, path.lastIndexOf("/")) || "/";
  if (dir !== ED.dir) await edBrowse(dir);
  return true;
}

async function edLoad(path, asText = false) {
  const kind = asText ? null : edMediaKind(path);
  if (kind) return edShowMedia(path, kind);
  let f;
  try {
    f = await edFetch("/api/fs/read?path=" + encodeURIComponent(path));
  } catch (e) {
    edStatus("✕ " + path + ": " + e.message, "err");
    SFX.play("error");
    return false;
  }
  edSetBuffer(f.path, f.content, f.version, f.eol,
    `${f.content.split("\n").length} LINES · ${fmtBytes(f.size)} · ${f.eol.toUpperCase()}` +
    (f.writable ? "" : " · ⚠ READ-ONLY ON DISK"));
  const dir = f.path.slice(0, f.path.lastIndexOf("/")) || "/";
  if (dir !== ED.dir) await edBrowse(dir);
  return true;
}

async function edSave(force = false) {
  if (ED.path === null || ED.viewer) return false;
  try {
    const out = await edFetch("/api/fs/write", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: ED.path, content: $("#ed-text").value,
        version: ED.ver, eol: ED.eol, force }),
    });
    const created = ED.ver === null;
    ED.path = out.path; // "~/x" new-file buffers come back expanded
    $("#ed-file").textContent = out.path;
    ED.ver = out.version;
    ED.orig = $("#ed-text").value;
    edRefreshChrome();
    edStatus(`✓ SAVED ${new Date().toLocaleTimeString()} · ${fmtBytes(out.size)}`, "ok");
    SFX.play("click");
    if (created) SUG.dirs.clear(); // autocomplete must see the new file
    if (created && ED.dir) await edBrowse(ED.dir); // new file joins the listing
    return true;
  } catch (e) {
    SFX.play("error");
    if (e.status === 409) {
      edConfirm(`⚠ ${e.message.toUpperCase()} —`, [
        ["RELOAD FROM DISK", "ghost", () => edLoad(ED.path)],
        ["OVERWRITE", "accent", () => edSave(true)],
        ["CANCEL", "ghost", () => $("#ed-text").focus()],
      ]);
    } else edStatus("✕ save failed: " + e.message, "err");
    return false;
  }
}

// Enter in the path box: folder → browse, file → open, nothing → new buffer
async function edGo(raw) {
  const path = raw.trim();
  if (!path) return;
  try {
    await edFetch("/api/fs/list?path=" + encodeURIComponent(path));
    await edBrowse(path);
    return;
  } catch (e) {
    if (e.status !== 404) { edStatus("✕ " + e.message, "err"); return; }
  }
  try {
    await edFetch("/api/fs/read?path=" + encodeURIComponent(path));
  } catch (e) {
    if (e.status === 404) {
      edGuard(() => edSetBuffer(path, "", null, "lf", "NEW FILE — SAVE creates it (parent folder must exist)"));
      return;
    }
  }
  edGuard(() => edLoad(path));
}

async function openEditor(path) {
  SFX.play("click");
  $("#editor-panel").classList.remove("hidden");
  if (path) {
    edGuard(async () => {
      // unloadable (binary, too big, gone): still land in its folder
      if (!(await edLoad(path)) && !(await edBrowse(path.slice(0, path.lastIndexOf("/")) || "/")))
        await edBrowse(ED.dir || "~");
      $("#ed-text").focus();
    });
    return;
  }
  let dir = ED.dir;
  try { dir = dir || localStorage.getItem("lb-ed-dir"); } catch {}
  if (!(await edBrowse(dir || "~"))) await edBrowse("~");
}

function edClose() {
  edGuard(() => {
    // closing = discarding: next open starts from disk, not a stale buffer
    if (ED.path !== null && edDirty()) $("#ed-text").value = ED.orig;
    $("#ed-confirm").classList.add("hidden");
    edRefreshChrome();
    $("#editor-panel").classList.add("hidden");
  });
}

// ✎ on a file-tool card (read_file/write_file/edit_file…): jump into the editor.
// Lives INSIDE the card's <summary> — never a bare #chat child.
function edLink(card, args) {
  const p = args && typeof args.file_path === "string" ? args.file_path : null;
  if (!p || !p.startsWith("/")) return;
  const b = el("button", "ed-open", "✎ open");
  b.title = "open " + p + " in the FILES editor";
  b.onclick = (e) => { e.preventDefault(); e.stopPropagation(); openEditor(p); };
  card.querySelector("summary").appendChild(b);
}

$("#btn-files").onclick = () => openEditor();
$("#btn-ed-close").onclick = () => { SFX.play("click"); edClose(); };
$("#btn-ed-save").onclick = () => edSave();
$("#btn-ed-revert").onclick = () => {
  SFX.play("click");
  $("#ed-text").value = ED.orig;
  edGutter();
  edRefreshChrome();
  edStatus("REVERTED to last loaded/saved version");
};
$("#editor-panel").onclick = (e) => { if (e.target.id === "editor-panel") edClose(); };
$("#ed-hidden").onchange = () => {
  if (ED.dir) edBrowse(ED.dir);
  if (!$("#ed-suggest").classList.contains("hidden")) sugUpdate();
};
// ---- path box: autocomplete (/… ~/…) + recursive name search (anything else)
// Path mode lists the typed folder (cached 10s) and filters by the last
// segment; a segment that matches nothing there falls through to a search
// BELOW that folder. Search mode asks /api/fs/find (rg, time-boxed) under
// the folder the tree shows. Seq-tokened: a slow reply for an older
// keystroke never paints over a newer one.
const SUG = { items: [], sel: -1, moved: false, seq: 0, abort: null, timer: null, dirs: new Map() };
const isPathish = (v) => v.startsWith("/") || v.startsWith("~");
const sepNorm = (x) => x.toLowerCase().replace(/[_\-\s]+/g, " ");

function sugClose() {
  clearTimeout(SUG.timer);
  SUG.seq++;
  if (SUG.abort) SUG.abort.abort();
  $("#ed-suggest").classList.add("hidden");
  SUG.items = [];
  SUG.sel = -1;
}

function sugRender(items, note) {
  const box = $("#ed-suggest");
  SUG.items = items;
  SUG.sel = items.length ? 0 : -1;
  SUG.moved = false;
  box.replaceChildren();
  items.forEach((it, i) => {
    const r = el("div", "ed-sug" + (it.dir ? " dir" : "") + (i === 0 ? " sel" : ""));
    // trailing LRM: .rel is direction:rtl (ellipsis eats the HEAD of long
    // paths); without it the bidi algorithm moves the final "/" to the front
    r.append(el("span", "nm", (it.dir ? "▸ " : "") + it.name),
             el("span", "rel", it.sub ? it.sub + "\u200E" : ""));
    r.title = it.path;
    // mousedown, not click: fires before the input's blur closes the list
    r.addEventListener("mousedown", (e) => { e.preventDefault(); sugPick(i); });
    box.appendChild(r);
  });
  if (note) box.appendChild(el("div", "ed-sug-note", note));
  box.classList.toggle("hidden", !items.length && !note);
}

function sugMove(d) {
  const n = SUG.items.length;
  if (!n) return;
  SUG.sel = (SUG.sel + d + n) % n;
  SUG.moved = true;
  const rows = $("#ed-suggest").querySelectorAll(".ed-sug");
  rows.forEach((r, i) => r.classList.toggle("sel", i === SUG.sel));
  rows[SUG.sel].scrollIntoView({ block: "nearest" });
}

async function sugPath(v, seq) {
  const cut = v.lastIndexOf("/");
  const dir = v.slice(0, cut + 1);
  const prefix = v.slice(cut + 1);
  let hit = SUG.dirs.get(dir);
  if (!hit || Date.now() - hit.t > 10000) {
    try {
      hit = { t: Date.now(), out: await edFetch("/api/fs/list?path=" + encodeURIComponent(dir)) };
    } catch (e) {
      if (seq === SUG.seq) sugRender([], "✕ " + dir + ": " + e.message);
      return;
    }
    SUG.dirs.set(dir, hit);
  }
  if (seq !== SUG.seq) return;
  const out = hit.out;
  const hid = $("#ed-hidden").checked || prefix.startsWith(".");
  const np = sepNorm(prefix);
  const ents = out.entries.filter((e) => hid || !e.name.startsWith("."));
  const starts = ents.filter((e) => sepNorm(e.name).startsWith(np));
  const inner = np ? ents.filter((e) => !sepNorm(e.name).startsWith(np) && sepNorm(e.name).includes(np)) : [];
  const base = out.path === "/" ? "" : out.path;
  const items = [...starts, ...inner].slice(0, 100).map((e) => ({
    name: e.name, dir: e.dir, path: base + "/" + e.name,
    value: dir + e.name + (e.dir ? "/" : ""), starts: sepNorm(e.name).startsWith(np),
  }));
  if (items.length || !prefix) { sugRender(items, items.length ? null : "(empty folder)"); return; }
  sugRender([], `nothing named “${prefix}” in ${dir} — searching below…`);
  await sugSearch(prefix, out.path, seq);
}

async function sugSearch(q, root, seq) {
  if (SUG.abort) SUG.abort.abort();
  SUG.abort = new AbortController();
  let out;
  try {
    out = await edFetch(`/api/fs/find?root=${encodeURIComponent(root)}&q=${encodeURIComponent(q)}` +
      `&hidden=${$("#ed-hidden").checked}`, { signal: SUG.abort.signal });
  } catch (e) {
    if (e.name !== "AbortError" && seq === SUG.seq) sugRender([], "✕ search failed: " + e.message);
    return;
  }
  if (seq !== SUG.seq) return;
  const items = out.results.map((r) => {
    const i = r.rel.lastIndexOf("/");
    return { name: r.rel.slice(i + 1), sub: i > 0 ? r.rel.slice(0, i) + "/" : "",
             dir: r.dir, path: r.path, value: r.path + (r.dir ? "/" : "") };
  });
  sugRender(items, !items.length ? `no matches under ${out.root}`
    : out.partial ? "… search time-boxed — add words to narrow" : null);
}

function sugUpdate() {
  const raw = $("#ed-path").value.trim();
  const seq = ++SUG.seq;
  clearTimeout(SUG.timer);
  if (!raw) { sugClose(); return; }
  if (isPathish(raw)) {
    const v = raw === "~" ? "~/" : raw;
    SUG.timer = setTimeout(() => sugPath(v, seq), 60);
  } else {
    const root = ED.dir || "~";
    sugRender([], `searching ${root} …`);
    SUG.timer = setTimeout(() => sugSearch(raw, root, seq), 250);
  }
}

function sugPick(i) {
  const it = SUG.items[i];
  if (!it) return;
  if (it.dir) {
    // drill in: box shows the folder, tree follows, list shows its contents
    $("#ed-path").value = it.value;
    edBrowse(it.path);
    sugUpdate();
  } else {
    $("#ed-path").value = it.path;
    sugClose();
    edGuard(() => edLoad(it.path));
  }
}

// Tab: shell-style — extend to the longest common prefix of the matches;
// when that adds nothing (or you've arrowed to one), take the selection
function sugTab() {
  const v = $("#ed-path").value;
  if (!SUG.items.length) return;
  if (isPathish(v) && !SUG.moved) {
    const vals = SUG.items.filter((it) => it.starts).map((it) => it.value);
    if (vals.length > 1) {
      let p = vals[0];
      for (const x of vals) while (!x.toLowerCase().startsWith(p.toLowerCase())) p = p.slice(0, -1);
      if (p.length > v.length) { $("#ed-path").value = p; sugUpdate(); return; }
    }
  }
  const it = SUG.items[Math.max(SUG.sel, 0)];
  $("#ed-path").value = it.value;
  if (it.dir) edBrowse(it.path);
  sugUpdate();
}

$("#ed-path").addEventListener("input", sugUpdate);
$("#ed-path").addEventListener("focus", () => { if ($("#ed-path").value.trim()) sugUpdate(); });
$("#ed-path").addEventListener("blur", () => setTimeout(sugClose, 120));
$("#ed-path").addEventListener("keydown", (e) => {
  const open = !$("#ed-suggest").classList.contains("hidden");
  if (e.key === "ArrowDown" && open) { e.preventDefault(); sugMove(1); }
  else if (e.key === "ArrowUp" && open) { e.preventDefault(); sugMove(-1); }
  else if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); sugTab(); }
  else if (e.key === "Escape" && open) { e.preventDefault(); e.stopPropagation(); sugClose(); } // list only, not the panel
  else if (e.key === "Enter") {
    e.preventDefault();
    if (open && SUG.sel >= 0) sugPick(SUG.sel);
    else { sugClose(); edGo($("#ed-path").value); }
  }
});
$("#ed-text").addEventListener("input", () => { edGutter(); edRefreshChrome(); });
$("#ed-text").addEventListener("scroll", edSyncScroll);
$("#ed-text").addEventListener("keydown", (e) => {
  if (e.key === "Tab" && !e.ctrlKey && !e.altKey && !e.metaKey) {
    e.preventDefault();
    // execCommand keeps the native undo stack (setting .value would wipe it)
    if (!e.shiftKey) document.execCommand("insertText", false, ED.indent);
  }
});
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s" &&
      !$("#editor-panel").classList.contains("hidden")) {
    e.preventDefault();
    if (ED.path !== null) edSave();
  }
});
window.addEventListener("beforeunload", (e) => { if (edDirty()) e.preventDefault(); });
$("#todo-head").onclick = () => {
  SFX.play("click");
  localStorage.setItem(
    "lb-todos-collapsed",
    localStorage.getItem("lb-todos-collapsed") === "on" ? "off" : "on");
  applyTodosCollapsed();
};
$("#traj-search").addEventListener("input", () => renderTraj());
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

// read-aloud toggle (per browser; provider is server CONFIG)
function applyVoiceMode() {
  $("#btn-voice").textContent = "🔊 VOICE: " + voiceMode.toUpperCase();
  $("#btn-voice").classList.toggle("on", voiceMode !== "off");
  $("#btn-voice").classList.toggle("talk", voiceMode === "talk");
}
// OFF → SPEAK → TALK (hands-free; only offered where the mic works) → OFF
$("#btn-voice").onclick = () => {
  SFX.play("click");
  voiceMode = voiceMode === "off" ? "speak"
    : voiceMode === "speak" && micSupported() ? "talk" : "off";
  localStorage.setItem("lb-voice", voiceMode === "off" ? "off" : "speak");
  if (voiceMode === "off") stopSpeaking();
  if (voiceMode !== "talk" && MIC.state === "rec") micStop(false);
  applyVoiceMode();
  if (voiceMode === "talk") talkTick(); // this click is the gesture the mic needs
};
applyVoiceMode();
// ---------- mic (speech → text) ----------
// Browsers hand out the mic only in a secure context (the https:// address,
// or http://localhost). One tap records one utterance; it ends itself after
// ~1.4 s of quiet once speech was heard (or on a second tap), /api/stt
// transcribes it, and the text lands in the composer — sent straight away
// when VOICE: SPEAK is on and the box was empty (talk → hear the answer).
// Its own short-lived AudioContext: sfx.js's page-lifetime one is for output.
const MIC = { state: "idle", stream: null, ctx: null, node: null, chunks: [], rate: 48000 };
const MIC_QUIET_MS = 1400, MIC_NOSPEECH_MS = 8000, MIC_MAX_MS = 60000;
const TALK_NOSPEECH_MS = 30000; // hands-free: re-arm quietly after this
const micSupported = () => window.isSecureContext && !!navigator.mediaDevices?.getUserMedia;

function micPaint() {
  const b = $("#btn-mic");
  b.classList.toggle("on", MIC.state === "rec");
  b.classList.toggle("busy", MIC.state === "stt");
  b.textContent = MIC.state === "rec" ? "● REC" : MIC.state === "stt" ? "… STT" : "🎙 MIC";
  if (MIC.state !== "rec") b.style.removeProperty("--lvl");
  if (!micSupported()) {
    b.disabled = true;
    b.title = "The mic needs HTTPS (or localhost): open LangBang at its https:// address";
  }
}

async function micStart() {
  if (MIC.state !== "idle") return;
  MIC.state = "arming"; // getUserMedia is async: talkTick must not open a second one
  stopSpeaking(); // never record our own read-aloud
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) {
    MIC.state = "idle";
    SFX.play("error");
    if (voiceMode === "talk") { voiceMode = "speak"; applyVoiceMode(); } // don't retry-loop a refusal
    return voiceFail("MIC: " + (e.name === "NotAllowedError"
      ? "permission denied: allow the microphone for this site" : e.message || e.name));
  }
  const ctx = new AudioContext();
  // TALK re-opens outside a click; browsers allow that once the page had a
  // gesture and the mic is live — but never hang on a context kept suspended
  await Promise.race([ctx.resume(), new Promise((r) => setTimeout(r, 1500))]);
  if (ctx.state !== "running") {
    stream.getTracks().forEach((t) => t.stop());
    ctx.close();
    MIC.state = "idle";
    if (voiceMode === "talk") { voiceMode = "speak"; applyVoiceMode(); }
    return voiceFail("MIC: the browser kept audio paused; tap 🎙 (or VOICE → TALK) to continue");
  }
  const src = ctx.createMediaStreamSource(stream);
  // ScriptProcessor (deprecated but everywhere): no worklet module to serve,
  // and it only pulls audio while connected to the destination (outputs silence)
  const node = ctx.createScriptProcessor(4096, 1, 1);
  Object.assign(MIC, { state: "rec", stream, ctx, node, chunks: [], rate: ctx.sampleRate });
  let floor = 0, nFloor = 0, heard = false, quietMs = 0, totalMs = 0;
  node.onaudioprocess = (e) => {
    if (MIC.state !== "rec") return;
    const x = e.inputBuffer.getChannelData(0);
    MIC.chunks.push(new Float32Array(x));
    let sum = 0;
    for (let i = 0; i < x.length; i++) sum += x[i] * x[i];
    const rms = Math.sqrt(sum / x.length), ms = (x.length / MIC.rate) * 1000;
    totalMs += ms;
    if (totalMs < 300) { floor += rms; nFloor++; return; } // room noise floor
    const thr = Math.max(0.012, (floor / Math.max(1, nFloor)) * 3);
    if (rms > thr) { heard = true; quietMs = 0; } else quietMs += ms;
    $("#btn-mic").style.setProperty("--lvl", Math.min(1, rms / (thr * 4)).toFixed(2));
    if ((heard && quietMs > MIC_QUIET_MS) || totalMs > MIC_MAX_MS) micStop(true);
    else if (!heard && totalMs > (voiceMode === "talk" ? TALK_NOSPEECH_MS : MIC_NOSPEECH_MS)) micStop(false);
  };
  src.connect(node);
  node.connect(ctx.destination);
  micPaint();
}

async function micStop(keep) {
  if (MIC.state !== "rec") return;
  MIC.state = keep ? "stt" : "idle";
  MIC.node.onaudioprocess = null;
  MIC.node.disconnect();
  MIC.stream.getTracks().forEach((t) => t.stop()); // browser's mic indicator goes off
  MIC.ctx.close();
  const chunks = MIC.chunks, rate = MIC.rate;
  MIC.chunks = [];
  micPaint();
  if (!keep) return;
  try {
    const res = await fetch("/api/stt", { method: "POST", headers: { "Content-Type": "audio/wav" },
                                          body: wav16k(chunks, rate) });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(out.detail || "HTTP " + res.status);
    const text = (out.text || "").trim();
    if (!text) { // noise, a cough: hands-free just listens again
      if (voiceMode !== "talk") voiceFail("MIC: didn't catch that, try again");
      return;
    }
    const box = $("#input");
    const wasEmpty = !box.value.trim();
    box.value = wasEmpty ? text : box.value.trimEnd() + " " + text;
    box.dispatchEvent(new Event("input")); // autosize / path-suggest listeners
    const auto = voiceMode === "talk" || (wasEmpty && voiceMode === "speak");
    if (auto && !(threadId && RUNS.has(threadId))) {
      MIC.state = "idle";
      micPaint();
      talkSending = true; // send() resolves when the run ends; the answer is queued by then
      try { await send(); } finally { talkSending = false; }
    } else box.focus();
  } catch (e) {
    voiceFail("MIC: transcription failed: " + e.message);
  } finally {
    MIC.state = "idle";
    micPaint();
  }
}

// Float32 chunks at the context rate → 16 kHz mono PCM16 WAV (what /api/stt
// takes). Box-filter decimation: averaging each output sample's input window
// is the low-pass, so 48 kHz speech doesn't alias into the 0–8 kHz band.
function wav16k(chunks, rate) {
  let n = 0;
  for (const c of chunks) n += c.length;
  const all = new Float32Array(n);
  let o = 0;
  for (const c of chunks) { all.set(c, o); o += c.length; }
  const ratio = rate / 16000, len = Math.floor(n / ratio);
  const pcm = new Int16Array(len);
  for (let i = 0; i < len; i++) {
    const a = Math.floor(i * ratio), b = Math.min(n, Math.max(a + 1, Math.floor((i + 1) * ratio)));
    let s = 0;
    for (let j = a; j < b; j++) s += all[j];
    pcm[i] = Math.max(-1, Math.min(1, s / (b - a))) * 0x7fff;
  }
  const buf = new ArrayBuffer(44 + pcm.length * 2), d = new DataView(buf);
  const tag = (p, s) => { for (let i = 0; i < 4; i++) d.setUint8(p + i, s.charCodeAt(i)); };
  tag(0, "RIFF"); d.setUint32(4, 36 + pcm.length * 2, true); tag(8, "WAVE");
  tag(12, "fmt "); d.setUint32(16, 16, true); d.setUint16(20, 1, true); d.setUint16(22, 1, true);
  d.setUint32(24, 16000, true); d.setUint32(28, 32000, true); d.setUint16(32, 2, true);
  d.setUint16(34, 16, true); tag(36, "data"); d.setUint32(40, pcm.length * 2, true);
  new Int16Array(buf, 44).set(pcm);
  return new Blob([buf], { type: "audio/wav" });
}

$("#btn-mic").onclick = () => {
  SFX.play("click");
  if (MIC.state === "idle") micStart(); // also barges in on read-aloud
  else if (MIC.state === "rec") micStop(true);
};
micPaint();

// ---- TALK (hands-free): listen → send on a pause → hear the answer → listen.
// The mic re-opens only once everything is quiet: no run in this thread, no
// clip playing / synthesizing / queued — so it never records the read-aloud
// (no barge-in; tap 🎙 to cut an answer short and talk). VOICE → OFF ends it.
let talkSending = false;
function talkTick() {
  if (voiceMode !== "talk" || MIC.state !== "idle" || talkSending) return;
  if (voiceCur || synthing || speakQ.length) return;
  if ((threadId && RUNS.has(threadId)) || uploading) return;
  micStart();
}
setInterval(talkTick, 400);

// runs need voice.speak_reasoning before CONFIG is ever opened
api.settings().then((s) => {
  loadedVoice = { ...DEFAULT_VOICE, ...(s.voice || {}) };
  applySplash(s.splash_image);
}).catch(() => {});

// NEW CHAT splash: CSS paints /api/splash behind an EMPTY #chat (a ::before,
// so no node lands in #chat), only on a draft — never while a thread loads
function syncDraft() { document.body.classList.toggle("draft", !threadId); }
// boot: a ?thread= link is about to open a thread — don't flash the splash first
let splashVer = 0;
function applySplash(path) {
  document.body.classList.toggle("has-splash", !!(path || "").trim());
  // cache-bust on change: same URL, different file
  document.documentElement.style.setProperty("--splash", `url("/api/splash?v=${++splashVer}")`);
}
if (!new URLSearchParams(location.search).get("thread")) syncDraft();

checkHealth();
setInterval(checkHealth, 15000);
// detached-run poller: runs live on the SERVER, so a phone sleeping / a tab
// backgrounding / a laptop lid no longer stops them — this finds their hubs,
// puts ▶ dots on their sidebar rows, re-attaches zombie streams, and
// reconciles bundles whose hub vanished (server restart). visibilitychange
// makes a phone wake re-attach instantly instead of waiting for the next
// tick. The initial call doubles as the boot-time attach for a ?thread= deep
// link into a mid-run thread (openThread ran before serverRuns was filled).
pollRuns();
setInterval(pollRuns, 4000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollRuns(); });
refreshThreads().then(() => {
  const p = new URLSearchParams(location.search);
  const t = p.get("thread");
  if (t) { const th = threads.find((x) => x.id === t); if (th) openThread(th); }
  const q = p.get("q"); // shareable search deep-link
  if (q) { $("#search").value = q; runSearch(); }
});

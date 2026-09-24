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
let pendingFiles = []; // {file} non-image attachments, uploaded at send time
let uploading = false; // blocks re-Enter while attachment bytes are in flight
const MAX_IMAGES = 4;
const MAX_IMG_BYTES = 6 * 1024 * 1024;
// non-image attachments upload to data/uploads/ on send; caps mirror main.py
const MAX_FILES = 6;
const MAX_FILE_BYTES = 20 * 1024 * 1024;
const IMG_TYPE_RE = new RegExp("^image/(png|jpeg|webp|gif)$");

// agentic-visibility state
let run = null;        // live-run bundle {t0, iv, tools:Map, subs:Map, stats} while streaming
let trajCache = null;  // trajectory rows/totals for the current thread
let trajVisible = false;

// read-aloud state (which provider speaks is the server's CONFIG; we just
// queue mp3 clips from /api/tts and play one at a time)
let voiceMode = localStorage.getItem("lb-voice") === "speak" ? "speak" : "off";

// ---------- API ----------
const api = {
  async settings() { return (await fetch("/api/settings")).json(); },
  async saveSettings(patch) {
    return (await fetch("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patch }),
    })).json();
  },
  async mcpTest(config) {
    return (await fetch("/api/mcp/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    })).json();
  },
  async soundsSlots() { return (await fetch("/api/sounds/slots")).json(); },
  async sfxRegen(slot) {
    return (await fetch("/api/sounds/regen", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slot }),
    })).json();
  },
  async sfxRegenStatus(slot) { return (await fetch("/api/sounds/regen/" + slot)).json(); },
  async health() { return (await fetch("/api/health")).json(); },
  async threads() { return (await fetch("/api/threads")).json(); },
  async newThread(title) {
    return (await fetch("/api/threads", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    })).json();
  },
  async delThread(id) { await fetch("/api/threads/" + id, { method: "DELETE" }); },
  async renameThread(id, title) {
    return (await fetch("/api/threads/" + id, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    })).json();
  },
  async autoTitle(id) {
    return (await fetch(`/api/threads/${id}/retitle`, { method: "POST" })).json();
  },
  async revertTitle(id) {
    return (await fetch(`/api/threads/${id}/revert-title`, { method: "POST" })).json();
  },
  async recap(id) {
    return (await fetch(`/api/threads/${id}/recap`, { method: "POST" })).json();
  },
  async touchThread(id) { await fetch("/api/threads/" + id + "/touch", { method: "POST" }); },
  async messages(id) { return (await fetch(`/api/threads/${id}/messages`)).json(); },
  async trajectory(id) { return (await fetch(`/api/threads/${id}/trajectory`)).json(); },
  async search(q) { return (await fetch("/api/search?q=" + encodeURIComponent(q))).json(); },
  async schedules() { return (await fetch("/api/schedules")).json(); },
  async newSchedule(t) {
    return (await fetch("/api/schedules", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(t),
    })).json();
  },
  async putSchedule(id, patch) {
    return (await fetch("/api/schedules/" + id, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch),
    })).json();
  },
  async delSchedule(id) { await fetch("/api/schedules/" + id, { method: "DELETE" }); },
  async runSchedule(id) {
    return (await fetch("/api/schedules/" + id + "/run", { method: "POST" })).json();
  },
  async cronNext(cron) {
    return (await fetch("/api/schedules/next?cron=" + encodeURIComponent(cron))).json();
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

function addMsgRaw(cls, text) {
  // plain-text row (no markdown round-trip) — usage readouts etc.
  const m = el("div", cls, text);
  $("#chat").appendChild(m);
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
  $("#chat").innerHTML = "";
  $("#chat")._pinned = true; // opening a thread always shows its newest message
  // run provenance: a scheduled firing's segment = the stamped prompt up to
  // (exclusive) the next human message. A human WITHOUT a stamp is manual
  // chat (even inside a schedule's thread) and ends the segment. Survives
  // compaction — archived heads keep their sched stamps in their JSON.
  let curSched = null;
  for (const m of msgs) {
    if (m.role === "system") continue;
    if (m.compacted) {
      const b = addBlock("compact", `⟲ CONTEXT COMPACTED — ${m.compacted.count} EARLIER MSGS SUMMARIZED`);
      b.querySelector("pre").textContent = textOf(m.content);
      continue; // notes sit between runs; never touch curSched
    }
    if (m.shell) {
      const b = shellBlock(m.shell.cmd);
      shellSeal(b, m.shell);
      continue;
    }
    if (m.role === "human") {
      const b = addMsg("user", textOf(m.content), imagesOf(m.content));
      if (m.sched) b.prepend(schedStrip(m.sched));
      curSched = m.sched || null;
    } else if (m.role === "ai") {
      if (m.thinking) {
        const b = addBlock("thinking", "◈ THINKING");
        b.querySelector("pre").textContent = m.thinking;
      }
      if (textOf(m.content).trim()) {
        const b = addMsg("assistant", textOf(m.content));
        if (curSched) b.prepend(runChip(curSched));
        attachSpeak(b, textOf(m.content));
      }
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
  if ((!text && !images.length && !pendingFiles.length) || streaming || uploading) return;
  // `!cmd` = shell mode (Claude Code style): run on the server, no model call
  if (!images.length && !pendingFiles.length && text.startsWith("!")) {
    const cmd = text.slice(1).trim();
    if (!cmd) return;
    $("#input").value = "";
    await runShell(cmd);
    return;
  }
  stopSpeaking(); // a new run interrupts whatever was being read aloud
  if (!threadId) await newThread();
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
  $("#chat")._pinned = true; // sending always reveals your own message
  addMsg("user", msgText, images);
  SFX.play("message_sent");
  streaming = true;
  aborter = new AbortController();
  setBusy(true);
  // live-run bundle: timers are recomputed from t0 on every tick (background
  // tabs throttle intervals — never accumulate elapsed by counting ticks)
  run = {
    t0: Date.now(),
    iv: setInterval(tickRun, 250),
    tools: new Map(), // run_id -> card el; pairs start/end even when parallel
    subs: new Map(),  // task run_id -> {card, body, t0}
    stats: { steps: 0, llm_s: 0, tool_s: 0, in: 0, out: 0, ttfts: 0, ttft_n: 0 },
    todosTouched: false, // did THIS run write the list? drives the stale badge
  };
  $("#sb-live").classList.remove("hidden");
  // In-pane liveness card: on a big context the first token can take 30s+
  // (prefill), and a chat pane that shows nothing reads as "broken". Lives
  // until the first stream event of any kind (handleEvent) or teardown.
  const ctxTok = (threads.find((x) => x.id === threadId) || {}).context_tokens || 0;
  run.waitLabel = "⏳ AWAITING MODEL" + (ctxTok ? ` — CTX ~${fmtTok(ctxTok)}` : "");
  run.waiting = addBlock("waiting", run.waitLabel + " …");
  run.waiting.open = true;
  tickRun();

  let asstMsg = null; // created lazily on first visible token — no empty cursor boxes
  let asstRaw = "";
  let thinkingBlock = null;
  let renderTimer = null;
  const ensureAsst = () => {
    if (!asstMsg) {
      asstMsg = addMsg("assistant", "");
      asstRaw = "";
      asstMsg.classList.add("cursor");
    }
  };
  const scheduleRender = () => {
    if (renderTimer) return;
    renderTimer = setTimeout(() => {
      renderTimer = null;
      if (asstMsg) setMarkdown(asstMsg, asstRaw);
      scrollBottom();
    }, 80);
  };
  // MUST run before any finalize-then-append (attachSpeak): a pending render
  // would fire after the append and setMarkdown() would wipe the new child
  const flushRender = () => {
    if (!renderTimer) return;
    clearTimeout(renderTimer);
    renderTimer = null;
    if (asstMsg) setMarkdown(asstMsg, asstRaw);
  };

  try {
    const res = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: threadId, text: msgText, images }),
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
  if (run.waiting) run.waiting.remove(); // zero-event ends (error/abort) never hit handleEvent
  if (asstMsg) {
    flushRender();
    asstMsg.classList.remove("cursor");
    attachSpeak(asstMsg, asstRaw); // final answer bubble (partial on abort/error — speakable anyway)
  }
  streaming = false;
  setBusy(false);
  clearInterval(run.iv);
  // aborted/errored runs: cards whose tool_end never arrived get sealed ✕ —
  // never leave a spinner that will never stop
  for (const card of run.tools.values()) {
    const s = card.querySelector("summary");
    s.textContent = "✕ " + s.textContent.replace(/ …$/, "");
    card.open = false;
  }
  run = null;
  $("#sb-live").classList.add("hidden");
  await refreshThreads();
  // rows are committed per-event server-side, so totals read back coherently
  // even after an abort or an error mid-run
  refreshStats();

  function handleEvent(ev) {
    if (run && run.waiting) { run.waiting.remove(); run.waiting = null; } // first sign of life
    if (ev.type === "token") {
      if (ev.text.trim()) ensureAsst(); // whitespace-only content never opens a bubble
      if (asstMsg) { asstRaw += ev.text; scheduleRender(); }
    }
    else if (ev.type === "thinking") {
      if (!thinkingBlock) { thinkingBlock = addBlock("thinking", "◈ THINKING"); }
      thinkingBlock.querySelector("pre").textContent += ev.text;
    } else if (ev.type === "tool_start") {
      SFX.play("tool_start");
      // close the current answer bubble; the next one opens on its first token
      if (asstMsg) {
        flushRender();
        asstMsg.classList.remove("cursor");
        attachSpeak(asstMsg, asstRaw);
        asstMsg = null;
      }
      let card, host = $("#chat");
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
        if (ev.sub && run.subs.get(ev.sub)) host = run.subs.get(ev.sub).body;
      }
      card.open = true;
      host.appendChild(card);
      run.tools.set(ev.run_id, card);
    } else if (ev.type === "tool_end") {
      SFX.play("tool_end");
      run.stats.steps++;
      if (ev.name !== "task" && typeof ev.dur === "number") run.stats.tool_s += ev.dur;
      const card = run.tools.get(ev.run_id);
      run.tools.delete(ev.run_id);
      if (!card) {
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
    } else if (ev.type === "todos") {
      if (run) run.todosTouched = true;
      renderTodos(ev.todos);
    } else if (ev.type === "usage") {
      // one line per model call (ReAct rounds and the compaction summarizer
      // each report their own); prefill_tps is ttft-inclusive, so it's a
      // lower bound on real prefill speed — label kept honest as "~".
      run.stats.steps++;
      run.stats.in += ev.input || 0;
      run.stats.out += ev.output || 0;
      run.stats.llm_s += ev.seconds || 0;
      run.stats.ttfts += ev.ttft || 0;
      run.stats.ttft_n++;
      addMsgRaw(
        "usage",
        `⚡ IN ${fmtTok(ev.input)} → OUT ${fmtTok(ev.output)} · TTFT ${ev.ttft}s · ` +
          `PREFILL ~${fmtTok(ev.prefill_tps)}/s · DECODE ${ev.decode_tps}/s · ${ev.seconds}s`
      );
    } else if (ev.type === "error") {
      SFX.play("error");
      addMsg("error", ev.message);
    } else if (ev.type === "done") {
      SFX.play("message_received");
      // run ended without ever writing the list while items sit open → the
      // card shows a mid-run snapshot; say so (model finished, bookkeeping
      // didn't follow — e.g. a resumed run that dove straight back to work)
      if (run && !run.todosTouched) markTodosStale();
      // read the FINAL answer bubble only — mid-run "let me check…" bubbles
      // keep their manual 🔊 (auto-reading play-by-play is filler audio)
      if (voiceMode === "speak" && asstRaw.trim()) speakRaw(asstRaw);
    }
    scrollBottom();
  }
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
  if (aborter) aborter.abort();
}

// ---------- shell mode (`!cmd`): run on the server, no model call ----------
function shellBlock(cmd) {
  const b = addBlock("shell", `$ ${cmd} …`);
  b.open = true; // user ran it to see the result — never hide the output
  return b;
}

function shellSeal(b, { cmd, out, exit, dur }) {
  b.querySelector("summary").textContent =
    `$ ${cmd} · exit=${exit ?? "?"} · ${fmtShort(dur || 0)}`;
  b.querySelector("pre").textContent = out;
}

async function runShell(cmd) {
  if (!threadId) await newThread();
  streaming = true; // reuse the chat gate: no model run may interleave a shell
  aborter = new AbortController();
  setBusy(true);
  SFX.play("tool_start");
  $("#chat")._pinned = true; // same reveal rule as send()
  const card = shellBlock(cmd);
  try {
    const res = await fetch("/api/shell", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: threadId, command: cmd }),
      signal: aborter.signal,
    });
    if (!res.ok) throw new Error((await res.text()).slice(0, 300) || String(res.status));
    shellSeal(card, await res.json());
    SFX.play("tool_end");
  } catch (e) {
    if (e.name === "AbortError") {
      // STOP only abandons the view: the server-side command runs to
      // completion and is saved to the thread — a reload shows its card
      card.querySelector("summary").textContent = "✕ $ " + cmd;
    } else {
      SFX.play("error");
      card.remove();
      addMsg("error", "SHELL FAILED: " + e.message);
    }
  }
  streaming = false;
  aborter = null;
  setBusy(false);
  await refreshThreads(); // context grew — CTX chip and thread order may move
  refreshStats();
}

// ---------- agentic visibility: live run bar, to-dos, totals, trajectory ----------
function tickRun() {
  if (!run) return;
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
// so this survives compaction (archived messages keep their args)
function replayTodos(msgs) {
  for (let i = msgs.length - 1; i >= 0; i--)
    for (const tc of msgs[i].tool_calls || [])
      if (tc.name === "write_todos" && Array.isArray(tc.args?.todos))
        return renderTodos(tc.args.todos);
  hideTodos();
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
  if (trajCache && !streaming) renderTraj();
  else refreshStats();
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
  const BADGE = { user: "USER", model: "ASSISTANT", tool: "TOOL", error: "ERROR" };
  // Rows land in seq order, but tool rows are logged at span END — a task's
  // children would render above it. Sort by effective START (end - dur); the
  // sort is stable, so ties keep log order.
  const st = (e) => e.ts - (typeof e.dur === "number" ? e.dur : 0);
  for (const g of groups) g.rows.sort((a, b) => st(a) - st(b));
  groups.forEach((g, gi) => {
    const t0 = Math.min(...g.rows.map(st));
    const span = Math.max(Math.max(...g.rows.map((e) => e.ts)) - t0, 0.001);
    const card = el("div", "traj-card");
    card.appendChild(
      el("div", "traj-head",
        `RUN ${gi + 1} · ${new Date(t0 * 1000).toLocaleTimeString()} · ${fmtShort(span)}`)
    );
    const strip = el("div", "traj-strip");
    strip.appendChild(el("i", "traj-tick input")); // input sits at 0 (CSS left:0)
    for (const e of g.rows) {
      if (e.type !== "model" && e.type !== "tool") continue;
      const tk = el("i", "traj-tick " + e.type);
      tk.style.left = (100 * (st(e) - t0)) / span + "%";
      tk.title = `${e.name || e.type}${e.dur != null ? " · " + fmtShort(e.dur) : ""}`;
      strip.appendChild(tk);
    }
    card.appendChild(strip);
    for (const e of g.rows) {
      const m = e.meta || {};
      const label = BADGE[e.type] || e.type;
      const preview =
        e.type === "tool" ? (m.desc ? "◈ " + m.desc : String(m.in || ""))
        : e.type === "error" ? String(m.message || "")
        : String(m.text || "");
      const row = el("div", "traj-row" + (m.sub ? " sub" : ""));
      row.appendChild(el("span", "traj-badge " + e.type, label));
      row.appendChild(el("span", "traj-name", e.name || (e.dur != null ? fmtShort(e.dur) : "")));
      row.appendChild(el("span", "traj-pre", preview));
      if (q && !(label + " " + (e.name || "") + " " + preview).toLowerCase().includes(q))
        row.classList.add("hidden");
      row.onclick = () => {
        if (row._meta) { row._meta.remove(); row._meta = null; return; }
        // meta pre is a SIBLING of the row — keep a direct ref to toggle it
        row._meta = el("pre", "traj-meta", JSON.stringify({ ...e, meta: m }, null, 2));
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
async function refreshThreads() {
  threads = await api.threads();
  const box = $("#threads");
  box.innerHTML = "";
  for (const t of threads) {
    const d = el("div", "thread" + (t.id === threadId ? " active" : ""));
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
    x.onclick = async (e) => {
      e.stopPropagation();
      await api.delThread(t.id);
      if (t.id === threadId) { threadId = null; $("#chat").innerHTML = ""; resetTrajView(); }
      refreshThreads();
    };
    d.appendChild(x);
    d.onclick = () => openThread(t);
    box.appendChild(d);
  }
  $("#btn-recap").disabled = !threadId; // recap needs an open thread
  updateCtxTag();
}

function startInlineRename(t, row, name) {
  const inp = el("input", "t-edit");
  inp.value = t.title === "New chat" ? "" : t.title;
  inp.placeholder = t.title || "title…";
  row.replaceChild(inp, name);
  inp.focus();
  inp.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
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

async function openThread(t) {
  SFX.play("click");
  if (recapAbort) recapAbort.abort();
  closeRecap(); // one thread's recap must not follow you to another
  threadId = t.id;
  $("#chat-title").textContent = t.title.toUpperCase();
  const bumped = api.touchThread(t.id); // resuming = current: sorts it to the top
  const msgs = await api.messages(t.id);
  renderHistory(msgs);
  replayTodos(msgs); // last write_todos call re-draws the panel on switch/reload
  refreshStats();
  await bumped; // order may already have moved — refresh AFTER the bump lands
  refreshThreads();
}

async function newThread() {
  const t = await api.newThread("New chat");
  if (recapAbort) recapAbort.abort();
  closeRecap();
  threadId = t.id;
  $("#chat").innerHTML = "";
  $("#chat-title").textContent = "NEW CHAT";
  resetTrajView();
  SFX.play("thread_new");
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
// Synthesis happens server-side (/api/tts -> one mp3 per request, whole
// reply at once); we keep a small objectURL queue and play one clip at a
// time. A fresh speak interrupts whatever was playing — one voice, ever.
const speakAudio = new Audio();
let speakQ = [];      // pending object URLs
let speakBusy = false;
let speakOwner = null; // bubble whose 🔊 currently reads (■ STOP state)

async function speakRaw(raw, owner = null) {
  const text = raw.trim();
  if (!text) return;
  let res;
  try {
    res = await fetch("/api/tts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
  } catch (e) {
    return voiceFail("VOICE: server unreachable — " + e);
  }
  if (!res.ok) {
    let msg = "HTTP " + res.status;
    try { msg = (await res.json()).detail || msg; } catch {}
    return voiceFail("VOICE: " + msg);
  }
  speakQ.push(URL.createObjectURL(await res.blob()));
  speakOwner = owner;
  pumpSpeak();
}

function pumpSpeak() {
  if (speakBusy || !speakQ.length) return;
  speakBusy = true;
  speakAudio.src = speakQ.shift();
  speakAudio.play().catch((e) => {
    speakBusy = false; // autoplay/codec problem — voiceFail throttles repeats
    voiceFail("VOICE: playback failed — " + e);
  });
}

function stopSpeaking() {
  for (const u of speakQ) URL.revokeObjectURL(u);
  speakQ = [];
  speakBusy = false;
  speakAudio.pause();
  speakAudio.removeAttribute("src");
  speakAudio.load();
  markSpeaking(null);
}

speakAudio.onended = speakAudio.onerror = () => {
  speakBusy = false;
  if (!speakQ.length) markSpeaking(null); // natural end (or dead clip skipped)
  pumpSpeak();
};

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

let lastVoiceFail = 0;
function voiceFail(msg) {
  const now = Date.now();
  if (now - lastVoiceFail < 3000) return; // one bubble per burst, not per retry
  lastVoiceFail = now;
  addMsg("error", msg);
  SFX.play("error");
}

// 🔊 rides INSIDE the bubble (.msg) — never as a bare #chat child (the
// search-jump feature keys off #chat child indices). setMarkdown() rewrites
// innerHTML while streaming, so callers attach only at FINALIZATION.
function attachSpeak(msg, raw) {
  if (!raw || !raw.trim() || msg.querySelector(".speak-btn")) return;
  const b = el("button", "cb-btn speak-btn", "🔊");
  b.title = "Read this reply aloud";
  b.onclick = (e) => {
    e.stopPropagation();
    const stopping = b.textContent === "■ STOP"; // read state BEFORE reset
    stopSpeaking(); // also clears any other bubble that was reading
    if (stopping) return;
    markSpeaking(msg); // immediate ■ STOP feedback — synthesis may take seconds
    speakRaw(raw, msg);
  };
  msg._speakBtn = b;
  msg.appendChild(b);
}

// ---------- settings ----------
// config.save() merges the TOP level only — nested dicts must be sent whole,
// hence the spread-from-loaded base in saveSettings.
const DEFAULT_VOICE = {
  tts_provider: "gtts", tts_lang: "en", tts_tld: "com",
  stt_provider: "sr", stt_lang: "en-US",
  gcloud_key_file: "", gcloud_tts_lang: "en-US", gcloud_tts_voice: "en-US-Wavenet-J",
};
let loadedVoice = { ...DEFAULT_VOICE };

async function openSettings() {
  const s = await api.settings();
  for (const k of ["base_url", "model", "temperature", "max_tokens", "max_react_iterations", "system_prompt",
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
  $("#set-thinking").checked = !!s.enable_thinking;
  $("#set-compact").checked = !!s.compact_enabled;
  $("#set-deep_agent").checked = !!s.deep_agent;
  $("#set-skills_enabled").checked = !!s.skills_enabled;
  loadedVoice = { ...DEFAULT_VOICE, ...(s.voice || {}) };
  for (const k of Object.keys(DEFAULT_VOICE))
    $("#set-voice-" + k).value = loadedVoice[k];
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
    compact_enabled: $("#set-compact").checked,
    deep_agent: $("#set-deep_agent").checked,
    skills_enabled: $("#set-skills_enabled").checked,
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
    },
  });
  $("#settings-panel").classList.add("hidden");
  SFX.play("settings_saved");
  checkHealth();
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
    const enLab = el("label", "sched-en");
    const en = document.createElement("input");
    en.type = "checkbox";
    en.checked = !!s.enabled;
    en.onchange = async () => { await api.putSchedule(s.id, { enabled: en.checked }); refreshSchedules(); };
    enLab.append(en, document.createTextNode(" enabled"));
    head.appendChild(enLab);
    card.appendChild(head);

    card.appendChild(el("div", "sched-sub", "⏰ " + s.human + "  ·  " + s.cron));
    card.appendChild(
      el("div", "sched-sub dim",
         (s.running ? "◉ RUNNING · " : "") +
         (s.enabled ? "next " + schedWhen(s.next_run) : "paused") +
         " · last " + schedWhen(s.last_run)));

    const act = el("div", "sched-actions");
    const run = el("button", "btn ghost sm", "▶ RUN");
    run.title = "Fire one run now (does not shift the cron rhythm)";
    run.onclick = async () => { SFX.play("click"); await api.runSchedule(s.id); setTimeout(refreshSchedules, 1200); };
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
  $("#sched-cron").value = s.cron;
  schedShowEditor(true);
  schedPreviewNow();
  $("#sched-editor").scrollIntoView({ block: "nearest" });
}

function schedNew() {
  SFX.play("click");
  schedEditing = null;
  $("#sched-editor-title").textContent = "NEW TASK";
  $("#sched-title").value = "";
  $("#sched-prompt").value = "";
  $("#sched-cron").value = "";
  $("#sched-preview").textContent = "";
  schedShowEditor(true);
  $("#sched-title").focus();
}

async function saveScheduleTask() {
  const err = $("#sched-error");
  err.classList.add("hidden");
  const body = {
    title: $("#sched-title").value.trim(),
    prompt: $("#sched-prompt").value.trim(),
    cron: $("#sched-cron").value.trim(),
  };
  if (!body.title || !body.prompt || !body.cron) {
    err.textContent = "Title, prompt and cron are all required.";
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
$("#sched-cron").addEventListener("input", () => {
  clearTimeout(schedPrevT);
  schedPrevT = setTimeout(schedPreviewNow, 350);
});
$("#btn-schedules").onclick = openSchedules;
$("#btn-sched-close").onclick = () => { SFX.play("click"); closeSchedules(); };
$("#btn-sched-new").onclick = schedNew;
$("#btn-sched-save").onclick = saveScheduleTask;

// ---------- boot ----------
async function checkHealth() {
  const h = await api.health();
  const el2 = $("#health");
  el2.textContent = "LINK: " + (h.backend_up ? "ONLINE" : "OFFLINE");
  el2.className = "health " + (h.backend_up ? "up" : "down");
  $("#model-tag").textContent = h.model;
  supportsVision = !!h.supports_vision;
  // attach stays offered without vision: non-image files (and images as
  // plain files) ride the upload path — the agent opens them from disk
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

$("#btn-recap").onclick = openRecap;
$("#btn-recap-close").onclick = () => { SFX.play("click"); closeRecap(); };
$("#recap-panel").onclick = (e) => { if (e.target.id === "recap-panel") closeRecap(); };
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#recap-panel").classList.contains("hidden")) closeRecap();
});
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
  $("#btn-voice").textContent = "🔊 VOICE: " + (voiceMode === "speak" ? "SPEAK" : "OFF");
  $("#btn-voice").classList.toggle("on", voiceMode === "speak");
}
$("#btn-voice").onclick = () => {
  SFX.play("click");
  voiceMode = voiceMode === "speak" ? "off" : "speak";
  localStorage.setItem("lb-voice", voiceMode);
  if (voiceMode === "off") stopSpeaking();
  applyVoiceMode();
};
applyVoiceMode();

checkHealth();
setInterval(checkHealth, 15000);
refreshThreads().then(() => {
  const p = new URLSearchParams(location.search);
  const t = p.get("thread");
  if (t) { const th = threads.find((x) => x.id === t); if (th) openThread(th); }
  const q = p.get("q"); // shareable search deep-link
  if (q) { $("#search").value = q; runSearch(); }
});

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
  async health() { return (await fetch("/api/health")).json(); },
  async threads() { return (await fetch("/api/threads")).json(); },
  async newThread(title) {
    return (await fetch("/api/threads", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    })).json();
  },
  async delThread(id) { await fetch("/api/threads/" + id, { method: "DELETE" }); },
  async touchThread(id) { await fetch("/api/threads/" + id + "/touch", { method: "POST" }); },
  async messages(id) { return (await fetch(`/api/threads/${id}/messages`)).json(); },
  async trajectory(id) { return (await fetch(`/api/threads/${id}/trajectory`)).json(); },
  async search(q) { return (await fetch("/api/search?q=" + encodeURIComponent(q))).json(); },
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
    if (m.compacted) {
      const b = addBlock("compact", `⟲ CONTEXT COMPACTED — ${m.compacted.count} EARLIER MSGS SUMMARIZED`);
      b.querySelector("pre").textContent = textOf(m.content);
      continue;
    }
    if (m.shell) {
      const b = shellBlock(m.shell.cmd);
      shellSeal(b, m.shell);
      continue;
    }
    if (m.role === "human") addMsg("user", textOf(m.content), imagesOf(m.content));
    else if (m.role === "ai") {
      if (m.thinking) {
        const b = addBlock("thinking", "◈ THINKING");
        b.querySelector("pre").textContent = m.thinking;
      }
      if (textOf(m.content).trim())
        attachSpeak(addMsg("assistant", textOf(m.content)), textOf(m.content));
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
  // `!cmd` = shell mode (Claude Code style): run on the server, no model call
  if (!images.length && text.startsWith("!")) {
    const cmd = text.slice(1).trim();
    if (!cmd) return;
    $("#input").value = "";
    await runShell(cmd);
    return;
  }
  stopSpeaking(); // a new run interrupts whatever was being read aloud
  if (!threadId) await newThread();
  $("#input").value = "";
  pendingImages = [];
  renderAttachStrip();
  addMsg("user", text, images);
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

function setBusy(b) {
  $("#btn-send").textContent = b ? "■ STOP" : "SEND ▶";
  $("#btn-send").classList.toggle("stop", b);
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
}

// ---------- threads ----------
async function refreshThreads() {
  threads = await api.threads();
  const box = $("#threads");
  box.innerHTML = "";
  for (const t of threads) {
    const d = el("div", "thread" + (t.id === threadId ? " active" : ""));
    d.appendChild(el("span", "ctx", t.context_tokens != null ? fmtTok(t.context_tokens) : ""));
    d.appendChild(el("span", "t-name", t.title));
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
  updateCtxTag();
}

// what the next model call will actually re-prefill for the open thread
function updateCtxTag() {
  const t = threads.find((x) => x.id === threadId);
  $("#ctx-tag").textContent = t ? "CTX ~" + fmtTok(t.context_tokens) : "";
}

async function openThread(t) {
  SFX.play("click");
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
  $("#set-mcp").value = JSON.stringify(s.mcp_servers || {}, null, 2);
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
    compact_enabled: $("#set-compact").checked,
    deep_agent: $("#set-deep_agent").checked,
    skills_enabled: $("#set-skills_enabled").checked,
    compact_trigger_tokens: parseInt($("#set-compact_trigger_tokens").value) || 0,
    compact_keep_messages: parseInt($("#set-compact_keep_messages").value) || 20,
    compact_summary_tokens: parseInt($("#set-compact_summary_tokens").value) || 800,
    local_tools,
    mcp_servers: mcp,
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
$("#tab-chat").onclick = () => { SFX.play("click"); showTab("chat"); };
$("#tab-traj").onclick = () => { SFX.play("click"); showTab("traj"); };
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

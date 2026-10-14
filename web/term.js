// ▮ TERM — browser terminals (xterm.js) on tmux sessions the server keeps
// (server/term.py), plus the fleet host list: probe, SSH tab, run-on-many.
// A tab here is just a tmux client: hiding the panel or closing the browser
// tab leaves the shell running; only ✕ (or `exit`) ends it.
"use strict";

const TERM = {
  tabs: new Map(),   // sid -> {sid, title, host, term, fit, ws, box, tab, dead, retry}
  active: null,      // sid on screen, or "out" for the run-on-many results
  hosts: [],
  probes: {},        // name -> last probe result
  sel: new Set(JSON.parse(localStorage.getItem("lb-tf-sel") || "[]")),
  editing: null,     // host name being edited, "" = new
  poll: null,
  out: null,         // last run-on-many {command, results}
};
const T_THEME = {
  background: "#060a12", foreground: "#cfe6f5", cursor: "#3fd6ff", cursorAccent: "#060a12",
  selectionBackground: "#1d3a5f", black: "#0b1322", brightBlack: "#5f7d99",
  red: "#ff4d5e", brightRed: "#ff7a87", green: "#43e08a", brightGreen: "#7af0b0",
  yellow: "#ff8f2b", brightYellow: "#ffb46b", blue: "#3f8cff", brightBlue: "#7ab0ff",
  magenta: "#c77dff", brightMagenta: "#dcaaff", cyan: "#3fd6ff", brightCyan: "#8fe8ff",
  white: "#cfe6f5", brightWhite: "#ffffff",
};

function tErr(msg) {
  const e = $("#tf-error");
  e.textContent = msg || "";
  e.classList.toggle("hidden", !msg);
}
const tSaveSel = () => { try { localStorage.setItem("lb-tf-sel", JSON.stringify([...TERM.sel])); } catch {} };

async function openTerm() {
  SFX.play("click");
  $("#term-panel").classList.remove("hidden");
  let fleetOff = false;
  try { fleetOff = localStorage.getItem("lb-tf-hide") === "1"; } catch {}
  // phones: the terminal gets the screen unless the list was asked for
  if (matchMedia("(max-width: 760px)").matches && localStorage.getItem("lb-tf-hide") === null) fleetOff = true;
  $("#term-panel").classList.toggle("no-fleet", fleetOff);
  await tRefresh();
  if (!TERM.active && TERM.tabs.size) tShow([...TERM.tabs.keys()].pop());
  else tShow(TERM.active);
  clearInterval(TERM.poll);
  TERM.poll = setInterval(() => { if (!$("#term-panel").classList.contains("hidden")) tRefresh(); }, 8000);
}

function closeTerm() {
  SFX.play("click");
  $("#term-panel").classList.add("hidden");
  clearInterval(TERM.poll);
}

// sessions + hosts from the server; adopts sessions opened elsewhere
// (another device, before a reload) and drops ones that ended
async function tRefresh() {
  let d;
  try { d = await J(await fetch("/api/term")); } catch (e) { tErr(e.message); return; }
  if (!d.available) tErr("tmux is not installed on the server — `sudo dnf install tmux`, then reopen.");
  TERM.hosts = d.hosts || [];
  const live = new Set();
  for (const s of d.sessions) {
    live.add(s.id);
    const t = TERM.tabs.get(s.id);
    if (!t) tAdd(s);
    else if (t.title !== s.title) { t.title = s.title; t.tab.querySelector(".tt-name").textContent = s.title; }
  }
  for (const [sid, t] of TERM.tabs) if (!live.has(sid) && !t.dead) tEnded(t, "ended");
  tHosts();
  tEmpty();
}

function tEmpty() {
  const none = !TERM.tabs.size && TERM.active !== "out";
  $("#term-empty").classList.toggle("hidden", !none);
}

function tAdd(s) {
  const box = el("div", "term-screen hidden");
  $("#term-screens").appendChild(box);
  const tab = el("div", "term-tab");
  tab.setAttribute("role", "tab");
  const dot = el("span", "tt-dot " + (s.host === "local" ? "local" : "ssh"));
  const name = el("span", "tt-name", s.title);
  name.title = (s.host === "local" ? "this box" : "ssh " + s.host) + " — double-click to rename";
  const x = el("button", "tt-x", "✕");
  x.title = "end this terminal (the shell is killed)";
  tab.append(dot, name, x);
  $("#term-tabs").appendChild(tab);
  const term = new Terminal({
    theme: T_THEME, fontFamily: '"JetBrains Mono", ui-monospace, monospace', fontSize: 13,
    cursorBlink: true, scrollback: 5000, allowProposedApi: true, macOptionIsMeta: true,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.loadAddon(new WebLinksAddon.WebLinksAddon());
  term.open(box);
  const t = { sid: s.id, title: s.title, host: s.host, term, fit, box, tab, ws: null, dead: false, retry: 0 };
  TERM.tabs.set(s.id, t);
  term.onData((d) => { if (t.ws && t.ws.readyState === 1) t.ws.send(new TextEncoder().encode(d)); });
  term.onBinary((d) => {
    if (t.ws && t.ws.readyState === 1) t.ws.send(Uint8Array.from(d, (c) => c.charCodeAt(0) & 255));
  });
  term.onResize(({ cols, rows }) => {
    if (t.ws && t.ws.readyState === 1) t.ws.send(JSON.stringify({ resize: [cols, rows] }));
  });
  tab.onclick = (e) => { if (e.target !== x) { SFX.play("click"); tShow(s.id); } };
  name.ondblclick = (e) => { e.stopPropagation(); tRename(t); };
  x.onclick = async (e) => {
    e.stopPropagation();
    if (t.dead) { tDrop(t); return; }
    if (!x.classList.contains("armed")) {
      x.classList.add("armed"); x.textContent = "⚠";
      setTimeout(() => { x.classList.remove("armed"); x.textContent = "✕"; }, 3000);
      return;
    }
    SFX.play("click");
    await fetch("/api/term/" + s.id, { method: "DELETE" }).catch(() => {});
    tDrop(t);
  };
  tConnect(t);
  return t;
}

function tConnect(t) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  try { t.fit.fit(); } catch {}
  const ws = new WebSocket(`${proto}//${location.host}/api/term/${t.sid}/ws?cols=${t.term.cols}&rows=${t.term.rows}`);
  ws.binaryType = "arraybuffer";
  t.ws = ws;
  let ended = false;
  ws.onopen = () => {
    t.retry = 0;
    t.tab.classList.remove("lost");
    // a fit while the socket was still connecting (or the screen hidden) fired
    // onResize into the void — always state the size we actually have
    try { if (!t.box.classList.contains("hidden")) t.fit.fit(); } catch {}
    ws.send(JSON.stringify({ resize: [t.term.cols, t.term.rows] }));
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data !== "string") { t.term.write(new Uint8Array(ev.data)); return; }
    let j = null;
    try { j = JSON.parse(ev.data); } catch {}
    if (j && j.exit) { ended = j.exit !== "detached"; if (ended) tEnded(t, j.exit); }
  };
  ws.onclose = () => {
    if (t.ws !== ws || ended || t.dead) return;  // superseded, or the shell is gone
    // a dropped link (server restart, wifi): tmux kept the shell — re-attach
    t.tab.classList.add("lost");
    const wait = Math.min(15000, 500 * 2 ** t.retry++);
    setTimeout(() => { if (t.ws === ws && !t.dead) tConnect(t); }, wait);
  };
}

function tEnded(t, why) {
  if (t.dead) return;
  t.dead = true;
  t.tab.classList.add("dead");
  t.term.write(`\r\n\x1b[33m[terminal ${why} — ✕ closes the tab]\x1b[0m\r\n`);
  try { t.ws && t.ws.close(); } catch {}
}

function tDrop(t) {
  t.dead = true;
  try { t.ws && t.ws.close(); } catch {}
  t.term.dispose();
  t.box.remove();
  t.tab.remove();
  TERM.tabs.delete(t.sid);
  if (TERM.active === t.sid) tShow([...TERM.tabs.keys()].pop() || null);
  tEmpty();
}

function tShow(sid) {
  TERM.active = sid;
  for (const [k, t] of TERM.tabs) {
    const on = k === sid;
    t.box.classList.toggle("hidden", !on);
    t.tab.classList.toggle("on", on);
  }
  $("#tf-out").classList.toggle("hidden", sid !== "out");
  const ot = $("#term-tabs .term-tab.out");
  if (ot) ot.classList.toggle("on", sid === "out");
  tEmpty();
  const t = TERM.tabs.get(sid);
  if (t) requestAnimationFrame(() => { try { t.fit.fit(); } catch {} t.term.focus(); });
}

function tRename(t) {
  const name = t.tab.querySelector(".tt-name");
  const inp = el("input", "tt-ren");
  inp.value = t.title;
  name.replaceWith(inp);
  inp.focus(); inp.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const v = inp.value.trim();
    inp.replaceWith(name);
    if (save && v && v !== t.title) {
      t.title = v; name.textContent = v;
      await fetch("/api/term/" + t.sid, { method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: v }) }).catch(() => {});
    }
    t.term.focus();
  };
  inp.onkeydown = (e) => {
    e.stopPropagation();
    if (e.key === "Enter") finish(true);
    else if (e.key === "Escape") finish(false);
  };
  inp.onblur = () => finish(true);
}

async function tNew(host) {
  SFX.play("click");
  tErr("");
  // size the new tmux window to the visible area up front (no reflow flash)
  const m = $("#term-main").getBoundingClientRect();
  const cols = Math.max(40, Math.floor((m.width - 16) / 7.8)), rows = Math.max(10, Math.floor((m.height - 16) / 17));
  try {
    const s = await J(await fetch("/api/term", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host: host || "local", cols, rows }) }));
    const t = TERM.tabs.get(s.id) || tAdd(s);
    tShow(t.sid);
  } catch (e) { tErr(e.message); }
}

window.addEventListener("resize", () => {
  const t = TERM.tabs.get(TERM.active);
  if (t && !$("#term-panel").classList.contains("hidden")) try { t.fit.fit(); } catch {}
});
new ResizeObserver(() => {
  const t = TERM.tabs.get(TERM.active);
  if (t && !t.box.classList.contains("hidden")) try { t.fit.fit(); } catch {}
}).observe($("#term-screens"));

// ---------- fleet hosts ----------

function tAgo(s) {
  if (s == null) return "";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  return d ? `${d}d ${h}h` : `${h}h ${Math.floor((s % 3600) / 60)}m`;
}

function tHostLine(p) {
  if (!p) return ["", "not probed yet"];
  if (!p.ok) return ["down", p.error || "unreachable"];
  const bits = [];
  if (p.load) {
    const l1 = parseFloat(p.load);
    bits.push(`load ${p.load.split(" ")[0]}${p.cpus ? "/" + p.cpus : ""}`);
    if (p.cpus && l1 > p.cpus) bits[0] = "⚠ " + bits[0];
  }
  if (p.mem_mb) bits.push(`mem ${Math.round((100 * p.mem_mb[0]) / p.mem_mb[1])}%`);
  if (p.disk) bits.push(`disk ${p.disk}`);
  if (p.gpu && p.gpu.util !== "") bits.push(`gpu ${p.gpu.util}%`);
  bits.push(`up ${tAgo(p.uptime_s)}`, `${p.ms} ms`);
  const hot = (p.disk && parseInt(p.disk) >= 90) || (p.mem_mb && p.mem_mb[0] / p.mem_mb[1] > 0.92);
  return [hot ? "warn" : "up", bits.join(" · ")];
}

function tHosts() {
  const list = $("#tf-list");
  list.replaceChildren();
  if (!TERM.hosts.length) {
    list.appendChild(el("div", "tf-none", "No hosts yet — IMPORT picks them from ~/.ssh/config, or + ADD one."));
  }
  for (const h of TERM.hosts) {
    const p = TERM.probes[h.name];
    const [state, line] = tHostLine(p);
    const row = el("div", "tf-host " + state);
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = TERM.sel.has(h.name);
    cb.title = "include in RUN ▶";
    cb.onchange = () => { cb.checked ? TERM.sel.add(h.name) : TERM.sel.delete(h.name); tSaveSel(); };
    const dot = el("span", "tf-dot");
    const main = el("div", "tf-main");
    const nm = el("div", "tf-name", h.name);
    nm.title = `ssh ${h.ssh}${h.note ? " — " + h.note : ""}`;
    const ln = el("div", "tf-line", line);
    ln.title = line;
    main.append(nm, ln);
    const go = el("button", "btn ghost sm", "▶");
    go.title = "open an SSH terminal on " + h.name;
    go.onclick = () => tNew(h.name);
    const ed = el("button", "btn ghost sm", "✎");
    ed.title = "edit";
    ed.onclick = () => tEdit(h.name);
    row.append(cb, dot, main, go, ed);
    list.appendChild(row);
  }
  $("#tf-all").checked = TERM.hosts.length > 0 && TERM.hosts.every((h) => TERM.sel.has(h.name));
}

async function tProbe() {
  SFX.play("click");
  const b = $("#btn-tf-probe");
  b.disabled = true; b.textContent = "⟳ …";
  try {
    const d = await J(await fetch("/api/fleet/probe", { method: "POST" }));
    for (const r of d.results) TERM.probes[r.name] = r;
    tHosts();
  } catch (e) { tErr(e.message); }
  b.disabled = false; b.textContent = "⟳ PROBE";
}

async function tSaveHosts(hosts) {
  const d = await J(await fetch("/api/fleet/hosts", { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hosts }) }));
  TERM.hosts = d.hosts;
  tHosts();
}

function tEdit(name) {
  TERM.editing = name;
  const h = TERM.hosts.find((x) => x.name === name) || { name: "", ssh: "", note: "" };
  $("#tf-name").value = h.name;
  $("#tf-ssh").value = h.ssh;
  $("#tf-note").value = h.note || "";
  $("#btn-tf-del").classList.toggle("hidden", !name);
  $("#tf-import").classList.add("hidden");
  $("#tf-edit").classList.remove("hidden");
  $("#tf-name").focus();
}

async function tImport() {
  SFX.play("click");
  const box = $("#tf-import");
  if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
  $("#tf-edit").classList.add("hidden");
  let d;
  try { d = await J(await fetch("/api/fleet/hosts")); } catch (e) { tErr(e.message); return; }
  const have = new Set(TERM.hosts.map((h) => h.ssh));
  const fresh = d.ssh_config.filter((h) => !have.has(h.ssh));
  box.replaceChildren(el("div", "tf-sub", fresh.length ? "FROM ~/.ssh/config" : "Every ~/.ssh/config host is already listed."));
  const picks = [];
  for (const h of fresh) {
    const lab = el("label", "chk");
    const cb = el("input");
    cb.type = "checkbox"; cb.checked = true;
    picks.push([cb, h]);
    lab.append(cb, document.createTextNode(" " + h.name + (h.note ? "  — " + h.note : "")));
    box.appendChild(lab);
  }
  const row = el("div", "row end");
  const cancel = el("button", "btn ghost sm", "CANCEL");
  cancel.onclick = () => box.classList.add("hidden");
  row.appendChild(cancel);
  if (fresh.length) {
    const add = el("button", "btn accent sm", "ADD SELECTED");
    add.onclick = async () => {
      try {
        await tSaveHosts([...TERM.hosts, ...picks.filter(([cb]) => cb.checked).map(([, h]) => h)]);
        box.classList.add("hidden");
        tProbe();
      } catch (e) { tErr(e.message); }
    };
    row.appendChild(add);
  }
  box.appendChild(row);
  box.classList.remove("hidden");
}

async function tRun() {
  const cmd = $("#tf-cmd").value.trim();
  const names = TERM.hosts.map((h) => h.name).filter((n) => TERM.sel.has(n));
  if (!cmd) { $("#tf-cmd").focus(); return; }
  if (!names.length) { tErr("tick at least one host (or “all”)"); return; }
  tErr("");
  SFX.play("click");
  const b = $("#btn-tf-run");
  b.disabled = true; b.textContent = "RUNNING…";
  tOut({ command: cmd, hosts: names, results: null });
  try {
    const d = await J(await fetch("/api/fleet/exec", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hosts: names, command: cmd, timeout: Number($("#tf-timeout").value) || 60 }) }));
    tOut({ command: cmd, hosts: names, results: d.results });
  } catch (e) { tErr(e.message); tOut(null); }
  b.disabled = false; b.textContent = "RUN ▶";
}

// run-on-many results: its own pseudo-tab next to the terminals
function tOut(o) {
  TERM.out = o;
  let tab = $("#term-tabs .term-tab.out");
  if (!o) { if (tab) tab.remove(); if (TERM.active === "out") tShow([...TERM.tabs.keys()].pop() || null); return; }
  if (!tab) {
    tab = el("div", "term-tab out");
    tab.append(el("span", "tt-name", "⇶ RUN"));
    const x = el("button", "tt-x", "✕");
    x.onclick = (e) => { e.stopPropagation(); tOut(null); };
    tab.appendChild(x);
    tab.onclick = () => tShow("out");
    $("#term-tabs").prepend(tab);
  }
  const box = $("#tf-out");
  box.replaceChildren();
  const head = el("div", "tfo-head");
  head.append(el("span", "tfo-cmd", "$ " + o.command));
  box.appendChild(head);
  if (!o.results) {
    box.appendChild(el("div", "tf-none", `running on ${o.hosts.join(", ")}…`));
  } else {
    const ok = o.results.filter((r) => r.code === 0).length;
    head.append(el("span", "tfo-sum", `${ok}/${o.results.length} ok`));
    for (const r of o.results) {
      const card = el("details", "tfo-card " + (r.code === 0 ? "ok" : "bad"));
      card.open = true;
      const sm = el("summary");
      sm.append(el("span", "tfo-host", r.name),
        el("span", "tfo-code", r.code === 0 ? "✓ exit 0" : r.code === 124 ? "⏱ timeout" : "✕ exit " + r.code),
        el("span", "tfo-ms", (r.ms / 1000).toFixed(1) + " s"));
      card.appendChild(sm);
      const pre = el("pre", "tfo-pre");
      pre.textContent = (r.out || "") + (r.err ? (r.out ? "\n" : "") + r.err : "") || "(no output)";
      if (r.err && !r.out) pre.classList.add("err");
      card.appendChild(pre);
      box.appendChild(card);
    }
  }
  tShow("out");
}

$("#btn-term").onclick = () => openTerm();
$("#btn-term-close").onclick = () => closeTerm();
$("#btn-term-new").onclick = () => tNew("local");
$("#btn-term-fleet").onclick = () => {
  SFX.play("click");
  const off = !$("#term-panel").classList.toggle("no-fleet") ? "0" : "1";
  try { localStorage.setItem("lb-tf-hide", off); } catch {}
  const t = TERM.tabs.get(TERM.active);
  if (t) requestAnimationFrame(() => { try { t.fit.fit(); } catch {} });
};
$("#btn-tf-probe").onclick = () => tProbe();
$("#btn-tf-add").onclick = () => { SFX.play("click"); tEdit(""); };
$("#btn-tf-import").onclick = () => tImport();
$("#btn-tf-cancel").onclick = () => { SFX.play("click"); $("#tf-edit").classList.add("hidden"); };
$("#btn-tf-save").onclick = async () => {
  const h = { name: $("#tf-name").value.trim(), ssh: $("#tf-ssh").value.trim(), note: $("#tf-note").value.trim() };
  if (!h.name || !h.ssh) { tErr("name and SSH target are both needed"); return; }
  const rest = TERM.hosts.filter((x) => x.name !== TERM.editing && x.name !== h.name);
  const at = TERM.hosts.findIndex((x) => x.name === TERM.editing);
  if (at >= 0) rest.splice(Math.min(at, rest.length), 0, h); else rest.push(h);
  try {
    await tSaveHosts(rest);
    if (TERM.editing && TERM.editing !== h.name && TERM.sel.delete(TERM.editing)) { TERM.sel.add(h.name); tSaveSel(); }
    $("#tf-edit").classList.add("hidden");
    tErr("");
  } catch (e) { tErr(e.message); }
};
$("#btn-tf-del").onclick = async () => {
  const b = $("#btn-tf-del");
  if (!b.classList.contains("armed")) {
    b.classList.add("armed"); b.textContent = "⚠ SURE?";
    setTimeout(() => { b.classList.remove("armed"); b.textContent = "DELETE"; }, 3000);
    return;
  }
  try {
    await tSaveHosts(TERM.hosts.filter((x) => x.name !== TERM.editing));
    $("#tf-edit").classList.add("hidden");
  } catch (e) { tErr(e.message); }
};
$("#tf-all").onchange = () => {
  for (const h of TERM.hosts) $("#tf-all").checked ? TERM.sel.add(h.name) : TERM.sel.delete(h.name);
  tSaveSel();
  tHosts();
};
$("#btn-tf-run").onclick = () => tRun();
$("#tf-cmd").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); tRun(); }
});
for (const b of document.querySelectorAll("#term-keys button")) {
  // keep focus (and the phone keyboard) on the terminal
  b.addEventListener("pointerdown", (e) => e.preventDefault());
  b.onclick = () => {
    const t = TERM.tabs.get(TERM.active);
    if (!t || t.dead || !t.ws || t.ws.readyState !== 1) return;
    t.ws.send(new TextEncoder().encode(JSON.parse('"' + b.dataset.k + '"')));
    t.term.focus();
  };
}

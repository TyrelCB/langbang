// WATCH — in-page live stream player (HLS via hls.js, plus direct video URLs).
// Self-contained: no app.js helpers, loads BEFORE app.js and exposes
// window.langbangWatch = { open, play, stop } for stream chips in answers.
(() => {
  const $ = (s) => document.querySelector(s);
  const panel = $("#watch-panel"), video = $("#watch-video"), urlBox = $("#watch-url"),
        status = $("#watch-status");
  let hls = null, retryTimer = null;

  const isHls = (u) => /\.m3u8(\?|$)/i.test(u) || /\/manifest(\?|$)/i.test(u);

  function say(t, bad) {
    status.textContent = t || "";
    status.style.color = bad ? "var(--red, #f66)" : "";
  }

  function stop() {
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
    if (hls) { hls.destroy(); hls = null; }
    video.pause(); video.removeAttribute("src"); video.load();
    panel.classList.remove("playing");
  }

  function attachHlsLive(url) {
    // live edge: hls.js starts live playlists at the live edge by default —
    // no manual startLoad here (it aborts the in-flight first frag load).
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      say("loaded — " + (hls.levels.length || 1) + " level(s), starting");
      video.play().catch(() => say("press ▶ to start (autoplay blocked)"));
    });
    hls.on(Hls.Events.ERROR, (e, data) => {
      if (!data.fatal) return;
      say("error: " + data.details + " — retrying in 3s", true);
      // network errors are usually the upstream token rolling over or the
      // proxy restarting; a fresh startLoad() recovers without a reload.
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
      else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
      else {
        stop();
        retryTimer = setTimeout(() => play(url), 3000);
      }
    });
    // stall watchdog: if playback freezes for >8s on a live stream, jump to live edge
    let last = 0;
    setInterval(() => {
      if (!hls || video.paused || !isLive()) return;
      if (video.currentTime === last && video.readyState < 4) {
        say("stalled — resyncing to live edge", true);
        hls.startLoad();
        const end = video.seekable.length ? video.seekable.end(video.seekable.length - 1) : 0;
        if (end > 0) video.currentTime = end - 2;
      }
      last = video.currentTime;
    }, 8000);
  }

  const isLive = () => hls?.liveSession?.latency !== undefined ||
    (hls?.levels?.[0]?.details?.live ?? false);

  function play(url) {
    stop();
    url = url.trim();
    if (!url) return;
    if (!/^(https?|blob):/i.test(url)) { say("need an http(s) URL", true); return; }
    urlBox.value = url;
    panel.classList.add("playing");
    say("connecting…");
    if (isHls(url) && window.Hls && Hls.isSupported()) {
      hls = new Hls({ lowLatencyMode: true, enableWorker: true,
        manifestLoadingTimeOut: 10000, manifestLoadingMaxRetry: 2 });
      hls.loadSource(url);
      hls.attachMedia(video);
      attachHlsLive(url);
    } else {
      // native HLS (Safari) or a direct progressive file
      video.src = url;
      video.play().then(() => say("playing")).catch(() => say("press ▶ (autoplay blocked)"));
      video.onerror = () => say("failed to load — is the stream up? CORS?", true);
    }
  }

  function open(url) {
    panel.classList.remove("hidden");
    if (url) play(url);
    else urlBox.focus();
  }
  function close() { stop(); panel.classList.add("hidden"); say(""); }

  $("#btn-watch").onclick = () => open();
  $("#btn-watch-close").onclick = close;
  $("#btn-watch-play").onclick = () => play(urlBox.value);
  urlBox.onkeydown = (e) => { if (e.key === "Enter") play(urlBox.value); };
  panel.onclick = (e) => { if (e.target === panel) close(); };
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !panel.classList.contains("hidden")) close();
  });

  // ---- CAST: enumerate LAN receivers, POST /api/stream/cast ----
  const castSel = $("#watch-cast");
  let discovering = false;
  async function fillDevices() {
    if (discovering) return;
    discovering = true;
    castSel.options[0].textContent = "⤴ SCANNING…";
    try {
      const r = await fetch("/api/stream/devices");
      const { devices } = await r.json();
      if (!r.ok) throw new Error(devices?.detail || r.status);
      castSel.replaceChildren(new Option("⤴ CAST TO…", ""));
      for (const d of devices)
        castSel.append(new Option(`${d.name}${d.model ? " · " + d.model : ""}`, d.name));
      if (!devices.length) castSel.replaceChildren(new Option("⤴ NO DEVICES", ""));
    } catch (e) {
      castSel.options[0].textContent = "⤴ CAST FAILED";
    } finally { discovering = false; }
  }
  castSel.onfocus = fillDevices;
  castSel.onchange = async () => {
    const name = castSel.value;
    if (!name) return;
    castSel.disabled = true;
    say("casting to " + name + "…");
    try {
      const r = await fetch("/api/stream/cast", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device: name, url: urlBox.value, title: "LangBang WATCH" }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.status);
      say("cast to " + name + ": " + j.state);
    } catch (e) { say("cast failed: " + e.message, true); }
    castSel.disabled = false;
  };

  // ---- SHARE: temp public URL under *.ai.tyrel.cloud ----
  const shared = $("#watch-shared"), shareBtn = $("#btn-watch-share");
  shareBtn.onclick = async () => {
    shareBtn.disabled = true; shareBtn.textContent = "…MINTING";
    try {
      const r = await fetch("/api/stream/share", { method: "POST" });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.status);
      shared.classList.remove("hidden");
      shared.replaceChildren();
      const a = document.createElement("a");
      a.href = j.url; a.target = "_blank"; a.textContent = j.url;
      a.style.color = "var(--cyan)";
      const cp = el("button", "btn ghost sm", "COPY");
      cp.style.marginLeft = "8px";
      cp.onclick = () => { navigator.clipboard.writeText(j.url); cp.textContent = "COPIED"; };
      shared.append("🔗 ", a, cp, el("span", "", "  (6h — plays the same cached feed anywhere)"));
    } catch (e) { say("share failed: " + e.message, true); }
    shareBtn.disabled = false; shareBtn.textContent = "🔗 SHARE";
  };


  window.langbangWatch = { open, play, stop, close };
})();

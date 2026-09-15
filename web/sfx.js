// LangBang SFX manager. Audio assets are NOT generated yet (Spark is VRAM-tight
// on 2026-09-14 — sound generation is deferred; see SOUND_DESIGN.md for the
// Mega Man X style brief). This module plays files from web/sounds/ when they
// exist and stays silent otherwise. Wire-up is done: drop the files in and flip
// the manifest entries from TODO to filenames.
const SFX = (() => {
  const enabled = () => localStorage.getItem("lb-sound") === "on";
  const cache = new Map();
  let manifest = null;

  async function loadManifest() {
    try {
      const r = await fetch("/static/sounds/manifest.json");
      manifest = await r.json();
    } catch { manifest = {}; }
  }

  async function play(name) {
    if (!enabled()) return;
    const file = manifest && manifest[name] && manifest[name].file;
    if (!file || String(file).startsWith("TODO")) return;
    try {
      if (!cache.has(name)) {
        const r = await fetch("/static/sounds/" + file);
        if (!r.ok) return;
        cache.set(name, await r.arrayBuffer());
      }
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const buf = await ctx.decodeAudioData(cache.get(name).slice(0));
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(ctx.destination);
      src.start();
    } catch { /* stay silent, never break chat */ }
  }

  loadManifest();
  return { play };
})();

// LangBang SFX manager: plays web/sounds/ cues from manifest.json, gated by
// the ♪ SOUND toggle (localStorage lb-sound). Assets were generated from the
// Mega Man X style brief in SOUND_DESIGN.md; regenerate one cue from CONFIG →
// SOUNDBOARD (or `python -m server.sfxgen <slot>`). Unknown/missing slots
// stay silent — never break chat.
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

  // play("click") obeys the ♪ SOUND toggle; play("click", true) always sounds
  // (the CONFIG soundboard auditions cues with the toggle off)
  async function play(name, force) {
    if (!enabled() && !force) return;
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
      // optional per-slot gain (manifest "gain") keeps the mix from the design
      // doc even though every render was level-normalized at generation time
      const gain = ctx.createGain();
      gain.gain.value = Number(manifest[name].gain) || 1;
      src.connect(gain).connect(ctx.destination);
      src.start();
    } catch { /* stay silent, never break chat */ }
  }

  // drop the in-memory buffer so the next play() refetches — the soundboard
  // calls this after regenerating a cue, making the new take live without reload
  function reload(name) { cache.delete(name); }

  loadManifest();
  return { play, reload };
})();

// LangBang SFX manager: plays web/sounds/ cues from manifest.json, gated by
// the ♪ SOUND toggle (localStorage lb-sound). Assets were generated from the
// Mega Man X style brief in SOUND_DESIGN.md; regenerate one cue from CONFIG →
// SOUNDBOARD (or `python -m server.sfxgen <slot>`). Unknown/missing slots
// stay silent — never break chat.
const SFX = (() => {
  const enabled = () => localStorage.getItem("lb-sound") === "on";
  const cache = new Map();
  let manifest = null;

  // ONE AudioContext for the page's whole life. Chrome caps contexts per
  // page, and — the real killer — a context constructed OUTSIDE a user
  // gesture (game.js rAF loop: jump/kill/hurt/death/clear) starts
  // "suspended" and src.start() on it is silent. Chat cues used to dodge
  // this only by accident (they fired inside click handlers). resume() plus
  // the gesture kick below keeps the single context RUNNING for everyone.
  let ctx = null;
  const audioCtx = () => {
    if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    return ctx;
  };
  for (const ev of ["pointerdown", "keydown"])
    window.addEventListener(ev, () => audioCtx(), { once: true, capture: true });

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
      const ac = audioCtx();
      const buf = await ac.decodeAudioData(cache.get(name).slice(0));
      const src = ac.createBufferSource();
      src.buffer = buf;
      // optional per-slot gain (manifest "gain") keeps the mix from the design
      // doc even though every render was level-normalized at generation time
      const gain = ac.createGain();
      gain.gain.value = Number(manifest[name].gain) || 1;
      src.connect(gain).connect(ac.destination);
      src.start();
    } catch { /* stay silent, never break chat */ }
  }

  // drop the in-memory buffer so the next play() refetches — the soundboard
  // calls this after regenerating a cue, making the new take live without reload
  function reload(name) { cache.delete(name); }

  loadManifest();
  return { play, reload, get _ctx() { return ctx; } }; // _ctx for CDP assertions
})();

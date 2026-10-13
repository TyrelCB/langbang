// Audio visualizers for the ✎ FILES audio viewer: a vanilla port of
// music-sync's six (~/projects/music_app/web/src/visualizers): orb, plasma
// and kaleidoscope are WebGL2 fragment shaders; bars, radial and particles
// are 2D canvas. All read one AnalyserNode split into bass/mid/treble.
//
// AudioViz.attach(mediaEl, stageEl, {seed}) -> {setMode, setBackdrop, destroy}
// - The analyser taps the element through ONE shared AudioContext (browsers
//   cap the count). It is created on the first `play`, which is always a user
//   gesture here (the native controls), so it never starts suspended. Once an
//   element is routed through the context, its sound plays only while the
//   context runs, so every `play` resumes it.
// - setBackdrop([{url, start, end}]): generated art for the track. The image
//   for the current position cross-fades in behind the visualizer, which
//   switches to screen blending so its dark parts show the picture.
(function () {
  "use strict";
  let CTX = null;
  const ctx = () => (CTX = CTX || new (window.AudioContext || window.webkitAudioContext)());

  function readBands(an, buf) {
    if (!an) return { bass: 0.15, mid: 0.15, treble: 0.15 };
    an.getByteFrequencyData(buf);
    const n = buf.length, bEnd = Math.floor(n * 0.08), mEnd = Math.floor(n * 0.35);
    let s = 0;
    for (let i = 0; i < bEnd; i++) s += buf[i];
    const bass = bEnd ? s / bEnd / 255 : 0;
    s = 0;
    for (let i = bEnd; i < mEnd; i++) s += buf[i];
    const mid = mEnd > bEnd ? s / (mEnd - bEnd) / 255 : 0;
    s = 0;
    for (let i = mEnd; i < n; i++) s += buf[i];
    return { bass, mid, treble: n > mEnd ? s / (n - mEnd) / 255 : 0 };
  }

  function hashHue(str) {
    let h = 0;
    for (let i = 0; i < str.length; i++) { h = (h << 5) - h + str.charCodeAt(i); h |= 0; }
    return Math.abs(h) % 360;
  }
  function hslToRgb(h, s, l) {
    s /= 100; l /= 100;
    const k = (n) => (n + h / 30) % 12, a = s * Math.min(l, 1 - l);
    const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
    return [f(0), f(8), f(4)];
  }

  // ---- WebGL2 shader modes ------------------------------------------------
  const VERT = `#version 300 es
in vec2 position;
void main() { gl_Position = vec4(position, 0.0, 1.0); }`;
  const HEAD = `#version 300 es
precision highp float;
out vec4 fragColor;
uniform vec2 uResolution;
uniform float uTime;
uniform float uBass;
uniform float uMid;
uniform float uTreble;
uniform vec3 uColorA;
uniform vec3 uColorB;
#define PI 3.14159265359
#define TAU 6.28318530718
mat2 rot(float a) { float s = sin(a), c = cos(a); return mat2(c, -s, s, c); }
`;
  const SHADERS = {
    // raymarched noise-deformed sphere: bass = pulse, mid = bumps, treble = spin
    orb: HEAD + `
float hash(vec3 p) { p = fract(p * 0.3183099 + 0.1); p *= 17.0; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }
float noise(vec3 p) {
  vec3 i = floor(p); vec3 f = fract(p); f = f * f * (3.0 - 2.0 * f);
  return mix(mix(mix(hash(i), hash(i + vec3(1,0,0)), f.x), mix(hash(i + vec3(0,1,0)), hash(i + vec3(1,1,0)), f.x), f.y),
             mix(mix(hash(i + vec3(0,0,1)), hash(i + vec3(1,0,1)), f.x), mix(hash(i + vec3(0,1,1)), hash(i + vec3(1,1,1)), f.x), f.y), f.z);
}
float map(vec3 p) {
  p.xy *= rot(uTime * (0.25 + uTreble * 0.5));
  p.xz *= rot(uTime * 0.18);
  return length(p) - 1.0 - noise(p * 2.2 + uTime * 0.15) * (0.08 + uMid * 0.12);
}
vec3 calcNormal(vec3 p) {
  vec2 e = vec2(1.0, -1.0) * 0.001;
  return normalize(e.xyy * map(p + e.xyy) + e.yyx * map(p + e.yyx) + e.yxy * map(p + e.yxy) + e.xxx * map(p + e.xxx));
}
void main() {
  vec2 uv = (gl_FragCoord.xy - 0.5 * uResolution) / min(uResolution.x, uResolution.y);
  vec3 ro = vec3(0.0, 0.0, 4.2), rd = normalize(vec3(uv, -2.4));
  float scale = 0.62 + uBass * 0.12, t = 0.0; bool hit = false; vec3 pos = ro;
  for (int i = 0; i < 80; i++) {
    pos = ro + rd * t; float d = map(pos / scale) * scale;
    if (d < 0.0015) { hit = true; break; }
    t += d; if (t > 8.0) break;
  }
  vec3 bg = mix(vec3(0.02, 0.02, 0.03), vec3(0.05, 0.04, 0.07), uv.y * 0.5 + 0.5);
  if (!hit) { fragColor = vec4(bg, 1.0); return; }
  vec3 n = calcNormal(pos / scale), l = normalize(vec3(0.6, 0.7, 0.8));
  float diff = max(dot(n, l), 0.0), rim = pow(1.0 - max(dot(n, -rd), 0.0), 2.5);
  float spec = pow(max(dot(reflect(-l, n), -rd), 0.0), 24.0);
  vec3 base = mix(uColorA, uColorB, clamp(n.y * 0.5 + 0.5 + noise(pos * 1.5 + uTime * 0.1) * 0.2, 0.0, 1.0));
  vec3 color = base * (0.35 + diff * 0.85) + rim * uColorB * 0.9 + spec * 0.6;
  fragColor = vec4(mix(color, bg, clamp(t / 8.0, 0.0, 1.0) * 0.15), 1.0);
}`,
    // domain-warped flow field: bass = warp, mid = colour ramp, treble = flow speed
    plasma: HEAD + `
vec2 hash2(vec2 p) { p = vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3))); return -1.0 + 2.0 * fract(sin(p) * 43758.5453123); }
float noise(vec2 p) {
  vec2 i = floor(p), f = fract(p), u = f * f * (3.0 - 2.0 * f);
  return mix(mix(dot(hash2(i), f), dot(hash2(i + vec2(1,0)), f - vec2(1,0)), u.x),
             mix(dot(hash2(i + vec2(0,1)), f - vec2(0,1)), dot(hash2(i + vec2(1,1)), f - vec2(1,1)), u.x), u.y);
}
float fbm(vec2 p) { float v = 0.0, a = 0.5; for (int i = 0; i < 5; i++) { v += noise(p) * a; p *= 2.02; a *= 0.5; } return v; }
vec3 palette(float t) { return vec3(0.50,0.42,0.55) + vec3(0.50,0.45,0.50) * cos(TAU * (vec3(1.00,1.05,0.95) * t + vec3(0.10,0.42,0.72))); }
void main() {
  vec2 uv = (gl_FragCoord.xy - 0.5 * uResolution) / min(uResolution.x, uResolution.y);
  vec2 p = uv * 2.2;
  float t = uTime * (0.16 + uTreble * 0.28), warp = 0.9 + uBass * 2.2;
  vec2 q = vec2(fbm(p + vec2(0.0, t)), fbm(p + vec2(3.7, -t * 0.8)));
  vec2 r = vec2(fbm(p + warp * q + vec2(1.7, 9.2) + t * 0.35), fbm(p + warp * q + vec2(8.3, 2.8) - t * 0.28));
  float f = fbm(p + warp * r);
  float fil = pow(abs(sin(f * 8.0 + t * 1.5)), 6.0);
  float ramp = f * 1.4 + length(r) * 0.4 + uMid * 0.5 + t * 0.05;
  vec3 color = palette(ramp) + palette(ramp + 0.35) * fil * (0.5 + uBass * 1.4);
  color *= 0.55 + f * 0.9 + uBass * 0.5;
  color *= 1.0 - dot(uv, uv) * 0.45;
  color = color / (1.0 + color);
  fragColor = vec4(pow(color, vec3(0.75)), 1.0);
}`,
    // mirrored wedges of rings + spokes: bass = zoom/ring density, mid = hue, treble = spin
    kaleidoscope: HEAD + `
vec3 palette(float t) { return vec3(0.55,0.40,0.65) + vec3(0.45,0.35,0.45) * cos(TAU * (t + vec3(0.00,0.20,0.55))); }
void main() {
  vec2 uv = (gl_FragCoord.xy - 0.5 * uResolution) / min(uResolution.x, uResolution.y);
  float wedge = TAU / 8.0, a = mod(atan(uv.y, uv.x), wedge), r = length(uv);
  a = abs(a - wedge * 0.5);
  vec2 p = vec2(cos(a), sin(a)) * r;
  p *= rot(uTime * (0.08 + uTreble * 0.35));
  p *= 2.6 + sin(uTime * 0.3) * 0.4 - uBass * 1.1;
  vec3 color = vec3(0.0);
  for (int i = 0; i < 3; i++) {
    float fi = float(i);
    vec2 q = p * (1.0 + fi * 0.6); q *= rot(uTime * 0.12 * (fi + 1.0));
    float rr = length(q), aa = atan(q.y, q.x);
    float rings = fract(rr * (1.8 + uBass * 2.5) - uTime * 0.35);
    rings = pow(1.0 - abs(rings * 2.0 - 1.0), 14.0);
    float glow = (rings + pow(abs(cos(aa * 6.0 + uTime * 0.4)), 12.0) * 0.7) * exp(-rr * 0.55);
    color += palette(fi * 0.22 + rr * 0.15 + uMid * 0.5 + uTime * 0.05) * glow * (1.0 + uBass * 1.2);
  }
  color += palette(uMid * 0.6 + 0.1) * exp(-r * 6.0) * (0.6 + uBass * 2.2);
  color *= 1.0 - r * 0.35;
  color = color / (1.0 + color);
  fragColor = vec4(pow(color, vec3(0.72)), 1.0);
}`,
  };

  function shaderMode(canvas, src, seed) {
    const gl = canvas.getContext("webgl2", { premultipliedAlpha: false });
    if (!gl) return null;
    const sh = (type, s) => {
      const o = gl.createShader(type);
      gl.shaderSource(o, s);
      gl.compileShader(o);
      if (!gl.getShaderParameter(o, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(o));
      return o;
    };
    const prog = gl.createProgram();
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, src));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    gl.useProgram(prog);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(prog, "position");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    const U = {};
    for (const n of ["uResolution", "uTime", "uBass", "uMid", "uTreble", "uColorA", "uColorB"]) U[n] = gl.getUniformLocation(prog, n);
    const hue = hashHue(seed || "");
    const ca = hslToRgb(hue, 70, 55), cb = hslToRgb((hue + 60) % 360, 70, 35);
    return {
      draw(b, t) {
        gl.viewport(0, 0, canvas.width, canvas.height);
        gl.uniform2f(U.uResolution, canvas.width, canvas.height);
        gl.uniform1f(U.uTime, t);
        gl.uniform1f(U.uBass, b.bass);
        gl.uniform1f(U.uMid, b.mid);
        gl.uniform1f(U.uTreble, b.treble);
        gl.uniform3f(U.uColorA, ca[0], ca[1], ca[2]);
        gl.uniform3f(U.uColorB, cb[0], cb[1], cb[2]);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
      },
      destroy() { gl.deleteProgram(prog); gl.deleteBuffer(buf); },
    };
  }

  // ---- 2D canvas modes ------------------------------------------------------
  function barsMode(canvas) {
    const c = canvas.getContext("2d"), levels = new Float32Array(72);
    return {
      draw(b, t, data) {
        const { width: w, height: h } = canvas;
        c.clearRect(0, 0, w, h);
        const half = levels.length, n = data ? data.length : 0;
        for (let i = 0; i < half; i++) {
          const idx = Math.floor(Math.pow(i / half, 1.7) * n * 0.7);
          levels[i] = Math.max(data ? data[idx] / 255 : 0, levels[i] * 0.88);
        }
        const base = h * 0.62, maxBar = h * 0.5, slot = w / (half * 2), bw = slot * 0.62;
        for (let i = 0; i < half; i++) {
          const v = levels[i], bh = Math.max(2, v * maxBar);
          const g = c.createLinearGradient(0, base - bh, 0, base);
          g.addColorStop(0, `hsl(${280 - v * 60}, 95%, ${62 + v * 18}%)`);
          g.addColorStop(1, "hsl(255, 80%, 42%)");
          const xR = w / 2 + i * slot + (slot - bw) / 2, xL = w / 2 - (i + 1) * slot + (slot - bw) / 2;
          c.shadowBlur = 18 * v;
          c.shadowColor = `hsla(${285 - v * 55}, 100%, 65%, ${0.5 * v})`;
          c.fillStyle = g;
          c.fillRect(xR, base - bh, bw, bh);
          c.fillRect(xL, base - bh, bw, bh);
          c.shadowBlur = 0;
          c.globalAlpha = 0.22;
          const rh = bh * 0.45, rg = c.createLinearGradient(0, base, 0, base + rh);
          rg.addColorStop(0, `hsla(${280 - v * 60}, 90%, 60%, 0.8)`);
          rg.addColorStop(1, "hsla(255, 80%, 40%, 0)");
          c.fillStyle = rg;
          c.fillRect(xR, base, bw, rh);
          c.fillRect(xL, base, bw, rh);
          c.globalAlpha = 1;
        }
      },
    };
  }

  function radialMode(canvas) {
    const c = canvas.getContext("2d"), BARS = 128, levels = new Float32Array(BARS);
    let bs = 0;
    return {
      draw(b, t, data) {
        const { width: w, height: h } = canvas, n = data ? data.length : 0;
        c.clearRect(0, 0, w, h);
        const cx = w / 2, cy = h / 2, unit = Math.min(w, h);
        bs = bs * 0.82 + b.bass * 0.18;
        for (let i = 0; i < BARS; i++) {
          const idx = Math.floor(Math.pow(i / BARS, 1.6) * n * 0.7);
          levels[i] = Math.max(data ? data[idx] / 255 : 0, levels[i] * 0.86);
        }
        const inner = unit * (0.12 + bs * 0.035), maxS = unit * 0.3;
        c.lineCap = "round";
        for (let ring = 0; ring < 2; ring++) {
          const rotA = t * 0.12 * (ring ? -1 : 1), sc = ring ? 0.62 : 1, al = ring ? 0.4 : 1;
          for (let i = 0; i < BARS; i++) {
            const v = levels[i], ang = (i / BARS) * Math.PI * 2 + rotA, r0 = inner * sc, r1 = r0 + v * maxS * sc;
            const hue = 268 - v * 70 + ring * 25;
            c.strokeStyle = `hsla(${hue}, 95%, ${58 + v * 22}%, ${al * (0.35 + v * 0.65)})`;
            c.lineWidth = Math.max(1.5, unit * 0.005 * sc);
            c.shadowBlur = 16 * v * al;
            c.shadowColor = `hsla(${hue}, 100%, 65%, ${0.6 * v})`;
            c.beginPath();
            c.moveTo(cx + Math.cos(ang) * r0, cy + Math.sin(ang) * r0);
            c.lineTo(cx + Math.cos(ang) * r1, cy + Math.sin(ang) * r1);
            c.stroke();
          }
        }
        c.shadowBlur = 0;
        const cr = inner * 0.82, g = c.createRadialGradient(cx, cy, 0, cx, cy, cr);
        g.addColorStop(0, `hsla(270, 100%, ${72 + bs * 20}%, ${0.55 + bs * 0.4})`);
        g.addColorStop(1, "hsla(265, 90%, 50%, 0)");
        c.fillStyle = g;
        c.beginPath();
        c.arc(cx, cy, cr, 0, Math.PI * 2);
        c.fill();
      },
    };
  }

  function particlesMode(canvas) {
    const c = canvas.getContext("2d");
    const P = Array.from({ length: 110 }, () => ({ angle: Math.random() * Math.PI * 2, radius: Math.random(),
      speed: 0.4 + Math.random() * 0.9, size: 1.2 + Math.random() * 2.6, hue: 250 + Math.random() * 70 }));
    let bs = 0, prev = 0;
    const pts = [];
    return {
      draw(b) {
        const { width: w, height: h } = canvas;
        // trails: fade what's there toward TRANSPARENT (not to a dark fill),
        // so a backdrop image stays visible underneath
        c.globalCompositeOperation = "destination-out";
        c.fillStyle = "rgba(0, 0, 0, 0.3)";
        c.fillRect(0, 0, w, h);
        bs = bs * 0.84 + b.bass * 0.16;
        const kick = Math.max(0, b.bass - prev) * 2.2;
        prev = b.bass;
        const cx = w / 2, cy = h / 2, unit = Math.min(w, h), maxR = unit * 0.46;
        pts.length = 0;
        c.globalCompositeOperation = "lighter";
        for (const p of P) {
          p.angle += (0.0015 + b.treble * 0.009) * p.speed * 14;
          p.radius += kick * 0.03 * p.speed;
          if (p.radius > 1) p.radius = 0.08 + Math.random() * 0.12;
          const r = unit * 0.06 + p.radius * maxR * (0.65 + bs * 0.35);
          const x = cx + Math.cos(p.angle) * r, y = cy + Math.sin(p.angle) * r;
          const v = 0.35 + (1 - p.radius) * 0.65, size = p.size * (1 + bs * 1.6) * (unit / 600);
          pts.push({ x, y });
          const g = c.createRadialGradient(x, y, 0, x, y, size * 4);
          g.addColorStop(0, `hsla(${p.hue}, 100%, ${72 + bs * 15}%, ${0.9 * v})`);
          g.addColorStop(1, `hsla(${p.hue}, 100%, 60%, 0)`);
          c.fillStyle = g;
          c.beginPath();
          c.arc(x, y, size * 4, 0, Math.PI * 2);
          c.fill();
        }
        const ld = unit * 0.11;
        c.lineWidth = Math.max(0.6, unit * 0.0016);
        for (let i = 0; i < pts.length; i++) {
          for (let j = i + 1; j < pts.length; j++) {
            const dx = pts[i].x - pts[j].x, dy = pts[i].y - pts[j].y, d2 = dx * dx + dy * dy;
            if (d2 > ld * ld) continue;
            c.strokeStyle = `hsla(275, 90%, 72%, ${(1 - Math.sqrt(d2) / ld) * 0.28 * (0.4 + bs)})`;
            c.beginPath();
            c.moveTo(pts[i].x, pts[i].y);
            c.lineTo(pts[j].x, pts[j].y);
            c.stroke();
          }
        }
        c.globalCompositeOperation = "source-over";
      },
    };
  }

  const MODES = ["orb", "bars", "radial", "particles", "plasma", "kaleidoscope"];
  const MAKERS = { bars: barsMode, radial: radialMode, particles: particlesMode };

  function attach(media, stage, opts = {}) {
    let an = null, data = null, mode = null, impl = null, canvas = null, raf = 0, dead = false;
    let art = [], shown = null;
    const t0 = performance.now();
    const back = [document.createElement("img"), document.createElement("img")];
    for (const im of back) { im.className = "aviz-back"; im.alt = ""; stage.appendChild(im); }
    let front = 0;

    const tap = () => {
      if (an) return;
      try {
        const ac = ctx();
        const srcNode = ac.createMediaElementSource(media);
        an = ac.createAnalyser();
        an.fftSize = 2048;
        srcNode.connect(an);
        an.connect(ac.destination);
        data = new Uint8Array(an.frequencyBinCount);
      } catch (e) { console.warn("[viz] analyser unavailable:", e); }
    };
    const onPlay = () => { tap(); if (CTX && CTX.state !== "running") CTX.resume(); };
    media.addEventListener("play", onPlay);

    const fit = () => {
      if (!canvas) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = Math.max(1, Math.round(canvas.clientWidth * dpr)), h = Math.max(1, Math.round(canvas.clientHeight * dpr));
      if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    };
    const ro = new ResizeObserver(fit);

    function setMode(m) {
      if (!MODES.includes(m)) m = "orb";
      if (impl && impl.destroy) impl.destroy();
      if (canvas) { ro.unobserve(canvas); canvas.remove(); }
      // a canvas keeps its first context type: a new one per mode
      canvas = document.createElement("canvas");
      canvas.className = "aviz-canvas" + (art.length ? " blend" : "");
      stage.insertBefore(canvas, stage.querySelector(".aviz-over"));
      ro.observe(canvas);
      fit();
      mode = m;
      try {
        impl = SHADERS[m] ? shaderMode(canvas, SHADERS[m], opts.seed) : MAKERS[m](canvas);
      } catch (e) { console.warn("[viz]", m, e); impl = null; }
      if (!impl && SHADERS[m]) { impl = null; return setMode("radial"); } // no WebGL2
      return m;
    }

    function pickArt() {
      if (!art.length) return;
      const t = media.currentTime || 0;
      const a = art.find((x) => t >= x.start && t < x.end) || (t < art[0].start ? art[0] : art[art.length - 1]);
      if (a === shown) return;
      shown = a;
      const nxt = back[1 - front];
      nxt.onload = () => { nxt.classList.add("on"); back[front].classList.remove("on"); front = 1 - front; };
      nxt.src = a.url;
    }

    function setBackdrop(items) {
      art = (items || []).slice().sort((a, b) => a.start - b.start);
      shown = null;
      if (canvas) canvas.classList.toggle("blend", art.length > 0);
      if (!art.length) for (const im of back) im.classList.remove("on");
      pickArt();
    }
    media.addEventListener("timeupdate", pickArt);
    media.addEventListener("seeked", pickArt);

    function frame() {
      if (dead) return;
      raf = requestAnimationFrame(frame);
      if (document.hidden || !impl || !canvas.width) return;
      const live = an && !media.paused;
      const b = live ? readBands(an, data) : { bass: 0.12, mid: 0.12, treble: 0.12 };
      if (!live && data) data.fill(0);
      impl.draw(b, (performance.now() - t0) / 1000, data);
    }
    setMode(opts.mode);
    frame();
    return {
      setMode, setBackdrop,
      get mode() { return mode; },
      destroy() {
        dead = true;
        cancelAnimationFrame(raf);
        ro.disconnect();
        media.removeEventListener("play", onPlay);
        media.removeEventListener("timeupdate", pickArt);
        media.removeEventListener("seeked", pickArt);
        if (impl && impl.destroy) impl.destroy();
      },
    };
  }

  window.AudioViz = { attach, MODES };
})();

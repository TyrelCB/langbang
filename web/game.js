// LangBang X-SIM — a Mega Man X style platformer for waiting on the model.
// Self-contained: canvas, fixed 16px tiles, one hand-built stage (X-01).
// Open with the ⚔ X-SIM button (topbar) or G when the composer isn't focused;
// ESC closes instantly so you can watch the chat stream. Deep-link: ?game=1.
// Sound plays through the same SFX slots as chat (notation only for now —
// game_* slots live in SOUND_DESIGN.md / sounds/manifest.json, no assets).
(() => {
"use strict";

// ---------- level ----------
// Tiles: '#' solid  '^' spike  '+' orb  'G' goal door
//        'E' ground bot  'F' flyer  'T' turret  'S' player spawn
const LW = 120, LH = 20, TILE = 16;
const VIEW_W = 560, VIEW_H = 320, SCALE = 3;

const MAP = (() => {
  const g = Array.from({ length: LH }, () => new Array(LW).fill(" "));
  const fill = (c0, r0, c1, r1, ch) => {
    for (let r = r0; r <= r1; r++)
      for (let c = c0; c <= c1; c++) {
        if (r < 0 || r >= LH || c < 0 || c >= LW) throw new Error(`map oob ${c},${r}`);
        g[r][c] = ch;
      }
  };
  fill(0, 0, LW - 1, 0, "#");          // ceiling
  fill(0, 17, LW - 1, 19, "#");        // ground
  // pits (bottomless — falling out of the world kills, X-style)
  fill(28, 17, 31, 19, " ");
  fill(96, 17, 99, 19, " ");
  fill(101, 17, 103, 19, " ");
  // intro steps
  fill(20, 16, 21, 16, "#"); fill(23, 15, 24, 16, "#");
  // platform over pit A + high platform later
  fill(33, 13, 38, 13, "#");
  fill(58, 12, 61, 12, "#");           // turret ledge
  fill(77, 8, 79, 8, "#");
  // the wall-kick shaft: two thin pillars, corridor between, climb out the top
  fill(70, 4, 70, 16, "#");
  fill(73, 4, 73, 16, "#");
  // spikes on ground
  fill(47, 16, 48, 16, "^"); fill(51, 16, 52, 16, "^");
  // pickups
  g[12][35] = "+"; g[14][49] = "+"; g[10][71] = "+"; g[16][72] = "+";
  g[7][78] = "+"; g[14][112] = "+";
  // enemies
  g[16][18] = "E"; g[16][64] = "E"; g[16][105] = "E";
  g[10][30] = "F"; g[8][66] = "F"; g[9][106] = "F";
  g[16][60] = "T"; g[16][110] = "T";
  // exit door (2 wide, 3 tall)
  fill(115, 14, 116, 16, "G");
  g[16][3] = "S";
  return g;
})();

const solid = (c, r) =>
  c < 0 || c >= LW || r >= LH || r < 0 ? true : MAP[r][c] === "#";
const tileAt = (x, y) => MAP[Math.floor(y / TILE)]?.[Math.floor(x / TILE)] ?? " ";

// ---------- tuning (px, seconds) ----------
const RUN = 205, ACC = 1750, AIR_ACC = 1150, FRIC = 2100;
const GRAV = 2300, MAX_FALL = 820, JUMP_V = 620, JUMP_CUT = 0.45;
const COYOTE = 0.08, BUF = 0.09;
const SLIDE_MAX = 150, WJ_VX = 200, WJ_VY = 600, WJ_LOCK = 0.08;
const DASH_SPD = 460, DASH_T = 0.2, DASH_CD = 0.25;
const BSPD = 560, SHOT_CD = 0.15, MAX_SHOTS = 6;
const CHARGE_LV1 = 0.35, CHARGE_FULL = 1.05;   // hold Z: lv1 then full charge
const IFRAMES = 1.1, HP_MAX = 12;

// ---------- state ----------
const K = new Set();
let cv = null, ctx = null, running = false, raf = 0, last = 0;
let camX = 0, shake = 0, gt = 0;               // global clock (s)
let P, enemies, shots, eshots, orbs, parts, hist;
let state = "ready";                            // ready | play | dying | win
let timeS = 0, dieT = 0, bestT = null;

try { bestT = parseFloat(localStorage.getItem("lb-game-best")) || null; } catch {}

const sfx = (n) => { try { window.SFX && SFX.play(n); } catch {} };
const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const fmtT = (s) => `${Math.floor(s / 60)}:${(s % 60).toFixed(2).padStart(5, "0")}`;

function resetStage() {
  let sx = 56, sy = 252;
  enemies = []; orbs = [];
  for (let r = 0; r < LH; r++) for (let c = 0; c < LW; c++) {
    const t = MAP[r][c], cx = c * TILE, cy = r * TILE;
    if (t === "S") { sx = cx + 8; sy = cy + TILE; }
    else if (t === "E") enemies.push({ k: "E", w: 14, h: 10, x: cx + 8, y: cy + TILE, vx: 55, hp: 1, a: 0 });
    else if (t === "F") enemies.push({ k: "F", w: 13, h: 11, x: cx + 8, y: cy + 8, ax: cx + 8, ay: cy + 8, hp: 1, a: Math.random() * 6 });
    else if (t === "T") enemies.push({ k: "T", w: 14, h: 14, x: cx + 8, y: cy + TILE, hp: 2, a: 1.2 + Math.random() });
    else if (t === "+") orbs.push({ x: cx + 8, y: cy + 8, t: Math.random() * 6 });
  }
  P = { x: sx, y: sy, w: 10, h: 20, vx: 0, vy: 0, f: 1, hp: HP_MAX,
        onG: false, coyote: 0, bufT: 0, inv: 0, dashT: 0, dashCd: 0,
        shotCd: 0, wall: 0, lockT: 0, muzzle: 0,
        charging: false, chargeT: 0, chargedPing: false };
  shots = []; eshots = []; parts = []; hist = [];
  timeS = 0; camX = clamp(P.x - VIEW_W / 2, 0, LW * TILE - VIEW_W);
}

// ---------- input ----------
const GAME_KEYS = new Set([
  "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Space", "Enter",
  "KeyA", "KeyD", "KeyW", "KeyX", "KeyZ", "KeyJ", "KeyK", "KeyC", "KeyR",
  "ShiftLeft", "ShiftRight", "ControlLeft", "ControlRight",
]);
// aliases so awkward physical-keyboard rollover never eats an action:
// blaster = Z/J/K/C, dash = X/Shift/Ctrl
const SHOOT_KEYS = ["KeyZ", "KeyJ", "KeyK", "KeyC"];
const DASH_KEYS = ["KeyX", "ShiftLeft", "ShiftRight", "ControlLeft", "ControlRight"];

function onKey(e, down) {
  if (!running) return;
  if (e.key === "Escape") {
    if (down) close();
    e.preventDefault(); e.stopImmediatePropagation();
    return;
  }
  if (e.code === "KeyG") {
    if (down && !e.repeat) close();
    e.preventDefault(); e.stopImmediatePropagation();
    return;
  }
  if (!GAME_KEYS.has(e.code)) return;
  // The overlay covers the whole UI and open() blurs any text field, so
  // while the sim runs we own these keys — stopImmediatePropagation keeps
  // them from also driving the chat UI (space scrolling, enter sending).
  e.preventDefault(); e.stopImmediatePropagation();
  const code = e.code;
  if (down) {
    if (!K.has(code)) {
      if (["Space", "ArrowUp", "KeyW"].includes(code)) P.bufT = BUF;
      if (DASH_KEYS.includes(code)) tryDash();
      if (SHOOT_KEYS.includes(code) && state === "play" && !P.charging) {
        P.charging = true; P.chargeT = 0; P.chargedPing = false;
        if (P.shotCd <= 0 && shots.length < MAX_SHOTS) fireShot();
      }
      if (code === "Enter" && (state === "ready" || state === "win")) {
        resetStage(); state = "play";
      }
      if (code === "KeyR" && state === "play") resetStage();
    }
    K.add(code);
  } else K.delete(code);
}
const kd = (e) => onKey(e, true), ku = (e) => onKey(e, false);

const left = () => K.has("ArrowLeft") || K.has("KeyA");
const right = () => K.has("ArrowRight") || K.has("KeyD");
const jumpHeld = () => K.has("Space") || K.has("ArrowUp") || K.has("KeyW");

function tryDash() {
  if (state !== "play" || P.dashCd > 0 || P.dashT > 0) return;
  P.dashT = DASH_T; P.dashCd = DASH_CD; P.f = left() ? -1 : right() ? 1 : P.f;
  sfx("game_dash");
}

function fireShot() {
  shots.push({ x: P.x + P.f * 9, y: P.y - 11, vx: P.f * BSPD, w: 8, h: 4, centered: true });
  P.shotCd = SHOT_CD; P.muzzle = 0.07; sfx("game_shoot");
}

// charged shots hit harder and punch THROUGH enemies, like the X buster
function fireCharge(lvl) {
  const s = lvl === 2
    ? { w: 18, h: 12, spd: 640, dmg: 4 }
    : { w: 12, h: 8, spd: 600, dmg: 2 };
  shots.push({ x: P.x + P.f * 12, y: P.y - 11, vx: P.f * s.spd, w: s.w, h: s.h,
               dmg: s.dmg, pierce: true, lvl, centered: true });
  P.muzzle = 0.14;
  if (lvl === 2) shake = Math.max(shake, 0.1);
  sfx("game_shoot");
}

// ---------- physics helpers ----------
// AABB. Entities are feet-anchored (y = feet, grows upward);
// shots are center-anchored (y = center of the pellet).
const cy = (o) => (o.centered ? o.y : o.y - o.h / 2);
const hit = (a, b) =>
  a.x - a.w / 2 < b.x + b.w / 2 && a.x + a.w / 2 > b.x - b.w / 2 &&
  cy(a) - a.h / 2 < cy(b) + b.h / 2 && cy(a) + a.h / 2 > cy(b) - b.h / 2;

function collide(x, y, w, h) {
  const c0 = Math.floor((x - w / 2) / TILE), c1 = Math.floor((x + w / 2 - 0.01) / TILE);
  const r0 = Math.floor((y - h) / TILE), r1 = Math.floor((y - 0.01) / TILE);
  for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) if (solid(c, r)) return true;
  return false;
}

function hurt(n, srcX) {
  if (P.inv > 0 || state !== "play") return;
  P.hp -= n; P.inv = IFRAMES; shake = 0.3;
  P.vy = -320; P.vx = 190 * (P.x < srcX ? -1 : 1); P.dashT = 0;
  sfx("game_hurt");
  if (P.hp <= 0) die();
}

function die() {
  state = "dying"; dieT = 0; P.hp = 0;
  sfx("game_death");
  for (let i = 0; i < 16; i++) {
    const a = (i / 16) * Math.PI * 2;
    parts.push({ x: P.x, y: P.y - 10, vx: Math.cos(a) * (90 + Math.random() * 140),
                 vy: Math.sin(a) * (90 + Math.random() * 140) - 60,
                 t: 0.9, c: i % 2 ? "#ff8f2b" : "#3fd6ff" });
  }
}

// ---------- update ----------
function update(dt) {
  gt += dt;
  parts = parts.filter((p) => (p.t -= dt) > 0);
  parts.forEach((p) => { p.x += p.vx * dt; p.y += p.vy * dt; p.vy += 900 * dt; });
  if (shake > 0) shake -= dt;

  if (state === "dying") {
    dieT += dt;
    // classic respawn: straight back into the run, no menu
    if (dieT > 1.0) { resetStage(); state = "play"; }
    return;
  }
  if (state !== "play") return;
  timeS += dt;

  const p = P;
  p.inv -= dt; p.dashCd -= dt; p.bufT -= dt; p.coyote -= dt;
  p.lockT -= dt; p.shotCd -= dt; p.muzzle -= dt;
  if (p.onG) p.coyote = COYOTE;

  // horizontal control (wall-jump lock keeps its arc untouched)
  const want = (right() ? 1 : 0) - (left() ? 1 : 0);
  if (p.dashT > 0) {
    p.dashT -= dt; p.vx = p.f * DASH_SPD;
    hist.push({ x: p.x, y: p.y, f: p.f, t: 0.18 });
  } else if (p.lockT <= 0 || want === p.kickDir) {
    if (want) {
      const acc = (p.onG ? ACC : AIR_ACC) * dt;
      if (Math.sign(p.vx) === want && Math.abs(p.vx) >= RUN) p.vx = want * RUN;
      else p.vx = clamp(p.vx + want * acc, -RUN, RUN);
      p.f = want;
    } else {
      const f = (p.onG ? FRIC : 400) * dt;
      p.vx = Math.abs(p.vx) <= f ? 0 : p.vx - Math.sign(p.vx) * f;
    }
  }
  // excess speed from dashes/wall-jumps bleeds back down to RUN
  if (p.dashT <= 0 && (p.lockT <= 0 || want === p.kickDir) && Math.abs(p.vx) > RUN)
    p.vx -= Math.sign(p.vx) * Math.min(Math.abs(p.vx) - RUN, 1200 * dt);

  // jump
  const wallSlide = !p.onG && p.vy > 0 && want !== 0 && p.lockT <= 0 &&
    collide(p.x + want * 2, p.y, p.w, p.h);
  p.wall = wallSlide ? want : 0;
  if (wallSlide) p.vy = Math.min(p.vy, SLIDE_MAX);
  if (p.bufT > 0) {
    if (p.onG || p.coyote > 0) {
      p.vy = -JUMP_V; p.onG = false; p.coyote = 0; p.bufT = 0; sfx("game_jump");
    } else if (wallSlide) {
      p.vy = -WJ_VY; p.vx = -want * WJ_VX; p.f = -want;
      p.lockT = WJ_LOCK; p.kickDir = want; p.bufT = 0; sfx("game_jump");
    }
  }
  if (!jumpHeld() && p.vy < -JUMP_V * JUMP_CUT) p.vy = -JUMP_V * JUMP_CUT;
  if (p.vy < 0 && p.dashT > 0) { p.dashT = 0; p.vy = -JUMP_V; } // dash-jump keeps vx

  // gravity + integrate
  p.vy = Math.min(p.vy + GRAV * dt, MAX_FALL);
  if (collide(p.x + p.vx * dt, p.y, p.w, p.h)) {
    p.vx = 0;
  } else p.x += p.vx * dt;
  p.x = clamp(p.x, 6, LW * TILE - 6);
  const fallSpeed = p.vy;
  if (collide(p.x, p.y + p.vy * dt, p.w, p.h)) {
    if (p.vy > 0) { p.onG = true; if (fallSpeed > 640) shake = Math.max(shake, 0.08); }
    p.vy = 0;
  } else { p.y += p.vy * dt; p.onG = false; }
  // snap feet to tile grid so collision rows are exact
  // (p.y is the foot line; small float drift is fine at these speeds)

  // buster: press = quick shot, hold = charge, release = charged blast
  if (p.charging) {
    if (SHOOT_KEYS.some((c) => K.has(c))) {
      p.chargeT += dt;
      if (!p.chargedPing && p.chargeT >= CHARGE_FULL) {
        p.chargedPing = true; sfx("game_charge_full");
      }
    } else {
      const lvl = p.chargeT >= CHARGE_FULL ? 2 : p.chargeT >= CHARGE_LV1 ? 1 : 0;
      p.charging = false; p.chargeT = 0; p.chargedPing = false;
      if (lvl && shots.length < MAX_SHOTS) fireCharge(lvl);
    }
  }
  shots = shots.filter((b) => {
    b.x += b.vx * dt;
    if (solid(Math.floor(b.x / TILE), Math.floor(b.y / TILE))) {
      parts.push({ x: b.x, y: b.y, vx: 0, vy: -60, t: 0.12, c: "#3fd6ff" });
      return false;
    }
    return Math.abs(b.x - p.x) < 900;
  });

  // enemies
  for (const e of enemies) {
    if (e.dead) continue;
    if (e.k === "E") {
      // feet ride the tile line; turn at walls and at floor edges
      const ahead = e.x + Math.sign(e.vx) * (e.w / 2 + 2);
      const wallAhead = collide(ahead, e.y, 2, 10);
      const floorAhead = collide(ahead, e.y + 2, 3, 4);
      if (wallAhead || !floorAhead) e.vx *= -1;
      e.x += e.vx * dt; e.a += dt;
    } else if (e.k === "F") {
      const dx = p.x - e.x, dy = p.y - 10 - e.y, d = Math.hypot(dx, dy);
      if (d < 150 && d > 1) { e.x += (dx / d) * 85 * dt; e.y += (dy / d) * 85 * dt; }
      else { e.x += (e.ax - e.x) * Math.min(1, dt * 2); e.y += (e.ay - e.y) * Math.min(1, dt * 2); }
      e.a += dt;
    } else if (e.k === "T") {
      e.a -= dt;
      const dx = p.x - e.x, dy = p.y - 10 - e.y;
      if (e.a <= 0 && Math.abs(dx) < 260 && Math.abs(dy) < 40) {
        eshots.push({ x: e.x + Math.sign(dx) * 9, y: e.y - 4, vx: Math.sign(dx) * 210, w: 6, h: 5, centered: true });
        e.a = 1.7;
      }
    }
    if (state === "play" && hit(p, e)) hurt(1, e.x);
  }

  // enemy shots
  eshots = eshots.filter((b) => {
    b.x += b.vx * dt;
    if (solid(Math.floor(b.x / TILE), Math.floor(b.y / TILE))) return false;
    if (state === "play" && hit(p, b)) { hurt(1, b.x); return false; }
    return Math.abs(b.x - p.x) < 900;
  });

  // player shots vs enemies
  for (const b of shots) {
    for (const e of enemies) {
      if (e.dead) continue;
      if (hit(b, e)) {
        if (!b.pierce) b.dead = true;
        e.hp -= b.dmg || 1;
        if (e.hp <= 0) {
          e.dead = true; sfx("game_kill");
          for (let i = 0; i < 8; i++)
            parts.push({ x: e.x, y: e.y - e.h / 2, vx: (Math.random() - 0.5) * 260,
                         vy: (Math.random() - 0.7) * 260, t: 0.45,
                         c: i % 2 ? "#ff8f2b" : "#ff4d8f" });
        }
        break;
      }
    }
  }
  shots = shots.filter((b) => !b.dead);

  // orbs (float +4px bob via .t)
  orbs = orbs.filter((o) => {
    o.t += dt;
    if (Math.abs(p.x - o.x) < 11 && Math.abs(p.y - 10 - o.y) < 13) {
      p.hp = Math.min(HP_MAX, p.hp + 3); sfx("game_kill");
      for (let i = 0; i < 6; i++)
        parts.push({ x: o.x, y: o.y, vx: (Math.random() - .5) * 180, vy: -Math.random() * 200, t: 0.4, c: "#ffd166" });
      return false;
    }
    return true;
  });

  // hazards
  for (let r = 0; r < LH; r++) for (let c = 0; c < LW; c++) {
    if (MAP[r][c] !== "^") continue;
    const cx = c * TILE + 8, cy = r * TILE + 12;
    if (Math.abs(p.x - cx) < 12 && Math.abs(p.y - 4 - cy) < 12) hurt(2, p.x);
  }
  if (p.y > LH * TILE + 40) { P.hp = 0; die(); }

  // goal
  const gx = 115 * TILE, gy = 14 * TILE;
  if (p.x + 5 > gx && p.x - 5 < gx + 32 && p.y > gy && p.y - 20 < gy + 48) {
    state = "win"; sfx("game_clear");
    if (bestT === null || timeS < bestT) {
      bestT = timeS; try { localStorage.setItem("lb-game-best", String(bestT)); } catch {}
      P.newBest = true;
    } else P.newBest = false;
  }

  hist = hist.filter((h) => (h.t -= dt) > 0);
  camX = clamp(p.x - VIEW_W / 2, 0, LW * TILE - VIEW_W);
}

// ---------- drawing ----------
function rect(x, y, w, h, c) { ctx.fillStyle = c; ctx.fillRect(Math.round(x), Math.round(y), w, h); }
function txt(s, x, y, c, size = 8, align = "left") {
  ctx.fillStyle = c; ctx.font = `${size}px "JetBrains Mono", ui-monospace, monospace`;
  ctx.textAlign = align; ctx.fillText(s, x, y);
}

const C = { bg: "#060a12", tile: "#16273f", tileTop: "#2c4a72", spike: "#cdd9e5",
  A: "#3fd6ff", D: "#1a7ea8", O: "#ff8f2b", W: "#eafcff", RED: "#ff4d5e", MAG: "#ff4d8f", PUR: "#b06cff" };

function drawTiles() {
  const c0 = Math.floor(camX / TILE), c1 = Math.min(LW - 1, c0 + Math.ceil(VIEW_W / TILE) + 1);
  for (let r = 0; r < LH; r++) for (let c = c0; c <= c1; c++) {
    const t = MAP[r][c], x = c * TILE - camX, y = r * TILE;
    if (t === "#") {
      rect(x, y, TILE, TILE, C.tile);
      if (!solid(c, r - 1)) rect(x, y, TILE, 2, C.tileTop);
      if ((c + r) % 3 === 0) rect(x + 3, y + 6, 2, 2, "#1d3350");
    } else if (t === "^") {
      for (let i = 0; i < 2; i++) {
        const bx = x + i * 8;
        ctx.fillStyle = C.spike; ctx.beginPath();
        ctx.moveTo(bx, y + TILE); ctx.lineTo(bx + 4, y + 3); ctx.lineTo(bx + 8, y + TILE);
        ctx.fill();
      }
    } else if (t === "G") {
      rect(x, y, TILE, TILE, "#0a1224");
      const pulse = 3 + Math.sin(gt * 4) * 2;
      rect(x + 2, y + 2, TILE - 4, TILE - 4, `rgba(255,143,43,${0.25 + pulse * 0.02})`);
    } else if (t === "+") {
      const ox = c * TILE + 8, oy = r * TILE + 8;
      const o = orbs.find((q) => q.x === ox && q.y === oy);
      if (o) {
        const bob = Math.sin(gt * 3 + o.t) * 3;
        rect(x + 3, y + 4 + bob, 10, 4, C.O); rect(x + 6, y + 1 + bob, 4, 10, C.O);
        rect(x + 6, y + 5 + bob, 4, 2, C.W);
      }
    }
  }
  // door frame over the G block
  const gx = 115 * TILE - camX, gy = 14 * TILE;
  ctx.strokeStyle = C.A; ctx.lineWidth = 1;
  ctx.strokeRect(gx + 0.5, gy + 0.5, 31, 47);
  txt("EXIT", gx + 16, gy - 3, C.O, 7, "center");
}

function drawBg() {
  rect(0, 0, VIEW_W, VIEW_H, C.bg);
  // far skyline (slow parallax)
  ctx.fillStyle = "#0b1626";
  for (let i = -1; i < 6; i++) {
    const x = ((i * 190 - camX * 0.2) % (VIEW_W + 190) + VIEW_W + 190) % (VIEW_W + 190) - 95;
    const h = 70 + ((i * 37) % 40);
    ctx.fillRect(x, VIEW_H - h, 60, h);
    ctx.fillRect(x + 70, VIEW_H - h * 0.6, 40, h * 0.6);
  }
  // mid grid dots
  ctx.fillStyle = "#10233a";
  const off = (camX * 0.5) % 32;
  for (let x = -off; x < VIEW_W; x += 32)
    for (let y = 8; y < VIEW_H; y += 32) ctx.fillRect(x, y, 2, 2);
}

function drawX(p, ghost = false) {
  if (ghost) ctx.globalAlpha = 0.35;
  const a = C.A, d = C.D;
  const x = Math.round(p.x - camX), y = Math.round(p.y);
  const run = p.onG && Math.abs(p.vx) > 20;
  const ph = run ? [[0, 0], [1, -1], [2, 0], [1, 1]][Math.floor(gt * 9) % 4] : null;
  const f = p.f;
  // legs
  if (ph) { rect(x - 3 + ph[0] * f, y - 7, 3, 7, d); rect(x + 1 - ph[0] * f, y - 7 + ph[1], 3, 7 - ph[1], d); }
  else if (!p.onG) { rect(x - 4, y - 8, 4, 6, d); rect(x + 1, y - 6, 4, 4, d); }
  else { rect(x - 3, y - 7, 3, 7, d); rect(x + 1, y - 7, 3, 7, d); }
  // torso + shoulders
  rect(x - 4, y - 15, 8, 8, a);
  rect(x - 6, y - 15, 2, 4, a); rect(x + 4, y - 15, 2, 4, a);
  rect(x - 2, y - 13, 4, 3, C.O);            // chest core
  // head
  rect(x - 3, y - 21, 6, 6, a);
  rect(x + (f > 0 ? 0 : -2), y - 19, 3, 2, C.W);   // visor
  rect(x - 1 + f * 2, y - 23, 2, 2, C.O);          // crest
  // arm / buster
  if (p.muzzle > 0 && !ghost) {
    rect(x + (f > 0 ? 2 : -10), y - 12, 8, 3, d);
    rect(x + (f > 0 ? 10 : -14), y - 13, 4, 5, C.O);
  } else rect(x + (f > 0 ? 3 : -6), y - 13, 3, 6, d);
  if (ghost) ctx.globalAlpha = 1;
}

function drawEnemies() {
  for (const e of enemies) {
    if (e.dead) continue;
    const x = Math.round(e.x - camX), y = Math.round(e.y);
    if (x < -30 || x > VIEW_W + 30) continue;
    if (e.k === "E") {
      rect(x - 7, y - 9, 14, 8, C.MAG); rect(x - 5, y - 11, 10, 2, C.MAG);
      rect(x + (e.vx > 0 ? 2 : -5), y - 8, 3, 3, C.RED);          // eye
      rect(x - (e.vx > 0 ? 9 : 4), y - 6, 4, 2, "#7c3b63");        // tail
      const gait = Math.floor(e.a * 10) % 2;
      rect(x - 5, y - 1 + gait, 3, 2 - gait, "#6b2c50"); rect(x + 2, y - 1 - gait + 1, 3, 2 - gait, "#6b2c50");
    } else if (e.k === "F") {
      const flap = Math.floor(e.a * 9) % 2 ? -2 : 1;
      rect(x - 5, y - 4, 10, 8, C.PUR);
      rect(x - 9, y - 3 + flap, 4, 2, "#8a4fd0"); rect(x + 5, y - 3 - flap, 4, 2, "#8a4fd0");
      rect(x - 2, y - 2, 4, 3, C.RED);
    } else {
      rect(x - 7, y - 6, 14, 6, "#3c4d63");
      rect(x - 4, y - 11, 8, 5, C.RED);
      const dir = P.x >= e.x ? 1 : -1;
      rect(x + (dir > 0 ? 2 : -10), y - 10, 8, 3, "#9aa7b8");
    }
  }
}

function drawHud() {
  txt("STAGE X-01 // HUNTING PROTOCOL", VIEW_W / 2, 10, C.D, 8, "center");
  txt("ENERGY", 8, 20, C.D, 7);
  for (let i = 0; i < HP_MAX; i++) {
    const x = 8 + i * 10, y = 23;
    if (i < P.hp) rect(x, y, 8, 5, P.hp <= 4 && Math.floor(gt * 6) % 2 ? C.O : C.A);
    else rect(x, y, 8, 1, C.D);
  }
  txt(fmtT(timeS), VIEW_W - 8, 28, C.W, 9, "right");
  txt(bestT !== null ? "BEST " + fmtT(bestT) : "BEST --:--", VIEW_W - 8, 39, C.D, 7, "right");
}

function drawPanel() {
  ctx.fillStyle = "rgba(6,10,18,0.72)"; ctx.fillRect(0, 0, VIEW_W, VIEW_H);
  const cx = VIEW_W / 2;
  const lines = state === "win"
    ? [["STAGE CLEAR", C.A, 18],
       [fmtT(timeS), C.W, 12],
       [P.newBest ? "NEW BEST!" : bestT !== null ? "BEST " + fmtT(bestT) : "", C.O, 10],
       ["[ENTER] RUN IT BACK", Math.floor(gt * 2) % 2 ? C.O : C.D, 9]]
    : [["X-SIM  //  STAGE X-01", C.A, 15],
       ["←→/AD RUN   SPACE JUMP   X DASH   Z BLASTER · HOLD Z = CHARGE", C.W, 8],
       ["WALL-KICK: HOLD INTO WALL + JUMP", C.W, 8],
       ["R RESTART   ESC EXIT SIM", C.D, 8],
       ["[ENTER] BEGIN THE HUNT", Math.floor(gt * 2) % 2 ? C.O : C.D, 10]];
  let y = VIEW_H / 2 - 34;
  for (const [s, c, n] of lines) {
    if (!s) { y += 12; continue; }
    txt(s, cx, y, c, n, "center"); y += n + 6;
  }
}

function draw() {
  ctx.setTransform(SCALE, 0, 0, SCALE, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.translate(shake > 0 ? (Math.random() - 0.5) * 6 : 0,
                shake > 0 ? (Math.random() - 0.5) * 6 : 0);
  drawBg();
  drawTiles();
  for (const h of hist) drawX({ x: h.x, y: h.y, f: h.f, onG: false, vx: 0, muzzle: 0, hp: 1, }, true);
  drawEnemies();
  for (const b of shots) {
    const bx = b.x - camX;
    if (b.lvl === 2) {
      rect(bx - 11, b.y - 8, 22, 16, "rgba(255,143,43,.35)");
      rect(bx - 9, b.y - 6, 18, 12, C.A); rect(bx - 5, b.y - 3, 10, 6, C.W);
    } else if (b.lvl) {
      rect(bx - 6, b.y - 4, 12, 8, C.A); rect(bx - 3, b.y - 2, 6, 4, C.W);
    } else {
      rect(bx - 4, b.y - 2, 8, 4, C.A); rect(bx - 1, b.y - 1, 3, 2, C.W);
    }
  }
  for (const b of eshots) { rect(b.x - camX - 3, b.y - 2, 6, 5, C.O); }
  for (const p of parts) { ctx.fillStyle = p.c; ctx.fillRect(Math.round(p.x - camX) - 1, Math.round(p.y) - 1, 2, 2); }
  if (state === "play" || state === "win") {
    if (!(P.inv > 0 && Math.floor(gt * 14) % 2)) drawX(P);
    if (P.charging && P.chargeT > 0.12) {
      const full = P.chargeT >= CHARGE_FULL;
      ctx.strokeStyle = full ? C.O : C.A;
      ctx.globalAlpha = 0.45 + 0.35 * Math.sin(gt * 22);
      ctx.lineWidth = full ? 2 : 1;
      ctx.beginPath();
      ctx.arc(Math.round(P.x - camX), Math.round(P.y - 11),
              9 + Math.min(P.chargeT / CHARGE_FULL, 1) * 9, 0, 6.283);
      ctx.stroke();
      ctx.globalAlpha = 1; ctx.lineWidth = 1;
    }
  }
  drawHud();
  if (state === "ready" || state === "win") drawPanel();
  if (state === "dying") txt("REBOOTING…", VIEW_W / 2, VIEW_H / 2, C.RED, 10, "center");
}

// ---------- loop ----------
function frame(ts) {
  if (!running) return;
  const dt = Math.min((ts - last) / 1000 || 0.016, 0.033);
  last = ts;
  update(dt);
  draw();
  raf = requestAnimationFrame(frame);
}

function open() {
  if (running) return;
  cv = document.getElementById("game-canvas");
  ctx = cv.getContext("2d");
  document.getElementById("game-overlay").classList.remove("hidden");
  document.activeElement?.blur?.();
  resetStage(); state = "ready";
  running = true; last = performance.now();
  window.addEventListener("keydown", kd, true);
  window.addEventListener("keyup", ku, true);
  raf = requestAnimationFrame(frame);
}
function close() {
  if (!running) return;
  running = false;
  cancelAnimationFrame(raf);
  window.removeEventListener("keydown", kd, true);
  window.removeEventListener("keyup", ku, true);
  K.clear();
  document.getElementById("game-overlay").classList.add("hidden");
}

// global hotkey to OPEN (the running-state capture listener handles closing);
// ignored while typing in any text field so chat input can contain "g"
window.addEventListener("keydown", (e) => {
  if (running || e.repeat || e.code !== "KeyG" || e.ctrlKey || e.metaKey || e.altKey) return;
  if (document.activeElement?.matches?.("input, textarea")) return;
  open();
});

document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("btn-game");
  if (btn) btn.onclick = () => { sfx("click"); open(); };
  document.getElementById("btn-game-close").onclick = () => { sfx("click"); close(); };
  const qp = new URLSearchParams(location.search);
  if (qp.get("game")) {
    open();
    // ?game=1&play=1 skips the ready screen (also handy for smoke tests)
    if (qp.get("play")) { resetStage(); state = "play"; }
  }
});

window.LBGame = {
  open, close, isOpen: () => running,
  keys: () => [...K],
  debug: () => ({ keys: [...K], state, charging: P?.charging, chargeT: P?.chargeT,
                 shots: shots.map((s) => ({ lvl: s.lvl || 0, dmg: s.dmg || 1 })) }),
};
})();

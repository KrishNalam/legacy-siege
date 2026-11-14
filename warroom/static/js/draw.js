// Pure Canvas2D drawing primitives (procedural art, no images). Coordinates are virtual stage px.
export const TAU = Math.PI * 2;

export const C = {
  bg: "#04070d", ink: "#e8eef8", ink2: "#a9b8cc", muted: "#64768e", line: "#1a2738",
  red: "#ff4757", amber: "#ffb020", green: "#2ee6a0", mongo: "#00ed64", blue: "#4cc9f0",
  gold: "#ffd23f", violet: "#b388ff", pink: "#f9a8d4", steel: "#152233",
};
export const HEALTH = { up: C.green, degraded: C.amber, down: C.red };
export const ROLE = {
  queen: "#ffd23f", scout: "#4cc9f0", worker: "#7aa2ff", guard: "#b388ff", waggle: "#5eead4",
  sentinel: "#38bdf8", nurse: "#f9a8d4", gardener: "#86efac",
  red: "#ff5a5f",  // Red fleet attackers (thought feed only; they are not bees)
};
export const FONT = {
  display: (px, w = 700) => `${w} ${px}px "Chakra Petch", Inter, sans-serif`,
  ui: (px, w = 600) => `${w} ${px}px Inter, system-ui, sans-serif`,
  mono: (px, w = 500) => `${w} ${px}px "JetBrains Mono", "Liberation Mono", monospace`,
};

export function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}
export function hash(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}
export function rng(seed) { // mulberry32
  let a = seed >>> 0;
  return () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

export function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}
export function hexPath(ctx, x, y, r, flat = true) {
  ctx.beginPath();
  for (let i = 0; i < 6; i++) {
    const a = (Math.PI / 3) * i + (flat ? 0 : Math.PI / 6);
    const px = x + r * Math.cos(a), py = y + r * Math.sin(a);
    i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
  }
  ctx.closePath();
}
export function glowCircle(ctx, x, y, r, color, a = 0.5) {
  const g = ctx.createRadialGradient(x, y, 0, x, y, r);
  g.addColorStop(0, hexA(color, a)); g.addColorStop(1, hexA(color, 0));
  ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.fill();
}
export function pill(ctx, x, y, text, { fg = C.ink, bg = "#0f1826", border = C.line, font = FONT.mono(10, 600), padX = 6, h = 16, align = "center" } = {}) {
  ctx.font = font;
  const w = ctx.measureText(text).width + padX * 2;
  const x0 = align === "center" ? x - w / 2 : align === "right" ? x - w : x;
  roundRect(ctx, x0, y - h / 2, w, h, h / 2);
  ctx.fillStyle = bg; ctx.fill();
  if (border) { ctx.strokeStyle = border; ctx.lineWidth = 1; ctx.stroke(); }
  ctx.fillStyle = fg; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(text, x0 + w / 2, y + 0.5);
  return w;
}
export function ellipsize(ctx, text, maxW) {
  if (ctx.measureText(text).width <= maxW) return text;
  let lo = 0, hi = text.length;
  while (lo < hi) { const m = (lo + hi + 1) >> 1; ctx.measureText(text.slice(0, m) + "…").width <= maxW ? (lo = m) : (hi = m - 1); }
  return text.slice(0, lo) + "…";
}
export function wrap(ctx, text, maxW, maxLines = 3) {
  const words = String(text).split(/\s+/); const lines = []; let cur = "";
  for (const w of words) {
    const t = cur ? cur + " " + w : w;
    if (ctx.measureText(t).width > maxW && cur) { lines.push(cur); cur = w; } else cur = t;
    if (lines.length === maxLines) break;
  }
  if (lines.length < maxLines && cur) lines.push(cur);
  if (lines.length === maxLines) lines[maxLines - 1] = ellipsize(ctx, lines[maxLines - 1], maxW);
  return lines;
}

// ------------------------------------------------------------------ bees
export function drawBee(ctx, x, y, o) {
  const { color = C.blue, scale = 1, facing = 1, t = 0, dead = false, alpha = 1, phase = 0, role } = o;
  ctx.save();
  ctx.globalAlpha *= alpha;
  ctx.translate(x, y);
  if (!dead) glowCircle(ctx, 0, 2, 30 * scale, color, 0.32);
  ctx.scale(scale * facing, scale);
  if (dead) ctx.rotate(Math.PI * 0.92);
  const body = dead ? "#5b6472" : "#ffc933", body2 = dead ? "#3d4450" : "#f59e0b", stripe = dead ? "#262b33" : "#1a1206";
  // wings (behind)
  const flap = dead ? 0.15 : Math.sin(t * 42 + phase) * 0.55;
  for (const [dx, base] of [[-1, -0.25], [2.5, 0.15]]) {
    ctx.save(); ctx.translate(dx, -5); ctx.rotate(-1.05 + base + flap);
    ctx.beginPath(); ctx.ellipse(0, -8, 5.2, 10, 0, 0, TAU);
    ctx.fillStyle = dead ? "rgba(150,160,175,.35)" : "rgba(205,232,255,.55)"; ctx.fill();
    ctx.strokeStyle = dead ? "rgba(150,160,175,.5)" : hexA(color, 0.9); ctx.lineWidth = 0.9; ctx.stroke();
    ctx.restore();
  }
  // abdomen
  ctx.save();
  ctx.beginPath(); ctx.ellipse(-5, 1, 11, 8, 0, 0, TAU);
  const g = ctx.createLinearGradient(0, -8, 0, 9); g.addColorStop(0, body); g.addColorStop(1, body2);
  ctx.fillStyle = g; ctx.fill(); ctx.clip();
  ctx.fillStyle = stripe;
  for (const sx of [-3, -9, -15]) ctx.fillRect(sx - 1.6, -9, 3.2, 20);
  ctx.restore();
  ctx.beginPath(); ctx.moveTo(-15.5, -1.5); ctx.lineTo(-20, 1); ctx.lineTo(-15.5, 3); ctx.fillStyle = stripe; ctx.fill();
  // thorax + head
  ctx.beginPath(); ctx.arc(5, 0, 5.6, 0, TAU); ctx.fillStyle = dead ? "#3d4450" : "#3a2a0a"; ctx.fill();
  ctx.beginPath(); ctx.arc(11.2, 0.5, 4.6, 0, TAU); ctx.fillStyle = stripe; ctx.fill();
  if (dead) {
    ctx.strokeStyle = "#cbd5e1"; ctx.lineWidth = 1.1; ctx.beginPath();
    ctx.moveTo(11, -1.6); ctx.lineTo(14, 1.4); ctx.moveTo(14, -1.6); ctx.lineTo(11, 1.4); ctx.stroke();
  } else {
    ctx.beginPath(); ctx.arc(12.8, -0.6, 1.5, 0, TAU); ctx.fillStyle = "#fff"; ctx.fill();
  }
  ctx.strokeStyle = stripe; ctx.lineWidth = 1; ctx.beginPath();
  ctx.moveTo(12, -3.5); ctx.quadraticCurveTo(14, -9, 17.5, -9.5); ctx.moveTo(10.5, -3.8); ctx.quadraticCurveTo(11.5, -10, 14, -11.5); ctx.stroke();
  ctx.restore();
  if (!dead) drawAccessory(ctx, x, y, role, color, scale, t);
  ctx.globalAlpha = 1;
}

function drawAccessory(ctx, x, y, role, color, s, t) {
  ctx.save(); ctx.translate(x, y - 14 * s);
  ctx.fillStyle = color; ctx.strokeStyle = color; ctx.lineWidth = 1.4;
  if (role === "queen") { // crown
    ctx.beginPath(); ctx.moveTo(-7, 2); ctx.lineTo(-7, -5); ctx.lineTo(-3.5, -1); ctx.lineTo(0, -7); ctx.lineTo(3.5, -1); ctx.lineTo(7, -5); ctx.lineTo(7, 2); ctx.closePath();
    ctx.shadowColor = color; ctx.shadowBlur = 10; ctx.fill();
  } else if (role === "guard") { // shield
    ctx.translate(15 * s, 12 * s); ctx.beginPath(); ctx.moveTo(0, -6); ctx.lineTo(5.5, -4); ctx.lineTo(4.5, 3); ctx.lineTo(0, 6.5); ctx.lineTo(-4.5, 3); ctx.lineTo(-5.5, -4); ctx.closePath();
    ctx.fillStyle = "#1b1030"; ctx.fill(); ctx.stroke(); ctx.beginPath(); ctx.moveTo(-2.2, 0); ctx.lineTo(-0.4, 2); ctx.lineTo(2.6, -2); ctx.stroke();
  } else if (role === "nurse") { // cross
    ctx.fillRect(-1.6, -6, 3.2, 9); ctx.fillRect(-4.5, -3.1, 9, 3.2);
  } else if (role === "sentinel") { // eye
    ctx.beginPath(); ctx.ellipse(0, -2, 6, 3.4, 0, 0, TAU); ctx.stroke(); ctx.beginPath(); ctx.arc(0, -2, 1.7, 0, TAU); ctx.fill();
  } else if (role === "scout") { // radar ping
    const r = 3 + ((t * 1.2) % 1) * 7; ctx.globalAlpha = 1 - ((t * 1.2) % 1); ctx.beginPath(); ctx.arc(0, -2, r, 0, TAU); ctx.stroke();
  } else if (role === "waggle") { // wave
    ctx.beginPath(); for (let i = -6; i <= 6; i++) { const yy = -2 + Math.sin(i * 0.9 + t * 8) * 2; i === -6 ? ctx.moveTo(i, yy) : ctx.lineTo(i, yy); } ctx.stroke();
  } else if (role === "gardener") { // leaf
    ctx.beginPath(); ctx.ellipse(0, -2, 3.2, 6, 0.6, 0, TAU); ctx.fill();
  }
  ctx.restore();
}

export function drawTicket(ctx, x, y, label, { color = "#f4e9c8", glow = null, scale = 1 } = {}) {
  ctx.save(); ctx.translate(x, y); ctx.scale(scale, scale); ctx.rotate(-0.06);
  if (glow) glowCircle(ctx, 0, 0, 26, glow, 0.45);
  ctx.font = FONT.mono(9, 700);
  const w = Math.max(30, ctx.measureText(label).width + 12);
  ctx.beginPath(); ctx.moveTo(-w / 2, -8); ctx.lineTo(w / 2, -8); ctx.lineTo(w / 2, -3); ctx.arc(w / 2, 0, 3, -Math.PI / 2, Math.PI / 2, true);
  ctx.lineTo(w / 2, 8); ctx.lineTo(-w / 2, 8); ctx.lineTo(-w / 2, 3); ctx.arc(-w / 2, 0, 3, Math.PI / 2, -Math.PI / 2, true); ctx.closePath();
  ctx.fillStyle = color; ctx.fill(); ctx.strokeStyle = "rgba(0,0,0,.35)"; ctx.lineWidth = 1; ctx.stroke();
  ctx.fillStyle = "#3b2f12"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(label, 0, 0.5);
  ctx.restore();
}

// ------------------------------------------------------------------ chaos fleet
export function drawMothership(ctx, x, y, t, { rage = 0, scale = 1 } = {}) {
  ctx.save(); ctx.translate(x, y + Math.sin(t * 1.3) * 4); ctx.scale(scale, scale);
  // rotating hex force field
  ctx.save(); ctx.rotate(t * 0.25);
  ctx.strokeStyle = hexA(C.red, 0.28 + rage * 0.4); ctx.lineWidth = 1.5; ctx.setLineDash([10, 8]);
  hexPath(ctx, 0, 0, 92); ctx.stroke(); ctx.setLineDash([]);
  ctx.restore();
  glowCircle(ctx, 0, 0, 120, C.red, 0.18 + rage * 0.3);
  // hull wings
  ctx.beginPath(); ctx.moveTo(-86, 18); ctx.lineTo(-40, -6); ctx.lineTo(40, -6); ctx.lineTo(86, 18); ctx.lineTo(54, 30); ctx.lineTo(-54, 30); ctx.closePath();
  let g = ctx.createLinearGradient(0, -6, 0, 30); g.addColorStop(0, "#3a1218"); g.addColorStop(1, "#14060a");
  ctx.fillStyle = g; ctx.fill(); ctx.strokeStyle = hexA(C.red, 0.7); ctx.lineWidth = 1.5; ctx.stroke();
  for (const sx of [-70, 70]) { glowCircle(ctx, sx, 22, 12, C.red2 || "#ff7a59", 0.8); }
  // ears
  for (const sx of [-44, 44]) {
    ctx.beginPath(); ctx.arc(sx, -20, 17, 0, TAU); ctx.fillStyle = "#2a0d12"; ctx.fill(); ctx.strokeStyle = hexA(C.red, 0.8); ctx.stroke();
    ctx.beginPath(); ctx.arc(sx, -20, 9, 0, TAU); ctx.fillStyle = "#4a1a20"; ctx.fill();
  }
  // head
  ctx.beginPath(); ctx.arc(0, -16, 38, 0, TAU);
  g = ctx.createRadialGradient(-10, -30, 4, 0, -16, 40); g.addColorStop(0, "#4d1720"); g.addColorStop(1, "#1a070b");
  ctx.fillStyle = g; ctx.fill(); ctx.strokeStyle = hexA(C.red, 0.9); ctx.lineWidth = 2; ctx.stroke();
  // face mask
  ctx.beginPath(); ctx.ellipse(0, -6, 27, 22, 0, 0, TAU); ctx.fillStyle = "#2c0f14"; ctx.fill();
  // angry eyes (slanted), flare on rage
  const eye = 0.75 + 0.25 * Math.sin(t * 6) + rage;
  for (const s of [-1, 1]) {
    ctx.save(); ctx.translate(s * 13, -20); ctx.rotate(s * 0.35);
    ctx.shadowColor = C.red; ctx.shadowBlur = 16 + rage * 20;
    ctx.beginPath(); ctx.moveTo(-9, -1); ctx.lineTo(9, -4); ctx.lineTo(6, 4); ctx.lineTo(-7, 3); ctx.closePath();
    ctx.fillStyle = hexA("#ff2a3d", Math.min(1, eye)); ctx.fill();
    ctx.restore();
  }
  // jagged grin
  ctx.beginPath(); ctx.moveTo(-18, 2);
  for (let i = 0; i <= 8; i++) ctx.lineTo(-18 + i * 4.5, 2 + (i % 2 ? 7 : 0) + Math.sin(t * 3) * 0.5);
  ctx.lineTo(18, 2); ctx.quadraticCurveTo(0, 16, -18, 2); ctx.closePath();
  ctx.fillStyle = "#0a0204"; ctx.fill(); ctx.strokeStyle = "#ffb3ba"; ctx.lineWidth = 1.2; ctx.stroke();
  ctx.restore();
}

export function drawDrone(ctx, x, y, t, i, { charge = 0 } = {}) {
  const bob = Math.sin(t * 2 + i * 1.7) * 5;
  ctx.save(); ctx.translate(x, y + bob); ctx.rotate(Math.sin(t * 1.4 + i) * 0.08);
  glowCircle(ctx, 0, 0, 24 + charge * 18, C.red, 0.18 + charge * 0.5);
  ctx.strokeStyle = "#7a2a33"; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(-14, -6); ctx.lineTo(14, 6); ctx.moveTo(14, -6); ctx.lineTo(-14, 6); ctx.stroke();
  for (const [rx, ry] of [[-14, -6], [14, -6], [-14, 6], [14, 6]]) {
    ctx.beginPath(); ctx.ellipse(rx, ry - 2, 8, 2 + Math.abs(Math.sin(t * 50 + rx)) * 1.2, 0, 0, TAU);
    ctx.fillStyle = "rgba(255,120,130,.22)"; ctx.fill();
  }
  ctx.beginPath(); ctx.moveTo(-9, 0); ctx.lineTo(0, -7); ctx.lineTo(9, 0); ctx.lineTo(0, 6); ctx.closePath();
  ctx.fillStyle = "#24090d"; ctx.fill(); ctx.strokeStyle = hexA(C.red, 0.9); ctx.lineWidth = 1.2; ctx.stroke();
  ctx.beginPath(); ctx.arc(0, -0.5, 2.6 + charge * 1.5, 0, TAU); ctx.fillStyle = charge > 0.1 ? "#fff" : "#ff3b4e";
  ctx.shadowColor = C.red; ctx.shadowBlur = 10; ctx.fill();
  ctx.restore();
  return y + bob;
}

// ------------------------------------------------------------------ lightning
export function boltPoints(x1, y1, x2, y2, seed, detail = 6) {
  const r = rng(seed);
  let pts = [[x1, y1], [x2, y2]];
  let disp = Math.hypot(x2 - x1, y2 - y1) * 0.22;
  for (let k = 0; k < detail; k++) {
    const next = [pts[0]];
    for (let i = 0; i < pts.length - 1; i++) {
      const [ax, ay] = pts[i], [bx, by] = pts[i + 1];
      const mx = (ax + bx) / 2, my = (ay + by) / 2;
      const nx = -(by - ay), ny = bx - ax, nl = Math.hypot(nx, ny) || 1;
      const d = (r() - 0.5) * disp;
      next.push([mx + (nx / nl) * d, my + (ny / nl) * d], pts[i + 1]);
    }
    pts = next; disp *= 0.55;
  }
  return pts;
}
export function strokeBolt(ctx, pts, color, a) {
  ctx.save(); ctx.lineJoin = "round"; ctx.lineCap = "round";
  for (const [w, al, col] of [[16, 0.22, color], [7, 0.7, color], [2.6, 1, "#ffffff"]]) {
    ctx.beginPath(); pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.strokeStyle = col === "#ffffff" ? `rgba(255,255,255,${a})` : hexA(col, al * a); ctx.lineWidth = w;
    ctx.shadowColor = color; ctx.shadowBlur = w > 5 ? 30 : 6; ctx.stroke();
  }
  ctx.restore();
}

// ------------------------------------------------------------------ buildings
function face(ctx, x, y, w, h, depth, health, glowA) {
  // top face (parallelogram) + front face + side face, outlined in the health color
  const hc = HEALTH[health] || C.muted;
  ctx.save();
  ctx.shadowColor = hc; ctx.shadowBlur = 22 * glowA;
  ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + depth, y - depth * 0.6); ctx.lineTo(x + w + depth, y - depth * 0.6); ctx.lineTo(x + w, y); ctx.closePath();
  ctx.fillStyle = "#1d2e44"; ctx.fill();
  ctx.beginPath(); ctx.moveTo(x + w, y); ctx.lineTo(x + w + depth, y - depth * 0.6); ctx.lineTo(x + w + depth, y + h - depth * 0.6); ctx.lineTo(x + w, y + h); ctx.closePath();
  ctx.fillStyle = "#0b1422"; ctx.fill();
  const g = ctx.createLinearGradient(x, y, x, y + h); g.addColorStop(0, "#18273b"); g.addColorStop(1, "#0d1624");
  ctx.fillStyle = g; ctx.fillRect(x, y, w, h);
  ctx.restore();
  ctx.strokeStyle = hexA(hc, 0.85); ctx.lineWidth = 1.6;
  ctx.beginPath(); ctx.moveTo(x, y + h); ctx.lineTo(x, y); ctx.lineTo(x + depth, y - depth * 0.6); ctx.lineTo(x + w + depth, y - depth * 0.6);
  ctx.lineTo(x + w + depth, y + h - depth * 0.6); ctx.lineTo(x + w, y + h); ctx.closePath(); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + w, y); ctx.lineTo(x + w + depth, y - depth * 0.6); ctx.moveTo(x + w, y); ctx.lineTo(x + w, y + h); ctx.strokeStyle = hexA(hc, 0.45); ctx.stroke();
}

function led(ctx, x, y, color, on, r = 2) {
  ctx.beginPath(); ctx.arc(x, y, r, 0, TAU);
  ctx.fillStyle = on ? color : hexA(color, 0.18); ctx.shadowColor = color; ctx.shadowBlur = on ? 6 : 0; ctx.fill(); ctx.shadowBlur = 0;
}

export function drawMainframe(ctx, b, t, s) {
  const { x, y, w, h } = b; const health = s.status;
  face(ctx, x, y, w, h, 16, health, s.glow);
  const abend = s.batch === "ABENDED" || s.batch === "HELD";
  // tape reels
  for (let i = 0; i < 2; i++) {
    const cx = x + 30 + i * 46, cy = y + 46;
    ctx.beginPath(); ctx.arc(cx, cy, 19, 0, TAU); ctx.fillStyle = "#0a111c"; ctx.fill(); ctx.strokeStyle = "#2e4562"; ctx.lineWidth = 2; ctx.stroke();
    ctx.beginPath(); ctx.arc(cx, cy, 11, 0, TAU); ctx.fillStyle = "#3a2a14"; ctx.fill();
    const rot = abend ? 0.4 : t * (2.2 + i * 0.6);
    ctx.strokeStyle = "#8aa0bd"; ctx.lineWidth = 2;
    for (let k = 0; k < 3; k++) { const a = rot + (k * TAU) / 3; ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.cos(a) * 15, cy + Math.sin(a) * 15); ctx.stroke(); }
    ctx.beginPath(); ctx.arc(cx, cy, 3, 0, TAU); ctx.fillStyle = "#cbd5e1"; ctx.fill();
  }
  // LED matrix
  const r = rng(7);
  for (let row = 0; row < 5; row++) for (let col = 0; col < 6; col++) {
    const lx = x + 112 + col * 10, ly = y + 22 + row * 10;
    const seed = r();
    const on = Math.sin(t * (2 + seed * 5) + seed * 40) > (abend ? 0.6 : 0.1);
    led(ctx, lx, ly, abend && row < 2 ? C.red : row === 4 ? C.amber : C.green, on, 2.1);
  }
  // console strip + vents
  ctx.fillStyle = "#0a111c"; ctx.fillRect(x + 12, y + 80, w - 24, 22);
  ctx.font = FONT.mono(10, 700); ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillStyle = abend ? (Math.sin(t * 8) > 0 ? C.red : "#7a1b26") : "#5ef5a8";
  ctx.fillText(abend ? "NIGHTBAT ABEND S0C7" : "NIGHTBAT  OK   CICS UP", x + 18, y + 91);
  ctx.strokeStyle = "#1f3047"; ctx.lineWidth = 2;
  for (let i = 0; i < 9; i++) { ctx.beginPath(); ctx.moveTo(x + 14 + i * 18, y + h - 22); ctx.lineTo(x + 26 + i * 18, y + h - 22); ctx.stroke(); ctx.beginPath(); ctx.moveTo(x + 14 + i * 18, y + h - 14); ctx.lineTo(x + 26 + i * 18, y + h - 14); ctx.stroke(); }
}

export function drawUnix(ctx, b, t, s) {
  const { x, y, w, h } = b; const health = s.status;
  face(ctx, x, y, w, h, 14, health, s.glow);
  const units = 8, uh = (h - 20) / units;
  const disk = s.disk ?? 40;
  for (let i = 0; i < units; i++) {
    const uy = y + 10 + i * uh;
    ctx.fillStyle = "#0a111c"; ctx.fillRect(x + 8, uy + 2, w - 16, uh - 4);
    ctx.strokeStyle = "#22344c"; ctx.lineWidth = 1; ctx.strokeRect(x + 8.5, uy + 2.5, w - 17, uh - 5);
    for (let k = 0; k < 4; k++) { ctx.fillStyle = "#1c2b40"; ctx.fillRect(x + 50 + k * 14, uy + uh / 2 - 2, 10, 4); }
    const act = Math.sin(t * (6 + i) + i * 3.3) > (health === "up" ? -0.2 : 0.5);
    led(ctx, x + 18, uy + uh / 2, health === "up" ? C.green : health === "degraded" ? C.amber : C.red, act, 2.2);
    led(ctx, x + 28, uy + uh / 2, C.blue, Math.sin(t * 11 + i) > 0.3, 1.6);
  }
  // disk gauge on the side
  const gh = h - 20, gx = x + w - 6;
  ctx.fillStyle = "#0a111c"; ctx.fillRect(gx - 2, y + 10, 4, gh);
  ctx.fillStyle = disk >= 90 ? C.red : disk >= 80 ? C.amber : C.green;
  ctx.fillRect(gx - 2, y + 10 + gh * (1 - disk / 100), 4, gh * (disk / 100));
}

export function drawPortal(ctx, b, t, s) {
  const { x, y, w, h } = b; const health = s.status;
  face(ctx, x, y, w, h, 14, health, s.glow);
  // window grid
  for (let r = 0; r < 3; r++) for (let c = 0; c < 5; c++) {
    const on = (hash(`${r}${c}`) % 3) !== 0;
    ctx.fillStyle = on ? "rgba(76,201,240,.22)" : "rgba(76,201,240,.06)";
    ctx.fillRect(x + 12 + c * 26, y + 12 + r * 12, 18, 7);
  }
  // the customer-facing screen
  const sx = x + 12, sy = y + 54, sw = w - 24, sh = h - 76;
  ctx.fillStyle = "#081420"; ctx.fillRect(sx, sy, sw, sh);
  ctx.fillStyle = "#13243a"; ctx.fillRect(sx, sy, sw, 13);
  for (let i = 0; i < 3; i++) led(ctx, sx + 7 + i * 7, sy + 6.5, [C.red, C.amber, C.green][i], true, 2);
  ctx.font = FONT.mono(8, 500); ctx.fillStyle = C.muted; ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillText("portal.bank", sx + 30, sy + 7);
  ctx.font = FONT.mono(9, 600);
  const stale = s.stale;
  const rows = [["Checking", "12,408.11"], ["Savings", "88,120.54"], ["Card", "-1,203.90"]];
  rows.forEach(([k, v], i) => {
    const ry = sy + 26 + i * 16;
    ctx.fillStyle = C.ink2; ctx.textAlign = "left"; ctx.fillText(k, sx + 8, ry);
    ctx.textAlign = "right"; ctx.fillStyle = stale ? (Math.sin(t * 3 + i) > 0 ? "#ffcf70" : "#9b7a35") : "#bff7dc"; ctx.fillText("$" + v, sx + sw - 8, ry);
  });
  ctx.textAlign = "left"; ctx.font = FONT.mono(8, 500); ctx.fillStyle = stale ? C.amber : C.muted;
  ctx.fillText(stale ? "as of: yesterday 02:00" : "as of: now", sx + 8, sy + sh - 9);
  if (stale) {
    ctx.save(); ctx.translate(sx + sw / 2, sy + sh / 2 + 4); ctx.rotate(-0.22);
    ctx.font = FONT.display(17, 700); ctx.textAlign = "center"; ctx.lineWidth = 2;
    ctx.strokeStyle = hexA(C.amber, 0.9); ctx.fillStyle = hexA(C.amber, 0.95);
    ctx.strokeRect(-40, -12, 80, 24); ctx.fillText("STALE", 0, 1); ctx.restore();
  }
  ctx.font = FONT.mono(8, 700); ctx.textAlign = "right"; ctx.fillStyle = "#5ef5a8"; ctx.fillText("HTTP 200", sx + sw - 4, sy + 7);
}

export function drawCracks(ctx, b, vulns, t) {
  for (const v of vulns) {
    if (!v.open && !v.exploited) continue;
    const r = rng(hash(v.id || v.kind || "v"));
    let px = b.x + 14 + r() * (b.w - 28), py = b.y + (r() < 0.5 ? 0 : b.h * 0.3 * r());
    const pts = [[px, py]];
    for (let i = 0; i < 6; i++) { px += (r() - 0.5) * 18; py += 7 + r() * 9; pts.push([px, py]); }
    const hot = v.exploited;
    ctx.save(); ctx.lineJoin = "round";
    if (hot) { ctx.shadowColor = C.red; ctx.shadowBlur = 10 + Math.sin(t * 6) * 6; }
    ctx.strokeStyle = hot ? C.red : "rgba(255,190,120,.75)"; ctx.lineWidth = hot ? 2.2 : 1.4;
    ctx.beginPath(); pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y))); ctx.stroke();
    ctx.beginPath(); const [bx, by] = pts[3]; ctx.moveTo(bx, by); ctx.lineTo(bx + 9 * (r() - 0.3), by + 8); ctx.stroke();
    ctx.restore();
  }
}

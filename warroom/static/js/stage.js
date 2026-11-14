// The stage: one Canvas2D scene, one requestAnimationFrame loop.
//   left = Chaos fleet (Red) · center = legacy estate on the dependency chain · right = the Hive (Blue)
//   bottom = the MongoDB spine (one honeycomb cell per collection).
// Everything drawn is derived from the store (MongoDB change streams); effects are short-lived
// decorations triggered by change events (see beats.js).
import {
  C, HEALTH, ROLE, FONT, TAU, hexA, hash, roundRect, hexPath, glowCircle, pill, ellipsize, wrap,
  drawBee, drawTicket, drawMothership, drawDrone, boltPoints, strokeBolt,
  drawMainframe, drawUnix, drawPortal, drawCracks,
} from "./draw.js";
import { state, currentIncident, currentRun, ts, prettyTime } from "./store.js";

export const VW = 1480, VH = 1000;
const GROUND = 596, DEAD_Y = 746, PIPE_Y = GROUND - 44;
const ROLE_ORDER = ["queen", "scout", "worker", "guard", "waggle", "sentinel", "nurse", "gardener"];
const DEFAULT_ROSTER = [
  ["queen-1", "queen"], ["scout-1", "scout"], ["scout-2", "scout"], ["worker-mainframe-1", "worker"],
  ["worker-unix-1", "worker"], ["worker-web-1", "worker"], ["guard-1", "guard"], ["waggle-1", "waggle"],
  ["sentinel-1", "sentinel"], ["nurse-1", "nurse"], ["nurse-2", "nurse"], ["gardener-1", "gardener"],
];

const BUILDINGS = {
  mainframe: { id: "mainframe", label: "MAINFRAME", sub: "z/OS · NIGHTBAT batch", cx: 410, w: 176, h: 148, draw: drawMainframe, depth: 16 },
  unix: { id: "unix", label: "UNIX MIDDLEWARE", sub: "AIX · nightly file transfer", cx: 652, w: 118, h: 208, draw: drawUnix, depth: 14 },
  portal: { id: "portal", label: "WEB PORTAL", sub: "customer balances", cx: 892, w: 150, h: 190, draw: drawPortal, depth: 14 },
};
// buildings are drawn by fixed-pixel art routines at their raw size, then scaled up about their base (BSCALE);
// b.x/y/w/h/depth are the on-stage (scaled) box every other routine uses.
const BSCALE = 1.15;
for (const b of Object.values(BUILDINGS)) {
  b.raw = { x: b.cx - b.w / 2, y: GROUND - b.h, w: b.w, h: b.h, depth: b.depth, cx: b.cx };
  b.w *= BSCALE; b.h *= BSCALE; b.depth *= BSCALE; b.x = b.cx - b.w / 2; b.y = GROUND - b.h;
}
const LINKS = [["mainframe", "unix"], ["unix", "portal"]];
/** x-range of the pipe between two adjacent systems, oriented from → to (works both directions). */
function pipeEnds(from, to) {
  const link = LINKS.find(([u, d]) => (u === from && d === to) || (u === to && d === from));
  if (!link) return null;
  const up = BUILDINGS[link[0]], down = BUILDINGS[link[1]];
  const x1 = up.x + up.w + up.depth * 0.6, x2 = down.x - 2;
  return from === link[0] ? [x1, x2] : [x2, x1];
}

const SHIP = { x: 148, y: 205 };
const DRONES = [[-98, -78], [98, -78], [-120, 34], [120, 34], [-72, 128], [72, 128]];
const HIVE = { x: 1250, y: 292, r: 40 };
const BEE_SCALE = 1.18;
const BENCH = { x: 1150, y: 672 };
const SHOP = { x: 1388, y: 640 };

// MongoDB spine: one cell per collection, with the feature it demonstrates
export const SPINE = [
  ["world", "systems", "updateLookup"], ["world", "chaos_events", "Red only"], ["world", "runs", "run → round"],
  ["world", "system_logs", "TTL 24h"], ["world", "op_receipts", "idempotent"],
  ["swarm", "incidents", "$jsonSchema · partial unique"], ["swarm", "tasks", "findOneAndUpdate lease"],
  ["swarm", "agents", "heartbeats"], ["swarm", "ledger", "transactions"], ["swarm", "thoughts", "TTL 24h"],
  ["swarm", "messages", "waggle bus"], ["swarm", "playbooks", "$vectorSearch"], ["swarm", "approvals", "conditional update"],
  ["swarm", "upgrade_requests", "Gardener queue"], ["swarm", "checkpoints", "LangGraph saver"],
];
const SPINE_TOP = 800, CELL_R = 48, CELL_X0 = 352, CELL_DX = 73;
const DB_COLOR = { world: "#ff6b6b", swarm: "#4cc9f0" };

function hiveCells() {
  const cells = [[0, 0]];
  const dirs = [[1, 0], [1, -1], [0, -1], [-1, 0], [-1, 1], [0, 1]];
  for (let ring = 1; ring <= 2; ring++) {
    let q = -ring, r = ring; // start at direction 4 * ring
    for (let side = 0; side < 6; side++) for (let k = 0; k < ring; k++) { cells.push([q, r]); q += dirs[side][0]; r += dirs[side][1]; }
  }
  return cells.map(([q, r]) => ({ x: HIVE.x + HIVE.r * 1.5 * q, y: HIVE.y + HIVE.r * Math.sqrt(3) * (r + q / 2), q, r }));
}
const CELLS = hiveCells();
// preferred hive cells (ring 1 first) so the colony looks deliberate, not random
const CELL_ORDER = [0, 4, 1, 5, 2, 6, 3, 13, 7, 15, 9, 17, 11, 8, 10, 12, 14, 16, 18];

const ease = (p) => (p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const shortTask = (t) => String(t || "").replace(/^INC-0*/, "#");
export function shortAgent(id) {
  return String(id).replace("worker-mainframe", "w·mainframe").replace("worker-unix", "w·unix").replace("worker-web", "w·web");
}
function roleOf(a) { return a.role || "worker"; }

export class Stage {
  constructor(canvas, { onInspect } = {}) {
    this.cv = canvas; this.ctx = canvas.getContext("2d");
    this.onInspect = onInspect || (() => {});
    this.bees = new Map(); this.slots = new Map(); this.tickets = new Map();
    this.killed = new Map(); this.stamps = []; this.banners = []; this.particles = []; this.bolts = []; this.pulses = []; this.beams = []; this.rings = []; this.floaters = []; this.sparks = [];
    this.cellGlow = {}; this.cellShown = {}; this.droneCharge = DRONES.map(() => 0);
    this.shipRage = 0; this.hunt = null; this.shake = {}; this.lastVersion = {}; this.blamePulseT = {};
    this.hits = []; this.hover = null; this.t = 0; this.last = performance.now();
    this.resize();
    addEventListener("resize", () => this.resize());
    canvas.addEventListener("mousemove", (e) => this.onMove(e));
    canvas.addEventListener("mouseleave", () => { this.hover = null; this.tooltip?.classList.remove("on"); });
    canvas.addEventListener("click", (e) => { const h = this.pick(e); if (h) this.onInspect(h); });
    this.tooltip = document.getElementById("tooltip");
    requestAnimationFrame((t) => this.frame(t));
  }

  // ---------------------------------------------------------------- sizing / input
  resize() {
    const r = this.cv.getBoundingClientRect(), dpr = Math.min(devicePixelRatio || 1, 2);
    this.cv.width = Math.round(r.width * dpr); this.cv.height = Math.round(r.height * dpr);
    this.dpr = dpr; this.cssW = r.width; this.cssH = r.height;
    this.scale = Math.min(r.width / VW, r.height / VH);
    this.ox = (r.width - VW * this.scale) / 2; this.oy = (r.height - VH * this.scale) / 2;
    this.bg = null;
  }
  toVirtual(e) {
    const r = this.cv.getBoundingClientRect();
    return [(e.clientX - r.left - this.ox) / this.scale, (e.clientY - r.top - this.oy) / this.scale];
  }
  pick(e) {
    const [x, y] = this.toVirtual(e);
    for (let i = this.hits.length - 1; i >= 0; i--) {
      const h = this.hits[i];
      if (h.r ? Math.hypot(x - h.x, y - h.y) <= h.r : x >= h.x && x <= h.x + h.w && y >= h.y && y <= h.y + h.h) return h;
    }
    return null;
  }
  onMove(e) {
    const h = this.pick(e); this.hover = h;
    this.cv.style.cursor = h ? "pointer" : "default";
    if (!this.tooltip) return;
    if (h && h.tip) {
      const r = this.cv.getBoundingClientRect();
      this.tooltip.textContent = h.tip; this.tooltip.classList.add("on");
      this.tooltip.style.left = `${Math.min(e.clientX - r.left + 14, r.width - 260)}px`;
      this.tooltip.style.top = `${e.clientY - r.top + 16}px`;
    } else this.tooltip.classList.remove("on");
  }
  hit(o) { this.hits.push(o); }

  // ---------------------------------------------------------------- positions (used by effects)
  sysDoc(id) { return state.systems.get(id) || null; }
  posOfSystem(id) { const b = BUILDINGS[id]; return b ? { x: b.cx + b.depth / 2, y: b.y - 4 } : null; }
  centerOfSystem(id) { const b = BUILDINGS[id]; return b ? { x: b.cx, y: b.y + b.h / 2 } : null; }
  posOfAgent(id) { const b = this.bees.get(id); return b ? { x: b.x, y: b.y } : null; }
  cellPos(coll) {
    const i = SPINE.findIndex((s) => s[1] === coll);
    if (i < 0) return null;
    return { x: CELL_X0 + i * CELL_DX, y: SPINE_TOP + 88 + (i % 2 ? CELL_R * 0.87 : 0) };
  }
  shipPos() { return { x: SHIP.x, y: SHIP.y }; }
  hivePos() { return { x: HIVE.x, y: HIVE.y }; }
  workshopPos() { return { x: SHOP.x, y: SHOP.y - 30 }; }

  // ---------------------------------------------------------------- effects API
  strike(ev) {
    const i = hash(String(ev._id || ev.fault)) % DRONES.length;
    this.droneCharge[i] = 1; this.shipRage = 1;
    const target = ev.fault === "agent_kill" ? null : this.posOfSystem(ev.target);
    if (!target) { this.hunt = { t0: this.t, until: this.t + 30, ev }; return; }
    const lie = ev.fault === "lying_success" || ev.fault === "slow_apply" || ev.fault === "false_log";
    this.addBolt(i, target, lie ? "#ff4fd8" : C.red);
    this.shake[ev.target] = this.t;
    this.float(target.x, BUILDINGS[ev.target].y + BUILDINGS[ev.target].h * 0.62, ev.fault + (ev.params?.count ? ` ×${ev.params.count}` : ""), lie ? "#ff9cf0" : "#ffb3ba", 17);
  }
  strikeAgent(agentId, label = "SIGKILL") {
    const p = this.posOfAgent(agentId); this.hunt = null;
    if (!p) return;
    // the process is gone right now; the Undertaker only marks the doc dead after missed heartbeats (~10 s)
    this.killed.set(agentId, { t: this.t + 0.35, seen: state.agents.get(agentId)?.last_seen ?? null });
    const i = hash(agentId) % DRONES.length; this.droneCharge[i] = 1; this.shipRage = 1;
    this.addBolt(i, p, C.red, () => this.posOfAgent(agentId));
    this.float(p.x, p.y - 30, label, "#ffb3ba", 15);
  }
  addBolt(droneIdx, to, color, toFn = null) {
    const [dx, dy] = DRONES[droneIdx];
    this.bolts.push({ from: { x: SHIP.x + dx, y: SHIP.y + dy }, to, toFn, color, t0: this.t, dur: 1.4, seed: hash(String(this.t)) });
    this.flash = { t0: this.t, color };
    this.rings.push({ x: to.x, y: to.y, color, t0: this.t + 0.12, dur: 0.9, r: 70 });
    for (let k = 0; k < 26; k++) {
      const a = Math.random() * TAU, v = 80 + Math.random() * 260;
      this.sparks.push({ x: to.x, y: to.y, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 60, t0: this.t + 0.1, dur: 0.5 + Math.random() * 0.6, color });
    }
  }
  pipePulse(from, to, color, label) {
    this.pulses.push({ from, to, color, t0: this.t, dur: 1.1, label });
  }
  beam(fromFn, toFn, color, label, dur = 3) { this.beams.push({ fromFn, toFn, color, label, t0: this.t, dur }); }
  burst(sysId, color, text) {
    const p = this.posOfSystem(sysId); if (!p) return;
    this.rings.push({ x: p.x, y: p.y + 40, color, t0: this.t, dur: 1.1, r: 110 });
    // on the building face, so it never collides with the symptom bubble above the roof
    if (text) this.float(p.x, BUILDINGS[sysId].y + BUILDINGS[sysId].h * 0.55, text, color, 17);
  }
  burstAt(p, color, text) {
    if (!p) return;
    this.rings.push({ x: p.x, y: p.y, color, t0: this.t, dur: 0.9, r: 60 });
    if (text) this.float(p.x, p.y - 34, text, color, 13);
  }
  float(x, y, text, color, size = 15, dur = 2.6) {
    // stack instead of overlapping: nudge up past any young floater near the same spot
    for (let guard = 0; guard < 6; guard++) {
      const clash = this.floaters.find((f) => this.t - f.t0 < f.dur * 0.7 && Math.abs(f.x - x) < 150 && Math.abs((f.y - (this.t - f.t0) / f.dur * 34) - y) < size + 6);
      if (!clash) break;
      y -= size + 8;
    }
    this.floaters.push({ x, y, text, color, size, t0: this.t, dur });
  }
  /** A big rubber stamp slammed onto a building (ACK ≠ APPLIED, VERIFIED, REJECTED …). */
  stamp(sysId, text, color, { sub = "", dur = 3.6, rot = -0.14 } = {}) {
    const b = BUILDINGS[sysId]; if (!b) return;
    // queue behind a stamp that is still young on the same building, so each one is readable (≥ 2 s)
    const prev = this.stamps.filter((s) => s.sys === sysId).sort((p, q) => q.t0 - p.t0)[0];
    const t0 = prev ? Math.max(this.t, prev.t0 + 2) : this.t;
    if (prev) prev.dur = Math.min(prev.dur, t0 - prev.t0 + 0.15);
    this.stamps.push({ sys: sysId, x: b.cx + b.depth / 2, y: b.y + b.h * 0.46, text, sub, color, rot, t0, dur });
  }
  /** A centred banner across the estate (resolved / never again). */
  banner(text, sub, color, dur = 4) { this.banners = [{ text, sub, color, t0: this.t, dur }]; }
  writeParticle(coll, from, color) {
    const to = this.cellPos(coll); if (!to) return;
    if (!from || this.particles.length > 140) { this.pulseCell(coll); return; }
    const db = SPINE.find((s) => s[1] === coll)[0];
    this.particles.push({ from: { ...from }, to, coll, color: color || DB_COLOR[db], t0: this.t, dur: 0.75 + Math.random() * 0.35, bend: (Math.random() - 0.5) * 160 });
  }
  pulseCell(coll, amt = 1) { this.cellGlow[coll] = Math.min(1.4, (this.cellGlow[coll] || 0) + amt); }

  // ---------------------------------------------------------------- frame loop
  frame(now) {
    const dt = Math.min(0.05, (now - this.last) / 1000); this.last = now; this.t += dt;
    try { this.update(dt); this.render(); } catch (e) { console.error(e); }
    requestAnimationFrame((t) => this.frame(t));
  }

  agentsList() {
    if (state.agents.size) return [...state.agents.values()];
    return DEFAULT_ROSTER.map(([id, role]) => ({ _id: id, role, status: role === "nurse" ? "standby" : "idle", ghost: true }));
  }

  slotFor(id, role) {
    if (this.slots.has(id)) return this.slots.get(id);
    const used = new Set(this.slots.values());
    const order = id === "queen-1" || (role === "queen" && !used.has(0)) ? CELL_ORDER : CELL_ORDER.slice(1);
    const s = order.find((c) => !used.has(c)) ?? (this.slots.size % CELLS.length);
    this.slots.set(id, s); return s;
  }

  homeOf(a, agents) {
    const tg = a.target;
    if (BUILDINGS[tg]) {
      const here = agents.filter((o) => o.status !== "dead" && o.target === tg)
        .sort((p, q) => ROLE_ORDER.indexOf(roleOf(p)) - ROLE_ORDER.indexOf(roleOf(q)) || String(p._id).localeCompare(q._id));
      const i = Math.max(0, here.findIndex((o) => o._id === a._id));
      const b = BUILDINGS[tg];
      const offs = [[-46, -150], [46, -150], [-62, -212], [62, -212], [0, -270], [-104, -186], [104, -186]];
      const [ox, oy] = offs[i % offs.length];
      return { x: b.cx + ox, y: b.y + oy, facing: ox > 0 ? -1 : 1 };
    }
    const onBench = (o) => roleOf(o) === "nurse" && (o.target === "bench" || (!o.target && o.status !== "working"));
    if (onBench(a)) {
      const bench = agents.filter((o) => o.status !== "dead" && onBench(o))
        .sort((p, q) => String(p._id).localeCompare(q._id));
      const i = Math.max(0, bench.findIndex((o) => o._id === a._id));
      const gap = bench.length > 3 ? 46 : 66; // centred on the bench, spaced so the name tags don't collide
      return { x: BENCH.x + 3 + (i - (bench.length - 1) / 2) * gap, y: BENCH.y - 28, facing: -1 };
    }
    if (tg === "workshop" || (!tg && roleOf(a) === "gardener")) return { x: SHOP.x - 6, y: SHOP.y - 64, facing: -1 };
    const c = CELLS[this.slotFor(a._id, roleOf(a))];
    return { x: c.x, y: c.y + 2, facing: -1 };
  }

  update(dt) {
    const agents = this.agentsList();
    const seen = new Set();
    for (const a of agents) {
      seen.add(a._id);
      let b = this.bees.get(a._id);
      const home = this.homeOf(a, agents);
      if (!b) {
        b = { id: a._id, x: home.x, y: home.y, from: { ...home }, to: { ...home }, t0: -9, dur: 0.01, facing: -1, phase: Math.random() * 10, dead: false, vy: 0, waypoints: [] };
        if (a.status === "dead") { b.dead = true; b.y = DEAD_Y; }
        this.bees.set(a._id, b);
      }
      b.doc = a;
      const k = this.killed.get(a._id);
      if (k && a.status !== "dead" && (a.last_seen ?? null) !== k.seen) this.killed.delete(a._id); // respawned
      const down = a.status === "dead" || (k && this.t >= k.t && this.killed.has(a._id));
      b.sigkilled = down && a.status !== "dead";
      if (down) {
        if (!b.dead) {
          b.dead = true; b.vy = -120; b.waypoints = [];
          if (a.task && !this.tickets.has(a.task)) this.tickets.set(a.task, { task: a.task, x: b.x + 48, y: DEAD_Y + 8, holder: null, from: a._id, dropT: this.t });
        }
        b.vy += 700 * dt; b.y = Math.min(DEAD_Y, b.y + b.vy * dt);
        if (b.y >= DEAD_Y) b.vy = 0;
        continue;
      }
      if (b.dead) { b.dead = false; b.from = { x: b.x, y: b.y }; b.to = { x: b.x, y: b.y }; }
      // a live bee that now owns a dropped ticket: fly to it first (Nurse takeover)
      const tk = a.task && this.tickets.get(a.task);
      if (tk && !tk.holder && tk.from !== a._id && !b.waypoints.some((w) => w.ticket === tk.task)) {
        b.waypoints.push({ x: tk.x - 4, y: tk.y - 22, ticket: tk.task });
      }
      const dest = b.waypoints[0] || home;
      if (Math.hypot(dest.x - b.to.x, dest.y - b.to.y) > 2) {
        const dist = Math.hypot(dest.x - b.x, dest.y - b.y);
        b.from = { x: b.x, y: b.y }; b.to = { x: dest.x, y: dest.y }; b.t0 = this.t; b.dur = clamp(dist / 340, 0.45, 1.9);
        b.ctrl = { x: (b.x + dest.x) / 2, y: Math.min(b.y, dest.y) - Math.min(130, dist * 0.32) };
        if (Math.abs(dest.x - b.x) > 4) b.facing = dest.x > b.x ? 1 : -1;
      }
      const p = clamp((this.t - b.t0) / b.dur, 0, 1), e = ease(p);
      if (p < 1 && b.ctrl) {
        const u = 1 - e;
        b.x = u * u * b.from.x + 2 * u * e * b.ctrl.x + e * e * b.to.x;
        b.y = u * u * b.from.y + 2 * u * e * b.ctrl.y + e * e * b.to.y;
      } else {
        b.x = b.to.x; b.y = b.to.y;
        if (b.waypoints.length) {
          const w = b.waypoints.shift();
          if (w.ticket) { const t = this.tickets.get(w.ticket); if (t) { t.holder = a._id; t.pickT = this.t; this.burstAt({ x: b.x, y: b.y }, C.pink); } }
        } else if (home.facing && p >= 1) b.facing = home.facing;
      }
      b.moving = p < 1;
    }
    for (const id of [...this.bees.keys()]) if (!seen.has(id)) this.bees.delete(id);
    // tickets: forget once their task is done or the holder lets go
    for (const [task, tk] of this.tickets) {
      const tdoc = state.tasks.get(task);
      if ((state.agents.size && !state.agents.has(tk.from)) || (tdoc && (tdoc.status === "done" || tdoc.status === "failed")) || (tk.holder && state.agents.get(tk.holder)?.task !== task && this.t - (tk.pickT || 0) > 3)) this.tickets.delete(task);
    }
    // version bumps (Gardener deploys) → celebration
    for (const id of Object.keys(BUILDINGS)) {
      const v = this.sysDoc(id)?.version;
      if (v != null) {
        if (this.lastVersion[id] != null && v > this.lastVersion[id]) {
          this.burst(id, C.green);
          this.stamp(id, `UPGRADED v${this.lastVersion[id]} → v${v}`, C.green, { sub: "Gardener hot-deploy · approved", dur: 4.5, rot: -0.06 });
          this.beam(() => this.workshopPos(), () => this.posOfSystem(id), C.green, "deploy", 2.5);
        }
        this.lastVersion[id] = v;
      }
    }
    // failure pulses travel downstream while a system blames its upstream
    for (const [up, down] of LINKS) {
      if (this.sysDoc(down)?.blames === up && this.t - (this.blamePulseT[down] || -9) > 0.85) {
        this.blamePulseT[down] = this.t; this.pipePulse(up, down, C.red);
      }
    }
    for (const k of Object.keys(this.cellGlow)) this.cellGlow[k] *= Math.pow(0.12, dt);
    this.droneCharge = this.droneCharge.map((c) => c * Math.pow(0.2, dt));
    this.shipRage *= Math.pow(0.3, dt);
    if (this.hunt && this.t > this.hunt.until) this.hunt = null;
    const alive = (arr) => arr.filter((o) => this.t - o.t0 < o.dur + 0.05);
    this.bolts = alive(this.bolts); this.rings = alive(this.rings); this.floaters = alive(this.floaters);
    this.pulses = alive(this.pulses); this.beams = alive(this.beams); this.sparks = alive(this.sparks);
    this.stamps = alive(this.stamps); this.banners = alive(this.banners);
    const done = this.particles.filter((p) => this.t - p.t0 >= p.dur);
    for (const p of done) this.pulseCell(p.coll, 0.8);
    this.particles = this.particles.filter((p) => this.t - p.t0 < p.dur);
  }

  // ---------------------------------------------------------------- render
  render() {
    const { ctx, dpr } = this;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    if (!this.bg) this.bg = this.buildBackground();
    ctx.drawImage(this.bg, 0, 0);
    ctx.setTransform(dpr * this.scale, 0, 0, dpr * this.scale, dpr * this.ox, dpr * this.oy);
    this.hits = [];
    const inc = currentIncident();
    this.drawZoneHeaders();
    this.drawSpine();
    this.drawPipes();
    for (const id of Object.keys(BUILDINGS)) this.drawBuilding(id, inc);
    this.drawDiagnosis(inc);
    this.drawFleet();
    this.drawHive();
    this.drawBees();
    this.drawEffects();
  }

  buildBackground() {
    const c = document.createElement("canvas"); c.width = this.cv.width; c.height = this.cv.height;
    const g = c.getContext("2d");
    g.fillStyle = "#04070d"; g.fillRect(0, 0, c.width, c.height);
    g.setTransform(this.dpr * this.scale, 0, 0, this.dpr * this.scale, this.dpr * this.ox, this.dpr * this.oy);
    const x0 = -this.ox / this.scale, y0 = -this.oy / this.scale, x1 = VW + this.ox / this.scale, y1 = VH + this.oy / this.scale;
    const rad = (x, y, r, col, a) => { const gr = g.createRadialGradient(x, y, 0, x, y, r); gr.addColorStop(0, hexA(col, a)); gr.addColorStop(1, hexA(col, 0)); g.fillStyle = gr; g.fillRect(x0, y0, x1 - x0, y1 - y0); };
    rad(140, 260, 420, "#ff2a3d", 0.13); rad(1250, 300, 460, "#1e90ff", 0.12); rad(650, 470, 520, "#4cc9f0", 0.05);
    g.strokeStyle = "rgba(90,130,180,.06)"; g.lineWidth = 1;
    for (let x = Math.floor(x0 / 40) * 40; x < x1; x += 40) { g.beginPath(); g.moveTo(x, y0); g.lineTo(x, SPINE_TOP - 10); g.stroke(); }
    for (let y = Math.floor(y0 / 40) * 40; y < SPINE_TOP - 10; y += 40) { g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke(); }
    // ground plane of the estate
    const gp = g.createLinearGradient(0, GROUND, 0, GROUND + 160); gp.addColorStop(0, "rgba(76,201,240,.10)"); gp.addColorStop(1, "rgba(76,201,240,0)");
    g.fillStyle = gp; g.fillRect(300, GROUND, 700, 160);
    g.strokeStyle = "rgba(76,201,240,.25)"; g.beginPath(); g.moveTo(300, GROUND + 0.5); g.lineTo(1000, GROUND + 0.5); g.stroke();
    // zone dividers
    g.setLineDash([3, 7]); g.strokeStyle = "rgba(120,150,190,.14)";
    for (const x of [296, 1012]) { g.beginPath(); g.moveTo(x, 20); g.lineTo(x, SPINE_TOP - 24); g.stroke(); }
    g.setLineDash([]);
    // spine band
    const sb = g.createLinearGradient(0, SPINE_TOP - 6, 0, VH + 40); sb.addColorStop(0, "#06120c"); sb.addColorStop(1, "#030806");
    g.fillStyle = sb; g.fillRect(x0, SPINE_TOP - 6, x1 - x0, y1 - SPINE_TOP + 6);
    g.strokeStyle = hexA(C.mongo, 0.45); g.lineWidth = 1.5; g.beginPath(); g.moveTo(x0, SPINE_TOP - 6); g.lineTo(x1, SPINE_TOP - 6); g.stroke();
    g.strokeStyle = hexA(C.mongo, 0.05); g.lineWidth = 1;
    for (let i = 0; i < 40; i++) { hexPath(g, x0 + i * 42, SPINE_TOP + 40 + (i % 2) * 24, 24); g.stroke(); hexPath(g, x0 + i * 42, SPINE_TOP + 130 + (i % 2) * 24, 24); g.stroke(); }
    // vignette
    const v = g.createRadialGradient(VW / 2, VH / 2, VH * 0.45, VW / 2, VH / 2, VH * 1.05); v.addColorStop(0, "rgba(0,0,0,0)"); v.addColorStop(1, "rgba(0,0,0,.55)");
    g.fillStyle = v; g.fillRect(x0, y0, x1 - x0, y1 - y0);
    return c;
  }

  drawZoneHeaders() {
    const { ctx } = this;
    ctx.textBaseline = "alphabetic";
    ctx.textAlign = "left"; ctx.font = FONT.display(24); ctx.fillStyle = "#ff8a95"; ctx.fillText("RED · THE FLEET", 22, 40);
    ctx.font = FONT.mono(12); ctx.fillStyle = "#b8606b"; ctx.fillText("seeded Chaos Monkey attacks", 22, 59);
    ctx.textAlign = "right"; ctx.font = FONT.display(24); ctx.fillStyle = "#8fd8ff"; ctx.fillText("BLUE · THE HIVE", VW - 22, 40);
    ctx.font = FONT.mono(12); ctx.fillStyle = "#5a8fb0"; ctx.fillText("12 AI agent processes defend", VW - 22, 59);
    // the centre header steps aside while cinematic captions occupy the top of the stage
    this.capEl ||= document.getElementById("captions");
    if (this.capEl && this.capEl.childElementCount) return;
    ctx.textAlign = "center"; ctx.font = FONT.display(24); ctx.fillStyle = "#cfe0f2"; ctx.fillText("THE LEGACY ESTATE", 652, 40);
    ctx.font = FONT.mono(12); ctx.fillStyle = "#5d7591"; ctx.fillText("First National (simulated) · mainframe → unix → portal", 652, 59);
    // colour key, so a first-time viewer can read every effect on the stage
    const key = [["attack", C.red], ["diagnosis", C.blue], ["verified", C.green], ["recall", C.gold], ["checkpoint", C.pink]];
    ctx.font = FONT.mono(11, 600);
    const kw = key.map(([k]) => ctx.measureText(k).width + 22), ktot = kw.reduce((a, w) => a + w, 0);
    let kx = 652 - ktot / 2;
    ctx.textAlign = "left"; ctx.textBaseline = "middle";
    key.forEach(([k, col], i) => {
      ctx.beginPath(); ctx.arc(kx + 5, 80, 4, 0, TAU); ctx.fillStyle = col; ctx.fill();
      ctx.fillStyle = hexA(col, 0.85); ctx.fillText(k, kx + 13, 80.5); kx += kw[i];
    });
    ctx.textBaseline = "alphabetic";
  }

  // ---------------------------------------------------------------- estate
  drawPipes() {
    const { ctx, t } = this;
    for (const [up, down] of LINKS) {
      const a = BUILDINGS[up], b = BUILDINGS[down];
      const x1 = a.x + a.w + a.depth * 0.6, x2 = b.x - 2;
      const broken = this.sysDoc(down)?.blames === up;
      const col = broken ? C.red : C.green;
      ctx.lineCap = "round";
      ctx.strokeStyle = "#0e1a29"; ctx.lineWidth = 16; ctx.beginPath(); ctx.moveTo(x1, PIPE_Y); ctx.lineTo(x2, PIPE_Y); ctx.stroke();
      ctx.strokeStyle = hexA(col, broken ? 0.55 + 0.25 * Math.sin(t * 6) : 0.28); ctx.lineWidth = 2;
      for (const dy of [-8, 8]) { ctx.beginPath(); ctx.moveTo(x1, PIPE_Y + dy); ctx.lineTo(x2, PIPE_Y + dy); ctx.stroke(); }
      ctx.setLineDash([6, 12]); ctx.lineDashOffset = -t * (broken ? 6 : 38);
      ctx.strokeStyle = hexA(col, broken ? 0.35 : 0.65); ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(x1, PIPE_Y); ctx.lineTo(x2, PIPE_Y); ctx.stroke();
      ctx.setLineDash([]); ctx.lineDashOffset = 0;
      for (const fx of [x1 + 3, x2 - 3]) { ctx.fillStyle = "#1d2e44"; ctx.fillRect(fx - 3, PIPE_Y - 11, 6, 22); }
      if (broken) {
        ctx.font = FONT.mono(11, 700); ctx.fillStyle = "#ff8a95"; ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
        ctx.fillText("BLAMES", (x1 + x2) / 2, PIPE_Y - 15);
        ctx.beginPath(); const mx = (x1 + x2) / 2; ctx.moveTo(mx - 14, PIPE_Y - 26); ctx.lineTo(mx - 22, PIPE_Y - 30); ctx.lineTo(mx - 14, PIPE_Y - 34); ctx.strokeStyle = "#ff8a95"; ctx.lineWidth = 1.5; ctx.stroke();
        ctx.beginPath(); ctx.moveTo(mx - 22, PIPE_Y - 30); ctx.lineTo(mx + 18, PIPE_Y - 30); ctx.stroke();
      }
      this.hit({ x: x1, y: PIPE_Y - 12, w: x2 - x1, h: 24, tip: broken ? `${down}.blames = "${up}"  (world.systems)` : `${up} → ${down}: healthy`, kind: "doc", db: "world", coll: "systems", id: down });
    }
  }

  buildingState(id) {
    const d = this.sysDoc(id) || {};
    const m = d.metrics || {};
    const status = d.status || "up";
    return {
      d, m, status, version: d.version ?? 1, blames: d.blames, vulns: d.vulns || [], faults: d.faults_active || [],
      batch: m.batch_status, disk: m.disk_pct, stale: id === "portal" && (status !== "up" || !!d.blames),
      glow: status === "up" ? 0.5 : 0.8 + 0.4 * Math.sin(this.t * (status === "down" ? 9 : 5)),
    };
  }

  drawBuilding(id, inc) {
    const { ctx, t } = this;
    const b = BUILDINGS[id], s = this.buildingState(id), hc = HEALTH[s.status] || C.muted;
    const sk = this.shake[id] != null ? Math.max(0, 0.6 - (t - this.shake[id])) : 0;
    ctx.save();
    if (sk > 0) ctx.translate(Math.sin(t * 90) * 6 * sk, Math.cos(t * 70) * 2 * sk);
    ctx.beginPath(); ctx.ellipse(b.cx + 8, GROUND + 4, b.w * 0.62, 10, 0, 0, TAU); ctx.fillStyle = "rgba(0,0,0,.55)"; ctx.fill();
    glowCircle(ctx, b.cx, b.y + b.h * 0.55, b.w * 0.95, hc, s.status === "up" ? 0.08 : 0.2 + 0.08 * Math.sin(t * 5));
    ctx.save(); ctx.translate(b.cx, GROUND); ctx.scale(BSCALE, BSCALE); ctx.translate(-b.cx, -GROUND);
    b.draw(ctx, b.raw, t, s);
    drawCracks(ctx, b.raw, s.vulns, t);
    ctx.restore();
    if (s.status === "down") for (let k = 0; k < 3; k++) { const p = ((t * 0.4 + k / 3) % 1); glowCircle(ctx, b.cx + Math.sin(k * 7 + t) * 20, b.y - p * 60, 18 + p * 18, "#556070", 0.35 * (1 - p)); }
    ctx.restore();
    // symptom bubble
    // the mainframe's own /health still says "up" while its batch has ABENDed: the silent failure
    const silent = id === "mainframe" && s.status === "up" && s.batch && !["OK", "RUNNING"].includes(s.batch);
    const bubble = s.d.symptom && s.status !== "up" ? prettyTime(s.d.symptom) : silent ? `/health says UP, but NIGHTBAT ${s.batch}` : null;
    if (bubble) {
      const bc = silent ? C.red : hc;
      ctx.font = FONT.ui(13.5, 600);
      const lines = wrap(ctx, bubble, 214, 3);
      const bw = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 22, bh = lines.length * 17 + 12;
      const bx = b.cx - bw / 2, by = b.y - 36 - bh;
      roundRect(ctx, bx, by, bw, bh, 8); ctx.fillStyle = silent ? "rgba(30,4,8,.94)" : "rgba(22,13,2,.94)"; ctx.fill(); ctx.strokeStyle = hexA(bc, 0.95); ctx.lineWidth = 1.6; ctx.stroke();
      ctx.beginPath(); ctx.moveTo(b.cx - 7, by + bh); ctx.lineTo(b.cx, by + bh + 9); ctx.lineTo(b.cx + 7, by + bh); ctx.fillStyle = hexA(bc, 0.95); ctx.fill();
      ctx.fillStyle = silent ? "#ffd0d4" : "#ffe7b8"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      lines.forEach((l, i) => ctx.fillText(l, b.cx, by + 14.5 + i * 17));
    }
    // labels under the building
    let y = GROUND + 26;
    ctx.textAlign = "center"; ctx.textBaseline = "alphabetic"; ctx.font = FONT.display(19); ctx.fillStyle = C.ink;
    ctx.fillText(b.label, b.cx - 16, y);
    const lw = ctx.measureText(b.label).width;
    const vUp = this.lastVersion[id] > 1 || s.version > 1;
    pill(ctx, b.cx - 16 + lw / 2 + 21, y - 6, `v${s.version}`, { fg: vUp ? "#032015" : C.ink, bg: vUp ? C.green : "#111c2b", border: vUp ? C.green : "#2b3e56", font: FONT.mono(12, 700), h: 19 });
    y += 21;
    const stTxt = s.status.toUpperCase() + (s.blames ? ` · blames ${s.blames}` : "");
    ctx.font = FONT.mono(12, 700);
    const sw = ctx.measureText(stTxt).width + 26;
    roundRect(ctx, b.cx - sw / 2, y - 10, sw, 20, 10); ctx.fillStyle = hexA(hc, 0.16); ctx.fill(); ctx.strokeStyle = hexA(hc, 0.75); ctx.lineWidth = 1.2; ctx.stroke();
    ctx.beginPath(); ctx.arc(b.cx - sw / 2 + 10, y - 0.5, 3.5, 0, TAU); ctx.fillStyle = hc; ctx.fill();
    ctx.fillStyle = hc; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(stTxt, b.cx + 5, y);
    y += 23;
    const chips = [];
    if (id === "mainframe") chips.push([`batch ${s.batch || "OK"}`, s.batch && s.batch !== "OK" && s.batch !== "RUNNING" ? C.red : C.ink2]);
    if (id === "unix") chips.push([`disk ${s.disk != null ? Math.round(s.disk) : "—"}%`, s.disk >= 90 ? C.red : s.disk >= 80 ? C.amber : C.ink2]);
    if (id === "portal") chips.push([`sessions ${s.m.sessions ?? "—"}`, C.ink2]);
    if (s.m.latency_ms != null) chips.push([`${Math.round(s.m.latency_ms)}ms`, s.m.latency_ms > 800 ? C.amber : C.ink2]);
    if (s.m.error_rate != null) chips.push([`err ${(s.m.error_rate * 100).toFixed(1)}%`, s.m.error_rate > 0.05 ? C.red : C.ink2]);
    ctx.font = FONT.mono(11.5, 600);
    const widths = chips.map(([tx]) => ctx.measureText(tx).width + 12), tot = widths.reduce((a, w) => a + w + 4, -4);
    let cx = b.cx - tot / 2;
    chips.forEach(([tx, col], i) => { pill(ctx, cx + widths[i] / 2, y, tx, { fg: col, bg: col === C.red ? "#2a0b10" : "#0b1422", border: col === C.red ? "#7a2430" : "#1f3047", font: FONT.mono(11.5, 600), padX: 6, h: 19 }); cx += widths[i] + 4; });
    y += 20;
    const logs = state.logs[id] || [];
    const last = logs[logs.length - 1];
    if (last) {
      ctx.font = FONT.mono(10.5, 500); ctx.textAlign = "center"; ctx.textBaseline = "middle";
      const lvl = String(last.level || "").toUpperCase();
      ctx.fillStyle = lvl.startsWith("ERR") || lvl === "FATAL" ? "#ff8a95" : lvl.startsWith("WARN") ? "#ffcf70" : "#5d7089";
      const ln = String(last.line || ""), code = last.code && !ln.startsWith(last.code) ? last.code + " " : "";
      ctx.fillText(ellipsize(ctx, `${code}${ln}`, 232), b.cx, y);
      y += 16;
    }
    const open = s.vulns.filter((v) => v.open).length, expl = s.vulns.filter((v) => v.exploited).length;
    if (open || s.faults.length) {
      ctx.font = FONT.mono(10.5, 600); ctx.textAlign = "center"; ctx.textBaseline = "middle";
      const parts = [];
      if (open) parts.push(`${open} open vuln${open > 1 ? "s" : ""}${expl ? ` · ${expl} exploited` : ""}`);
      ctx.fillStyle = expl ? "#ff8a95" : "#c9a36b";
      if (parts.length) { ctx.fillText(parts.join(""), b.cx, y); y += 14; }
      if (s.faults.length) {
        const txt = "injected: " + s.faults.map((f) => f.fault).join(", ");
        ctx.font = FONT.mono(10.5, 600); const w = ctx.measureText(txt).width + 12;
        ctx.setLineDash([3, 3]); ctx.strokeStyle = hexA(C.red, 0.6); ctx.strokeRect(b.cx - w / 2, y - 9, w, 18); ctx.setLineDash([]);
        ctx.fillStyle = "#ff8a95"; ctx.fillText(txt, b.cx, y);
      }
    }
    this.hit({ x: b.x, y: b.y - 12, w: b.w + b.depth, h: b.h + 12, tip: `world.systems · ${id} · ${s.status} · v${s.version}`, kind: "doc", db: "world", coll: "systems", id });
  }

  drawDiagnosis(inc) {
    if (!inc || !inc.active) return;
    const { ctx, t } = this;
    const path = inc.diagnosis_path || [];
    path.forEach((sid, i) => {
      const b = BUILDINGS[sid]; if (!b) return;
      const x = b.x - 6, y = b.y - 4;
      glowCircle(ctx, x, y, 26, C.blue, 0.35);
      ctx.beginPath(); ctx.arc(x, y, 14, 0, TAU); ctx.fillStyle = "#06263a"; ctx.fill(); ctx.strokeStyle = C.blue; ctx.lineWidth = 2; ctx.stroke();
      ctx.font = FONT.mono(14, 700); ctx.fillStyle = "#cdeeff"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(String(i + 1), x, y + 0.5);
    });
    // recall golden path: the chain stops the round-1 walk visited are skipped this time
    if (inc.recall?.used) {
      const skipped = ["portal", "unix", "mainframe"].filter((sid) => (inc.affected || []).includes(sid) && !path.includes(sid) && sid !== inc.root_cause_system && sid !== inc.recall?.system);
      for (const sid of skipped) {
        const b = BUILDINGS[sid], x = b.x - 6, y = b.y - 4;
        ctx.save(); ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.arc(x, y, 14, 0, TAU); ctx.fillStyle = "#1f1a05"; ctx.fill();
        ctx.strokeStyle = C.gold; ctx.lineWidth = 2; ctx.stroke(); ctx.restore();
        ctx.beginPath(); ctx.moveTo(x - 8, y + 8); ctx.lineTo(x + 8, y - 8); ctx.strokeStyle = C.gold; ctx.lineWidth = 2; ctx.stroke();
        pill(ctx, x + 4, y + 27, "SKIPPED", { fg: "#1f1a05", bg: C.gold, border: null, font: FONT.mono(11, 700), h: 18, padX: 6 });
      }
    }
    const root = inc.root_cause_system;
    if (root && BUILDINGS[root] && inc.active) {
      const b = BUILDINGS[root], cx = b.cx + 4, cy = b.y + b.h / 2;
      ctx.save(); ctx.translate(cx, cy); ctx.rotate(t * 0.6);
      ctx.strokeStyle = hexA(C.red, 0.85); ctx.lineWidth = 2; const R = b.w * 0.62;
      for (let k = 0; k < 4; k++) { ctx.beginPath(); ctx.arc(0, 0, R, k * Math.PI / 2 + 0.25, k * Math.PI / 2 + 1.3); ctx.stroke(); }
      ctx.restore();
      pill(ctx, cx, b.y + 12, "ROOT CAUSE", { fg: "#fff", bg: "#b3122a", border: null, font: FONT.display(13, 700), h: 21, padX: 8 });
    }
  }

  // ---------------------------------------------------------------- fleet
  drawFleet() {
    const { ctx, t } = this;
    drawMothership(ctx, SHIP.x, SHIP.y, t, { rage: this.shipRage, scale: 1.2 });
    this.hit({ x: SHIP.x, y: SHIP.y, r: 60, tip: "Chaos Monkey mothership · world.chaos_events", kind: "atlas", coll: "chaos_events" });
    this.droneY = DRONES.map(([dx, dy], i) => drawDrone(ctx, SHIP.x + dx, SHIP.y + dy, t, i, { charge: this.droneCharge[i] }));
    // run / seed
    const run = currentRun();
    ctx.textAlign = "center"; ctx.font = FONT.mono(12, 600); ctx.fillStyle = "#c06a76";
    ctx.fillText(run ? `${run._id} · ${run.scenario} · seed ${run.seed ?? "—"}` : "no run · free play", SHIP.x, 372);
    // fault log
    const evs = [...state.chaos.values()].sort((a, b) => ts(b.ts) - ts(a.ts)).slice(0, 8);
    let y = 400;
    ctx.textAlign = "left"; ctx.font = FONT.display(14, 700); ctx.fillStyle = "#ff8a95"; ctx.fillText("FAULT LOG", 20, y);
    ctx.font = FONT.mono(10.5); ctx.fillStyle = "#8a4a54"; ctx.fillText("world.chaos_events · hidden from Hive", 104, y);
    y += 12;
    if (!evs.length) { ctx.font = FONT.mono(12); ctx.fillStyle = "#5a3a40"; ctx.fillText("quiet night… for now", 22, y + 20); }
    evs.forEach((ev, i) => {
      const yy = y + i * 43, fresh = clamp(1 - (Date.now() - ts(ev.ts)) / 6000, 0, 1);
      roundRect(ctx, 14, yy, 268, 38, 6);
      ctx.fillStyle = fresh > 0 ? hexA("#ff2a3d", 0.08 + fresh * 0.25) : "rgba(40,10,16,.45)"; ctx.fill();
      ctx.strokeStyle = hexA(C.red, 0.18 + fresh * 0.6); ctx.lineWidth = 1; ctx.stroke();
      ctx.font = FONT.mono(13, 700); ctx.fillStyle = "#ffb3ba"; ctx.textBaseline = "alphabetic";
      ctx.fillText(ev.fault, 24, yy + 16);
      const fw = ctx.measureText(ev.fault).width;
      ctx.font = FONT.mono(12, 500); ctx.fillStyle = "#e08a95";
      const tgt = ev.fault === "agent_kill"
        ? (ev.result?.agent || (ev.result?.how === "gave_up" ? "skipped: no worker mid-step" : ev.result ? "no victim" : "hunting…"))
        : ev.target;
      ctx.fillText(ellipsize(ctx, `→ ${tgt}`, 248 - fw - 8), 24 + fw + 8, yy + 16);
      ctx.font = FONT.mono(10.5, 500); ctx.fillStyle = "#9a5a64";
      const when = ev.ts ? new Date(ts(ev.ts)).toLocaleTimeString([], { hour12: false }) : "";
      ctx.fillText(`${when}  ${ev._id || ""}  ${ev.manual ? "manual" : ev.scenario || ""}`, 24, yy + 31);
      const st = ev.result?.how === "gave_up" ? "skipped" : ev.status || (ev.result ? (ev.result.ok ? "ok" : "failed") : "firing");
      ctx.textAlign = "right"; ctx.fillStyle = st === "ok" ? "#ff8a95" : st === "failed" || st === "skipped" ? "#7a4a52" : "#fff";
      ctx.fillText(st.toUpperCase(), 274, yy + 31); ctx.textAlign = "left";
      this.hit({ x: 14, y: yy, w: 268, h: 38, tip: `world.chaos_events · ${ev._id}`, kind: "doc", db: "world", coll: "chaos_events", id: ev._id });
    });
    // agent_kill in flight: a reticle sweeping the hive
    if (this.hunt) {
      const k = this.t - this.hunt.t0, x = HIVE.x + Math.sin(k * 1.7) * 90, yv = HIVE.y + Math.cos(k * 1.3) * 70;
      ctx.save(); ctx.strokeStyle = hexA(C.red, 0.9); ctx.lineWidth = 2; ctx.translate(x, yv); ctx.rotate(k);
      ctx.beginPath(); ctx.arc(0, 0, 22, 0, TAU); ctx.stroke();
      for (let q = 0; q < 4; q++) { ctx.rotate(Math.PI / 2); ctx.beginPath(); ctx.moveTo(14, 0); ctx.lineTo(30, 0); ctx.stroke(); }
      ctx.restore();
      pill(ctx, HIVE.x - 10, HIVE.y - 196, "AGENT KILL ARMED · waiting for a worker to send a step", { fg: "#fff", bg: "#5a0d18", border: C.red, font: FONT.mono(12, 700), h: 22, padX: 10 });
    }
  }

  // ---------------------------------------------------------------- hive
  drawHive() {
    const { ctx, t } = this;
    glowCircle(ctx, HIVE.x, HIVE.y, 190, "#ffb020", 0.08);
    const honey = Math.min(state.playbooks.size, CELLS.length);
    CELLS.forEach((c, i) => {
      hexPath(ctx, c.x, c.y, HIVE.r - 2.5);
      const filled = i >= CELLS.length - honey; // honey fills from the outer ring inward
      const pb = filled ? [...state.playbooks.values()][CELLS.length - 1 - i] : null;
      const g = ctx.createLinearGradient(c.x, c.y - HIVE.r, c.x, c.y + HIVE.r);
      if (pb && !pb.stale) { g.addColorStop(0, "#7a5408"); g.addColorStop(1, "#3d2a03"); }
      else if (pb) { g.addColorStop(0, "#3a3a3a"); g.addColorStop(1, "#1f1f1f"); }
      else { g.addColorStop(0, "#1d1606"); g.addColorStop(1, "#0f0b03"); }
      ctx.fillStyle = g; ctx.fill();
      ctx.strokeStyle = hexA("#ffc933", pb && !pb.stale ? 0.85 : 0.38); ctx.lineWidth = 1.6; ctx.stroke();
      if (pb) this.hit({ x: c.x, y: c.y, r: HIVE.r - 4, tip: `honey = swarm.playbooks · ${pb._id}${pb.stale ? " (stale)" : ""}`, kind: "doc", db: "swarm", coll: "playbooks", id: pb._id });
    });
    ctx.textAlign = "center"; ctx.font = FONT.mono(12, 600); ctx.fillStyle = "#b8923a";
    const stale = [...state.playbooks.values()].filter((p) => p.stale).length;
    ctx.fillText(`honey = ${state.playbooks.size} playbook${state.playbooks.size === 1 ? "" : "s"}${stale ? ` (${stale} stale)` : ""} · $vectorSearch memory`, HIVE.x, HIVE.y + 206);
    // nurse bench
    ctx.fillStyle = "#3a2614"; roundRect(ctx, BENCH.x - 82, BENCH.y - 6, 170, 9, 3); ctx.fill();
    ctx.fillStyle = "#24170c"; ctx.fillRect(BENCH.x - 72, BENCH.y + 3, 6, 18); ctx.fillRect(BENCH.x + 70, BENCH.y + 3, 6, 18);
    ctx.font = FONT.mono(12, 600); ctx.fillStyle = "#c98fb2"; ctx.fillText("NURSE BENCH · standby", BENCH.x + 3, BENCH.y + 40);
    // gardener workshop
    const up = [...state.upgrades.values()].some((u) => ["planning", "testing", "deploying"].includes(u.status));
    ctx.save(); ctx.translate(SHOP.x, SHOP.y);
    ctx.beginPath(); ctx.moveTo(-58, 0); ctx.lineTo(-58, -40); ctx.lineTo(0, -72); ctx.lineTo(58, -40); ctx.lineTo(58, 0); ctx.closePath();
    ctx.fillStyle = "#0c1a12"; ctx.fill(); ctx.strokeStyle = hexA(C.green, up ? 0.9 : 0.4); ctx.lineWidth = 1.5; ctx.stroke();
    ctx.translate(32, -22); ctx.rotate(up ? t * 2 : 0);
    ctx.strokeStyle = hexA(C.green, 0.9); ctx.lineWidth = 3;
    for (let k = 0; k < 8; k++) { ctx.rotate(Math.PI / 4); ctx.beginPath(); ctx.moveTo(7, 0); ctx.lineTo(12, 0); ctx.stroke(); }
    ctx.beginPath(); ctx.arc(0, 0, 7, 0, TAU); ctx.lineWidth = 2.5; ctx.stroke();
    ctx.restore();
    ctx.font = FONT.mono(12, 600); ctx.fillStyle = "#6fc496"; ctx.textAlign = "center"; ctx.fillText("GARDENER WORKSHOP", SHOP.x, SHOP.y + 20);
    this.hit({ x: SHOP.x - 58, y: SHOP.y - 72, w: 116, h: 90, tip: "swarm.upgrade_requests · Gardener", kind: "atlas", coll: "upgrade_requests" });
  }

  drawBees() {
    const { ctx, t } = this;
    // dropped tickets lie on the floor next to the fallen bee
    for (const tk of this.tickets.values()) if (!tk.holder) drawTicket(ctx, tk.x, tk.y, shortTask(tk.task), { glow: Math.sin(t * 5) > 0 ? C.pink : null });
    const bees = [...this.bees.values()].sort((a, b) => (a.dead === b.dead ? a.y - b.y : a.dead ? -1 : 1));
    for (const b of bees) {
      const a = b.doc || {}, role = roleOf(a), color = ROLE[role] || C.blue;
      const bob = b.dead ? 0 : Math.sin(t * 3 + b.phase) * 3;
      const scale = (role === "queen" ? 1.3 : 1) * BEE_SCALE;
      drawBee(ctx, b.x, b.y + bob, { color, role, scale, facing: b.facing, t, dead: b.dead, alpha: a.ghost ? 0.35 : 1, phase: b.phase });
      const held = [...this.tickets.values()].find((tk) => tk.holder === b.id);
      b.held = held; b.bob = bob;
      if (held) drawTicket(ctx, b.x + 6, b.y + bob + 22, shortTask(held.task), { glow: C.pink, scale: 1.05 });
      else if (a.task && !b.dead && a.status === "working") drawTicket(ctx, b.x + 16, b.y + bob + 12, "", { scale: 0.45 });
      this.hit({ x: b.x, y: b.y, r: 24, tip: `swarm.agents · ${b.id} · ${a.status || "?"}${a.phase ? " · " + a.phase : ""}${a.task ? " · " + a.task : ""}`, kind: "doc", db: "swarm", coll: "agents", id: b.id });
    }
    // labels in a second pass so no sprite covers another bee's name; greedy placement avoids label overlap
    const placed = [];
    const free = (r) => !placed.some((q) => r.x < q.x + q.w && r.x + r.w > q.x && r.y < q.y + q.h && r.y + r.h > q.y);
    const order = [...bees].sort((p, q) => (q.doc?.status === "working") - (p.doc?.status === "working") || (p.dead - q.dead));
    for (const b of order) { // phase pills first: they carry the story (fixing / verifying / step sent)
      const a = b.doc || {}, role = roleOf(a), color = ROLE[role] || C.blue, bob = b.bob || 0;
      const scale = (role === "queen" ? 1.3 : 1) * BEE_SCALE;
      if (b.dead || !a.phase || b.moving) continue;
      const txt = String(a.phase).replace(/_/g, " ");
      ctx.font = FONT.mono(11, 700);
      const w = ctx.measureText(txt).width + 12, y = b.y + bob - 27 * scale;
      placed.push({ x: b.x - w / 2, y: y - 8, w, h: 16 });
      pill(ctx, b.x, y, txt, { fg: "#04070d", bg: color, border: null, font: FONT.mono(11, 700), h: 16, padX: 6 });
    }
    for (const b of order) {
      const a = b.doc || {}, role = roleOf(a), color = ROLE[role] || C.blue, bob = b.bob || 0, held = b.held;
      const scale = (role === "queen" ? 1.3 : 1) * BEE_SCALE;
      ctx.font = FONT.mono(11, 600); ctx.textAlign = "center"; ctx.textBaseline = "middle";
      const lbl = shortAgent(b.id);
      const lw = ctx.measureText(lbl).width + 8, lh = 15;
      const base = b.y + bob + (held ? 36 : 21) * scale;
      let ly = base;
      for (const dy of [0, 14, -14, 28, -28, 42]) {
        const r = { x: b.x - lw / 2, y: base + dy - lh / 2, w: lw, h: lh };
        if (free(r)) { ly = base + dy; break; }
      }
      placed.push({ x: b.x - lw / 2, y: ly - lh / 2, w: lw, h: lh + (b.dead ? 14 : 0) });
      roundRect(ctx, b.x - lw / 2, ly - lh / 2, lw, lh, 4); ctx.fillStyle = "rgba(4,8,14,.84)"; ctx.fill();
      ctx.fillStyle = b.dead ? "#8c96a8" : a.ghost ? hexA(color, 0.5) : color; ctx.fillText(lbl, b.x, ly + 0.5);
      if (b.dead) { ctx.font = FONT.mono(11, 700); ctx.fillStyle = "#ff8a95"; ctx.fillText(b.sigkilled ? "SIGKILL · no heartbeat" : "DEAD · lease released", b.x, ly + 15); }
    }
    if (!state.agents.size) {
      ctx.font = FONT.mono(12, 600); ctx.fillStyle = "#4f7f9c"; ctx.textAlign = "center";
      ctx.fillText("swarm offline · waiting for heartbeats in swarm.agents", HIVE.x, HIVE.y - 150);
    }
  }

  // ---------------------------------------------------------------- MongoDB spine
  drawSpine() {
    const { ctx, t } = this;
    const y0 = SPINE_TOP;
    ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
    // left block: title + ops/s + sparkline
    ctx.save(); ctx.translate(22, y0 + 22);
    ctx.fillStyle = C.mongo; ctx.shadowColor = C.mongo; ctx.shadowBlur = 8;
    hexPath(ctx, 8, -5, 8, false); ctx.fill(); ctx.shadowBlur = 0;
    ctx.font = FONT.display(22); ctx.fillStyle = "#d9ffe9"; ctx.fillText("MONGODB SPINE", 24, 2);
    ctx.font = FONT.mono(11.5, 600); ctx.fillStyle = "#4fb07a";
    ctx.fillText("each pixel above = one change event", 0, 21);
    ctx.font = FONT.mono(34, 700); ctx.fillStyle = "#eafff2";
    const ops = state.opsPerSec || 0;
    ctx.fillText(ops >= 10 ? ops.toFixed(0) : ops.toFixed(1), 0, 64);
    const ow = ctx.measureText(ops >= 10 ? ops.toFixed(0) : ops.toFixed(1)).width;
    ctx.font = FONT.mono(11, 600); ctx.fillStyle = "#3f8f62"; ctx.fillText("ops/s", ow + 8, 64);
    const hist = state.opsHistory, max = Math.max(4, ...hist);
    ctx.beginPath();
    hist.forEach((v, i) => { const x = (i / 89) * 280, y = 112 - (v / max) * 34; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.strokeStyle = C.mongo; ctx.lineWidth = 1.6; ctx.stroke();
    if (hist.length) { ctx.lineTo(((hist.length - 1) / 89) * 280, 112); ctx.lineTo(0, 112); ctx.closePath(); ctx.fillStyle = hexA(C.mongo, 0.1); ctx.fill(); }
    ctx.font = FONT.mono(10.5, 500); ctx.fillStyle = "#3d8a5f";
    ctx.fillText(`${state.meta?.dbs?.world || "world"} + ${state.meta?.dbs?.swarm || "swarm"} · 1 change stream → all screens`, 0, 132);
    ctx.fillText(`resume token in swarm.stats · updateLookup`, 0, 147);
    ctx.restore();
    // features header
    ctx.font = FONT.mono(11.5, 600); ctx.textAlign = "right"; ctx.fillStyle = "#4fb07a";
    ctx.fillText("change streams · transactions · $vectorSearch · TTL · partial unique · $jsonSchema · leases", VW - 18, y0 + 16);
    // cells
    SPINE.forEach(([db, coll, feature], i) => {
      const { x, y } = this.cellPos(coll);
      const glow = clamp(this.cellGlow[coll] || 0, 0, 1.4);
      const target = state.counts?.[coll] ?? 0;
      const shown = (this.cellShown[coll] ??= target);
      this.cellShown[coll] = Math.abs(target - shown) < 0.5 ? target : shown + (target - shown) * 0.15;
      ctx.save();
      if (glow > 0.05) { ctx.shadowColor = C.mongo; ctx.shadowBlur = 30 * glow; }
      hexPath(ctx, x, y, CELL_R - 3);
      const g = ctx.createLinearGradient(x, y - CELL_R, x, y + CELL_R);
      g.addColorStop(0, `rgba(${Math.round(8 + 20 * glow)},${Math.round(36 + 120 * glow)},${Math.round(22 + 60 * glow)},1)`);
      g.addColorStop(1, "#04100a");
      ctx.fillStyle = g; ctx.fill();
      ctx.strokeStyle = hexA(C.mongo, 0.35 + 0.6 * Math.min(1, glow)); ctx.lineWidth = 1.4 + 2 * Math.min(1, glow); ctx.stroke();
      ctx.restore();
      ctx.fillStyle = DB_COLOR[db]; ctx.fillRect(x - 9, y - CELL_R + 9, 18, 2.5);
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.font = FONT.mono(target >= 10000 ? 16 : 20, 700); ctx.fillStyle = glow > 0.3 ? "#ffffff" : "#e6fff0";
      ctx.fillText(Math.round(this.cellShown[coll]).toLocaleString(), x, y - 5);
      ctx.fillStyle = glow > 0.3 ? "#c8ffdf" : "#7fd0a2";
      const nm = coll.length > 12 ? coll.split("_") : [coll];
      ctx.font = FONT.mono(nm.length > 1 ? 9.5 : coll.length > 10 ? 9.5 : 10.5, 600);
      nm.forEach((part, k) => ctx.fillText(part + (k < nm.length - 1 ? "_" : ""), x, y + 15 + k * 11 - (nm.length - 1) * 4));
      // feature tag outside the strip
      const up = i % 2 === 0;
      ctx.font = FONT.mono(10.5, 600); ctx.fillStyle = "#4fa576";
      ctx.fillText(feature, x, up ? y - CELL_R - 7 : y + CELL_R + 9);
      this.hit({ x, y, r: CELL_R - 4, tip: `${db}.${coll} · ${target} docs · ${feature}`, kind: "atlas", coll, db });
    });
    // legend
    ctx.textAlign = "left"; ctx.font = FONT.mono(11, 600);
    const lx = VW - 70, ly = y0 + 172;
    ctx.fillStyle = DB_COLOR.world; ctx.fillRect(lx, ly - 4, 10, 3); ctx.fillStyle = "#a35660"; ctx.fillText("world", lx + 14, ly);
    ctx.fillStyle = DB_COLOR.swarm; ctx.fillRect(lx, ly + 10, 10, 3); ctx.fillStyle = "#4f7f9c"; ctx.fillText("swarm", lx + 14, ly + 14);
  }

  // ---------------------------------------------------------------- transient effects
  drawEffects() {
    const { ctx, t } = this;
    // pipe pulses (downstream failure / upstream diagnosis)
    for (const p of this.pulses) {
      const ends = pipeEnds(p.from, p.to); if (!ends) continue;
      const k = clamp((t - p.t0) / p.dur, 0, 1);
      const x = ends[0] + (ends[1] - ends[0]) * ease(k);
      const dir = Math.sign(ends[1] - ends[0]);
      for (let q = 4; q >= 1; q--) glowCircle(ctx, x - dir * q * 9, PIPE_Y, 14, p.color, 0.18 * (5 - q) / 4);
      glowCircle(ctx, x, PIPE_Y, 32, p.color, 0.9 * (1 - k * 0.3));
      ctx.beginPath(); ctx.arc(x, PIPE_Y, 4.5, 0, TAU); ctx.fillStyle = "#fff"; ctx.fill();
    }
    // beams (recall golden path, checkpoint fetch, deploy)
    for (const bm of this.beams) {
      const f = bm.fromFn(), to = bm.toFn(); if (!f || !to) continue;
      const k = (t - bm.t0) / bm.dur, fade = k < 0.15 ? k / 0.15 : k > 0.8 ? (1 - k) / 0.2 : 1;
      const cx = (f.x + to.x) / 2, cy = Math.min(f.y, to.y) - 160;
      ctx.save(); ctx.lineCap = "round";
      ctx.shadowColor = bm.color; ctx.shadowBlur = 18;
      ctx.strokeStyle = hexA(bm.color, 0.35 * fade); ctx.lineWidth = 9; ctx.beginPath(); ctx.moveTo(f.x, f.y); ctx.quadraticCurveTo(cx, cy, to.x, to.y); ctx.stroke();
      ctx.strokeStyle = hexA(bm.color, 0.95 * fade); ctx.lineWidth = 2.2; ctx.setLineDash([10, 8]); ctx.lineDashOffset = -t * 120; ctx.stroke(); ctx.setLineDash([]);
      ctx.restore();
      for (let q = 0; q < 3; q++) {
        const u = ((t - bm.t0) * 0.9 + q / 3) % 1, w = 1 - u;
        const px = w * w * f.x + 2 * w * u * cx + u * u * to.x, py = w * w * f.y + 2 * w * u * cy + u * u * to.y;
        glowCircle(ctx, px, py, 16, bm.color, 0.9 * fade);
      }
      if (bm.label) pill(ctx, cx, cy / 2 + Math.min(f.y, to.y) / 2 + 40, bm.label, { fg: "#04070d", bg: bm.color, border: null, font: FONT.mono(10.5, 700), h: 18 });
    }
    // write particles: actor → collection cell
    for (const p of this.particles) {
      const k = ease(clamp((t - p.t0) / p.dur, 0, 1));
      const cx = (p.from.x + p.to.x) / 2 + p.bend, cy = Math.min(p.from.y, p.to.y) + (p.to.y - p.from.y) * 0.35;
      const u = 1 - k, x = u * u * p.from.x + 2 * u * k * cx + k * k * p.to.x, y = u * u * p.from.y + 2 * u * k * cy + k * k * p.to.y;
      glowCircle(ctx, x, y, 10, p.color, 0.7);
      ctx.beginPath(); ctx.arc(x, y, 2.2, 0, TAU); ctx.fillStyle = "#fff"; ctx.fill();
    }
    // lightning
    for (const bo of this.bolts) {
      const k = (t - bo.t0) / bo.dur; const to = bo.toFn ? bo.toFn() || bo.to : bo.to;
      const a = k < 0.6 ? 1 : (1 - k) / 0.4;
      const seed = bo.seed + Math.floor(t * 24);
      const pts = boltPoints(bo.from.x, bo.from.y, to.x, to.y, seed);
      strokeBolt(ctx, pts, bo.color, Math.max(0, a) * (0.75 + 0.25 * Math.random()));
      const [fx, fy] = pts[Math.floor(pts.length * 0.55)]; // a fork for drama
      strokeBolt(ctx, boltPoints(fx, fy, fx + (to.x - bo.from.x) * 0.18 + 30, fy + 70, seed + 7, 4), bo.color, Math.max(0, a) * 0.5);
      if (k < 0.25) glowCircle(ctx, to.x, to.y, 90, bo.color, 0.6 * (1 - k * 4));
    }
    for (const r of this.rings) {
      const k = clamp((t - r.t0) / r.dur, 0, 1); if (k <= 0) continue;
      ctx.beginPath(); ctx.arc(r.x, r.y, 8 + r.r * ease(k), 0, TAU); ctx.strokeStyle = hexA(r.color, 0.8 * (1 - k)); ctx.lineWidth = 3 * (1 - k) + 0.5; ctx.stroke();
    }
    for (const s of this.sparks) {
      const k = t - s.t0; if (k < 0) continue;
      const x = s.x + s.vx * k, y = s.y + s.vy * k + 300 * k * k, a = 1 - k / s.dur;
      ctx.fillStyle = hexA(s.color === C.red ? "#ffb3ba" : s.color, Math.max(0, a)); ctx.fillRect(x - 1.2, y - 1.2, 2.4, 2.4);
    }
    if (this.flash && t - this.flash.t0 < 0.35) {
      ctx.fillStyle = hexA(this.flash.color, 0.16 * (1 - (t - this.flash.t0) / 0.35));
      ctx.fillRect(-200, -200, VW + 400, SPINE_TOP + 200);
    }
    for (const f of this.floaters) {
      const k = (t - f.t0) / f.dur, a = k < 0.1 ? k / 0.1 : 1 - Math.max(0, (k - 0.6) / 0.4);
      ctx.font = FONT.display(f.size, 700); ctx.textAlign = "center"; ctx.textBaseline = "middle";
      const y = f.y - k * 34;
      ctx.lineWidth = 5; ctx.strokeStyle = `rgba(4,7,13,${0.9 * a})`; ctx.strokeText(f.text.toUpperCase(), f.x, y);
      ctx.fillStyle = hexA(f.color.length === 7 ? f.color : "#ffffff", a); ctx.fillText(f.text.toUpperCase(), f.x, y);
    }
    for (const st of this.stamps) {
      if (t < st.t0) continue;
      const k = (t - st.t0) / st.dur, a = k > 0.82 ? (1 - k) / 0.18 : 1;
      const p = clamp((t - st.t0) / 0.28, 0, 1), sc = 1 + 1.6 * Math.pow(1 - p, 3);
      ctx.save(); ctx.translate(st.x, st.y); ctx.rotate(st.rot); ctx.scale(sc, sc); ctx.globalAlpha = Math.max(0, a) * (0.35 + 0.65 * p);
      ctx.font = FONT.mono(12, 700); const subW = st.sub ? ctx.measureText(st.sub).width : 0;
      ctx.font = FONT.display(30, 700); ctx.textAlign = "center"; ctx.textBaseline = "middle";
      const w = Math.max(ctx.measureText(st.text).width, subW) + 30, h = st.sub ? 62 : 46;
      ctx.shadowColor = st.color; ctx.shadowBlur = 24;
      roundRect(ctx, -w / 2, -h / 2, w, h, 7); ctx.fillStyle = "rgba(6,4,8,.86)"; ctx.fill();
      ctx.lineWidth = 4; ctx.strokeStyle = st.color; ctx.stroke(); ctx.shadowBlur = 0;
      roundRect(ctx, -w / 2 + 5, -h / 2 + 5, w - 10, h - 10, 4); ctx.lineWidth = 1.2; ctx.stroke();
      ctx.fillStyle = st.color; ctx.fillText(st.text, 0, st.sub ? -9 : 1);
      if (st.sub) { ctx.font = FONT.mono(12, 700); ctx.fillStyle = "#fff"; ctx.fillText(st.sub, 0, 17); }
      ctx.restore();
      if (p < 1 && p > 0) for (let q = 0; q < 2; q++) glowCircle(ctx, st.x, st.y, 120 * (1 - p), st.color, 0.25);
    }
    for (const bn of this.banners) {
      const k = (t - bn.t0) / bn.dur, a = k < 0.08 ? k / 0.08 : k > 0.85 ? (1 - k) / 0.15 : 1;
      const y = 250, w = 640, h = bn.sub ? 92 : 64;
      ctx.save(); ctx.globalAlpha = Math.max(0, a);
      const g = ctx.createLinearGradient(652 - w / 2, 0, 652 + w / 2, 0);
      g.addColorStop(0, hexA(bn.color, 0)); g.addColorStop(0.2, "rgba(4,10,14,.9)"); g.addColorStop(0.8, "rgba(4,10,14,.9)"); g.addColorStop(1, hexA(bn.color, 0));
      ctx.fillStyle = g; ctx.fillRect(652 - w / 2, y - h / 2, w, h);
      ctx.fillStyle = bn.color; ctx.fillRect(652 - w * 0.35, y - h / 2, w * 0.7, 2); ctx.fillRect(652 - w * 0.35, y + h / 2 - 2, w * 0.7, 2);
      ctx.shadowColor = bn.color; ctx.shadowBlur = 20;
      ctx.font = FONT.display(40, 700); ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillStyle = bn.color;
      ctx.fillText(bn.text, 652, bn.sub ? y - 12 : y);
      ctx.shadowBlur = 0;
      if (bn.sub) { ctx.font = FONT.mono(15, 600); ctx.fillStyle = "#e8eef8"; ctx.fillText(bn.sub, 652, y + 26); }
      ctx.restore();
    }
  }
}

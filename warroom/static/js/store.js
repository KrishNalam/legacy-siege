// Client-side mirror of MongoDB, fed ONLY by the /ws change stream (snapshot + change events).
// It holds no truth of its own: on reconnect it is rebuilt from a fresh snapshot.

const MAP_COLLS = {
  systems: "systems", agents: "agents", incidents: "incidents", tasks: "tasks", chaos_events: "chaos",
  approvals: "approvals", upgrade_requests: "upgrades", ledger: "ledger", playbooks: "playbooks", runs: "runs",
  roster: "roster",  // Comms live roster (SWARM_COMMS=1)
};
const LEDGER_KEY = (d) => d.action_id || d._id;

export const state = {
  systems: new Map(), agents: new Map(), incidents: new Map(), tasks: new Map(), chaos: new Map(),
  approvals: new Map(), upgrades: new Map(), ledger: new Map(), playbooks: new Map(), runs: new Map(), roster: new Map(),
  thoughts: [], logs: {}, current: null, counts: {}, opsHistory: [], opsPerSec: 0,
  connected: false, serverOffsetMs: 0, meta: {}, lastCheckpoint: null,
};

const listeners = new Map();
export function on(evt, fn) { (listeners.get(evt) || listeners.set(evt, []).get(evt)).push(fn); }
function emit(evt, ...args) { for (const fn of listeners.get(evt) || []) { try { fn(...args); } catch (e) { console.error(evt, e); } } }

export const serverNow = () => Date.now() + state.serverOffsetMs;
/** Replace raw ISO datetimes inside agent / system text with a short local clock time (projector-friendly). */
const ISO_RE = /\d{4}-\d\d-\d\d[T ](\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)?/g;
export function prettyTime(s) {
  return String(s ?? "").replace(ISO_RE, (m, hms, tz) => {
    if (!tz) return hms;
    const d = new Date(m.replace(" ", "T"));
    return isNaN(d) ? hms : d.toLocaleTimeString([], { hour12: false });
  });
}
export const ts = (iso) => (iso ? Date.parse(iso) : NaN);

function resetMaps() {
  for (const k of Object.values(MAP_COLLS)) state[k] = new Map();
  state.thoughts = []; state.logs = {};
}

function applySnapshot(s) {
  resetMaps();
  const fill = (key, docs, keyFn = (d) => d._id) => { for (const d of docs || []) state[key].set(keyFn(d), d); };
  fill("systems", s.systems); fill("agents", s.agents); fill("incidents", s.incidents); fill("tasks", s.tasks);
  fill("chaos", s.chaos_events); fill("approvals", s.approvals); fill("upgrades", s.upgrades);
  fill("ledger", s.ledger, LEDGER_KEY); fill("playbooks", s.playbooks); fill("runs", s.runs); fill("roster", s.roster);
  state.thoughts = s.thoughts || [];
  state.logs = s.logs || {};
  state.current = s.current || null;
  state.counts = s.counts || {};
  state.meta = s.meta || {};
  if (s.meta?.server_time) state.serverOffsetMs = Date.parse(s.meta.server_time) - Date.now();
  emit("snapshot", s);
}

function applyChange(m) {
  const { coll, op, key, doc } = m;
  if (coll === "chaos_events" && doc?.kind === "decision") return;  // Red Planner / Runner decision log, not a strike
  let prev = null;
  const mk = MAP_COLLS[coll];
  if (mk) {
    const k = coll === "ledger" && doc ? LEDGER_KEY(doc) : key;
    prev = state[mk].get(k) || null;
    if (op === "delete") {
      if (coll === "ledger") { for (const [lk, d] of state.ledger) if (d._id === key) state.ledger.delete(lk); }
      else state[mk].delete(k);
    } else if (doc) state[mk].set(k, doc);
  } else if (coll === "thoughts" && op === "insert" && doc) {
    state.thoughts.push(doc); if (state.thoughts.length > 120) state.thoughts.splice(0, state.thoughts.length - 120);
  } else if (coll === "system_logs" && op === "insert" && doc?.system) {
    const arr = (state.logs[doc.system] ||= []); arr.push(doc); if (arr.length > 6) arr.shift();
  } else if (coll === "stats" && key === "current") {
    prev = state.current; state.current = doc;
  } else if (coll === "checkpoints" && doc) {
    state.lastCheckpoint = doc;
  }
  if (op === "delete" && !mk && coll === "thoughts") state.thoughts = state.thoughts.filter((t) => t._id !== key);
  emit("change", m, prev);
  emit(`change:${coll}`, doc, prev, m);
}

function applyTick(m) {
  state.counts = { ...state.counts, ...(m.counts || {}) };
  state.opsPerSec = m.ops_per_sec || 0;
  state.opsHistory.push(state.opsPerSec); if (state.opsHistory.length > 90) state.opsHistory.shift();
  emit("tick", m);
}

// The page may be served under a path prefix (e.g. a reverse proxy at /p/8700/), so every
// URL is resolved against the page's own directory instead of the host root.
export const BASE = location.pathname.endsWith("/") ? location.pathname : location.pathname.replace(/[^/]*$/, "");
const rel = (path) => BASE + path.replace(/^\//, "");

let ws = null, retry = 500;
export function connect() {
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${rel("/ws")}`;
  ws = new WebSocket(url);
  ws.onopen = () => { retry = 500; };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "snapshot") { state.connected = true; applySnapshot(m); emit("status", "live"); }
    else if (m.type === "change") applyChange(m);
    else if (m.type === "tick") applyTick(m);
    else if (m.type === "status") emit("status", m.stream);
  };
  ws.onclose = () => {
    state.connected = false; emit("status", "down");
    setTimeout(connect, retry); retry = Math.min(retry * 2, 5000);
  };
}

// ------------------------------------------------------------------ derived selectors
export function currentIncident() {
  let best = null;
  for (const i of state.incidents.values()) {
    if (i.active) { if (!best || !best.active || ts(i.opened_at) > ts(best.opened_at)) best = i; }
    else if (!best || (!best.active && ts(i.opened_at) > ts(best.opened_at))) best = i;
  }
  return best;
}
export function currentRun() {
  const id = state.current?.run_id;
  return (id && state.runs.get(id)) || null;
}
export function pendingApprovals() {
  return [...state.approvals.values()].filter((a) => a.status === "pending").sort((a, b) => ts(a.ts) - ts(b.ts));
}
export function latestUpgrade() {
  return [...state.upgrades.values()].sort((a, b) => ts(a.ts) - ts(b.ts)).pop() || null;
}

// ------------------------------------------------------------------ API
export async function api(method, path, body) {
  const r = await fetch(rel(path), { method, headers: body ? { "content-type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  let data = null; try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || `${r.status}`);
  return data;
}

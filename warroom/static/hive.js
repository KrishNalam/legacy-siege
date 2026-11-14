// Legacy Siege · hive (stage) view. The agents are the star: who is doing what, the AI's suggestion vs the
// checker's verdict, the kill and the takeover, and one round-1-vs-round-2 comparison.
// Same data source as the classic view: the /ws change stream (store.js) + the existing REST endpoints.
// Read-only except the judge buttons. Everything is derived client-side from MongoDB documents.
import { state, on, connect, api, currentRun, pendingApprovals, latestUpgrade, serverNow, ts, prettyTime } from "./js/store.js";

const $ = (s) => document.querySelector(s);
const XMARK = '<svg viewBox="0 0 16 16" width="0.9em" height="0.9em" aria-hidden="true"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" fill="none"/></svg>';
const esc = (s) => prettyTime(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const secs = (s) => (s == null || isNaN(s) ? "—" : s >= 100 ? `${Math.round(s)} s` : `${(+s).toFixed(s < 10 ? 1 : 0)} s`);
const ago = (iso) => { const d = (serverNow() - ts(iso)) / 1000; return isNaN(d) ? "" : d < 60 ? `${Math.max(0, Math.round(d))}s ago` : `${Math.round(d / 60)}m ago`; };
const clock = (iso) => (iso ? new Date(ts(iso)).toLocaleTimeString([], { hour12: false }) : "");

// ------------------------------------------------------------------ plain-English vocabulary
const SYS = {
  portal: { name: "Portal", role: "the bank website customers use" },
  unix: { name: "Unix", role: "old server that moves the files" },
  mainframe: { name: "Mainframe", role: "core ledger, where the money lives" },
};
const CHAIN = ["portal", "unix", "mainframe"];
const sn = (s) => SYS[s]?.name || (s ? s[0].toUpperCase() + s.slice(1) : "?");
const OPS = {
  quarantine_record: "set aside the bad record", restart_batch: "restart the nightly batch",
  rerun_transfer: "re-send the transfer files", refresh_cache: "refresh customer balances",
  map_field: "point the portal at the renamed field", clear_sessions: "clear stuck sessions",
  kill_sessions: "clear stuck sessions", purge_spool: "free up disk space", free_disk: "free up disk space",
  restart_cron: "restart the scheduled jobs", chmod_config: "lock down the open config file",
  patch_sqli: "patch the security hole", rotate_credential: "change the default password",
};
const op = (o) => OPS[o] || String(o || "a fix").replace(/_/g, " ");
const Op = (o) => { const s = op(o); return s[0].toUpperCase() + s.slice(1); };
const FAULT = {
  batch_abort: "a corrupted record crashed its nightly batch", schema_drift: "a vendor patch renamed a data field",
  session_leak: "stuck sessions piled up", disk_fill: "its disk filled up", cron_stop: "its scheduled jobs stopped",
  sqli_exploit: "an attacker is using a security hole", config_exploit: "an attacker changed an open config file",
  credential_exploit: "an attacker is using a default password", slow_apply: "its changes apply late",
};
const ROLE = {
  queen: { c: "#ffc94d", tag: "coordinator" }, scout: { c: "#5cc8ff", tag: "investigator" },
  worker: { c: "#8fb0ff", tag: "fixer" }, guard: { c: "#34e39a", tag: "the checker" },
  nurse: { c: "#ff9ccf", tag: "takes over dead bees’ jobs" }, sentinel: { c: "#7fd8ff", tag: "watches system health" },
  gardener: { c: "#a6f0a0", tag: "writes permanent code fixes" }, waggle: { c: "#6ee7d8", tag: "shares what was learned" },
};
const ROLE_ORDER = ["queen", "scout", "worker", "guard", "nurse", "sentinel", "gardener", "waggle"];
const NAMES = { "worker-web-1": "Portal worker", "worker-unix-1": "Unix worker", "worker-mainframe-1": "Mainframe worker",
  "queen-1": "Queen", "queen-2": "Backup queen", "guard-1": "Guard", "sentinel-1": "Lookout", "gardener-1": "Gardener",
  "scout-1": "Scout", "nurse-1": "Nurse", "waggle-1": "Messenger" };
function agentName(id) {
  if (!id) return "a bee";
  const a = state.agents.get(id);
  if (a?.role === "queen") {
    if (a.status === "dead" || a.status === "standby") return a.status === "dead" ? "Queen" : "Backup queen";
    return [...state.agents.values()].some((q) => q.role === "queen" && q.status === "dead") ? "New queen" : "Queen";
  }
  if (NAMES[id]) return NAMES[id];
  const m = id.match(/^(\w+?)-(?:(\w+)-)?(\d+)$/);
  if (!m) return id;
  const base = { sentinel: "Lookout", waggle: "Messenger" }[m[1]] || m[1][0].toUpperCase() + m[1].slice(1);
  return m[2] ? `${sn(m[2] === "web" ? "portal" : m[2])} worker ${m[3]}` : `${base} ${m[3]}`;
}
const roleOf = (id) => state.agents.get(id)?.role || String(id || "").split("-")[0];

// ------------------------------------------------------------------ thought → plain words
// Returns {text, story} (story = worth a Timeline line) or null to skip. `text` may contain trusted markup
// built here; anything from a document is escaped.
function stepByAction(inc, aid) {
  for (const i of inc ? [inc] : state.incidents.values()) for (const s of i.steps || []) if ((s.attempts || []).includes(aid) || s.action_id === aid) return s;
  return null;
}
function translate(t) {
  const x = String(t.text || ""), k = t.kind, inc = state.incidents.get(t.incident);
  let m;
  const S = (s) => esc(sn(String(s).toLowerCase()));
  // --- headline moments (Timeline + agent row)
  if ((m = x.match(/^Opened (INC-\d+): (\w+) (\w+)/))) return { text: `Alarm: ${S(m[2])} is ${m[3] === "down" ? "down" : "in trouble"}. Opened a case.`, story: true };
  if (/^Memory is empty|^No usable playbook/.test(x)) return { text: "Never seen this before, so we trace it step by step.", story: true };
  if ((m = x.match(/^Recall hit \S+ \(score ([\d.]+)/))) return { text: `<span class="g">Seen this before!</span> Reusing last night’s fix and skipping the search.`, story: true };
  if (/shows the remembered fault/.test(x)) return { text: "Confirmed: it’s the same problem as last night.", story: true };
  if ((m = x.match(/^(\w+) \/health says (\w+):.*?(\w+) blames (\w+)/i))) return { text: `${S(m[1])} is unhealthy but blames <b>${S(m[4])}</b>. Following the trail.`, story: true };
  if ((m = x.match(/^Root cause after (\d+) hops?.*?\[(\w+) on (\w+)\]/))) return { text: `Found it after ${m[1]} check${m[1] === "1" ? "" : "s"}: the real problem is the <b>${S(m[3])}</b> (${esc(FAULT[m[2]] || m[2])}).`, story: true };
  if ((m = x.match(/^Root cause.*?\[(\w+) on (\w+)\]/))) return { text: `The real problem is the <b>${S(m[2])}</b> (${esc(FAULT[m[1]] || m[1])}).`, story: true };
  if ((m = x.match(/^Plan, (\d+) steps?/))) return { text: `Made a ${m[1]}-step fix plan.`, story: true };
  if (k === "reject") { const s = t.system || stepByAction(inc, (x.match(/INC-\S+-a\d+/) || [])[0])?.system; return { text: `Re-checked the ${S(s || "system")} → <b>it LIED</b>: said “done”, nothing changed. Rejected.`, story: true }; }
  if ((m = x.match(/^Verified (\S+-a(\d+))/))) { const st = stepByAction(inc, m[1]); const n = +m[2]; return { text: `${n > 1 ? "Second try is real: " : "Confirmed: "}“${esc(op(st?.op))}” really happened on the ${S(st?.system || t.system || "system")} <span class="g">✓</span>`, story: true }; }
  if ((m = x.match(/^(\w+) on (\w+) is destructive/))) return { text: `“${esc(Op(m[1]))}” is risky. Asking a human first.`, story: true };
  if (/^Beekeeper approved/.test(x)) return { text: "A human approved. Going ahead.", story: true };
  if (/^Beekeeper denied|denied APR/.test(x)) return { text: "A human said no. Holding back.", story: true };
  if ((m = x.match(/^(\S+) stopped heartbeating/))) return { text: `${esc(agentName(m[1]))} went silent: marked dead, its job is up for grabs.`, story: true };
  if ((m = x.match(/^Took over (\S+) from (\S+) after (\d+)s/))) return { text: `<span class="g">Took over</span> ${esc(agentName(m[2]))}’s job ${m[3]} s after it died, from its saved progress.`, story: true };
  if (/^Loaded .*checkpoint/.test(x)) return { text: "Loaded the dead bee’s saved progress from MongoDB. Finished steps are not repeated.", story: false };
  if (/already took effect/.test(x)) return { text: "The dead bee’s last action had already worked, so it is not sent twice.", story: true };
  if (/resending the SAME action id/.test(x)) return { text: "Re-sending the same action safely (the system ignores duplicates).", story: false };
  if ((m = x.match(/^(\S+) went silent — (\S+) takes the queen lease/))) return { text: `${esc(agentName(m[1]))} went silent. <span class="g">I’m queen now</span> and carry on.`, story: true };
  if ((m = x.match(/^Resolved (INC-\d+) in (\d+)s/))) return { text: `<span class="g">All green.</span> Fixed in ${m[2]} s. Lesson saved for next time.`, story: true };
  if ((m = x.match(/resolved (\d+) times .*filed/))) return { text: `Same failure ${m[1]} times → asked the Gardener for a permanent code fix.`, story: true };
  if ((m = x.match(/proposed a (\d+)-line diff/))) return { text: `Wrote a ${m[1]}-line code fix.`, story: true };
  if (/running test_.*replaying/.test(x)) return { text: "Testing the fix in a sandbox, replaying the attack.", story: false };
  if (/sent to the Beekeeper/.test(x)) return { text: "Tests pass. Waiting for a human OK to deploy.", story: true };
  if ((m = x.match(/deployed - (\w+) version (\d+) -> (\d+)/))) return { text: `Deployed: ${S(m[1])} upgraded v${m[2]} → v${m[3]}.`, story: true };
  if (/replayed .*watching/.test(x)) return { text: "Replaying the original attack to prove the fix…", story: true };
  if (/proven - /.test(x)) return { text: `<span class="g">Proven:</span> the attack came back and nothing broke.`, story: true };
  if (/ rejected: /.test(x) && k === "upgrade") return { text: "Code fix rejected.", story: true };
  if ((m = x.match(/renamed (\S+) → (\S+) · mapping learned v(\d+)/))) return { text: `Learned the renamed field: “${esc(m[1])}” is now “${esc(m[2])}”. Every bee uses it from now on.`, story: true };
  // --- small moments (agent row only)
  if ((m = x.match(/^Reading (\w+) \/health/))) return { text: `Checking the ${S(m[1])}…` };
  if ((m = x.match(/^(\w+) reports (\w+) and blames (\w+)/))) return { text: `${S(m[1])} says it’s ${esc(m[2])} and blames ${S(m[3])}.` };
  if ((m = x.match(/^(\w+) reports up, blaming nothing/))) return { text: `${S(m[1])} says it’s fine. Digging into its logs…` };
  if ((m = x.match(/^(\w+) \/health says up:/))) return { text: `${S(m[1])} claims it’s fine, but its logs show the crash.` };
  if ((m = x.match(/^Sending (\w+)(?:\(.*?\))? to (\w+)/))) return { text: `Doing it: ${esc(op(m[1]))} on the ${S(m[2])}.` };
  if ((m = x.match(/^(\w+) answered accepted=True applied=True/))) return { text: `${S(m[1])} replied “done”. Waiting for the checker…` };
  if (/^Guard rejected/.test(x)) return { text: "The checker rejected it. Trying again." };
  if ((m = x.match(/^Reading back (\w+) to check (\w+)/))) return { text: `Re-checking the ${S(m[1])}: did “${esc(op(m[2]))}” really happen?` };
  if ((m = x.match(/^Hop (\d+): sending .* to examine (\w+)/))) return { text: `Check ${m[1]}: sending a worker to the ${S(m[2])}.` };
  if ((m = x.match(/^Dispatching (\w+) to/))) return { text: `Assigned the next step: ${esc(op(m[1]))}.` };
  if (/^Every step verified/.test(x)) return { text: "Every step confirmed by the checker. Waiting for green." };
  if (/blast radius settle/.test(x)) return { text: "Waiting a moment to see what else breaks." };
  if (/Checking the hive's memory/.test(x)) return { text: "Asking the shared memory: have we seen this before?" };
  if (/^Claimed INC/.test(x)) return { text: "Took the case." };
  if ((m = x.match(/^Assigning INC-\d+ to ([\w-]+)/))) return { text: `Gave the case to the ${esc(agentName(m[1]))}.` };
  if (/^Holding the queen lease/.test(x)) return { text: "In charge. Goal: every system green." };
  if ((m = x.match(/^INC-\d+ opened \((.*?)\)/))) return { text: `New problem (${esc(m[1].split(/,\s*/).map(sn).join(", "))}). Goal stands: all green.` };
  if ((m = x.match(/^(\w+) also unhealthy/))) return { text: `${S(m[1])} is hurt too: same case.` };
  if (/needs your approval/.test(x)) return { text: "Asked the human for an OK." };
  if (/online in the workshop/.test(x)) return { text: "Ready in the workshop." };
  if (/^picked up /.test(x)) return { text: "Starting a permanent code fix." };
  if (/reading systems\//.test(x)) return { text: "Reading the old code." };
  if (/hot-reloading/.test(x)) return { text: "Installing the new code." };
  if (/marked \d+ playbook\(s\) stale/.test(x)) return { text: "Old notes marked out of date after the upgrade." };
  if (/^reverted systems/.test(x)) return { text: "Put the original code back (fresh demo)." };
  return { text: esc(x.replace(/\s*\((?:lease|generation)[^)]*\)/g, "").replace(/\bINC-\d+(-\w+)*\b/g, "the case").slice(0, 140)) };
}

// chaos events → the attacker's lines
const ATTACK = {
  lying_success: (e) => `Rigged the ${sn(e.target)} to lie: its next fix will say “done” without doing it.`,
  batch_abort: (e) => `Slipped a corrupted record into the ${sn(e.target)}’s nightly batch.`,
  schema_drift: (e) => `A “vendor patch” silently renamed a data field on ${sn(e.target)}. It still says it’s healthy.`,
  session_leak: (e) => `Flooded the ${sn(e.target)} with stuck sessions.`, disk_fill: (e) => `Filled up the ${sn(e.target)}’s disk.`,
  cron_stop: (e) => `Stopped the ${sn(e.target)}’s scheduled jobs.`, slow_apply: (e) => `Made the ${sn(e.target)} apply changes late.`,
  sqli_exploit: (e) => `Attacked a security hole on the ${sn(e.target)}.`, config_exploit: (e) => `Tampered with an open config file on ${sn(e.target)}.`,
  agent_kill: (e) => (e.params?.selector?.agent ? `Going after the ${agentName(e.params.selector.agent)} to kill it…` : "Hunting a busy worker bee to kill mid-job…"),
};
function attackLines() {
  const out = [];
  for (const e of state.chaos.values()) {
    const f = ATTACK[e.fault];
    out.push({ id: e._id, ts: e.ts, who: "Attacker", c: "#ff5a5f", text: esc(f ? f(e) : `Attack: ${e.fault} on ${sn(e.target)}.`) });
    if (e.fault === "agent_kill" && e.result?.agent) out.push({ id: `${e._id}:hit`, ts: e.done_at || e.ts, who: "Attacker", c: "#ff5a5f", text: `<b>Killed the ${esc(agentName(e.result.agent))}</b> in the middle of a job (a real process kill).`, kill: e.result.agent });
    else if (e.fault === "agent_kill" && e.status === "failed") out.push({ id: `${e._id}:miss`, ts: e.done_at || e.ts, who: "Attacker", c: "#ff5a5f", text: "Found no busy bee to kill this time." });
  }
  return out;
}

// ------------------------------------------------------------------ scope helpers
const runId = () => state.current?.run_id || null;
function nightIncidents(rid = runId()) { return [...state.incidents.values()].filter((i) => !rid || i.run_id === rid).sort((a, b) => ts(a.opened_at) - ts(b.opened_at)); }
function focusIncident() {
  const list = nightIncidents();
  return list.find((i) => i.active) || list[list.length - 1] || null;
}
const runStartedAt = () => ts(state.current?.ts) || ts(currentRun()?.started_at);

// ------------------------------------------------------------------ beats
const lit = new Map(); // beat id -> detail (sticky until a full reset)
let lastBeatState = {};
function computeBeats() {
  const incs = [...state.incidents.values()];
  const thoughts = state.thoughts;
  const b = {};
  // 1 cascade traced
  const casc = incs.find((i) => (i.diagnosis_path || []).length >= 3 && i.root_cause_system);
  const cur = focusIncident();
  if (casc) b.cascade = { s: "done", d: (casc.diagnosis_path || []).map(sn).join(" → ") };
  else if (cur?.active && (cur.diagnosis_path || []).length) b.cascade = { s: "active", d: `${cur.diagnosis_path.map(sn).join(" → ")} → …` };
  // 2 lie caught
  const lieInc = incs.find((i) => (i.steps || []).some((s) => s.status === "verified" && (s.attempts || []).length > 1));
  const rej = thoughts.find((t) => t.kind === "reject");
  if (lieInc || rej) { const s = (lieInc?.steps || []).find((x) => (x.attempts || []).length > 1); b.lie = { s: "done", d: `${sn(s?.system || rej?.system || "mainframe")} said “done”. It wasn’t.` }; }
  // 3 kill + takeover
  const ho = incs.flatMap((i) => i.handoffs || []).pop();
  const qho = [...thoughts].reverse().find((t) => /takes the queen lease/.test(t.text || ""));
  const killed = [...state.chaos.values()].filter((e) => e.fault === "agent_kill" && e.result?.agent).pop();
  if (ho) b.kill = { s: "done", d: `${agentName(ho.to)} took over from ${agentName(ho.from)}` };
  else if (qho) b.kill = { s: "done", d: `${agentName(qho.agent)} took over as queen` };
  else if (killed) b.kill = { s: "active", d: `${agentName(killed.result.agent)} killed · waiting for a nurse` };
  // 4 round 2 faster
  const r = rounds();
  if (r[1] && r[2] && r[2].ttr < r[1].ttr) b.round2 = { s: "done", d: `${secs(r[1].ttr)} → ${secs(r[2].ttr)} (${(r[1].ttr / r[2].ttr).toFixed(1)}× faster)` };
  else if (incs.some((i) => i.active && i.round === 2)) b.round2 = { s: "active", d: "Night 2 running…" };
  // 5 permanent upgrade
  const u = latestUpgrade();
  if (u?.status === "proven") b.upgrade = { s: "done", d: `${sn(u.system)} v${u.version_before ?? 1} → v${u.version_after ?? 2}, attack shrugged off` };
  else if (u && u.status !== "rejected") b.upgrade = { s: "active", d: { deployed: "deployed · proving it…", awaiting_approval: "waiting for a human OK" }[u.status] || "Gardener writing the code fix…" };
  // (+) renamed field (schema drift), only once it shows up
  const driftSeen = [...state.chaos.values()].some((e) => e.fault === "schema_drift") || incs.some((i) => i.fault_class === "schema_drift" || i.ai?.kind === "schema_mapping") || driftMappings.size;
  if (driftSeen) {
    const learned = thoughts.find((t) => /mapping learned/.test(t.text || "")) || [...driftMappings.values()].pop();
    const mapStep = incs.flatMap((i) => i.steps || []).find((s) => s.op === "map_field" && s.status === "verified");
    if (learned || mapStep) {
      const mm = (learned?.text || "").match(/renamed (\S+) → (\S+)/);
      const from = mm?.[1] || learned?.previous, to = mm?.[2] || learned?.source || mapStep?.params?.source;
      b.drift = { s: "done", d: from && to ? `“${from}” → “${to}”, learned` : "mapping learned + verified" };
    } else b.drift = { s: "active", d: "a field was renamed · bees adapting…" };
  }
  for (const [k, v] of Object.entries(b)) if (v.s === "done") lit.set(k, v.d);
  for (const k of lit.keys()) if (!b[k] || b[k].s !== "done") b[k] = { s: "done", d: lit.get(k) };
  return b;
}
const BEATS = [
  ["cascade", "Cascade traced", "follow the trail to the real cause"],
  ["lie", "Lie caught", "the checker catches a fake “done”"],
  ["kill", "Kill + takeover", "a bee dies, another resumes its job"],
  ["round2", "Round 2 faster", "the bees remember last night"],
  ["upgrade", "Permanent fix", "same failure twice → code upgrade"],
  ["drift", "Renamed field", "a data field renamed, bees adapt"],
];
function renderBeats() {
  const b = computeBeats();
  const el = $("#beats");
  const shown = BEATS.filter(([id]) => id !== "drift" || b.drift);
  el.innerHTML = shown.map(([id, title, hint], i) => {
    const v = b[id];
    return `<div class="beat ${v?.s || ""} ${id === "drift" ? "drift" : ""}" data-beat="${id}"><span class="n">${v?.s === "done" ? "✓" : i + 1}</span>
      <div><div class="bt">${esc(title)}</div><div class="bs">${esc(v?.d || hint)}</div></div></div>`;
  }).join("");
  for (const [id] of shown) {
    if (b[id]?.s === "done" && lastBeatState[id] && lastBeatState[id] !== "done") el.querySelector(`[data-beat="${id}"]`)?.classList.add("flash");
  }
  lastBeatState = Object.fromEntries(shown.map(([id]) => [id, b[id]?.s || "none"]));
}

// ------------------------------------------------------------------ rounds (the one comparison)
function rounds() {
  const by = {};
  for (const i of state.incidents.values()) {
    if (i.round == null || i.status !== "resolved" || i.metrics?.ttr_s == null) continue;
    const r = (by[i.round] ||= { n: 0, ttr: 0, hops: 0, recall: false });
    r.n++; r.ttr += i.metrics.ttr_s; r.hops += i.metrics.hops ?? (i.diagnosis_path || []).length; r.recall ||= !!i.recall?.used;
  }
  for (const r of Object.values(by)) { r.ttr /= r.n; r.hops /= r.n; }
  // prefer the server's aggregation when it has a round we don't hold (snapshot keeps 10 incidents)
  for (const s of score?.rounds || []) if (!by[s.round] && s.avg_ttr_s) by[s.round] = { n: s.resolved, ttr: s.avg_ttr_s, hops: s.avg_hops, recall: s.recall_used > 0 };
  return by;
}
let score = null;
async function refreshScore() { try { score = await api("GET", "/api/scorecard"); render(); } catch { /* optional */ } }
function renderMeter() {
  const r = rounds(), live = [...state.incidents.values()].find((i) => i.active && i.round != null);
  const rows = [1, 2].map((n) => {
    const done = r[n]?.ttr, isLive = !done && live?.round === n;
    const v = done ?? (isLive ? (serverNow() - ts(live.opened_at)) / 1000 : null);
    return { n, v, isLive };
  });
  const max = Math.max(...rows.map((x) => x.v || 0), 1);
  const x = r[1] && r[2] && r[2].ttr < r[1].ttr ? `${(r[1].ttr / r[2].ttr).toFixed(1)}× faster` : "";
  const sub = r[1] && r[2] ? `Night 2 remembered the fix: ${Math.round(r[2].hops)} check${Math.round(r[2].hops) === 1 ? "" : "s"} instead of ${Math.round(r[1].hops)}.`
    : r[1] ? "Now run Night 2: same attack, but the bees remember." : "Same attack two nights in a row. Do the bees get faster?";
  $("#meter").innerHTML = `<h2>Time to fix <span class="x">${esc(x)}</span></h2><div class="bars">${rows.map((row) => `
    <div class="bar ${row.n === 2 ? "r2" : ""} ${row.isLive ? "live" : ""}"><span class="lbl">Night ${row.n}</span>
      <span class="trk"><span class="fill" style="width:${row.v ? Math.max(3, (row.v / max) * 100) : 0}%"></span></span>
      <span class="val">${row.v != null ? secs(row.v) : "—"}</span></div>`).join("")}</div><div class="sub">${esc(sub)}</div>`;
}

// ------------------------------------------------------------------ the team (agents are the star)
const flashAt = new Map();
let prevActs = new Map();
function lastThoughtBy() {
  const m = new Map();
  for (const t of state.thoughts) if (t.agent) m.set(t.agent, t);
  return m;
}
function renderTeam() {
  const last = lastThoughtBy();
  const standbyLast = (a) => (a.status === "standby" ? 1 : 0);
  const agents = [...state.agents.values()].sort((a, b) => ROLE_ORDER.indexOf(a.role) - ROLE_ORDER.indexOf(b.role)
    || (a.role === "queen" ? standbyLast(a) - standbyLast(b) : 0) || String(a._id).localeCompare(String(b._id)));
  const seenRole = new Set();
  const show = agents.filter((a) => {
    const first = !seenRole.has(a.role); seenRole.add(a.role);
    if (a.role === "waggle" && !last.has(a._id)) return false;
    return first || a.role === "worker" || a.role === "queen" || a.status === "dead" || (a.status !== "standby" && a.status !== "idle") || last.has(a._id) && ts(last.get(a._id).ts) > runStartedAt();
  });
  const rid = runId();
  const lastAttack = attackLines().filter((l) => !rid || state.chaos.get(l.id.split(":")[0])?.run_id === rid).sort((a, b) => ts(a.ts) - ts(b.ts)).pop();
  const cur = focusIncident();
  const hero = new Set();
  // who matters right now: the agent of the latest thought, a just-killed bee, a nurse that just took over
  const newest = state.thoughts[state.thoughts.length - 1];
  if (newest && serverNow() - ts(newest.ts) < 6000) hero.add(newest.agent);
  const rows = [];
  rows.push(`<li class="ag attacker ${lastAttack && serverNow() - ts(lastAttack.ts) < 6000 ? "hero" : ""}">${avatar("#ff5a5f", true)}<div><div class="nm">Chaos Monkey</div><div class="rl">the attacker (us)</div></div>
    <div class="act">${lastAttack ? lastAttack.text : "Waiting for the night to start."}</div><span class="tg">${lastAttack ? esc(ago(lastAttack.ts)) : "ready"}</span></li>`);
  for (const a of show) {
    const t = last.get(a._id), role = ROLE[a.role] || { c: "#b9c4d6", tag: a.role };
    let tr = t && ts(t.ts) >= runStartedAt() - 5000 ? translate(t) : null;
    // a stale in-between line ("waiting for the checker…") after the night is over would mislead: show idle instead
    if (tr && !tr.story && !cur?.active && serverNow() - ts(t.ts) > 30000) tr = null;
    let act, tag, cls;
    if (a.status === "dead") {
      const job = a.task || a.incident;
      act = `<b>KILLED</b> mid-job${job ? "" : ""}. ${tr ? `Last words: ${tr.text}` : ""}`; tag = "KILLED"; cls = "dead";
    } else if (tr) { act = tr.text; tag = ago(t.ts); cls = a.task || a.phase || serverNow() - ts(t.ts) < 8000 ? "busy" : "idle"; }
    else if (a.status === "standby") { act = a.role === "queen" ? "On standby, ready to take over if the queen dies." : a.role === "nurse" ? "On standby, ready to take over a dead bee’s job." : "On standby."; tag = "standby"; cls = "standby"; }
    else { act = a.role === "guard" ? "Waiting to re-check every fix." : a.role === "worker" ? `Waiting for a job on the ${esc(sn(a.specialty === "web" ? "portal" : a.specialty))}.` : "Waiting."; tag = "idle"; cls = "idle"; }
    if (prevActs.has(a._id) && prevActs.get(a._id) !== act) flashAt.set(a._id, performance.now());
    prevActs.set(a._id, act);
    const fl = performance.now() - (flashAt.get(a._id) || -1e9) < 900;
    const isHero = hero.has(a._id) || (a.status === "dead" && serverNow() - ts(a.last_seen || a.updated_at) < 15000);
    rows.push(`<li class="ag ${cls} ${fl ? "flash" : ""} ${isHero ? "hero" : ""}" style="--c:${role.c}" title="${esc(a._id)}">${avatar(role.c)}
      <div><div class="nm">${esc(agentName(a._id))}</div><div class="rl">${esc(role.tag)}</div></div>
      <div class="act">${act}</div><span class="tg">${esc(tag)}</span></li>`);
  }
  $("#team").innerHTML = rows.join("");
  void cur;
}
function avatar(c, attacker = false) {
  return attacker
    ? `<svg class="av" viewBox="0 0 32 32" aria-hidden="true"><path d="M16 3l11 6.5v13L16 29 5 22.5v-13z" fill="none" stroke="${c}" stroke-width="2.5"/><path d="M11 11l10 10M21 11L11 21" stroke="${c}" stroke-width="3" stroke-linecap="round"/></svg>`
    : `<svg class="av" viewBox="0 0 32 32" aria-hidden="true"><path d="M16 3l11 6.5v13L16 29 5 22.5v-13z" fill="${c}" opacity=".18" stroke="${c}" stroke-width="2.5"/><ellipse cx="16" cy="17" rx="5" ry="6.5" fill="${c}"/><path d="M11.5 15h9M11.5 19h9" stroke="#0f1625" stroke-width="1.6"/><ellipse cx="11" cy="10.5" rx="3.6" ry="2.4" fill="${c}" opacity=".55"/><ellipse cx="21" cy="10.5" rx="3.6" ry="2.4" fill="${c}" opacity=".55"/></svg>`;
}

// ------------------------------------------------------------------ the three systems (small, secondary)
const lieAt = new Map(); // system -> ms when the Guard caught it lying
function sysCaption(s, d, inc) {
  const sym = String(d?.symptom || "");
  if (inc?.active && inc.root_cause_system === s && d?.status === "up") return `Says it’s fine, but ${FAULT[inc.fault_class] || "it is the cause"}.`;
  if (d?.status === "up" || !d) return (d?.version || 1) > 1 ? `Running upgraded code (v${d.version}).` : "Working normally.";
  if (s === "portal" && /stale/i.test(sym)) return "Customers see old balances (page still loads).";
  if (s === "portal" && /unreadable|unmarshal|manifest/i.test(sym)) return "Can’t read the transfer file: a field name changed.";
  if (s === "unix" && /transfer|xfer|failed/i.test(sym)) return "File transfer from the mainframe failed.";
  if (s === "mainframe" && /ABEND|batch/i.test(sym)) return "Nightly batch crashed.";
  return prettyTime(sym).replace(/\s*\(.*?\)\s*/g, " ").slice(0, 70) || "Not healthy.";
}
function renderEstate() {
  const inc = focusIncident(), active = inc?.active ? inc : null;
  const path = active?.diagnosis_path || [];
  const out = [];
  CHAIN.forEach((s, i) => {
    const d = state.systems.get(s);
    const root = active?.root_cause_system === s;
    const st = d?.status || "?";
    const cls = root ? "bad root" : st === "up" ? "ok" : st === "down" ? "bad" : "warn";
    const word = root && st === "up" ? "CAUSE" : st === "up" ? "OK" : st === "down" ? "DOWN" : st === "degraded" ? "TROUBLE" : "…";
    const steps = (active?.steps || []).filter((x) => x.system === s);
    const flags = [];
    if (root) flags.push('<span class="flag root">ROOT CAUSE</span>');
    else if (path.includes(s)) flags.push('<span class="flag checked">CHECKED</span>');
    if (steps.some((x) => x.status === "sent")) flags.push('<span class="flag fixing">FIXING</span>');
    else if (steps.length && steps.every((x) => x.status === "verified")) flags.push('<span class="flag fixed">FIXED ✓</span>');
    if ((d?.version || 1) > 1) flags.push(`<span class="flag ver">UPGRADED v${d.version}</span>`);
    const lie = performance.now() - (lieAt.get(s) || -1e9) < 7000;
    out.push(`<div class="sys ${cls}"><div class="hd"><span class="nm">${esc(sn(s))}</span><span class="st">${word}</span></div>
      <div class="role">${esc(SYS[s].role)}</div><div class="cap">${esc(sysCaption(s, d, active))}</div><div class="flags">${flags.join("")}</div>
      ${lie ? '<div class="stamp" style="color:var(--red)">CAUGHT LYING</div>' : ""}</div>`);
    if (i < CHAIN.length - 1) {
      const nxt = CHAIN[i + 1];
      const trail = d?.blames === nxt || (path.includes(s) && path.includes(nxt));
      out.push(`<div class="link ${trail ? "trail" : ""}"><svg viewBox="0 0 64 20" aria-hidden="true"><path d="M2 10h56M50 3l8 7-8 7" fill="none" stroke="currentColor" stroke-width="3"/></svg><span>${trail ? "blames" : "depends on"}</span></div>`);
    }
  });
  $("#estate").innerHTML = out.join("");
}

// ------------------------------------------------------------------ NOW line
function nowLine() {
  if (!state.connected) return "Reconnecting to the live feed…";
  const apr = pendingApprovals()[0];
  if (apr) return apr.kind === "upgrade" ? `Waiting for <span class="a">your OK</span> to deploy the Gardener’s code fix.` : `Waiting for <span class="a">your OK</span>: a risky step needs a human.`;
  const inc = focusIncident();
  const rid = runId();
  const recentReject = [...state.thoughts].reverse().find((t) => t.kind === "reject" && serverNow() - ts(t.ts) < 7000);
  if (recentReject) return `The checker caught the ${esc(sn(recentReject.system || "mainframe"))} <b>lying</b>: it said “done”, nothing changed.`;
  const deadNow = [...state.agents.values()].find((a) => a.status === "dead" && a.role !== "nurse" && (a.task || a.incident));
  const lastHo = inc && (inc.handoffs || [])[inc.handoffs.length - 1];
  if (lastHo && serverNow() - ts(lastHo.ts || lastHo.at) < 9000) return `The ${esc(agentName(lastHo.to))} <span class="g">took over</span> the dead ${esc(agentName(lastHo.from))}’s job from its saved progress.`;
  if (deadNow && inc?.active) return `The ${esc(agentName(deadNow._id))} was <b>killed</b> mid-job. A nurse bee will take over…`;
  if (inc?.active) {
    const path = inc.diagnosis_path || [];
    if (inc.recall?.used && inc.root_cause_system) return `Seen this before → straight to the <b>${esc(sn(inc.root_cause_system))}</b>, no search needed.`;
    const steps = inc.steps || [];
    const done = steps.filter((s) => s.status === "verified").length;
    if (inc.root_cause_system && steps.length && done < steps.length) {
      const next = steps.find((s) => s.status !== "verified");
      return `Fixing (${done + 1} of ${steps.length}): ${esc(op(next?.op))} on the ${esc(sn(next?.system))}.`;
    }
    if (inc.root_cause_system && steps.length) return `All fixes confirmed by the checker. Waiting for green…`;
    const parts = path.map((s, i) => {
      const d = state.systems.get(s);
      if (s === inc.root_cause_system) return `the real cause is the <b>${esc(sn(s))}</b>`;
      if (i === 0) return `${esc(sn(s))} is ${s === "portal" && /stale/i.test(d?.symptom || "") ? "stale" : "in trouble"}`;
      return `${esc(sn(s))} is blamed`;
    });
    if (!inc.root_cause_system && path.length) parts.push(`checking ${esc(sn(CHAIN[CHAIN.indexOf(path[path.length - 1]) + 1] || "upstream"))}…`);
    return parts.length ? parts.join(" → ") : `${esc(sn(inc.system))} is in trouble. The bees are on it.`;
  }
  if (inc && inc.status === "resolved") return `<span class="g">All green.</span> Fixed in ${secs(inc.metrics?.ttr_s)}${inc.round ? ` on Night ${inc.round}` : ""}. Lesson saved for next time.`;
  const u = latestUpgrade();
  if (u && u.status === "proven" && rid && state.current?.round >= 3) return `Attacked again, and <span class="g">nothing broke</span>. The upgrade holds.`;
  if (rid && serverNow() - runStartedAt() < 20000) return `Night ${esc(state.current?.round ?? "")} has started. The attacker strikes…`;
  if (rid) return `Night ${esc(state.current?.round ?? "")}: all systems green.`;
  return "All quiet. Press <span class=\"a\">Start Night 1</span> to launch tonight’s attack.";
}

// ------------------------------------------------------------------ AI suggests / Checker says
function latestAIIncident() {
  const list = nightIncidents().filter((i) => i.ai);
  return list.find((i) => i.active) || list[list.length - 1] || null;
}
function renderJudge() {
  const aiInc = latestAIIncident(), ai = aiInc?.ai, inc = focusIncident();
  const el = $("#judge");
  if (ai) {
    const mapping = ai.kind === "schema_mapping";
    const status = !ai.status ? "done" : ai.status === "thinking" && (serverNow() - ts(ai.started_at)) / 1000 > 30 ? "offline" : ai.status;
    const ops = ai.proposed_ops || [];
    const vs = ops.map((o, i) => ({ ...o, ...((ai.verdicts || [])[i] || {}) }));
    const bad = vs.filter((v) => v.verdict === "overruled"), missed = ai.missed_ops || [];
    const label = (o) => `${esc(Op(o.op))}${o.target ? ` <span class="dimt">· ${esc(sn(o.target))}</span>` : ""}`;
    let left, right, verdict = "";
    if (status === "thinking") {
      left = `<div class="off thinking">Claude is reading the evidence the bees gathered…</div>`;
      right = `<div class="off">Waiting. Nothing the AI says runs until the checker approves it.</div>`;
    } else if (status !== "done") {
      left = `<div class="off">No answer this time.</div>`;
      right = `<div class="off">The bees carry on: the checker decides every step anyway.</div>`;
    } else {
      left = `${ai.hypothesis ? `<div class="hyp">“${esc(ai.hypothesis)}”</div>` : ""}<ul>${vs.slice(0, 4).map((o) => `<li><span>→</span><span>${label(o)}</span></li>`).join("")}</ul>`;
      right = `<ul>${vs.slice(0, 4).map((o) => o.verdict === "overruled"
        ? `<li><span class="no">${XMARK}</span><span><b class="no">Blocked</b> ${label(o)}<span class="why">${esc(o.reason || o.rule || "not safe")}</span></span></li>`
        : `<li><span class="ok">✓</span><span>OK: ${label(o)}${o.gated ? " <span class=\"why\">(needs a human OK first)</span>" : ""}</span></li>`).join("")}
        ${missed.slice(0, 2).map((m) => `<li><span class="ok">+</span><span>Adds ${label(m)}<span class="why">the AI missed this step</span></span></li>`).join("")}</ul>`;
      verdict = bad.length ? `<div class="verdict no">Checker blocked ${bad.length} AI suggestion${bad.length === 1 ? "" : "s"}</div>`
        : mapping ? `<div class="verdict ok">Checker verified the AI’s mapping by reading the data back ✓</div>`
        : ai.agrees_with_policy ? `<div class="verdict ok">AI and checker agree ✓</div>`
        : `<div class="verdict mid">${ai.root_agrees === false ? "AI blamed a different system · the checker’s rules decide" : `Checker added ${missed.length} step${missed.length === 1 ? "" : "s"} the AI missed`}</div>`;
    }
    el.innerHTML = `<h2>${mapping ? "AI field mapping" : "AI second opinion"} <span class="h2s">${esc(ai.model || "Claude")}${ai.latency_ms != null ? ` · ${(ai.latency_ms / 1000).toFixed(1)} s` : ""}</span></h2>
      <div class="cols"><div class="col ai"><h3>AI suggests…</h3>${left}</div><div class="col chk"><h3>Checker says…</h3>${right}</div></div>${verdict}`;
    return;
  }
  // no AI on this incident: the Guard's verdicts, one row per fix step
  const steps = inc?.steps || [];
  if (!inc || !steps.length) {
    el.innerHTML = `<h2>Bees act · the checker verifies</h2><div class="cols"><div class="col ai"><h3>Bees say…</h3><div class="off">“Done!” (a system can say that and be lying)</div></div>
      <div class="col chk"><h3>Checker says…</h3><div class="off">The Guard bee reads every system back. Nothing counts as fixed until it confirms.</div></div></div>`;
    return;
  }
  const rowsL = steps.slice(0, 5).map((s) => `<li><span>${s.n ?? ""}</span><span>${esc(Op(s.op))} <span class="dimt">· ${esc(sn(s.system))}</span></span></li>`).join("");
  const rowsR = steps.slice(0, 5).map((s) => {
    const n = (s.attempts || []).length;
    if (s.status === "verified") return `<li><span class="ok">✓</span><span>${n > 1 ? `<b class="no">Caught a lie</b>, then confirmed` : `Confirmed <span class="dimt">· read it back</span>`}</span></li>`;
    if (s.status === "failed") return `<li><span class="no">${XMARK}</span><span><b class="no">Rejected</b> · ${state.thoughts.some((t) => t.kind === "reject" && t.incident === inc._id && (!t.system || t.system === s.system)) ? "it lied" : "failed"}, retrying</span></li>`;
    if (s.status === "sent" || s.status === "unknown") return `<li><span class="wait">…</span><span>${n > 1 ? `<b class="no">Rejected a lie</b>, re-checking` : "Re-checking"}</span></li>`;
    if (s.status === "awaiting_approval" || s.destructive && !s.approval_id && s.status === "planned") return `<li><span class="wait">?</span><span>Needs a human OK</span></li>`;
    return `<li><span class="wait">–</span><span>Not yet</span></li>`;
  }).join("");
  const lies = steps.filter((s) => (s.attempts || []).length > 1).length;
  const allOk = steps.every((s) => s.status === "verified");
  el.innerHTML = `<h2>Bees act · the checker verifies <span class="h2s">Guard bee reads every system back</span></h2>
    <div class="cols"><div class="col ai"><h3>Bees did…</h3><ul>${rowsL}</ul></div><div class="col chk"><h3>Checker says…</h3><ul>${rowsR}</ul></div></div>
    ${lies ? `<div class="verdict no">Caught ${lies} lie${lies === 1 ? "" : "s"}: a system said “done” and wasn’t</div>` : allOk ? `<div class="verdict ok">Every step confirmed by reading the system back ✓</div>` : ""}`;
}

// ------------------------------------------------------------------ Timeline (headline moments only)
let shownStory = new Set();
function renderStory() {
  const items = attackLines();
  for (const t of state.thoughts) {
    const tr = translate(t);
    if (!tr?.story) continue;
    const role = roleOf(t.agent);
    items.push({ id: t._id, ts: t.ts, who: `${agentName(t.agent)}`, c: ROLE[role]?.c || "#b9c4d6", text: tr.text });
  }
  items.sort((a, b) => ts(b.ts) - ts(a.ts));
  const top = items.slice(0, 10);
  const el = $("#story");
  if (!top.length) { el.innerHTML = '<li class="empty">Press Start: the attacker strikes, the bees respond. Each big moment lands here.</li>'; return; }
  el.innerHTML = top.map((i) => `<li class="${shownStory.size && !shownStory.has(i.id) ? "new" : ""}" style="--c:${i.c}"><span><span class="t">${esc(clock(i.ts))}</span> <span class="who">${esc(i.who)}:</span>${i.text}</span></li>`).join("");
  shownStory = new Set(items.map((i) => i.id));
}

// ------------------------------------------------------------------ top bar, buttons, approval
function nightPlan() {
  const rounds = [...state.runs.values()].map((r) => r.round || 0);
  const maxRound = Math.max(0, ...rounds, state.current?.round || 0);
  const activeInc = [...state.incidents.values()].some((i) => i.active);
  const rid = runId();
  const startedRecently = rid && serverNow() - runStartedAt() < 20000 && !nightIncidents(rid).length;
  if (activeInc) return { label: `Night ${state.current?.round ?? maxRound} in progress`, disabled: true };
  if (startedRecently) return { label: `Night ${state.current?.round ?? maxRound} starting…`, disabled: true };
  if (maxRound === 0) return { label: "Start Night 1", run: async () => api("POST", "/api/scenario/bank-night-1") };
  if (maxRound === 1) return { label: "Start Night 2", run: async () => { await api("POST", "/api/reset", { mode: "next_round" }); return api("POST", "/api/scenario/bank-night-2"); } };
  if (maxRound === 2) {
    const u = latestUpgrade();
    if (u && !["proven", "rejected"].includes(u.status)) return { label: u.status === "deployed" ? "Proving the upgrade…" : "Waiting for the upgrade…", disabled: true };
    return { label: "Start Night 3", run: async () => api("POST", "/api/scenario/bank-night-3") };
  }
  return { label: "Start over", run: async () => { if (!confirm("Start over: wipe everything (including what the bees learned)?")) return null; return api("POST", "/api/reset", { mode: "full" }); } };
}
function renderTop() {
  const cur = state.current, r = cur?.round;
  $("#night").textContent = r ? `Night ${r}` : "No night running";
  const plan = nightPlan(), b = $("#btn-start");
  b.textContent = plan.label; b.disabled = !!plan.disabled || busy.has(b);
  b.onclick = plan.run ? () => act(b, plan.run) : null;
  const armed = [...state.agents.values()].some((a) => a.role === "worker" && a.status !== "dead" && a.phase === "step_sent");
  $("#btn-kill").classList.toggle("armed", armed);
}
const busy = new Set();
async function act(btn, fn) {
  busy.add(btn); btn.disabled = true;
  try { await fn(); } catch (e) { toast(String(e.message || e)); } finally { setTimeout(() => { busy.delete(btn); btn.disabled = false; render(); }, 1500); }
}
let toastT = null;
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(toastT); toastT = setTimeout(() => (t.hidden = true), 5000); }
function liveQueen() {
  const qs = [...state.agents.values()].filter((a) => a.role === "queen" && a.status !== "dead");
  return (qs.find((a) => a.status !== "standby") || qs[0])?._id || "queen-1";
}
let shownApproval = null;
function renderApproval() {
  const a = pendingApprovals()[0], box = $("#approval");
  if (!a) { box.hidden = true; shownApproval = null; return; }
  if (shownApproval === a._id) return;
  shownApproval = a._id;
  const inc = state.incidents.get(a.incident);
  const step = inc?.steps?.find((s) => s.step_id === a.step_id);
  const upg = a.kind === "upgrade" ? state.upgrades.get(a.upgrade) : null;
  if (a.kind === "upgrade") {
    $("#ap-t").textContent = `Deploy the Gardener’s code fix to the ${sn(upg?.system || "mainframe")}?`;
    const tests = upg?.tests ? ((upg.tests.pytest_passed ?? upg.tests.passed) ? "tests passed" : "tests FAILED") : "tests run";
    $("#ap-d").textContent = `${upg?.diff_lines ? `${upg.diff_lines}-line change · ` : ""}${tests} · after deploying, the attack is replayed to prove it.`;
  } else {
    const o = step?.op || (String(a.desc || "").match(/^(\w+)/) || [])[1];
    $("#ap-t").textContent = `${Op(o)} on the ${sn(step?.system || (String(a.desc || "").match(/ on (\w+)/) || [])[1])}?`;
    $("#ap-d").textContent = `A risky step: the bees won’t do it without a human OK.${step?.desc ? ` (${prettyTime(step.desc)})` : ""}`;
  }
  box.hidden = false;
}
async function decide(decision) {
  const id = shownApproval; if (!id) return;
  $("#approval").hidden = true;
  try { await api("POST", `/api/approvals/${encodeURIComponent(id)}`, { decision }); } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------------ details (the jargon lives here)
async function openDetails() {
  $("#details").hidden = false;
  const body = $("#det-body");
  body.innerHTML = "<section>Loading…</section>";
  const [sc, at] = await Promise.all([api("GET", "/api/scorecard").catch(() => null), api("GET", "/api/atlas").catch(() => null)]);
  const inc = [...state.incidents.values()].filter((i) => i.recall).sort((a, b) => ts(b.opened_at) - ts(a.opened_at))[0];
  const r = inc?.recall;
  const recall = r ? `<p>Last search (${esc(inc._id)}): <code>$vectorSearch</code> on <code>playbook_vec</code> · model ${esc(r.model || state.meta?.embed_model || "")} · ${r.used ? "<b>used</b>: chain walk skipped" : "no usable hit"}</p>
    <table><tr><th>playbook</th><th>score</th><th>status</th></tr>${(r.hits || []).map((h) => `<tr><td>${esc(h.playbook_id)}</td><td>${(h.score ?? 0).toFixed(3)}</td><td>${h.stale ? "stale" : `v${esc(h.system_version ?? "?")}`}</td></tr>`).join("") || "<tr><td colspan=3>0 hits (first time)</td></tr>"}</table>` : "<p>No memory search yet.</p>";
  const per = sc?.rounds?.length ? `<table><tr><th>night</th><th>incidents</th><th>resolve</th><th>diagnosis</th><th>hops</th><th>recall used</th></tr>${sc.rounds.map((x) => `<tr><td>${esc(x.round)}</td><td>${esc(x.incidents)}</td><td>${secs(x.avg_ttr_s)}</td><td>${secs(x.avg_diagnosis_s)}</td><td>${esc(x.avg_hops ?? "—")}</td><td>${esc(x.recall_used)}</td></tr>`).join("")}</table><p>From a MongoDB aggregation pipeline over <code>swarm.incidents</code> (grouped by round).</p>` : "<p>No finished nights yet.</p>";
  const colls = at ? `<p>${esc(at.uri_host)} · change stream ${state.connected ? "live" : "down"} · ${(state.opsPerSec || 0).toFixed(1)} writes/s</p><table><tr><th>collection</th><th>db</th><th>docs</th><th>indexes</th></tr>${at.collections.map((c) => `<tr><td><code>${esc(c.coll)}</code>${c.validator ? " · $jsonSchema" : ""}${(c.search_indexes || []).map((s) => ` · ${esc(s.type || "search")}: ${esc(s.name)}`).join("")}</td><td>${esc(c.db_name)}</td><td>${c.count.toLocaleString()}</td><td>${(c.indexes || []).filter((i) => i.name !== "_id_").length}</td></tr>`).join("")}</table>` : "<p>Atlas info unavailable.</p>";
  body.innerHTML = `<section><h3>Memory search (Atlas Vector Search)</h3>${recall}</section><section><h3>Per night</h3>${per}</section>
    <section class="full"><h3>MongoDB: every piece of state lives here</h3>${colls}<p>Everything on screen comes from one MongoDB change stream. <a href="./?classic=1" style="color:var(--honey)">Open the classic war room view</a> for the full detail.</p></section>`;
}

// ------------------------------------------------------------------ wiring
const driftMappings = new Map();
function bind() {
  $("#btn-kill").addEventListener("click", (e) => act(e.currentTarget, () => api("POST", "/api/kill", { selector: { role: "worker", phase: "step_sent" } })));
  $("#btn-queen").addEventListener("click", (e) => act(e.currentTarget, () => api("POST", "/api/kill", { selector: { agent: liveQueen() } })));
  const pop = $("#more-pop");
  $("#btn-more").addEventListener("click", (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; });
  document.addEventListener("click", (e) => { if (!pop.contains(e.target)) pop.hidden = true; });
  pop.querySelectorAll("[data-scn]").forEach((b) => b.addEventListener("click", () => { pop.hidden = true; act($("#btn-start"), () => api("POST", `/api/scenario/${b.dataset.scn}`)); }));
  pop.querySelectorAll("[data-reset]").forEach((b) => b.addEventListener("click", () => {
    pop.hidden = true;
    const mode = b.dataset.reset;
    if (mode === "full" && !confirm("Full reset: wipe everything, including what the bees learned?")) return;
    act($("#btn-start"), () => api("POST", "/api/reset", { mode }));
  }));
  $("#btn-details").addEventListener("click", () => { pop.hidden = true; openDetails(); });
  $("#det-x").addEventListener("click", () => ($("#details").hidden = true));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#details").hidden = true; });
  $("#ap-yes").addEventListener("click", () => decide("approved"));
  $("#ap-no").addEventListener("click", () => decide("denied"));
}

let raf = 0;
function render() {
  if (raf) return;
  raf = requestAnimationFrame(() => {
    raf = 0;
    if (!state.runs.size && !state.incidents.size && !state.chaos.size) lit.clear(); // a full reset wipes the beats
    for (const [name, fn] of [["top", renderTop], ["beats", renderBeats], ["now", () => { $("#now").innerHTML = nowLine(); }], ["team", renderTeam],
      ["estate", renderEstate], ["judge", renderJudge], ["meter", renderMeter], ["story", renderStory], ["approval", renderApproval]]) {
      try { fn(); } catch (e) { console.error(name, e); }
    }
  });
}

on("snapshot", () => { render(); refreshScore(); });
on("change", (m, prev) => {
  if (m.coll === "thoughts" && m.op === "insert" && m.doc?.kind === "reject" && m.doc.system) lieAt.set(m.doc.system, performance.now());
  if (m.coll === "schema_mappings" && m.doc?.source && m.doc?.previous) driftMappings.set(m.key, m.doc);
  if (m.coll === "incidents" && m.doc?.status === "resolved" && prev?.status !== "resolved") refreshScore();
  render();
});
on("status", (s) => { const c = $("#conn"); c.classList.toggle("live", s === "live"); c.classList.toggle("down", s !== "live"); $("#conn-txt").textContent = s === "live" ? "LIVE from MongoDB" : "reconnecting…"; render(); });
on("tick", render);

bind();
connect();
window.__stage = { state, render, translate };

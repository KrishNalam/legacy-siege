// Legacy Siege · diagram view: the agents and the systems they work on, drawn as one live wiring diagram in a
// printed-transcript style. Exhibit A = who is working on what right now; Exhibit B = every agent's moves on one
// time axis, and every attack paired with the defence that answered it.
// Data: the /ws change stream (js/store.js) + the existing REST endpoints. No new contracts.
// The plain-English vocabulary, NOW line, approvals and buttons are shared with W9's flow view (flow.js).
import { state, on, connect, api, currentRun, pendingApprovals, latestUpgrade, serverNow, ts, prettyTime } from "./js/store.js";

const $ = (s) => document.querySelector(s);
const XMARK = '<svg viewBox="0 0 16 16" width="0.9em" height="0.9em" aria-hidden="true"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" fill="none"/></svg>';
const esc = (s) => prettyTime(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const secs = (s) => (s == null || isNaN(s) ? "—" : s >= 100 ? `${Math.round(s)} s` : `${(+s).toFixed(s < 10 ? 1 : 0)} s`);

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

// ================================================================== the diagram (agents are the stars)
// One 1600 × 900 sheet scaled to fit. Systems sit on their dependency chain in the middle; every agent has a
// fixed seat around them; a wire is drawn from an agent to the system it is working on right now.
const FW = 1600, FH = 900;
const SW = 170, SH = 100, AW = 160, AH = 112;
const SYSXY = { portal: [560, 302], unix: [790, 302], mainframe: [1020, 302] };
const TOP = 146, BOT = 446;
const SEATS = {
  "queen-1": [105, TOP], "queen-2": [335, TOP], "scout-1": [565, TOP], "scout-2": [795, TOP], "guard-1": [1025, TOP],
  "sentinel-1": [1255, TOP], "gardener-1": [1425, TOP],
  "waggle-1": [335, BOT], "worker-web-1": [565, BOT], "worker-unix-1": [795, BOT], "worker-mainframe-1": [1025, BOT],
  "nurse-1": [1255, BOT], "nurse-2": [1425, BOT],
};
const SPARE = [[105, BOT], [1255, BOT + 0], [1425, BOT + 0]];
const ATK = { x: 40, y: 286, w: 240 }, MEM = { x: 1330, y: 286, w: 210 };
const GLYPH = { queen: "Q", scout: "S", worker: "W", guard: "G", nurse: "N", sentinel: "L", gardener: "R", waggle: "M" };
const TAG = { queen: "COORDINATOR", scout: "INVESTIGATOR", worker: "FIXER", guard: "THE CHECKER", nurse: "STANDBY · TAKES OVER",
  sentinel: "LOOKOUT", gardener: "WRITES CODE FIXES", waggle: "MESSENGER" };
const SYSROLE = { portal: "THE BANK WEBSITE", unix: "MOVES THE FILES", mainframe: "THE LEDGER (1987)" };
const U = (s) => esc(String(s ?? "")).toUpperCase();
const PERSONA = { batch_abort: "SABOTEUR", disk_fill: "SABOTEUR", cron_stop: "SABOTEUR", session_leak: "SABOTEUR",
  agent_kill: "ASSASSIN", lying_success: "LIAR", slow_apply: "LIAR", false_log: "LIAR",
  sqli_exploit: "HACKER", config_exploit: "HACKER", credential_exploit: "HACKER", schema_drift: "DRIFTER" };
const persona = (e) => String(e?.red?.persona || PERSONA[e?.fault] || "ATTACKER").toUpperCase();

function seatMap() {
  const out = {}; let spare = 0;
  const agents = [...state.agents.values()].sort((a, b) => String(a._id).localeCompare(String(b._id), undefined, { numeric: true }));
  for (const a of agents) {
    if (String(a._id).startsWith("red-")) continue; // the Red fleet is drawn as the attacker box
    if (SEATS[a._id]) out[a._id] = SEATS[a._id];
  }
  for (const a of agents) {
    if (out[a._id] || String(a._id).startsWith("red-")) continue;
    if (a.status === "dead" && !a.task && !a.incident) continue;
    const s = SPARE.find((p, i) => i >= spare && !Object.values(out).some((q) => q[0] === p[0] && q[1] === p[1]));
    if (!s) continue;
    spare = SPARE.indexOf(s) + 1; out[a._id] = s;
  }
  return out;
}

// the latest thought per agent (this night) and the agent that spoke last
function lastThoughts(since) {
  const by = new Map(), moves = new Map(); let last = null;
  for (const t of state.thoughts) {
    if (ts(t.ts) < since) continue;
    by.set(t.agent, t); if (!last || ts(t.ts) >= ts(last.ts)) last = t;
    if (stripLetter(t)) moves.set(t.agent, (moves.get(t.agent) || 0) + 1);
  }
  return { by, last, moves };
}

const PHASE = { diagnosing: "TRACING", step_sent: "FIX SENT", awaiting_verify: "RE-CHECKING", awaiting_approval: "NEEDS YOUR OK",
  verifying: "RE-CHECKING", planning: "PLANNING", recalling: "SEARCHING MEMORY" };

function seatModel(a, since, lt, inc) {
  const role = a.role || roleOf(a._id);
  const sys = SYSXY[a.target] ? a.target : null;
  const t = lt.by.get(a._id);
  const fresh = t && serverNow() - ts(t.ts) < 4500;
  const life = Math.min(10, lt.moves.get(a._id) || 0); // one cell per move tonight (see Exhibit B)
  let cls = "idle", ph = a.status === "standby" ? "STANDING BY" : "IDLE";
  if (a.status === "working" || a.phase || (a.incident && inc?.active && a.incident === inc._id)) {
    cls = "work"; ph = PHASE[a.phase] || (a.phase ? U(String(a.phase).replace(/_/g, " ")) : "WORKING");
    if (sys) ph += ` · ${U(sn(sys))}`;
  }
  if (a.status === "standby") cls += " standby";
  if (a.phase === "awaiting_approval") cls += " ask";
  if (role === "guard" && t?.kind === "reject" && serverNow() - ts(t.ts) < 15000) { cls = "work caught"; ph = "CAUGHT A LIE"; }
  else if (role === "guard" && t?.kind === "verify" && fresh) { cls += " ok"; ph = "VERIFIED ✓"; }
  if (t?.kind === "handoff" && fresh && role === "nurse") { cls = "work saved"; ph = "TOOK OVER A JOB"; }
  if (a.status === "dead") { cls = "dead"; ph = "† KILLED"; }
  if (lt.last && t && lt.last._id === t._id && fresh) cls += " turn";
  let tx = t ? (translate(t)?.text || "") : "";
  tx = tx.replace(/<[^>]+>/g, "");
  if (!tx) tx = a.status === "dead" ? "Its process was killed." : role === "nurse" ? "Waits to take over a dead agent’s job." : "";
  return { id: a._id, role, sys, cls, ph, tx, life, name: agentName(a._id) };
}

function sysModel(s, inc, lieSys, lieFresh) {
  const d = state.systems.get(s), st = d?.status || "?";
  const live = inc?.active ? inc : null, path = live?.diagnosis_path || [];
  const root = live && live.root_cause_system === s;
  const cls = root ? "bad" : st === "up" ? "ok" : st === "down" ? "bad" : "warn";
  const word = st === "up" ? ((d?.version || 1) > 1 ? `UP · V${d.version}` : "UP") : st === "down" ? "DOWN" : st === "?" ? "…" : "TROUBLE";
  const sym = String(d?.symptom || "");
  const cap = !live ? ((d?.version || 1) > 1 ? "RUNNING THE UPGRADED CODE" : "WORKING")
    : root && st === "up" ? "SAYS OK. IT IS THE CAUSE"
    : st === "up" ? "WORKING"
    : s === "portal" && /stale/i.test(sym) ? "CUSTOMERS SEE OLD BALANCES"
    : s === "portal" ? "CAN’T READ ITS DATA" : s === "unix" ? "FILE TRANSFER FAILED" : s === "mainframe" ? "NIGHTLY BATCH CRASHED" : "NOT HEALTHY";
  const band = root ? '<span class="band">ROOT CAUSE</span>' : live && path.includes(s) ? '<span class="band ck">CHECKED</span>' : "";
  const stamp = lieSys.has(s) ? `<span class="stamp${lieFresh ? " fresh" : ""}">ACK ≠ APPLIED</span>` : "";
  return `<div class="sysb ${cls}" style="left:${SYSXY[s][0]}px;top:${SYSXY[s][1]}px">${band}<div class="sn"><span>${U(sn(s))}</span><small>V${esc(d?.version ?? 1)}</small></div>
    <div class="sr">${SYSROLE[s] || ""}</div><div class="ss"><b>■ ${word}</b></div><div class="sc">${esc(cap)}</div>${stamp}</div>`;
}

// geometry
const seatBox = (p) => ({ x: p[0], y: p[1], w: AW, h: AH });
const sysBox = (s) => ({ x: SYSXY[s][0], y: SYSXY[s][1], w: SW, h: SH });
function curve(a, b, k = 0) {
  // a, b boxes: connect the nearest sides with a soft S-curve
  const ac = { x: a.x + a.w / 2, y: a.y + a.h / 2 }, bc = { x: b.x + b.w / 2, y: b.y + b.h / 2 };
  const dx = bc.x - ac.x, dy = bc.y - ac.y;
  if (Math.abs(dy) > Math.abs(dx) * 0.35 && Math.abs(dy) > 60) {
    const sx = ac.x + Math.max(-a.w / 2 + 14, Math.min(a.w / 2 - 14, dx * 0.15)) + k;
    const ex = bc.x + Math.max(-b.w / 2 + 16, Math.min(b.w / 2 - 16, -dx * 0.12)) + k;
    const sy = dy > 0 ? a.y + a.h : a.y, ey = dy > 0 ? b.y - 6 : b.y + b.h + 6;
    const m = (sy + ey) / 2;
    return { d: `M${sx} ${sy} C${sx} ${m} ${ex} ${m} ${ex} ${ey}`, mx: (sx + ex) / 2, my: m };
  }
  const sx = dx > 0 ? a.x + a.w : a.x, ex = dx > 0 ? b.x - 8 : b.x + b.w + 8;
  const sy = ac.y + k, ey = bc.y + k, m = (sx + ex) / 2;
  return { d: `M${sx} ${sy} C${m} ${sy} ${m} ${ey} ${ex} ${ey}`, mx: m, my: (sy + ey) / 2 };
}

// ------------------------------------------------------------------ attack → defence pairs
function defenceFor(e, nightIncs) {
  const t0 = ts(e.ts);
  if (e.fault === "agent_kill") {
    const v = e.result?.agent;
    if (!v) return ["failed", "error"].includes(e.status) || e.result?.how === "gave_up" ? { c: "wait", t: "NO BUSY AGENT TO KILL" } : { c: "wait", t: "HUNTING A BUSY AGENT…" };
    if (roleOf(v) === "queen") {
      const q = [...state.thoughts].reverse().find((t) => /takes the queen lease/.test(t.text || "") && ts(t.ts) >= t0);
      return q ? { c: "ok", t: `${U(agentName(q.agent))} TOOK OVER AS QUEEN` } : { c: "wait", t: "BACKUP QUEEN NOTICES THE SILENCE…" };
    }
    const ho = nightIncs.flatMap((i) => i.handoffs || []).find((h) => h.from === v);
    if (ho) return { c: "ok", t: `${U(agentName(ho.to))} TOOK OVER${ho.takeover_s != null ? ` IN ${Math.round(ho.takeover_s)} S` : ""} · NO STEP REPEATED` };
    return { c: "wait", t: "A NURSE PICKS UP ITS JOB…" };
  }
  if (["lying_success", "slow_apply", "false_log"].includes(e.fault)) {
    const rj = state.thoughts.find((t) => t.kind === "reject" && ts(t.ts) >= t0 && (!t.system || t.system === e.target));
    const st = nightIncs.flatMap((i) => i.steps || []).find((s) => s.system === e.target && (s.attempts || []).length > 1);
    if (rj || st) return { c: "ok", t: `GUARD CAUGHT IT: “DONE” BUT NOTHING CHANGED${st?.status === "verified" ? " · REDONE ✓" : ""}` };
    return { c: "wait", t: "THE GUARD RE-CHECKS EVERY “DONE”" };
  }
  const inc = nightIncs.find((i) => ts(i.opened_at) >= t0 - 3000 && (i.root_cause_system === e.target || (i.affected || []).includes(e.target) || i.system === e.target));
  if (e.fault === "schema_drift") {
    if (inc?.status === "resolved") return { c: "ok", t: "NEW FIELD NAME LEARNED · EVERY AGENT USES IT" };
    return inc ? { c: "wait", t: "WORKING OUT THE NEW FIELD NAME…" } : { c: "wait", t: "LOOKING FOR WHAT CHANGED…" };
  }
  if (!inc) {
    const u = latestUpgrade();
    if (u?.status === "proven" && serverNow() - t0 > 8000) return { c: "ok", t: "NOTHING BROKE: THE UPGRADE HOLDS" };
    return serverNow() - t0 > 30000 ? { c: "wait", t: "NO DAMAGE SEEN" } : { c: "wait", t: "WAITING FOR IT TO SPREAD…" };
  }
  const who = inc.recall?.used ? "REMEMBERED IT" : "SCOUT TRACED IT";
  if (inc.status === "resolved") return { c: "ok", t: `${who} · FIXED IN ${secs(inc.metrics?.ttr_s).toUpperCase()}` };
  if (inc.status === "failed") return { c: "bad", t: "NOT FIXED" };
  const steps = inc.steps || [], done = steps.filter((s) => s.status === "verified").length;
  if (inc.root_cause_system && steps.length) return { c: "wait", t: `${who} · FIXING ${Math.min(done + 1, steps.length)} OF ${steps.length}` };
  return { c: "wait", t: inc.recall?.used ? "REMEMBERED · GOING STRAIGHT THERE" : `SCOUT TRACING · ${(inc.diagnosis_path || []).map((s) => U(sn(s))).join(" → ")}…` };
}
const ATKWORD = {
  lying_success: (e) => `RIGGED ${U(sn(e.target))} TO LIE`, slow_apply: (e) => `MADE ${U(sn(e.target))} APPLY LATE`,
  false_log: (e) => `PLANTED FALSE LOGS ON ${U(sn(e.target))}`,
  batch_abort: (e) => `CORRUPTED THE ${U(sn(e.target))} BATCH`, disk_fill: (e) => `FILLED ${U(sn(e.target))}’S DISK`,
  cron_stop: (e) => `FROZE ${U(sn(e.target))}’S JOBS`, session_leak: (e) => `JAMMED ${U(sn(e.target))} SESSIONS`,
  schema_drift: (e) => `RENAMED A FIELD ON ${U(sn(e.target))}`, agent_kill: (e) => (e.result?.agent ? `KILLED ${U(agentName(e.result.agent))}` : "HUNTING AN AGENT"),
  sqli_exploit: (e) => `BROKE IN TO ${U(sn(e.target))}`, config_exploit: (e) => `TAMPERED WITH ${U(sn(e.target))}`,
  credential_exploit: (e) => `USED A DEFAULT PASSWORD ON ${U(sn(e.target))}`,
};
const atkWord = (e) => (ATKWORD[e.fault] ? ATKWORD[e.fault](e) : `${U(String(e.fault).replace(/_/g, " "))} ON ${U(sn(e.target))}`);
function duels() {
  const rid = runId();
  const evs = [...state.chaos.values()].filter((e) => !rid || e.run_id === rid).sort((a, b) => ts(a.ts) - ts(b.ts));
  const incs = nightIncidents();
  return evs.map((e) => ({ id: e._id, e, who: persona(e), atk: atkWord(e), def: defenceFor(e, incs) }));
}

// ------------------------------------------------------------------ action strips (who did what, on one time axis)
function stripLetter(t) {
  const x = String(t.text || "");
  if (t.kind === "reject") return ["X", "x"];
  if (t.kind === "verify" || /^Verified /.test(x)) return ["V", "v"];
  if (t.kind === "handoff" || /^Took over/.test(x)) return ["T", "t"];
  if (t.kind === "recall" && /^Recall hit/.test(x)) return ["R", "r"];
  if (t.kind === "learn" || /^Resolved /.test(x)) return ["M", "m"];
  if (t.kind === "approval" || /destructive|approval/.test(x)) return ["A", "a"];
  if (/^Sending /.test(x) || t.kind === "action") return ["S", ""];
  if (/\/health|^Hop \d|^Reading /.test(x)) return ["H", ""];
  return null;
}

let lastSeenThought = "";
const seenDuel = new Set(), seenStrip = new Set();
function renderStrips(since) {
  const rid0 = runId();
  const t0 = since + 5000, now = serverNow(), span = Math.max(60000, now - t0 + 8000);
  const rows = new Map();
  for (const t of state.thoughts) {
    if (ts(t.ts) < t0 || String(t.agent || "").startsWith("red-")) continue;
    const L = stripLetter(t); if (!L) continue;
    const r = rows.get(t.agent) || rows.set(t.agent, { id: t.agent, marks: [] }).get(t.agent);
    r.marks.push({ k: L[0], c: L[1], at: ts(t.ts), id: t._id });
  }
  for (const e of state.chaos.values()) {
    if (e.fault !== "agent_kill" || !e.result?.agent || (runId() && e.run_id !== runId())) continue;
    const v = e.result.agent, r = rows.get(v) || rows.set(v, { id: v, marks: [] }).get(v);
    r.marks.push({ k: "†", c: "k", at: ts(e.done_at || e.ts), id: `${e._id}:k` });
  }
  const list = [...rows.values()].sort((a, b) => Math.min(...a.marks.map((m) => m.at)) - Math.min(...b.marks.map((m) => m.at))).slice(-7);
  const pct = (at) => Math.max(0, Math.min(100, ((at - t0) / span) * 100));
  const minute = (i) => `${i}:00`;
  let html = `<div class="st head" style="top:0"><span class="name">AGENT</span><span class="track">${[0, 1, 2, 3, 4, 5].filter((i) => pct(t0 + i * 60000) < 94).map((i) => `<span style="left:${pct(t0 + i * 60000)}%">${minute(i)}</span>`).join("")}</span></div>`;
  if (!list.length) html += `<div class="st" style="top:22px"><span class="name dim">—</span><span class="track dim">${rid0 ? "NO AGENT MOVES YET TONIGHT." : "PRESS START: THE ATTACK BEGINS, AND EACH AGENT’S MOVES PRINT HERE."}</span></div>`;
  list.forEach((r, i) => {
    const a = state.agents.get(r.id), dead = a?.status === "dead";
    const first = Math.min(...r.marks.map((m) => m.at)), last = Math.max(...r.marks.map((m) => m.at));
    const marks = r.marks.map((m) => { const n = !seenStrip.has(m.id); seenStrip.add(m.id); return `<b class="${m.c}${n ? " n" : ""}" style="left:${pct(m.at)}%">${m.k}</b>`; }).join("");
    html += `<div class="st${dead ? " dead" : ""}" style="top:${22 * (i + 1)}px"><span class="name">${U(agentName(r.id))}</span><span class="track">
      <i class="ln" style="left:${pct(first)}%;width:${Math.max(0, pct(dead ? last : now) - pct(first))}%"></i>
      ${dead ? "" : `<i class="ln f" style="left:${pct(now)}%;width:${100 - pct(now)}%"></i>`}${marks}</span></div>`;
  });
  const host = $("#strips");
  if (host.dataset.html !== html) { host.innerHTML = html; host.dataset.html = html; }
}

function renderBoard() {
  const ds = duels().slice(-5);
  let html = `<div class="du head" style="top:0"><span class="who">ATTACKER</span><span>ATTACK</span><span class="ar"></span><span>DEFENCE</span></div>`;
  if (!ds.length) html += `<div class="du" style="top:22px"><span class="who" style="background:var(--faint)">—</span><span class="dim">EVERY ATTACK WE LAUNCH</span><span class="ar">→</span><span class="dim">AND HOW THE AGENTS ANSWERED IT</span></div>`;
  ds.forEach((d, i) => {
    const key = `${d.id}|${d.def.t}`; const n = !seenDuel.has(key); seenDuel.add(key);
    html += `<div class="du${n ? " n" : ""}" style="top:${22 * (i + 1)}px"><span class="who">${esc(d.who)}</span><span class="atk">${esc(d.atk)}</span><span class="ar">→</span><span class="def ${d.def.c}">${esc(d.def.t)}</span></div>`;
  });
  // the one number: night 1 vs night 2
  const r = rounds(), liveR2 = [...state.incidents.values()].find((i) => i.active && i.round === 2);
  const t1 = r[1]?.ttr, t2 = r[2]?.ttr ?? (liveR2 ? (serverNow() - ts(liveR2.opened_at)) / 1000 : null);
  const max = Math.max(t1 || 0, t2 || 0, 1), bw = (t) => Math.round((t / max) * 220);
  const y = 22 * 6 + 8;
  html += `<div class="score" style="top:${y}px"><span class="dim">TIME TO FIX</span><span>NIGHT 1 ${t1 != null ? secs(t1).toUpperCase() : "—"}</span>${t1 != null ? `<span class="bar2" style="width:${bw(t1)}px"></span>` : ""}</div>`;
  html += `<div class="score" style="top:${y + 22}px"><span class="dim" style="visibility:hidden">TIME TO FIX</span><span>NIGHT 2 ${t2 != null ? secs(t2).toUpperCase() : "—"}</span>${t2 != null ? `<span class="bar2 r2" style="width:${bw(t2)}px"></span>` : ""}${r[1] && r[2] && r[2].ttr < r[1].ttr ? `<span class="x">${(r[1].ttr / r[2].ttr).toFixed(1)}× FASTER · ${Math.round(r[2].hops)} CHECK, NOT ${Math.round(r[1].hops)}</span>` : r[1] && !r[2] ? '<span class="dim">SAME ATTACK NEXT NIGHT. FASTER?</span>' : ""}</div>`;
  const host = $("#board");
  if (host.dataset.html !== html) { host.innerHTML = html; host.dataset.html = html; }
}

let lieShownAt = 0;
function renderDiagram() {
  // fit the sheet
  const fit = $("#fit"), fr = $("#frame");
  const s = Math.min(fit.clientWidth / FW, fit.clientHeight / FH);
  fr.style.transform = `translate(${Math.max(0, (fit.clientWidth - FW * s) / 2)}px, ${Math.max(0, (fit.clientHeight - FH * s) / 2)}px) scale(${s})`;

  const rid = runId(), since = (runStartedAt() || 0) - 5000;
  const inc = focusIncident(), live = inc?.active ? inc : null;
  const lt = lastThoughts(since);
  const seats = seatMap();
  // lies caught this night (Guard rejects)
  const rejects = state.thoughts.filter((t) => t.kind === "reject" && ts(t.ts) >= since && !String(t.agent || "").startsWith("red-"));
  const lieSys = new Set(rejects.map((t) => t.system || stepByAction(null, (String(t.text).match(/INC-\S+-a\d+/) || [])[0])?.system || "mainframe"));
  for (const s2 of (live?.steps || [])) if ((s2.attempts || []).length > 1) lieSys.add(s2.system);
  const lastRj = rejects[rejects.length - 1];
  const lieFresh = lastRj && serverNow() - ts(lastRj.ts) < 1500 && lastRj._id !== lieShownAt;

  let nodes = "";
  // systems
  for (const s2 of CHAIN) nodes += sysModel(s2, inc, lieSys, lieFresh);
  if (lieFresh) lieShownAt = lastRj._id;
  // agents
  const seatModels = {};
  for (const [id, p] of Object.entries(seats)) {
    const a = state.agents.get(id); if (!a) continue;
    const m = seatModel(a, since, lt, inc); seatModels[id] = m;
    const bar = Array.from({ length: 10 }, (_, i) => `<i class="${i < m.life ? "f" : ""}"></i>`).join("");
    nodes += `<div class="seat ${m.cls}" data-id="${esc(id)}" style="left:${p[0]}px;top:${p[1]}px"><div class="who"><span>${U(m.name)}</span><span class="gl">${GLYPH[m.role] || "·"}</span></div>
      <div class="rl">${TAG[m.role] || U(m.role)}</div><div class="bar" title="moves tonight">${bar}</div><div class="ph">${esc(m.ph)}</div><div class="tx">${esc(m.tx)}</div></div>`;
  }
  // attacker box: the personas who struck tonight
  const ds = duels();
  const per = [...new Set(ds.map((d) => d.who))];
  nodes += `<div class="side red" style="left:${ATK.x}px;top:${ATK.y}px;width:${ATK.w}px"><div class="hd"><span>THE ATTACKERS</span><small>US</small></div>
    ${ds.length ? ds.slice(-4).map((d) => `<div class="ln"><span class="p">${esc(d.who)}</span> ${esc(d.atk)}</div>`).join("") : '<div class="ln dim">WAITING FOR NIGHT 1.</div><div class="ln dim">WE BREAK THE BANK;</div><div class="ln dim">THE AGENTS FIX IT.</div>'}
    ${per.length ? "" : ""}</div>`;
  // memory box
  const pbs = [...state.playbooks.values()];
  const recallOn = live?.recall?.used || (inc?.recall?.used && inc?.status === "resolved");
  const saved = inc?.status === "resolved";
  const cnt = state.counts || {};
  const dots = Array.from({ length: 48 }, (_, i) => `<i class="${i < Math.min(48, pbs.length * 6) ? "on" : ""}"></i>`).join("");
  nodes += `<div class="side mem" style="left:${MEM.x}px;top:${MEM.y}px"><div class="hd"><span>MONGODB · MEMORY</span><small>${(state.opsPerSec || 0).toFixed(1)} W/S</small></div>
    <div class="ln">${pbs.length} LESSON${pbs.length === 1 ? "" : "S"} SAVED${saved ? ' <span style="color:var(--amber)">+1 TONIGHT</span>' : ""}</div>
    <div class="ln">${recallOn ? '<span style="color:var(--amber)">RECALLED BY VECTOR SEARCH</span>' : live ? "SEARCHED BY MEANING ($vectorSearch)" : "EVERY FIX IS REMEMBERED"}</div>
    <div class="ln dim">${(cnt.thoughts ?? cnt["swarm.thoughts"] ?? state.thoughts.length).toLocaleString?.() ?? ""} THOUGHTS · ${state.agents.size} AGENTS</div><div class="dots">${dots}</div></div>`;

  // wires
  let w = "";
  const lbl = (c, txt, cls = "") => `<text class="wl ${cls}" x="${c.mx}" y="${c.my - 4}" text-anchor="middle">${esc(txt)}</text>`;
  // the dependency chain (double rule) with "depends on" labels
  for (let i = 0; i < CHAIN.length - 1; i++) {
    const a = sysBox(CHAIN[i]), b = sysBox(CHAIN[i + 1]);
    const blames = state.systems.get(CHAIN[i])?.blames === CHAIN[i + 1] && live;
    const y = a.y + a.h / 2, x1 = a.x + a.w + 6, x2 = b.x - 8;
    w += `<path class="w dbl ${blames ? "red" : ""}" d="M${x1} ${y} L${x2} ${y}"/><path class="w dbl-in" d="M${x1} ${y} L${x2} ${y}"/>`;
    w += `<text class="wl ${blames ? "red" : ""}" x="${(x1 + x2) / 2}" y="${y - 8}" text-anchor="middle">${blames ? "BLAMES" : "NEEDS"}</text>`;
  }
  // attacker → systems / victims
  const atkBox = { x: ATK.x, y: ATK.y, w: ATK.w, h: 104 };
  const hitSys = new Set(ds.filter((d) => d.e.fault !== "agent_kill" && SYSXY[d.e.target]).map((d) => d.e.target));
  let k = -8;
  // attack wires arc over the chain and land on the top of the system they hit
  for (const s2 of hitSys) {
    const b = sysBox(s2), sx = ATK.x + ATK.w, sy = ATK.y + 16 + k, ex = b.x + 22 + k, ey = b.y - 8;
    const ok = (state.systems.get(s2)?.status || "up") === "up" && !(live && live.root_cause_system === s2);
    w += `<path class="w red ${ok ? "" : "on"}" d="M${sx} ${sy} C${sx + 140} ${sy} ${ex} ${ey - 120} ${ex} ${ey}"/>`; k += 10;
  }
  if (hitSys.size) w += `<text class="wl red" x="${ATK.x + ATK.w + 14}" y="${ATK.y + 8}">ATTACKS</text>`;
  for (const d of ds) {
    const v = d.e.fault === "agent_kill" && d.e.result?.agent; if (!v || !seats[v]) continue;
    const c = curve(atkBox, seatBox(seats[v])); w += `<path class="w red on" d="${c.d}"/>` + lbl(c, "KILL", "red");
  }
  // agents → the system they are on
  for (const [id, m] of Object.entries(seatModels)) {
    if (!m.sys || m.cls.startsWith("idle") || m.cls.startsWith("dead")) continue;
    const c = curve(seatBox(seats[id]), sysBox(m.sys));
    const col = m.cls.includes("caught") ? "red" : m.cls.includes("ok") ? "grn" : "";
    w += `<path class="w on ${col}" d="${c.d}"/>` + lbl(c, m.ph.split(" · ")[0], `on ${col}`);
  }
  // takeovers: nurse → the dead agent's seat
  for (const h of nightIncidents().flatMap((i) => i.handoffs || [])) {
    if (!seats[h.from] || !seats[h.to]) continue;
    const c = curve(seatBox(seats[h.to]), seatBox(seats[h.from]), 6);
    w += `<path class="w amb on" d="${c.d}"/>` + lbl(c, "TOOK OVER ITS JOB", "amb");
  }
  // memory: recall → scout, lesson saved
  const memBox = { x: MEM.x, y: MEM.y, w: MEM.w, h: 104 };
  if (recallOn) {
    const sc = inc.owner && seats[inc.owner] ? inc.owner : Object.keys(seats).find((i) => roleOf(i) === "scout");
    if (sc) { const c = curve(memBox, seatBox(seats[sc])); w += `<path class="w amb on" d="${c.d}"/>` + lbl(c, "REMEMBERED", "amb"); }
  }
  if (saved && inc.root_cause_system && SYSXY[inc.root_cause_system]) {
    const c = curve(sysBox(inc.root_cause_system), memBox, 10); w += `<path class="w amb" d="${c.d}"/>` + lbl(c, "LESSON SAVED", "amb");
  }
  const wsvg = $("#wires");
  if (wsvg.dataset.html !== w) { wsvg.innerHTML = w; wsvg.dataset.html = w; }
  const host = $("#nodes");
  if (host.dataset.html !== nodes) { host.innerHTML = nodes; host.dataset.html = nodes; }

  // headings
  const busyN = [...state.agents.values()].filter((a) => a.status === "working").length;
  $("#ex-a-l").textContent = `${[...state.agents.values()].filter((a) => !String(a._id).startsWith("red-") && a.status !== "dead").length} AGENTS ALIVE · ${busyN} WORKING`;
  const t0 = runStartedAt();
  const el = rid && t0 ? Math.max(0, Math.floor((serverNow() - t0) / 1000)) : null;
  $("#clock").textContent = rid ? `NIGHT ${state.current?.round ?? "?"} · T+${el != null ? `${String(Math.floor(el / 60)).padStart(2, "0")}:${String(el % 60).padStart(2, "0")}` : "—"}` : "NO NIGHT RUNNING";
  renderStrips(since);
  renderBoard();
  void lastSeenThought;
}

// ------------------------------------------------------------------ buttons
function maxRound() { return Math.max(0, ...[...state.runs.values()].map((r) => r.round || 0), state.current?.round || 0); }
function nightBusy() {
  const rid = runId();
  return [...state.incidents.values()].some((i) => i.active) || (rid && serverNow() - runStartedAt() < 20000 && !nightIncidents(rid).length);
}
function renderTop() {
  const r = state.current?.round, mr = maxRound(), busyNight = nightBusy();
  $("#night").textContent = r ? `Night ${r}${busyNight ? " · running" : ""}` : "Ready";
  const start = $("#btn-start"), next = $("#btn-next");
  start.textContent = mr === 0 ? "Start" : "Start over";
  start.disabled = busy.has(start) || (mr > 0 && busyNight);
  start.onclick = () => act(start, async () => {
    if (mr === 0) return api("POST", "/api/scenario/bank-night-1");
    if (!confirm("Start over: wipe everything (including what the bees learned) and run Night 1?")) return null;
    await api("POST", "/api/reset", { mode: "full" });
    return api("POST", "/api/scenario/bank-night-1");
  });
  const u = latestUpgrade();
  const upgBusy = mr === 2 && u && !["proven", "rejected"].includes(u.status);
  next.textContent = mr >= 1 && mr < 3 ? `Next night (${mr + 1})` : "Next night";
  next.disabled = busy.has(next) || mr === 0 || mr >= 3 || busyNight || upgBusy;
  next.title = upgBusy ? "Wait: the Gardener is still installing and proving its code fix" : "";
  next.onclick = () => act(next, async () => {
    if (mr === 1) { await api("POST", "/api/reset", { mode: "next_round" }); return api("POST", "/api/scenario/bank-night-2"); }
    return api("POST", "/api/scenario/bank-night-3");
  });
  const armed = [...state.agents.values()].some((a) => a.role === "worker" && a.status !== "dead" && a.phase === "step_sent");
  $("#btn-kill").classList.toggle("armed", armed);
}

function bind() {
  $("#btn-kill").addEventListener("click", (e) => act(e.currentTarget, () => api("POST", "/api/kill", { selector: { role: "worker", phase: "step_sent" } })));
  $("#btn-queen").addEventListener("click", (e) => act(e.currentTarget, () => api("POST", "/api/kill", { selector: { agent: liveQueen() } })));
  const pop = $("#more-pop");
  $("#btn-more").addEventListener("click", (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; });
  document.addEventListener("click", (e) => { if (!pop.contains(e.target)) pop.hidden = true; });
  pop.querySelectorAll("[data-scn]").forEach((b) => b.addEventListener("click", () => { pop.hidden = true; act($("#btn-next"), () => api("POST", `/api/scenario/${b.dataset.scn}`)); }));
  pop.querySelectorAll("[data-reset]").forEach((b) => b.addEventListener("click", () => {
    pop.hidden = true;
    const mode = b.dataset.reset;
    if (mode === "full" && !confirm("Full reset: wipe everything, including what the bees learned?")) return;
    act($("#btn-next"), () => api("POST", "/api/reset", { mode }));
  }));
  $("#btn-details").addEventListener("click", () => { pop.hidden = true; openDetails(); });
  $("#det-x").addEventListener("click", () => ($("#details").hidden = true));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#details").hidden = true; });
  $("#ap-yes").addEventListener("click", () => decide("approved"));
  $("#ap-no").addEventListener("click", () => decide("denied"));
  window.addEventListener("resize", render);
  // presenter keys (as in Krish's concept): S start, K kill a worker, Q (or Shift+K) kill the queen, N next night, R reset menu
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey || /input|select|textarea/i.test(e.target.tagName)) return;
    const k = e.key.toLowerCase();
    const press = (id) => { const b = $(id); if (b && !b.disabled) b.click(); };
    if (k === "s") press("#btn-start");
    else if (k === "q" || (k === "k" && e.shiftKey)) press("#btn-queen");
    else if (k === "k") press("#btn-kill");
    else if (k === "n") press("#btn-next");
    else if (k === "r") { e.stopPropagation(); $("#more-pop").hidden = !$("#more-pop").hidden; }
  });
}

let raf = 0;
function render() {
  if (raf) return;
  raf = requestAnimationFrame(() => {
    raf = 0;
    for (const [name, fn] of [["top", renderTop], ["diagram", renderDiagram], ["now", () => { $("#now").innerHTML = nowLine(); }], ["approval", renderApproval]]) {
      try { fn(); } catch (e) { console.error(name, e); }
    }
  });
}

on("snapshot", () => { render(); refreshScore(); });
on("change", (m, prev) => {
  if (m.coll === "schema_mappings" && m.doc?.source && m.doc?.previous) driftMappings.set(m.key, m.doc);
  if (m.coll === "incidents" && m.doc?.status === "resolved" && prev?.status !== "resolved") refreshScore();
  render();
});
on("status", (s) => { const c = $("#conn"); c.classList.toggle("live", s === "live"); c.classList.toggle("down", s !== "live"); $("#conn-txt").textContent = s === "live" ? "LIVE FROM MONGODB" : "RECONNECTING…"; render(); });
on("tick", render);

bind();
connect();
window.__diagram = { state, render, translate, duels };

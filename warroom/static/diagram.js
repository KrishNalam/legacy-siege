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
// a Guard/Scout "Rejected memory …" line is kind:reject too, but it caught a poisoned memory, not a lying system
const isMemReject = (t) => /^Rejected memory/.test(String(t?.text || ""));
const isLie = (t) => t?.kind === "reject" && !isMemReject(t);
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
  if (isMemReject(t)) return { text: `<b>Caught a poisoned memory</b>: the remembered fix didn’t match what the Guard verified. Retired it${/Restored /.test(x) ? " and restored the verified one" : ""}.`, story: true };
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
  const recentReject = [...state.thoughts].reverse().find((t) => isLie(t) && serverNow() - ts(t.ts) < 7000);
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
  try { await api("POST", `/api/approvals/${encodeURIComponent(id)}`, { decision }); } catch (e) {
    // The request failed: bring the card back (if it is still pending) so the click can be retried.
    if (shownApproval === id) { shownApproval = null; renderApproval(); }
    toast(`Couldn’t send your answer (${e.message}). Please try again.`);
  }
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
const SHORT_OPS = { quarantine_record: "Set aside the bad record", restart_batch: "Restart the batch", rerun_transfer: "Re-send the files",
  refresh_cache: "Refresh balances", map_field: "Use the new field name" };
const sop = (o) => SHORT_OPS[o] || Op(o);
const PERSONA = { batch_abort: "SABOTEUR", disk_fill: "SABOTEUR", cron_stop: "SABOTEUR", session_leak: "SABOTEUR",
  agent_kill: "ASSASSIN", agent_freeze: "ASSASSIN", lying_success: "LIAR", slow_apply: "LIAR", false_log: "LIAR", lie_to_agent: "LIAR",
  poison_memory: "MEMORY",
  sqli_exploit: "HACKER", config_exploit: "HACKER", credential_exploit: "HACKER", schema_drift: "DRIFTER" };
const persona = (e) => String(e?.red?.persona || PERSONA[e?.fault] || "ATTACKER").toUpperCase().replace(/ STRIKER$/, ""); // "Memory Striker" → MEMORY

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
    if (ts(t.ts) < since || isProgress(t)) continue;
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
  if (role === "guard" && t?.kind === "reject" && serverNow() - ts(t.ts) < 15000) { cls = "work caught"; ph = isMemReject(t) ? "CAUGHT A POISONED MEMORY" : "CAUGHT A LIE"; }
  else if (role === "guard" && t?.kind === "verify" && fresh) { cls += " ok"; ph = "VERIFIED ✓"; }
  if (t?.kind === "handoff" && fresh && role === "nurse") { cls = "work saved"; ph = "TOOK OVER A JOB"; }
  const ro = state.roster instanceof Map ? state.roster.get(a._id) : null; // W22 comms roster, when it is on
  if (a.status === "dead") { cls = "dead"; ph = ro?.taken_over_by?.to ? `† KILLED · JOB → ${U(agentName(ro.taken_over_by.to))}` : "† KILLED"; }
  if (lt.last && t && lt.last._id === t._id && fresh) cls += " turn";
  let tx = t ? (translate(t)?.text || "") : "";
  tx = tx.replace(/<[^>]+>/g, "");
  if (!tx) tx = a.status === "dead" ? "Its process was killed." : role === "nurse" ? "Waits to take over a dead agent’s job." : "";
  const boss = ro?.parent && a.status !== "dead" && cls.startsWith("work") ? ` · FOR ${U(agentName(ro.parent))}` : "";
  return { id: a._id, role, sys, cls, ph, tx, life, name: agentName(a._id), boss };
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
const sysBox = (s) => ({ x: SYSXY[s][0], y: SYSXY[s][1], w: SW, h: SH });

// ------------------------------------------------------------------ attack → defence pairs
function defenceFor(e, nightIncs) {
  const t0 = ts(e.ts);
  if (e.fault === "agent_kill" || e.fault === "agent_freeze") { // a frozen bee loses its lease like a dead one: same nurse takeover
    const v = e.result?.agent;
    if (!v) return ["failed", "error"].includes(e.status) || e.result?.how === "gave_up" ? { c: "wait", t: `NO BUSY AGENT TO ${e.fault === "agent_kill" ? "KILL" : "FREEZE"}` } : { c: "wait", t: "HUNTING A BUSY AGENT…" };
    if (roleOf(v) === "queen") {
      const q = [...state.thoughts].reverse().find((t) => /takes the queen lease/.test(t.text || "") && ts(t.ts) >= t0);
      return q ? { c: "ok", t: `${U(agentName(q.agent))} TOOK OVER AS QUEEN` } : { c: "wait", t: "BACKUP QUEEN NOTICES THE SILENCE…" };
    }
    const ho = nightIncs.flatMap((i) => i.handoffs || []).find((h) => h.from === v);
    if (ho) return { c: "ok", t: `${U(agentName(ho.to))} TOOK OVER${ho.takeover_s != null ? ` IN ${Math.round(ho.takeover_s)} S` : ""} · NO STEP REPEATED` };
    return { c: "wait", t: "A NURSE PICKS UP ITS JOB…" };
  }
  if (["lying_success", "slow_apply", "false_log"].includes(e.fault)) {
    const rj = state.thoughts.find((t) => isLie(t) && ts(t.ts) >= t0 && (!t.system || t.system === e.target));
    const st = nightIncs.flatMap((i) => i.steps || []).find((s) => s.system === e.target && (s.attempts || []).length > 1);
    if (rj || st) return { c: "ok", t: `GUARD CAUGHT THE LIE${st?.status === "verified" ? " · REDONE, VERIFIED" : " (ACK ≠ APPLIED)"}` };
    return { c: "wait", t: "THE GUARD RE-CHECKS EVERY “DONE”" };
  }
  if (e.fault === "lie_to_agent") { // an injected log line: workers treat logs as data, the Guard's read-back decides
    const rj = state.thoughts.find((t) => isLie(t) && ts(t.ts) >= t0 && (!t.system || t.system === e.target));
    if (rj) return { c: "ok", t: "LOG DOUBTED · GUARD READ-BACK CAUGHT IT" };
    return { c: "wait", t: "LOGS ARE DATA · GUARD READS BACK" };
  }
  if (e.fault === "poison_memory") { // judged where the plant is recalled, possibly a later night
    if (["failed", "error"].includes(e.status) || e.result?.skipped) return { c: "wait", t: "NO MEMORY TO POISON" };
    const mr = state.thoughts.find((t) => isMemReject(t) && ts(t.ts) >= t0);
    const ri = [...state.incidents.values()].find((i) => ts(i.opened_at) >= t0 - 3000 && (i.recall?.rejected || (i.recall?.hits || []).some((h) => h.rejected)));
    if (mr || ri) return { c: "ok", t: /Restored /.test(mr?.text || "") ? "RECALL REJECTED · VERIFIED FIX RESTORED" : "GUARD REJECTED THE POISONED FIX" };
    return { c: "wait", t: "RECALL MUST MATCH WHAT THE GUARD VERIFIED" };
  }
  const after = nightIncs.filter((i) => ts(i.opened_at) >= t0 - 3000);
  const inc = after.find((i) => i.root_cause_system === e.target || (i.affected || []).includes(e.target) || i.system === e.target)
    || after.find((i) => !i.root_cause_system); // a cascade surfaces downstream first: the root is not known yet
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
  schema_drift: (e) => `RENAMED A FIELD ON ${U(sn(e.target))}`, lie_to_agent: (e) => `PLANTED A LYING LOG ON ${U(sn(e.target))}`,
  poison_memory: () => "POISONED A REMEMBERED FIX", agent_freeze: (e) => (e.result?.agent ? `FROZE ${U(agentName(e.result.agent))}` : "HUNTING AN AGENT"), agent_kill: (e) => (e.result?.agent ? `KILLED ${U(agentName(e.result.agent))}` : "HUNTING AN AGENT"),
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
  if (/^Sending /.test(x)) return ["S", ""];
  if (/^Opened INC/.test(x)) return ["!", "k"];
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
  const ds = duels().slice(-4);
  const roster = rosterRowsHtml(4);
  let html = roster || `<div class="du head" style="top:0"><span class="who">ATTACKER</span><span>ATTACK</span><span class="ar"></span><span>DEFENCE</span></div>`;
  if (!roster && !ds.length) html += `<div class="du" style="top:22px"><span class="who" style="background:var(--faint)">—</span><span class="dim">EVERY ATTACK WE LAUNCH</span><span class="ar">→</span><span class="dim">AND HOW THE AGENTS ANSWERED IT</span></div>`;
  if (!roster) ds.forEach((d, i) => {
    const key = `${d.id}|${d.def.t}`; const n = !seenDuel.has(key); seenDuel.add(key);
    html += `<div class="du${n ? " n" : ""}" style="top:${22 * (i + 1)}px"><span class="who">${esc(d.who)}</span><span class="atk">${esc(d.atk)}</span><span class="ar">→</span><span class="def ${d.def.c}">${esc(d.def.t)}</span></div>`;
  });
  // the one number: night 1 vs night 2
  const r = rounds(), liveR2 = [...state.incidents.values()].find((i) => i.active && i.round === 2);
  const t1 = r[1]?.ttr, t2 = r[2]?.ttr ?? (liveR2 ? (serverNow() - ts(liveR2.opened_at)) / 1000 : null);
  const max = Math.max(t1 || 0, t2 || 0, 1), bw = (t) => Math.round((t / max) * 220);
  const y = 22 * 5 + 8;
  html += `<div class="score" style="top:${y}px"><span class="dim">TIME TO FIX</span><span>NIGHT 1 ${t1 != null ? secs(t1).toUpperCase() : "—"}</span>${t1 != null ? `<span class="bar2" style="width:${bw(t1)}px"></span>` : ""}</div>`;
  html += `<div class="score" style="top:${y + 22}px"><span class="dim" style="visibility:hidden">TIME TO FIX</span><span>NIGHT 2 ${t2 != null ? secs(t2).toUpperCase() : "—"}</span>${t2 != null ? `<span class="bar2 r2" style="width:${bw(t2)}px"></span>` : ""}${r[1] && r[2] && r[2].ttr < r[1].ttr ? `<span class="x">${(r[1].ttr / r[2].ttr).toFixed(1)}× FASTER · ${Math.round(r[2].hops)} CHECK, NOT ${Math.round(r[1].hops)}</span>` : r[1] && !r[2] ? '<span class="dim">SAME ATTACK NEXT NIGHT. FASTER?</span>' : ""}</div>`;
  const pp = progressPanel("bees", null, true);
  if (pp) { // W24 on: the progress panel takes the fix-time rows (fix time stays on the overview box)
    html = html.replace(/<div class="score"[\s\S]*$/, "");
    html += `<div class="eta" data-eta="bees" style="top:${22 * 5 + 6}px">${pp}</div>`;
  }
  const host = $("#board");
  if (host.dataset.html !== html) { host.innerHTML = html; host.dataset.html = html; }
}

// keyed DOM patch: replace only the items whose markup changed (keeps stepped animations from restarting)
function patch(host, items, svg = false) {
  const have = new Map([...host.children].map((e) => [e.dataset.k, e]));
  for (const it of items) {
    const el = have.get(it.k);
    have.delete(it.k);
    if (el && el.dataset.h === it.html) continue;
    const t = document.createElement("template");
    t.innerHTML = svg ? `<svg xmlns="http://www.w3.org/2000/svg">${it.html}</svg>` : it.html.trim();
    const n = svg ? t.content.firstElementChild.firstElementChild : t.content.firstElementChild;
    n.dataset.k = it.k; n.dataset.h = it.html;
    if (el) el.replaceWith(n); else host.appendChild(n);
    for (const am of n.querySelectorAll ? n.querySelectorAll("animateMotion") : []) am.beginElement?.();
  }
  for (const e of have.values()) e.remove();
}

let lieShownAt = 0;
function renderBees() {
  const since = (runStartedAt() || 0) - 5000;
  const inc = focusIncident(), live = inc?.active ? inc : null;
  const lt = lastThoughts(since);
  const seats = seatMap();
  // lies caught this night (Guard rejects)
  const rejects = state.thoughts.filter((t) => isLie(t) && ts(t.ts) >= since && !String(t.agent || "").startsWith("red-"));
  const lieSys = new Set(rejects.map((t) => t.system || stepByAction(null, (String(t.text).match(/INC-\S+-a\d+/) || [])[0])?.system || "mainframe"));
  for (const s2 of (live?.steps || [])) if ((s2.attempts || []).length > 1) lieSys.add(s2.system);
  const lastRj = rejects[rejects.length - 1];
  const lieFresh = lastRj && serverNow() - ts(lastRj.ts) < 1500 && lastRj._id !== lieShownAt;

  const nodes = []; // [{k, html}] patched by key so live elements (and their animations) survive re-renders
  // systems
  for (const s2 of CHAIN) nodes.push({ k: `sys:${s2}`, html: sysModel(s2, inc, lieSys, lieFresh) });
  if (lieFresh) lieShownAt = lastRj._id;
  // agents
  const seatModels = {};
  for (const [id, p] of Object.entries(seats)) {
    const a = state.agents.get(id); if (!a) continue;
    const m = seatModel(a, since, lt, inc); seatModels[id] = m;
    const bar = Array.from({ length: 10 }, (_, i) => `<i class="${i < m.life ? "f" : ""}"></i>`).join("");
    nodes.push({ k: `seat:${id}`, html: `<div class="seat ${m.cls}" data-id="${esc(id)}" style="left:${p[0]}px;top:${p[1]}px"><div class="who"><span>${U(m.name)}</span><span class="gl">${GLYPH[m.role] || "·"}</span></div>
      <div class="rl">${TAG[m.role] || U(m.role)}${m.boss}</div><div class="bar" title="moves tonight">${bar}</div><div class="ph">${esc(m.ph)}</div><div class="tx">${esc(m.tx)}</div></div>` });
  }
  // attacker box: the personas who struck tonight
  const ds = duels();
  nodes.push({ k: "atk", html: `<div class="side red" style="left:${ATK.x}px;top:${ATK.y}px;width:${ATK.w}px"><div class="hd"><span>THE ATTACKERS</span><small>US</small></div>
    ${ds.length ? ds.slice(-4).map((d) => `<div class="ln"><span class="p">${esc(d.who)}</span> ${esc(d.atk)}</div>`).join("") : '<div class="ln dim">WAITING FOR NIGHT 1.</div><div class="ln dim">WE BREAK THE BANK;</div><div class="ln dim">THE AGENTS FIX IT.</div>'}</div>` });
  // memory box
  const pbs = [...state.playbooks.values()];
  const recallOn = live?.recall?.used || (inc?.recall?.used && inc?.status === "resolved");
  const saved = inc?.status === "resolved";
  const dots = Array.from({ length: 48 }, (_, i) => `<i class="${i < Math.min(48, pbs.length * 6) ? "on" : ""}"></i>`).join("");
  nodes.push({ k: "mem", html: `<div class="side mem" style="left:${MEM.x}px;top:${MEM.y}px"><div class="hd"><span>MONGODB · MEMORY</span><small>${(state.opsPerSec || 0).toFixed(1)} W/S</small></div>
    <div class="ln">${pbs.length} LESSON${pbs.length === 1 ? "" : "S"} SAVED${saved ? ' <span style="color:var(--amber)">+1 TONIGHT</span>' : ""}</div>
    <div class="ln">${recallOn ? '<span style="color:var(--amber)">RECALLED BY VECTOR SEARCH</span>' : live ? "SEARCHED BY MEANING" : "EVERY FIX IS REMEMBERED"}</div>
    <div class="ln dim">${state.thoughts.filter((t) => ts(t.ts) >= since).length} THOUGHTS TONIGHT</div><div class="dots">${dots}</div></div>` });

  // wires: orthogonal, in the two corridors above and below the chain, so they never run under a card
  const W = []; // [{k, html}]
  const CT = 266, CB = 436; // corridor y (top / bottom); each wire takes its own lane
  let lt0 = 0, lb0 = 0;
  const laneT = () => CT + 5 * (lt0++ % 4), laneB = () => CB - 5 * (lb0++ % 4);
  const path = (k, d, cls, label, lx, ly, anchor = "start") => W.push({ k, html: `<g><path class="w ${cls}" d="${d}"/>${label ? `<text class="wl ${cls.replace(/\bon\b/, "")} ${cls.includes("on") ? "on" : ""}" x="${lx}" y="${ly}" text-anchor="${anchor}">${esc(label)}</text>` : ""}</g>` });
  // the dependency chain (double rule)
  for (let i = 0; i < CHAIN.length - 1; i++) {
    const a = sysBox(CHAIN[i]), b = sysBox(CHAIN[i + 1]);
    const blames = state.systems.get(CHAIN[i])?.blames === CHAIN[i + 1] && live;
    const y = a.y + a.h / 2, x1 = a.x + a.w + 6, x2 = b.x - 8;
    W.push({ k: `chain:${i}`, html: `<g><path class="w dbl ${blames ? "red" : ""}" d="M${x1} ${y} L${x2} ${y}"/><path class="w dbl-in" d="M${x1} ${y} L${x2} ${y}"/><text class="wl ${blames ? "red" : ""}" x="${(x1 + x2) / 2}" y="${y - 8}" text-anchor="middle">${blames ? "BLAMES" : "NEEDS"}</text></g>` });
  }
  // agents → the system they are on (top seats drop to the system's top, bottom seats rise to its bottom)
  const atSys = {};
  for (const [id, m] of Object.entries(seatModels)) {
    if (!m.sys || m.cls.startsWith("idle") || m.cls.startsWith("dead")) continue;
    const p = seats[id], b = sysBox(m.sys), top = p[1] < b.y;
    const n = (atSys[`${m.sys}${top}`] = (atSys[`${m.sys}${top}`] || 0) + 1);
    const sx = p[0] + AW / 2, ex = b.x + 40 + (n - 1) * 26;
    const col = m.cls.includes("caught") ? "red" : m.cls.includes("ok") ? "grn" : m.cls.includes("saved") ? "amb" : "";
    const d = top ? `M${sx} ${p[1] + AH - 2} L${sx} ${laneT()} L${ex} ${CT + 5 * ((lt0 - 1) % 4)} L${ex} ${b.y - 8}`
      : `M${sx} ${p[1]} L${sx} ${laneB()} L${ex} ${CB - 5 * ((lb0 - 1) % 4)} L${ex} ${b.y + b.h + 8}`;
    path(`ag:${id}:${m.sys}`, d, `on ${col}`);
  }
  // attacker → systems it hit: out of the box's right side, up into the top corridor, down onto the system
  const hitSys = [...new Set(ds.filter((d) => d.e.fault !== "agent_kill" && SYSXY[d.e.target]).map((d) => d.e.target))];
  hitSys.forEach((s2, i) => {
    const b = sysBox(s2), sx = ATK.x + ATK.w, sy = ATK.y + 20 + 8 * i, cx = sx + 24 + 8 * i, cy = CT - 4 - 6 * i, ex = b.x + b.w - 24 - 8 * i;
    const hurt = (state.systems.get(s2)?.status || "up") !== "up" || (live && live.root_cause_system === s2);
    path(`atk:${s2}`, `M${sx} ${sy} L${cx} ${sy} L${cx} ${cy} L${ex} ${cy} L${ex} ${b.y - 8}`, `red ${hurt ? "on" : ""}`, i === 0 ? "ATTACKS" : "", sx + 4, sy - 5);
  });
  // attacker → killed agent
  for (const d of ds) {
    const v = d.e.fault === "agent_kill" && d.e.result?.agent; if (!v || !seats[v]) continue;
    const p = seats[v], vx = p[0] + AW / 2 + 20, sx = ATK.x + 30;
    const dd = p[1] > ATK.y ? `M${sx} ${ATK.y + 84} L${sx} ${CB + 6} L${vx} ${CB + 6} L${vx} ${p[1] - 2}` : `M${sx} ${ATK.y} L${sx} ${CT - 10} L${vx} ${CT - 10} L${vx} ${p[1] + AH - 2}`;
    path(`kill:${d.id}`, dd, "red on", `KILLED ${U(agentName(v))}`, sx + 8, p[1] > ATK.y ? CB + 2 : CT - 14);
  }
  // takeovers: the nurse's job came from the dead agent's seat
  for (const h of nightIncidents().flatMap((i) => i.handoffs || [])) {
    if (!seats[h.from] || !seats[h.to]) continue;
    const a = seats[h.from], b = seats[h.to], y = (a[1] > 300 ? a[1] + AH + 6 : a[1] - 8);
    const x1 = a[0] + AW / 2 + 40, x2 = b[0] + AW / 2 - 20;
    const yy = a[1] > 300 ? Math.min(FH, a[1] + AH + 4) : a[1] - 6;
    path(`ho:${h.from}:${h.to}`, a[1] === b[1] ? `M${x1} ${a[1] + AH - 2} L${x1} ${yy + 6} L${x2} ${yy + 6} L${x2} ${b[1] + AH - 2}` : `M${x1} ${a[1]} L${x1} ${y} L${x2} ${y} L${x2} ${b[1]}`,
      "amb on", "TOOK OVER ITS JOB", (x1 + x2) / 2, yy + 3, "middle");
  }
  // memory: recalled → the scout; lesson saved ← the fixed system
  const memH = 96;
  if (recallOn) {
    const sc = inc.owner && seats[inc.owner] ? inc.owner : Object.keys(seats).find((i) => roleOf(i) === "scout");
    if (sc) { const p = seats[sc], mx = MEM.x + 30, px = p[0] + AW / 2 + 30, y = CT - 10;
      path(`recall:${inc._id}`, `M${mx} ${MEM.y} L${mx} ${y} L${px} ${y} L${px} ${p[1] + AH - 2}`, "amb on", "REMEMBERED: SKIP THE SEARCH", mx - 8, y - 4, "end"); }
  }
  if (saved && SYSXY[inc.root_cause_system]) {
    const b = sysBox(inc.root_cause_system), y = b.y + b.h + 20, mx = MEM.x + 60;
    path(`saved:${inc._id}`, `M${b.x + b.w - 30} ${b.y + b.h + 6} L${b.x + b.w - 30} ${y} L${mx} ${y} L${mx} ${MEM.y + memH}`, "amb", "LESSON SAVED", mx - 8, y - 4, "end");
  }
  patch($("#wires"), W, true);
  patch($("#nodes"), nodes);

  renderStrips(since);
  renderBoard();
  void lastSeenThought;
}


// ================================================================== W24 progress + learned ETAs, W22 roster (both behind their switches)
// Progress: swarm.thoughts {kind:"progress", progress:{area, phase, step, of, pct, eta_s, eta_p90_s, eta_k, elapsed_s, text}} and
// swarm.stats {_id:"progress:<area>"} — both already on the change stream. Off = no such docs = every slot stays empty.
const progressByArea = new Map(); // area -> {..., at}
function noteProgress(p, at) {
  if (!p?.area) return;
  const cur = progressByArea.get(p.area);
  if (!cur || at >= cur.at) progressByArea.set(p.area, { ...p, at });
}
function seedProgress() { progressByArea.clear(); for (const t of state.thoughts) if (t.kind === "progress") noteProgress(t.progress, ts(t.ts)); }
const isProgress = (t) => t?.kind === "progress";
function prog(area, maxAgeS = 180) {
  const p = progressByArea.get(area);
  return p && (serverNow() - p.at) / 1000 <= maxAgeS ? p : null;
}
function etaWords(p) {
  if (!p) return "";
  if (p.phase === "done") return p.elapsed_s != null ? `DONE IN ${secs(p.elapsed_s).toUpperCase()}` : "DONE";
  if (p.eta_s == null || !p.eta_k) return "FIRST TIME · NO ETA YET";
  const left = Math.round(p.eta_s - (serverNow() - p.at) / 1000);
  return `${left > 0 ? `~${left} S LEFT` : "ANY MOMENT"} · ${p.eta_k} PAST RUN${p.eta_k === 1 ? "" : "S"}`;
}
// one short chip for a box's NOW line
function etaChip(area, filter) {
  const p = prog(area, 90);
  if (!p || p.phase === "done" || (filter && !filter(p))) return "";
  if (p.eta_s == null || !p.eta_k) return "NO ETA YET";
  const left = Math.round(p.eta_s - (serverNow() - p.at) / 1000);
  return left > 0 ? `~${left} S LEFT` : "ANY MOMENT";
}
// the panel inside a zoomed box: latest line, a bar, the ETA, and the last few events
function progressPanel(area, filter, compact = false) {
  const p = prog(area, 600);
  if (!p || (filter && !filter(p))) return "";
  const hist = state.thoughts.filter((t) => isProgress(t) && t.progress?.area === area && (!filter || filter(t.progress))).slice(-4).reverse();
  const pct = p.pct != null ? Math.max(0, Math.min(100, Math.round(p.pct > 1 ? p.pct : p.pct * 100))) : null; // 0..1 or 0..100
  return `<div class="pp"><div class="pp-h"><span class="pp-k">PROGRESS</span><span>${esc(String(p.text || "").toUpperCase())}</span></div>
    <div class="pp-b">${pct != null ? `<i style="width:${pct}%"></i>` : ""}</div>
    <div class="pp-e"><span class="pp-eta">${esc(etaWords(p))}</span>${p.eta_p90_s != null && p.phase !== "done" ? `<span class="dim">P90 ${secs(p.eta_p90_s).toUpperCase()}</span>` : ""}${pct != null ? `<span class="dim">${pct}%</span>` : ""}</div>
    ${compact ? "" : hist.slice(1).map((t) => `<div class="pp-l dim">${esc(String(t.progress?.text || t.text || "").toUpperCase())}</div>`).join("")}</div>`;
}
const bySystem = (sys) => (p) => !sys || p.system === sys;
const fixing = (p) => /^(step:|incident|diagnose:)/.test(String(p.action_type || ""));

// W22 roster: state.roster when store.js maps it, else our own copy from the snapshot + change stream
const localRoster = new Map();
const rosterMap = () => (state.roster instanceof Map && state.roster.size ? state.roster : localRoster);
function rosterRowsHtml(n = 5) {
  const R = [...rosterMap().values()];
  if (!R.length) return "";
  const rank = { working: 0, dead: 1, standby: 2, idle: 3, retired: 4 };
  const rows = R.filter((r) => !String(r._id).startsWith("red-")).sort((a, b) => (rank[a.state] ?? 5) - (rank[b.state] ?? 5)).slice(0, n);
  return `<div class="du ro head" style="top:0"><span class="who">AGENT</span><span>STATE · DOING</span><span class="ar"></span><span>REPORTS TO</span></div>`
    + rows.map((r, i) => `<div class="du ro" style="top:${22 * (i + 1)}px"><span class="who ${r.state === "dead" ? "" : "rw"}" style="${r.state === "dead" ? "" : "background:var(--ink);color:var(--paper)"}">${U(agentName(r._id))}</span><span class="${r.state === "dead" ? "r" : r.state === "working" ? "" : "dim"}">${U(r.state)}${r.doing ? ` · ${esc(String(r.doing).toUpperCase())}` : ""}</span><span class="ar">${r.parent ? "←" : ""}</span><span class="dim">${r.parent ? U(agentName(r.parent)) : r.took_over ? `TOOK OVER ${U(agentName(r.took_over.from))}` : ""}</span></div>`).join("");
}

// ================================================================== level 0: four boxes · zoom in / out
// CHAOS MONKEY → LEGACY SYSTEMS ← THE BEES, THE GARDENER below Legacy pointing up into it.
const BOXR = {
  chaos: { x: 40, y: 104, w: 360, h: 420 }, legacy: { x: 500, y: 104, w: 600, h: 420 },
  bees: { x: 1200, y: 104, w: 360, h: 420 }, gardener: { x: 580, y: 608, w: 440, h: 206 },
};
const FULL = { x: 0, y: 84, w: 1600, h: 760 };
const ZOOMS = ["overview", "chaos", "legacy", "bees", "gardener"];
const KEYZ = { 1: "chaos", 2: "legacy", 3: "bees", 4: "gardener" };
const TITLE = { overview: "EXHIBIT A: THE WHOLE NIGHT", chaos: "EXHIBIT A.1: INSIDE CHAOS MONKEY", legacy: "EXHIBIT A.2: INSIDE THE LEGACY SYSTEMS", bees: "EXHIBIT A.3: INSIDE THE BEES", gardener: "EXHIBIT A.4: INSIDE THE GARDENER" };
let zoom = (() => { try { const z = new URLSearchParams(location.search).get("zoom"); return ZOOMS.includes(z) ? z : "overview"; } catch { return "overview"; } })();
let zooming = false;
// transform that maps the FULL exhibit onto box b (and its inverse)
const toBox = (b) => { const s = Math.min(b.w / FULL.w, b.h / FULL.h), cx = b.x + b.w / 2, cy = b.y + b.h / 2; return `translate(${cx - s * (FULL.x + FULL.w / 2)}px, ${cy - s * (FULL.y + FULL.h / 2)}px) scale(${s})`; };
const fromBox = (b) => { const s = Math.max(FULL.w / b.w, FULL.h / b.h), cx = b.x + b.w / 2, cy = b.y + b.h / 2; return `translate(${FULL.x + FULL.w / 2 - s * cx}px, ${FULL.y + FULL.h / 2 - s * cy}px) scale(${s})`; };
function setZoom(z) {
  if (!ZOOMS.includes(z) || z === zoom || zooming) return;
  const from = zoom, inward = z !== "overview", box = BOXR[inward ? z : from];
  const ov = $("#v-overview"), det = $(`#v-${inward ? z : from}`);
  zooming = true;
  if (mode === "how") { mode = "live"; $("#v-how").hidden = true; }
  zoom = z; // render the destination once before it animates in
  for (const v of ZOOMS) $(`#v-${v}`).hidden = !(v === "overview" || v === (inward ? z : from));
  try { ({ chaos: renderChaos, legacy: renderLegacy, bees: renderBees, gardener: renderGardener, overview: renderOverview })[inward ? z : "overview"](); } catch (e) { console.error(e); }
  for (const el of [ov, det]) el.classList.remove("anim");
  // start state
  ov.style.transform = inward ? "none" : fromBox(box); ov.style.opacity = inward ? "1" : "0";
  det.style.transform = inward ? toBox(box) : "none"; det.style.opacity = inward ? "0" : "1";
  void det.offsetWidth;
  for (const el of [ov, det]) el.classList.add("anim");
  ov.style.transform = inward ? fromBox(box) : "none"; ov.style.opacity = inward ? "0" : "1";
  det.style.transform = inward ? "none" : toBox(box); det.style.opacity = inward ? "1" : "0";
  setTimeout(() => {
    for (const el of [ov, det]) { el.classList.remove("anim"); el.style.transform = ""; el.style.opacity = ""; }
    zooming = false;
    try { history.replaceState(null, "", z === "overview" ? location.pathname : `?zoom=${z}`); } catch { /* ignore */ }
    render();
  }, 470);
}

// live pulses: a dot runs along an arrow when the event it stands for really happens
const pulses = []; // {id, wire, color, at}
function pulse(wire, color) { pulses.push({ id: `${wire}:${Date.now()}:${Math.random()}`, wire, color, at: Date.now() }); }
function watchForPulses(m, prev) {
  if (m.coll === "chaos_events" && m.op === "insert" && m.doc) pulse(m.doc.fault === "agent_kill" ? "kill" : "atk", "var(--red)");
  if (m.coll === "chaos_events" && m.doc?.fault === "agent_kill" && m.doc?.result?.agent && !prev?.result?.agent) pulse("kill", "var(--red)");
  if (m.coll === "thoughts" && m.op === "insert" && m.doc) {
    if (m.doc.kind === "verify" || /^Verified /.test(m.doc.text || "")) pulse("fix", "var(--green)");
    if (m.doc.kind === "reject") pulse("fix", "var(--red)");
  }
  if (m.coll === "upgrade_requests" && m.doc) {
    if (m.op === "insert") pulse("req", "var(--amber)");
    if (m.doc.status === "deployed" && prev?.status !== "deployed") pulse("code", "var(--amber)");
    if (m.doc.status === "proven" && prev?.status !== "proven") pulse("code", "var(--green)");
  }
  if (m.coll === "incidents" && m.doc?.status === "resolved" && prev?.status !== "resolved") pulse("fix", "var(--green)");
}

// the Bees box's hive grid: one cell per agent
function hiveCells() {
  const R = state.roster instanceof Map ? state.roster : null;
  return [...state.agents.values()].filter((a) => !String(a._id).startsWith("red-"))
    .sort((a, b) => String(a._id).localeCompare(String(b._id), undefined, { numeric: true }))
    .map((a) => {
      const r = R?.get(a._id), role = a.role || roleOf(a._id);
      const cls = a.status === "dead" ? "dead" : (r?.took_over && a.status !== "idle") ? "took" : a.status === "working" || a.phase ? "work" : a.status === "standby" ? "stand" : "idle";
      return `<i class="hc ${cls}" title="${esc(agentName(a._id))}">${GLYPH[role] || "·"}</i>`;
    }).join("");
}
const tickMark = (s) => s.status === "verified" ? '<b class="g">✓</b>' : s.status === "failed" ? '<b class="r">X</b>' : ["sent", "unknown"].includes(s.status) ? "<b>›</b>" : '<b class="dim">·</b>';
function legacyChain(inc, lieSys) {
  const live = inc?.active ? inc : null, path = live?.diagnosis_path || [];
  const box = (s) => {
    const d = state.systems.get(s), st = d?.status || "?", root = live && live.root_cause_system === s;
    const cls = root ? "bad" : st === "up" ? "ok" : st === "down" ? "bad" : "warn";
    const w = root ? "ROOT CAUSE" : st === "up" ? ((d?.version || 1) > 1 ? `UP · V${d.version}` : "UP") : st === "down" ? "DOWN" : "TROUBLE";
    const sym = String(d?.symptom || "");
    const cap = !live ? (SYSROLE[s] || "") : root && st === "up" ? "SAYS OK. IT IS THE CAUSE" : st === "up" ? "WORKING"
      : s === "portal" && /stale/i.test(sym) ? "CUSTOMERS SEE OLD BALANCES" : s === "unix" ? "FILE TRANSFER FAILED" : s === "mainframe" ? "NIGHTLY BATCH CRASHED" : "NOT HEALTHY";
    return `<span class="mc ${cls}"><span class="mn">${U(sn(s))}</span><b>${w}</b><span class="ms">${esc(cap)}${live && path.includes(s) && !root ? " · CHECKED" : ""}</span>${lieSys.has(s) ? '<span class="stampS">ACK ≠ APPLIED</span>' : ""}</span>`;
  };
  const arrow = (a, b) => { const bl = live && state.systems.get(a)?.blames === b; return `<span class="mca ${bl ? "red" : ""}">${bl ? "BLAMES<br>→" : "NEEDS<br>→"}</span>`; };
  return `${box("portal")}${arrow("portal", "unix")}${box("unix")}${arrow("unix", "mainframe")}${box("mainframe")}`;
}
function liesTonight(since, live) {
  const rejects = state.thoughts.filter((t) => isLie(t) && ts(t.ts) >= since && !String(t.agent || "").startsWith("red-"));
  const lieSys = new Set(rejects.map((t) => t.system || stepByAction(null, (String(t.text).match(/INC-\S+-a\d+/) || [])[0])?.system || "mainframe"));
  for (const s2 of (live?.steps || [])) if ((s2.attempts || []).length > 1) lieSys.add(s2.system);
  return { rejects, lieSys };
}

// the Bees' one-line status: who is doing what, in as few words as possible
function beeNow(agents, live) {
  const PH = { diagnosing: "TRACING", step_sent: "FIXING", awaiting_verify: "CHECKING", awaiting_approval: "WAITS FOR YOU" };
  const busy = agents.filter((a) => a.status !== "dead" && (a.phase || a.status === "working") && a.role !== "queen");
  const a = busy.find((x) => x.phase === "step_sent") || busy.find((x) => x.phase) || busy[0];
  if (a) return `<span class="pn2">${U(agentName(a._id))}</span>${PH[a.phase] || "WORKING"}${SYSXY[a.target] ? ` ${U(sn(a.target))}` : ""}`;
  return live ? "ON IT" : '<span class="dim">ALL QUIET</span>';
}

function renderOverview() {
  const since = (runStartedAt() || 0) - 5000;
  const inc = focusIncident(), live = inc?.active ? inc : null;
  const ds = duels(), lt = lastThoughts(since);
  const agents = [...state.agents.values()].filter((a) => !String(a._id).startsWith("red-"));
  const dead = agents.filter((a) => a.status === "dead").length, working = agents.filter((a) => a.status === "working" || a.phase).length;
  const { rejects, lieSys } = liesTonight(since, live);
  const hos = nightIncidents().flatMap((i) => i.handoffs || []).length;
  const steps = inc?.steps || [];
  const notRed = (t) => !String(t?.agent || "").startsWith("red-");
  const last = lt.last && notRed(lt.last) ? lt.last : [...state.thoughts].reverse().find((t) => ts(t.ts) >= since && notRed(t) && !isProgress(t));
  const lastTx = last ? (translate(last)?.text || "").replace(/<[^>]+>/g, "") : "";
  const r = rounds(), liveR2 = [...state.incidents.values()].find((i) => i.active && i.round === 2);
  const t1 = r[1]?.ttr, t2 = r[2]?.ttr ?? (liveR2 ? (serverNow() - ts(liveR2.opened_at)) / 1000 : null);
  const mx = Math.max(t1 || 0, t2 || 0, 1), bw = (t) => Math.round((t / mx) * 150);
  const look = (n) => `<div class="look"><span>${n}</span>LOOK INSIDE +</div>`;
  const ETAF = { chaos: ["chaos"], legacy: ["bees", fixing], bees: ["bees"], gardener: ["gardener"] };
  const liveLine = (k, html) => `<div class="nowl"><span class="lk">NOW</span>${html} <span class="eta" data-eta="${k}">${etaChip(...ETAF[k])}</span></div>`; // W24 progress/ETA
  const B = BOXR;
  const lieFresh = rejects.some((t) => serverNow() - ts(t.ts) < 12000);
  const killFresh = ds.some((d) => d.e.fault === "agent_kill" && d.e.result?.agent && serverNow() - ts(d.e.done_at || d.e.ts) < 20000);
  const u = latestUpgrade();
  const at = u ? ["requested", "planning", "testing", "awaiting_approval", "deploying", "deployed", "proven"].indexOf(u.status) : -1;
  const pos = (b) => `left:${b.x}px;top:${b.y}px;width:${b.w}px;height:${b.h}px`;

  // CHAOS MONKEY
  const struck = new Set(ds.map((d) => d.who));
  const lastDuel = ds[ds.length - 1];
  const chaos = `<div class="big chaos ${ds.length ? "hot" : ""}" data-zoom="chaos" style="${pos(B.chaos)}">
    <div class="bh"><span class="bt">CHAOS MONKEY</span><span class="dim">US, ATTACKING</span></div>
    <div class="pers">${["SABOTEUR", "LIAR", "ASSASSIN", "HACKER", "DRIFTER", "MEMORY"].map((n) => `<span class="${struck.has(n) ? "on" : ""}">${n}</span>`).join("")}</div>
    <div class="bl-k">ATTACK <span class="ar">→</span> DEFENCE</div>
    ${ds.length ? ds.slice(-2).map((d) => `<div class="duo"><div><span class="pn">${esc(d.who)}</span> ${esc(d.atk)}</div><div class="def ${d.def.c}">↳ ${esc(d.def.t)}</div></div>`).join("") : '<div class="bl dim">—</div>'}
    ${liveLine("chaos", lastDuel ? `<span class="r">${esc(lastDuel.who)}</span> ${esc(lastDuel.atk)}` : '<span class="dim">READY</span>')}
    ${look(1)}</div>`;

  // LEGACY SYSTEMS
  const cascade = live ? (live.root_cause_system ? `REAL CAUSE: ${sn(live.root_cause_system)}` : `${sn(live.system)} IN TROUBLE · TRACING…`) : inc?.status === "resolved" ? "ALL GREEN AGAIN" : "ALL UP";
  const legacy = `<div class="big legacy ${live ? "hot" : ""}" data-zoom="legacy" style="${pos(B.legacy)}">
    <div class="bh"><span class="bt">LEGACY SYSTEMS</span><span class="dim">THE OLD BANK</span></div>
    <div class="chainL">${legacyChain(inc, lieSys)}</div>
    <div class="bl-k" style="margin-top:22px">FIXES${steps.length ? ` · ${steps.filter((s) => s.status === "verified").length}/${steps.length} VERIFIED` : ""}</div>
    <div class="bl">${steps.length ? steps.slice(0, 4).map((s) => `${tickMark(s)} ${U(sop(s.op))}${(s.attempts || []).length > 1 ? ' <b class="r">LIED</b>' : ""}`).join("&nbsp;&nbsp;") : inc?.status === "resolved" ? '<span class="g">ALL FIXED ✓</span>' : '<span class="dim">—</span>'}</div>
    ${liveLine("legacy", esc(cascade.toUpperCase()))}
    ${look(2)}</div>`;

  // THE BEES
  const bees = `<div class="big bees ${live || working ? "hot" : ""}" data-zoom="bees" style="${pos(B.bees)}">
    <div class="bh"><span class="bt">THE BEES</span><span class="dim">${agents.length - dead} AI AGENTS</span></div>
    <div class="hive">${hiveCells()}</div>
    <div class="cnt"><span class="${rejects.length ? "r" : ""} ${lieFresh ? "flash" : ""}">LIES <b>${rejects.length}</b></span><span class="${dead ? "r" : ""} ${killFresh ? "flash" : ""}">KILLED <b>${dead}</b></span><span class="${hos ? "a" : ""}">TAKEOVERS <b>${hos}</b></span></div>
    <div class="ttf"><span class="dim">FIX TIME</span><span>N1 ${t1 != null ? secs(t1).toUpperCase() : "—"}</span>${t1 != null ? `<i style="width:${bw(t1)}px"></i>` : ""}</div>
    <div class="ttf"><span class="dim" style="visibility:hidden">FIX TIME</span><span>N2 ${t2 != null ? secs(t2).toUpperCase() : "—"}</span>${t2 != null ? `<i class="g2" style="width:${bw(t2)}px"></i>` : ""}${r[1] && r[2] && r[2].ttr < r[1].ttr ? `<span class="x">${(r[1].ttr / r[2].ttr).toFixed(1)}×</span>` : ""}</div>
    ${liveLine("bees", beeNow(agents, live))}
    ${look(3)}</div>`;

  // THE GARDENER
  const gThought = [...state.thoughts].reverse().find((t) => roleOf(t.agent) === "gardener");
  const gard = `<div class="big gard ${u && !["proven", "rejected"].includes(u.status) ? "hot" : ""}" data-zoom="gardener" style="${pos(B.gardener)}">
    <div class="bh"><span class="bt">THE GARDENER</span><span class="dim">PERMANENT FIXES</span></div>
    <div class="gsteps">${["REQUEST", "WRITE", "TEST", "YOUR OK", "DEPLOY", "PROVE", "FIXED"].map((n, i) => `<span class="${u?.status === "rejected" ? "" : at > i || u?.status === "proven" ? "done" : at === i ? "cur" : ""}">${n}</span>`).join("")}</div>
    ${liveLine("gardener", u ? (u.status === "proven" ? `<span class="g">${U(sn(u.system))} V${u.version_before ?? 1} → V${u.version_after ?? 2}: ATTACK REPLAYED, NOTHING BROKE</span>` : esc((gThought ? (translate(gThought)?.text || "").replace(/<[^>]+>/g, "") : String(u.status)).toUpperCase())) : '<span class="dim">WAITING FOR A REPEAT</span>')}
    <div class="look" style="height:24px;line-height:22px"><span>4</span>LOOK INSIDE +</div></div>`;

  patch($("#ov-nodes"), [{ k: "chaos", html: chaos }, { k: "legacy", html: legacy }, { k: "bees", html: bees }, { k: "gard", html: gard }]);

  // arrows
  const atkNow = ds.some((d) => d.e.fault !== "agent_kill" && serverNow() - ts(d.e.ts) < 25000) || !!live;
  const fixNow = state.thoughts.some((t) => (t.kind === "verify" || /^Verified /.test(t.text || "")) && serverNow() - ts(t.ts) < 8000);
  const deployed = u && ["deployed", "proven"].includes(u.status);
  const cx = B.chaos.x + B.chaos.w, lx1 = B.legacy.x, lx2 = B.legacy.x + B.legacy.w, bx = B.bees.x;
  const gy = B.gardener.y, gyb = B.gardener.y + B.gardener.h;
  const WIRES = {
    atk: { d: `M${cx + 6} 250 L${lx1 - 12} 250`, cls: `red ${atkNow ? "on" : ""}`, label: "ATTACKS", lx: (cx + lx1) / 2, ly: 242 },
    fix: { d: `M${bx - 6} 250 L${lx2 + 12} 250`, cls: `${fixNow ? "grn on" : live ? "on" : ""}`, label: "WATCH + FIX", lx: (bx + lx2) / 2, ly: 242 },
    lie: { d: `M${bx - 6} 330 L${lx2 + 12} 330`, cls: `${rejects.length ? "red" : ""} ${lieFresh ? "on" : ""}`, label: rejects.length ? "LIE CAUGHT" : "CHECKS", lx: (bx + lx2) / 2, ly: 322 },
    kill: { d: `M${B.chaos.x + B.chaos.w / 2} ${B.chaos.y - 6} L${B.chaos.x + B.chaos.w / 2} 92 L${B.bees.x + B.bees.w / 2} 92 L${B.bees.x + B.bees.w / 2} ${B.bees.y - 12}`, cls: `red ${killFresh ? "on" : ""}`, label: "KILLS A BEE", lx: 800, ly: 88 },
    code: { d: `M800 ${gy - 6} L800 ${B.legacy.y + B.legacy.h + 12}`, cls: `${deployed ? "amb" : ""} ${u?.status === "deployed" ? "on" : ""}`, label: deployed ? `NEW CODE V${u.version_after ?? 2}` : "NEW CODE", lx: 810, ly: (gy + B.legacy.y + B.legacy.h) / 2 + 4, anchor: "start" },
    req: { d: `M${B.bees.x + B.bees.w / 2} ${B.bees.y + B.bees.h + 6} L${B.bees.x + B.bees.w / 2} ${gy + 60} L${B.gardener.x + B.gardener.w + 12} ${gy + 60}`, cls: `${u ? "amb" : ""} ${u && ["requested", "planning"].includes(u.status) ? "on" : ""}`, label: "SAME FAILURE ×2", lx: B.bees.x + B.bees.w / 2 - 10, ly: gy + 52, anchor: "end" },
    prove: { d: `M${B.chaos.x + B.chaos.w / 2} ${B.chaos.y + B.chaos.h + 6} L${B.chaos.x + B.chaos.w / 2} ${gy + 60} L${B.gardener.x - 12} ${gy + 60}`, cls: `${u?.status === "proven" ? "grn" : u?.status === "deployed" ? "red on" : ""}`, label: u?.status === "proven" ? "REPLAYED: NOTHING BROKE" : "PROOF: REPLAY", lx: B.chaos.x + B.chaos.w / 2 + 10, ly: gy + 52 },
  };
  const mk = (cls) => (cls.includes("red") ? "r" : cls.includes("amb") ? "a" : cls.includes("grn") ? "g" : "i");
  const W = [{ k: "defs", html: `<defs>${[["r", "var(--red)"], ["a", "var(--amber)"], ["g", "var(--green)"], ["i", "var(--faint)"]].map(([n, c]) => `<marker id="ah-${n}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="${c}"/></marker>`).join("")}</defs>` }];
  for (const [k, w] of Object.entries(WIRES)) W.push({ k, html: `<g><path id="wire-${k}" class="w ${w.cls}" d="${w.d}" marker-end="url(#ah-${mk(w.cls)})"/><text class="wl ${w.cls.replace(/\bon\b/, "")}" x="${w.lx}" y="${w.ly}" text-anchor="${w.anchor || "middle"}">${esc(w.label)}</text></g>` });
  const now = Date.now();
  for (let i = pulses.length - 1; i >= 0; i--) if (now - pulses[i].at > 1400) pulses.splice(i, 1);
  for (const p of pulses) if (WIRES[p.wire]) W.push({ k: p.id, html: `<circle class="pulse" r="5" fill="${p.color}" style="color:${p.color}"><animateMotion begin="indefinite" dur="1.1s" fill="freeze" path="${WIRES[p.wire].d}"/></circle>` });
  patch($("#ov-wires"), W, true);
}

// ================================================================== "How it works": one architecture diagram per box
// n: [id, x, y, w, h, title, sub, kind]  kind: agent | db | sys | ext | ai     e: [from, to, label, cls?, via?]
const HOW = {
  chaos: {
    lead: "ATTACKERS PLAN, STRIKE, WATCH THE BEES, AND LEARN.",
    n: [
      ["plan", 60, 170, 210, 90, "SCENARIO", "seeded nights 1 · 2 · 3", "ext"],
      ["scouts", 340, 170, 220, 90, "2 SCOUTS", "hive · systems", "agent"],
      ["cmd", 630, 170, 260, 90, "COMMANDER", "observe → think → act → reflect", "ai"],
      ["strikers", 960, 170, 260, 90, "5 STRIKERS", "saboteur · liar · assassin · hacker · drifter", "agent"],
      ["runner", 1290, 170, 250, 90, "RUNNER", "chaos.runner.fire()", "agent"],
      ["legacy", 1290, 380, 250, 80, "LEGACY SYSTEMS", "faults land here", "sys"],
      ["bees", 960, 380, 260, 80, "THE BEES", "SIGKILL a real process", "sys"],
      ["red_agents", 340, 560, 220, 56, "red_agents", "findings board", "db"],
      ["events", 960, 560, 260, 56, "chaos_events", "every strike + result", "db"],
      ["lessons", 630, 560, 260, 56, "lessons", "what worked last time", "db"],
    ],
    e: [["plan", "runner", "", "", "M270 215 L270 130 L1415 130 L1415 164"], ["scouts", "cmd", "FINDINGS"], ["cmd", "strikers", "ONE CAMPAIGN"], ["strikers", "runner", "STRIKE"],
      ["runner", "legacy", "FAULT", "red"], ["runner", "bees", "KILL", "red", "M1330 260 L1330 330 L1090 330 L1090 374"], ["scouts", "red_agents", "POST"], ["runner", "events", "LOG", "", "M1540 230 L1572 230 L1572 588 L1226 588"],
      ["events", "lessons", "REFLECT"], ["lessons", "cmd", "LEARN", "amb"]],
    note: "CLAUDE PICKS ONE MOVE FROM A VETTED MENU · NO KEY = A FIXED PLAYBOOK · EVERY STRIKE GOES THROUGH ONE RUNNER",
  },
  legacy: {
    lead: "THREE OLD SYSTEMS. EACH ONE NEEDS THE NEXT.",
    n: [
      ["portal", 80, 200, 330, 120, "PORTAL", "bank website · answers 200 even when stale", "sys"],
      ["unix", 630, 200, 330, 120, "UNIX", "cron pulls the nightly file", "sys"],
      ["mf", 1180, 200, 330, 120, "MAINFRAME", "1987 ledger · NIGHTBAT batch", "sys"],
      ["api", 630, 420, 330, 80, "SAME API ON ALL THREE", "/health /metrics /logs /ops /state", "ext"],
      ["systems", 80, 600, 330, 56, "world.systems", "status · blames · version", "db"],
      ["receipts", 630, 600, 330, 56, "op_receipts", "one per action id: no double-apply", "db"],
      ["logs", 1180, 600, 330, 56, "system_logs", "what each one prints", "db"],
    ],
    e: [["portal", "unix", "NEEDS"], ["unix", "mf", "NEEDS"], ["api", "systems", "", "", "M700 500 L700 550 L245 550 L245 594"], ["api", "receipts", ""], ["api", "logs", "", "", "M890 500 L890 550 L1345 550 L1345 594"],
      ["portal", "api", "", "faint", "M245 320 L245 460 L624 460"], ["mf", "api", "", "faint", "M1345 320 L1345 460 L966 460"]],
    note: "BREAK THE MAINFRAME → UNIX GETS NO FILE → THE WEBSITE SHOWS OLD BALANCES. THE SYMPTOM IS FAR FROM THE CAUSE.",
  },
  bees: {
    lead: "EVERY AGENT IS ITS OWN PROCESS. ALL STATE LIVES IN MONGODB.",
    n: [
      ["sent", 40, 160, 190, 84, "SENTINEL ×2", "watch /health", "agent"],
      ["queen", 270, 160, 190, 84, "QUEEN", "+ backup queen", "agent"],
      ["scouts", 500, 160, 210, 84, "SCOUTS", "trace the cause", "agent"],
      ["workers", 750, 160, 210, 84, "WORKERS", "send one fix", "agent"],
      ["guard", 1000, 160, 210, 84, "GUARD", "read back: is it true?", "agent"],
      ["legacy", 1250, 160, 300, 84, "LEGACY SYSTEMS", "/ops · /state", "sys"],
      ["nurse", 750, 330, 210, 76, "NURSE", "takes over a dead job", "agent"],
      ["comms", 270, 330, 190, 76, "COMMS", "messages · roster", "agent"],
      ["ai", 500, 330, 210, 76, "CLAUDE", "second opinion", "ai"],
      ["incidents", 40, 520, 190, 56, "incidents", "", "db"], ["agents", 270, 520, 190, 56, "agents", "heartbeats", "db"],
      ["playbooks", 500, 520, 210, 56, "playbooks", "$vectorSearch", "db"], ["checkpoints", 750, 520, 210, 56, "checkpoints", "LangGraph", "db"],
      ["ledger", 1000, 520, 210, 56, "ledger", "every attempt", "db"], ["approvals", 1250, 520, 300, 56, "approvals", "a human OK", "db"],
    ],
    e: [["sent", "queen", "ALARM"], ["queen", "scouts", "ASSIGN"], ["scouts", "workers", "STEPS"], ["workers", "guard", "CHECK"], ["guard", "legacy", "READ BACK", "grn"],
      ["workers", "legacy", "", "", "M855 160 L855 130 L1400 130 L1400 154"], ["nurse", "workers", "RESUME", "amb"], ["checkpoints", "nurse", "", "amb"],
      ["sent", "incidents", ""], ["queen", "agents", "", "faint"], ["scouts", "playbooks", "RECALL", "amb", "M650 244 L650 514"], ["workers", "checkpoints", "", "faint", "M900 244 L900 514"],
      ["guard", "ledger", "VERIFY", "grn"], ["ai", "scouts", "", "faint"], ["comms", "agents", "", "faint"], ["workers", "approvals", "RISKY?", "warn", "M940 244 L940 280 L1400 280 L1400 514"]],
    note: "LEASES IN MONGODB: KILL ANY AGENT, ANOTHER PICKS UP ITS JOB FROM THE CHECKPOINT · ONLY THE GUARD MAY SAY “VERIFIED”",
  },
  gardener: {
    lead: "A FAILURE THAT COMES BACK BECOMES NEW, TESTED CODE.",
    n: [
      ["sent", 40, 180, 200, 84, "SENTINEL", "same failure ×2", "agent"],
      ["req", 290, 180, 200, 84, "REQUEST", "upgrade_requests", "db"],
      ["write", 540, 180, 200, 84, "WRITE", "a small, known fix", "agent"],
      ["test", 790, 180, 200, 84, "TEST", "pytest + replay in a sandbox", "agent"],
      ["ok", 1040, 180, 200, 84, "YOUR OK", "approvals", "ext"],
      ["deploy", 1290, 180, 250, 84, "DEPLOY", "hot reload · version +1", "agent"],
      ["legacy", 1290, 380, 250, 84, "LEGACY SYSTEMS", "now running v2", "sys"],
      ["prove", 790, 380, 250, 84, "PROVE", "chaos replays the attack", "agent"],
      ["stale", 290, 380, 300, 84, "OLD PLAYBOOKS", "marked stale", "db"],
    ],
    e: [["sent", "req", ""], ["req", "write", ""], ["write", "test", ""], ["test", "ok", ""], ["ok", "deploy", ""], ["deploy", "legacy", "NEW CODE", "amb"],
      ["legacy", "prove", "ATTACK AGAIN", "red"], ["prove", "stale", "", "faint"]],
    note: "CLAUDE ONLY EXPLAINS THE FIX · NOTHING SHIPS WITHOUT TESTS AND A HUMAN OK · IT IS PROVEN BY THE SAME ATTACK",
  },
};
let mode = "live"; // live | how
function renderHow() {
  const H = HOW[zoom]; if (!H) return;
  const N = Object.fromEntries(H.n.map((n) => [n[0], { id: n[0], x: n[1], y: n[2], w: n[3], h: n[4], t: n[5], s: n[6], k: n[7] }]));
  const edge = ([a, b, label, cls = "", via]) => {
    const A = N[a], B = N[b];
    let d = via, lx, ly;
    if (!d) {
      const ac = { x: A.x + A.w / 2, y: A.y + A.h / 2 }, bc = { x: B.x + B.w / 2, y: B.y + B.h / 2 };
      if (Math.abs(ac.y - bc.y) < 20) { const x1 = ac.x < bc.x ? A.x + A.w : A.x, x2 = ac.x < bc.x ? B.x - 10 : B.x + B.w + 10; d = `M${x1} ${ac.y} L${x2} ${bc.y}`; lx = (x1 + x2) / 2; ly = ac.y - 30; }
      else if (Math.abs(ac.x - bc.x) < 20) { const y1 = ac.y < bc.y ? A.y + A.h : A.y, y2 = ac.y < bc.y ? B.y - 10 : B.y + B.h + 10; d = `M${ac.x} ${y1} L${bc.x} ${y2}`; lx = ac.x + 8; ly = (y1 + y2) / 2 + 4; }
      else { const y1 = ac.y < bc.y ? A.y + A.h : A.y, y2 = ac.y < bc.y ? B.y - 10 : B.y + B.h + 10, my = (y1 + y2) / 2; d = `M${ac.x} ${y1} L${ac.x} ${my} L${bc.x} ${my} L${bc.x} ${y2}`; lx = (ac.x + bc.x) / 2; ly = my - 6; }
    } else { const pts = d.match(/-?\d+(\.\d+)?/g).map(Number); lx = (pts[0] + pts[2]) / 2 + 8; ly = (pts[1] + pts[3]) / 2 - 6; }
    const mk = cls.includes("red") ? "r" : cls.includes("amb") ? "a" : cls.includes("grn") ? "g" : cls.includes("warn") ? "a" : "i2";
    labels.push(label ? `<text class="wl ${cls}" x="${lx}" y="${ly}" text-anchor="${via ? "start" : "middle"}">${esc(label)}</text>` : "");
    return `<path class="w how ${cls}" d="${d}" marker-end="url(#hw-${mk})"/>`;
  };
  const labels = [];
  const svg = `<svg class="wires" viewBox="0 0 1600 900" style="position:absolute;left:0;top:0;width:1600px;height:900px"><defs>${[["r", "var(--red)"], ["a", "var(--amber)"], ["g", "var(--green)"], ["i2", "var(--muted)"]].map(([n, c]) => `<marker id="hw-${n}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="${c}"/></marker>`).join("")}</defs>${H.e.map(edge).join("")}</svg>`;
  const nodes = Object.values(N).map((n) => `<div class="hn ${n.k}" style="left:${n.x}px;top:${n.y}px;width:${n.w}px;height:${n.h}px"><div class="ht">${esc(n.t)}</div>${n.s ? `<div class="hs">${esc(n.s)}</div>` : ""}</div>`).join("");
  const over = `<svg class="wires" viewBox="0 0 1600 900" style="position:absolute;left:0;top:0;width:1600px;height:900px">${labels.join("")}</svg>`;
  const html = `<div class="zc">${svg}<div class="zcol how-lead" style="left:60px;top:96px;width:1480px">${esc(H.lead)}</div>${nodes}
    <div class="how-key" style="left:60px;top:700px"><span class="hn agent sm">AGENT</span><span class="hn ai sm">CLAUDE</span><span class="hn db sm">MONGODB COLLECTION</span><span class="hn sys sm">SYSTEM</span></div>
    <div class="zcol how-note" style="left:60px;top:752px;width:1480px">${esc(H.note)}</div>${over}</div>`;
  const host = $("#how-body");
  if (host.dataset.h !== html) { host.innerHTML = html; host.dataset.h = html; }
}
function setMode(m) {
  if (zoom === "overview" || m === mode) return;
  mode = m; render();
}

// ================================================================== level 1: inside Chaos Monkey
function renderChaos() {
  const ds = duels();
  const PERS = [["SABOTEUR", "Legacy breakage: crashed batches, full disks, dead scheduled jobs, stuck sessions."], ["ASSASSIN", "Really kills (SIGKILL) an agent's process in the middle of a job."],
    ["LIAR", "Makes a system answer “done” without doing anything, or apply late."], ["HACKER", "Walks in through holes planted in the old systems."], ["DRIFTER", "Silently renames data fields under the agents."],
    ["MEMORY", "Poisons a fix the Hive remembers, so recall must match what the Guard verified."]];
  const struck = new Map(); for (const d of ds) struck.set(d.who, (struck.get(d.who) || 0) + 1);
  const clock = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString([], { hour12: false }); };
  const rows = ds.slice(-15);
  const html = `<div class="zc">
    <div class="zcol" style="left:60px;top:150px;width:470px">
      <div class="bl-k">THE ATTACKERS · US</div>
      ${PERS.map(([n, d]) => `<div class="pz ${struck.has(n) ? "on" : ""}"><div><span class="pn">${n}</span><span class="pc">${struck.has(n) ? `${struck.get(n)} TONIGHT` : "NOT TONIGHT"}</span></div><div class="pd">${esc(d)}</div></div>`).join("")}
      <div class="bl-k" style="margin-top:14px">HOW THE BEES DEFEND</div>
      <div class="pd">Every “done” is re-checked by the Guard, every job is saved in MongoDB so a nurse can take it over, every fix is remembered for next time, and a repeat failure becomes new code.</div>
    </div>
    <div class="zcol" style="left:580px;top:150px;width:980px">
      <div class="bl-k">ATTACK <span class="ar">→</span> DEFENCE · EVERY ATTACK TONIGHT AND WHAT ANSWERED IT</div>
      <div class="dz head"><span>TIME</span><span>ATTACKER</span><span>ATTACK</span><span></span><span>DEFENCE</span></div>
      ${rows.length ? rows.map((d) => `<div class="dz"><span class="dim">${clock(d.e.ts)}</span><span class="who">${esc(d.who)}</span><span>${esc(d.atk)}</span><span class="ar">→</span><span class="def ${d.def.c}">${esc(d.def.t)}</span></div>`).join("") : '<div class="dz"><span></span><span class="who" style="background:var(--faint)">—</span><span class="dim">NO ATTACKS YET TONIGHT.</span><span></span><span class="dim">PRESS START.</span></div>'}
    </div>
    <div class="eta" data-eta="chaos" style="left:580px;top:640px;width:980px">${progressPanel("chaos")}</div>
  </div>`;
  const host = $("#chaos-body");
  if (host.dataset.h !== html) { host.innerHTML = html; host.dataset.h = html; }
}

// ================================================================== level 1: inside the Legacy systems
function renderLegacy() {
  const since = (runStartedAt() || 0) - 5000;
  const inc = focusIncident(), live = inc?.active ? inc : null, path = live?.diagnosis_path || [];
  const { lieSys } = liesTonight(since, live);
  const ledger = [...state.ledger.values()].filter((l) => ts(l.ts) >= since).sort((a, b) => ts(a.ts) - ts(b.ts));
  const X = { portal: 60, unix: 580, mainframe: 1100 };
  const cards = CHAIN.map((s) => {
    const d = state.systems.get(s) || {}, st = d.status || "?", m = d.metrics || {}, root = live && live.root_cause_system === s;
    const cls = root || st === "down" ? "bad" : st === "up" ? "" : "warn";
    const w = root ? "ROOT CAUSE" : st === "up" ? "UP" : st === "down" ? "DOWN" : "TROUBLE";
    const rows = [["SAYS", `${U(st)}${d.blames ? ` · BLAMES ${U(sn(d.blames))}` : ""}`], ["VERSION", `V${esc(d.version ?? 1)}${(d.version || 1) > 1 ? ' <span class="g">UPGRADED</span>' : ""}`],
      ["SYMPTOM", d.symptom ? esc(String(d.symptom)).slice(0, 60) : "—"], ["LATENCY", m.latency_ms != null ? `${Math.round(m.latency_ms)} MS` : "—"],
      ["ERRORS", m.error_rate != null ? `${(m.error_rate * 100).toFixed(0)}%` : "—"], [s === "mainframe" ? "BATCH" : s === "unix" ? "DISK" : "SESSIONS", esc(s === "mainframe" ? (m.batch_status || "—") : s === "unix" ? (m.disk_pct != null ? `${Math.round(m.disk_pct)}%` : "—") : (m.sessions ?? "—"))]];
    const ops = ledger.filter((l) => l.system === s).slice(-4);
    const logs = (state.logs[s] || []).slice(-4);
    return `<div class="lz ${cls}" style="left:${X[s]}px">
      ${root ? '<span class="band">ROOT CAUSE</span>' : live && path.includes(s) ? '<span class="band ck">CHECKED BY THE SCOUT</span>' : ""}
      <h3><span>${U(sn(s))}</span><small>${SYSROLE[s] || ""}</small></h3>
      <div class="ss" style="margin:6px 0"><b class="${cls === "bad" ? "r" : cls === "warn" ? "" : "g"}">■ ${w}</b></div>
      ${rows.map(([k, v]) => `<div class="kv"><span class="dim">${k}</span><span>${v}</span></div>`).join("")}
      <div class="bl-k">FIXES SENT HERE TONIGHT</div>
      ${ops.length ? ops.map((l) => `<div class="kv"><span>${l.status === "verified" ? '<b class="g">✓ VERIFIED</b>' : l.status === "failed" ? '<b class="r">X IT LIED</b>' : U(l.status)}</span><span>${U(sop(l.op))}</span></div>`).join("") : '<div class="pd">None.</div>'}
      <div class="bl-k">ITS OWN LOG</div>
      ${logs.length ? logs.map((g) => `<div class="log">${esc(prettyTime(g.line || g.code || ""))}</div>`).join("") : '<div class="pd">Quiet.</div>'}
      ${lieSys.has(s) ? '<span class="stamp">ACK ≠ APPLIED</span>' : ""}
    </div>`;
  }).join("");
  const arrows = [["portal", "unix", 500], ["unix", "mainframe", 1020]].map(([a, b, x]) => {
    const bl = live && state.systems.get(a)?.blames === b;
    return `<path class="w dbl ${bl ? "red" : ""}" d="M${x + 6} 400 L${x + 66} 400"/><path class="w dbl-in" d="M${x + 6} 400 L${x + 66} 400"/><text class="wl ${bl ? "red" : ""}" x="${x + 40}" y="390" text-anchor="middle">${bl ? "BLAMES" : "NEEDS"}</text>`;
  }).join("");
  const html = `<div class="zc"><svg class="wires" viewBox="0 0 1600 900" style="position:absolute;left:0;top:0;width:1600px;height:900px">${arrows}</svg>
    <div class="zcol" style="left:60px;top:86px;width:1480px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"><span class="dim">CASCADE&nbsp;&nbsp;</span>${esc((live ? nowLine().replace(/<[^>]+>/g, "") : "the website needs the unix server, which needs the mainframe").toUpperCase())}</div>
    ${cards}<div class="eta" data-eta="legacy-inside" style="left:60px;top:704px;width:1480px">${progressPanel("bees", fixing)}</div></div>`;
  const host = $("#legacy-body");
  if (host.dataset.h !== html) { host.innerHTML = html; host.dataset.h = html; }
}

// ================================================================== level 1: inside the Gardener
function renderGardener() {
  const u = latestUpgrade();
  const order = ["requested", "planning", "testing", "awaiting_approval", "deploying", "deployed", "proven"];
  const NAMES = ["REQUEST", "WRITE", "TEST", "YOUR OK", "DEPLOY", "PROVE", "FIXED"];
  const WHY = ["Same failure twice → the Lookout files a request", "Reads the old code, writes a small fix", "Runs the tests and replays the attack in a sandbox", "Nothing ships without a human OK", "Installs the code and bumps the version", "Chaos Monkey replays the original attack", "The attack came back. Nothing broke."];
  const at = u ? order.indexOf(u.status) : -1;
  const x0 = 60, wd = 200, gap = 13;
  const stages = NAMES.map((n, i) => { const st = u?.status === "rejected" ? "" : at > i || u?.status === "proven" ? "done" : at === i ? "cur" : ""; return `<div class="gz ${st}" style="left:${x0 + i * (wd + gap)}px;top:160px;width:${wd}px"><div class="gzn"><b>${st === "done" ? "✓" : i + 1}</b> ${n}</div><div class="pd">${WHY[i]}</div></div>`; }).join("");
  const thoughts = state.thoughts.filter((t) => roleOf(t.agent) === "gardener" || t.kind === "upgrade").slice(-9);
  const tests = u?.tests ? ((u.tests.pytest_passed ?? u.tests.passed) ? '<span class="g">PASSED</span>' : '<span class="r">FAILED</span>') : "—";
  const kv = [["SYSTEM", u ? U(sn(u.system)) : "—"], ["WEAKNESS", u ? esc(u.weakness || "") : "—"], ["EVIDENCE", u ? esc((u.evidence || []).join(" · ")) : "—"],
    ["CODE CHANGE", u?.diff_lines ? `${u.diff_lines} LINES` : u?.diff ? `${String(u.diff).split("\n").length} LINES` : "—"], ["TESTS", tests],
    ["APPROVAL", u?.approval_id ? esc(state.approvals.get(u.approval_id)?.status?.toUpperCase() || u.approval_id) : "—"],
    ["VERSION", u ? `V${u.version_before ?? "?"} → ${u.version_after ? `V${u.version_after}` : "…"}` : "—"],
    ["PROOF", u?.prove ? (u.status === "proven" ? '<span class="g">ATTACK REPLAYED · NO INCIDENT</span>' : "WATCHING…") : "—"]];
  const html = `<div class="zc">
    <svg class="wires" viewBox="0 0 1600 900" style="position:absolute;left:0;top:0;width:1600px;height:900px"><path class="w ${at >= 0 ? "amb" : ""}" d="M${x0} 214 L${x0 + 7 * (wd + gap) - gap} 214"/></svg>
    ${stages}
    <div class="zcol" style="left:60px;top:330px;width:700px"><div class="bl-k">THE CODE FIX</div>${kv.map(([k, v]) => `<div class="kv"><span class="dim">${k}</span><span>${v}</span></div>`).join("")}</div>
    <div class="zcol" style="left:820px;top:330px;width:740px"><div class="bl-k">WHAT THE GARDENER IS DOING</div>${thoughts.length ? thoughts.map((t) => `<div class="kv"><span class="dim">${new Date(t.ts).toLocaleTimeString([], { hour12: false })}</span><span>${esc((translate(t)?.text || "").replace(/<[^>]+>/g, ""))}</span></div>`).join("") : '<div class="pd">Waiting: after the same failure is fixed on two nights, the Gardener writes a permanent code fix.</div>'}</div>
    <div class="eta" data-eta="gardener" style="left:60px;top:640px;width:1500px">${progressPanel("gardener")}</div>
  </div>`;
  const host = $("#gard-body");
  if (host.dataset.h !== html) { host.innerHTML = html; host.dataset.h = html; }
}

function renderDiagram() {
  const fit = $("#fit"), fr = $("#frame");
  const s = Math.min(fit.clientWidth / FW, fit.clientHeight / FH);
  fr.style.transform = `translate(${Math.max(0, (fit.clientWidth - FW * s) / 2)}px, ${Math.max(0, (fit.clientHeight - FH * s) / 2)}px) scale(${s})`;
  const rid = runId();
  $("#ex-title").textContent = TITLE[zoom];
  $("#z-back").hidden = zoom === "overview";
  const agents = [...state.agents.values()].filter((a) => !String(a._id).startsWith("red-"));
  $("#ex-a-l").textContent = zoom === "overview" ? `${agents.filter((a) => a.status !== "dead").length} AGENTS ALIVE` : "";
  const t0 = runStartedAt();
  const el = rid && t0 ? Math.max(0, Math.floor((serverNow() - t0) / 1000)) : null;
  $("#clock").textContent = rid ? `NIGHT ${state.current?.round ?? "?"} · T+${el != null ? `${String(Math.floor(el / 60)).padStart(2, "0")}:${String(el % 60).padStart(2, "0")}` : "—"}` : "NO NIGHT RUNNING";
  const tog = $("#vtog"); tog.hidden = zoom === "overview";
  for (const b of tog.querySelectorAll("button")) b.classList.toggle("on", b.dataset.v === mode);
  if (zooming) return;
  const how = zoom !== "overview" && mode === "how";
  for (const v of ZOOMS) { const el2 = $(`#v-${v}`), hide = how || v !== zoom; if (el2.hidden !== hide) el2.hidden = hide; }
  if ($("#v-how").hidden !== !how) $("#v-how").hidden = !how;
  if (how) return renderHow();
  ({ overview: renderOverview, chaos: renderChaos, legacy: renderLegacy, bees: renderBees, gardener: renderGardener })[zoom]();
}

function bindZoom() {
  // pointerdown, not click: a box can be re-rendered between press and release, which would swallow a click
  $("#ov-nodes").addEventListener("pointerdown", (e) => { const b = e.target.closest("[data-zoom]"); if (b && e.button === 0) setZoom(b.dataset.zoom); });
  $("#z-back").addEventListener("click", () => setZoom("overview"));
  for (const b of $("#vtog").querySelectorAll("button")) b.addEventListener("click", () => setMode(b.dataset.v));
  const help = () => { $("#help").hidden = !$("#help").hidden; };
  $("#btn-help").addEventListener("click", help);
  document.querySelectorAll("[data-help]").forEach((b) => b.addEventListener("click", help));
  $("#help-x").addEventListener("click", () => ($("#help").hidden = true));
  $("#help").addEventListener("click", (e) => { if (e.target.id === "help") $("#help").hidden = true; });
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey || /input|select|textarea/i.test(e.target.tagName)) return;
    if (e.key === "Escape") { if (!$("#help").hidden) $("#help").hidden = true; else if ($("#details").hidden) setZoom("overview"); }
    else if (e.key === "0") setZoom("overview");
    else if (KEYZ[e.key]) setZoom(KEYZ[e.key]);
    else if (e.key.toLowerCase() === "v") setMode(mode === "live" ? "how" : "live");
    else if (e.key === "?" || e.key.toLowerCase() === "h") help();
  });
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
on("snapshot", (snap) => { seedProgress(); localRoster.clear(); for (const r of snap?.roster || []) localRoster.set(r._id, r); });
on("change", (m, prev) => {
  if (m.coll === "thoughts" && m.op === "insert" && isProgress(m.doc)) noteProgress(m.doc.progress, ts(m.doc.ts));
  if (m.coll === "stats" && String(m.key || "").startsWith("progress:") && m.doc) noteProgress({ area: String(m.key).slice(9), ...m.doc }, ts(m.doc.ts) || serverNow());
  if (m.coll === "roster") { if (m.op === "delete") localRoster.delete(m.key); else if (m.doc) localRoster.set(m.key, m.doc); }
  try { watchForPulses(m, prev); } catch (e) { console.error(e); }
  if (m.coll === "incidents" && m.doc?.status === "resolved" && prev?.status !== "resolved") refreshScore();
  render();
});
on("status", (s) => { const c = $("#conn"); c.classList.toggle("live", s === "live"); c.classList.toggle("down", s !== "live"); $("#conn-txt").textContent = s === "live" ? "LIVE FROM MONGODB" : "RECONNECTING…"; render(); });
on("tick", render);

bind();
bindZoom();
connect();
window.__diagram = { state, render, translate, duels, setZoom, setMode, get mode() { return mode; }, get zoom() { return zoom; }, get zooming() { return zooming; } };

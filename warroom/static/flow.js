// Legacy Siege · flow view: what the AI agents do, drawn as ONE flowchart that lights up as each step really
// happens. Attack → cascade (portal → unix → mainframe) → the Scout finds the cause → AI suggests / plan →
// the checker (Guard bee) verifies → fixed → remembered in MongoDB → next night: recalled, faster.
// Branches appear when they happen: a killed bee and who took over; a renamed field (schema drift).
// Same data source as the classic view: the /ws change stream (store.js) + existing REST endpoints.
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

// ------------------------------------------------------------------ the diagram: fixed 1800-px canvas, scaled to fit
const W = 1800;
const ROW_A = 40, H_A = 360, ROW_B = 490, H_B = 200, ROW_C = 760, H_C = 180;
const L = {
  attack: { x: 10, y: ROW_A, w: 200, h: H_A }, cascade: { x: 244, y: ROW_A, w: 520, h: H_A },
  diagnose: { x: 798, y: ROW_A, w: 220, h: H_A }, ai: { x: 1052, y: ROW_A, w: 230, h: H_A },
  check: { x: 1316, y: ROW_A, w: 260, h: H_A }, fixed: { x: 1610, y: ROW_A, w: 180, h: H_A },
  killed: { x: 10, y: ROW_B, w: 220, h: H_B }, takeover: { x: 266, y: ROW_B, w: 250, h: H_B },
  upgrade: { x: 1030, y: ROW_B, w: 250, h: H_B }, round2: { x: 1316, y: ROW_B, w: 260, h: H_B },
  memory: { x: 1610, y: ROW_B, w: 180, h: H_B },
  d1: { x: 420, y: ROW_C, w: 250, h: H_C }, d2: { x: 708, y: ROW_C, w: 250, h: H_C },
  d3: { x: 996, y: ROW_C, w: 250, h: H_C }, d4: { x: 1284, y: ROW_C, w: 250, h: H_C },
};
const C = { red: "#ff5a5f", blue: "#5cc8ff", violet: "#c4a1ff", green: "#34e39a", honey: "#ffc94d", pink: "#ff9ccf", amber: "#ffb627", grey: "#8a96ad" };
const BEE = (c) => `<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M16 3l11 6.5v13L16 29 5 22.5v-13z" fill="${c}" opacity=".18" stroke="${c}" stroke-width="2.5"/><ellipse cx="16" cy="17" rx="5" ry="6.5" fill="${c}"/><path d="M11.5 15h9M11.5 19h9" stroke="#0f1625" stroke-width="1.6"/></svg>`;
const MONKEY = `<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M16 3l11 6.5v13L16 29 5 22.5v-13z" fill="none" stroke="${C.red}" stroke-width="2.5"/><path d="M11 11l10 10M21 11L11 21" stroke="${C.red}" stroke-width="3" stroke-linecap="round"/></svg>`;
const DB = `<svg viewBox="0 0 32 32" aria-hidden="true"><ellipse cx="16" cy="8" rx="10" ry="4" fill="none" stroke="${C.green}" stroke-width="2.5"/><path d="M6 8v16c0 2.2 4.5 4 10 4s10-1.8 10-4V8M6 16c0 2.2 4.5 4 10 4s10-1.8 10-4" fill="none" stroke="${C.green}" stroke-width="2.5"/></svg>`;
const AI = `<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M16 4l3 9 9 3-9 3-3 9-3-9-9-3 9-3z" fill="${C.violet}"/></svg>`;

const IC = (d, fill = false) => `<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="${d}" fill="${fill ? "currentColor" : "none"}" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const ICONS = {
  attack: IC("M13 2L4 14h7l-1 8 9-12h-7z"), cascade: IC("M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01"),
  diagnose: IC("M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15zM16 16l5 5"), ai: IC("M12 3l2.2 6.8L21 12l-6.8 2.2L12 21l-2.2-6.8L3 12l6.8-2.2z"),
  check: IC("M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6zM8.5 12l2.5 2.5 4.5-5"), fixed: IC("M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM8 12l3 3 5-6"),
  memory: IC("M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"),
  round2: IC("M3 5l8 7-8 7zM13 5l8 7-8 7z"), killed: IC("M6 6l12 12M18 6L6 18"), takeover: IC("M4 12h13M13 7l5 5-5 5"),
  upgrade: IC("M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"),
  d1: IC("M4 7h10M4 12h7M4 17h10M17 9l4 3-4 3"), d2: IC("M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15zM16 16l5 5"),
  d3: IC("M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6zM8.5 12l2.5 2.5 4.5-5"), d4: IC("M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"),
};
function latestBy(roles, since) {
  for (let i = state.thoughts.length - 1; i >= 0; i--) {
    const t = state.thoughts[i];
    if (roles.includes(roleOf(t.agent)) && ts(t.ts) >= since) return t;
  }
  return null;
}
const actLine = (roles, since) => { const t = latestBy(roles, since); return t ? translate(t)?.text || "" : ""; };
const driftMappings = new Map();
const SHORT_OPS = { quarantine_record: "Set aside the bad record", restart_batch: "Restart the batch", rerun_transfer: "Re-send the files",
  refresh_cache: "Refresh balances", map_field: "Use the new field name" };
const sop = (o) => SHORT_OPS[o] || Op(o);
const firstSentence = (t) => String(t).split(/(?<=[.;!?])\s/)[0];
const trimWords = (t, n) => (t.length <= n ? t : `${t.slice(0, n).replace(/\s+\S*$/, "")}…`);
// the second opinion's named guardrails (W2) in plain words
const RULE_WORDS = { P1: "not an allowed step", P2: "wrong order", P3: "too risky", P4: "not needed", P5: "wrong system", P6: "needs a human OK" };
const ruleWords = (v) => RULE_WORDS[String(v.rule || "").slice(0, 2)] || trimWords(firstSentence(v.reason || "not safe"), 60);

// Build the whole diagram model from MongoDB state. Every node: {id, state: idle|active|done|bad|dead|ghost, c, k, num, html}
function model() {
  const rid = runId(), since = (runStartedAt() || 0) - 5000;
  const inc = focusIncident(), live = inc?.active ? inc : null;
  const N = {}, E = [];
  const node = (id, o) => (N[id] = { id, state: "idle", c: C.green, ...o });
  // 1 · attack
  const attacks = attackLines().filter((l) => state.chaos.get(l.id.split(":")[0])?.run_id === rid).sort((a, b) => ts(a.ts) - ts(b.ts));
  const SHORT = { lying_success: (e) => `Rigged the ${sn(e.target)} to lie`, batch_abort: (e) => `Corrupted a record in the ${sn(e.target)}’s batch`,
    schema_drift: (e) => `Renamed a data field on ${sn(e.target)}` };
  const sysAttacks = [...state.chaos.values()].filter((e) => e.run_id === rid && e.fault !== "agent_kill").sort((a, b) => ts(a.ts) - ts(b.ts))
    .map((e) => ({ text: esc(SHORT[e.fault] ? SHORT[e.fault](e) : (ATTACK[e.fault] ? ATTACK[e.fault](e) : `${e.fault} on ${sn(e.target)}`)) }))
    .filter((l, i, arr) => arr.findIndex((x) => x.text === l.text) === i);
  node("attack", { num: 1, k: "Attack", c: C.red, state: attacks.length ? "done" : "idle",
    html: attacks.length ? `<div class="ticks">${(sysAttacks.length ? sysAttacks.slice(-3) : attacks.slice(-1)).map((l) => `<div class="tick"><span class="m no">!</span><span>${l.text}</span></div>`).join("")}</div>`
      : `<div class="b dim">Press Start: we attack the bank.</div>`,
    who: `${MONKEY}<span>Chaos Monkey (us)</span>` });
  // 2 · cascade (the three systems)
  const path = inc?.diagnosis_path || [];
  const lied = new Set(state.thoughts.filter((t) => t.kind === "reject" && inc && t.incident === inc._id).map((t) => t.system || "mainframe"));
  for (const s of (inc?.steps || [])) if ((s.attempts || []).length > 1 && s.status === "verified") lied.add(s.system);
  const sysBox = (s) => {
    const d = state.systems.get(s), st = d?.status || "?";
    const root = live && inc.root_cause_system === s;
    const cls = root ? "bad root" : st === "up" ? "ok" : st === "down" ? "bad" : "warn";
    const word = root && st === "up" ? "CAUSE" : st === "up" ? ((d?.version || 1) > 1 ? `OK · v${d.version}` : "OK") : st === "down" ? "DOWN" : "TROUBLE";
    const sym = String(d?.symptom || "");
    const cap = !live ? ((d?.version || 1) > 1 ? "Upgraded code" : "Working")
      : root && st === "up" ? "Says OK, but it’s the cause"
      : st === "up" ? "Working"
      : s === "portal" && /stale/i.test(sym) ? "Customers see old balances"
      : s === "portal" ? "Can’t read its data"
      : s === "unix" ? "File transfer failed"
      : s === "mainframe" ? "Nightly batch crashed" : "Not healthy";
    const tag = root ? '<span class="tagx">ROOT CAUSE</span>' : live && path.includes(s) ? '<span class="tagx ck">CHECKED</span>' : "";
    return `<div class="sys ${cls}"><div class="sn">${esc(sn(s))}</div><div class="ss">${word}</div><div class="sc">${esc(cap)}</div>${tag}${lied.has(s) ? '<span class="lied">LIED!</span>' : ""}</div>`;
  };
  const sarrow = (a, b) => { const on = live && (state.systems.get(a)?.blames === b || (path.includes(a) && path.includes(b))); return `<div class="sarrow ${on ? "on" : ""}"><svg viewBox="0 0 44 20"><path d="M2 10h36M31 3l8 7-8 7" fill="none" stroke="currentColor" stroke-width="3"/></svg>${on ? "blames" : ""}</div>`; };
  const quietNight = rid && !inc && attacks.length;
  node("cascade", { num: 2, k: "Cascade: who is really broken?", c: C.blue,
    state: !inc ? (quietNight ? "done" : "idle") : live && !inc.root_cause_system ? "active" : "done",
    flag: quietNight && latestUpgrade()?.status === "proven" ? '<div class="flag green">ATTACKED · NOTHING BROKE</div>' : "",
    html: `<div class="syss">${sysBox("portal")}${sarrow("portal", "unix")}${sysBox("unix")}${sarrow("unix", "mainframe")}${sysBox("mainframe")}</div>
      <div class="b dim" style="margin-top:22px">${inc?.root_cause_system ? `${esc(sn(inc.system))} looked broken. The real cause: <b style="color:var(--red)">${esc(sn(inc.root_cause_system))}</b>.` : live ? "Each one blames the system it depends on…" : "Website ← Unix ← Mainframe: each depends on the next."}</div>` });
  // 3 · diagnose (Scout)
  const recalled = inc?.recall?.used;
  const nChecks = inc?.metrics?.hops ?? path.length;
  node("diagnose", { num: 3, k: "Find the cause", c: C.blue,
    state: !inc ? "idle" : inc.root_cause_system ? "done" : (path.length || inc.recall) ? "active" : live ? "active" : "idle",
    flag: recalled ? '<div class="flag gold">REMEMBERED!</div>' : "",
    html: !inc ? `<div class="b dim">The Scout follows the trail from the website inward.</div>`
      : recalled ? `<div class="h">Seen it before</div><div class="b">Recalled from MongoDB → straight to the ${esc(sn(inc.root_cause_system || "mainframe"))}.</div>`
      : inc.root_cause_system ? `<div class="h">Found it</div><div class="b">${nChecks} check${nChecks === 1 ? "" : "s"}: ${path.map((s) => esc(sn(s))).join(" → ")}</div>`
      : `<div class="h">Tracing…</div><div class="b">${path.map((s) => esc(sn(s))).join(" → ") || "Starting at the website"}${path.length ? " → …" : ""}</div>`,
    who: `${BEE(C.blue)}<span>Scout bee</span>`, act: live && !inc.root_cause_system ? actLine(["scout", "worker"], since) : null });
  // 4 · AI suggests (or the plan when there is no AI on this incident)
  const ai = inc?.ai, steps = inc?.steps || [];
  if (ai) {
    const status = !ai.status ? "done" : ai.status;
    const ops = (ai.proposed_ops || []).map((o) => esc(sop(o.op))).slice(0, 3).join(" · ");
    node("ai", { num: 4, k: ai.kind === "schema_mapping" ? "AI suggests a mapping" : "AI suggests", c: C.violet,
      state: status === "thinking" ? "active" : status === "done" ? "done" : "ghost",
      html: status === "thinking" ? `<div class="h">Thinking…</div><div class="b dim">Claude reads the evidence the bees gathered.</div>`
        : status === "done" ? `<div class="b" style="font-size:18px">“${esc(trimWords(firstSentence(ai.hypothesis || ""), 95))}”</div>${ops ? `<div class="b dim" style="font-size:16px">Try: ${ops}</div>` : ""}`
        : `<div class="h">No answer</div><div class="b dim">The checker decides anyway.</div>`,
      who: `${AI}<span>Claude (AI second opinion)</span>` });
  } else {
    node("ai", { num: 4, k: "Plan the fix", c: C.violet, state: steps.length ? "done" : "idle",
      html: steps.length ? `<div class="h">${steps.length} steps</div><div class="b" style="font-size:18px">${steps.slice(0, 5).map((s) => esc(sop(s.op))).join(" → ")}</div>`
        : `<div class="b dim">The Scout plans the steps. Claude’s second opinion shows here when it is on.</div>`,
      who: `${BEE(C.blue)}<span>Scout bee</span>` });
  }
  // 5 · checker verifies (Guard)
  const vs = ai?.verdicts || [];
  const blocked = vs.filter((v) => v.verdict === "overruled");
  const lieNow = steps.some((s) => (s.attempts || []).length > 1) || lied.size > 0;
  const allOk = steps.length && steps.every((s) => s.status === "verified");
  const tick = (s) => {
    const n = (s.attempts || []).length;
    if (s.status === "verified") return `<div class="tick"><span class="m ok">✓</span><span>${esc(sop(s.op))}${n > 1 ? ' <b style="color:var(--red)">· lied first</b>' : ""}</span></div>`;
    if (s.status === "failed") return `<div class="tick"><span class="m no">${XMARK}</span><span>${esc(sop(s.op))} <b style="color:var(--red)">· it lied</b></span></div>`;
    if (["sent", "unknown"].includes(s.status)) return `<div class="tick"><span class="m wait">…</span><span>${esc(sop(s.op))}</span></div>`;
    if (s.approval_id && !["verified"].includes(s.status) && pendingApprovals().some((a) => a._id === s.approval_id)) return `<div class="tick"><span class="m wait">?</span><span>${esc(sop(s.op))} <span class="sub">needs your OK</span></span></div>`;
    return `<div class="tick"><span class="m wait">–</span><span style="color:var(--dim)">${esc(sop(s.op))}</span></div>`;
  };
  node("check", { num: 5, k: "Checker verifies", c: C.green,
    state: !steps.length ? "idle" : allOk ? "done" : lieNow && live ? "bad" : "active",
    flag: lieNow ? '<div class="flag">CAUGHT A LIE</div>' : blocked.length ? `<div class="flag">BLOCKED ${blocked.length} AI STEP${blocked.length === 1 ? "" : "S"}</div>` : "",
    html: steps.length ? `<div class="ticks">${steps.slice(0, 4).map(tick).join("")}</div>${blocked.length ? `<div class="b" style="font-size:16px;color:var(--red)">Blocked the AI’s “${esc(sop(blocked[0].op).toLowerCase())}”: ${esc(ruleWords(blocked[0]))}</div>` : ""}`
      : `<div class="b dim">Reads every system back. A “done” only counts if it’s true.</div>`,
    who: `${BEE(C.green)}<span>Guard bee · the checker</span>` });
  // 6 · fixed
  const done = steps.filter((s) => s.status === "verified").length;
  node("fixed", { num: 6, k: "Fixed", c: C.green,
    state: inc?.status === "resolved" ? "done" : live && steps.length ? "active" : "idle",
    html: inc?.status === "resolved" ? `<div class="h">All green</div><div class="b">in ${secs(inc.metrics?.ttr_s)}</div>`
      : live && steps.length ? `<div class="h">${done} of ${steps.length}</div><div class="b dim">fixes confirmed</div>` : `<div class="b dim">All green again.</div>` });
  // 7 · remembered in MongoDB
  const saved = inc?.status === "resolved";
  node("memory", { num: 7, k: "Memory", c: C.honey, state: saved ? "done" : "idle",
    html: `<div class="h" style="font-size:24px">${saved ? "Saved in MongoDB" : "Remember"}</div><div class="b dim" style="font-size:17px">${saved ? "Any bee can recall it." : "Each fix is saved."}</div>`,
    who: `${DB}<span>MongoDB</span>` });
  // 8 · next night: recalled, faster
  const r = rounds(), liveR2 = [...state.incidents.values()].find((i) => i.active && i.round === 2);
  const t1 = r[1]?.ttr, t2 = r[2]?.ttr ?? (liveR2 ? (serverNow() - ts(liveR2.opened_at)) / 1000 : null);
  node("round2", { num: 8, k: "Next night: faster", c: C.honey,
    state: r[1] && r[2] && r[2].ttr < r[1].ttr ? "done" : liveR2 ? "active" : "idle",
    html: `<div class="times"><span class="t1">${t1 != null ? secs(t1) : "—"}</span><span class="ar">→</span><span class="t2">${t2 != null ? secs(t2) : "?"}</span>${r[1] && r[2] && r[2].ttr < r[1].ttr ? `<span class="x">${(r[1].ttr / r[2].ttr).toFixed(1)}×</span>` : ""}</div>
      <div class="b dim" style="font-size:17px">${r[1] && r[2] ? `Night 1 → Night 2 · ${Math.round(r[2].hops)} check, not ${Math.round(r[1].hops)}` : r[1] ? "Night 2: same attack. Faster?" : "Time to fix: Night 1 → Night 2"}</div>` });
  // permanent fix (Gardener), only once it starts
  const u = latestUpgrade();
  if (u && u.status !== "rejected") {
    const H = { requested: "Writing a code fix", planning: "Writing a code fix", testing: "Testing the fix", awaiting_approval: "Waiting for your OK", deploying: "Installing it", deployed: "Proving it", proven: "Fixed for good" };
    node("upgrade", { k: "Permanent fix", c: C.green, state: u.status === "proven" ? "done" : "active",
      html: `<div class="h" style="font-size:24px">${H[u.status] || "Code fix"}</div><div class="b dim" style="font-size:17px">${u.status === "proven" ? `${esc(sn(u.system))} v${u.version_before ?? 1} → v${u.version_after ?? 2}: the attack came back, nothing broke.` : "Same failure twice → new code."}</div>`,
      who: `${BEE("#a6f0a0")}<span>Gardener bee</span>` });
  }
  // kill branch: a killed bee, and who took over
  const kills = [...state.chaos.values()].filter((e) => e.fault === "agent_kill" && e.run_id === rid).sort((a, b) => ts(a.ts) - ts(b.ts));
  const k = kills[kills.length - 1];
  if (k) {
    const victim = k.result?.agent, vrole = victim ? roleOf(victim) : null;
    const qho = victim && vrole === "queen" ? [...state.thoughts].reverse().find((t) => /takes the queen lease/.test(t.text || "") && ts(t.ts) >= ts(k.ts)) : null;
    const ho = victim && vrole !== "queen" ? nightIncidents().flatMap((i) => i.handoffs || []).find((h) => h.from === victim) : null;
    const hunting = !victim && !["failed", "error"].includes(k.status);
    node("killed", { k: "Bee killed", c: C.red, state: victim ? "dead" : hunting ? "active" : "ghost",
      flag: victim ? '<div class="flag">KILLED</div>' : "",
      html: victim ? `<div class="h" style="font-size:24px">${esc(vrole === "queen" ? "The Queen" : agentName(victim))}</div><div class="b dim" style="font-size:17px">Real process killed mid-job.</div>`
        : hunting ? `<div class="h" style="font-size:24px">Hunting…</div><div class="b dim" style="font-size:17px">Waiting for a bee in the middle of a job.</div>`
        : `<div class="h" style="font-size:24px">Missed</div><div class="b dim" style="font-size:17px">No busy bee to kill.</div>` });
    node("takeover", { k: "Takes over", c: C.pink, state: ho || qho ? "done" : victim ? "active" : "ghost",
      html: qho ? `<div class="h" style="font-size:24px">Backup queen</div><div class="b" style="font-size:17px">took over and carries on.</div>`
        : ho ? `<div class="h" style="font-size:24px">${esc(agentName(ho.to))}</div><div class="b" style="font-size:17px">took over the job from its saved progress${ho.takeover_s != null ? ` (${Math.round(ho.takeover_s)} s)` : ""}. No step repeated.</div>`
        : victim ? `<div class="h" style="font-size:24px">Coming…</div><div class="b dim" style="font-size:17px">${vrole === "queen" ? "The backup queen notices the silence." : "A nurse picks up the job when its lease runs out."}</div>` : `<div class="b dim">—</div>`,
      who: `${BEE(C.pink)}<span>${vrole === "queen" ? "Backup queen" : "Nurse bee"}</span>`, toCheck: vrole !== "queen" });
  }
  // schema drift branch: only when it fires
  const incs = [...state.incidents.values()];
  const driftInc = nightIncidents().filter((i) => i.fault_class === "schema_drift" || i.ai?.kind === "schema_mapping").pop();
  const driftFired = [...state.chaos.values()].some((e) => e.fault === "schema_drift" && e.run_id === rid) || driftInc;
  if (driftFired) {
    const mapStep = (driftInc?.steps || []).find((s) => s.op === "map_field");
    const learned = [...state.thoughts].reverse().find((t) => /mapping learned/.test(t.text || "") && (t.incident === driftInc?._id || ts(t.ts) >= since));
    const mm = (learned?.text || driftInc?.root_cause || "").match(/(\w+) → (\w+)/) || [];
    const from = mm[1] || [...driftMappings.values()].pop()?.previous, to = mm[2] || mapStep?.params?.source || [...driftMappings.values()].pop()?.source;
    node("d1", { k: "Field renamed", c: C.violet, state: "done", html: `<div class="b">A vendor patch renamed a data field on Unix. It still says it’s fine.</div>` });
    node("d2", { k: "Work out the new name", c: C.violet, state: to ? "done" : "active",
      html: to ? `<div class="h" style="font-size:24px">“${esc(from || "?")}” → “${esc(to)}”</div><div class="b dim" style="font-size:17px">${driftInc?.ai ? "Claude suggested it" : "Compared old and new data"}</div>` : `<div class="b dim">Comparing old and new data…</div>` });
    node("d3", { k: "Checker proves it", c: C.green, state: mapStep?.status === "verified" ? "done" : mapStep ? "active" : "idle",
      html: mapStep?.status === "verified" ? `<div class="b">Values line up with yesterday’s data ✓</div>` : `<div class="b dim">Same numbers under the new name?</div>` });
    node("d4", { k: "Mapping remembered", c: C.honey, state: learned || driftMappings.size ? "done" : "idle",
      html: `<div class="b ${learned ? "" : "dim"}">${learned ? "Saved in MongoDB: every bee reads the new name now." : "Saved for every bee."}</div>`, who: `${DB}<span>MongoDB</span>` });
  }
  void incs;
  return N;
}

// wires: [from, to, path builder, label]
function wiresFor(N) {
  const c = (b) => ({ cx: b.x + b.w / 2, cy: b.y + b.h / 2, r: b.x + b.w, b: b.y + b.h });
  const out = [];
  const add = (from, to, d, label, lx, ly, cls = "") => { if (N[from] && N[to]) out.push({ from, to, d, label, lx, ly, cls }); };
  const midA = ROW_A + H_A / 2;
  const seq = ["attack", "cascade", "diagnose", "ai", "check", "fixed"];
  for (let i = 1; i < seq.length; i++) {
    const a = L[seq[i - 1]], b = L[seq[i]];
    add(seq[i - 1], seq[i], `M${a.x + a.w + 2} ${midA} L${b.x - 6} ${midA}`, "", 0, 0);
  }
  const fx = c(L.fixed), mem = L.memory, r2 = L.round2, up = L.upgrade, dg = c(L.diagnose);
  add("fixed", "memory", `M${fx.cx} ${fx.b + 2} L${fx.cx} ${mem.y - 6}`, "saved", fx.cx + 10, (fx.b + mem.y) / 2 + 6);
  add("memory", "round2", `M${mem.x - 2} ${ROW_B + H_B / 2} L${r2.x + r2.w + 6} ${ROW_B + H_B / 2}`, "", 0, 0);
  add("memory", "diagnose", `M${mem.x + 30} ${mem.y - 2} L${mem.x + 30} ${ROW_A + H_A + 50} L${dg.cx} ${ROW_A + H_A + 50} L${dg.cx} ${ROW_A + H_A + 6}`, "next night: recalled", dg.cx + 14, ROW_A + H_A + 44, "dash recall");
  add("round2", "upgrade", `M${r2.x - 2} ${ROW_B + H_B / 2} L${up.x + up.w + 6} ${ROW_B + H_B / 2}`, "", 0, 0);
  const at = c(L.attack), kb = L.killed, tk = L.takeover, ck = c(L.check);
  add("attack", "killed", `M${at.cx} ${at.b + 2} L${at.cx} ${kb.y - 6}`, "kills", at.cx + 10, (at.b + kb.y) / 2 + 6);
  add("killed", "takeover", `M${kb.x + kb.w + 2} ${ROW_B + H_B / 2} L${tk.x - 6} ${ROW_B + H_B / 2}`, "", 0, 0);
  if (N.takeover?.toCheck) add("takeover", "check", `M${tk.x + tk.w / 2} ${tk.y - 2} L${tk.x + tk.w / 2} ${ROW_A + H_A + 28} L${ck.cx} ${ROW_A + H_A + 28} L${ck.cx} ${ROW_A + H_A + 6}`, "resumes the job", tk.x + tk.w / 2 + 12, ROW_A + H_A + 22, "dash");
  const cs = L.cascade;
  add("cascade", "d1", `M540 ${cs.y + cs.h + 2} L540 ${ROW_C - 6}`, "renamed field", 552, ROW_C - 40);
  for (const [a, b] of [["d1", "d2"], ["d2", "d3"], ["d3", "d4"]]) add(a, b, `M${L[a].x + L[a].w + 2} ${ROW_C + H_C / 2} L${L[b].x - 6} ${ROW_C + H_C / 2}`, "", 0, 0);
  return out;
}

const seen = new Set();
const flagsShown = new Set();
function renderFlow() {
  const N = model();
  const H = N.d1 ? ROW_C + H_C + 20 : ROW_B + H_B + 20;
  const stage = $("#stage"), wrap = $("#stage-wrap");
  const s = Math.min(wrap.clientWidth / W, wrap.clientHeight / H);
  stage.style.height = `${H}px`;
  stage.style.transform = `translate(${Math.max(0, (wrap.clientWidth - W * s) / 2)}px, ${Math.max(0, (wrap.clientHeight - H * s) / 2)}px) scale(${s})`;
  const wires = wiresFor(N);
  const stOf = (id) => N[id]?.state;
  const svg = `<defs>${["#2a3752", ...Object.values(C)].map((col, i) => `<marker id="m${i}" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="${col}"/></marker>`).join("")}</defs>`
    + wires.map((w) => {
      const st = stOf(w.to), on = ["active", "bad"].includes(st), dn = ["done", "dead"].includes(st) && stOf(w.from) !== "idle";
      const col = w.cls.includes("recall") ? C.honey : N[w.to].c;
      const mi = (on || dn) ? 1 + Object.values(C).indexOf(col) : 0;
      return `<path class="wire ${w.cls} ${on ? "on" : dn ? "done" : ""}" style="--c:${col}" d="${w.d}" marker-end="url(#m${mi})"/>`
        + (w.label ? `<text class="wlabel ${on || dn ? "on" : ""}" style="--c:${col}" x="${w.lx}" y="${w.ly}">${esc(w.label)}</text>` : "");
    }).join("");
  const wsvg = $("#wires");
  wsvg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (wsvg.dataset.html !== svg) { wsvg.innerHTML = svg; wsvg.dataset.html = svg; } // re-render only on change: keeps the dash flow smooth
  const host = $("#nodes");
  for (const el of [...host.children]) if (!N[el.dataset.id]) el.remove();
  for (const n of Object.values(N)) {
    const b = L[n.id];
    let el = host.querySelector(`[data-id="${n.id}"]`);
    if (!el) {
      el = document.createElement("div"); el.dataset.id = n.id; host.appendChild(el);
      if (seen.size && !["attack", "cascade", "diagnose", "ai", "check", "fixed", "memory", "round2"].includes(n.id)) { el.dataset.pop = "1"; setTimeout(() => { delete el.dataset.pop; render(); }, 700); }
    }
    const cls = `node ${n.state}${el.dataset.pop ? " appear" : ""}`;
    if (el.className !== cls) el.className = cls;
    el.style.cssText = `left:${b.x}px;top:${b.y}px;width:${b.w}px;height:${b.h}px;--c:${n.c}`;
    // a flag stamps in once (the first time it appears), not again on every re-render
    const flagKey = `${n.id}|${n.flag || ""}`;
    const flag = n.flag && flagsShown.has(flagKey) ? n.flag.replace('class="flag', 'class="flag still') : (n.flag || "");
    const body = `<div class="k">${n.num ? `<span class="num">${n.num}</span>` : ""}${ICONS[n.id] || ""}<span>${esc(n.k)}</span></div>
      ${n.html}${n.act != null ? `<div class="act">${n.act}</div>` : ""}${n.who && !["idle", "ghost"].includes(n.state) ? `<div class="who">${n.who}</div>` : ""}`;
    const key = `${n.flag || ""}${body}`;
    if (el.dataset.key !== key) {
      const lieOld = new Set([...el.querySelectorAll(".lied")].map((x) => x.parentElement?.querySelector(".sn")?.textContent));
      el.innerHTML = flag + body;
      for (const x of el.querySelectorAll(".lied")) if (lieOld.has(x.parentElement?.querySelector(".sn")?.textContent)) x.classList.add("still");
      el.dataset.key = key;
    }
    if (n.flag) flagsShown.add(flagKey);
  }
  for (const id of Object.keys(N)) seen.add(id);
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
    for (const [name, fn] of [["top", renderTop], ["flow", renderFlow], ["now", () => { $("#now").innerHTML = nowLine(); }], ["approval", renderApproval]]) {
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
on("status", (s) => { const c = $("#conn"); c.classList.toggle("live", s === "live"); c.classList.toggle("down", s !== "live"); $("#conn-txt").textContent = s === "live" ? "LIVE from MongoDB" : "reconnecting…"; render(); });
on("tick", render);

bind();
connect();
window.__stage = { state, render, translate, model };

// DOM side of the war room: top bar, side-panel cards, approval modal, inspector drawer, Atlas view.
import { state, api, currentIncident, currentRun, pendingApprovals, latestUpgrade, serverNow, ts, prettyTime } from "./store.js";
import { ROLE } from "./draw.js";
import { shortAgent } from "./stage.js";

const $ = (s) => document.querySelector(s);
const esc = (s) => prettyTime(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtS = (s) => (s == null || isNaN(s) ? "—" : s >= 100 ? `${Math.round(s)}s` : `${(+s).toFixed(1)}s`);

// tiny inline SVG icons (no icon font / emoji dependency)
const P = {
  thought: '<circle cx="8" cy="8" r="3"/>',
  action: '<path d="M4 3l9 5-9 5z"/>',
  verify: '<path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" stroke-width="2.2"/>',
  reject: '<path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="2.2"/>',
  recall: '<path d="M8 1.5l1.9 4.2 4.6.5-3.4 3.1 1 4.5L8 11.5 3.9 13.8l1-4.5L1.5 6.2l4.6-.5z"/>',
  handoff: '<path d="M2 5h9l-2.5-2.5M14 11H5l2.5 2.5" fill="none" stroke="currentColor" stroke-width="1.8"/>',
  learn: '<path d="M8 1.5l6 6.5-6 6.5-6-6.5z"/>',
  approval: '<path d="M3 14V2h8l-1.5 3L11 8H4.5" fill="none" stroke="currentColor" stroke-width="1.8"/>',
  upgrade: '<path d="M8 2l6 7h-4v5H6V9H2z"/>',
};
const XMARK = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" fill="none"/></svg>';
const icon = (k) => `<svg viewBox="0 0 16 16" width="13" height="13" fill="currentColor" aria-hidden="true">${P[k] || P.thought}</svg>`;

const STATUS_PILL = {
  open: "amber", claimed: "amber", diagnosing: "blue", fixing: "blue", verifying: "blue",
  awaiting_approval: "gold", resolved: "green", failed: "red",
};

export class Panels {
  constructor({ onInspect }) {
    this.onInspect = onInspect;
    this.stamps = new Map(); // step_id -> {t, verifiedAt}
    this.stepEls = new Map();
    this.renderedIncident = null;
    this.modalId = null;
    this.bindTopbar();
    this.bindOverlays();
    setInterval(() => this.tickTimers(), 250);
  }

  // ---------------------------------------------------------------- top bar
  bindTopbar() {
    document.querySelectorAll("[data-scn]").forEach((b) => b.addEventListener("click", () => this.act(b, () => api("POST", `/api/scenario/${b.dataset.scn}`))));
    $("#btn-kill").addEventListener("click", (e) => this.act(e.currentTarget, () => api("POST", "/api/kill", { selector: { role: "worker", phase: "step_sent" } })));
    // the queen wearing the crown right now (queen-1 at boot; queen-2 after a failover), so a second press is a real test too
    const crowned = () => [...state.agents.values()].find((a) => a.role === "queen" && !["dead", "standby"].includes(a.status))?._id || "queen-1";
    $("#btn-kill-queen").addEventListener("click", (e) => this.act(e.currentTarget, () => api("POST", "/api/kill", { selector: { agent: crowned() } })));
    const faults = {
      batch_abort: ["mainframe"], lying_success: ["mainframe", "unix", "portal"], slow_apply: ["mainframe", "unix", "portal"],
      disk_fill: ["unix"], cron_stop: ["unix"], session_leak: ["portal"], sqli_exploit: ["portal"], config_exploit: ["unix"],
      schema_drift: ["unix"],
    };
    const fs = $("#fault-sel"), tsel = $("#target-sel");
    fs.innerHTML = Object.keys(faults).map((f) => `<option>${f}</option>`).join("");
    const syncTargets = () => { tsel.innerHTML = faults[fs.value].map((t) => `<option>${t}</option>`).join(""); };
    fs.addEventListener("change", syncTargets); syncTargets();
    $("#btn-fire").addEventListener("click", (e) => this.act(e.currentTarget, () => api("POST", `/api/chaos/${fs.value}`, { target: tsel.value, params: fs.value === "lying_success" ? { count: 1 } : {} })));
    const menu = $("#btn-reset").parentElement;
    $("#btn-reset").addEventListener("click", (e) => { e.stopPropagation(); menu.classList.toggle("open"); });
    document.addEventListener("click", () => menu.classList.remove("open"));
    document.querySelectorAll("[data-reset]").forEach((b) => b.addEventListener("click", () => {
      const mode = b.dataset.reset;
      if (mode === "full" && !confirm("Full reset: wipe all runtime data including the Hive's playbooks?")) return;
      this.act($("#btn-reset"), () => api("POST", "/api/reset", { mode }));
    }));
    $("#btn-atlas").addEventListener("click", () => this.openAtlas());
  }

  async act(btn, fn) {
    btn.disabled = true;
    try { await fn(); } catch (e) { this.toast(String(e.message || e)); } finally { setTimeout(() => (btn.disabled = false), 600); }
  }
  toast(msg) { window.dispatchEvent(new CustomEvent("caption", { detail: { text: "Control failed", small: msg, color: "#ff4757" } })); }

  renderTopbar() {
    const run = currentRun(), cur = state.current;
    $("#run-id").textContent = run?._id || cur?.run_id || "—";
    $("#run-round").textContent = cur?.round ?? run?.round ?? "—";
    $("#run-scn").textContent = run?.scenario || cur?.scenario || "";
    const armed = [...state.agents.values()].some((a) => a.role !== "nurse" && a.status !== "dead" && a.phase === "step_sent");
    $("#btn-kill").classList.toggle("armed", armed);
  }
  setConn(status) {
    const el = $("#conn");
    el.classList.toggle("live", status === "live"); el.classList.toggle("down", status !== "live");
    this.connStatus = status; this.renderConn();
  }
  renderConn() {
    $("#conn-text").textContent = this.connStatus === "live" ? `LIVE · change stream · ${(state.opsPerSec || 0).toFixed(1)} ops/s` : "reconnecting…";
  }

  // ---------------------------------------------------------------- incident card
  renderIncident() {
    const inc = currentIncident();
    const body = $("#inc-body");
    if (!inc) {
      $("#inc-id").textContent = "no incident"; $("#inc-status").textContent = "idle"; $("#inc-status").className = "pill green";
      body.innerHTML = '<div class="empty">All systems green. The Hive is waiting in the honeycomb.</div>';
      this.stepEls.clear(); this.renderedIncident = null; this.renderRecall(null); this.renderAI(null); return;
    }
    const cur = state.current;
    if (!inc.active && cur?.run_id && inc.run_id && inc.run_id !== cur.run_id) {
      // a new night is running and has not opened an incident (night 3 after the upgrade: nothing to fix)
      const attacks = [...state.chaos.values()].filter((c) => c.run_id === cur.run_id);
      $("#inc-id").textContent = "no incident"; $("#inc-status").textContent = "quiet"; $("#inc-status").className = "pill green";
      body.innerHTML = `<div class="quiet"><div class="q-big">${attacks.length ? "Attacked, still green" : "All systems green"}</div>
        <div class="q-sub">${esc(cur.run_id)} · night ${esc(cur.round ?? "?")} · ${attacks.length} attack${attacks.length === 1 ? "" : "s"} (${esc([...new Set(attacks.map((c) => c.fault))].join(", ") || "none yet")}) · 0 incidents</div>
        <div class="q-last">last incident ${esc(inc._id)} · ${esc(inc.status)}${inc.metrics?.ttr_s != null ? ` · TTR ${fmtS(inc.metrics.ttr_s)}` : ""}</div></div>`;
      this.quiet = true; this.stepEls.clear(); this.renderedIncident = null; this.renderRecall(null); this.renderAI(null); return;
    }
    this.quiet = false;
    $("#inc-id").textContent = inc._id;
    $("#inc-id").onclick = () => this.onInspect({ db: "swarm", coll: "incidents", id: inc._id });
    $("#inc-id").style.cursor = "pointer";
    const st = $("#inc-status"); st.textContent = String(inc.status || "").replace(/_/g, " "); st.className = `pill ${STATUS_PILL[inc.status] || ""}`;
    if (this.renderedIncident !== inc._id) {
      body.innerHTML = '<div class="inc-head"></div><ol class="steps"></ol>';
      this.stepEls.clear(); this.renderedIncident = inc._id;
    }
    const path = inc.diagnosis_path || [];
    const usedRecall = inc.recall?.used;
    const chain = path.map((s) => `<span class="chip ${s === inc.root_cause_system ? "root" : "walk"}">${esc(s)}</span>`).join('<span class="arrow">←</span>');
    const lastH = (inc.handoffs || [])[inc.handoffs?.length - 1];
    body.querySelector(".inc-head").innerHTML = `
      <div class="inc-line"><span>symptom</span><span class="chip">${esc(inc.system || "?")}</span>
        ${inc.root_cause_system ? `<span class="arrow">→ root</span><span class="chip root">${esc(inc.root_cause_system)}</span>` : ""}
        ${inc.fault_class ? `<b>${esc(inc.fault_class)}</b>` : ""}
        ${inc.round != null ? `<span class="chip">night ${esc(inc.round)}</span>` : ""}</div>
      <div class="inc-line"><span>walk</span>${chain || '<span class="dim mono">not started</span>'}
        ${usedRecall ? '<span class="chip skip">chain walk</span><span class="chip" style="color:#ffd23f;border-color:#6b5713">skipped by recall</span>' : ""}</div>
      ${inc.root_cause ? `<div class="inc-line"><span>cause</span><b>${esc(inc.root_cause)}</b></div>` : ""}
      <div class="inc-line mono" style="font-size:11px">owner <b>${esc(inc.owner || "—")}</b> · gen ${esc(inc.generation ?? 0)}${inc.affected?.length ? ` · affected ${esc(inc.affected.join(", "))}` : ""}</div>
      ${lastH ? `<div class="handoff">handoff ${esc(lastH.from)} → ${esc(lastH.to)} · resumed from checkpoint <b>${esc(String(lastH.checkpoint_id || "?").slice(0, 18))}</b>${lastH.reason ? ` · ${esc(lastH.reason)}` : ""}</div>` : ""}`;
    const ol = body.querySelector(".steps");
    const steps = inc.steps || [];
    const keep = new Set();
    steps.forEach((s, idx) => {
      const id = s.step_id || `${inc._id}-s${idx + 1}`; keep.add(id);
      let li = this.stepEls.get(id);
      if (!li) {
        li = document.createElement("li"); li.className = "step"; li.dataset.step = id;
        li.innerHTML = '<div class="c" style="display:contents"></div>';
        li.addEventListener("click", () => {
          const s2 = (currentIncident()?.steps || []).find((x) => x.step_id === id) || s;
          this.onInspect(s2.action_id ? { db: "swarm", coll: "ledger", id: s2.action_id } : { db: "swarm", coll: "incidents", id: inc._id });
        });
        this.stepEls.set(id, li);
      }
      if (ol.children[idx] !== li) ol.insertBefore(li, ol.children[idx] || null);
      const prevStatus = li.dataset.status;
      li.dataset.status = s.status;
      li.className = `step ${s.status || ""}`;
      if (prevStatus && prevStatus !== "verified" && s.status === "verified") { void li.offsetWidth; li.classList.add("flash"); this.unstamp(id); }
      const sentOn = ["sent", "verified", "failed", "unknown"].includes(s.status);
      const attempts = (s.attempts || []).length;
      const stamped = this.stamps.has(id) || attempts > 1;
      li.querySelector(".c").innerHTML = `
        <span class="n">${s.n ?? idx + 1}</span>
        <span class="op">${esc(s.op)} <i>@ ${esc(s.system)}</i>${s.source === "ai" ? `<span class="ai-tag" title="${esc(s.ai_why || "planned by Claude, validated by the guardrail")}">AI</span>` : ""}${stamped && s.status === "verified" ? '<span class="lie-tag">lie caught</span>' : ""}</span>
        <span class="track" title="planned → sent → verified">
          <span class="dot on"></span><span class="bar"></span>
          <span class="dot ${s.status === "failed" ? "bad" : sentOn ? "on" : ""}"></span><span class="bar"></span>
          <span class="dot ${s.status === "verified" ? "ok" : s.status === "failed" ? "bad" : ""}"></span></span>
        ${s.status === "verified" && s.evidence ? "" : `<span class="meta"><span>${esc(s.status)}</span>${attempts ? `<span>${attempts} attempt${attempts > 1 ? "s" : ""}</span>` : ""}
          ${s.agent ? `<span>${esc(shortAgent(s.agent))}</span>` : ""}${s.destructive ? '<span class="destr">destructive · approval first</span>' : ""}</span>`}
        ${s.status === "verified" && s.evidence ? `<span class="ev" title="${esc(s.evidence)}">${attempts > 1 ? `${attempts} attempts · ` : ""}${esc(s.evidence)}</span>` : ""}`;
    });
    for (const [id, li] of this.stepEls) if (!keep.has(id)) { li.remove(); this.stepEls.delete(id); }
    this.renderRecall(inc);
    this.renderAI(inc);
  }

  stamp(stepId) {
    const li = this.stepEls.get(stepId); if (!li) return false;
    li.querySelector(".stamp")?.remove();
    const el = document.createElement("div"); el.className = "stamp"; el.innerHTML = "ACK ≠ APPLIED";
    li.appendChild(el); li.classList.add("shake"); setTimeout(() => li.classList.remove("shake"), 400);
    this.stamps.set(stepId, { t: Date.now() });
    setTimeout(() => this.unstamp(stepId, true), 6000);
    return true;
  }
  unstamp(stepId, timeout = false) {
    const li = this.stepEls.get(stepId); const el = li?.querySelector(".stamp");
    if (!el || el.classList.contains("fade")) return;
    if (timeout && li.dataset.status !== "verified") { /* keep it until verified, but not forever */ if (Date.now() - (this.stamps.get(stepId)?.t || 0) < 14000) { setTimeout(() => this.unstamp(stepId, true), 2000); return; } }
    el.classList.add("fade"); setTimeout(() => el.remove(), 800);
  }

  tickTimers() {
    const inc = currentIncident(), el = $("#inc-timer");
    if (inc?.ai?.status === "thinking" && !this.quiet) this.renderAI(inc);
    if (!inc || this.quiet) { el.textContent = ""; return; }
    const start = ts(inc.opened_at);
    if (inc.status === "resolved" || !inc.active) {
      el.textContent = inc.metrics?.ttr_s != null ? `TTR ${fmtS(inc.metrics.ttr_s)}` : inc.closed_at ? `TTR ${fmtS((ts(inc.closed_at) - start) / 1000)}` : "";
    } else if (!isNaN(start)) {
      const s = Math.max(0, (serverNow() - start) / 1000);
      el.textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
    }
  }

  // ---------------------------------------------------------------- recall card ($vectorSearch)
  renderRecall(inc) {
    const card = $("#recall-card"), r = inc?.recall;
    if (!r) { card.hidden = true; return; }
    card.hidden = false;
    const used = $("#recall-used"); used.textContent = r.used ? "used · chain walk skipped" : "no usable hit"; used.className = `pill ${r.used ? "gold" : ""}`;
    const hits = (r.hits || []).slice(0, 3);
    $("#recall-body").innerHTML = `
      <div class="recall-q" title="${esc(r.query)}"><b>query</b> "${esc(r.query)}"</div>
      <div class="recall-f" title="${esc(JSON.stringify(r.filter || {}))}">filter ${esc(JSON.stringify(r.filter || {}))}${r.model ? ` · ${esc(r.model)}` : ""}</div>
      <div class="hits">${hits.length ? hits.map((h, i) => {
        const usable = r.used && i === 0;
        return `<div class="hit ${h.stale ? "stale" : ""}" data-pb="${esc(h.playbook_id)}"><span>${esc(h.playbook_id)}</span>
          <span class="sb"><i style="width:${Math.round(Math.max(0, Math.min(1, h.score || 0)) * 100)}%"></i></span>
          <span>${(h.score ?? 0).toFixed(3)}</span>
          <span class="tag ${usable ? "ok" : h.stale ? "no" : ""}">${h.stale ? "stale" : usable ? "usable" : `v${h.system_version ?? "?"}`}</span></div>`;
      }).join("") : '<div class="recall-none">0 hits: first time the Hive has seen this. It has to walk the chain.</div>'}</div>`;
    $("#recall-body").querySelectorAll("[data-pb]").forEach((el) => el.addEventListener("click", () => this.onInspect({ db: "swarm", coll: "playbooks", id: el.dataset.pb })));
  }

  // ---------------------------------------------------------------- AI second opinion (incidents.ai)
  // Claude proposes a root cause + op plan from the Hive's evidence; the deterministic policy and fresh
  // read-backs dispose. Nothing Claude says is executed. Absent field (no key) → card hidden.
  renderAI(inc) {
    // Shared shape (CONTRACTS §8): {kind, status?, model, hypothesis, cites, confidence, latency_ms,
    // proposed_ops[{op, target}], verdicts[{op, target, verdict: accepted|overruled, rule, reason}], evidence?}
    const card = $("#ai-card"), ai = inc?.ai;
    if (!ai) { card.hidden = true; this.aiKey = null; return; }
    card.hidden = false;
    const mapping = ai.kind === "schema_mapping";
    const age = (serverNow() - ts(ai.started_at)) / 1000;
    const status = !ai.status ? "done" : ai.status === "thinking" && age > 30 ? "offline" : ai.status;
    const ops = ai.proposed_ops || [];
    const vs = ops.map((o, i) => ({ ...o, ...((ai.verdicts || [])[i] || {}) }));
    const bad = vs.filter((v) => v.verdict === "overruled"), missed = ai.missed_ops || [];
    const key = `${inc._id}|${status}|${JSON.stringify(ai)}`;  // any change to the doc re-renders
    if (this.aiKey === key) return;
    this.aiKey = key;
    $("#ai-card .kicker").textContent = mapping ? "AI FIELD MAPPING" : "AI SECOND OPINION";
    $("#ai-model").textContent = `${ai.model || "claude"}${ai.latency_ms != null ? ` · ${(ai.latency_ms / 1000).toFixed(1)}s` : ""}${ai.confidence != null ? ` · conf ${(+ai.confidence).toFixed(2)}` : ""}`;
    const pill = $("#ai-verdict");
    if (status === "thinking") { pill.textContent = "Claude is thinking…"; pill.className = "pill violet pulse"; }
    else if (status !== "done") { pill.textContent = "offline"; pill.className = "pill"; }
    else if (ai.decision === "ai_plan") { pill.textContent = "Claude's plan runs"; pill.className = "pill green"; }
    else if (ai.decision === "rules_plan") { pill.textContent = "overruled → rules plan"; pill.className = "pill red"; }
    else if (bad.length) { pill.textContent = `${bad.length} overruled`; pill.className = "pill red"; }
    else if (mapping || ai.agrees_with_policy) { pill.textContent = mapping ? "verified ✓" : "agrees ✓"; pill.className = "pill green"; }
    else { pill.textContent = missed.length ? `${missed.length} added by policy` : "root differs"; pill.className = "pill blue"; }
    if (ai.drives && status === "offline") { pill.textContent = "timeout → rules plan"; pill.className = "pill"; }
    const flow = ai.drives
      ? '<div class="ai-flow"><b>Claude reasons + plans</b> → <i>guardrail P1–P6</i> → <i>passes: it runs · fails: rules plan</i> → <i>Guard verifies</i></div>'
      : mapping
      ? '<div class="ai-flow"><b>rules had no fix</b> → <i>Claude proposed a mapping</i> → <i>Guard verified by read-back</i></div>'
      : '<div class="ai-flow"><b>Claude proposes</b> → <i>guardrail + read-back check</i> → <i>Guard verifies</i> · nothing Claude says runs unchecked</div>';
    const t = ai.tally;
    const tally = t ? `<div class="ai-tally">all incidents · ${esc(t.proposed)} proposed · <b class="ok">${esc(t.accepted)} accepted</b> · <b class="bad">${esc(t.overruled)} overruled</b>${t.llm_calls != null ? ` · LLM fallbacks ${esc(t.llm_fallbacks)}/${esc(t.llm_calls)}` : ""}</div>` : "";
    const decision = ai.decision_reason ? `<div class="ai-decision ${ai.decision === "ai_plan" ? "ok" : "bad"}">${esc(ai.decision_reason)}</div>` : "";
    if (status !== "done") {
      $("#ai-body").innerHTML = flow + decision + `<div class="ai-off">${status === "thinking" ? "Asking Claude for an independent diagnosis from the evidence the Hive gathered…" : `No answer (${esc(ai.error || "timeout or error")}). The Hive carries on: the rules plan decides.`}</div>` + tally;
      return;
    }
    const pol = ai.policy || {};
    const root = mapping || !ai.root_system ? "" : ai.root_agrees
      ? `<div class="ai-root"><span class="ok">✓</span><span>root cause ${esc(ai.root_system)} · ${esc(ai.fault_class)}</span><span class="dim">same as policy</span></div>`
      : `<div class="ai-root"><span class="bad">${XMARK}</span><span>Claude: ${esc(ai.root_system)} · ${esc(ai.fault_class)}</span><span class="dim">policy: ${esc(pol.root_system || "?")} · ${esc(pol.fault_class || "?")}</span></div>`;
    const ev = new Map((ai.evidence || []).map((e) => [e.id, e]));
    const cited = (ai.cites || []).slice(0, 8);
    const lead = cited.map((c) => ev.get(c)).find((e) => e && /logs/.test(e.src)) || ev.get(cited[0]);
    const cites = cited.map((c) => { const e = ev.get(c); return `<span class="ai-cite" title="${esc(e ? `${e.src}: ${e.text}` : c)}">${esc(c)}</span>`; }).join("")
      + (lead ? `<div class="ai-lead" title="${esc(lead.text)}"><b>${esc(lead.id)}</b> ${esc(lead.src)}: ${esc(lead.text)}</div>` : "");
    const label = (o) => `${esc(o.op)}${o.target ? ` <i>@ ${esc(o.target)}</i>` : ""}`;
    // accepted + policy-added ops as one wrap of chips; each overruled op gets its own row with rule + reason
    const chips = vs.filter((o) => o.verdict !== "overruled").map((o) => `<span class="ai-chip ok" title="${esc([o.rule, o.reason, o.note].filter(Boolean).join(" · "))}">✓ ${label(o)}${o.gated ? '<b>approval first</b>' : ""}</span>`)
      .concat(missed.map((m) => `<span class="ai-chip add" title="Claude didn't propose it; the policy plan includes it">+ ${label(m)}<b>policy adds</b></span>`)).join("");
    const rows = bad.map((o) => `<li class="ai-op overruled"><span class="mk">${XMARK}</span><span class="nm">${label(o)}</span><span class="tg">${esc(o.rule || "overruled")}</span><span class="why">${esc(o.reason || "refused by the guardrail")}</span></li>`).join("");
    const hyps = (ai.hypotheses || []).slice(0, 3).map((h, i) => `<li class="${i ? "" : "top"}" title="${esc(h.why || "")}"><b>H${i + 1}</b> ${esc(h.system)}/${esc(h.fault_class)} <span>${esc(h.why || "")}</span>${(h.evidence || []).length ? ` <i>${esc(h.evidence.join(" "))}</i>` : ""}</li>`).join("");
    $("#ai-body").innerHTML = `${flow}${decision}
      ${hyps ? `<ol class="ai-hyps">${hyps}</ol>` : ai.hypothesis ? `<div class="ai-hyp" title="${esc(ai.hypothesis)}">“${esc(ai.hypothesis)}”</div>` : ""}
      ${cites ? `<div class="ai-cites"><span class="dim">cites</span>${cites}</div>` : ""}
      ${root}
      ${chips ? `<div class="ai-chips">${chips}</div>` : ""}
      ${rows ? `<ol class="ai-ops">${rows}</ol>` : ""}${!vs.length ? '<div class="ai-off">Claude proposed no ops.</div>' : ""}
      ${tally}`;
  }

  // ---------------------------------------------------------------- learning meter (aggregation pipeline)
  async refreshScorecard() {
    try { this.score = await api("GET", "/api/scorecard"); } catch { return; }
    this.renderLearning();
  }
  renderLearning() {
    const rounds = (this.score?.rounds || []).filter((r) => r.round != null).slice(-4);
    const sp = $("#speedup");
    sp.textContent = this.score?.speedup && this.score.speedup > 1 ? `TTR ${this.score.speedup.toFixed(1)}× faster` : "";
    sp.className = "pill gold";
    if (!rounds.length) { $("#learn-body").innerHTML = '<div class="empty" style="grid-column:1/4">Run a night: diagnosis time, hops and time-to-resolve per night land here.</div>'; return; }
    const fmtN = (v) => (v == null ? "—" : (+v).toFixed(v % 1 ? 1 : 0));
    const tile = (title, key, fmt, color) => {
      const vals = rounds.map((r) => r[key] ?? 0), max = Math.max(...vals, 1e-6);
      const first = rounds[0][key], last = rounds[rounds.length - 1][key];
      const many = rounds.length > 1 && first != null && last != null;
      const factor = many && last > 0 && first / last >= 1.15 ? `${(first / last).toFixed(1)}× ${key === "avg_hops" ? "fewer" : "faster"}` : "";
      const bars = rounds.map((r, i) => {
        const v = r[key] ?? 0, h = Math.max(3, Math.round((v / max) * 26));
        return `<span title="Night ${r.round}: ${fmt(r[key])}${r.recall_used ? " (recall)" : ""}" style="height:${h}px;background:${i === rounds.length - 1 ? color : "#2b4a66"}"><b>N${r.round}</b></span>`;
      }).join("");
      return `<div class="tile"><h4>${title}</h4>
        <div class="big">${many ? `<s>${fmt(first)}</s><span class="arrow">→</span>${fmt(last)}` : fmt(last)}</div>
        <div class="fx">${factor || (many ? "&nbsp;" : `night ${rounds[0].round}`)}</div>
        <div class="bars">${bars}</div></div>`;
    };
    $("#learn-body").innerHTML = tile("DIAGNOSIS", "avg_diagnosis_s", fmtS, "#ffd23f") + tile("HOPS", "avg_hops", fmtN, "#4cc9f0") + tile("RESOLVE", "avg_ttr_s", fmtS, "#2ee6a0");
  }

  // ---------------------------------------------------------------- gardener workshop
  renderGarden() {
    const u = latestUpgrade(), card = $("#garden-card");
    if (!u) { card.hidden = true; return; }
    card.hidden = false;
    $("#upg-id").textContent = u._id; $("#upg-id").onclick = () => this.onInspect({ db: "swarm", coll: "upgrade_requests", id: u._id }); $("#upg-id").style.cursor = "pointer";
    const st = $("#upg-status"); st.textContent = String(u.status || "").replace(/_/g, " "); st.className = `pill ${u.status === "proven" || u.status === "deployed" ? "green" : u.status === "rejected" ? "red" : u.status === "awaiting_approval" ? "gold" : "blue"}`;
    const order = ["requested", "planning", "testing", "awaiting_approval", "deploying", "deployed", "proven"];
    const labels = ["req", "plan", "test", "approve", "deploy", "live", "proven"];
    const at = order.indexOf(u.status);
    const dl = String(u.diff || "").split("\n").filter((l) => !l.startsWith("---") && !l.startsWith("+++") && !l.startsWith("@@") && l.trim());
    const firstChange = Math.max(0, dl.findIndex((l) => l.startsWith("-") || l.startsWith("+")));
    const diff = dl.slice(Math.max(0, firstChange - 1)).filter((l) => !/^\+\s*("""|$)/.test(l) && !/^\+\s+[A-Z][\w ,\-()]*\.$/.test(l)).slice(0, 7)
      .map((l) => `<span class="${l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : ""}">${esc(l)}</span>`).join("\n");
    const vb = u.version_before ?? state.systems.get(u.system)?.version ?? 1;
    const va = u.version_after;
    $("#garden-body").innerHTML = `
      <div class="upg-steps">${labels.map((l, i) => `<span class="${i < at ? "done" : i === at ? "now" : ""}">${l}</span>`).join("")}</div>
      <div class="upg-line"><b style="color:var(--ink)">${esc(u.system)}</b><span>${esc(u.weakness || u.fault_class || "")}</span>
        ${u.diff_lines ? `<span>${esc(u.diff_lines)}-line diff</span>` : ""}<span class="ver">v${esc(vb)}</span><span class="arrow">→</span><span class="ver ${va ? "new" : ""}">v${esc(va ?? vb + 1)}</span>
        ${u.tests ? `<span style="color:${u.tests.pytest_passed ?? u.tests.passed ? "var(--green)" : "var(--red)"}">pytest ${u.tests.pytest_passed ?? u.tests.passed ? "✓" : "✗"}</span><span style="color:${u.tests.replay_passed ?? u.tests.passed ? "var(--green)" : "var(--red)"}">replay ${u.tests.replay_passed ?? u.tests.passed ? "✓" : "✗"}</span>` : ""}
        ${u.evidence?.length ? `<span class="dim">from ${esc(u.evidence.join(", "))}</span>` : ""}
        ${u.prove ? `<span style="color:${u.prove.incident_opened ? "var(--red)" : "var(--green)"}">replay: ${u.prove.incident_opened ? "incident opened" : "no incident"}</span>` : ""}</div>
      ${diff ? `<pre class="diff">${diff}</pre>` : ""}`;
  }

  // ---------------------------------------------------------------- activity stream
  addThought(t, { animate = true } = {}) {
    const ol = $("#activity");
    const li = document.createElement("li");
    const color = ROLE[t.role] || "#a9b8cc";
    li.className = t.kind || "thought"; li.style.setProperty("--c", color);
    if (!animate) li.style.animation = "none";
    const when = t.ts ? new Date(ts(t.ts)).toLocaleTimeString([], { hour12: false }) : "";
    li.innerHTML = `<span class="ic">${icon(t.kind)}</span>
      <div class="hd"><span class="who">${esc(shortAgent(t.agent || "?"))}</span><span class="t">${esc(when)}${t.incident ? " · " + esc(t.incident) : ""}</span></div>
      <div class="txt">${esc(t.text)}</div>`;
    ol.prepend(li);
    while (ol.children.length > 40) ol.lastChild.remove();
  }
  renderActivity() {
    $("#activity").innerHTML = "";
    for (const t of state.thoughts.slice(-40)) this.addThought(t, { animate: false });
  }

  // ---------------------------------------------------------------- Beekeeper approval modal
  renderApprovals() {
    const pend = pendingApprovals();
    const a = pend[0];
    if (!a) { $("#modal").hidden = true; this.modalId = null; return; }
    if (this.modalId === a._id) return;
    this.modalId = a._id;
    const inc = state.incidents.get(a.incident);
    const step = inc?.steps?.find((s) => s.step_id === a.step_id);
    const upg = a.kind === "upgrade" ? state.upgrades.get(a.upgrade) : null;
    const desc = String(a.desc || "");
    const call = desc.includes(": ") ? desc.slice(0, desc.indexOf(": ")) : step ? `${step.op} on ${step.system}` : desc;
    const human = desc.includes(": ") ? desc.slice(desc.indexOf(": ") + 2) : "";
    $("#apr-kicker").textContent = a.kind === "upgrade" ? "GARDENER REQUESTS APPROVAL · CODE UPGRADE" : "QUEEN REQUESTS APPROVAL · DESTRUCTIVE STEP";
    $("#apr-title").innerHTML = a.kind === "upgrade"
      ? `Deploy <em>${esc(a.upgrade || "upgrade")}</em> to ${esc(upg?.system || "the estate")}`
      : `<code>${esc(call.replace(/ on (\w+)$/, ""))}</code> <span class="on">on ${esc(step?.system || (call.match(/ on (\w+)$/) || [])[1] || "")}</span>`;
    $("#apr-desc").textContent = prettyTime(a.kind === "upgrade" ? (upg?.weakness || a.desc || "") : human);
    $("#apr-meta").innerHTML = `<span>${esc(a._id)}</span>${a.incident ? `<span>${esc(a.incident)}</span>` : ""}${a.step_id ? `<span>${esc(a.step_id)}</span>` : ""}
      ${upg?.diff_lines ? `<span>${esc(upg.diff_lines)}-line diff</span>` : ""}${upg?.tests ? `<span class="ok">pytest ${upg.tests.pytest_passed ?? upg.tests.passed ? "passed" : "FAILED"} · replay ${upg.tests.replay_passed ?? upg.tests.passed ? "passed" : "FAILED"}</span>` : ""}
      <span>requested by ${esc(a.requested_by || "queen-1")}</span>${pend.length > 1 ? `<span>+${pend.length - 1} more pending</span>` : ""}`;
    $("#apr-risk").innerHTML = a.risk ? `<b>RISK</b> ${esc(a.risk)}` : "";
    $("#apr-note").textContent = a.kind === "upgrade"
      ? "On approve: hot-deploy the patch, mark old playbooks stale, then Chaos replays the fault to prove it."
      : "Nothing has been sent yet. The ledger row stays 'planned' until you decide.";
    $("#modal").hidden = false;
  }
  bindOverlays() {
    const decide = (decision) => async () => {
      const id = this.modalId; if (!id) return;
      $("#modal").hidden = true;
      try { await api("POST", `/api/approvals/${encodeURIComponent(id)}`, { decision }); } catch (e) { this.toast(e.message); }
    };
    $("#apr-yes").addEventListener("click", decide("approved"));
    $("#apr-no").addEventListener("click", decide("denied"));
    $("#drawer-x").addEventListener("click", () => ($("#drawer").hidden = true));
    $("#atlas-x").addEventListener("click", () => ($("#atlas").hidden = true));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#drawer").hidden = true; $("#atlas").hidden = true; } });
  }

  // ---------------------------------------------------------------- inspector drawer
  async inspect({ db, coll, id }) {
    const dr = $("#drawer"); dr.hidden = false;
    $("#drawer-title").textContent = `${db}.${coll} · ${id}`;
    $("#drawer-q").textContent = `GET /api/doc/${db}/${coll}/${id}`;
    $("#drawer-json").textContent = "loading…";
    try {
      const r = await api("GET", `/api/doc/${db}/${coll}/${encodeURIComponent(id)}`);
      $("#drawer-title").textContent = `${r.db_name}.${coll}`;
      $("#drawer-q").textContent = `db.${coll}.findOne(${JSON.stringify(r.query)})`;
      $("#drawer-json").innerHTML = highlight(r.doc);
    } catch (e) { $("#drawer-json").textContent = String(e.message || e); }
  }

  // ---------------------------------------------------------------- Atlas view
  async openAtlas(focus) {
    const ov = $("#atlas"); ov.hidden = false;
    $("#atlas-grid").innerHTML = '<div class="empty">loading…</div>';
    try {
      const r = await api("GET", "/api/atlas");
      $("#atlas-sub").textContent = `${r.uri_host} · ${r.collections.length} collections · counts, indexes and one real document each`;
      $("#atlas-grid").innerHTML = r.collections.map((c) => `
        <div class="acoll" data-db="${c.db}" data-coll="${c.coll}" ${focus === c.coll ? 'style="border-color:#00ed64;box-shadow:0 0 20px rgba(0,237,100,.3)"' : ""}>
          <h3>${esc(c.coll)}<span class="db">${esc(c.db_name)}</span><span class="cnt">${c.count.toLocaleString()}</span></h3>
          <div class="ix">${c.validator ? '<span class="val">$jsonSchema</span>' : ""}${c.search_indexes.map((s) => `<span class="vec">${esc(s.type || "search")}: ${esc(s.name)}${s.queryable ? "" : " (building)"}</span>`).join("")}
            ${c.indexes.filter((i) => i.name !== "_id_").map((i) => `<span title="${esc(JSON.stringify(i.keys))}">${esc(i.name)}${i.unique ? " · unique" : ""}${i.partial ? " · partial" : ""}${i.ttl_s ? ` · TTL ${Math.round(i.ttl_s / 3600)}h` : ""}</span>`).join("")}</div>
          <pre>${c.sample ? esc(JSON.stringify(c.sample, null, 1)).slice(0, 600) : "(empty)"}</pre></div>`).join("");
      $("#atlas-grid").querySelectorAll(".acoll").forEach((el) => el.addEventListener("click", () => {
        const c = r.collections.find((x) => x.coll === el.dataset.coll && x.db === el.dataset.db);
        if (c?.sample?._id != null) this.inspect({ db: c.db, coll: c.coll, id: typeof c.sample._id === "object" ? JSON.stringify(c.sample._id) : c.sample._id });
      }));
      if (focus) $(`.acoll[data-coll="${focus}"]`)?.scrollIntoView({ block: "center" });
    } catch (e) { $("#atlas-grid").textContent = String(e.message || e); }
  }
}

function highlight(obj) {
  const json = esc(JSON.stringify(obj, null, 2));
  return json.replace(/(&quot;(?:\\.|[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false)\b|\bnull\b|-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/gi, (m, str, colon, bool) => {
    if (str) return colon ? `<span class="k">${str}</span>${colon}` : `<span class="s">${str}</span>`;
    if (bool) return `<span class="b">${m}</span>`;
    if (m === "null") return `<span class="z">${m}</span>`;
    return `<span class="n">${m}</span>`;
  });
}

// Story beats: turn raw MongoDB change events into visual moments.
// Each handler compares the new document with the previous one (both come from the change stream)
// and fires stage effects, captions and panel animations. Nothing here writes anything.
import { state, on, currentIncident } from "./store.js";
import { C } from "./draw.js";

export function installBeats(stage, panels, caption) {
  // ------------------------------------------------ every write → a particle into its spine cell
  on("change", (m) => {
    const actor = actorPos(stage, m);
    stage.writeParticle(m.coll, actor);
  });
  on("tick", (m) => {
    for (const [coll, n] of Object.entries(m.ops || {})) {
      if (coll === "checkpoint_writes") stage.pulseCell("checkpoints", Math.min(0.6, 0.2 * n));
      if (coll === "system_state") stage.pulseCell("systems", Math.min(0.4, 0.1 * n));
    }
    if (m.heartbeats) stage.pulseCell("agents", Math.min(0.5, 0.08 * m.heartbeats));
  });

  // ------------------------------------------------ Red: chaos strikes
  on("change:chaos_events", (doc, prev, m) => {
    if (!doc) return;
    if (panels.quiet) panels.renderIncident();
    if (m.op === "insert") {
      stage.strike(doc);
      if (doc.fault === "agent_kill" && doc.params?.selector?.agent) caption(`Chaos: SIGKILL ${doc.params.selector.agent}`, "a real process kill · no warning to the swarm", C.red);
      else if (doc.fault === "agent_kill") caption("Chaos: agent kill armed", "SIGKILL the next worker that sends a step", C.red);
      else if (doc.fault === "schema_drift") caption(`Chaos: vendor patch on ${doc.target}`, "a field in the xfer manifest is silently renamed · unix still says healthy", C.red);
      else if (doc.fault === "lying_success") caption(`Chaos: ${doc.target} will lie`, `next ${doc.params?.count ?? 1} op returns success without applying`, "#ff4fd8");
      else caption(`Chaos strikes: ${doc.fault}`, `→ ${doc.target}${doc.manual ? " (manual)" : doc.scenario ? ` · ${doc.scenario}` : ""}`, C.red);
    } else if (doc.fault === "agent_kill" && doc.result?.agent && !prev?.result?.agent) {
      stage.strikeAgent(doc.result.agent, "SIGKILL");
      caption(`SIGKILL → ${doc.result.agent}`, doc.result.role === "queen" ? "real process killed · no goodbye · the queen lease will expire"
        : "real process killed mid-step · its ticket drops · the lease will expire", C.red);
    } else if (doc.fault === "agent_kill" && doc.result && !doc.result.ok && !prev?.result) {
      stage.hunt = null;  // stop the reticle: nobody is going to be killed
      if (doc.result.how === "gave_up") caption("Kill skipped: no worker mid-step", `gave up after ${Math.round(doc.result.waited_s ?? 0)} s · nobody was killed`, C.ink2);
      else caption("Kill called off", doc.result.error || "no victim", C.ink2);
    }
  });

  // ------------------------------------------------ the estate
  on("change:systems", (doc, prev) => {
    if (!doc || !prev) return;
    if (prev.status !== doc.status) {
      if (doc.status !== "up") {
        stage.burst(doc._id, doc.status === "down" ? C.red : C.amber);
        if (doc._id === "portal") caption("Customers see it first", doc.symptom || "portal degraded: stale balances, still HTTP 200", C.amber);
      } else stage.burst(doc._id, C.green, "recovered");
    }
  });

  // the Gardener's patch at work: a malformed record is rejected instead of ABENDing the batch
  let lastReject = 0;
  on("change:system_logs", (doc, prev, m) => {
    if (!doc || m.op !== "insert" || !/REJECTED/i.test(doc.line || "")) return;
    if (performance.now() - lastReject < 6000) return;
    lastReject = performance.now();
    const v = state.systems.get(doc.system)?.version;
    stage.stamp(doc.system, "RECORD REJECTED", C.green, { sub: `v${v ?? "?"} validation · batch continues`, dur: 4.5, rot: -0.08 });
    caption(`${doc.system} v${v ?? "?"}: bad record rejected`, "the batch keeps running · no ABEND, no incident", C.green);
  });

  // ------------------------------------------------ incidents: the Hive's work
  on("change:incidents", (doc, prev, m) => {
    panels.renderIncident();
    if (!doc) return;
    if (m.op === "insert" || !prev) {
      if (m.op === "insert") caption(`Incident ${doc._id} opened`, `symptom on ${doc.system}: ${(doc.symptoms || [])[0] || "unhealthy"}`, C.amber);
      return;
    }
    // diagnosis walk (upstream, portal → unix → mainframe)
    const p0 = prev.diagnosis_path || [], p1 = doc.diagnosis_path || [];
    for (let i = p0.length; i < p1.length; i++) {
      if (i > 0) stage.pipePulse(p1[i - 1], p1[i], C.blue);
      stage.burst(p1[i], C.blue, i === 0 ? "inspect" : `hop ${i}`);
    }
    if (doc.root_cause_system && !prev.root_cause_system) {
      stage.burst(doc.root_cause_system, C.red, "root cause");
      caption(`Root cause: ${doc.root_cause_system}`, doc.root_cause || doc.fault_class || "", C.red);
    }
    // AI card landed (first observed completion: change-stream lookups can skip "thinking"; absent status = done)
    const aiDone = (a) => a && (!a.status || a.status === "done");
    // (skipped while a lie is being caught, so it never covers the "ACK ≠ APPLIED" caption)
    const lieInFlight = (doc.steps || []).some((st) => (st.attempts || []).length > 1 && st.status !== "verified");
    if (aiDone(doc.ai) && !aiDone(prev.ai) && !lieInFlight) {
      const ai = doc.ai, ops = ai.proposed_ops || [];
      const bad = ops.map((o, i) => ({ ...o, ...((ai.verdicts || [])[i] || {}) })).filter((v) => v.verdict === "overruled");
      const missed = ai.missed_ops || [];
      const who = doc.owner || "scout-1";
      const p = stage.posOfAgent(who); if (p) stage.float(p.x, p.y - 44, ai.kind === "schema_mapping" ? "Claude: field mapping" : "Claude: second opinion", C.violet, 13, 3);
      const what = ai.kind === "schema_mapping" ? "AI field mapping" : "AI second opinion";
      const title = ai.decision === "ai_plan" ? "Claude's plan passed the guardrail: it runs"
        : ai.decision === "rules_plan" ? "Guardrail overruled Claude: rules plan runs"
        : bad.length ? `${what}: ${bad.length} op${bad.length === 1 ? "" : "s"} overruled`
        : ai.kind === "schema_mapping" ? `${what}: verified by read-back`
        : ai.agrees_with_policy ? `${what}: agrees`
        : !ai.root_agrees ? `${what}: different root cause` : `${what}: missed ${missed.length} step${missed.length === 1 ? "" : "s"}`;
      const sub = ai.decision_reason && ai.decision ? ai.decision_reason
        : bad.length ? `${bad[0].op} → ${bad[0].rule || "refused"}: ${bad[0].reason || ""}`
        : ai.kind === "schema_mapping" ? (ai.hypothesis || "rules had no fix → Claude proposed a mapping → Guard verified")
        : ai.agrees_with_policy ? "Claude reached the same plan independently · the guardrail still checks every op"
        : !ai.root_agrees ? `Claude: ${ai.root_system}/${ai.fault_class} · policy: ${ai.policy?.root_system}/${ai.policy?.fault_class} · policy decides`
        : `policy adds ${missed.map((m) => m.op).join(", ")}`;
      caption(title, sub, C.violet);
    }
    // recall ($vectorSearch) — golden path from the playbooks cell to the scout
    if (doc.recall && !prev.recall) {
      const hit = (doc.recall.hits || [])[0];
      const who = doc.owner || "scout-1";
      if (doc.recall.used) {
        stage.beam(() => stage.cellPos("playbooks"), () => stage.posOfAgent(who) || stage.hivePos(), C.gold, `$vectorSearch ${hit ? hit.score.toFixed(2) : ""}`, 4);
        caption("Recall hit: honey found", `${hit?.playbook_id || "playbook"} · score ${hit ? hit.score.toFixed(3) : "?"} · chain walk skipped`, C.gold);
      } else {
        stage.beam(() => stage.cellPos("playbooks"), () => stage.posOfAgent(who) || stage.hivePos(), "#64768e", "no usable hit", 2.2);
        caption("Recall: nothing usable", "first time: the scout walks the chain", C.blue);
      }
    }
    // steps: sent / verified / new attempts
    const before = new Map((prev.steps || []).map((s) => [s.step_id, s]));
    for (const s of doc.steps || []) {
      const o = before.get(s.step_id);
      if (o && o.status !== s.status) {
        if (s.status === "sent") stage.beam(() => stage.posOfAgent(s.agent) || stage.hivePos(), () => stage.posOfSystem(s.system), C.blue, s.op, 1.6);
        if (s.status === "verified") {
          const guard = [...state.agents.values()].find((a) => a.role === "guard")?._id || "guard-1";
          stage.beam(() => stage.posOfAgent(guard) || stage.hivePos(), () => stage.posOfSystem(s.system), C.green, "read-back OK", 1.8);
          if ((s.attempts || []).length > 1) {
            stage.burst(s.system, C.green);
            stage.stamp(s.system, "VERIFIED", C.green, { sub: `attempt ${(s.attempts || []).length} · read-back evidence`, rot: 0.1 });
          } else stage.burst(s.system, C.green, `${s.op} verified`);
        }
      }
      if (o && (s.attempts || []).length > (o.attempts || []).length && (o.attempts || []).length > 0 && s.status !== "verified") {
        if (!panels.stamps.has(s.step_id)) panels.stamp(s.step_id);
      }
    }
    // nurse takeover from a MongoDB checkpoint
    const h0 = (prev.handoffs || []).length, hs = doc.handoffs || [];
    for (let i = h0; i < hs.length; i++) {
      const h = hs[i], cp = String(h.checkpoint_id || "").slice(0, 14);
      stage.beam(() => stage.cellPos("checkpoints"), () => stage.posOfAgent(h.to) || stage.hivePos(), C.pink, `checkpoint ${cp}`, 4);
      caption(`Nurse takeover: ${h.to}`, `resumed from checkpoint ${cp || "?"} · no verified step repeated`, C.pink);
      const p = stage.posOfAgent(h.to); if (p) stage.float(p.x, p.y - 44, "resumed from checkpoint", C.pink, 13, 3.5);
    }
    if (doc.status === "resolved" && prev.status !== "resolved") {
      const ttr = doc.metrics?.ttr_s;
      stage.banner("ESTATE GREEN", `${doc._id} resolved${ttr != null ? ` · TTR ${Math.round(ttr)}s` : ""} · ${doc.metrics?.hops ?? "?"} hop${doc.metrics?.hops === 1 ? "" : "s"} · playbook saved`, C.green, 4.5);
      const who = doc.owner || "scout-1";
      stage.beam(() => stage.posOfAgent(who) || stage.hivePos(), () => stage.cellPos("playbooks"), C.gold, "playbook + embedding", 3);
      caption(`Resolved ${doc._id}`, `${ttr != null ? `time to resolve ${Math.round(ttr)}s · ` : ""}${doc.metrics?.hops ?? "?"} hops · playbook saved as honey`, C.green);
      for (const s of doc.affected || [doc.system]) stage.burst(s, C.green);
      panels.refreshScorecard();
    }
  });

  // ------------------------------------------------ schema drift: a verified field mapping was learned
  on("change:schema_mappings", (doc, prev, m) => {
    if (!doc || !doc.source || prev?.version === doc.version) return;
    if (doc.version === 1 || doc.source === doc.field) return;  // round reset rewound the live mapping to v1
    const sys = doc.system || "unix";
    const word = doc.reused ? "MAPPING REUSED" : "MAPPING LEARNED";
    stage.stamp(sys, word, C.green, { sub: `${doc.previous ?? "?"} → ${doc.source} · v${doc.version}`, dur: 5, rot: -0.06 });
    caption(`${sys} renamed ${doc.previous ?? "?"} → ${doc.source} · ` +
      (doc.reused ? `seen before: mapping v${doc.version} reused` : `mapping learned v${doc.version}`),
      doc.reused ? "Recalled from swarm.schema_mappings history (no Claude call) · Guard verified by read-back"
                 : "Guard verified by read-back · stored in swarm.schema_mappings for every agent", C.green);
  });

  // ------------------------------------------------ thoughts (activity) + the Guard's rejection stamp
  on("change:thoughts", (doc, prev, m) => {
    if (!doc || m.op !== "insert") return;
    panels.addThought(doc);
    if (doc.kind === "reject") {
      const inc = state.incidents.get(doc.incident) || currentIncident();
      // the attempt the Guard just rejected: an in-flight step on that system (sent/failed/unknown)
      const open = (inc?.steps || []).filter((s) => s.status !== "verified" && (!doc.system || s.system === doc.system));
      const step = open.filter((s) => ["sent", "failed", "unknown"].includes(s.status)).pop() || open[0];
      if (step) panels.stamp(step.step_id);
      if (doc.system) { stage.burst(doc.system, C.red); stage.stamp(doc.system, "ACK ≠ APPLIED", C.red, { sub: "said applied:true · state unchanged", dur: 4.5 }); }
      caption("ACK ≠ APPLIED", `${doc.agent || "guard"}: ${doc.text}`, C.red);
    }
    if (doc.kind === "handoff" && state.agents.get(doc.agent)?.role === "queen") {
      caption(`Queen failover: ${doc.agent} takes the crown`, doc.text, C.pink);
    }
  });

  // ------------------------------------------------ agents: deaths
  on("change:agents", (doc, prev) => {
    panels.renderTopbar();
    if (!doc || !prev) return;
    if (doc.status === "dead" && prev.status !== "dead") {
      caption(`${doc._id} is down`, doc.role === "queen" ? "queen lease expires · the standby queen takes the crown"
        : `${doc.task ? `ticket ${doc.task} dropped · ` : ""}lease expires, a nurse will pick it up`, C.red);
    }
  });

  on("change:approvals", () => panels.renderApprovals());
  on("change:upgrade_requests", (doc, prev) => {
    panels.renderGarden();
    if (doc && prev && doc.status !== prev.status) {
      if (doc.status === "deployed") caption(`Gardener deployed ${doc.system} upgrade`, `v${doc.version_before ?? "?"} → v${doc.version_after ?? "?"} · old playbooks marked stale`, C.green);
      else if (doc.status === "proven") {
        caption("Never again", "Chaos replayed the fault: no incident opened", C.green);
        stage.banner("NEVER AGAIN", `${doc._id} proven · ${doc.system} v${doc.version_after ?? "?"} shrugged off ${doc.fault_class || "the fault"}`, C.green, 5);
      }
    } else if (doc && !prev) caption("Sentinel: recurring fault", `${doc.fault_class || doc.weakness} on ${doc.system} → Gardener`, C.green);
  });
  on("change:stats", (doc, prev, m) => { panels.renderTopbar(); if (m.key === "current") panels.renderIncident(); });
  on("change:runs", () => panels.renderTopbar());
}

// who caused this write? (so the particle starts at the right sprite)
function actorPos(stage, m) {
  const d = m.doc || {};
  switch (m.coll) {
    case "systems": return stage.posOfSystem(m.key);
    case "system_logs": case "op_receipts": return stage.posOfSystem(d.system);
    case "chaos_events": case "runs": return stage.shipPos();
    case "agents": return stage.posOfAgent(m.key);
    case "thoughts": case "ledger": return stage.posOfAgent(d.agent);
    case "messages": return stage.posOfAgent(d.from);
    case "incidents": case "tasks": return stage.posOfAgent(d.owner) || stage.hivePos();
    case "playbooks": return stage.hivePos();
    case "approvals": return d.status === "pending" ? stage.posOfAgent(d.requested_by || "queen-1") : { x: 740, y: -20 };
    case "upgrade_requests": return stage.workshopPos();
    case "checkpoints": {
      const a = [...state.agents.values()].find((x) => x.task && x.task === d.thread_id);
      return (a && stage.posOfAgent(a._id)) || stage.hivePos();
    }
    default: return null;
  }
}

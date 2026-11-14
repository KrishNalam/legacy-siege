# Legacy Siege — CONTRACTS (frozen; change only via the integrator)

Self-healing ops team of AI agents ("the Hive", Blue) keeps a simulated bank's
interconnected legacy estate online while a seeded Chaos Monkey ("the Fleet", Red)
attacks both the systems and the agents. A neutral **Gardener** turns repeated fixes
into permanent code upgrades. **MongoDB is the spine: every piece of state lives in
MongoDB; processes are disposable.** The war-room visualization only *watches* MongoDB
(change streams) — it never holds state.

Repo root: `~/projects/legacy-siege`. Python 3.12 venv at `.venv` (`.venv/bin/python`).
Run anything as a module from repo root: `.venv/bin/python -m swarm.supervisor`.
Design doc (source of intent): `docs/design-doc.txt` (PDF text; letters 'a'/'g' were dropped by the font).

**Scope (cuts agreed):** 3 systems only (no IE console), no network partition, no schema drift,
no adaptive/LLM chaos, Gardener ships ONE predetermined remediation (batch record validation).
**Never cut** (they carry the score): (1) cascading failure traced portal→unix→mainframe,
(2) lying system caught by the Guard bee, (3) agent kill → Nurse takeover from a MongoDB
checkpoint, (4) round-2 speedup via `$vectorSearch` recall. Narration is "activity", not chain of thought.

## 0. Shared code (`common/`, already written — import it, don't fork it)
- `common/config.py` — env/config (`MONGODB_URI`, ports, timings, `PACE`, keys, `CHAIN`).
- `common/db.py` — `client()`, `world()`, `swarm()`, `col(name)` (routes to right DB), `now()`, `next_id(prefix)` (atomic `INC-0001`), partition helpers (unused now).
- `common/embed.py` — `embed(text, input_type)`, `provider()` → `"voyage"`|`"local-hash"`. Dim `config.EMBED_DIM`=1024. Deterministic (blake2b feature hashing).
- `common/llm.py` — `llm_available()`, `complete(system, prompt, max_tokens) -> str|None`. **Always have a deterministic fallback**; the demo must run with no keys.
- `common/events.py` — `think(agent, role, text, incident, system, kind)` → `swarm.thoughts`; `say(frm, to, kind, body)` → `swarm.messages`.
- `scripts/setup_db.py` — collections, validators, indexes, vector index. `--reset` wipes runtime data. If you need a new index/validator, add it there (integrator merges).
- `run.py` — starts systems, war room, swarm supervisor, gardener.

Env defaults: `MONGODB_URI=mongodb://localhost:27027/?directConnection=true` (docker
`legacy-siege-mongo`, mongodb-atlas-local:8.0 — vector search, change streams, transactions verified).
Systems `0.0.0.0:8701`, war room `0.0.0.0:8700`. Human URL: `http://100.105.248.23:8700`.
Timings: `HEARTBEAT_S=3`, `LEASE_S=10`, `PACE=1.0` (multiplier on deliberate delays; `PACE=0` in tests), `SLOW_APPLY_S=20`.
**Tests must use isolated DBs**: set `WORLD_DB=world_test_<name>` / `SWARM_DB=swarm_test_<name>` env before importing `common`, drop them after.

## 1. Databases & collections
Two DBs so Red and Blue never share writes: `world` (Red: A + C) and `swarm` (Blue: B + D; war room writes only `approvals` decisions and `runs`).
All timestamps are BSON datetimes (UTC, tz-aware client).

### world.systems  (A only)
```
{_id: "mainframe"|"unix"|"portal", display, version: 1, status: "up"|"degraded"|"down",
 blames: null|"<upstream id>", depends_on: [ids],
 metrics: {latency_ms, error_rate, queue_depth, disk_pct, last_batch_ok: datetime|null, sessions, batch_status},
 vulns: [{id, kind, desc, open, exploited}], faults_active: [{fault, since, params}],  # ground truth: viz/scorecard only, NEVER read by swarm
 symptom: null|str, updated_at}
```
**Health semantics (makes diagnosis a real walk):** after `batch_abort`, the mainframe's own
`/health` still says `up` (its online region is fine) — the failure is only visible in its
`/logs` (`IEF450I NIGHTBAT ABEND=S0C7 ...`) and `/metrics` (`batch_status:"ABENDED"`, stale `last_batch_ok`).
A few ticks later unix → `degraded`, blames `mainframe` (no new batch file); then portal →
`degraded`, blames `unix`, serving **stale balances with HTTP 200**. The portal symptom is what the audience sees first.
### world.system_state (A only) `{_id, system, kind, data, updated_at}` — batch queue/poison records, config files, cron table, sessions, ledger rows.
### world.system_logs (A only) `{system, ts, level, code, line}` TTL 24h.
### world.op_receipts (A only) `{_id: action_id, system, op, params, accepted, claimed_applied, effective: bool, lied: bool, apply_at: datetime|null, applied_at, ts}` — persisted so transport retries and delayed (slow) ops survive restarts.
### world.chaos_scenarios (C only) `{_id, seed, title, steps: [{t, fault, target, params}]}`
### world.chaos_events (C only) `{_id, fault, target, params, ts, scenario, run_id, round, seed, manual, result}` — **the swarm never reads this.**
### world.runs (war room/C) `{_id: "RUN-0001", scenario, round, started_at, ended_at}` — joins chaos, incidents, scorecard. War room also mirrors `{_id:"current", run_id, round}` into `swarm.stats` so Blue can tag incidents with a round without reading Red's data.

### swarm.incidents (B)
```
{_id: "INC-0001", active: true, correlation_key: "chain:portal", status: "open"|"claimed"|"diagnosing"|"fixing"|"verifying"|"awaiting_approval"|"resolved"|"failed",
 system: "<symptom system>", affected: ["portal","unix"], root_cause_system, fault_class, symptoms: [str], root_cause,
 owner: null|"scout-1", lease_until, generation: 0, run_id, round,
 recall: {query: str, filter: {...}, hits: [{playbook_id, score, stale, fault_class, system_version}], used: bool, model},
 diagnosis_path: ["portal","unix","mainframe"], handoffs: [{from, to, ts, reason, checkpoint_id}],
 steps: [{step_id: "INC-0001-s1", n, desc, system, op, params, destructive, task_id,
          status: "planned"|"sent"|"verified"|"failed"|"unknown", action_id (current attempt), attempts: [action_id...],
          agent, evidence: null|str, approval_id, ts}],
 opened_at, closed_at,
 metrics: {ttr_s, diagnosis_s, approval_wait_s, takeover_s, hops, steps, attempts}}
```
- **Dedup / coalescing:** unique partial index on `correlation_key` where `active: true`. Key = `"chain:" + <most-downstream unhealthy system of the contiguous unhealthy segment>`. A newly unhealthy system adjacent (via `CHAIN`) to an active incident's `affected` set is `$addToSet` into that incident, never a new one. All three unhealthy at once → exactly one incident. Resolve sets `active:false`.
- `$jsonSchema` rejects unknown step status and any `verified` step without non-empty `evidence`.
### swarm.tasks (B) — the unit of leased worker work
`{_id: "INC-0001-t1", incident, specialty: "mainframe"|"unix"|"web", kind: "diagnose"|"fix", step_ids: [...], status: "open"|"claimed"|"done"|"failed",
  owner: null|agent_id, lease_until, generation: int, checkpoint_thread: "<task _id>", ts}`
- Claim = `findOneAndUpdate({_id, $or:[{owner:null},{lease_until:{$lt:now}}]}, {$set:{owner, lease_until}, $inc:{generation:1}})`. Renew/release are conditional on `{owner, generation}` (fencing). Every step write by a worker is conditional on its task's `{owner, generation}` still matching — a stale (killed-and-replaced) worker's writes are rejected.
- LangGraph thread id = task `_id` (checkpoints follow the task, not the process). Incident-level (Scout/Queen) thread ids = `"<incident>:scout"`, `"<incident>:queen"`.
### swarm.agents (B)
```
{_id: "worker-mainframe-1", role: "queen"|"scout"|"worker"|"guard"|"waggle"|"sentinel"|"nurse"|"gardener",
 specialty, status: "idle"|"working"|"standby"|"dead", target: null|"<system id>"|"hive"|"bench"|"workshop",
 incident, task, phase: null|"diagnosing"|"step_sent"|"awaiting_verify"|"awaiting_approval"|...,
 pid, last_seen, standby_for, generation, started_at, died_at}
```
`target` drives sprite position. `phase:"step_sent"` is what the judge's kill button waits for.
### swarm.ledger (B, D) — one **mutable receipt per attempt**
`{action_id (unique), step_id, incident, task_id, agent, system, op, params, status: "planned"|"sent"|"verified"|"failed"|"unknown", system_response, evidence, approver, ts, updated_at}`.
Each step transition = **one transaction**: update the incident step + upsert/update that attempt's ledger row.
Retrying the same attempt (transport retry / reconcile) reuses its `action_id` (A returns the stored receipt; insert is a dup-key no-op).
When the Guard proves an attempt did NOT apply (lying), the step gets a **new attempt** `action_id = "<step_id>-a<n>"`.
### swarm.thoughts (B, D) `{agent, role, incident, system, text, kind: "thought"|"action"|"verify"|"reject"|"recall"|"handoff"|"learn"|"approval"|"upgrade", ts}` TTL 24h. One short sentence.
### swarm.messages (B) waggle bus `{from, to, kind, body, ts}` TTL 24h.
### swarm.playbooks (B; staleness by B)
`{_id, incident, system: <ROOT-CAUSE system>, symptom_system, system_version: <root system version when learned>, fault_class,
  symptoms, symptom_text, root_cause, fix: [{system, op, params}], outcome, metrics, embedding, embedding_model: "local-hash-v1"|"voyage-3.5", stale: bool, created_at, verified_at}`
Vector index `playbook_vec`: filters `system`, `fault_class`, `stale`, `system_version`, `embedding_model`.
Recall query is by **symptom text** with `filter: {embedding_model: <current>, stale: false}` (do not filter by root system — unknown at recall time); a hit is usable only if `system_version` == current version of the hit's root `system`. Store the exact query, filter, hits and scores on the incident (`recall`) — the viz shows it.
When Sentinel sees a system's `version` increase, B marks that system's playbooks `stale: true` (Gardener never writes playbooks).
### swarm.approvals (B creates; war room decides)
`{_id: "APR-0001", kind: "step"|"upgrade", incident, step_id, action_id, upgrade, requested_by, desc, risk, status: "pending"|"approved"|"denied", decided_by, ts, decided_at}`
Approval is requested and granted **before** the op is sent. Decisions update only `{status:"pending"}` docs.
### swarm.upgrade_requests (B Sentinel creates; D advances)
`{_id: "UPG-0001", system, weakness, fault_class, evidence: [incident ids], status: "requested"|"planning"|"testing"|"awaiting_approval"|"deploying"|"deployed"|"proven"|"rejected", diff, tests: {passed, output}, approval_id, version_before, version_after, prove: {fault, fired_at, window_s, incident_opened: bool}, ts}`
### swarm.checkpoints / checkpoint_writes — LangGraph `MongoDBSaver` (db `swarm`).
### swarm.stats — `{_id:"current", run_id, round}` (war room) and cached scorecard (optional). `{_id:"stream_tokens"}` for viz resume tokens.

## 2. Systems HTTP API (A → B). FastAPI app `systems.app:app` on :8701
- `GET /{sys}/health` → `{system, status, blames, version}` *(portal is HTTP 200 even when broken)*
- `GET /{sys}/metrics` → `{system, version, latency_ms, error_rate, queue_depth, disk_pct, last_batch_ok, batch_status, sessions}`
- `GET /{sys}/logs?since=<iso>&limit=50` → `{system, lines: [{ts, level, code, line}]}`
- `GET /{sys}/ops` → `[{op, params, destructive, desc, postcondition}]`
- `POST /{sys}/ops/{op}` body `{action_id, params}` → `{accepted, applied, message, action_id}`
  - Same `action_id` again → returns the stored receipt, does nothing new.
  - `lying_success` active (counter N in system_state): returns `{accepted:true, applied:true}`, does nothing, decrements N.
  - `slow_apply` active: returns `{accepted:true, applied:false}`; effect lands `SLOW_APPLY_S` later (persisted `apply_at` in op_receipts; tick loop applies).
- `GET /{sys}/state` → frozen read-back fields used for verification:
  - mainframe: `{batch: {job:"NIGHTBAT", status:"OK"|"ABENDED"|"RUNNING"|"HELD", poison_records:[ids], quarantined:[ids], last_run_ok}, credentials: {OPERATOR: "default"|"rotated"}, validation_enabled: bool, version}`
  - unix: `{spool: {path, disk_pct}, cron: {xfer_pull: "running"|"stopped"}, config: {"/etc/xfer.conf": "0666"|"0640"}, transfer: {last_file_batch_ts, status}, version}`
  - portal: `{sessions: n, search_patch: "none"|"parameterize_search", cache: {balances_as_of, stale: bool}, version}`
- `POST /{sys}/security/probe/{vuln_id}` → `{vuln_id, exploitable, detail}`
- `GET /{sys}/source` → current module source (viz/Gardener).
- Chaos hooks (C only): `POST /{sys}/_chaos/{fault}` `{params}` → `{ok, effect}`; `POST /_chaos/clear`.
- Admin: `POST /_admin/reset` (baseline: version 1, all up, vulns open/unexploited, faults & pending delayed ops cleared, receipts cleared) · `POST /_admin/reload/{sys}` (A re-imports `systems/<sys>.py`, bumps `world.systems.version`, returns `{version_before, version_after}`).
- 1-second tick loop evolves state, applies delayed ops, cascades health, writes logs/metrics into `world`.

### Ops catalog (postconditions are what the Guard checks via `/state` + `/health`)
| system | op | params | destructive | postcondition |
|---|---|---|---|---|
| mainframe | `quarantine_record` | `{record_id}` | no | id in `batch.quarantined`, not in `poison_records` (health may stay degraded) |
| mainframe | `restart_batch` | `{job:"NIGHTBAT"}` | **yes** | `batch.status=="OK"`, `last_run_ok` fresh (fails if poison remains) |
| mainframe | `rotate_credential` | `{account:"OPERATOR"}` | **yes** | `credentials.OPERATOR=="rotated"`, probe not exploitable |
| unix | `purge_spool` | `{path:"/var/spool/xfer"}` | no | `spool.disk_pct < 80` |
| unix | `restart_cron` | `{job:"xfer_pull"}` | no | `cron.xfer_pull=="running"` |
| unix | `chmod_config` | `{file:"/etc/xfer.conf", mode:"0640"}` | no | config mode 0640, probe not exploitable |
| unix | `rerun_transfer` | `{}` | no | `transfer.last_file_batch_ts` ≥ mainframe `last_run_ok` |
| portal | `flush_sessions` | `{}` | no | `sessions` below threshold |
| portal | `apply_patch` | `{patch:"parameterize_search"}` | no | `search_patch` set, probe not exploitable |
| portal | `refresh_cache` | `{}` | no | `cache.stale==false` (only possible once unix transfer is fresh) |

Full fix for batch_abort: `quarantine_record` → `restart_batch` (approval) → `rerun_transfer` → `refresh_cache`; then all three `/health` up.

## 3. Chaos (C)
- `chaos/faults.py` fault library (code only): `batch_abort`, `disk_fill`, `cron_stop`, `session_leak`, `lying_success` (params `{count:1}`), `slow_apply`, `sqli_exploit`, `config_exploit`, `agent_kill`.  Additive: `schema_drift` (W3), `false_log`, `credential_exploit` (Red fleet, §8).
- `chaos/runner.py`: `start_run(scenario, round) -> run_id`, `run_scenario(name, round=None)` (background-thread friendly), `fire(fault, target, params, manual=True, run_id=None)`. Every fault writes `world.chaos_events`.
- `chaos/scenarios.py: seed()` writes: `bank-night-1` (t=0 lying_success{count:1} on mainframe, t=1 batch_abort mainframe), `bank-night-2` (batch_abort mainframe — same class, no lying, for fair round-2 timing), `bank-night-3` (replay batch_abort after Gardener upgrade → no incident), `free-play` (empty; manual buttons).
- Agent kill: selector `{"agent": id}` or `{"role":"worker", "phase":"step_sent"}` (waits up to 30 s for a matching agent, then gives up: nobody is killed and the event records `result.how:"gave_up"`, `selector`, `waited_s`, error "kill skipped: …"; no fallback to another worker). Reads `swarm.agents.pid`, `os.kill(pid, SIGKILL)`. No warning to the swarm.
- CLI: `python -m chaos.cli run bank-night-1 | fire batch_abort mainframe | kill worker`.

## 4. Swarm (B) — processes, not threads
- `swarm/supervisor.py` spawns one OS process per agent (`python -m swarm.agent <role> <id>`), and re-spawns dead processes as fresh **nurse** standbys after `NURSE_RESPAWN_S` (generation+1).
- Roster: `queen-1` (+`queen-2` standby), `scout-1`, `scout-2`, `worker-mainframe-1`, `worker-unix-1`, `worker-web-1`, `guard-1`, `waggle-1`, `sentinel-1` (also the Undertaker reaper), `nurse-1`, `nurse-2`.
- Heartbeat `last_seen` every `HEARTBEAT_S`; leases renewed with heartbeats; lose your lease → stop acting.
- Undertaker (in sentinel): stale `last_seen` → `status:"dead"`, release its task/incident leases (set `lease_until` to past), waggle posts a handoff message. Nurse claims the expired **task** lease (generation+1), loads the LangGraph checkpoint (thread = task id), **reconciles any `sent` step against `/state` before retrying** (it may already be applied → hand to Guard; never re-send blindly), continues from last verified step. Record `handoffs[]` with `checkpoint_id` and skipped steps.
- Lifecycle: Detect (Sentinel polls `/health`; open/coalesce incident) → Claim (Scout lease on incident) → Recall (`$vectorSearch`) → Diagnose (worker tasks walk `blames`: portal→unix→mainframe reading logs/metrics/state; record `diagnosis_path`, `hops`) → Plan steps → Approve destructive steps first → Act (worker sends attempt; step `sent`) → Verify (**only guard-1 may set `verified`**, with evidence = read-back facts; rejects lies with kind `reject` thought "ACK ≠ APPLIED") → Resolve & learn (playbook embedded & saved) → Sentinel: same fault_class resolved ≥2 times → `upgrade_requests`.
- Round-2 speedup: a usable recall hit (score ≥ `RECALL_THRESHOLD`, non-stale, version match) lets the Scout skip the chain walk and plan the playbook's fix directly. Same pacing both rounds. Metrics separate `diagnosis_s`, `approval_wait_s`, `takeover_s`.
- Each role is a LangGraph graph compiled with `MongoDBSaver`. Deterministic policy decides *which* op runs; LLM (if key) only phrases thoughts.

## 5. Gardener (D)
- `python -m gardener.main` (heartbeats as role `gardener`, target `workshop`). Watches `upgrade_requests` with status `requested`.
- Plan: read `systems/mainframe.py` + incidents; diff from `gardener/remediations/batch_validation.py` (predetermined, honest; LLM may only write the explanation). < 50 lines, only inside `systems/`.
- Test: `pytest systems/tests/test_mainframe.py` + regression replay in an isolated DB (`WORLD_DB=world_test_gardener`).
- Gate: `approvals` doc `kind:"upgrade"` (Beekeeper approves in war room).
- Deploy: write file, `POST /_admin/reload/mainframe` (A bumps version). B's Sentinel stales old playbooks.
- Prove: `chaos.runner.fire("batch_abort","mainframe", manual=False)`; watch 30 s; no new incident with that system → `proven`.

## 6. War room (viz) — `warroom.server:app` on :8700 (serves `warroom/static/`)
- `GET /` single page, no build step, no CDN (vendor any lib into `static/vendor/`).
- `WS /ws`: open change streams **first** (`full_document="updateLookup"`), then send `{"type":"snapshot", ...}` (systems, agents, active+last 10 incidents, tasks, last 50 thoughts, last 20 chaos_events, pending approvals, upgrades, last 30 ledger, counts per collection, current run), then stream `{"type":"change", db, coll, op, key, doc, ts}` (deletes carry `key` only). JSON-encode BSON (datetimes → ISO, ObjectId → str). Persist resume tokens in `swarm.stats{_id:"stream_tokens"}`; resume after restart.
- Control: `POST /api/scenario/{name}` (starts run, sets `swarm.stats.current`) · `POST /api/chaos/{fault}` `{target, params}` · `POST /api/kill` `{selector}` · `POST /api/approvals/{id}` `{decision}` (pending-only) · `POST /api/reset` `{mode:"full"|"next_round"}` (`next_round` keeps playbooks + incident history) · `GET /api/scorecard` (aggregation pipeline: per round TTR, diagnosis_s, hops, attempts; chaos_events vs incidents) · `GET /api/atlas` (collection stats, indexes, sample docs) · `GET /api/doc/{db}/{coll}/{id}` (click-to-inspect).
- Layout: **left = Chaos fleet (Red)**, **center = legacy estate on the dependency chain with pipes**, **right = the Hive (Blue bees)**, **bottom = MongoDB spine** (honeycomb, one cell per collection, pulses on each change event, live counts). Thought/activity stream, incident step timeline, learning meter, Gardener workshop, Beekeeper approval modal.
- Visual beats: red projectiles hit servers; failure pulses travel downstream, blue diagnosis pulses upstream; Guard stamps **"ACK ≠ APPLIED"** on a lied step, then green when verified with evidence; killed bee greys out and drops its ticket, Nurse fetches the same ticket via the MongoDB checkpoint cell; recall fires a golden path from the `playbooks` cell to the Scout, skipping diagnosis stops; click anything → its real MongoDB document. Aggregate heartbeat noise.

## 7. Ports & processes
| process | command |
|---|---|
| MongoDB (docker `legacy-siege-mongo`) | `localhost:27027` |
| systems | `.venv/bin/python -m uvicorn systems.app:app --host 0.0.0.0 --port 8701` |
| war room | `.venv/bin/python -m uvicorn warroom.server:app --host 0.0.0.0 --port 8700` |
| swarm | `.venv/bin/python -m swarm.supervisor` |
| gardener | `.venv/bin/python -m gardener.main` |
| everything | `.venv/bin/python run.py [--reset]` |

## 8. Integrator notes (merged after the parallel build)
- `common.db.col()` routes `op_receipts` and `runs` to `world`; `tasks`/`checkpoints`/`checkpoint_writes` to `swarm`.
- `scripts/setup_db.py` adds: `op_receipts.pending_delayed_ops` (partial), `chaos_events{run_id,ts}`, `runs{ended_at,started_at}`,
  `tasks{status,specialty,ts}`, `incidents{"steps.status"}` (partial active), `incidents{run_id}`, `approvals{incident,step_id}`,
  `upgrade_requests{system,fault_class}` **unique** (`one_upgrade_per_weakness`), `playbooks{system,stale,system_version}`.
- `swarm.stats{_id:"roster_reset", at}`: written by the war room on every reset (`full` and `next_round`). The supervisor
  restores the original roster (killed workers come back in their own role, extra respawned nurses retire) so each night
  starts fully staffed. A `full` reset also calls `gardener.pipeline.revert("mainframe")` before wiping.
- Agents re-register if a reset wiped `swarm.agents` (heartbeat and `Agent.set`); the Gardener too.
- Recall confirm hop (round 2) narrates without the deliberate pause; `diagnosis_s` is measured before the final sentence in both paths.
- Nurses pick up a task whose specialty has no live worker after 0.5 s (was `NURSE_PICKUP_S`).
- Rehearsal: `python -m scripts.demo_check` (see README).
- **Schema drift — the attack (additive, W3).** Re-adds a small slice of the cut "schema drift":
  - Chaos: fault `schema_drift` (target `unix`, params `{to?}`): a "vendor patch" silently renames the balance field
    in the unix xfer manifest (`balance` → `acct_bal` → `ledger_bal` …). Scenario `bank-drift` (seed 1004) fires it
    at t=0. `SCHEMA_DRIFT_DEMO=1` (default off) appends it to `bank-night-3` at t=45 (after the prove window);
    keep it off for `scripts.demo_check` (night 3 must open no incident). Built-in scenarios are re-upserted from
    code when the stored copy differs.
  - Systems (additive fields only): `GET /unix/state` adds `feed: {name:"xfer_manifest", schema_version, sample:[{acct,
    <balance field>, as_of}]}`; `GET /portal/state` adds `feed: {map: {balance: <manifest field>}, errors}`;
    `GET /unix/view/manifest` (JSON). Portal op `map_field {field:"balance", source}` (non-destructive); `refresh_cache`
    also fails while the mapping doesn't match. A drifted feed makes the portal `degraded`, blames `unix`,
    `error_rate` 0.25; unix stays `up`. Full fix: `map_field` → `refresh_cache`.
  - War room: `schema_drift` in the fault launcher, with its own strike caption.
- **`swarm.incidents.ai` (optional, advisory) — shared shape for every "Claude proposes, verification disposes" feature.**
  Written by W2's second opinion (`kind:"second_opinion"`) and W3's schema-drift mapping (`kind:"schema_mapping"`);
  the war-room "AI" card renders either kind from these common fields:
  ```
  ai: {kind: "second_opinion"|"schema_mapping",
       status: "thinking"|"done"|"offline",          # optional; absent = done
       model, hypothesis: str, cites: [str],          # cites = ids of the evidence lines Claude relied on
       confidence: 0..1|null, latency_ms: int,
       proposed_ops: [{op, target, params: {}, risk: "safe"|"destructive", why}],
       verdicts:     [{op, target, verdict: "accepted"|"overruled", rule, reason}],   # aligned with proposed_ops by index
       evidence: [{id, src, text}]}                   # optional: the cited lines, so the card can show them
  ```
  `rule` names the guardrail that decided (e.g. `"P3 blast radius"`, `"P6 approval gate"`, or W3's read-back check);
  `reason` is one plain sentence. Kind-specific extras are allowed next to these fields (second_opinion adds
  `request_id, started_at, done_at, error, root_system, fault_class, root_agrees, policy{root_system, fault_class},
  missed_ops[{op, target, params}], tally{proposed, accepted, overruled}`). **No decision path reads `ai`.**
- **Second opinion details (W2, `swarm/second_opinion.py`).** Behind `AI_SECOND_OPINION=1` (default off) and a key.
  The Scout starts one background thread right after writing the plan (≤1 call per incident, ≤2 in flight per
  process, 20 s end-to-end budget, never awaited). Claude (`LLM_MODEL`, structured JSON) gets numbered evidence
  (symptoms, each hop's status/blames/facts, the last few `/logs` lines per system; never the policy's fault_class,
  root cause or plan) plus the ops catalog, and is asked for the fastest safe recovery. A deterministic judge
  checks each proposed op against the plan being executed and read-backs taken before the call; named rules:
  P1 catalog, P2 dependency order, P3 blast radius, P4 read-back, P5 scope, P6 approval gate (accepted, gated).
  Writes are conditional on `{_id, opened_at}` + `ai.request_id` (a reset can reuse an `INC-` id). Running tally
  in `swarm.stats{_id:"ai_tally", proposed, accepted, overruled, incidents}` (copied onto each `ai.tally`).
  No flag / no key / timeout / error → field absent or `status:"offline"`. `common.llm.complete_json()` is the
  structured-output sibling of `complete()` (no sampling params, thinking off, effort low, no retries).
  `scripts.demo_check --check-ai` adds an opt-in row (not one of the 26 beats; skipped without flag + key).
- **AI drives (W2, `AI_DRIVES=1`, default off; needs a key).** In the Scout's `plan` node (walk path only; a usable
  recall hit skips it, so Night 2 stays fast) `second_opinion.decide()` asks Claude synchronously; one deadline
  `AI_DRIVE_TIMEOUT_S` (6 s) covers evidence + Claude (≈7 s worst case with bookkeeping); late answers are discarded. It asks for ranked hypotheses (`ai.hypotheses[{system, fault_class, evidence, why}]`), the root
  system and a plan. If the root matches the walk's proven root AND every op is accepted by the judge AND no plan
  step is missing, Claude's plan is what runs (steps carry `source:"ai"`, `ai_why`); otherwise, or on timeout/error/
  schema mismatch, or when Claude left params to the policy, the rules plan runs (`source:"policy"`). With both
  switches off, steps carry no `source`/`ai_why` field (identical to before). `ai.drives:true`, `ai.decision: "ai_plan"|"rules_plan"`,
  `ai.decision_reason`. The Guard still verifies every step. The reasoning is posted as `thoughts` lines
  ("Claude H1 … [E8, E12]", "Claude proposes …", "Guardrail: …"). Both AI paths skip an incident whose `ai.kind` is
  another feature's (W3's `schema_mapping`), so neither clobbers the other.
- **LLM guardrails (`common/llm.py`, reusable).** `guarded_json(task, system, prompt, schema, max_tokens, timeout)
  → (dict|None, meta)`: hard wall-clock timeout, output validated against the JSON schema locally (mismatch →
  None), `max_tokens` capped (`LLM_MAX_TOKENS_CAP`, 1500), `UNTRUSTED_NOTE` appended to the system prompt,
  `redact()` strips env secrets / key shapes / URI credentials from everything sent, outcome counter
  (`guard_stats()`, `swarm.stats{_id:"llm_guard"}` `{<task>.<outcome>, all.calls}`). Wrap untrusted system or log
  text with `fence(source, text)` (control/bidi chars stripped, truncated, no tags). Provider seam
  `PROVIDERS[LLM_PROVIDER]` (only `anthropic` today; add e.g. `openrouter` with the same signature).
- **Schema drift — the adaptation (additive, W3, `swarm/schema.py`).**
  - New collections `swarm.schema_samples` (TTL 24h on `ts`; `_id:"good:unix:xfer_manifest"` = known-good sample the
    sentinel rewrites every 10 s while unix+portal are up and the sample matches the expected schema) and
    `swarm.schema_mappings` (`_id:"unix.xfer_manifest.balance"`, `{source, previous, version, history[], learned_from,
    verified_by, evidence, reused}`). `schema_samples` is wiped on full and next_round resets. `schema_mappings` is
    durable memory: next_round only *rewinds* the live mapping (`source` back to the field, `version:1`, the estate's
    feed is v1 again) and keeps `history[]` (every Guard-verified rename); only a full reset deletes it.
  - The unix diagnosis hop matches the manifest sample against the expected schema with a `$jsonSchema` query and
    diffs its fields against the good sample with an aggregation (`$objectToArray`/`$setDifference`/`$type`) →
    `fault_class:"schema_drift"`, plan `map_field` + `refresh_cache` on portal (`policy.rebind` uses today's names).
  - Only the Guard stores a mapping: after it verifies `map_field` by read-back (portal reads the new field, values
    line up with the good sample within 35%) it writes the mapping (max version+1, or the earlier version with
    `reused:true` when this exact change is in `history`), re-captures the good sample at once, and says
    "unix renamed X → Y · mapping learned vN" (or "· seen before: mapping vN reused"). The resolved incident's playbook (with embedding) is the memory that
    a repeat drift recalls via `$vectorSearch`. `schema_drift` is excluded from the sentinel's Gardener upgrade
    requests (the mapping is the adaptation).
  - Who proposes the mapping (`findings.params.proposed_by`): **memory** (`schema_mappings.history` holds a verified
    entry for this exact change, else a non-stale schema_drift playbook maps to one of the new fields: reused, no Claude
    call, after the same value check as Claude's answer; a memory that fails it is marked stale) → **claude** (key + `AI_SECOND_OPINION=1`: Claude names the field
    that means the same thing, `common.llm.complete`, `SCHEMA_AI_TIMEOUT_S` budget (6 s) in a side thread; the rules
    pre-check it, field exists + numeric + values line up, and overrule it otherwise) → **rules** (no key/flag, timeout,
    error, overruled: best type + value match). With the flag on, the worker writes `incident.ai` in the shared shape
    with `kind:"schema_mapping"` (+ `proposed_by`, `guard:{verdict, rule, reason, version}` stamped by the Guard after
    the read-back). The Guard narrates the chain, naming the real proposer: "rules: no op for unknown field acct_bal → Claude
    proposed balance→acct_bal → Guard verified (3/3 values match) → mapping v2 stored" / "memory: MongoDB already knew
    balance→acct_bal (no Claude call) → Guard verified (…) → mapping v2 reused". The sentinel says "unix feed schema
    changed: balance missing, acct_bal new" as soon as its periodic sample stops matching (notice only). W2: `second_opinion` should skip an
    incident that already has `ai.kind == "schema_mapping"`.
  - War room watches `swarm.schema_mappings` (caption + "MAPPING LEARNED" / "MAPPING REUSED" stamp on unix).
    Rehearsal: `python -m scripts.drift_check` (14 checks incl. the repeat-from-memory round; separate from demo_check).
- **Red fleet — AI attackers (additive, W14; opt-in `RED_FLEET=1`, default off).** Seeded scenarios are unchanged.
  - Chaos: `python -m chaos.redfleet` (started by `run.py` only when `RED_FLEET=1`) supervises five attacker processes
    (`--agent red-saboteur|red-assassin|red-liar|red-hacker|red-drifter`), each a LangGraph loop scout → plan → strike → observe.
    Claude picks one item from the persona's menu (library faults × the Hive's state); off-menu picks are rejected and a
    deterministic playbook decides. Every strike goes through `runner.fire(..., extra={"by": <persona id>, "red": {persona,
    planner, question, reason}})`: `fire()` gained an optional `extra` (attribution fields; core fields can't be overwritten),
    so `chaos_events` may carry `by`/`red`. New collection `world.red_agents` `{_id: persona id, name, motto, question, pid,
    status: "hunting"|"holding"|"striking"|"stopped", hold, last_seen, attacks, last_attack}` (C only). Red writes
    `swarm.thoughts` with `role:"red"`, `agent:<persona id>` (like any agent); it never writes other swarm collections.
  - New faults: `false_log` (any system, params `{blame?}`): one-shot misleading log lines blaming another system
    (never an exploit signature, never `S0C7`); nothing else changes. `credential_exploit` (mainframe): default OPERATOR
    logon logged as `... factory default password accepted` + a ledger tamper; resolved when the probe is no longer
    exploitable (`rotate_credential`); `/_admin/reset` restores the default credential.
  - Guardrails kept (no permanent damage): refuses `WORLD_DB=world`/`SWARM_DB=swarm` unless `RED_FLEET_ALLOW_LIVE=1`;
    holds while an `upgrade_requests` doc is mid-pipeline; never kills a `gardener` or the last live queen.
    Pacing knobs: `RED_FLEET_COOLDOWN_S` (30, per attacker), `RED_FLEET_MAX_ATTACKS` (default 2 per attacker per night; 0 = unlimited),
    `RED_FLEET_FIRST_STRIKE_S`, `RED_FLEET_SKIP_SCENARIOS`, `RED_FLEET_FAULTS`, `RED_FLEET_AGENTS`, `RED_FLEET_LLM=0`.
- **Dynamic swarm scaling (additive, W15; switch `SWARM_DYNAMIC=1`, default off = the fixed roster above, unchanged).**
  - Standing roster shrinks to `queen-1/2, guard-1, waggle-1, sentinel-1, nurse-1/2`; no standing scouts or workers.
  - `swarm.spawn_requests {_id:"SPW-0001", role:"scout"|"worker", specialty, incident, task, reason, requested_by,
    status:"pending"|"spawned"|"cancelled", agent_id, pid, warm, note, ts, spawned_at, spawn_ms}` (TTL 24 h). The Queen
    writes one per new incident when no idle scout is free; a Scout writes one per task when no idle worker of the task's
    `specialty` is free (any specialty string works: new incident types get `worker-<specialty>-<n>`). The supervisor
    polls pending requests, starts the agent from a pre-imported warm spare (`python -m swarm.agent --warm`, `SWARM_WARM`
    kept ready), assigns a spawned scout its incident (`assigned_to`), and cancels requests whose work was already taken.
    Hard ceiling `SWARM_MAX_AGENTS` (default 24 live agent processes; over it, requests wait and nurses cover the work).
  - Spawned agents carry `swarm.agents.spawned: true` and run with `--ephemeral`: once their incident is closed
    (`SWARM_RETIRE_AFTER_S`, 3 s) or after `SWARM_IDLE_S` (15 s) without work they set `phase:"retiring"`, re-check for
    waiting work, write a `kind:"retire"` thought and **delete their own agent doc** (the bee leaves the war room) and
    exit 0; the supervisor does not respawn them. A spawned bee that dies (SIGKILL) is handled like any bee: Undertaker,
    Nurse takeover from its checkpoint, a replacement nurse on the bench.
  - Requests are narrated as `kind:"spawn"` thoughts. Nurses leave work alone for `SWARM_SPAWN_GRACE_S` (8 s) while its
    spawn is under way. `swarm.stats{_id:"swarm_scale"}` = `{dynamic, live, spawned_live, peak, spawned, retired,
    cold_spawns, warm_ready, max, base, avg_spawn_ms}`, counters reset per night (roster reset).
- **Red v5 continuation (W14c; additive, default off).** Runner remains code with a vetted whitelist and executors;
  no Runner agent or Mongo fault catalog. Every chaos event has `source`, `who`, `why`, plus `outcome` with
  `guard_caught` / `recovery_s` (null means not observed), incident read-backs and a temporal-association caveat.
  Fleet OBSERVE refreshes recent outcomes; `CHAOS_OUTCOMES=1` enables bounded delayed sampling for seeded/manual
  attacks without the fleet. Strikers persist reflection/lesson in the event and read it into their next THINK.
  `world.red_agents` adds intent, bounded lessons, pending reflection, planner, llm_calls and fallback_count;
  thoughts explicitly label `policy (no reasoning)` and show the call/fallback counts.
  `CHAOS_AGENT_FREEZE=1` adds `agent_freeze` (`freeze_s`, max 60); PID-checked guardian thaws even after attacker
  death, and reset thaws records in `swarm.frozen_agents`. `CHAOS_AGENT_LIE=1` adds `lie_to_agent`: Runner calls
  the existing false_log hook with `{prompt_injection:true}`; the system also checks the switch and writes a fixed
  instruction-like log line, changing no actual state. Poison preparation is NOT in the whitelist: no Memory Striker
  until visible Guard rejection is proved. A full reset removes any `red_planted:true` playbooks/lessons (next-round keeps them, W27c).

- **Comms / Sentinel #2 (additive W17, `SWARM_COMMS=1`, default off).** `messages` retains its
  existing fields; opt-in writes add `priority` (higher first), `severity`, `source`, `incident`,
  `system`, `count`, `comms_processed`. Index `comms_priority_incident` orders priority/incident/time.
  The existing Waggle batches at most 256 changes or 0.5 s, ranks severity then pending human
  approval then system (portal/unix/mainframe), then duplicate count, and merges equivalent text with labelled counters/durations
  removed within the same incident/system/kind/severity. Original raw messages remain for audit;
  duplicates carry `merged_into` and their representative carries the total `count`. Consumers use
  `comms_processed:true` with no `merged_into` for the prioritised feed. A `kind:comms`
  thought says "Comms: N messages, M merged, top: …"; cumulative totals live in `stats:comms`.
  The existing Sentinel separately scans incident signatures and non-stale failure lessons. At 3+
  occurrences it upserts `gardener_notes {_id:signature, source, system, problem, count, evidence,
  text, ts, updated_at}` and emits a `gardener_note` message/thought once per signature. These are
  advisory notes, not upgrade requests; Gardener behavior is unchanged. Full reset clears notes.

- **Gardener help (additive W19, `GARDENER_HELP=1`, default off).** One collection, `gardener_requests
  {_id, kind: repeat|help (W20 adds tool), from, system, status: open→answering→answered, answer, ts}`, index
  `open_requests {status, ts}`, wiped on full reset. Answered by a thread inside `gardener.main` (`gardener/help.py`).
  *repeat* (`_id GR-<signature>`, Sentinel #2, 3+ occurrences, once per signature): the Gardener acks the
  `gardener_notes` row and, when a vetted remediation covers that system + fault class, files the upgrade request
  (or re-queues a rejected one); the unchanged pipeline (plan → sandbox tests + replay → approval → deploy → prove)
  runs it. The Sentinel's own UPGRADE_AFTER=2 trigger is unchanged. *help* (`_id GH-<step_id>`, a Worker whose step
  failed Guard verification twice): `{incident, step_id, op, params, attempts, evidence, fault_class}`; answer
  `{kind: playbook|lesson|runbook|upgrade|remediation|none, text, ref, advice: {params?, fast?}}`. The Worker waits
  `GARDENER_HELP_WAIT_S` (5 s), writes advice params onto the step (`params_before_help` kept) and records
  `steps.$.gardener_help`; the Guard verifies that attempt like any other.
  *Known-fix push*: the Sentinel sets `incidents.known_fix {from, source:"playbook", playbook_id, system,
  fault_class, system_version, learned_in, fix, to, ts, used?}` (once) on a claimed/diagnosing incident whose
  symptom lines match a current playbook, plus a `known_fix` message to the owning Scout. The Scout keeps its vector
  search (recall.used semantics unchanged); on a miss it uses the pushed playbook via the normal confirm hop.
  All three narrate as thoughts (kinds upgrade / handoff / action / recall).
- **Red Commander + 2 Scouts (W18; additive, `RED_COMMANDER=1`, default off, only with `RED_FLEET=1`).** Runs as a
  thread in `python -m chaos.redfleet` (or `--agent red-commander`). New `world.red_agents` entries: `red-commander`
  `{kind:"commander", status, planner, campaign?, lessons[≤8], last_reflection, llm_calls, fallback_count}` and
  `red-scout-hive` / `red-scout-systems` `{kind:"scout", findings, summary, run_id}`. Striker entries gain
  `order {campaign, run_id, combo, step, of, fault, target, victim?, blame?, to?, status:"queued"|"assigned"|"taken"|
  "done"|"failed", event?}`; with the switch off Strikers ignore it. Orders are executed only from the Striker's own
  menu + `validate` and `runner.fire`; `chaos_events.red` may carry `campaign {id, step, of, combo, ordered_by}` and
  `campaign_reflection`. Thoughts: `role:"red"`, agents `red-commander`, `red-scout-hive`, `red-scout-systems`.
  No new faults, no new collections, no swarm writes beyond thoughts.
- **Memory Striker (W18; additive, `CHAOS_POISON_MEMORY=1`, default off; supersedes "poison not in the whitelist").**
  New fault `poison_memory` (kind agent, target `swarm`, params `{playbook?}`) and persona `red-memory` (no process
  unless the switch is on). It copies a genuine non-stale learned playbook (never `schema_drift`) into
  `swarm.playbooks {_id:"RED-POISON:<id>", red_planted:true, red_source}` in one of two shapes (result `shape`):
  `wrong_system` (every fix step's `system` moved to the wrong system) or, for `batch_abort`, `wrong_record`
  (`quarantine_record` aimed at `TX000000`; W27). The Scout's recall-time provenance check (`recovery.vet_recall`)
  rejects a wrong-record memory whose fix differs from the steps the Guard verified in its source incident
  (thought "Rejected memory …", playbook stale, misfit lesson, `recall.memory_rejected` + `recall.rejected`) and
  restores the twin from the same incident that matches those steps, if no Hive decision retired it. It hides the genuine one (`stale:true, red_hidden_by`). It never edits the genuine fix. A full reset
  (`runner.cancel()` / setup_db) deletes `red_planted` docs and un-hides `red_hidden_by` docs; a next-round reset
  (`runner.cancel(keep_memory=True)`) keeps them, like every other playbook (W27c). The visible defence needs
  `SWARM_RECOVERY=1` + `FAILURE_MEMORY=1` (W16): Guard read-back rejects the recalled repair, retires it, re-plans.
  Red `outcome.incidents[].memory_rejected` = incident `recall.rejected`; a rejection counts as `guard_caught:true`.
  Commander combo `poison-then-break`: poison a learned repair, then the Saboteur/Hacker causes that fault again.
- **Comms live roster (additive W22, same switch `SWARM_COMMS=1`, default off; `swarm/roster.py`).** A thread in the
  Waggle process sweeps every `ROSTER_SWEEP_S` (1 s) and derives `swarm.roster` from state agents already write
  (`agents`, claimed `tasks` and active `incidents` leases, incident steps + `handoffs`, `spawn_requests`); no other
  role writes it and no role reads it. One doc per agent `{_id, role, specialty, state: idle|working|standby|dead|retired,
  incident, system, task, step: "<step_id> <op> (<status>)", phase, doing (one line), parent (task holder → the
  incident's lease holder; scout → assigning queen), spawned_by, took_over {from, work, ts, takeover_s},
  taken_over_by {to, work, ts}, pid, generation, last_seen, died_at, retired_at, updated_at}`. Only changed docs are
  written (heartbeat refresh at most every `HEARTBEAT_S`). Killed agents stay `dead` with who took their work;
  a deleted agent doc (dynamic retire) becomes `retired` for 60 s, then is removed. Full reset clears it.
  War room: snapshot `roster`, change stream `coll:"roster"`, `GET /api/roster` → `{enabled, counts, agents}`;
  compact card `warroom/static/js/roster.js` (`installRoster(cardEl)`, `rosterRows()`), hidden while empty.
- **Red Planner (W34; additive, `RED_PLANNER=1`, default off, only with `RED_COMMANDER=1` + `RED_FLEET=1`).** Off = the
  Commander is unchanged. On, before each Commander THINK the Planner (`chaos.redfleet.planner`, same thread):
  - spawns 2 one-shot scouts and waits for their reports: `world.red_agents` `red-scout-swarm` (studies the Blue Hive)
    and `red-scout-legacy` (studies the legacy estate) `{kind:"scout", status:"reported", spawned_by:"red-planner",
    planning_id, findings, run_id, spawns}`;
  - writes `red-planner {kind:"planner", status, brief:{planning_id, focus:guard|nurse|memory|intrusion|compound,
    priority:high|normal, prefer[combo ids], reason, planner:claude|rule, scouts}}`. Claude may only pick a feasible
    focus (else rules). The Commander gets the brief in its prompt and prefers those combos;
  - campaigns, Striker `order`s and `chaos_events.red.campaign` gain `planning_id, priority, focus`;
  - with `RUNNER_DECIDES=1` the campaign is handed to the Runner: one `chaos.decider.propose(fault, target, params,
    by="red-planner", campaign={id, step, of, combo, ordered_by, planning_id, priority, focus}, prefer=spawn|execute)`
    per step, no Striker orders; the Commander follows the requests (event = CHX with `red.request`). Runner off =
    Striker orders as before;
  - decision log (`chaos.decisions`): `{kind:"decision", by:"red-planner", decision:spawn|campaign_brief|propose|handoff,
    planning_id, ...}`. Thoughts from agent `red-planner`, role `red`.
- **Runner decisions (W35; additive, `RUNNER_DECIDES=1`, default off).** Off = `runner.fire` unchanged.
  - **Admission.** On, a fire with `extra.by` = `red-*` goes through `chaos.decider.admit`. A held or denied fire returns
    `{status:"hold"|"deny", result:{ok:false,error}}`, and no CHX event is written.
  - **Decision log.** Each decision is a `world.chaos_events` doc in W34's shared log (`chaos.decisions`):
    `{kind:"decision", by:"runner", decision:pick|execute|hold|deny|spawn, origin:striker|commander|planner|poison|seeded,
    choice:{fault,target,by,request}, caps, reason, event}`. Executed CHX events carry `runner_decision`.
  - **Board state** lives on `world.red_agents {_id:"runner", night, strikes, spawns, last_strike_at, requests[]}`.
  - **Seeded steps** are always executed and logged.
- **Workflow drift (additive W26, `WORKFLOW_DRIFT=1`, default off; Statement 3 Q3 "workflow").**
  - Systems: mainframe fault `workflow_drift` ("JES2 maintenance"). After it, an ABENDed NIGHTBAT is held:
    `restart_batch` returns `{accepted:false, message:"$HASP101 NIGHTBAT HELD - JCL HOLD: issue release_hold before
    restart_batch"}` (new `systems.base.OpRefused`: refused outright, the receipt keeps the message). New op
    `release_hold {job:"NIGHTBAT"}` (non-destructive, postcondition `state.workflow.hold == false`) exists only after
    the fault; `GET /mainframe/ops` is now built per state and then lists it with `before:"restart_batch"`, and
    `restart_batch` gains `requires:["release_hold"]`. `GET /mainframe/state` adds `workflow:{version, hold, requires}`
    only once drifted. Baseline catalog/state unchanged. `/_admin/reset` restores workflow v1.
  - Chaos: `workflow_drift` (target mainframe, switch-gated). With the switch on, `bank-night-1/2` get it at t=0 and
    scenario `bank-workflow` (seed 1005: drift + batch_abort) exists; off = seeded nights unchanged.
  - Swarm (`swarm/workflow.py`): the worker notices a refused step, reads the live `/ops`, rules R1-R4 decide
    (refusal names the op, catalog lists it as a prerequisite, non-destructive, params given; Claude may propose with
    key + `AI_SECOND_OPINION=1`, same rules accept/overrule) and inserts step `<step_id>p` (n = refused n - 0.5) into
    the same fenced task; `incident.workflow_drift {system, op, requires, params, message, refused, proposed_by, ...}`.
    After the Guard verifies the prerequisite and the retried op it writes `swarm.schema_mappings`
    `{_id:"mainframe.workflow.restart_batch", kind:"workflow", system, op, requires, params, version (v1 = original),
    history[], learned_from, verified_by, evidence, reused, uses}` (no `source`/`field`, so the round-reset rewind and
    the war-room mapping stamp skip it). The Scout's plan puts learned prerequisites up front (if live `/ops` still
    lists them); such steps carry `workflow:{before, memory, version}`. Thoughts: "workflow changed: restart_batch now
    needs release_hold first …", "workflow memory: … planned up front, no refusal".
  - Rehearsal: `scripts.demo_check --drift` adds a field-drift row set (drift_check logic) and, with
    `WORKFLOW_DRIFT=1` on the stack and the checker, rows w1 (learned night 1) and w2 (reused night 2).

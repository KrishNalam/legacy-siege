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
- `chaos/faults.py` fault library (code only): `batch_abort`, `disk_fill`, `cron_stop`, `session_leak`, `lying_success` (params `{count:1}`), `slow_apply`, `sqli_exploit`, `config_exploit`, `agent_kill`.
- `chaos/runner.py`: `start_run(scenario, round) -> run_id`, `run_scenario(name, round=None)` (background-thread friendly), `fire(fault, target, params, manual=True, run_id=None)`. Every fault writes `world.chaos_events`.
- `chaos/scenarios.py: seed()` writes: `bank-night-1` (t=0 lying_success{count:1} on mainframe, t=1 batch_abort mainframe), `bank-night-2` (batch_abort mainframe — same class, no lying, for fair round-2 timing), `bank-night-3` (replay batch_abort after Gardener upgrade → no incident), `free-play` (empty; manual buttons).
- Agent kill: selector `{"agent": id}` or `{"role":"worker", "phase":"step_sent"}` (waits up to 30 s for a matching agent, then kills any busy worker). Reads `swarm.agents.pid`, `os.kill(pid, SIGKILL)`. No warning to the swarm.
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

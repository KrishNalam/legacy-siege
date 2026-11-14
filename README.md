# Legacy Siege

**A self-healing ops team of AI agents keeps a simulated bank's legacy estate alive while a seeded Chaos
Monkey attacks the systems and the agents themselves. MongoDB is the spine: every piece of state lives
in MongoDB, and every process can be killed.**

Design doc: https://claude.ai/artifact/TYfnA6aTSG99oMb9t1nzpr

- **Red, the Fleet** (`chaos/`): a seeded Chaos Monkey. It fires faults at three interconnected legacy
  systems (an IBM-style mainframe batch, a Unix file-transfer box and a web portal) and sends a real
  `SIGKILL` to agent processes.
- **Blue, the Hive** (`swarm/`): 12 agent *processes*. These are the queen (with a standby), scouts,
  specialist workers, a guard, nurses, the waggle bus and a sentinel. Each one is a LangGraph graph
  checkpointed into MongoDB. They detect incidents, walk the dependency chain, fix, verify, learn and
  remember.
- **The Gardener** (`gardener/`): turns a fault that keeps coming back into a permanent code upgrade.
  It writes a diff, runs the tests and a replay, gets human approval, hot-deploys the patch and then
  proves it by replaying the attack.
- **The War Room** (`warroom/`): a live visualisation that only *watches* MongoDB through change
  streams. It holds no state, and every pixel comes from a change event. Views: `/diagram` (Chaos Monkey →
  Legacy ← Bees, Gardener below; click or 1-4 to zoom, V toggles Live / How it works), `/flow`, the classic
  page, and `/why` (use cases and how each Statement 3 question is answered). `WARROOM_VIEW=diagram|flow|classic`
  picks what `/` serves (default classic).

What the demo shows, all checked automatically by `scripts/demo_check.py`:

1. A **cascading failure** traced portal → unix → mainframe. The customer-facing portal still answers
   HTTP 200 with stale balances, and the mainframe's own `/health` says "up".
2. A **lying system** caught by the Guard bee. The op returns `applied:true` but did nothing. The guard
   reads the state back, stamps it **ACK ≠ APPLIED** and opens a new attempt.
3. An **agent kill** while a step is in flight. A nurse takes over the leased task from its MongoDB
   LangGraph checkpoint, reconciles against the real state and never re-applies a verified step.
4. **Round-2 speedup** through `$vectorSearch` recall. The hive recognises the symptoms, skips the
   chain walk and fixes the problem in 1 hop instead of 3.
5. The **Gardener** turns two identical incidents into a code upgrade. After that, night 3's identical
   attack is shrugged off and no incident opens.

## Architecture

```
                         ┌────────────────────────── MongoDB (Atlas / atlas-local 8.0) ───────────────────────────┐
                         │  world DB (Red writes)                         swarm DB (Blue writes)                  │
                         │  systems  system_state  system_logs(TTL)       incidents($jsonSchema, partial-unique)  │
                         │  op_receipts(_id=action_id)  runs              tasks(leases)  agents  ledger(unique)    │
                         │  chaos_scenarios  chaos_events  counters       playbooks($vectorSearch)  approvals      │
                         │                                                upgrade_requests  thoughts/messages(TTL)│
                         │                                                checkpoints/checkpoint_writes (LangGraph)│
                         └──────▲───────────────▲──────────────────────────────▲──────────────▲──────────▲────────┘
                                │ txns          │ events/runs                   │ leases, txns │ change    │ change
                                │               │                               │ checkpoints  │ streams   │ streams
 ┌──────────────────────┐  HTTP │      ┌────────┴────────┐  SIGKILL   ┌─────────┴──────────────┴───┐  ┌────┴──────────────┐
 │ systems.app :8701    │◄──────┼──────│ chaos (Red)     │──────────► │ swarm.supervisor            │  │ warroom.server    │
 │ mainframe→unix→portal│  hooks│      │ faults/runner   │            │  queen ×2  scout ×2         │  │ :8700  (FastAPI + │
 │ 1s tick, cascade,    │◄──────┘      └────────▲────────┘            │  worker mainframe/unix/web  │  │ Canvas2D page)    │
 │ lying / slow ops     │◄──── /health /logs /state /ops ───────────── │  guard  nurse ×2  sentinel  │  │ /ws snapshot +    │
 └──────────▲───────────┘                       │ /api/scenario,      │  waggle  (1 OS process each)│  │ live changes      │
            │ reload                            │ /api/kill            └─────────────────────────────┘  │ /api/* buttons    │
 ┌──────────┴───────────┐   upgrade_requests    │                                                     └───────────────────┘
 │ gardener.main        │◄──────────────────────┼──────────────── (swarm DB) ── approvals ◄── Beekeeper clicks Approve
 │ plan→test→gate→deploy│                       └───────────────── judge buttons in the war room
 └──────────────────────┘
```

Lifecycle of an incident: the **Sentinel** polls `/health` and opens or coalesces an incident. A
**Scout** claims it with a lease, runs **recall** (`$vectorSearch`) and then **diagnoses** by sending
workers hop by hop along `blames`. It **plans** the steps, asks the Beekeeper to **approve** destructive
ones *before* sending, and dispatches leased fix tasks to **workers**. Each worker **sends** an attempt.
Only **guard-1** may set `verified`, using read-back evidence. Once all steps are verified and the systems
are healthy, the scout **resolves** the incident and saves a **playbook** with an embedding. When the same
fault class has been resolved twice, the sentinel files an **upgrade request** for the Gardener.

## MongoDB features, and where they live

| feature | what it does here | where |
|---|---|---|
| Multi-document **transactions** | Each step transition fences the task lease, updates the incident step and upserts that attempt's ledger receipt, all in one transaction, so the timeline and the audit ledger can never disagree | `swarm/steps.py:18`, `swarm/steps.py:43`, `swarm/steps.py:86` |
| | Resolving an incident and inserting its playbook happen atomically | `swarm/roles/scout.py:365` |
| | The systems estate: each tick, op or chaos hook changes state, the summary, logs and the receipt together | `systems/estate.py:60` |
| | Gardener: the approval request plus the status change, and the deploy ledger row plus the status change | `gardener/pipeline.py:289`, `gardener/pipeline.py:402` |
| **findOneAndUpdate leases** with generation fencing | A task or incident is claimed atomically (`$inc generation`). Renewals and every write are conditional on `{owner, generation}`, so a killed-and-replaced worker can't write | `swarm/leases.py:36`, `swarm/leases.py:73` |
| Exactly-once ops | `op_receipts._id = action_id`. A retry hits a duplicate key and gets the stored receipt | `systems/estate.py:170`, `systems/estate.py:200` |
| Unique index `ledger.action_id` | One receipt per attempt | `scripts/setup_db.py:103` |
| **Partial unique index** (cascade coalescing) | One *active* incident per `correlation_key`. A second sentinel insert gets a duplicate key and merges with `$addToSet` instead | `scripts/setup_db.py:98`, `swarm/roles/sentinel.py:94-120` |
| **`$jsonSchema` validation** | Rejects unknown step statuses and any `verified` step without evidence (the Guard's read-back) | `scripts/setup_db.py:20` |
| **Atlas Vector Search** (`$vectorSearch`) | Playbook recall by symptom text with pre-filters `{embedding_model, stale:false}`. The exact query, filter, hits and scores are stored on the incident | `swarm/recall.py:12-23`; index `playbook_vec` at `scripts/setup_db.py:126` |
| **Change streams** | War room: one cluster-level stream with `fullDocument:"updateLookup"`, a server-side `$match`/`$$REMOVE` and a persisted resume token | `warroom/stream.py:59`, `warroom/stream.py:190` |
| | Guard wake-up | `swarm/roles/guard.py:140` |
| | Waggle bus (database-level, resumable) | `swarm/roles/waggle.py:20`, `:107` |
| | The Gardener waits for an approval decision and watches for new incidents during prove | `gardener/pipeline.py:312`, `:441` |
| | Gardener queue | `gardener/main.py:72` |
| **Aggregation pipelines** | Orphaned work: `$lookup` into agents (nurse) | `swarm/roles/nurse.py:31` |
| | Recurring faults: `$group` + `$lookup` into upgrade_requests (sentinel) | `swarm/roles/sentinel.py:204` |
| | `$unwind` to find sent steps (guard) | `swarm/roles/guard.py:144` |
| | The scorecard per round | `warroom/api.py:175` |
| | Chaos scorecard | `chaos/score.py:73` |
| | Gardener evidence | `gardener/pipeline.py:188` |
| **TTL indexes** | Logs, thoughts and messages expire after 24h | `scripts/setup_db.py:87`, `:107`, `:110` |
| Partial index for delayed (slow_apply) ops | The tick claims due ops with findOneAndUpdate | `scripts/setup_db.py:94`, `systems/estate.py:156` |
| **LangGraph `MongoDBSaver`** | Every role is a StateGraph checkpointed in `swarm.checkpoints`. The thread id is the *task* id, so the checkpoint follows the work, not the process | `swarm/harness.py:27`; nurse resume `swarm/roles/nurse.py:68` |
| Atomic counters | `INC-0001`, `RUN-0001`, `CHX-0001` | `common/db.py:47`, `chaos/runner.py:42` |
| Conditional update | An approval decision lands only on `{status:"pending"}`, so double clicks are no-ops | `warroom/api.py:114` |
| **Change-stream pre-images** (`MONGO_SPINE=1`) | `changeStreamPreAndPostImages` on tasks/incidents/approvals; a resumable watcher with `fullDocumentBeforeChange` turns every claim, nurse takeover, release and Beekeeper decision into a sha256-chained `swarm.custody` row (before → after owner/generation, lease expired or not). Tamper-evident: `custody.verify_chain()` | `swarm/custody.py`, `scripts/setup_db.py` (`ensure_spine`) |
| **`$facet` + `$setWindowFields` + `$percentile`** (`MONGO_SPINE=1`) | `/api/spine`: the learning curve per fault class (`$documentNumber` night, `$shift` previous TTR, speedup), p50/p90 TTR per round, median takeover time, custody summary, all computed in MongoDB; shown in the Atlas overlay | `warroom/spine.py` |
| More `$jsonSchema` (`MONGO_SPINE=1`) | tasks (fencing token is an int, lease a date, status enum), approvals (a decision must carry `decided_by`/`decided_at`), upgrade_requests, playbooks (embedding exactly `EMBED_DIM` long, or the vector index would silently skip it) | `scripts/setup_db.py` |

## Run it (one command)

```bash
.venv/bin/python run.py --reset
```

This starts the systems (:8701), the war room (:8700), the swarm supervisor (12 agents) and the
Gardener. `run.py` creates the collections, validators and indexes, including the vector index, before
anything else starts. Logs go to `logs/*.log`. Open **http://100.105.248.23:8700** (or
`http://<host>:8700`). `PACE=0.3 .venv/bin/python run.py --reset` runs the demo about 3× faster, which
is useful while rehearsing. Stop the stack with Ctrl-C or `kill -TERM <run.py pid>`.

First-time setup: run `uv venv .venv && uv pip install -r deploy/requirements.txt`. For local MongoDB,
run `docker run -d --name legacy-siege-mongo -p 27027:27017 mongodb/mongodb-atlas-local:8.0`.

### Point it at MongoDB Atlas

Set `MONGODB_URI` in `.env` (or in the environment):

```
MONGODB_URI=mongodb+srv://USER:PASS@cluster.xxxxx.mongodb.net/?retryWrites=true&w=majority
```

Any tier works, including M0, which has Vector Search. Allow your IP in Atlas Network Access.
`.venv/bin/python -m scripts.atlas_check` verifies transactions, change streams, TTL, partial unique
indexes, `$jsonSchema` and a vector index round-trip on a throwaway DB. `deploy/README.md` covers
Docker, Fly and the other hosting options. The app reads only `MONGODB_URI`; other variables such as
`ATLAS_MONGODB_URI` are ignored.

### Keys (`.env`, all optional)

| var | effect without it |
|---|---|
| `ANTHROPIC_API_KEY` (+ `LLM_MODEL`) | No **AI second opinion** card (also needs `AI_SECOND_OPINION=1`) (Claude's independent diagnosis + op plan per incident, judged op-by-op by the deterministic policy and read-backs, `swarm/second_opinion.py`); agents narrate with fixed sentences and the Gardener writes a template explanation |
| `MONGO_SPINE=1` | Off by default. Adds change-stream pre-images + the lease chain of custody, four more `$jsonSchema` validators and the spine analytics card in the Atlas overlay (needs MongoDB 7.0+; run `scripts.setup_db` once with it on) |
| `AI_DRIVES=1` (+ key) | Off by default. When on, Claude reasons over the evidence at plan time and **its plan runs** if it passes every guardrail; otherwise (or when `AI_DRIVE_TIMEOUT_S`, 6 s for evidence + Claude, runs out; ≈7 s worst case with bookkeeping) the rules plan runs and the card says why |
| `VOYAGE_API_KEY` (+ `VOYAGE_MODEL`) | Embeddings use a deterministic local hashing model (`local-hash-v1`, 1024-d). The same vector index works either way |
| `PACE` (default 1.0) | Multiplier on the deliberate "thinking" delays (0 in tests) |

The entire demo runs with **no keys**. The policy that decides which op runs is deterministic.

## The 3-minute demo, mapped to the war room buttons

| time | click | what the audience sees | the MongoDB story |
|---|---|---|---|
| 0:00 | **Reset ▸ Full** | All green, the hive idle, the honeycomb empty | `setup_db.reset`, Gardener revert, roster restored |
| 0:10 | **▶ Night 1** | Red lightning hits the mainframe. The tape reels stop (ABEND), failure pulses run downstream, unix and then portal turn amber, and the portal shows a STALE stamp while still serving HTTP 200 | `chaos_events` insert → `world.systems` updates stream in |
| 0:25 | (watch) | A single incident card (coalesced). A blue diagnosis pulse walks portal → unix → mainframe with hop badges and a root-cause reticle | Partial unique index; `tasks` leases; `diagnosis_path` |
| 0:35 | (watch) | **AI SECOND OPINION** card: Claude's hypothesis and proposed ops, the evidence lines it cites and its confidence; each op marked ✓ (accepted), ✗ + named rule + reason (overruled, e.g. P3 blast radius) or + (policy adds); a running accepted/overruled tally. Nothing Claude says runs unchecked | `incidents.ai` written by a background thread; same change stream |
| 0:45 | (watch) | **ACK ≠ APPLIED** is stamped on `quarantine_record`; the guard opens attempt a2, and it turns green with evidence | `op_receipts.lied`; `ledger` a1 failed, a2 verified; `$jsonSchema` evidence rule |
| 0:55 | **Approve** in the Beekeeper modal | restart_batch waits for you, *before* anything is sent | `approvals` pending → approved |
| 1:00 | **✕ Kill a worker** (glows while a worker is in `step_sent`) | The worker bee falls and drops its ticket; a nurse flies over, picks it up, and a pink beam from the checkpoints cell reads "resumed from checkpoint …" | Real SIGKILL; Undertaker; lease `generation+1`; LangGraph checkpoint loaded from MongoDB; `handoffs[]` |
| 1:25 | (watch) | All green. A honey cell fills (playbook saved) | Resolve and playbook insert in one transaction, with an embedding |
| 1:35 | **Reset ▸ Next round**, then **▶ Night 2** | A golden beam runs from the playbooks cell to the scout: "Recall hit … skipping the chain walk". 1 hop instead of 3; the Learning chart shows round 1 vs round 2 | `$vectorSearch` with the stored query, filter and score; scorecard aggregation |
| 2:05 | **Approve** (restart_batch), then **Approve** (upgrade) | The Gardener card shows the diff, tests ✓ and replay ✓; v1 → v2 is deployed and old honey greys out (stale) | `upgrade_requests` state machine, change stream on approvals, stale playbooks |
| 2:30 | (watch) / **↻ Night 3 replay** | The same attack hits the mainframe and nothing happens: the record is rejected, no incident opens, the upgrade is "proven" | `prove.incident_opened:false` |
| 2:50 | **Atlas** | Every collection with its indexes (unique / partial / TTL / vector), validators and a real document; click anything to see its document | `GET /api/atlas`, `GET /api/doc/...` |

### Demo pace (stage run, opt-in)

Every knob below defaults to the old behaviour; put these lines in `.env` for the judged run, then
`box serve restart legacy-siege`:

```
PACE=0.1                 # deliberate "thinking" pauses (latency already paces the screen)
STEP_HOLD_S=0.6          # keep the agent-kill window open even with a tiny PACE
PROVE_WINDOW_S=12        # Gardener prove window (default 30; unpatched code opens an incident in ~6-15 s)
NIGHT3_WATCH_S=12        # demo_check's night-3 watch (default 25)
DEAD_AFTER_S=7           # Undertaker: >2 missed heartbeats (HEARTBEAT_S=3); task leases stay LEASE_S=10
SYSTEMS_TICK_FIXED=1     # 1 s tick on a fixed cadence (default sleeps 1 s AFTER a ~2 s remote tick)
CASCADE_TICKS=2          # staleness spreads one hop per 2 ticks instead of 3
DEMO_AUTOPLAY=1          # 3 clicks: Night 1, the first Approve, Kill (see below)
```

**Autoplay** (`DEMO_AUTOPLAY=1`, war room): Night 2 starts by itself `AUTOPLAY_GAP_S` (4 s) after Night 1
is green (with the next-round reset), and Night 3 after Night 2 is green *and* the upgrade is proven.
The first approval (Night 1's `restart_batch`) stays a manual click; every later approval auto-approves
after `AUTOPLAY_APPROVE_S` (3 s) with a visible "auto-approved (same as last time)" line
(`decided_by: autoplay`). The kill stays a manual press. `demo_check` follows along on its own when
`DEMO_AUTOPLAY=1` is set (or pass `--autoplay`).

Measured with `scripts.demo_check` (26/26 every run), time from the Night 1 click to the end of
Night 3. "RTT" is the MongoDB round trip, emulated with a TCP delay proxy in front of local MongoDB
(the Atlas Sandbox from this box is ~90 ms):

| settings | RTT | Night 1 | Night 2 | Gardener | Night 3 | Night 1 → end of Night 3 |
|---|---|---|---|---|---|---|
| defaults, PACE 0.6 | 0 ms | 49.6 s | 27.1 s | 40 s | 25 s | ~143 s |
| demo pace (PACE 0.3, prove 12, watch 12) | 0 ms | 35.7 s | 20.2 s | 22 s | 12 s | ~95 s |
| demo pace + autoplay | 0 ms | 35.9 s | 22.2 s | 22 s | 12 s | ~100 s |
| demo pace (PACE 0.3) | 94 ms | 106.5 s | 73.4 s | 28 s | 13 s | ~233 s |
| full stage block above | 94 ms | 103.7 s | 70.7 s | 40 s | 14 s | ~243 s |
| full stage block above | 20 ms | 38.7 s | 24.5 s | 22 s | 12 s | **~108 s** |
| Atlas Sandbox, defaults, PACE 0.6 (W4) | ~90 ms | 119 s | 69 s | | | 305 s whole check |

At ~90 ms RTT the run is bound by MongoDB round trips (each swarm step is ~20-30 of them: LangGraph
checkpoints, transactions, leases), not by pauses, so these settings alone cannot fit 3 minutes. Cut
the RTT: run the stack in the same region as the cluster, or put the cluster in a region close to the
box.

The fault launcher (top right) fires any single fault by hand, e.g. `lying_success`, `slow_apply`,
`disk_fill`, `cron_stop`, `session_leak`, or the exploits `sqli_exploit` / `config_exploit`, which open
a security incident. `schema_drift` (on unix) is the "the layout changed underneath us" beat: a vendor
patch silently renames the manifest's `balance` field to `acct_bal`. Unix stays green, but the portal can no
longer read balances. The Hive diffs the new sample against a known-good one in MongoDB (`$jsonSchema` +
a field-diff aggregation), proposes `balance → acct_bal`, and the Guard verifies it by read-back (the values line
up). The mapping is then stored in `swarm.schema_mappings` (v2) for every agent to use. With a key and
`AI_SECOND_OPINION=1`, Claude proposes the mapping (renaming is a question of meaning) and the rules pre-check it.
Without a key, a type + value match does the job. Fire it again and the next rename is learned as v3. Learned mappings
survive a round reset (only a full reset clears them), so when the same rename comes back in a later night the Hive
says "seen before: mapping v2 reused": the mapping comes from MongoDB memory with no Claude call, and
`$vectorSearch` recall makes it a single hop. The scenario `bank-drift` plays it, and `python -m scripts.drift_check` rehearses it
(14 checks).

### Red fleet (AI attackers, opt-in)

`RED_FLEET=1` makes `run.py` also start `python -m chaos.redfleet`: five AI attackers that play on top of the
seeded nights (which stay exactly as they are). Each is its own process running its own LangGraph loop
(scout → plan → strike → observe), aimed at "Finish the Job":

| attacker | strikes with | Statement 3 angle |
|---|---|---|
| `red-saboteur` | `batch_abort`, `disk_fill`, `cron_stop`, `session_leak`, timed to break the system the Hive is about to fix next | systems won't hold still mid-job |
| `red-assassin` | `agent_kill`: the worker mid-send, the crowned queen, a scout, the guard, the sentinel | Q2 resume after a crash |
| `red-liar` | `lying_success`, `slow_apply`, `false_log` (planted log lines and operator notes blaming the wrong system) | Q1 took effect vs only sent; knowing what actually happened |
| `red-hacker` | `credential_exploit`, `sqli_exploit`, `config_exploit` while the vuln is still open | an intruder mid-job |
| `red-drifter` | `schema_drift` (renames the balance field) | Q3 adapt when fields change |

Each attacker's Red Scout step reads the Hive's state from MongoDB (open incidents and their pending steps, live
agents and phases, systems, learned playbooks, what the rest of the fleet already did). Claude (`LLM_MODEL` via
`common/llm.py`) picks ONE item from that attacker's menu; the menu is built from the existing fault library, so an
off-menu pick is rejected (shown in the thought feed) and the attacker's own deterministic playbook decides instead,
which is also what runs with no key, on a timeout or on bad JSON. Every strike goes through `chaos.runner.fire`, so it
is an ordinary `chaos_events` document signed `by: "red-<name>"`; the war room captions it as "Red Liar: …". Their
reasoning shows in the thought feed (role `red`), and each attacker heartbeats into `world.red_agents`.

No caps by default (Krish's call); `RED_FLEET_COOLDOWN_S` (30 s per attacker) keeps the war room readable and
`RED_FLEET_MAX_ATTACKS`, `RED_FLEET_FIRST_STRIKE_S`, `RED_FLEET_SKIP_SCENARIOS`, `RED_FLEET_FAULTS`, `RED_FLEET_AGENTS`
and `RED_FLEET_LLM=0` tune it. Guardrails that stay (no permanent damage): it refuses the live `world`/`swarm` DBs
unless `RED_FLEET_ALLOW_LIVE=1`, holds while the Gardener is mid-upgrade, and never kills the Gardener or the last
live queen. Reset ▸ Full restores everything. Off by default: with `RED_FLEET` unset nothing changes; keep it off for
`scripts.demo_check` (it times the seeded beats).

**Red Commander + 2 Scouts (`RED_COMMANDER=1`, off by default, needs `RED_FLEET=1`).** A thread inside the fleet
supervisor (no extra process). Two cheap Scouts (`red-scout-hive`: busy / mid-send workers, free nurses, where the next
fix steps land; `red-scout-systems`: weak systems, open vulns, which systems already lie) post findings on
`world.red_agents`. The Commander (OBSERVE → THINK → ACT → REFLECT; Claude, or a labelled `policy (no reasoning)`
fallback) picks ONE campaign from the vetted combos feasible right now (`lie-then-freeze`, `lie-then-kill`,
`false-log-then-kill`, `break-then-drift`, `kill-then-exploit`, `poison-then-break`), assigns each step to a Striker on the board, and the
Striker executes it from its own menu through `runner.fire` (event `red.campaign = {id, step, of, combo}`). After the
last step it watches the Hive (`RED_COMMANDER_OBSERVE_S`, 45 s) and reflects (Guard catch, Nurse handoffs, recovery
time) into `chaos_events.red.campaign_reflection` and its lessons, which feed its next THINK. Knobs:
`RED_COMMANDER_COOLDOWN_S` (45), `RED_COMMANDER_FIRST_S` (20), `RED_COMMANDER_STEP_GAP_S` (3),
`RED_COMMANDER_STEP_TIMEOUT_S` (90, an unexecuted step calls the campaign off), `RED_COMMANDER_MAX` (0 = unlimited).

**Memory Striker (`CHAOS_POISON_MEMORY=1`, off by default).** `red-memory` plants a copy of a learned repair aimed at
the wrong system (a full unix spool → "purge_spool on mainframe") and hides the genuine playbook until the next reset.
With W16's defence on (`SWARM_RECOVERY=1 FAILURE_MEMORY=1`) the next recall picks it, the Guard's read-back rejects it
("Rejected memory RED-POISON:PB-0001 … retiring it"), and the Hive re-plans and learns a clean playbook. Batch-abort and
schema-drift repairs are never poisoned (the Hive re-derives those from today's facts anyway).

**Runner decisions (`RUNNER_DECIDES=1`, off by default).** The Runner stops being a dumb door. Every Red fire
(Strikers, Commander steps, Planner requests) is admitted first: `execute`, `hold` (nothing fired, retry later) or
`deny`. The stage caps keep autoplay moving:
- a night budget (`RUNNER_MAX_PER_NIGHT`, 3) and spacing between strikes (`RUNNER_GAP_S`, 20);
- no system fault while an incident is open;
- agent faults only hit workers;
- demo nights (`RUNNER_PROTECTED_SCENARIOS`, bank-night-1/2) take agent attacks only;
- `RUNNER_QUIET_SCENARIOS` (bank-night-3) takes no Red at all;
- no strikes mid-upgrade, or during autoplay's green gap.

With `RED_FLEET=1` a Runner loop also picks ONE candidate per tick: a queued Planner request
(`chaos.decider.propose`), a memory poison of what the Hive just learned, or a replay of tonight's seeded attack.
It then spawns the owning Striker (agent-victim faults, `RUNNER_MAX_SPAWNS` 1/night) or executes the fault directly.
Every decision is a `kind:"decision", by:"runner"` doc in `world.chaos_events` plus a `runner` feed line.
`RUNNER_LLM=1` lets Claude pick among the vetted candidates.

## Rehearse: `scripts/demo_check.py`

With the stack running:

```bash
.venv/bin/python -m scripts.demo_check            # full reset → nights 1-3 + Gardener → full reset
.venv/bin/python -m scripts.demo_check --keep     # leave the upgraded estate on screen at the end
```

The script drives the same API the buttons use. It auto-approves after `--approve-delay` seconds
(default 1.5) and fires `POST /api/kill {role:worker, phase:step_sent}` right after restart_batch is
approved. It then asserts every beat straight from MongoDB:

- **a.** The cascade (portal blames unix, unix blames mainframe, mainframe up but batch ABENDED), one
  coalesced incident, and diagnosis `portal→unix→mainframe` / `mainframe` / `batch_abort`.
- **b.** A guard-1 `reject` thought. Attempt a1 shows `applied:true` with ledger status failed, and the
  final attempt is verified with evidence. The ledger and incident steps stay in sync, with unique
  action_ids.
- **c.** The approval doc was created and decided before the op's ledger receipt, and the ledger
  records `approver: beekeeper`.
- **d.**
  - The process is really gone.
  - The Undertaker marks it dead within `LEASE_S + 2·HEARTBEAT_S`.
  - A nurse owns the task at generation 2, using a checkpoint that exists in `swarm.checkpoints`.
  - Verified steps are not re-sent, and the killed op hit the estate once.
  - A standby nurse is respawned.
- **e.** The incident resolves, all systems are up, and a playbook is saved with an embedding (1024
  dims), `embedding_model` and `system_version`.
- **f.** next_round reset, then night 2: `recall.used` with score ≥ 0.70, path `[mainframe]`, fewer hops,
  lower `diagnosis_s`, and the scorecard compares round 1 and round 2.
- **g.**
  - The upgrade request is filed and the Gardener tests it (pytest + replay).
  - The approval is given, `mainframe.py` is patched and the mainframe moves to v2.
  - v1 playbooks are marked stale, and the upgrade reaches `proven`.
  - Night 3 opens no incident.
  - The final full reset restores the legacy `mainframe.py`.

It prints a PASS/FAIL table with timings and exits non-zero on any failure. A full run takes about 3
minutes at `PACE=1` and about 2.3 minutes at `PACE=0.3`.

## Tests

Each workstream has its own suite on isolated databases (`world_test_*` / `swarm_test_*`):

```bash
PACE=0 .venv/bin/python -m pytest systems/tests chaos/tests gardener/tests warroom/tests -q
PACE=0 .venv/bin/python -m pytest swarm/tests -q        # real agent processes, ~90 s
```

## MongoDB MCP server (dev tool, read-only, opt-in)

`.mcp.json` registers the official [MongoDB MCP server](https://www.mongodb.com/docs/mcp-server/get-started/)
(pinned `mongodb-mcp-server@3.2.2`) in **read-only** mode for AI coding assistants (Claude Code asks before
enabling a project server, so it is off until you approve it). It points at local Mongo unless you export
`MDB_MCP_CONNECTION_STRING`. With it, a coding agent can inspect live collections, schemas, indexes and
`explain` plans; it exposes no insert/update/delete/drop tools. It is not part of the swarm's runtime: the
agents talk to Atlas through PyMongo directly.

## Layout

```
common/      config, db (client/world/swarm/col/next_id), embed (voyage | local-hash), llm, events (think/say)
systems/     the simulated estate: app.py (FastAPI), estate.py (tick engine, txns), mainframe/unix/portal.py
swarm/       supervisor, agent harness, leases, steps (txns), recall ($vectorSearch), verify, roles/*
chaos/       faults, scenarios (seeded), runner, score, cli (`python -m chaos.cli run bank-night-1`)
gardener/    main (watcher), pipeline (plan/test/gate/deploy/prove/revert), patcher, replay, remediations/
warroom/     server (FastAPI + /ws), stream (change streams), api (buttons), static/ (vanilla JS, Canvas2D)
scripts/     setup_db (collections, validators, indexes, vector index), atlas_check, demo_check
CONTRACTS.md the frozen interface between workstreams    docs/design-doc.txt  the original design
```

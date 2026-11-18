# Configuration reference

All settings are environment variables. They are read from the shell or from a `.env` file at the repo root ([`.env.example`](../.env.example) is a starting point).
Defaults are the values in the code (`common/config.py`, `swarm/settings.py` and the module named in each table).
Switches are off unless set to `1`, `true`, `yes` or `on`. The one exception is `RED_FLEET_ALLOW_LIVE`, which must be exactly `1`.

Restart `run.py` after changing anything.

## Core

| Variable | Default | What it does |
|---|---|---|
| `MONGODB_URI` | `mongodb://localhost:27027/?directConnection=true` | MongoDB with transactions, change streams and Atlas Vector Search. |
| `WORLD_DB` | `world` | The estate and chaos database ("Red"). |
| `SWARM_DB` | `swarm` | The agents' database ("Blue"). |
| `SYSTEMS_PORT` | `8701` | Legacy systems API port. |
| `WARROOM_PORT` | `8700` | War room port. `run.py` also accepts `PORT` if `WARROOM_PORT` is unset. |
| `SYSTEMS_URL` | `http://127.0.0.1:$SYSTEMS_PORT` | Where agents and the Gardener reach the systems API. |
| `WARROOM_URL` | `http://127.0.0.1:$WARROOM_PORT` | Where the rehearsal scripts reach the war room. |
| `PACE` | `1.0` | Multiplier on deliberate "thinking" pauses. `0` in tests. |
| `WARROOM_VIEW` | `classic` | What `/` serves: `classic`, `diagram` or `flow`. |

## AI (all optional)

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | Enables Claude. Without it, every LLM call returns nothing and the rules path runs. |
| `LLM_MODEL` | `claude-haiku-5-5` | Model id. |
| `LLM_TIMEOUT_S` | `8` | Timeout per LLM call. |
| `LLM_MAX_TOKENS_CAP` | `1500` | Hard cap on output tokens for structured calls. |
| `AI_SECOND_OPINION` | off | Claude reviews each plan in the background; a rules judge accepts or overrules each step (advisory, shown in the war room). Also lets Claude propose schema-drift mappings and new workflow steps, which the rules check before use. |
| `AI_DRIVES` | off | Claude plans at plan time; its plan runs only if every step passes the guardrails. |
| `AI_DRIVE_TIMEOUT_S` | `6.0` | Deadline for the `AI_DRIVES` call. |
| `VOYAGE_API_KEY` | none | Use Voyage embeddings for playbooks. Without it, a local hash embedding is used. |
| `VOYAGE_MODEL` | `voyage-4` | Voyage model id. |
| `EMBED_DIM` | `1024` | Vector size. The vector index is created once with this size; changing it later needs a new index. |
| `EMBED_TIMEOUT_S` / `EMBED_COOLDOWN_S` | `4` / `30` | Voyage timeout, and how long to skip Voyage after a failure. |

## Timing

| Variable | Default | What it does |
|---|---|---|
| `HEARTBEAT_S` | `3` | Agent heartbeat and lease renewal interval. |
| `LEASE_S` | `10` | Lease length. An agent silent this long is marked dead (`DEAD_AFTER_S` defaults to it). |
| `NURSE_RESPAWN_S` | `15` | Delay before the supervisor replaces a dead agent. |
| `RECALL_THRESHOLD` | `0.70` | Minimum vector-search score for a playbook to be used. |
| `UPGRADE_AFTER` | `2` | Distinct nights with the same fault before the Sentinel asks the Gardener for an upgrade. |
| `PROVE_WINDOW_S` | `30` | How long the Gardener watches for incidents after replaying the fault. |
| `SLOW_APPLY_S` | `20` | Delay used by the `slow_apply` fault. |
| `STEP_HOLD_S` | `1.2 × PACE` | Pause between marking a step sent and sending it (the window for "Kill a worker"). |
| `SYSTEMS_TICK_S` | `1.0` | Simulation tick. `0` disables the background tick (tests). |
| `SYSTEMS_TICK_FIXED` | off | Tick on a fixed cadence (helps against a remote cluster). |
| `BATCH_EVERY_TICKS` / `CASCADE_TICKS` | `2` / `3` | Mainframe batch cadence / how long downstream tolerates stale data. |

More swarm knobs live in `swarm/settings.py` (`SENTINEL_POLL_S`, `GUARD_GRACE_S`, `TASK_TIMEOUT_S`, `MAX_ATTEMPTS`, …).

## Demo

| Variable | Default | What it does |
|---|---|---|
| `DEMO_AUTOPLAY` | off | Night 1 → 2 → 3 advance by themselves. Every approval after the first is automatic. The kill stays manual. |
| `AUTOPLAY_GAP_S` | `4` | Pause after a night goes green. |
| `AUTOPLAY_APPROVE_S` | `3` | How long an approval stays on screen before autoplay approves it. |
| `SCHEMA_DRIFT_DEMO` | off | Adds a unix field rename to Night 3 at t=45 s. Keep it off for `scripts.demo_check`. |
| `WORKFLOW_DRIFT` | off | Adds a mainframe workflow change to Nights 1 and 2, and a `bank-workflow` scenario. |

## Gardener

| Variable | Default | What it does |
|---|---|---|
| `GARDENER_SANDBOX_URI` | unset | Run the Gardener's throwaway test database on another MongoDB (for example a local container). Falls back to `MONGODB_URI` if it doesn't answer. |
| `GARDENER_HELP` | off | Workers whose step fails twice ask the Gardener for help; adds agent `sentinel-2`. |
| `GARDENER_HELP_WAIT_S` | `5` | How long a worker waits for that answer. |
| `GARDENER_PAUSE` | off | All agents pause while the Gardener swaps code. |
| `GARDENER_PAUSE_LEASE_S` | `15` | The pause expires on its own after this long if the Gardener dies. |

## Swarm switches

| Switch | What it adds |
|---|---|
| `SWARM_DYNAMIC` | Scouts and workers are spawned on demand (`SWARM_MAX_AGENTS` 24, `SWARM_WARM` 2 spares, `SWARM_IDLE_S` 15). |
| `SWARM_COMMS` | Prioritised message feed and a live roster (`/api/roster`). |
| `SWARM_RECOVERY` | Re-plan fixes that keep failing. |
| `FAILURE_MEMORY` | Remember failed attempts as versioned lessons. |
| `SWARM_LEASE_DEADLINE` | Agents also stop acting when their last lease renewal is too old. |
| `PLATFORM_ENGINEERS` | Adds agent `platform-1`, which builds reusable tools from repeated slow steps. |
| `MONGO_SPINE` | `$jsonSchema` validators, change-stream pre-images and a lease chain-of-custody collection. |
| `PROGRESS_ETA` | Step-by-step ETAs learned from past runs. |

## Chaos and Red fleet switches

| Switch | What it adds |
|---|---|
| `RED_FLEET` | `python -m chaos.redfleet`: five AI attacker processes. Refuses `WORLD_DB=world` / `SWARM_DB=swarm` unless `RED_FLEET_ALLOW_LIVE=1`. |
| `RED_FLEET_AGENTS` | Comma list of attackers to run (default: all). |
| `RED_FLEET_MAX_ATTACKS` | Strikes per attacker per night (default `2`, `0` = unlimited). |
| `RED_FLEET_COOLDOWN_S` | Seconds between one attacker's strikes (default `30`). |
| `RED_FLEET_LLM` | Set to `0` to make attackers pick from their menu without Claude. |
| `RED_COMMANDER` | A commander that plans multi-step campaigns (needs `RED_FLEET`). |
| `RED_PLANNER` | Scouts that brief the commander (needs `RED_COMMANDER`). |
| `RUNNER_DECIDES` | Every Red strike goes through an admission check. |
| `CHAOS_OUTCOMES` | Record what each attack achieved, for seeded attacks too. |
| `CHAOS_AGENT_LIE` | Fault `lie_to_agent`: a prompt-injection line in the logs. |
| `CHAOS_AGENT_FREEZE` | Fault `agent_freeze`: `SIGSTOP` an agent, then wake it. |
| `CHAOS_POISON_MEMORY` | Fault `poison_memory` and a sixth attacker that plants a bad playbook. The visible defence needs `SWARM_RECOVERY=1` and `FAILURE_MEMORY=1`. |

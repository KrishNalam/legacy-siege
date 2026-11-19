# Configuration reference

All settings are environment variables. They are read from the shell or from a `.env` file at the repo root ([`.env.example`](../.env.example) is a starting point).
Defaults are the values in the code (`common/config.py`, `agents/settings.py` and the module named in each table).
Switches are off unless set to `1`, `true`, `yes` or `on`. The one exception is `ADVERSARY_ALLOW_LIVE`, which must be exactly `1`.

Restart `run.py` after changing anything.

## Core

| Variable | Default | What it does |
|---|---|---|
| `MONGODB_URI` | `mongodb://localhost:27027/?directConnection=true` | MongoDB with transactions, change streams and Atlas Vector Search. |
| `WORLD_DB` | `world` | The estate and chaos database ("Red"). |
| `SWARM_DB` | `swarm` | The agents' database ("Blue"). |
| `SYSTEMS_PORT` | `8701` | Legacy systems API port. |
| `CONSOLE_PORT` | `8700` | War room port. `run.py` also accepts `PORT` if `CONSOLE_PORT` is unset. |
| `SYSTEMS_URL` | `http://127.0.0.1:$SYSTEMS_PORT` | Where agents and the Gardener reach the systems API. |
| `CONSOLE_URL` | `http://127.0.0.1:$CONSOLE_PORT` | Where the rehearsal scripts reach the war room. |
| `PACE` | `1.0` | Multiplier on deliberate "thinking" pauses. `0` in tests. |
| `CONSOLE_VIEW` | `diagram` | What `/` serves: `diagram` or `flow`. |

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
| `RECOVERY_RESPAWN_S` | `15` | Delay before the supervisor replaces a dead agent. |
| `RECALL_THRESHOLD` | `0.70` | Minimum vector-search score for a playbook to be used. |
| `UPGRADE_AFTER` | `2` | Distinct nights with the same fault before the Sentinel asks the Gardener for an upgrade. |
| `PROVE_WINDOW_S` | `30` | How long the Gardener watches for incidents after replaying the fault. |
| `SLOW_APPLY_S` | `20` | Delay used by the `slow_apply` fault. |
| `STEP_HOLD_S` | `1.2 × PACE` | Pause between marking a step sent and sending it (the window for "Kill a worker"). |
| `SYSTEMS_TICK_S` | `1.0` | Simulation tick. `0` disables the background tick (tests). |
| `SYSTEMS_TICK_FIXED` | off | Tick on a fixed cadence (helps against a remote cluster). |
| `BATCH_EVERY_TICKS` / `CASCADE_TICKS` | `2` / `3` | Mainframe batch cadence / how long downstream tolerates stale data. |

More swarm knobs live in `agents/settings.py` (`MONITOR_POLL_S`, `VERIFIER_GRACE_S`, `TASK_TIMEOUT_S`, `MAX_ATTEMPTS`, …).

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
| `UPGRADER_SANDBOX_URI` | unset | Run the Gardener's throwaway test database on another MongoDB (for example a local container). Falls back to `MONGODB_URI` if it doesn't answer. |
| `UPGRADER_HELP` | off | Workers whose step fails twice ask the Gardener for help; adds agent `sentinel-2`. |
| `UPGRADER_HELP_WAIT_S` | `5` | How long a worker waits for that answer. |
| `UPGRADER_PAUSE` | off | All agents pause while the Gardener swaps code. |
| `UPGRADER_PAUSE_LEASE_S` | `15` | The pause expires on its own after this long if the Gardener dies. |

## Swarm switches

| Switch | What it adds |
|---|---|
| `AGENTS_AUTOSCALE` | Scouts and workers are spawned on demand (`AGENTS_MAX_AGENTS` 24, `AGENTS_WARM` 2 spares, `AGENTS_IDLE_S` 15). |
| `AGENTS_COMMS` | Prioritised message feed and a live roster (`/api/roster`). |
| `AGENTS_RECOVERY` | Re-plan fixes that keep failing. |
| `FAILURE_MEMORY` | Remember failed attempts as versioned lessons. |
| `AGENTS_LEASE_DEADLINE` | Agents also stop acting when their last lease renewal is too old. |
| `TOOLSMITH_ENGINEERS` | Adds agent `platform-1`, which builds reusable tools from repeated slow steps. |
| `MONGO_SPINE` | `$jsonSchema` validators, change-stream pre-images and a lease chain-of-custody collection. |
| `PROGRESS_ETA` | Step-by-step ETAs learned from past runs. |

## Chaos and Red fleet switches

| Switch | What it adds |
|---|---|
| `ADVERSARY` | `python -m adversary.redfleet`: five AI attacker processes. Refuses `WORLD_DB=world` / `SWARM_DB=swarm` unless `ADVERSARY_ALLOW_LIVE=1`. |
| `ADVERSARY_AGENTS` | Comma list of attackers to run (default: all). |
| `ADVERSARY_MAX_ATTACKS` | Strikes per attacker per night (default `2`, `0` = unlimited). |
| `ADVERSARY_COOLDOWN_S` | Seconds between one attacker's strikes (default `30`). |
| `ADVERSARY_LLM` | Set to `0` to make attackers pick from their menu without Claude. |
| `ADVERSARY_COMMANDER` | A commander that plans multi-step campaigns (needs `ADVERSARY`). |
| `ADVERSARY_PLANNER` | Scouts that brief the commander (needs `ADVERSARY_COMMANDER`). |
| `RUNNER_DECIDES` | Every Red strike goes through an admission check. |
| `ADVERSARY_OUTCOMES` | Record what each attack achieved, for seeded attacks too. |
| `ADVERSARY_AGENT_LIE` | Fault `lie_to_agent`: a prompt-injection line in the logs. |
| `ADVERSARY_AGENT_FREEZE` | Fault `agent_freeze`: `SIGSTOP` an agent, then wake it. |
| `ADVERSARY_POISON_MEMORY` | Fault `poison_memory` and a sixth attacker that plants a bad playbook. The visible defence needs `AGENTS_RECOVERY=1` and `FAILURE_MEMORY=1`. |

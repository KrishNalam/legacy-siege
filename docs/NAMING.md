# Naming

The product has two vocabularies. The **brand** tells a story: hornets attack the hive, the colony defends it, the
beekeeper strengthens it. The **plain names** say exactly what each part does. This file maps one to the other and
says where each is used.

Status: **names final.** Step 2 is done: UI labels come from `warroom/static/js/glossary.js`. Code, DB and env vars still use the old names (last column).

## Where each name is used

| Layer | Style | Example |
|---|---|---|
| Brand: pitch decks, posts, website, demo narration | Bee names only | "Hornets hit the hive. The Guard caught the lie." |
| Product UI | Bee name, plain name as a subtitle | **GUARD** · verifier |
| Docs | Bee name, defined once here, plain words around it | "The Guard (verifier) reads the state back." |
| Alerts, audit logs, compliance exports, API errors | Plain only | `verifier rejected step INC-0042-s1-a1: acknowledged but not applied` |
| Code, MongoDB, env vars, API fields | Plain only | `verifier.py`, `VERIFIER_GRACE_S` |

Rule: anything read **during an incident or by an auditor** is plain. Anything that **sells or explains** uses the
bee names. Every bee name maps to exactly one plain concept, and the plain name is always one glance away.

## The story

| Concept | Brand / UI | Code / DB | Old name |
|---|---|---|---|
| The customer's legacy systems | **The Hive** | `systems/`, DB `estate` | legacy systems, the estate, DB `world` |
| The agent team | **The Colony** | `agents/`, DB `agents` | the Hive, the Bees, Blue, `swarm/`, DB `swarm` |
| Attack and resilience testing | **Hornets** | `adversary/` | Red, the Fleet, Chaos Monkey, `chaos/` |
| Code upgrades, from outside the colony | **The Beekeeper** | `upgrader/` | the Gardener, `gardener/` |
| The human who approves risky steps | **You** | `approver` | the Beekeeper |
| The live dashboard | **Hive view** | `console/` | war room, `warroom/` |
| Decoy add-on that lures and traps attackers | **Honeypot** | `honeypot/` | (new) |
| Scaling out agents | **Swarm** | `autoscale` | `SWARM_DYNAMIC` |

## Colony roles

| Brand / UI | Plain (code, logs) | What it does | Old name |
|---|---|---|---|
| Queen | `coordinator` | Holds the goal, assigns incidents, relays approvals | `queen` |
| Scout | `investigator` | Owns an incident: recall, trace the chain, plan the fix | `scout` |
| Forager | `operator` | Sends fix commands to one system | `worker` |
| Guard | `verifier` | The only role that can mark a step done, by reading state back | `guard` |
| Sentinel | `monitor` | Polls health, opens incidents, files upgrade requests | `sentinel` |
| Undertaker | `reaper` | Marks silent agents dead and frees their leases | part of `sentinel` (split it out) |
| Reserve | `recovery` | Takes over a dead agent's work from its checkpoint | `nurse` |
| Waggle | `reporter` | Turns state changes into status messages | `waggle` |
| Builder | `toolsmith` | Turns repeated fixes into reusable tools | `platform` |

## What the colony keeps

| Brand / UI | Code / DB collection | Old name |
|---|---|---|
| Comb (the colony's memory) | `playbooks` | `playbooks`, "honey" in the UI |
| Scars (what went wrong before) | `lessons` | `lessons` |
| Dance floor (live activity) | `activity` | `thoughts` |
| (not shown) | `bus` | `messages` |
| Beekeeper's notes | `upgrader_notes`, `upgrader_requests` | `gardener_notes`, `gardener_requests` |

Unchanged (already plain): `incidents`, `tasks`, `ledger`, `approvals`, `agents`, `checkpoints`, `custody`, `runs`,
`roster`, `upgrade_requests`, `schema_samples`, `schema_mappings`, `spawn_requests`, `tools`, `tool_runs`.

## Hornets

The group gets a bee-world name. The roles inside stay plain, because security teams read them in attack reports.

| Brand / UI | Code | Old name |
|---|---|---|
| Hornets | `adversary` | Red fleet, Chaos Monkey |
| Hornet queen | `commander`, `planner` | `red-commander`, `red-planner` |
| Recon | `recon` | `red-scout-hive`, `red-scout-systems` |
| Saboteur, Liar, Assassin, Hacker, Drifter, Poisoner | `saboteur`, `liar`, `assassin`, `hacker`, `drifter`, `poisoner` | `red-saboteur` … `red-memory` |
| Seeded nights | `scenarios` | `bank-night-1/2/3` |
| The one door every attack goes through | `adversary.gate.fire()` | `chaos.runner.fire()` |

## Agent ids

`queen-1` → `coordinator-1` · `scout-1` → `investigator-1` · `worker-mainframe-1` → `operator-mainframe-1` ·
`guard-1` → `verifier-1` · `sentinel-1` → `monitor-1` · `nurse-1` → `recovery-1` · `waggle-1` → `reporter-1` ·
`platform-1` → `toolsmith-1` · `gardener-1` → `upgrader-1`.

## Env var prefixes

| Old | New |
|---|---|
| `SWARM_*` | `AGENTS_*` (`SWARM_DYNAMIC` → `AGENTS_AUTOSCALE`) |
| `NURSE_*` | `RECOVERY_*` |
| `SCOUT_*` | `INVESTIGATOR_*` |
| `GUARD_*` | `VERIFIER_*` |
| `SENTINEL_*` | `MONITOR_*` |
| `GARDENER_*` | `UPGRADER_*` |
| `RED_FLEET_*`, `RED_COMMANDER_*`, `RED_PLANNER`, `CHAOS_*` | `ADVERSARY_*` |
| `WARROOM_*` | `CONSOLE_*` |

Old names stay accepted as aliases for one release, so existing `.env` files keep working.

## UI labels

| Old | New |
|---|---|
| THE BEES | THE COLONY |
| LEGACY SYSTEMS | THE HIVE |
| CHAOS MONKEY | HORNETS |
| THE GARDENER | THE BEEKEEPER |
| honey | comb |
| Beekeeper (the human) | you |

## Rollout

1. Lock these names and the company name. This file is the reference.
2. UI and docs: read labels from one glossary file, so a rebrand is a one-file change.
3. Code: one branch, a mechanical old → new map, env aliases. All test suites and `scripts.demo_check` must pass.
4. Databases last, with a migration script (Atlas indexes and the vector index move with them).

# Legacy Siege — Handoff Sheet

_Last updated: 2026-11-14 ~10:50 EST (second orchestrator). This sheet is self-contained: read it top to bottom before touching anything._

## 1. What this is (one paragraph)
A self-healing operations team of AI agents ("the Hive", bees) keeps a simulated bank's legacy estate
(mainframe → Unix middleware → web portal) online while a seeded Chaos Monkey ("the Fleet") attacks
both the systems **and the agents themselves**. A neutral **Gardener** turns repeated fixes into
permanent code upgrades. **MongoDB Atlas is the spine:** every piece of state (systems, incidents,
leases, ledger, checkpoints, playbook memory) lives in MongoDB; the processes are disposable. The live
**war room** visualization only watches MongoDB change streams. Built for a MongoDB hackathon
(problem statement "Finish the Job"; judging: technical demo 35%, difficulty 30%, impact 20%, creativity 15%).

## 2. Where everything is
| thing | location |
|---|---|
| Code | `~/projects/legacy-siege` on sietchtabr (git; private GitHub repo **mh-anwar/legacy-siege**, branch `main`) |
| War room (live, always on) | **http://100.105.248.23:8700** (tailnet). Public behind the box login: `https://treehouse.anwar.app/p/8700/` (path prefix supported; WebSocket through that proxy not yet verified) |
| Legacy systems API | `http://100.105.248.23:8701` (`/portal/health`, `/mainframe/state`, …) |
| Always-on service | `box serve` service **legacy-siege** (runs `run.py`; survives reboots; `box serve logs legacy-siege`, `box serve restart legacy-siege`) |
| Process logs | `~/projects/legacy-siege/logs/{systems,warroom,swarm,gardener}.log` |
| Database | **MongoDB Atlas** cluster `main.qa11uno.mongodb.net` (8.0.37 enterprise, 3-node replica set, ~30 ms RTT from the box), DBs `world` (Red) and `swarm` (Blue). Connection string lives only in `.env` (chmod 600, git-ignored) as `MONGODB_URI` (and a copy `ATLAS_MONGODB_URI`). **Not confirmed to be the official Atlas Sandbox** — see open item 2 |
| Keys in `.env` | `ANTHROPIC_API_KEY` (added and verified 2026-11-14), `GARDENER_SANDBOX_URI` (local container). **No `VOYAGE_API_KEY`** — owner doesn't have one yet |
| Local MongoDB (dev/tests) | docker container `legacy-siege-mongo` (mongodb-atlas-local:8.0) on `localhost:27027` |
| Design doc | `docs/design-doc.pdf` / `docs/design-doc.txt` (text extraction drops letters 'a'/'g') |
| Interfaces | `CONTRACTS.md` (frozen contracts between the five workstreams; §8 integrator notes) |
| How to run / demo | `README.md` (architecture, MongoDB feature map with file:line, 3-minute demo script) |
| Screenshots | `docs/screenshots/01-idle.png` … `16-laptop-1366x768.png` (one per demo beat); `atlas-live-night1.png` = live Atlas run mid-takeover |
| Containers | `deploy/` (Dockerfile, compose for local + Atlas, fly.toml, README). Image `legacy-siege:dev` built locally (676 MB) |

## 3. Architecture in 30 seconds
- `systems/` — FastAPI app (:8701): three mock legacy systems with personalities, planted vulns, a
  1 s tick loop, honesty faults (an op that *lies* about success; an op that applies *late*). State in `world`.
- `swarm/` — one OS process per agent (queen ×2, scout ×2, workers ×3, guard, waggle, sentinel, nurses).
  LangGraph graphs checkpointed in MongoDB (`MongoDBSaver`). Leases with fencing generations,
  step + ledger writes in one transaction, only the Guard may mark a step verified (by read-back).
- `chaos/` — fault library + seeded scenarios (`bank-night-1/2/3`) + real SIGKILL of agents.
- `gardener/` — upgrade pipeline: plan (27-line diff) → pytest + fault replay → Beekeeper approval →
  hot reload (version 1→2) → prove (replay the fault, no incident).
- `warroom/` — FastAPI (:8700) + vanilla Canvas2D page; one change stream fanned out over WebSocket;
  judge buttons; MongoDB "spine" honeycomb at the bottom.
- `common/` — config, MongoDB access (`txn()` retrying transactions, `next_id`), embeddings, optional LLM.

## 4. How to operate it
- **Open the war room:** http://100.105.248.23:8700 → buttons: Night 1, Night 2, Night 3 (replay),
  Kill a worker, fault launcher, Reset (Full / Next round), Atlas.
- **Views:** `/diagram` (the stage view Krish demos: 4 boxes, zoom 1-4, V = Live / How it works, `?` help),
  `/why` (pitch page), `/flow`, classic on `/`. `WARROOM_VIEW=diagram` makes `/` serve the diagram.
- **Demo order:** Reset ▸ Full → Night 1 → Approve → Kill a worker (button glows when ready) → wait for
  green → Reset ▸ Next round → Night 2 → Approve → Approve (upgrade) → Night 3 → Atlas.
  Full table: README "The 3-minute demo".
- **Rehearse automatically (26 checks):** `cd ~/projects/legacy-siege && .venv/bin/python -m scripts.demo_check`
  (≈5½ min on Atlas; ends with a full reset). Exit code 0 = all pass.
- **Check a MongoDB cluster supports everything:** `.venv/bin/python -m scripts.atlas_check`.
- **Switch cluster:** edit `MONGODB_URI` in `.env`, run `.venv/bin/python -m scripts.setup_db`, then
  `box serve restart legacy-siege`.
- **Unit tests (use local Mongo so Atlas stays clean):**
  `MONGODB_URI='mongodb://localhost:27027/?directConnection=true' PACE=0 .venv/bin/python -m pytest -q <module>/tests`
- **Gardener CI sandbox:** `GARDENER_SANDBOX_URI` in `.env` points the Gardener's throwaway test DB at the
  local container (`localhost:27027`), cutting its test step from ~75 s to ~4 s. If the container is down
  (the box stops idle Docker DBs) it falls back to the live cluster automatically (slower, still passes).
  Restart the container with `docker start legacy-siege-mongo`. Live state is always on `MONGODB_URI`.
- **Speed knob:** `PACE` (in `.env`; currently **0.6**, code default 1.0) scales every deliberate "thinking"
  pause. Change it, then `box serve restart legacy-siege`.

## 5. Verified status (what was actually tested)
- **2026-11-14 10:21 (second orchestrator): 26/26 PASS on Atlas with the Anthropic key**, 318 s total,
  timings unchanged (N1 76 s, N2 45.5 s, diag 12.35→2.69 s). The scout's root-cause line and the
  Gardener's explanation were LLM-written (seen in `logs/swarm.log`, `logs/gardener.log`).
- **Not yet re-rehearsed after commit `3677f21`** (Gardener sandbox → local). Gardener unit tests pass (23),
  and `testing.run_tests` against local Mongo passes in 4 s. **First thing to do: run `scripts.demo_check`.**
- Full rehearsal **26/26 PASS on Atlas three times** (twice at PACE=1, once at PACE=0.6, the last two on
  the final code), plus 7× 26/26 earlier on local MongoDB.
- Unit tests: systems 23, chaos 21, gardener 23, warroom 13, swarm 13 (+1 opt-in) — all pass.
- Measured on Atlas: at PACE=1 Night 1 resolves in ~88 s (includes the lie, the approval and a ~10 s
  kill takeover) and Night 2 in ~52 s. At **PACE=0.6 (current setting in `.env`)**: Night 1 ~76 s,
  Night 2 ~45 s, diagnosis 14.2 s → 3.1 s, hops 3 → 1 thanks to `$vectorSearch`; whole rehearsal 315 s.
- War room: 0 console/page errors through full live runs; ~55–60 fps headless.
- Two independent reviews by gpt-6.1-sol (plan review + code review); findings fixed or listed below.

## 6. Open items (ranked)
1. **LLM is in, but barely visible.** `ANTHROPIC_API_KEY` works (`claude-haiku-5-5`, ~1–3 s/call). Fixed
   `common/llm.py`: current Claude models reject `temperature`, which had made every call silently return
   None; it now sends `thinking:{type:"disabled"}` + `output_config:{effort:"low"}`. LLM time is subtracted
   from the think pause (`swarm/harness.py` `think()`), so a key never slows the demo. Yet the LLM still only
   writes ONE scout sentence (`swarm/roles/scout.py:225`, `polish=True`) and the Gardener explanation.
   Biggest pitch risk remains "where's the AI?". Idea (not built, not reviewed): **"LLM proposes,
   verification disposes"**: after diagnosis, a background thread asks Claude (structured JSON) for a
   root-cause hypothesis and an op plan from the evidence gathered. Store it on the incident as
   `ai:{model, hypothesis, proposed_ops, agrees_with_policy, overruled_ops, latency_ms}` and show an
   "AI second opinion" chip in the war room, with the deterministic guardrail visibly overruling unsafe
   proposals. Other ideas: an LLM postmortem on the playbook doc; a judge's free-text question answered from
   MongoDB. Must not block, must work with no key, and demo_check must stay 26/26. Get a gpt-6.1-sol design
   review first (the previous advisor run was cancelled at handoff).
   **Voyage:** no key yet. When one arrives: add `VOYAGE_API_KEY`, then do a **full reset** (playbooks re-embed).
2. **Atlas Sandbox / compliance (owner is handling the cluster switch with the next agent):**
   - Owner wants to move to "the right Atlas" (the official Sandbox). Switch: put the new `mongodb+srv://`
     string in `.env` as `MONGODB_URI`, add the box's egress IP **99.232.124.76** (or 0.0.0.0/0) in Atlas Network
     Access, run `.venv/bin/python -m scripts.atlas_check`, then `.venv/bin/python -m scripts.setup_db`, then
     `box serve restart legacy-siege`, then `scripts.demo_check`. Get the string with the secure secret prompt
     (`delivery:"file"`), never in chat. The owner declined the prompt once ("hold on"), so let them lead.
   - **Repo stays private until submission** (owner's decision). History scanned: no keys or passwords in any
     commit. Only collaborator: mh-anwar. Ask for teammates' GitHub usernames before going public.
   - **Deadline:** the owner said it is NOT 17:00 (the design-doc date); the actual time was not given yet. Ask.
   - **Possible rule risk:** the design doc says "no project code before 10:45; commit history starts at
     10:45". This repo's first commits are 07:32 today. The owner should check the event's real rules.
     Don't rewrite history unasked.
   - 1-minute video still to record; lead the pitch with agent behaviour, not "dashboard".
3. **Timing on Atlas:** the Gardener's test step was the biggest single block (~75 s of the ~114 s Gardener
   phase), and is now ~4 s via `GARDENER_SANDBOX_URI` (needs a rehearsal to confirm). The rest is fixed waits:
   10 s dead-agent detection (`LEASE_S`), the 30 s prove window, cascade settle, and ~30 ms per MongoDB round
   trip (one 487 ms spike seen). For a 3-minute slot, consider a shorter prove window for the demo.
4. **PostHog:** the MCP is connected (`posthog-uoft`), but its only project is `uoft-index` (the owner's
   other site), and the free plan allows 1 project, so `project-create` is blocked. The owner chose "new
   project". Options: they create a new PostHog *organization* in the UI (free, gets its own project) and
   reconnect, or they accept `uoft-index` with an `app=legacy-siege` property. Planned use: `$ai_generation`
   events for the agents' LLM calls (LLM analytics), incident TTR events, war-room clicks. Must be async and
   a no-op without a key. Nothing built yet.
5. Known robustness gaps (not on the demo path), from the code review: no run-epoch fence on external
   ops issued just before a reset (#3/#6); Gardener crash-recovery between deploy and proof (#10);
   snapshot/stream ordering can briefly resurrect a deleted doc right after a reset (#11);
   a reset takes several seconds on Atlas (systems API answers 503 "resetting" in the gap — expected); two
   independent incidents that later join are not merged (#12); Gardener duplicates the agent
   heartbeat code instead of using `swarm.harness.Agent`; Guard checkpoints every pending poll.
6. `Reset ▸ Next round` after a Gardener deploy keeps the patched code; use **Full** reset to restart the demo.
7. WebSocket through the treehouse `/p/8700/` proxy is unverified (direct tailnet URL works).
8. Vercel and other integrations: no Vercel MCP connected as of this handoff. Check available tools first.

## 7. Gotchas
- `box serve` arg order: `box serve --port 8700 add NAME -- /abs/path/python run.py` (option first, absolute exe).
- Never `pkill -f` (kills your own shell); stop by PID or `box serve stop legacy-siege`.
- Chaos kills agents on purpose; `run.py` restarts crashed top-level processes; the supervisor
  re-spawns dead bees as nurses.
- `common/llm.py`: don't pass `temperature`/`top_p` (400 on current models; the wrapper swallows errors
  and returns None, so it fails silently). Test with a direct SDK call when changing it.
- Owner preferences: high effort; no multi-agent Workflow tool unless asked; Codex `gpt-6.1-sol` via
  `delegate_task` as advisor/reviewer; plain-language reports; never print `.env`.
- Tests must not run against Atlas (they create `*_test_*` DBs); point `MONGODB_URI` at localhost.

## 8. Feature switches (all default OFF; set to 1 in `.env`, restart)
The 15:00 stage pick and its evidence live in `/srv/shared/legacy-siege-mail/results/STAGE-SWITCHES.md`. On main:
- Hive: `SWARM_DYNAMIC` (agents scale), `SWARM_COMMS` (agents talk; also the live roster `swarm.roster`, `GET /api/roster`), `SWARM_RECOVERY` (re-plan failed fixes; its own
  switch, RED_FLEET no longer turns it on), `FAILURE_MEMORY` (learn from failures), `SWARM_LEASE_DEADLINE` (local lease
  deadline; keep off on stage).
- Stage: `WARROOM_VIEW` (`diagram|flow|classic`, what `/` serves; not a 0/1 switch), `DEMO_AUTOPLAY` (nights run on their own), `SCHEMA_DRIFT_DEMO` (night-3 field rename; off for demo_check).
- Chaos: `CHAOS_OUTCOMES`, `CHAOS_AGENT_LIE`, `CHAOS_AGENT_FREEZE`, `CHAOS_POISON_MEMORY` (defence fires only with
  `SWARM_RECOVERY=1 FAILURE_MEMORY=1`).
- Red: `RED_FLEET` (stage-safe only tamed: `RED_FLEET_AGENTS=red-assassin RED_FLEET_VICTIMS=worker:step_sent
  RED_FLEET_MAX_ATTACKS=1 RED_FLEET_SKIP_SCENARIOS=bank-night-2,bank-night-3`; needs `RED_FLEET_ALLOW_LIVE=1` on DBs
  named world/swarm), `RED_COMMANDER`.
- MongoDB / AI: `MONGO_SPINE` (pre-images, custody, validators, analytics), `AI_DRIVES`, `AI_SECOND_OPINION` (need the key).

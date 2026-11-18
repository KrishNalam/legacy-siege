# Legacy Siege: bug list

Review date: 2026-11-18. Commit reviewed: `ba36a0d` (main, clean, same as `origin/main`).
Most severe first. Each entry gives the place, a concrete failure, the severity, how it was checked, and a suggested fix.
No code was changed.

How things were checked:
- **Reproduced**: shown on a scratch copy against a local MongoDB 8.0 replica set.
- **Read**: traced through the code path line by line, not run end to end.
- **Plausible**: the mechanism is clear from the code, but it depends on timing and was not reproduced.

The full stack could not run here (see the note at the end), so no entry was checked against live agents.

---

## 1. Full reset can overwrite newer `systems/mainframe.py` with an old Gardener backup

- **Where:** `gardener/pipeline.py:617-641` (`latest_backup`, `revert`), called unconditionally from `warroom/api.py:174-179` on every full reset.
- **What goes wrong:** `revert()` takes the newest file in `gardener/backups/` and writes it over `systems/mainframe.py` whenever the two differ. It never checks whether an upgrade is actually deployed. `gardener/backups/` is git-ignored (`gardener/backups/.gitignore`), so old backups survive `git pull`.
- **Scenario:** You run the demo on Monday, so a backup of Monday's `mainframe.py` is written. On Tuesday someone edits `systems/mainframe.py`, committed or not. Any **Full reset** (war room button, `scripts.demo_check`, `scripts.record_demo`) silently replaces Tuesday's file with Monday's copy and hot-reloads it. Uncommitted work is lost.
- **Severity:** High (loses source code; also runs old code in the demo).
- **Check:** Reproduced. I put a stale backup in `gardener/backups/`, added a line to `systems/mainframe.py` and called `pipeline.revert('mainframe', reload=False)`. The file was replaced by the backup's one line.
- **Fix:** Revert only when an upgrade request for that system is `deployed`/`proven` and not yet `reverted_at`, **and** the current file's sha256 equals the patched sha recorded at deploy. Restore the backup named in that upgrade doc (`upg["backup"]`), not "the newest file". Delete or rename the backup after a successful revert.

## 2. `run.py --reset` and "Next round" leave the Gardener's patch live but report version 1

- **Where:** `scripts/setup_db.py:273-296` (`reset()` has no revert), `run.py:87-88`, `systems/estate.py:117-124` (`Estate.reset` re-seeds `version: 1` whatever module is loaded), `warroom/api.py:174` (only `mode == "full"` reverts).
- **What goes wrong:** Only the war room's Full reset calls `gardener.pipeline.revert`. Every other reset wipes `world.systems` (so the version goes back to 1) but keeps the patched `validate_record()` on disk and in memory.
- **Scenario:** Run `scripts.demo_check --keep` or `record_demo --keep`, or stop the stack after Night 2's upgrade is deployed. Then run `.venv/bin/python run.py --reset`. The mainframe says `version: 1`, but `GET /mainframe/state` shows `validation_enabled: true`. Night 1's `batch_abort` record is rejected instead of ABENDing the batch, so no incident opens and the demo is broken. The same mismatch happens after "Reset this night" following a deploy, which also leaves playbooks staled at the v2 bump.
- **Severity:** Medium-high (the documented reset breaks the demo with no visible cause).
- **Check:** Read. `run_batch()` (`systems/mainframe.py:163-170`) skips records that `validate_record()` rejects.
- **Fix:** Call `gardener.pipeline.revert(...)` from `setup_db.reset()` too (once bug 1 is fixed). Make `/_admin/reset` either revert the module or keep the live version number instead of forcing 1.

## 3. The Gardener gets stuck in `deployed` if it crashes or the replay fails during the prove window

- **Where:** `gardener/main.py:26` (`IN_FLIGHT` has no `"deployed"`), `gardener/pipeline.py:535` (writes `prove` before watching), `gardener/pipeline.py:526-534` (a failed replay also writes `prove`), `gardener/pipeline.py:600` (skips prove when `prove` exists).
- **What goes wrong:** On restart, `serve()` resumes only `planning/testing/awaiting_approval/deploying`, and `next_request()` looks only for `requested`. If the process dies during the 30 s prove window, or `fire_fault` raises, the request stays `deployed` forever. Even a manual `pipeline.run()` won't prove it, because the `prove` field is already set.
- **Scenario:** `run.py` restarts a crashed Gardener mid-prove. The upgrade never reaches `proven`, so `DEMO_AUTOPLAY` never starts Night 3 (`warroom/autoplay.py:86-90`), and the diagram's NEXT NIGHT button waits for proven or rejected.
- **Severity:** Medium (HANDOFF lists this as known gap #10; it is still present).
- **Check:** Read.
- **Fix:** Add `"deployed"` to `IN_FLIGHT`. In `run()`, prove again when `prove.incident_opened is None` or `prove.error` is set.

## 4. After a full reset the Queen skips new incidents whose ids it has seen before

- **Where:** `swarm/roles/queen.py:87, 144-145` (`handled` set, in process memory). A full reset wipes `swarm.counters` (`scripts/setup_db.py:282`), so ids restart at `INC-0001`. The queen process is not restarted (`swarm/supervisor.py:325-327` respawns only *missing* roster bees).
- **What goes wrong:** On the second demo after the queen started, `INC-0001` is already in `handled`. So no triage line, no "Assigning INC-0001 to scout-1", and with `SWARM_DYNAMIC=1` no scout spawn request. Scouts still self-claim it after 2 s (`swarm/roles/scout.py:716-719`). In dynamic mode no scout exists, so a Nurse has to cover (`swarm/roles/nurse.py:187-200`). The Guard has the same pattern (`swarm/roles/guard.py:51-52`, `seen`/`noted_slow` keyed by action id), so its "Reading back …" line goes missing for reused ids.
- **Severity:** Medium (slower and quieter Night 1 on every rehearsal after the first; dynamic mode loses the spawn beat).
- **Check:** Read.
- **Fix:** Key the set by `(_id, opened_at)`, or clear it when `swarm.stats{_id:"roster_reset"}.at` changes (the supervisor already watches that doc).

## 5. LangGraph checkpoints can leak across a full reset (thread ids are reused)

- **Where:** thread ids are the task id (`swarm/roles/worker.py:305-307`) and `"<incident>:scout"` (`swarm/roles/scout.py:57-58`). Resume logic: `worker.py:318-325`, `scout.py:697-700`. The reset wipes `checkpoints` (`scripts/setup_db.py:282`) while agents keep running.
- **What goes wrong:** An agent caught mid-node by a full reset finishes that node and writes a new checkpoint for its old thread *after* the wipe, then fails its next fenced write. The next night reuses the same ids (`INC-0001-t1`, `INC-0001:scout`). The new task's `graph.get_state(cfg).values` is non-empty, so the worker resumes the old graph with stale findings, or `scout.drive` returns `snap.values` as if the incident were already done.
- **Severity:** Medium (wrong diagnosis or a stalled incident; needs a reset while agents are busy).
- **Check:** Plausible, not reproduced. It is the same family as HANDOFF's "no run-epoch fence" (#3/#6).
- **Fix:** Put a reset epoch (for example `opened_at` or a counter in `swarm.stats`) in every thread id. Or skip the INC counter reset so ids are never reused.

## 6. Docker Compose breaks when `.env` sets `WARROOM_PORT` / `SYSTEMS_PORT`

- **Where:** `deploy/docker-compose.yml:30-41`, `deploy/docker-compose.atlas.yml:13-22`.
- **What goes wrong:** `env_file: ../.env` overrides the image's `ENV WARROOM_PORT=8700 SYSTEMS_PORT=8701`, but `ports:` always maps to container ports 8700/8701. AGENTS.md tells developers to set these ports per box account (for example 20100-20199).
- **Scenario:** With `WARROOM_PORT=20150` in `.env`, the war room listens on 20150 inside the container. `localhost:8700` refuses connections, but the image healthcheck still reports healthy, because it reads the same env var.
- **Severity:** Medium (the one-command start fails with no clear error).
- **Check:** Read.
- **Fix:** Pin `WARROOM_PORT: "8700"` and `SYSTEMS_PORT: "8701"` under `environment:` in both compose files (it already overrides `MONGODB_URI` this way).

## 7. `run.py` exits 0 when it gives up on a crash loop

- **Where:** `run.py:73-82` (`stop()` always calls `sys.exit(0)`), called from `run.py:112-113`.
- **What goes wrong:** After 5 crashes in 60 s, run.py stops everything and reports success. A supervisor that restarts only on failure (for example systemd `Restart=on-failure`) won't restart it and won't flag anything.
- **Severity:** Low-medium.
- **Check:** Read.
- **Fix:** Give `stop` an exit code and call `stop(code=1)` on the crash-loop path.

## 8. `setup_db` crashes with a raw traceback on MongoDB without Atlas Search

- **Where:** `scripts/setup_db.py:250` (`list_search_indexes()`), run with `check=True` by `run.py:88`.
- **What goes wrong:** On a plain MongoDB replica set (Community `mongod`), `$listSearchIndexes` fails with `SearchNotEnabled` (code 31082). `run.py` dies with a `CalledProcessError` traceback. Nothing says that Atlas or `mongodb/mongodb-atlas-local` is required.
- **Severity:** Low (a hard requirement with a confusing error).
- **Check:** Reproduced. Against a local Community 8.0.4 replica set, all 107 gardener/swarm test errors are this one `OperationFailure`.
- **Fix:** Catch code 31082 and print one line naming the requirement. State it in the README.

## 9. `PORT` is honoured by `run.py` but not by the rest of the code

- **Where:** `run.py:26` uses `WARROOM_PORT or PORT or 8700`. `common/config.py:30` reads only `WARROOM_PORT`. So do `scripts/demo_check.py:628`, `drift_check.py:147` and `record_demo.py:285` (`--base` default), and the healthcheck in `deploy/Dockerfile:39`.
- **What goes wrong:** With only `PORT=9000` set, the war room listens on 9000, while the rehearsal scripts and the healthcheck still call 8700.
- **Severity:** Low.
- **Check:** Read.
- **Fix:** `WARROOM_PORT = int(os.environ.get("WARROOM_PORT") or os.environ.get("PORT") or 8700)` in `common/config.py`, and use `config.WARROOM_PORT` in `run.py`.

## 10. Gardener resume after a crash mid-deploy bumps the version twice

- **Where:** `gardener/pipeline.py:447-453` skips the write when the file is already patched but always calls `reload_system()`. `systems/estate.py:132` does `$inc: {version: 1}` on every reload.
- **What goes wrong:** If the Gardener dies after the reload but before the `deployed` transaction (`pipeline.py:475-483`), the resumed `deploy()` reloads again. The mainframe goes v1 → v2 → v3, and the ledger receipt records `2 -> 3`.
- **Severity:** Low.
- **Check:** Read.
- **Fix:** Record `version_before` on the upgrade doc before reloading. On resume, skip the reload if the live version is already above it.

## 11. Test isolation: `common/tests` write to the default `swarm` database

- **Where:** `common/llm.py:128` writes `swarm.stats{_id:"llm_guard"}` through `col()`. `common/tests/test_llm.py` does not set `SWARM_DB`.
- **What goes wrong:** Running the documented test command writes a counter into the `swarm` DB on `localhost:27027`. That is the dev stack's live database, and CONTRACTS says tests must use `*_test_*` DBs.
- **Severity:** Low.
- **Check:** Reproduced. After the suite ran, the local `swarm` DB held `stats {_id: "llm_guard", all: {calls: 3, ...}}`.
- **Fix:** Add a `common/tests/conftest.py` that sets `WORLD_DB`/`SWARM_DB` to test names before importing `common`, like the other packages do.

## 12. `RED_FLEET_ALLOW_LIVE` accepts only `"1"`

- **Where:** `chaos/redfleet/core.py:60`. Every other switch accepts `1/true/yes/on`.
- **What goes wrong:** `RED_FLEET_ALLOW_LIVE=true` with the default DB names makes the Red fleet idle silently, apart from one line in `logs/redfleet.log`.
- **Severity:** Low.
- **Check:** Read.
- **Fix:** Use the same truthy parser as the other switches.

## 13. Gardener patches are lost when a container is recreated, but MongoDB still says `proven`

- **Where:** `gardener/pipeline.py:450` writes `systems/mainframe.py` inside the container. `deploy/fly.toml` and both compose files keep no volume for it.
- **What goes wrong:** After a Fly machine restart or `docker compose up --build`, the image's legacy `mainframe.py` is back. But `world.systems.version` is still 2 and the upgrade request still says `proven`. Night 3 then opens an incident, and the war room shows the upgrade as done.
- **Severity:** Low (only for deployed copies).
- **Check:** Read.
- **Fix:** On Gardener start-up, compare the live module's sha with `proven` upgrades and either re-apply or mark them reverted. Or store the patched source in MongoDB and load it at systems start.

## 14. `PUBLIC_HOST` is dead config with a hard-coded tailnet IP

- **Where:** `common/config.py:32` (default `100.105.248.23`). It is also set in both compose files. Nothing reads it.
- **Severity:** Low (clean-up; the IP leaks a private address into a public repo).
- **Check:** Read. `grep -rn PUBLIC_HOST` finds only the definition and the compose files.
- **Fix:** Delete it.

---

### Not bugs, but worth knowing

- `common/db.py:81-98` (partition helpers) is never called (CONTRACTS says so). It is dead code.
- `.dockerignore` excludes `deploy/README.md`, which does not exist.
- `scripts/tests/test_cut_video.py` needs `docs/video-script.md`. That file exists in the repo; I left it out of my copy on purpose.

### What could not be checked here

There is no Docker access on this machine, so `mongodb/mongodb-atlas-local` could not run. MongoDB Community has no Atlas Search, so `scripts.setup_db`, `run.py`, the swarm and `scripts.demo_check` did not run.

What did run:
- Unit tests: common 12/12, systems 29/29, chaos 81/81 and warroom 23/23 pass. In gardener (16 pass) and swarm (28 pass, 1 skipped), every error is the Atlas Search setup error. In scripts, 26 pass; the 4 failures are the video doc I left out.
- The systems API and the war room were started on 8701/8700, and Night 1 was fired through `POST /api/scenario/bank-night-1`.

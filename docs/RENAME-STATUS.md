# Plain-names rename: status

Plan and full name map: [NAMING.md](NAMING.md). Branch `plain-names` (stacked on `ui-glossary` → `naming` → `fix/readme-and-bugs`).

## Done (step 3, code)
- Packages: `swarm/` → `agents/`, `chaos/` → `adversary/`, `gardener/` → `upgrader/`, `warroom/` → `console/`.
- Roles, files, agent ids: coordinator, investigator, operator, verifier, monitor, recovery, reporter, toolsmith, upgrader.
- Env vars: `AGENTS_*`, `ADVERSARY_*`, `CONSOLE_*`, `RECOVERY_*`, `VERIFIER_*`, `MONITOR_*`, `UPGRADER_*`, …
  Old names still accepted (copied to the new name in `common/config.py`; test: `common/tests/test_env_aliases.py`).
- Human approver is `approver` in code and agent messages (`decided_by: "approver"`).
- Reaper split into `agents/roles/reaper.py`; it still runs inside the monitor's process (same timing, same roster).
- UI keeps bee names via `console/static/js/glossary.js`.
- Verified: all 7 test suites pass; `scripts.demo_check` 25/25 on the renamed stack (before the approver/reaper commit, after which common, console and agents suites were re-run).

## Not done yet
- Docs still use old folder/role/env names: README.md, component READMEs (now under the new folders), CONTRACTS.md,
  CLAUDE.md, AGENTS.md, docs/*.md, deploy/README.md, Dockerfile and fly.toml comments.
- Log files are now `logs/agents.log`, `logs/console.log`, `logs/upgrader.log`, `logs/adversary.log`; docs still say `logs/swarm.log` etc.
- One more full `scripts.demo_check` run after the docs pass.
- Optional: run the reaper as its own process (behaviour change: roster, UI cell count, demo_check).

## Step 4 (not started): databases
DB names `world` / `swarm` (`WORLD_DB`, `SWARM_DB`) and collections `thoughts`, `messages`, `gardener_notes`,
`gardener_requests`, `chaos_events`, `chaos_scenarios`, `platform_signals` keep their old names. Needs a migration
script; Atlas Search and vector indexes move with them.

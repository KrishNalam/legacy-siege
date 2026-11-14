# Legacy Siege — agent guide

Read these before changing anything: `README.md` (architecture, demo), `CONTRACTS.md` (frozen interfaces
between workstreams), `docs/HANDOFF.md` (status, open items, gotchas). Design doc:
https://claude.ai/artifact/TYfnA6aTSG99oMb9t1nzpr

## Layout
`systems/` mock legacy estate (FastAPI) · `swarm/` agent processes (LangGraph) · `chaos/` seeded faults +
SIGKILL · `gardener/` code-upgrade pipeline · `warroom/` live map (change streams) · `common/` config, DB, LLM ·
`scripts/` setup_db, atlas_check, demo_check · `run.py` starts the whole stack.

## Never break
`scripts.demo_check` must stay all-pass. The four beats that carry the score: cascade traced
portal → unix → mainframe, lying system caught by the Guard, agent kill + nurse takeover, round-2 speedup.
Do not change a contract in `CONTRACTS.md` without the team agreeing.

## Rules
- Secrets only in `.env` (git-ignored, chmod 600). Never print, log or commit it.
- Unit tests run against local MongoDB, never Atlas:
  `MONGODB_URI='mongodb://localhost:27027/?directConnection=true' PACE=0 .venv/bin/python -m pytest -q <module>/tests`
- Ports come from `SYSTEMS_PORT` / `WARROOM_PORT`. Each box account has its own port range
  (theclockkeeper: 20100–20199, never 20100 or 20199). Listen on `0.0.0.0`.
- Never `pkill -f` (it kills your own shell); stop processes by PID or via `box serve`.
- Don't pass `temperature`/`top_p` to current Claude models (`common/llm.py` swallows the 400 and returns None).
- Small commits, rebase on `origin/main` before pushing, never force-push `main`.

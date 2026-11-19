# Deploying Legacy Siege

One container runs the whole stack through `run.py`: systems API (:8701), war room (:8700),
swarm supervisor and gardener. They must stay in **one container** (one PID namespace):
chaos kills bees with a real `SIGKILL` on the PID stored in `swarm.agents`.
Because the agents live as processes, run **exactly one** instance. No autoscaling, no
scale-to-zero, no load balancing across replicas. All durable state lives in MongoDB, so
restarting the container is safe.

All commands below are run from the **repo root**.

## Local, with its own MongoDB (compose)

```bash
docker compose -f deploy/docker-compose.yml up --build -d
# war room: http://<host>:8700   systems API: http://<host>:8701
docker compose -f deploy/docker-compose.yml logs -f app
docker compose -f deploy/docker-compose.yml down        # add -v to wipe Mongo data
```

The compose `mongo` service is `mongodb/mongodb-atlas-local:8.0` (vector search + change
streams), with its data in a named volume. The app is pointed at it with
`MONGODB_URI=mongodb://mongo:27017/?directConnection=true` (this overrides `.env`).
To use different host ports: `CONSOLE_HOST_PORT=18800 SYSTEMS_HOST_PORT=18801 docker compose ...`.

Plain local dev without Docker is unchanged: `.venv/bin/python run.py` against the
`legacy-siege-mongo` container on port 27027.

## Against MongoDB Atlas

1. Any Atlas tier works, including free M0 (it has Vector Search, limited to 3 search indexes).
   Allow the host's IP in Atlas **Network Access**.
2. Check the cluster first: `MONGODB_URI='mongodb+srv://...' .venv/bin/python -m scripts.atlas_check`
3. Run:

```bash
MONGODB_URI='mongodb+srv://USER:PASS@cluster.xxxxx.mongodb.net/?retryWrites=true&w=majority' \
  docker compose -f deploy/docker-compose.atlas.yml up --build -d
```

(or put `MONGODB_URI` in `.env`). The container runs `scripts.setup_db` at startup, which
creates collections, validators and indexes, including the vector index.
Reset data: `docker compose -f deploy/docker-compose.atlas.yml run --rm app python run.py --reset`.

### A plain VM (any cloud)

Install Docker, copy the repo over, put the Atlas `MONGODB_URI` and keys in `.env`, then run the
Atlas compose command above. `restart: unless-stopped` brings it back after a reboot. Put
Caddy/nginx in front for HTTPS if you like. The proxy **must pass WebSocket upgrades**
(Caddy does this by default; for nginx set `proxy_http_version 1.1`, `Upgrade` and
`Connection "upgrade"` headers, and a long `proxy_read_timeout`).

### Fly.io

`deploy/fly.toml` sets up one always-on machine (`auto_stop_machines = "off"`,
`min_machines_running = 1`) with the war room on HTTPS. Fly passes WebSockets through.

```bash
fly apps create <name>            # then set app = "<name>" in deploy/fly.toml
fly secrets set --config deploy/fly.toml MONGODB_URI='mongodb+srv://...' \
  ANTHROPIC_API_KEY=... VOYAGE_API_KEY=...
fly deploy --config deploy/fly.toml --dockerfile deploy/Dockerfile --ha=false
fly scale count 1 --config deploy/fly.toml    # make sure there is only one machine
```

Fly egress IPs change, so allow `0.0.0.0/0` in Atlas Network Access or use a static egress IP.

### Railway / Render

Use `deploy/Dockerfile` as the Dockerfile path with the repo root as build context. Set one
instance, no autoscaling or sleep, and an HTTP port of 8700. Both support WebSockets.
Only one public port is exposed on these platforms. That's fine, because the bees reach the
systems API over `127.0.0.1:8701` inside the container.

## Environment variables

| var | default | notes |
|---|---|---|
| `MONGODB_URI` | `mongodb://localhost:27027/?directConnection=true` | **the one that matters**. Atlas `mongodb+srv://...` in the cloud |
| `ANTHROPIC_API_KEY` | unset | optional; deterministic fallbacks without it |
| `LLM_MODEL` | `claude-haiku-5-5` | |
| `VOYAGE_API_KEY` | unset | optional; local-hash embeddings without it |
| `PACE` | `1.0` | demo speed multiplier on deliberate delays |
| `CONSOLE_PORT` / `SYSTEMS_PORT` | `8700` / `8701` | ports inside the container |
| `PUBLIC_HOST` | `localhost` | unused by the app (kept for compatibility) |
| `WORLD_DB` / `SWARM_DB` | `world` / `swarm` | DB names; change them to share one cluster between deployments |

## Notes

- The war room is a FastAPI app that streams MongoDB change streams over **WebSockets**. Every
  proxy or platform in front of it must allow WebSocket upgrades and long-lived connections.
- The image is `python:3.12-slim`, runs as non-root `app` (uid 10001) and uses `tini` as PID 1
  to reap killed bees. Its healthcheck is `GET /` on :8700.
- Logs go to `/app/logs/*.log` inside the container (not to stdout, except `run.py`'s own lines).
  Use `docker compose exec app tail -f logs/swarm.log`.

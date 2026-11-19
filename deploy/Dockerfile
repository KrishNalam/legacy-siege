# Legacy Siege: one container runs the whole stack via run.py
# (systems :8701, war room :8700, swarm supervisor, gardener).
# Everything shares one PID namespace on purpose: chaos sends real SIGKILLs to
# bee PIDs it reads from swarm.agents, so all processes must live together.
#
# Build from repo root:  docker build -f deploy/Dockerfile -t legacy-siege:dev .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CONSOLE_PORT=8700 \
    SYSTEMS_PORT=8701

# tini = PID 1 that reaps zombies. Chaos SIGKILLs bees and run.py starts children
# in new sessions, so orphans get re-parented to PID 1 and must be reaped.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deps first so code edits don't bust the pip layer. All deps ship cp312
# manylinux wheels, so no compiler is needed.
COPY deploy/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app
# Repo is writable by the app user: run.py writes logs/ next to itself.
COPY --chown=app:app . /app
RUN mkdir -p /app/logs && chown app:app /app/logs

USER app

EXPOSE 8700 8701

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:%s/' % os.environ.get('CONSOLE_PORT','8700'), timeout=4); sys.exit(0)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "run.py"]

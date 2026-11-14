# 60-second video: voiceover script

Timed to the six beats that `scripts/cut_video.py` cuts (the burned-in captions match). About 140 words,
a calm pace of ~2.4 words per second. Read it over the silent cut, or feed it to a TTS, then:

```bash
.venv/bin/python -m scripts.cut_video ~/projects/legacy-siege-media/beats-<ts>.json --voiceover vo.mp3
```

**Numbers:** the round-2 caption prints the take's real diagnosis times and hops (from the beat log's
`metrics`). If they differ from "twelve → three", say what the caption says.

| time | beat | on screen | voiceover |
|---|---|---|---|
| 0–7 s | **Hook** | Title card, then red lightning hits the mainframe and the estate turns amber | Tonight, a Chaos Monkey attacks a legacy bank. Twelve AI agents have to keep it alive. |
| 7–17 s | **Cascade** | One incident card; a blue pulse walks portal → unix → mainframe | The portal still answers 200, with stale balances. A scout walks the blame chain, portal, unix, mainframe, and finds a crashed batch job. |
| 17–26 s | **The lie** | ACK ≠ APPLIED stamp on the mainframe step, attempt a2 goes green | A worker sends the fix. The mainframe says "applied". It's lying. The Guard reads the state back, rejects it, and tries again. |
| 26–36 s | **Real kill** | Beekeeper approval, the worker bee falls, a nurse picks up its ticket | Now we kill that worker mid-step, a real SIGKILL. A nurse takes its lease and resumes from the checkpoint in MongoDB, without redoing a step. |
| 36–48 s | **Learning** | Golden recall beam; 1 hop; Learning chart round 1 vs 2 | Night two, same attack. Vector search recalls last night's playbook: one hop instead of three, diagnosis in three seconds instead of twelve. |
| 48–60 s | **Fix for good** | Gardener card v1 → v2; night 3 stays green; Atlas collections view | Then the Gardener patches the code itself, tested and approved. Night three: nothing happens. Every lease, checkpoint and memory lives in one place. MongoDB is the hive's memory. |

## Plain text (for TTS)

Tonight, a Chaos Monkey attacks a legacy bank. Twelve AI agents have to keep it alive.

The portal still answers 200, with stale balances. A scout walks the blame chain, portal, unix,
mainframe, and finds a crashed batch job.

A worker sends the fix. The mainframe says "applied". It's lying. The Guard reads the state back,
rejects it, and tries again.

Now we kill that worker mid-step, a real SIGKILL. A nurse takes its lease and resumes from the
checkpoint in MongoDB, without redoing a step.

Night two, same attack. Vector search recalls last night's playbook: one hop instead of three, diagnosis
in three seconds instead of twelve.

Then the Gardener patches the code itself, tested and approved. Night three: nothing happens. Every
lease, checkpoint and memory lives in one place. MongoDB is the hive's memory.

## Recording and cutting

With a stack running (`run.py`) and `MONGODB_URI`/`WORLD_DB`/`SWARM_DB` pointing at the same databases:

```bash
.venv/bin/python -m scripts.record_demo --base http://127.0.0.1:8700      # ~3-5 min; runs demo_check's 26 checks
.venv/bin/python -m scripts.cut_video ~/projects/legacy-siege-media/beats-<ts>.json --print-plan
.venv/bin/python -m scripts.cut_video ~/projects/legacy-siege-media/beats-<ts>.json \
    --out ~/projects/legacy-siege-media/legacy-siege-60s.mp4 [--voiceover vo.mp3] [--layout doc]
```

- Output goes to `~/projects/legacy-siege-media/` (not the repo): `raw-<ts>.webm` (1920×1080) and
  `beats-<ts>.json` (beats with `t_start`/`t_end` in video seconds, raw events, demo_check results,
  `demo_ok`). Only cut a take whose `demo_ok` is true.
- `record_demo` ends with a full reset (Gardener revert) unless `--keep`. It needs Playwright Chromium
  (newest `~/.cache/ms-playwright/chromium-*`, launched with `--no-sandbox`) and `cut_video` needs a full
  ffmpeg (libx264, drawtext) on PATH.
- The Atlas view prints the cluster host: record against the Atlas-backed stack for the final take.
- `--layout doc` uses the design doc's 5/15/10/15/10/5 s split (no Atlas view at the end); the default
  layout is the one this voiceover is timed to.

## After the code freeze (schema-drift beat)

`cut_video.py` has a commented slot for a schema-drift beat between "Real kill" and "Learning". When it
exists, take ~5 s from "Learning" and add one line here, e.g. "The mainframe changes its record format
overnight. The hive notices the drift and adapts." Keep the total near 140 words.

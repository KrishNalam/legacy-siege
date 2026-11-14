# 60-second video: voiceover script

Timed to the seven segments that `scripts/cut_video.py` cuts (the burned-in captions match): the six story
beats plus a 9 s **code** segment, because the rules want the video to show the code as well as the
functionality. About 145 words, a calm pace of ~2.5 words per second. Three ways to get the audio:

```bash
# 1. a human recording (best): read the table below over the silent cut, then
.venv/bin/python -m scripts.cut_video ~/projects/legacy-siege-media/beats-<ts>.json --voiceover vo.mp3
# 2. offline TTS fallback: speaks the "Plain text" section below, one paragraph per segment, each started at
#    its segment's first second (piper neural voice if set up, else espeak-ng, else ffmpeg's flite)
.venv/bin/python -m scripts.cut_video ~/projects/legacy-siege-media/beats-<ts>.json --tts
# 3. neither: a silent AAC track (the video is still valid, but a submission needs audio)
```

**Numbers:** the round-2 caption prints the take's real diagnosis times and hops (from the beat log's
`metrics`). The voiceover deliberately says only "one hop instead of three" so it stays true for any take.

| time | segment | on screen | voiceover |
|---|---|---|---|
| 0–7 s | **Hook** | Title card, then red lightning hits the mainframe and the estate turns amber | Tonight, a Chaos Monkey attacks a legacy bank. Twelve AI agents have to keep it alive. |
| 7–15 s | **Cascade** | One incident card; a blue pulse walks portal → unix → mainframe | The portal still says 200, with stale balances. A scout walks the blame chain, portal, unix, mainframe, to a crashed batch job. |
| 15–24 s | **The lie** | ACK ≠ APPLIED stamp on the mainframe step, attempt a2 goes green | A worker sends the fix. The mainframe says "applied". It's lying. The Guard reads the state back, rejects it, and tries again. |
| 24–33 s | **Real kill** | Beekeeper approval, the worker bee falls, a nurse picks up its ticket | Now we kill that worker mid-step, a real SIGKILL. A nurse takes its lease and resumes from the checkpoint in MongoDB. |
| 33–42 s | **Code** | Three code cards, 3 s each: `swarm/leases.py` (the atomic `find_one_and_update` lease claim), `swarm/verify.py` (the Guard's read-back checks), `swarm/schema.py` (recall of a learned schema mapping) | Under the hood: one atomic update claims the lease. The Guard reads state back, never trusting an ACK. MongoDB remembers every fix. |
| 42–51 s | **Learning** | Golden recall beam; 1 hop; Learning chart round 1 vs 2 | Night two, same attack. Vector search recalls last night's playbook: one hop instead of three, and a much faster diagnosis. |
| 51–60 s | **Fix for good** | Gardener card v1 → v2; night 3 stays green; Atlas collections view | Then the Gardener patches the code itself, tested and approved. Night three: nothing happens. MongoDB is the hive's memory. |

## Plain text (for TTS)

One paragraph per segment; the `<!-- vo:key -->` tag above each says which segment it is spoken in
(`scripts/narration.py` reads this section, so keep the tags).

<!-- vo:hook -->
Tonight, a Chaos Monkey attacks a legacy bank. Twelve AI agents have to keep it alive.

<!-- vo:cascade -->
The portal still says 200, with stale balances. A scout walks the blame chain, portal, unix, mainframe,
to a crashed batch job.

<!-- vo:lie -->
A worker sends the fix. The mainframe says "applied". It's lying. The Guard reads the state back,
rejects it, and tries again.

<!-- vo:kill -->
Now we kill that worker mid-step, a real SIGKILL. A nurse takes its lease and resumes from the
checkpoint in MongoDB.

<!-- vo:code -->
Under the hood: one atomic update claims the lease. The Guard reads state back, never trusting an ACK.
MongoDB remembers every fix.

<!-- vo:learn -->
Night two, same attack. Vector search recalls last night's playbook: one hop instead of three, and a much
faster diagnosis.

<!-- vo:garden -->
Then the Gardener patches the code itself, tested and approved. Night three: nothing happens. MongoDB is
the hive's memory.

## Recording and cutting

With a stack running (`run.py`) and `MONGODB_URI`/`WORLD_DB`/`SWARM_DB` pointing at the same databases:

```bash
.venv/bin/python -m scripts.record_demo --base http://127.0.0.1:8700      # ~3-5 min; runs demo_check's 26 checks
.venv/bin/python -m scripts.cut_video ~/projects/legacy-siege-media/beats-<ts>.json --print-plan
.venv/bin/python -m scripts.cut_video ~/projects/legacy-siege-media/beats-<ts>.json \
    --out ~/projects/legacy-siege-media/legacy-siege-60s.mp4 [--voiceover vo.mp3 | --tts] [--layout doc] [--no-code]
```

- Output goes to `~/projects/legacy-siege-media/` (not the repo): `raw-<ts>.webm` (1920×1080) and
  `beats-<ts>.json` (beats with `t_start`/`t_end` in video seconds, raw events, demo_check results,
  `demo_ok`). Only cut a take whose `demo_ok` is true.
- `record_demo` ends with a full reset (Gardener revert) unless `--keep`. It needs Playwright Chromium
  (newest `~/.cache/ms-playwright/chromium-*`, launched with `--no-sandbox`) and `cut_video` needs a full
  ffmpeg (libx264, drawtext) on PATH.
- The Atlas view prints the cluster host: record against the Atlas-backed stack for the final take.
- **Code segment** (`--code`, default on; `--no-code` restores the old 7/10/9/10/12/12 s cut): three cards
  from `scripts/code_cards.py`, rendered from the repo at cut time (so they can't go stale) with Pillow, which
  `cut_video` fetches via `uv run --no-project --with pillow` if the venv has none. Edit the `CARDS` table
  there to change a file, an excerpt (anchor + line count), the highlighted lines or the caption. A card whose
  file isn't found is skipped (`swarm/schema.py` is read from `origin/t3/add-schema-drift-adaptation` until
  that branch is merged); the 9 s are split between the cards that remain.
- **`--layout doc`** is the design doc's cut with the code segment folded in: 5/12/8/11/9/9/6 s.
- **`--tts`**: `scripts/narration.py` speaks the "Plain text" section above, each paragraph starting
  0.3 s into its segment (sped up a little if it runs long; the cut warns past ×1.25: shorten the paragraph).
  Engines, first usable wins: `piper` (neural, private venv + voice model in `~/projects/legacy-siege-media/tts`,
  setup commands in the module docstring), `espeak-ng`, ffmpeg's `flite`. `python -m scripts.narration --list`
  shows what this machine has. A human `--voiceover` always wins over `--tts`.
- Check the audio is real: `ffmpeg -i out.mp4 -af volumedetect -vn -f null -` should report a `mean_volume`
  well above -40 dB (about -20 dB for the TTS mix).

## After the code freeze (schema-drift beat)

`cut_video.py` has a commented slot for a schema-drift beat after "Real kill" (and the code segment). When it
exists, take ~5 s from "Learning" and add one line here and a `<!-- vo:drift -->` paragraph, e.g. "The
mainframe changes its record format overnight. The hive notices the drift and adapts." Keep the total near
145 words. (The code segment already shows the schema mapping recall from `swarm/schema.py`.)

# 60-second video: script and voiceover

**Pitch: agents first.** The video is about AI agents attacking and patching a legacy estate. The war room
is a live view of those agents (always "the war room", never "dashboard"). Claude's role, worded the same
way every time: *Claude diagnoses each incident and proposes the fix; when the safety checks confirm it,
Claude's plan is what runs; unsafe ideas get overruled on screen.* (Never "Claude freely drives".)

About 150 words over 60 s (~2.5 words per second). The plain narration for TTS is also in
`docs/video-narration.txt`. MongoDB features named: **change streams, leases, Atlas as the hive's memory**
(also vector-search recall and checkpoints).

| time | beat | on screen | voiceover |
|---|---|---|---|
| 0–5 s | **Hook** | Title card "LEGACY SIEGE", then the war room: bees around the estate, red lightning hits the mainframe, the estate turns amber | Twelve AI agents defend a legacy bank. AI attackers try to break it. |
| 5–10 s | **Cascade** | One incident card; a blue pulse walks portal → unix → mainframe and stops on the crashed batch job | A scout traces the outage: portal, unix, mainframe, to a crashed batch job. |
| 10–19 s | **Claude, checked** | The AI card: Claude's ranked hypotheses and plan, the P1–P6 safety checks ticking green, "CLAUDE'S PLAN RUNS"; then an unsafe op struck through with its rule ("overruled: P3 blast radius") | Claude diagnoses each incident and proposes the fix. When the safety checks confirm it, Claude's plan is what runs. Unsafe ideas get overruled, on screen. |
| 19–24 s | **The lie** | `ACK ≠ APPLIED` stamp on the mainframe step; attempt a2 goes green | The mainframe says the fix applied. It's lying. The Guard reads it back and catches it. |
| 24–30 s | **Kill + Nurse** | Worker bee falls (real SIGKILL), its lease goes grey, a Nurse picks it up and the step resumes | We kill a worker mid-fix. A nurse takes its lease and resumes from MongoDB. |
| 30–38 s | **Code** | Three code cards, ~2.7 s each (`scripts/code_cards.py`): `agents/leases.py` `claim()` (atomic `find_one_and_update` with the fencing `$inc`), `agents/verify.py` `check()` (the Guard's read-back), `agents/schema.py` `remembered()` (learned mapping recalled from MongoDB) | In code: one atomic update claims a lease. The Guard trusts read-backs, never ACKs. Learned mappings live in Atlas. |
| 38–44 s | **Round 2** | Golden recall beam, 1 hop; the learning chart round 1 vs round 2 (real diagnosis times from the take) | Round two, same attack. MongoDB memory recalls the fix: one hop instead of three. |
| 44–49 s | **Schema drift** | Sentinel: "unix feed schema changed: balance missing, acct_bal new"; caption "MAPPING LEARNED", then on the repeat "MAPPING REUSED" | Overnight, a field gets renamed. The hive adapts, and remembers the mapping. |
| 49–55 s | **Red fleet** | A Red striker's loop (observe → think → act → reflect); it FREEZEs a worker; the lease expires and a Nurse takes over (~11 s on the take's clock) | Red strikers watch, think, strike and reflect. One freezes an agent; eleven seconds later, a nurse takes over. |
| 55–60 s | **Close** | Pull back to the whole war room going green, MongoDB spine honeycomb lit, Atlas collections view with the cluster host | Every frame is a MongoDB change stream. Atlas is the hive's memory. |

**Numbers on screen** come from the take (beat log `metrics`), so captions stay true. The voiceover only
says "one hop instead of three" and "eleven seconds" (`LEASE_S` = 10 s dead-agent detection + pickup);
re-check the second figure on the final take.

## Plain text (for TTS)

One paragraph per beat; the `<!-- vo:key -->` tag above each names its segment (`scripts/narration.py`
reads this section, so keep the tags). `docs/video-narration.txt` is the same text without tags.

<!-- vo:hook -->
Twelve AI agents defend a legacy bank. AI attackers try to break it.

<!-- vo:cascade -->
A scout traces the outage: portal, unix, mainframe, to a crashed batch job.

<!-- vo:ai -->
Claude diagnoses each incident and proposes the fix. When the safety checks confirm it, Claude's plan is
what runs. Unsafe ideas get overruled, on screen.

<!-- vo:lie -->
The mainframe says the fix applied. It's lying. The Guard reads it back and catches it.

<!-- vo:kill -->
We kill a worker mid-fix. A nurse takes its lease and resumes from MongoDB.

<!-- vo:code -->
In code: one atomic update claims a lease. The Guard trusts read-backs, never ACKs. Learned mappings
live in Atlas.

<!-- vo:learn -->
Round two, same attack. MongoDB memory recalls the fix: one hop instead of three.

<!-- vo:drift -->
Overnight, a field gets renamed. The hive adapts, and remembers the mapping.

<!-- vo:red -->
Red strikers watch, think, strike and reflect. One freezes an agent; eleven seconds later, a nurse takes
over.

<!-- vo:garden -->
Every frame is a MongoDB change stream. Atlas is the hive's memory.

## Cutting it

`scripts/cut_video.py` cuts these ten beats by default (`--layout script`, the table's times; `--layout brief`
keeps the older seven-segment cut). `--tts` speaks each tagged paragraph at the start of its segment. The
new beats come from `record_demo` markers: **Claude, checked** needs an `AI_DRIVES` stack (`incident.ai`
verdicts), **Schema drift** needs `record_demo --drift` (one bank-drift round after night 3), **Red fleet**
needs a `RED_FLEET=1` stack from `t3/build-red-fleet-attacker` (an `agent_freeze` by a `red-*` striker, then
the Nurse handoff). A beat the take lacks is cut from a fallback window with a plainer caption (`FALLBACK`
in `cut_video.py`, flagged "(fallback)" in `--print-plan`); `--at red_freeze=212-220` points a beat at the
right seconds of the raw video by hand.

```bash
.venv/bin/python -m scripts.record_demo --base http://127.0.0.1:8700 --drift   # ~4-6 min; runs demo_check
.venv/bin/python -m scripts.cut_video ~/projects/legacy-siege-media/beats-<ts>.json --print-plan
.venv/bin/python -m scripts.cut_video ~/projects/legacy-siege-media/beats-<ts>.json \
    --out ~/projects/legacy-siege-media/legacy-siege-60s.mp4 [--voiceover vo.mp3 | --tts] [--at BEAT=A-B] [--no-code]
```

- Output goes to `~/projects/legacy-siege-media/` (not the repo): `raw-<ts>.webm` (1920×1080) and
  `beats-<ts>.json`. Only cut a take whose `demo_ok` is true. Record against the Atlas-backed stack (the
  close shows the cluster host).
- `record_demo` needs Playwright Chromium; `cut_video` needs a full ffmpeg (libx264, drawtext) on PATH.
- Code cards are rendered from the repo at cut time (`scripts/code_cards.py`, `CARDS` table), so they
  can't go stale.
- `--tts` engines, first usable wins: `piper`, `espeak-ng`, ffmpeg's `flite`
  (`python -m scripts.narration --list`). A paragraph that runs past ×1.25 of its slot gets a warning:
  shorten it. A human `--voiceover` always wins over `--tts`.
- Check the audio: `ffmpeg -i out.mp4 -af volumedetect -vn -f null -` should report a `mean_volume` well
  above -40 dB.

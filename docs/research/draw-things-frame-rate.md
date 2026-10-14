# Draw Things frame rate

What Framecontrol can establish about a Draw Things model's frame rate before a
job runs. Phase 4 Milestone 09 accepts `knobs.seconds` for a Draw Things video
run only when that rate is known; this note records why none is, and how one
becomes known.

## What the project has

- The saved [`draw-things-cli generate --help`](draw-things-cli-generate-help.txt)
  defines a video's duration as its frames divided by the model's FPS and takes
  `--frames`. It has no seconds option and does not say any model's FPS.
- No configuration under `data/params/` defines a frame rate. A search of the
  maintained files for `fps`, `frameRate`, and `framesPerSecond` finds nothing,
  and Draw Things configurations carry `numFrames`, a count.
- A run's output shows its rate only after the run: the manifest and history
  record `output_frame_rate` as a measured fact, and one Wan 2.2 configuration
  has produced 16 fps files. That is an observation of one output, not a rule
  that holds for another model or CLI version, and a planned frame count must
  not come from a file that does not exist yet.

## Decision (2026-10-14)

`DRAW_THINGS_FRAME_RATES` in `jobs/backends/draw_things.py` maps a model file
name to its nominal rate and the source that established it. It ships empty, so
a Draw Things `knobs.seconds` is refused for every model with a message
pointing to `knobs.frames`, and `GET /v1/capabilities` lists no model under
`job_format.draw_things_seconds_models`. The adapter never reads an `fps`-like
key from a configuration and never infers a rate from a finished output.

An entry may be added only together with a note here that records, for that
model: how the rate was measured (the CLI version and the files it wrote), the
frame counts tried, whether the child produced the count it was asked for, and
whether the model accepts every count or only some (Wan-style models may need
`4n + 1`, which Framecontrol does not assume). Until then `frames` is passed
exactly, and the count the child produced is a measured output fact beside it.

## Test

A test injects a frame-rate table into the adapter to prove the accepted path:
`seconds` becomes `llround(seconds * fps)` frames, the run records `nominal_fps`
with its source, and a model missing from the table still fails.

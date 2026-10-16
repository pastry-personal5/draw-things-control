"""The built-in reference runs: what a backend's run is priced from on a host that has timed none of its runs yet.

Each is one run assembled from the measurements recorded in ``docs/research/time-estimation.md``, so the forecast has a
number from a run's first moment, with a wide range, until the host's own runs replace it. Only H3 has one: the Draw Things
runs measured there do not record their size or model, and its image modes run in seconds where its video modes take half
an hour, so a built-in Draw Things run would mislead more than it helped. Nothing here reads a file, a database, or the
clock."""

from __future__ import annotations

from functools import cache

from framecontrol.jobs.timing.features import FRAMECONTROL, HistoryRun, HistoryStage

H3 = "h3"
# Eight 243-frame 832×448 `r2av` runs (E0040 run 1, E0041 to E0047) took 63.5 to 74.3 s per step.
H3_STEP_SECONDS = 69.0
H3_STEPS = 30
# Setup up to the last `load transformer core` reading took 17.5 to 26.6 s, split here across its phases; only the total
# was measured.
H3_SETUP = (("tokenize", 0.2, 1), ("video_encode", 3.0, 9), ("vision", 4.0, 1), ("text_encode", 5.0, 50), ("refine_text", 2.5, 1), ("adaln", 1.5, 1), ("load_core", 5.8, 50))
# Audio VAE, video VAE load, and FFmpeg after the last step took 66 to 142 s, the longer for the longer clips; split here
# with FFmpeg counting the frames.
H3_TAIL = (("audio_decode", 20.0, None, None), ("video_load", 10.0, None, None), ("ffmpeg", 100.0, 243, "frames"))
# Framecontrol before the child, and after it without color correction (1 to 2 s).
H3_PREPARE_SECONDS, H3_FINISH_SECONDS = 1.0, 2.0


def _h3_run() -> HistoryRun:
    """A 243-frame 832×448 `r2av` run of 30 steps at the measured pace: about 2230 s, as E0047's 2310 s and its siblings'
    2055 to 2396 s."""
    workload = {"pipeline": "r2av", "width": 832, "height": 448, "pixels": 832 * 448, "frames": 243, "steps": H3_STEPS}
    stages = [HistoryStage("prepare", FRAMECONTROL, 0.0, H3_PREPARE_SECONDS)]
    cursor = H3_PREPARE_SECONDS
    for name, seconds, units in H3_SETUP:
        stages.append(HistoryStage(name, "child", cursor, cursor + seconds, units, units))
        cursor += seconds
    marks = tuple((cursor + index * H3_STEP_SECONDS, index) for index in range(H3_STEPS + 1))
    stages.append(HistoryStage("denoise", "child", cursor, marks[-1][0], H3_STEPS, H3_STEPS, "steps", marks))
    cursor = marks[-1][0]
    for name, seconds, units, unit in H3_TAIL:
        stages.append(HistoryStage(name, "child", cursor, cursor + seconds, units, units, unit))
        cursor += seconds
    stages.append(HistoryStage("verify", FRAMECONTROL, cursor, cursor + H3_FINISH_SECONDS))
    return HistoryRun(0, 1, "builtin", H3, "r2av", workload, {}, tuple(stages))


@cache
def reference_runs(backend: str) -> tuple[HistoryRun, ...]:
    """The built-in runs of ``backend``; none for a backend that has no measurements to build one from."""
    return (_h3_run(),) if backend == H3 else ()

"""The frozen copy of the TUI's old estimator: it matches the code it was taken from, and it keeps the faults the forecast replaces."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from typing import Any

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.core.process.output import OutputProcessor
from framecontrol.jobs.events import CooldownEnded, CooldownStarted, JobEvent, JobFinished, JobStarted, JobStatus, RunFinished, RunOutput, RunStarted, RunStatus
from framecontrol.tui.estimate import job_estimate, run_estimate
from framecontrol.tui.live_run import LiveRun, PastRun
from tests.timing.frozen_estimator import FrozenEstimator, line_of
from tests.timing.frozen_estimator import PastRun as FrozenPastRun

WALL = datetime(2026, 9, 26, 14, 0, 0)
AT = "2026-09-26T14:00:00+00:00"


def job_started(runs: int, cooldown: CooldownPolicy, first_run: int = 1) -> JobStarted:
    return JobStarted(at=AT, job_name="walk", job_file="/jobs/walk.yaml", source_text="", mode="i2v", total_runs=runs, output_directory="/out", input=None, model="base.ckpt", seed=1, seed_source="job", cooldown=cooldown, cooldown_source="job", manifest=None, log=None, first_run=first_run)


def run_started(number: int, total: int) -> RunStarted:
    return RunStarted(at=AT, number=number, total=total, pair="only", positive="walk", negative=None, input=None, resized_input=None, output=f"walk-{number}.mov", last_frame=None, command=())


def reading(number: int, stage: str, phase: str, done: int | None = None, total: int | None = None) -> RunOutput:
    """What the executor emits for a stage reading: the stage and counter, and no text of its own."""
    return RunOutput(at=AT, number=number, stream="stdout", text="", progress=None, percent=None, phase=phase, offset=0.0, stage=stage, done=done, total=total)


def run_finished(number: int, seconds: float) -> RunFinished:
    return RunFinished(at=AT, number=number, status=RunStatus.SUCCEEDED, exit_code=0, seconds=seconds, output=f"walk-{number}.mov", last_frame=None)


class Script:
    """One job's events with the time each arrives, played into the old code and the frozen copy alike."""

    def __init__(self, *, runs: int = 3, cooldown: CooldownPolicy | None = None, past: tuple[float, int | None] | None = None) -> None:
        self.cooldown = cooldown or CooldownPolicy("manual", seconds=100.0)
        self.runs = runs
        self.now = 0.0
        self.past = past
        self.old = LiveRun(clock=lambda: self.now, wall_clock=lambda: WALL + timedelta(seconds=self.now))
        if past is not None:
            self.old.past_run = PastRun(*past)
        self.frozen = FrozenEstimator(past_run=FrozenPastRun(*past) if past is not None else None)
        self.compared: list[float] = []
        self.play(0.0, job_started(runs, self.cooldown))

    def play(self, now: float, event: JobEvent) -> None:
        self.now = now
        if isinstance(event, RunOutput):
            text = line_of(event)
            old_event = RunOutput(at=event.at, number=event.number, stream=event.stream, text=text, progress=OutputProcessor._find_progress(text), percent=OutputProcessor._find_percent(text), phase=event.phase, offset=event.offset)
        else:
            old_event = event
        self.old.apply(old_event)
        self.frozen.observe(event, now)

    def compare(self, *times: float) -> None:
        for moment in times:
            self.now = moment
            expected_run, expected_job = run_estimate(self.old, moment), job_estimate(self.old, moment)
            actual_run, actual_job = self.frozen.run_estimate(moment), self.frozen.job_estimate(moment)
            self.compared.append(moment)
            self.assertions.assertEqual((actual_run.fraction, actual_run.remaining, actual_run.finishing), (expected_run.fraction, expected_run.remaining, expected_run.finishing), f"run at {moment}")
            self.assertions.assertEqual((actual_job.fraction, actual_job.remaining), (expected_job.fraction, expected_job.remaining), f"job at {moment}")

    assertions: unittest.TestCase


def draw_things_run(script: Script, number: int, begin: float, *, load: float = 30.0, per_step: float = 10.0, steps: int = 40, tail: float = 20.0, check_every: int = 7) -> float:
    """A Draw Things run as the executor's events: Starting, Processing, 40 Sampling readings, Finishing; returns when it finished."""
    script.play(begin, run_started(number, script.runs))
    script.play(begin + 1, reading(number, "starting", "Starting"))
    script.play(begin + 2, reading(number, "processing", "Processing"))
    for step in range(steps):
        moment = begin + load + step * per_step
        script.play(moment, reading(number, "sampling", "Sampling", step, steps))
        if step % check_every == 0:
            script.compare(moment, moment + per_step / 2)
    script.play(begin + load + steps * per_step, reading(number, "finishing", "Finishing"))
    end = begin + load + steps * per_step + tail
    script.compare(begin + load + steps * per_step + 1, end - 1)
    script.play(end, run_finished(number, load + steps * per_step))
    return end


class FrozenMatchesTheOldEstimatorTests(unittest.TestCase):
    def script(self, **options: Any) -> Script:
        script = Script(**options)
        script.assertions = self
        return script

    def test_a_job_of_draw_things_runs_with_a_manual_wait(self) -> None:
        script = self.script()
        end = draw_things_run(script, 1, 0.0)
        script.play(end, CooldownStarted(at=AT, after_run=1, seconds=100.0, until="14:00:00"))
        script.compare(end + 10, end + 90)
        script.play(end + 100, CooldownEnded(at=AT, waited_seconds=100.0, cut_short=False))
        end = draw_things_run(script, 2, end + 100)
        script.play(end, CooldownStarted(at=AT, after_run=2, seconds=100.0, until="14:00:00"))
        script.play(end + 100, CooldownEnded(at=AT, waited_seconds=100.0, cut_short=False))
        draw_things_run(script, 3, end + 100)
        self.assertGreater(len(script.compared), 20)

    def test_a_job_with_an_auto_wait_and_a_past_run(self) -> None:
        script = self.script(cooldown=CooldownPolicy("auto", ratio=0.5), past=(430.0, 40))
        script.compare(0.0, 5.0)
        script.play(1.0, run_started(1, 3))
        script.compare(1.0, 60.0, 200.0)
        script.play(6.0, reading(1, "starting", "Starting"))
        script.play(300.0, reading(1, "sampling", "Sampling", 0, 40))
        script.compare(300.0, 305.0)
        script.play(330.0, reading(1, "sampling", "Sampling", 3, 40))
        script.compare(330.0, 400.0)

    def test_an_h3_run_reads_every_phase_counter_as_progress(self) -> None:
        script = self.script(runs=2, cooldown=CooldownPolicy("off"))
        script.play(0.0, run_started(1, 2))
        script.compare(0.0, 10.0)
        for moment, stage, phase, done, total in ((0.5, "tokenize", "tokenizer", 0, 1), (0.7, "tokenize", "tokenizer", 1, 1), (3.0, "text_encode", "text encoder", 25, 50), (6.0, "text_encode", "text encoder", 50, 50), (40.0, "denoise", "denoise enqueue", 0, 30), (100.0, "denoise", "denoise enqueue", 1, 30), (160.0, "denoise", "denoise enqueue", 2, 30)):
            script.play(moment, reading(1, stage, phase, done, total))
            script.compare(moment, moment + 3)

    def test_a_counter_that_goes_back_and_a_stop(self) -> None:
        script = self.script()
        script.play(0.0, run_started(1, 3))
        for moment, done in ((10.0, 9), (20.0, 10), (30.0, 11), (40.0, 0), (45.0, 1)):
            script.play(moment, reading(1, "sampling", "Sampling", done, 40))
            script.compare(moment)
        script.old.request_stop()
        script.frozen.request_stop(45.0)
        script.compare(60.0)
        script.play(70.0, JobFinished(at=AT, status=JobStatus.INTERRUPTED, exit_code=130, completed_runs=0, total_runs=3, signal="SIGINT"))
        script.compare(80.0)

    def test_a_resumed_job_counts_only_the_runs_it_will_execute(self) -> None:
        script = self.script(runs=4)
        script.play(0.0, job_started(4, CooldownPolicy("off"), first_run=3))
        script.play(1.0, run_started(3, 4))
        script.compare(1.0, 50.0)


class FrozenKeepsTheFaultsTests(unittest.TestCase):
    """Milestone 10's evidence of what the step-counter heuristics get wrong, kept so the replay measures the change."""

    def estimator(self) -> FrozenEstimator:
        estimator = FrozenEstimator()
        estimator.observe(job_started(2, CooldownPolicy("off")), 0.0)
        estimator.observe(run_started(1, 2), 0.0)
        return estimator

    def test_an_early_h3_counter_that_is_already_complete_reads_as_finishing(self) -> None:
        estimator = self.estimator()
        estimator.observe(reading(1, "tokenize", "tokenizer", 1, 1), 0.7)
        run = estimator.run_estimate(1.0)
        self.assertTrue(run.finishing)
        self.assertEqual((run.fraction, run.remaining), (1.0, 0.0))

    def test_draw_things_last_step_line_reads_as_the_end_of_the_run(self) -> None:
        estimator = self.estimator()
        estimator.observe(reading(1, "sampling", "Sampling", 39, 40), 100.0)
        # `Sampling... 40 / 40` is printed as the last step begins, and the old reading takes it for the end.
        self.assertTrue(estimator.run_estimate(101.0).finishing)

    def test_the_first_run_is_priced_from_the_latest_run_of_any_job(self) -> None:
        estimator = FrozenEstimator(past_run=FrozenPastRun(430.0, 40))
        estimator.observe(job_started(2, CooldownPolicy("off")), 0.0)
        estimator.observe(run_started(1, 2), 0.0)
        self.assertEqual(estimator.run_estimate(30.0).remaining, 400.0)

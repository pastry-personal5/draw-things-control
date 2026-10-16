"""The worker's side of the forecast: it feeds each job event to the forecaster, decides which forecasts go out, and holds
the share a bar may not fall below.

A forecast is published at a stage, run, or cooldown change, when a park is reserved or withdrawn (the job's figures depend
on it), and, within a stage, whenever its units advanced, at most once every 2 s and at the stage's last reading, so a
243-frame FFmpeg counter cannot flood the event backlog. One identical to the last in stage, units, park state, and ends
(within a second) is not republished, so a completed step always shows. The server sets ``floor`` to the largest share it
has published or the last forecast would have shown at ``as_of``, so a bar never moves back within a run or a job, a
forecast that grows holds the bar still until the time catches up, and a client that attaches mid-run draws the same bar
with no history.

An exception in the model is logged once and ends this entry's forecasts; it never reaches the worker."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace

from loguru import logger

from framecontrol.jobs.definition import JobDefinition
from framecontrol.jobs.events import JobEvent, JobFinished, RunOutput, RunStarted, StageFinished, TimingFinished
from framecontrol.jobs.executor import JobExecutor
from framecontrol.jobs.timing.forecast import Forecast, Forecaster, JobPlan, PlannedRun, Span
from framecontrol.services.forecast_view import progress

# Within a stage, a reading is published at most this often, apart from the stage's last.
MIN_INTERVAL_SECONDS = 2.0
# Ends this close are the same end.
SAME_END_SECONDS = 1.0


def job_plan(executor: JobExecutor, job: JobDefinition, *, first_run: int, executable: str) -> JobPlan:
    """The runs an entry will execute, from ``first_run`` (a resumed entry only those it will run), each described as its
    timing will record it, and the job's cooldown policy."""
    runs = []
    for number in range(first_run, job.run_count + 1):
        described = executor.describe_run(job, number, executable=executable)
        runs.append(PlannedRun(number, described.backend, described.pipeline, described.workload, described.environment))
    return JobPlan(tuple(runs), job.cooldown)


class ForecastPublisher:
    """One entry's forecasts. The worker's thread feeds it events while a request thread may park or cancel the entry, so a
    lock keeps its state to one at a time."""

    def __init__(self, forecaster: Forecaster, publish: Callable[[Forecast | None], None], clock: Callable[[], float] = time.time) -> None:
        self._forecaster = forecaster
        self._publish = publish
        self._clock = clock
        self._last: Forecast | None = None
        self._parking = False
        self._parking_at_last = False
        self._last_reading_at: float | None = None
        self._run_floor = 0.0
        self._job_floor = 0.0
        self._done = False
        self._lock = threading.Lock()

    @property
    def latest(self) -> Forecast | None:
        """The forecast last published, or None before the first, after a cancel, and after the job."""
        return self._last

    def observe(self, event: JobEvent, now: float | None = None) -> None:
        """Feed a job event, at ``now`` (the clock, by default), and publish what it makes worth publishing."""
        if self._done:
            return
        moment = self._clock() if now is None else now
        with self._lock:
            try:
                self._observe(event, moment)
            except Exception:
                logger.exception("The forecast failed; this entry runs without one")
                self._finish()

    def park_changed(self, parking: bool, now: float | None = None) -> None:
        """A park was reserved or withdrawn: the job's figures depend on it, so a forecast goes out at once."""
        if self._done:
            return
        moment = self._clock() if now is None else now
        with self._lock:
            try:
                self._parking = parking
                self._forecaster.set_parking(parking)
                forecast = self._forecaster.forecast(moment)
                if forecast is not None:
                    self._send(forecast, moment)
            except Exception:
                logger.exception("The forecast failed; this entry runs without one")
                self._finish()

    def cancel(self) -> None:
        """A cancel was accepted: the forecast is cleared."""
        with self._lock:
            self._forecaster.cancel()
            self._finish()

    def _observe(self, event: JobEvent, now: float) -> None:
        self._forecaster.observe(event, now)
        if isinstance(event, JobFinished):
            self._finish()
            return
        if isinstance(event, RunStarted):
            self._run_floor = 0.0
            self._last = replace(self._last, run=None) if self._last is not None else None
        if isinstance(event, StageFinished | TimingFinished):
            return
        forecast = self._forecaster.forecast(now)
        if forecast is None:
            return
        if isinstance(event, RunOutput):
            if not self._advanced(forecast):
                return
            last_reading = forecast.stage_total is not None and forecast.stage_done == forecast.stage_total
            if not last_reading and self._last_reading_at is not None and now - self._last_reading_at < MIN_INTERVAL_SECONDS:
                return
            self._last_reading_at = now
        elif self._last_reading_at is not None and self._stage_changed(forecast):
            self._last_reading_at = None
        if self._identical(forecast):
            return
        self._send(forecast, now)

    def _advanced(self, forecast: Forecast) -> bool:
        last = self._last
        return last is None or (forecast.stage, forecast.stage_done, forecast.stage_total) != (last.stage, last.stage_done, last.stage_total)

    def _stage_changed(self, forecast: Forecast) -> bool:
        return self._last is None or forecast.stage != self._last.stage

    def _identical(self, forecast: Forecast) -> bool:
        last = self._last
        if last is None or (forecast.stage, forecast.stage_done, forecast.stage_total) != (last.stage, last.stage_done, last.stage_total) or self._parking != self._parking_at_last:
            return False
        return self._same_end(forecast.run, last.run) and self._same_end(forecast.job, last.job)

    @staticmethod
    def _same_end(new: Span | None, old: Span | None) -> bool:
        if new is None or old is None:
            return new is old
        return abs(new.end_at - old.end_at) <= SAME_END_SECONDS

    def _send(self, forecast: Forecast, now: float) -> None:
        run = self._held(forecast.run, self._last.run if self._last else None, now, "_run_floor")
        job = self._held(forecast.job, self._last.job if self._last else None, now, "_job_floor")
        published = replace(forecast, run=run, job=job)
        self._last = published
        self._parking_at_last = self._parking
        self._publish(published)

    def _held(self, span: Span | None, shown: Span | None, now: float, floor_name: str) -> Span | None:
        """``span`` with the floor the bar may not fall below: what was held, what the last forecast showed at ``now``, and
        the share this one puts at ``now``."""
        if span is None:
            return None
        length = span.end_at - span.started_at
        share = min(1.0, max(0.0, (now - span.started_at) / length)) if length > 0 else 0.0
        floor = max(getattr(self, floor_name), share)
        if shown is not None and shown.started_at == span.started_at:
            floor = max(floor, progress(shown.as_dict(), now))
        setattr(self, floor_name, floor)
        return replace(span, floor=floor)

    def _finish(self) -> None:
        had = self._last is not None
        self._done = True
        self._last = None
        if had:
            self._publish(None)

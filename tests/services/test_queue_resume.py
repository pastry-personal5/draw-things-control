"""Tests for resolving and accepting a resume."""

from __future__ import annotations

import os
from datetime import datetime

from draw_things_control.services.queue_resume import ResumeRefused, resume_entry
from draw_things_control.services.queue_submit import submit_job
from draw_things_control.state.executions import ExecutionSettings, NewExecution, NewRun
from draw_things_control.state.queue import QueueState
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data

NOW = datetime(2026, 9, 27, 15, 30, 12)


class QueueResumeTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(self.store.close)

    def submit(self, run_count: int = 7):
        path = self.write_job(job_data(run_count=run_count, prompt_pairs=[{"name": "only", "positive": "text"}]))
        return submit_job(path, self.global_config, self.params, self.store)

    def succeed_three_of_seven(self, entry_id: int, *, state: QueueState = QueueState.INTERRUPTED) -> tuple[int, str]:
        """Claim ``entry_id``, give it an execution with three succeeded runs of seven, and leave it ``state``."""
        claimed = self.store.queue.claim_oldest(NOW)
        assert claimed is not None and claimed.id == entry_id
        last_frame = self.output_directory / "last-frame-3.png"
        last_frame.parent.mkdir(parents=True, exist_ok=True)
        last_frame.write_bytes(b"png")
        execution_row = self.store.executions.start(NewExecution(job_name="sunset-walk", job_file="job.yaml", mode="i2v", started_at="2026-09-27T10:00:00+00:00", seed=42, settings=ExecutionSettings(output_directory=str(self.output_directory))))
        for number in (1, 2, 3):
            self.store.executions.start_run(execution_row, number, NewRun(pair="only", positive="text", started_at="2026-09-27T10:00:00+00:00", status="succeeded", output=f"run-{number}.mov", last_frame=last_frame.name if number == 3 else None))
        self.store.executions.finish(execution_row, status=str(state), exit_code=None, signal=None, finished_at="2026-09-27T10:10:00+00:00")
        execution_number = self.store.executions.number_of(execution_row)
        assert execution_number is not None
        self.store.queue.link_execution(entry_id, execution_number)
        self.store.queue.finish(entry_id, state=state, finished_at="2026-09-27T10:10:00+00:00")
        return execution_number, str(last_frame)

    def test_a_resume_of_three_succeeded_of_seven_starts_at_run_4(self) -> None:
        entry = self.submit(run_count=7)
        execution_number, last_frame = self.succeed_three_of_seven(entry.id)
        resumed = resume_entry(self.store, entry.id, self.global_config, self.params, clock=lambda: NOW)
        self.assertEqual((resumed.resumes, resumed.resumes_execution, resumed.resume_first_run, resumed.resume_input, resumed.resume_seed), (entry.queue_number, execution_number, 4, last_frame, 42))
        self.assertEqual(resumed.state, str(QueueState.QUEUED))

    def test_a_resume_is_refused_when_the_entry_is_not_in_a_resumable_state(self) -> None:
        entry = self.submit(run_count=1)
        with self.assertRaisesRegex(ResumeRefused, "queued"):
            resume_entry(self.store, entry.id, self.global_config, self.params, clock=lambda: NOW)

    def test_a_second_resume_of_the_same_entry_is_refused(self) -> None:
        entry = self.submit(run_count=7)
        self.succeed_three_of_seven(entry.id)
        resume_entry(self.store, entry.id, self.global_config, self.params, clock=lambda: NOW)
        with self.assertRaisesRegex(ResumeRefused, "already has a resume"):
            resume_entry(self.store, entry.id, self.global_config, self.params, clock=lambda: NOW)

    def test_a_resume_is_refused_with_no_succeeded_run(self) -> None:
        entry = self.submit(run_count=7)
        claimed = self.store.queue.claim_oldest(NOW)
        assert claimed is not None
        self.store.queue.finish(entry.id, state=QueueState.FAILED, finished_at="2026-09-27T10:10:00+00:00")
        with self.assertRaisesRegex(ResumeRefused, "no succeeded run"):
            resume_entry(self.store, entry.id, self.global_config, self.params, clock=lambda: NOW)

    def test_a_resume_is_refused_when_the_last_frame_is_gone(self) -> None:
        entry = self.submit(run_count=7)
        _number, last_frame = self.succeed_three_of_seven(entry.id)
        os.remove(last_frame)
        with self.assertRaisesRegex(ResumeRefused, "is gone"):
            resume_entry(self.store, entry.id, self.global_config, self.params, clock=lambda: NOW)

    def test_a_resume_is_refused_when_the_jobs_own_first_input_is_gone(self) -> None:
        entry = self.submit(run_count=7)
        self.succeed_three_of_seven(entry.id)
        os.remove(self.input_directory / "first-frame.png")
        with self.assertRaisesRegex(ResumeRefused, "first-frame.png"):
            resume_entry(self.store, entry.id, self.global_config, self.params, clock=lambda: NOW)

    def test_a_resume_is_refused_when_the_execution_was_pruned_distinct_from_no_succeeded_run(self) -> None:
        entry = self.submit(run_count=7)
        self.succeed_three_of_seven(entry.id)
        self.store.executions.prune(float("inf"))
        with self.assertRaisesRegex(ResumeRefused, "was pruned") as caught:
            resume_entry(self.store, entry.id, self.global_config, self.params, clock=lambda: NOW)
        # Distinct from "no succeeded run": this entry did have one, but it is gone now, not never-happened.
        self.assertNotIn("no succeeded run", str(caught.exception))

    def test_a_resume_of_a_resume_starts_from_the_last_succeeded_run_of_the_chain(self) -> None:
        entry = self.submit(run_count=7)
        self.succeed_three_of_seven(entry.id)
        first_resume = resume_entry(self.store, entry.id, self.global_config, self.params, clock=lambda: NOW)
        # The resume itself fails at run 5 with no further succeeded runs of its own: it should fall back to run 3's frame.
        claimed = self.store.queue.claim_oldest(NOW)
        assert claimed is not None and claimed.id == first_resume.id
        self.store.queue.finish(first_resume.id, state=QueueState.FAILED, finished_at="2026-09-27T10:20:00+00:00")
        second_resume = resume_entry(self.store, first_resume.id, self.global_config, self.params, clock=lambda: NOW)
        self.assertEqual((second_resume.resume_first_run, second_resume.resumes), (4, first_resume.queue_number))

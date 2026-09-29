"""Tests for deleting executions (Milestone 06): the in-use refusal, the resume warning, dry runs, and a resume after a
deletion."""

from __future__ import annotations

from datetime import datetime

from draw_things_control.services.history_delete import delete_executions
from draw_things_control.services.queue_resume import ResumeRefusedError, preview_resume, resume_entry
from draw_things_control.services.queue_submit import submit_job
from draw_things_control.state.executions import ExecutionSettings, NewExecution, NewRun
from draw_things_control.state.queue import NewQueueEntry, QueueRow, QueueState
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data

NOW = datetime(2026, 9, 30, 12, 0, 0)
AT = "2026-09-30T10:00:00+00:00"


def refused(store: Store, *numbers: int) -> dict[int, str]:
    return delete_executions(store, numbers, dry_run=True).refused


def ended(store: Store, *numbers: int) -> dict[int, list[str]]:
    return delete_executions(store, numbers, dry_run=True).resumes_ended


class HistoryDeleteTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(self.store.close)
        self.job = self.write_job(job_data(run_count=7, prompt_pairs=[{"name": "only", "positive": "text"}]))

    def entry(self, *, resumes: QueueRow | None = None, resumes_execution: int | None = None) -> QueueRow:
        """A queued entry: a submission, or a resume of ``resumes`` from ``resumes_execution``."""
        if resumes is None:
            return submit_job(self.job, self.global_config, self.params, self.store)
        new = NewQueueEntry(job_path=resumes.job_path, job_text=resumes.job_text, config_file=resumes.config_file, config_text=resumes.config_text, input_directory=resumes.input_directory, output_directory=resumes.output_directory, cooldown_default=None, settings=resumes.settings, submitted_at=AT, total_runs=7, resumes=resumes.queue_number, resumes_execution=resumes_execution, resume_first_run=4, resume_input="x.png", resume_seed=42)
        return self.store.queue.submit(new)

    def ran(self, entry: QueueRow, *, succeeded: int, total: int = 7, first_run: int = 1, state: QueueState | None = QueueState.INTERRUPTED, manifest: str | None = None) -> int:
        """Claim ``entry``, give it an execution with ``succeeded`` succeeded runs, and leave it ``state`` (None: running)."""
        claimed = self.store.queue.claim_oldest(NOW)
        assert claimed is not None and claimed.id == entry.id
        last_frame = self.output_directory / "last.png"
        last_frame.parent.mkdir(parents=True, exist_ok=True)
        last_frame.write_bytes(b"png")
        executions = self.store.executions
        execution_id = executions.start(NewExecution(job_name="walk", job_file=str(self.job), mode="i2v", started_at=AT, seed=42, total_runs=total, first_run=first_run, manifest_path=manifest, settings=ExecutionSettings(output_directory=str(self.output_directory))))
        for number in range(first_run, first_run + succeeded):
            executions.start_run(execution_id, number, NewRun(pair="only", positive="text", started_at=AT, status="succeeded", output="run.mov", last_frame=last_frame.name))
        if state is not None:
            executions.finish(execution_id, status=str(state), exit_code=None, signal=None, finished_at=AT)
        number = executions.number_of(execution_id)
        assert number is not None
        self.store.queue.link_execution(entry.id, number)
        if state is not None:
            self.store.queue.finish(entry.id, state=state, finished_at=AT)
        return number

    def test_a_queued_resume_keeps_the_execution_it_resumes_from(self) -> None:
        first = self.entry()
        execution = self.ran(first, succeeded=3)
        resume = self.entry(resumes=first, resumes_execution=execution)
        self.assertEqual(refused(self.store, execution), {execution: f"{resume.label} is queued to resume from E0001"})
        report = delete_executions(self.store, [execution])
        self.assertEqual((report.deleted, report.refused), ([], {execution: "Q0002 is queued to resume from E0001"}))
        self.assertIsNotNone(self.store.executions.by_number(execution))

    def test_a_queued_resume_keeps_each_execution_its_chain_reads(self) -> None:
        parked = self.entry()
        succeeded = self.ran(parked, succeeded=3, state=QueueState.PARKED)
        failed = self.entry(resumes=parked, resumes_execution=succeeded)
        between = self.ran(failed, succeeded=0, first_run=4, state=QueueState.FAILED)
        queued = self.entry(resumes=failed, resumes_execution=succeeded)
        in_use = refused(self.store, succeeded, between)
        self.assertEqual(in_use, {succeeded: f"{queued.label} is queued to resume from E0001", between: f"{queued.label} is queued to resume through E0002"})

    def test_a_running_entry_keeps_its_own_execution_and_a_running_one_is_refused(self) -> None:
        running = self.entry()
        execution = self.ran(running, succeeded=1, state=None)
        self.assertEqual(delete_executions(self.store, [execution]).refused, {execution: "E0001 is running; it cannot be deleted"})

    def test_a_running_entry_keeps_its_own_execution_once_that_has_finished(self) -> None:
        running = self.entry()
        execution = self.ran(running, succeeded=1, state=None)
        # JobFinished closed the execution; the worker has not yet finished the entry.
        row = self.store.executions.row_of(execution)
        assert row is not None
        self.store.executions.finish(row, status="succeeded", exit_code=0, signal=None, finished_at=AT)
        self.assertEqual(refused(self.store, execution), {execution: "Q0001 is running with E0001"})

    def test_a_dry_run_reports_what_a_deletion_would_and_changes_nothing(self) -> None:
        first = self.entry()
        execution = self.ran(first, succeeded=3)
        report = delete_executions(self.store, [execution, 99], dry_run=True)
        self.assertEqual((report.deleted, report.missing, report.resumes_ended, report.manifests_kept), ([execution], [99], {execution: ["Q0001"]}, []))
        self.assertIsNotNone(self.store.executions.by_number(execution))

    def test_a_manifest_that_cannot_be_deleted_is_reported_and_the_row_still_goes(self) -> None:
        directory = self.root / "locked"
        directory.mkdir()
        manifest = directory / "walk.json"
        manifest.write_text("{}", encoding="utf-8")
        execution = self.ran(self.entry(), succeeded=7, manifest=str(manifest), state=QueueState.SUCCEEDED)
        directory.chmod(0o500)
        try:
            report = delete_executions(self.store, [execution])
        finally:
            directory.chmod(0o700)
        self.assertEqual((report.deleted, report.manifests_kept), ([execution], [(execution, str(manifest))]))
        self.assertIsNone(self.store.executions.by_number(execution))

    def test_the_warning_names_each_resumable_state(self) -> None:
        for state in (QueueState.INTERRUPTED, QueueState.FAILED, QueueState.CANCELLED, QueueState.PARKED):
            with self.subTest(state=state):
                entry = self.entry()
                execution = self.ran(entry, succeeded=3, state=state)
                self.assertEqual(ended(self.store, execution), {execution: [entry.label]})

    def test_no_warning_without_a_resume_to_end(self) -> None:
        resumed = self.entry()
        resumed_from = self.ran(resumed, succeeded=3)
        self.ran(self.entry(resumes=resumed, resumes_execution=resumed_from), succeeded=1, first_run=4, state=QueueState.SUCCEEDED)
        nothing = self.ran(self.entry(), succeeded=0, state=QueueState.FAILED)
        finished = self.ran(self.entry(), succeeded=7, state=QueueState.CANCELLED)
        # A succeeded entry cannot be resumed; the resume above had a succeeded run, and so ends no resume of its own.
        self.assertEqual(ended(self.store, resumed_from, nothing, finished), {})

    def test_the_warning_follows_a_chain_of_resumes_through_an_ancestor(self) -> None:
        parked = self.entry()
        succeeded = self.ran(parked, succeeded=3, state=QueueState.PARKED)
        failed = self.entry(resumes=parked, resumes_execution=succeeded)
        between = self.ran(failed, succeeded=0, first_run=4, state=QueueState.FAILED)
        self.assertEqual(ended(self.store, succeeded, between), {succeeded: [failed.label], between: [failed.label]})
        report = delete_executions(self.store, [succeeded])
        self.assertEqual(report.resumes_ended, {succeeded: [failed.label]})
        # Nothing reads the rest of the chain once it is broken.
        self.assertEqual(ended(self.store, between), {})

    def test_a_resume_is_refused_after_its_execution_is_deleted(self) -> None:
        entry = self.entry()
        execution = self.ran(entry, succeeded=3)
        delete_executions(self.store, [execution])
        with self.assertRaisesRegex(ResumeRefusedError, "E0001 was pruned or deleted; it cannot be resumed"):
            resume_entry(self.store, entry.id, self.global_config, self.params, clock=lambda: NOW)
        stored = self.store.queue.get(entry.id)
        assert stored is not None
        self.assertEqual(preview_resume(self.store, stored, self.global_config, self.params).reason, "Q0001's execution E0001 was pruned or deleted; it cannot be resumed")

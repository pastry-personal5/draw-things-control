"""Tests for deleting executions (Milestone 06): the repository's transaction, and the store's log and manifest files."""

import os
from pathlib import Path

from draw_things_control.state.executions import ExecutionSettings, NewExecution, NewRun
from tests.state.test_store import StoreCase, iso


class DeleteTests(StoreCase):
    def execution(self, *, status: str = "succeeded", log: str | None = None, manifest: str | None = None) -> int:
        executions = self.store.executions
        execution_id = executions.start(NewExecution(job_name="walk", job_file="walk.yaml", mode="i2v", started_at=iso(1), settings=ExecutionSettings(output_directory="/out"), log_path=log, manifest_path=manifest))
        executions.start_run(execution_id, 1, NewRun(pair="p", positive="text", started_at=iso(1)))
        if status != "running":
            executions.finish(execution_id, status=status, exit_code=0, signal=None, finished_at=iso(1))
        number = executions.number_of(execution_id)
        assert number is not None
        return number

    def file(self, name: str) -> Path:
        path = self.path.parent / name
        path.write_text("{}", encoding="utf-8")
        return path

    def test_a_deletion_removes_the_row_and_its_runs_and_keeps_the_counter(self) -> None:
        number = self.execution()
        deletion = self.store.executions.delete([number], in_use={})
        self.assertEqual(([deleted.number for deleted in deletion.deleted], deletion.refused, deletion.missing), ([number], {}, []))
        self.assertIsNone(self.store.executions.by_number(number))
        self.assertEqual(self.store._database.connection().execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.execution(), number + 1)

    def test_running_in_use_and_missing_executions_are_skipped(self) -> None:
        running = self.execution(status="running")
        used = self.execution()
        deletion = self.store.executions.delete([running, used, 99], in_use={used: "Q0007 is queued to resume from E0002"})
        self.assertEqual(deletion.deleted, [])
        self.assertEqual(deletion.refused, {running: "E0001 is running; it cannot be deleted", used: "Q0007 is queued to resume from E0002"})
        self.assertEqual(deletion.missing, [99])
        self.assertIsNotNone(self.store.executions.by_number(running))

    def test_a_dry_run_reports_the_same_and_deletes_nothing(self) -> None:
        number = self.execution()
        deletion = self.store.executions.delete([number, number], in_use={}, dry_run=True)
        self.assertEqual([deleted.number for deleted in deletion.deleted], [number])
        self.assertIsNotNone(self.store.executions.by_number(number))

    def test_the_log_and_manifest_go_and_a_missing_one_is_no_error(self) -> None:
        log, manifest = self.file("walk.log"), self.file("walk.json")
        number = self.execution(log=str(log), manifest=str(manifest))
        gone = self.execution(log=str(self.path.parent / "gone.log"), manifest=str(self.path.parent / "gone.json"))
        deletion = self.store.executions.delete([number, gone], in_use={})
        self.assertEqual(self.store.delete_execution_files(deletion.deleted), [])
        self.assertFalse(log.exists() or manifest.exists())

    def test_a_symbolic_link_or_a_file_not_json_is_kept_and_reported(self) -> None:
        target = self.file("target.json")
        link = self.path.parent / "link.json"
        os.symlink(target, link)
        other = self.file("walk.txt")
        linked = self.execution(manifest=str(link))
        not_json = self.execution(manifest=str(other))
        deletion = self.store.executions.delete([linked, not_json], in_use={})
        self.assertEqual(self.store.delete_execution_files(deletion.deleted), [(linked, str(link)), (not_json, str(other))])
        self.assertTrue(link.is_symlink() and target.exists() and other.exists())
        self.assertIsNone(self.store.executions.by_number(linked))

    def test_a_manifest_that_cannot_be_deleted_is_reported_and_the_row_still_goes(self) -> None:
        directory = self.path.parent / "locked"
        directory.mkdir()
        manifest = directory / "walk.json"
        manifest.write_text("{}", encoding="utf-8")
        number = self.execution(manifest=str(manifest))
        directory.chmod(0o500)
        self.addCleanup(directory.chmod, 0o700)
        deletion = self.store.executions.delete([number], in_use={})
        self.assertEqual(self.store.delete_execution_files(deletion.deleted), [(number, str(manifest))])
        self.assertIsNone(self.store.executions.by_number(number))

    def test_files_in_a_directory_that_cannot_be_searched_are_reported_and_the_row_still_goes(self) -> None:
        directory = self.path.parent / "unsearchable"
        directory.mkdir()
        log, manifest = directory / "walk.log", directory / "walk.json"
        log.write_text("", encoding="utf-8")
        manifest.write_text("{}", encoding="utf-8")
        number = self.execution(log=str(log), manifest=str(manifest))
        # 0o000, not 0o500: checking for a symbolic link then fails too, not only the unlink.
        directory.chmod(0o000)
        self.addCleanup(directory.chmod, 0o700)
        deletion = self.store.executions.delete([number], in_use={})
        self.assertEqual(self.store.delete_execution_files(deletion.deleted), [(number, str(manifest))])
        self.assertIsNone(self.store.executions.by_number(number))

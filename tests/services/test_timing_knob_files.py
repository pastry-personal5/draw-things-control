"""The knob files' bytes and journals: atomic writes, the lock, backups, and a transaction that survives an interruption."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from framecontrol.core.errors import FramecontrolError
from framecontrol.services.timing_knob_files import KnobBusyError, KnobConflictError, KnobFiles, Written, digest, dump_yaml, read_yaml


class Interrupted(Exception):
    pass


def written_for(**texts: str) -> Written:
    return Written({name: {"digest": digest(text), "values": {}, "pins": {}} for name, text in texts.items()})


class FilesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.current: dict[str, str] = {}
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "timing"
        self.files = KnobFiles(self.root)

    def commit(self, **texts: str) -> str:
        self.current = {**getattr(self, "current", {}), **texts}
        with self.files.lock():
            return self.files.commit(texts, written_for(**self.current), ring="ring\n")

    def leftovers(self) -> list[str]:
        return sorted(path.name for path in self.root.rglob(".*.tmp"))


class AtomicWriteTests(FilesTestCase):
    def test_a_failure_between_the_temporary_file_and_the_rename_leaves_the_old_file(self) -> None:
        self.root.mkdir(parents=True)
        path = self.root / "a.yaml"
        path.write_text("old", encoding="utf-8")

        def fail(name: str) -> None:
            if name == "rename:a.yaml":
                raise Interrupted

        self.files.checkpoint = fail
        with self.assertRaises(Interrupted):
            self.files.atomic_write(path, "new")
        self.assertEqual(path.read_text(encoding="utf-8"), "old")
        self.assertEqual(self.leftovers(), [])

    def test_a_write_replaces_the_file_whole(self) -> None:
        path = self.root / "b.yaml"
        self.files.atomic_write(path, "one")
        self.files.atomic_write(path, "two")
        self.assertEqual(path.read_text(encoding="utf-8"), "two")
        self.assertEqual(self.leftovers(), [])


class LockTests(FilesTestCase):
    def test_updates_serialize_across_threads(self) -> None:
        inside, overlaps = [], []

        def work() -> None:
            with self.files.lock():
                inside.append(1)
                if len(inside) > 1:
                    overlaps.append(1)
                time.sleep(0.05)
                inside.pop()

        threads = [threading.Thread(target=work) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(overlaps, [])

    def test_another_holder_of_the_lock_file_is_waited_for_then_refused(self) -> None:
        other = KnobFiles(self.root)
        with self.files.lock(), self.assertRaises(KnobBusyError), other.lock(timeout=0.2):
            pass
        with other.lock(timeout=1.0):
            pass

    def test_the_lock_is_reentrant_in_a_thread(self) -> None:
        with self.files.lock(), self.files.lock():
            pass


class CommitTests(FilesTestCase):
    def test_a_commit_writes_the_files_the_ring_and_the_record_and_leaves_no_journal(self) -> None:
        self.commit(**{"a.yaml": "a1", "b.yaml": "b1"})
        self.assertEqual((self.root / "a.yaml").read_text(encoding="utf-8"), "a1")
        self.assertEqual(self.files.read_text("ring.jsonl"), "ring\n")
        self.assertEqual(self.files.read_written().files["b.yaml"]["digest"], digest("b1"))
        self.assertFalse(self.files.pending_path.exists())
        self.assertEqual(len(self.files.transactions()), 1)

    def test_only_the_newest_backups_of_each_file_remain(self) -> None:
        for number in range(30):
            with self.files.lock():
                self.files.commit({"a.yaml": f"a{number}"}, written_for(**{"a.yaml": f"a{number}"}), keep=20)
        backups = self.files.backups_of("a.yaml")
        self.assertEqual(len(backups), 20)
        self.assertEqual(backups[-1].read_text(encoding="utf-8"), "a28")
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "a9")

    def test_an_interruption_at_every_boundary_is_finished_and_never_reads_as_a_hand_edit(self) -> None:
        self.commit(**{"a.yaml": "a1", "b.yaml": "b1"})
        names: list[str] = []
        self.files.checkpoint = names.append
        self.commit(**{"a.yaml": "a2", "b.yaml": "b2"})
        self.assertGreaterEqual(len(names), 7)
        for boundary in dict.fromkeys(names):
            with self.subTest(boundary=boundary):
                self.setUp()
                self.commit(**{"a.yaml": "a1", "b.yaml": "b1"})

                def interrupt(name: str, boundary: str = boundary) -> None:
                    if name == boundary:
                        raise Interrupted

                self.files.checkpoint = interrupt
                with self.assertRaises(Interrupted), self.files.lock():
                    self.files.commit({"a.yaml": "a2", "b.yaml": "b2"}, written_for(**{"a.yaml": "a2", "b.yaml": "b2"}), ring="ring2\n")
                restarted = KnobFiles(self.root)
                with restarted.lock():
                    restarted.recover()
                state = {name: (self.root / name).read_text(encoding="utf-8") for name in ("a.yaml", "b.yaml")}
                record = restarted.read_written()
                if boundary in ("backups", "rename:pending-ring.jsonl", "rename:pending.json"):
                    # The journal was not yet durable: the old set stands, whole.
                    self.assertEqual(state, {"a.yaml": "a1", "b.yaml": "b1"})
                    self.assertEqual(record.files["a.yaml"]["digest"], digest("a1"))
                else:
                    self.assertEqual(state, {"a.yaml": "a2", "b.yaml": "b2"})
                    self.assertEqual({name: record.files[name]["digest"] for name in state}, {name: digest(text) for name, text in state.items()})
                    self.assertEqual(restarted.read_text("ring.jsonl"), "ring2\n")
                self.assertFalse(restarted.pending_path.exists())
                self.assertFalse(restarted.pending_ring_path.exists())
                self.assertEqual(self.leftovers(), [])

    def test_recovery_keeps_a_hand_edit_that_got_there_first(self) -> None:
        self.commit(**{"a.yaml": "a1"})
        self.files.checkpoint = lambda name: (_ for _ in ()).throw(Interrupted) if name == "pending" else None
        with self.assertRaises(Interrupted), self.files.lock():
            self.files.commit({"a.yaml": "a2"}, written_for(**{"a.yaml": "a2"}))
        (self.root / "a.yaml").write_text("the owner's edit", encoding="utf-8")
        restarted = KnobFiles(self.root)
        with restarted.lock():
            restarted.recover()
        self.assertEqual((self.root / "a.yaml").read_text(encoding="utf-8"), "the owner's edit")
        self.assertEqual(restarted.read_written().files["a.yaml"]["digest"], digest("a1"))

    def test_an_unreadable_journal_is_discarded_not_fatal(self) -> None:
        self.files.machine.mkdir(parents=True)
        self.files.pending_path.write_text("{not json", encoding="utf-8")
        with self.files.lock():
            self.assertTrue(self.files.recover())
        self.assertFalse(self.files.pending_path.exists())


class RollbackTests(FilesTestCase):
    def test_rollback_restores_the_files_as_a_set_and_the_record(self) -> None:
        self.commit(**{"a.yaml": "a1", "b.yaml": "b1"})
        self.commit(**{"a.yaml": "a2"})
        self.commit(**{"b.yaml": "b3"})
        with self.files.lock():
            restored = self.files.rollback(1)
        self.assertEqual(restored, ["b.yaml"])
        self.assertEqual((self.root / "b.yaml").read_text(encoding="utf-8"), "b1")
        self.assertEqual((self.root / "a.yaml").read_text(encoding="utf-8"), "a2")
        self.assertEqual(self.files.read_written().files["b.yaml"]["digest"], digest("b1"))

    def test_rolling_back_two_updates_puts_each_file_back_as_it_was_before_the_older(self) -> None:
        self.commit(**{"a.yaml": "a1", "b.yaml": "b1"})
        self.commit(**{"a.yaml": "a2"})
        self.commit(**{"b.yaml": "b3"})
        with self.files.lock():
            self.files.rollback(2)
        self.assertEqual((self.root / "a.yaml").read_text(encoding="utf-8"), "a1")
        self.assertEqual((self.root / "b.yaml").read_text(encoding="utf-8"), "b1")
        # A rollback is an update of its own, so it is journaled, backed up, and can be undone.
        self.assertEqual(len(self.files.transactions()), 4)

    def test_a_ring_only_commit_is_not_an_update_to_roll_back_and_does_not_crowd_out_one(self) -> None:
        self.commit(**{"a.yaml": "a1"})
        self.commit(**{"a.yaml": "a2"})
        with self.files.lock():
            for _ in range(30):
                self.files.commit({}, written_for(**self.current), ring="more\n")
            self.assertEqual(self.files.rollback(1), ["a.yaml"])
        self.assertEqual((self.root / "a.yaml").read_text(encoding="utf-8"), "a1")

    def test_a_rollback_can_itself_be_rolled_back(self) -> None:
        self.commit(**{"a.yaml": "a1"})
        self.commit(**{"a.yaml": "a2"})
        with self.files.lock():
            self.files.rollback(1)
            self.assertEqual((self.root / "a.yaml").read_text(encoding="utf-8"), "a1")
            self.files.rollback(1)
        self.assertEqual((self.root / "a.yaml").read_text(encoding="utf-8"), "a2")
        self.assertEqual(self.files.read_written().files["a.yaml"]["digest"], digest("a2"))

    def test_a_rollback_keeps_the_completed_run_counts(self) -> None:
        self.commit(**{"a.yaml": "a1"})
        with self.files.lock():
            written = self.files.read_written()
            written.groups["x/y"] = {"count": 8, "checkpoint": 8}
            self.files.commit({"a.yaml": "a2"}, written)
            self.files.rollback(1)
        self.assertEqual(self.files.read_written().groups["x/y"], {"count": 8, "checkpoint": 8})
        self.assertEqual(self.files.read_written().files["a.yaml"]["digest"], digest("a1"))

    def test_an_interrupted_rollback_is_finished_and_never_reads_as_a_hand_edit(self) -> None:
        self.commit(**{"a.yaml": "a1", "b.yaml": "b1"})
        self.commit(**{"a.yaml": "a2", "b.yaml": "b2"})
        names: list[str] = []
        self.files.checkpoint = names.append
        with self.files.lock():
            self.files.rollback(1)
        for boundary in dict.fromkeys(names):
            with self.subTest(boundary=boundary):
                self.setUp()
                self.commit(**{"a.yaml": "a1", "b.yaml": "b1"})
                self.commit(**{"a.yaml": "a2", "b.yaml": "b2"})

                def interrupt(name: str, boundary: str = boundary) -> None:
                    if name == boundary:
                        raise Interrupted

                self.files.checkpoint = interrupt
                with self.assertRaises(Interrupted), self.files.lock():
                    self.files.rollback(1)
                restarted = KnobFiles(self.root)
                with restarted.lock():
                    restarted.recover()
                state = {name: (self.root / name).read_text(encoding="utf-8") for name in ("a.yaml", "b.yaml")}
                record = restarted.read_written()
                self.assertIn(state, ({"a.yaml": "a1", "b.yaml": "b1"}, {"a.yaml": "a2", "b.yaml": "b2"}))
                # Whatever the files are, the record says the same: nothing for the next load to read as an edit.
                self.assertEqual({name: record.files[name]["digest"] for name in state}, {name: digest(text) for name, text in state.items()})

    def test_a_file_created_by_the_update_is_removed_by_rolling_it_back_and_the_removal_is_journaled(self) -> None:
        self.commit(**{"a.yaml": "a1"})
        names: list[str] = []
        self.files.checkpoint = names.append
        with self.files.lock():
            self.files.rollback(1)
        self.assertFalse((self.root / "a.yaml").exists())
        self.assertIn("pending", names)
        self.assertNotIn("a.yaml", self.files.read_written().files)

    def test_rolling_back_more_than_there_are_is_refused_and_changes_nothing(self) -> None:
        self.commit(**{"a.yaml": "a1"})
        with self.files.lock(), self.assertRaises(FramecontrolError) as caught:
            self.files.rollback(5)
        self.assertIn("1 committed update", str(caught.exception))
        self.assertEqual((self.root / "a.yaml").read_text(encoding="utf-8"), "a1")

    def test_a_missing_backup_refuses_the_rollback_before_anything_changes(self) -> None:
        self.commit(**{"a.yaml": "a1"})
        self.commit(**{"a.yaml": "a2"})
        for backup in self.files.backups_of("a.yaml"):
            backup.unlink()
        with self.files.lock(), self.assertRaises(FramecontrolError):
            self.files.rollback(1)
        self.assertEqual((self.root / "a.yaml").read_text(encoding="utf-8"), "a2")

    def test_a_commit_can_remove_a_file_keeping_it_in_the_backups(self) -> None:
        self.commit(**{"a.yaml": "a1"})
        with self.files.lock():
            written = self.files.read_written()
            written.files.pop("a.yaml")
            self.files.commit({"a.yaml": None}, written)
        self.assertFalse((self.root / "a.yaml").exists())
        self.assertEqual(self.files.backups_of("a.yaml")[-1].read_text(encoding="utf-8"), "a1")


class ExpectedDigestTests(FilesTestCase):
    def test_an_edit_made_after_the_caller_read_a_file_stops_the_whole_commit(self) -> None:
        self.commit(**{"a.yaml": "a1", "b.yaml": "b1"})
        seen = {"a.yaml": self.files.current_digest("a.yaml"), "b.yaml": self.files.current_digest("b.yaml")}
        (self.root / "b.yaml").write_text("the owner's edit", encoding="utf-8")
        with self.files.lock(), self.assertRaises(KnobConflictError):
            self.files.commit({"a.yaml": "a2", "b.yaml": "b2"}, written_for(**{"a.yaml": "a2", "b.yaml": "b2"}), ring="new ring\n", expected=seen)
        self.assertEqual((self.root / "a.yaml").read_text(encoding="utf-8"), "a1")
        self.assertEqual((self.root / "b.yaml").read_text(encoding="utf-8"), "the owner's edit")
        self.assertEqual(self.files.read_text("ring.jsonl"), "ring\n")
        self.assertFalse(self.files.pending_path.exists())

    def test_a_file_that_appeared_after_the_caller_found_it_absent_is_also_a_conflict(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "a.yaml").write_text("made meanwhile", encoding="utf-8")
        with self.files.lock(), self.assertRaises(KnobConflictError):
            self.files.commit({"a.yaml": "a1"}, written_for(**{"a.yaml": "a1"}), expected={"a.yaml": None})

    def test_matching_digests_commit(self) -> None:
        self.commit(**{"a.yaml": "a1"})
        with self.files.lock():
            self.files.commit({"a.yaml": "a2"}, written_for(**{"a.yaml": "a2"}), expected={"a.yaml": self.files.current_digest("a.yaml")})
        self.assertEqual((self.root / "a.yaml").read_text(encoding="utf-8"), "a2")


class RingFileTests(FilesTestCase):
    def test_the_counts_header_and_entries_round_trip_and_a_bad_line_is_dropped(self) -> None:
        text = self.files.ring_text({"x/y": 7}, [{"e": 1}, {"e": 2}])
        self.files.atomic_write(self.files.path_of("ring.jsonl"), text.replace('{"e":2}', "garbage"))
        counts, entries = self.files.read_ring()
        self.assertEqual((counts, entries), ({"x/y": 7}, [{"e": 1}]))

    def test_a_missing_ring_is_empty(self) -> None:
        self.assertEqual(self.files.read_ring(), ({}, []))


class YamlTests(FilesTestCase):
    def test_dump_keeps_key_order_and_read_reports_a_bad_file_instead_of_raising(self) -> None:
        text = dump_yaml({"schema": 1, "zeta": 1, "alpha": 2})
        self.assertLess(text.index("zeta"), text.index("alpha"))
        path = self.root / "x.yaml"
        self.files.atomic_write(path, "a: [unclosed")
        document, problem = read_yaml(path)
        self.assertIsNone(document)
        assert problem is not None
        self.assertIn("not valid YAML", problem)
        self.assertEqual(read_yaml(self.root / "missing.yaml"), (None, None))
        json.dumps(document)

"""The files under ``data/timing-data-analysis/`` as bytes and journals: the lock, atomic writes, backups, the ring file, the
record of what the machine last wrote, and the transaction that changes several of them at once.

A change is a set of whole files. ``commit`` backs up each file it replaces, writes ``pending.json`` (the new texts, the prior
digests, the backups), replaces the files, and removes the journal; ``recover`` finishes a journal an interruption left, so a
machine write is never read as a hand edit. Nothing here knows what a knob means."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from framecontrol.core.errors import FramecontrolError

WRITTEN_SCHEMA = 1
LOCK_TIMEOUT_SECONDS = 15.0
MANIFEST_LIMIT = 25
TUNING_FILE = "tuning.yaml"
RING_FILE = "ring.jsonl"
VALIDATED_FILE = "validated.json"
# LibYAML's loader when this PyYAML has it: the same safe subset, several times faster on the larger files.
SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
HEADER = "# Framecontrol timing knobs. Edit a number to pin it; delete a key to return it to automatic. Comments are not kept when Framecontrol rewrites this file; `notes:` is.\n"


class KnobBusyError(FramecontrolError):
    """Another update holds the knob files' lock."""


class KnobConflictError(FramecontrolError):
    """A knob file changed between the moment an update read it and the moment it would replace it (a hand edit)."""


@dataclass
class RingState:
    """The ring file: runs ever added per group, the newest ``(execution, run)`` ever added per group (so a run the ring
    trimmed is never added again), and the entries."""

    added: dict[str, int] = field(default_factory=dict)
    high: dict[str, tuple[int, int]] = field(default_factory=dict)
    entries: list[dict[str, Any]] = field(default_factory=list)


def digest(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def dump_yaml(mapping: Mapping[str, Any]) -> str:
    """The file text for a mapping: its keys in the order given, with the header comment."""
    return HEADER + yaml.safe_dump(dict(mapping), sort_keys=False, default_flow_style=False, allow_unicode=True)


def read_yaml(path: Path) -> tuple[Any, str | None]:
    """(the parsed document, an error text): the document is None when the file is absent or cannot be parsed."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeDecodeError) as error:
        return None, f"cannot be read: {error}"
    try:
        return yaml.load(text, Loader=SAFE_LOADER), None  # noqa: S506 (a safe loader)
    except yaml.YAMLError as error:
        return None, f"is not valid YAML: {getattr(error, 'problem', error)}"


def stamp_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Written:
    """What the machine last wrote: each file's digest, flattened values, pins, and read-only blocks; each group's processed
    count and checkpoint."""

    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"schema": WRITTEN_SCHEMA, "files": self.files, "groups": self.groups}

    @classmethod
    def from_dict(cls, data: Any) -> Written:
        if not isinstance(data, dict) or data.get("schema") != WRITTEN_SCHEMA:
            return cls()
        files, groups = data.get("files"), data.get("groups")
        return cls(files if isinstance(files, dict) else {}, groups if isinstance(groups, dict) else {})


class KnobFiles:
    """One timing-data directory. Methods that change files require the lock (``with files.lock():``)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.machine = directory / ".machine"
        self.backups = self.machine / "backups"
        self._thread_lock = threading.RLock()
        self._held = 0
        self._handle: Any = None
        # Called with the name of each boundary of a commit, for a test that interrupts one.
        self.checkpoint: Callable[[str], None] = lambda _name: None
        self._sequence = 0

    # ------------------------------------------------------------ paths

    def path_of(self, name: str) -> Path:
        return self.machine / name if name in (RING_FILE, VALIDATED_FILE) else self.directory / name

    @property
    def written_path(self) -> Path:
        return self.machine / "written.json"

    @property
    def pending_path(self) -> Path:
        return self.machine / "pending.json"

    @property
    def pending_ring_path(self) -> Path:
        return self.machine / "pending-ring.jsonl"

    @property
    def manifest_path(self) -> Path:
        return self.backups / "transactions.jsonl"

    @property
    def log_path(self) -> Path:
        return self.machine / "updates.log"

    # ------------------------------------------------------------ lock

    @contextlib.contextmanager
    def lock(self, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
        """The exclusive lock across threads and processes; re-entrant in a thread. Raises ``KnobBusyError`` after ``timeout``."""
        deadline = time.monotonic() + timeout
        if not self._thread_lock.acquire(timeout=timeout):
            raise KnobBusyError("The timing knob files are being updated")
        try:
            if self._held == 0:
                self.machine.mkdir(parents=True, exist_ok=True)
                handle = (self.machine / "lock").open("a+")
                while True:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            handle.close()
                            raise KnobBusyError("The timing knob files are being updated by another process") from None
                        time.sleep(0.05)
                self._handle = handle
            self._held += 1
            try:
                yield
            finally:
                self._held -= 1
                if self._held == 0:
                    with contextlib.suppress(OSError):
                        fcntl.flock(self._handle, fcntl.LOCK_UN)
                    self._handle.close()
                    self._handle = None
        finally:
            self._thread_lock.release()

    # ------------------------------------------------------------ writing

    def atomic_write(self, path: Path, text: str) -> None:
        """Write ``text`` to a temporary file beside ``path``, flush it to disk, and rename it over ``path``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            self.checkpoint(f"rename:{path.name}")
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise

    def read_text(self, name: str) -> str | None:
        try:
            return self.path_of(name).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return None

    def current_digest(self, name: str) -> str | None:
        try:
            return digest(self.path_of(name).read_bytes())
        except OSError:
            return None

    def signature(self) -> tuple[Any, ...]:
        """A cheap fingerprint of the knob files (their sizes and modification times), to know when to read them again."""
        result = []
        names = [TUNING_FILE, *sorted(path.name for path in self.directory.glob("*.yaml") if not path.name.endswith(".example.yaml") and path.name != TUNING_FILE)] if self.directory.is_dir() else []
        for name in names:
            try:
                info = (self.directory / name).stat()
                result.append((name, info.st_mtime_ns, info.st_size))
            except OSError:
                result.append((name, None, None))
        try:
            info = self.written_path.stat()
            result.append(("written", info.st_mtime_ns, info.st_size))
        except OSError:
            result.append(("written", None, None))
        try:
            info = self.path_of(VALIDATED_FILE).stat()
            result.append((VALIDATED_FILE, info.st_mtime_ns, info.st_size))
        except OSError:
            result.append((VALIDATED_FILE, None, None))
        return tuple(result)

    # ------------------------------------------------------------ the record

    def read_written(self) -> Written:
        try:
            return Written.from_dict(json.loads(self.written_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return Written()

    def read_ring_state(self) -> RingState:
        """The ring file. A line that is not JSON is dropped."""
        state = RingState()
        try:
            lines = self.path_of(RING_FILE).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return state
        for index, line in enumerate(lines):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if index == 0 and isinstance(item, dict) and "added" in item:
                added, high = item["added"], item.get("high")
                if isinstance(added, dict):
                    state.added = {str(key): value for key, value in added.items() if isinstance(value, int) and not isinstance(value, bool)}
                if isinstance(high, dict):
                    state.high = {str(key): (int(value[0]), int(value[1])) for key, value in high.items() if isinstance(value, list) and len(value) == 2 and all(isinstance(part, int) and not isinstance(part, bool) for part in value)}
            elif isinstance(item, dict):
                state.entries.append(item)
        return state

    def read_ring(self) -> tuple[dict[str, int], list[dict[str, Any]]]:
        """(the number of runs ever added per group, the entries)."""
        state = self.read_ring_state()
        return state.added, state.entries

    @staticmethod
    def ring_text(counts: Mapping[str, int], entries: list[dict[str, Any]], high: Mapping[str, tuple[int, int]] | None = None) -> str:
        lines = [json.dumps({"added": dict(counts), "high": {key: list(value) for key, value in (high or {}).items()}}, separators=(",", ":"))]
        lines.extend(json.dumps(entry, separators=(",", ":")) for entry in entries)
        return "\n".join(lines) + "\n"

    def log(self, message: str) -> None:
        """Append a line to ``updates.log``; never raises."""
        with contextlib.suppress(OSError):
            self.machine.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp_now()} {message}\n")

    # ------------------------------------------------------------ backups

    def _backup(self, name: str, keep: int) -> str | None:
        path = self.path_of(name)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        self.backups.mkdir(parents=True, exist_ok=True)
        self._sequence += 1
        backup = f"{name}.{datetime.now().strftime('%Y%m%dT%H%M%S%f')}.{self._sequence:04d}.bak"
        self._write_bytes(self.backups / backup, data)
        self.prune(name, keep)
        return backup

    def _write_bytes(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def backups_of(self, name: str) -> list[Path]:
        """The backups of file ``name``, oldest first."""
        if not self.backups.is_dir():
            return []
        return sorted(path for path in self.backups.iterdir() if path.name.startswith(f"{name}.") and path.name.endswith(".bak"))

    def prune(self, name: str, keep: int) -> None:
        for path in self.backups_of(name)[:-keep] if keep > 0 else self.backups_of(name):
            with contextlib.suppress(OSError):
                path.unlink()

    # ------------------------------------------------------------ transactions

    def commit(self, texts: Mapping[str, str | None], written: Written, *, ring: str | None = None, keep: int = 20, expected: Mapping[str, str | None] | None = None) -> str:
        """Replace the files in ``texts`` (by name; a text of None removes the file) as a set, with ``ring`` and ``written``
        beside them. ``expected`` is the digest each file had when the caller read it: if any differs now (a hand edit landed
        meanwhile) nothing is changed and ``KnobConflictError`` is raised, so the edit is reconsidered rather than overwritten.
        Returns the transaction id. Requires the lock."""
        self.recover(keep)
        if expected is not None:
            changed = sorted(name for name in texts if name in expected and self.current_digest(name) != expected[name])
            if changed:
                raise KnobConflictError(f"{', '.join(changed)} changed while the update was being made; it is left as edited and will be considered at the next update")
        identifier = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        prior = self.read_written()
        files: dict[str, dict[str, Any]] = {}
        for name, text in texts.items():
            files[name] = {"backup": self._backup(name, keep), "prior": self.current_digest(name), "new": digest(text) if text is not None else None}
        self.checkpoint("backups")
        if ring is not None:
            self.atomic_write(self.pending_ring_path, ring)
        journal = {"id": identifier, "at": stamp_now(), "texts": dict(texts), "files": files, "ring": ring is not None, "written": written.as_dict(), "prior_written": prior.as_dict()}
        self.atomic_write(self.pending_path, json.dumps(journal))
        self.checkpoint("pending")
        self._apply(journal, ring)
        self._finish(journal)
        return identifier

    def recover(self, keep: int = 20) -> bool:
        """Finish an interrupted commit (its files are replaced as the journal says, unless a hand edit got there first).
        True when there was one. Requires the lock."""
        if not self.pending_path.exists():
            with contextlib.suppress(OSError):
                self.pending_ring_path.unlink()
            return False
        try:
            journal = json.loads(self.pending_path.read_text(encoding="utf-8"))
            ring = self.pending_ring_path.read_text(encoding="utf-8") if journal.get("ring") else None
        except (OSError, ValueError):
            self.log("an unreadable journal was discarded")
            for path in (self.pending_path, self.pending_ring_path):
                with contextlib.suppress(OSError):
                    path.unlink()
            return True
        self._apply(journal, ring, recovering=True)
        self._finish(journal)
        self.log(f"recovered interrupted transaction {journal.get('id')}")
        return True

    def _apply(self, journal: Mapping[str, Any], ring: str | None, *, recovering: bool = False) -> None:
        skipped: set[str] = set()
        if recovering:
            skipped = {name for name in journal["texts"] if self.current_digest(name) not in (journal["files"][name]["prior"], journal["files"][name]["new"])}
            if skipped and VALIDATED_FILE in journal["texts"]:
                # An approval and the calibration it validates are one logical
                # write. Preserve every conflicting byte, but restore every
                # file record to its prior digest so a partial set is surfaced
                # as an edit or a damaged approval rather than trusted.
                skipped = set(journal["texts"])
        for name, text in journal["texts"].items():
            facts = journal["files"][name]
            current = self.current_digest(name)
            if name in skipped:
                continue
            if current != facts["new"]:
                if text is None:
                    with contextlib.suppress(OSError):
                        self.path_of(name).unlink()
                else:
                    self.atomic_write(self.path_of(name), text)
            self.checkpoint(f"file:{name}")
        if ring is not None:
            self.atomic_write(self.path_of(RING_FILE), ring)
            self.checkpoint("ring")
        written = dict(journal["written"])
        for name in sorted(skipped):
            # A hand edit arrived first: keep its prior record, so the next load reads the edit as one.
            prior_files = journal["prior_written"].get("files", {})
            if name in prior_files:
                written["files"][name] = prior_files[name]
            else:
                written["files"].pop(name, None)
            self.log(f"{name} was edited while an update was interrupted; the edit was kept")
        self.atomic_write(self.written_path, json.dumps(written))
        self.checkpoint("written")

    def _finish(self, journal: Mapping[str, Any]) -> None:
        entry = {"id": journal["id"], "at": journal["at"], "files": {name: facts["backup"] for name, facts in journal["files"].items()}, "prior_written": journal["prior_written"]}
        try:
            lines = self.manifest_path.read_text(encoding="utf-8").splitlines() if self.manifest_path.exists() else []
        except OSError:
            lines = []
        # A commit that replaced no knob file (only the ring and the record moved) is not an update to roll back.
        if journal["files"] and not any(f'"id": "{journal["id"]}"' in line for line in lines):
            lines.append(json.dumps(entry))
            self.backups.mkdir(parents=True, exist_ok=True)
            self.atomic_write(self.manifest_path, "\n".join(lines[-MANIFEST_LIMIT:]) + "\n")
        self.checkpoint("manifest")
        for path in (self.pending_path, self.pending_ring_path):
            with contextlib.suppress(OSError):
                path.unlink()

    # ------------------------------------------------------------ rollback and reset

    def transactions(self) -> list[dict[str, Any]]:
        try:
            lines = self.manifest_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries = []
        for line in lines:
            with contextlib.suppress(ValueError):
                item = json.loads(line)
                if isinstance(item, dict) and "id" in item:
                    entries.append(item)
        return entries

    def rollback(self, count: int = 1, keep: int = 20) -> list[str]:
        """Put the files back as they were before the ``count``-th most recent committed transaction, as a set, in a
        transaction of their own (so it is journaled, backed up, and can itself be rolled back), with the record of what the
        machine wrote from that time. The completed-run counts are kept as they are, so learning resumes at the next
        checkpoint and does not redo the one just undone. Returns the files restored. Requires the lock."""
        self.recover(keep)
        entries = self.transactions()
        if count < 1 or count > len(entries):
            raise FramecontrolError(f"There are {len(entries)} committed updates to roll back; {count} was asked")
        chosen = entries[-count:]
        restored: dict[str, str | None] = {}
        for entry in chosen:
            for name, backup in entry["files"].items():
                restored.setdefault(name, backup)
        texts: dict[str, str | None] = {}
        for name, backup in restored.items():
            if backup is None:
                texts[name] = None
                continue
            try:
                texts[name] = (self.backups / backup).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                raise FramecontrolError(f"The backup {backup} for {name} is gone, so the update cannot be rolled back") from None
        prior = Written.from_dict(chosen[0]["prior_written"])
        current = self.read_written()
        self.commit(texts, Written(prior.files, current.groups), keep=keep)
        self.log(f"rolled back {count} update(s): {', '.join(sorted(restored))}")
        return sorted(restored)

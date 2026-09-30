"""The state database's schema: each migration, in order."""

from __future__ import annotations

SCHEMA_V1 = """
CREATE TABLE executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    job_file TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    model TEXT,
    seed INTEGER,
    seed_source TEXT,
    cooldown_seconds REAL,
    cooldown_source TEXT,
    total_runs INTEGER,
    started_at TEXT NOT NULL,
    started_epoch REAL NOT NULL,
    finished_at TEXT,
    finished_epoch REAL,
    exit_code INTEGER,
    signal TEXT,
    manifest_path TEXT UNIQUE,
    log_path TEXT,
    config_file TEXT,
    job_yaml TEXT,
    settings TEXT NOT NULL DEFAULT '{}',
    recovered_at TEXT
);
CREATE INDEX executions_started ON executions (started_epoch DESC);
CREATE INDEX executions_finished ON executions (finished_epoch);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id INTEGER NOT NULL REFERENCES executions (id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    pair TEXT NOT NULL,
    positive TEXT NOT NULL,
    negative TEXT,
    input TEXT,
    resized_input TEXT,
    output TEXT,
    last_frame TEXT,
    command TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    started_epoch REAL NOT NULL,
    seconds REAL,
    exit_code INTEGER,
    status TEXT NOT NULL,
    cooldown_after_seconds REAL,
    UNIQUE (execution_id, number)
);
"""

# Each run's output as measured: its actual size and frame count, which may differ from what was requested.
SCHEMA_V2 = """
ALTER TABLE runs ADD COLUMN output_width INTEGER;
ALTER TABLE runs ADD COLUMN output_height INTEGER;
ALTER TABLE runs ADD COLUMN output_frames INTEGER
"""

# Milestone 10: an execution ID of its own (E0012), numbered by start time for the rows already there; counters that only
# go up, so a pruned execution's number or a retired job's is never given again; each job file name's ID (J0001); and the
# TUI's remembered settings, such as the Job Definition widget's sort. Phase 3 Milestone 5 keeps the queue's hold there
# too, under ``queue_hold``, with no migration of its own.
SCHEMA_V3 = """
ALTER TABLE executions ADD COLUMN execution_number INTEGER;
UPDATE executions SET execution_number = (SELECT COUNT(*) FROM executions AS earlier WHERE earlier.started_epoch < executions.started_epoch OR (earlier.started_epoch = executions.started_epoch AND earlier.id <= executions.id));
CREATE UNIQUE INDEX executions_number ON executions (execution_number);
CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT INTO counters (name, value) VALUES ('execution', (SELECT COALESCE(MAX(execution_number), 0) FROM executions)), ('job', 0);
CREATE TABLE job_definitions (number INTEGER PRIMARY KEY, file_name TEXT NOT NULL UNIQUE, first_seen_at TEXT NOT NULL, present INTEGER NOT NULL DEFAULT 1);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)
"""

# Phase 3 Milestone 1: the queue table, and an execution's resume point. Entries are linked to executions, and to the
# entry they resume, by their public numbers (execution_number, a queue_number), never by a row id: a row id is
# internal, and a plain number (unlike a foreign key) survives the row it names being pruned, which is how a resume
# tells "its ancestor was pruned" apart from "never ran". A resumed entry's own ``resumes`` names the queue entry it
# continues (its own chain of resumes, walked back to resolve the resume point), while ``resumes_execution`` names
# the specific execution that resume point's last succeeded run came from, resolved once, at resume(), and shown on
# JobStarted, the manifest, and the resumed execution's own ``resumes`` column.
SCHEMA_V4 = """
ALTER TABLE executions ADD COLUMN first_run INTEGER NOT NULL DEFAULT 1;
ALTER TABLE executions ADD COLUMN resumes INTEGER;
CREATE TABLE queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_number INTEGER NOT NULL,
    job_path TEXT NOT NULL,
    job_text TEXT NOT NULL,
    config_file TEXT NOT NULL,
    config_text TEXT NOT NULL,
    input_directory TEXT NOT NULL,
    output_directory TEXT NOT NULL,
    cooldown_default TEXT,
    settings TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    submitted_epoch REAL NOT NULL,
    started_at TEXT,
    started_epoch REAL,
    finished_at TEXT,
    finished_epoch REAL,
    execution_number INTEGER,
    resumes INTEGER,
    resumes_execution INTEGER,
    resume_first_run INTEGER,
    resume_input TEXT,
    resume_seed INTEGER,
    error TEXT
);
CREATE UNIQUE INDEX queue_number ON queue (queue_number);
CREATE INDEX queue_state ON queue (state);
CREATE INDEX queue_submitted ON queue (submitted_epoch);
INSERT INTO counters (name, value) VALUES ('queue', 0)
"""

# Phase 3 Milestone 2: the audit log. Built with the first endpoints that accept input, so no submission or write
# through the HTTP API goes unrecorded; unlike the rest of the history, it is never pruned by history_retention_days
# (owner decision, phase-3-changelog.md), so its own row id (never shown to a caller) is stable enough to page by.
SCHEMA_V5 = """
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    at_epoch REAL NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    outcome TEXT NOT NULL,
    caller TEXT NOT NULL
);
CREATE INDEX audit_log_at ON audit_log (at_epoch DESC)
"""

# Milestone 3: the queue's own run count, so a queued entry -- or one that never reached JobStarted -- can show
# "run 0/7" without reparsing job_text (a YAML parse and a base-configuration merge) on every list read. Backfilled
# from a linked execution's own total_runs; a pre-migration row with no execution (a queued, or a never-started,
# entry) has nothing SQL can recover it from, and stays NULL until it is next resubmitted or resumed.
SCHEMA_V6 = """
ALTER TABLE queue ADD COLUMN total_runs INTEGER;
UPDATE queue SET total_runs = (SELECT e.total_runs FROM executions e WHERE e.execution_number = queue.execution_number) WHERE execution_number IS NOT NULL
"""

# Each media check of a video job's run (its input, the resized copy, the video, the last frame), as its
# ``media_checked`` event said it: kept per run number rather than per run row, since the input's checks come before
# the run's row exists. Deleted with its execution.
SCHEMA_V7 = """
CREATE TABLE media_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id INTEGER NOT NULL REFERENCES executions (id) ON DELETE CASCADE,
    run INTEGER NOT NULL,
    stage TEXT NOT NULL,
    file TEXT NOT NULL,
    summary TEXT NOT NULL,
    verdict TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '[]',
    facts TEXT NOT NULL DEFAULT '{}',
    at TEXT NOT NULL
);
CREATE INDEX media_checks_execution ON media_checks (execution_id, run)
"""

# Forward-only: migration N runs when the database is at N - 1. The list index is the version reached. Any open migrates,
# a browsing one too (owner decision): an upgrade is the one write a read-only screen may make.
MIGRATIONS: tuple[str, ...] = (SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5, SCHEMA_V6, SCHEMA_V7)
SCHEMA_VERSION = len(MIGRATIONS)

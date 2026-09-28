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
# TUI's remembered settings, such as the Job Definition widget's sort.
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

# Forward-only: migration N runs when the database is at N - 1. The list index is the version reached. Any open migrates,
# a browsing one too (owner decision): an upgrade is the one write a read-only screen may make.
MIGRATIONS: tuple[str, ...] = (SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4)
SCHEMA_VERSION = len(MIGRATIONS)

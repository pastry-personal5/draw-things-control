# Milestone 06: YAML Draw Things Configurations

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** done
**Depends on:** none (it touches configuration loading only)

## Goal

Let people write the base configurations in `dt-config/` as YAML, which is
easier to read, comment, and edit by hand than JSON. `draw-things-cli` still
reads only JSON, so the app converts the YAML and passes the result inline
with `--config-json`, as jobs already do. No JSON file is written.

## Scope

In scope:

- YAML base configurations in `dt-config/` (`.yaml` or `.yml`), named by a
  job's `config_file`; a job naming a `.json` file is rejected
- Strict YAML reading: one mapping with string keys and values JSON can hold
- `generate --config-file` and `validate-config` accepting YAML as well as
  JSON
- A one-time conversion, done in this milestone, of each current
  `dt-config/*.json` file into a `.yaml` file next to it, and the job files
  in `data/` (`example-job.yaml`, `duo.yaml`) pointing at the YAML files

Out of scope:

- Editing, deleting, or deduplicating any `dt-config/*.json` file. They stay
  exactly as they are ([development rules](../development-rules.md#project-layout)).
- A conversion command. The conversion is done once, by hand, in this
  milestone; after it, people write YAML themselves.
- Writing any configuration file from the app. It reads YAML and passes
  JSON inline; it writes nothing to `dt-config/` or `state/`.
- A hint in `/jobs` or `validate-job` about jobs that still name JSON
  files; the validation error is enough.
- Changing `--config-json`, which stays inline JSON.
- Rewriting execution history: executions recorded or imported with a
  `.json` `config_file` keep it.

## Planned changes

### Formats and who reads them

| File | Format | Written by | Read by |
|------|--------|------------|---------|
| `dt-config/<name>.yaml` (or `.yml`) | YAML | a person | `dtc` (jobs, `generate`, `validate-config`) |
| `dt-config/<name>.json` | JSON | a person (phase 1) | `generate --config-file` and `validate-config` only |
| the `--config-json` value | JSON | `dtc` (jobs, and `generate` with a YAML file) | `draw-things-cli` |

### Reading YAML (`core/configuration.py`)

- `load_config` chooses the parser by extension: `.yaml` and `.yml` (any
  letter case) are read with PyYAML (already a dependency) through a
  `yaml.SafeLoader` subclass; any other extension is read as JSON, as today.
- A YAML configuration is rejected (a `ValueError`, so exit code 2) with the
  file name, and the line where YAML reports one, when:
  - it is not valid YAML, is empty, or is not one mapping;
  - a key appears twice in one mapping (PyYAML otherwise keeps the last one
    without a word, which would hide a typo);
  - a key is not a string (`1: x`);
  - a value is something JSON cannot hold: a date or time (`2026-09-25`
    unquoted), binary data, a set, `.inf`, `-.inf`, or `.nan` (which
    `json.dumps` would write as `Infinity` or `NaN`, invalid JSON), or an
    alias that refers to itself. The error names the key path, for example
    `loras[0].version`.
- YAML 1.1 reads unquoted `yes`, `no`, `on`, and `off` as booleans. That is
  kept, since the Draw Things keys that take them are booleans; the user
  guide says to quote them when a string is meant.
- Otherwise values pass through unchanged and keys keep their order, so the
  YAML conversion of a JSON file loads to an equal object.

### A job's base configuration (`core/generation_config.py`)

- A job's `config_file` must name a `.yaml` or `.yml` file in `dt-config/`.
  The existing path checks stay.
- A `config_file` that names a `.json` file is a validation error (exit 2)
  saying YAML is required and the JSON file is left alone. When a YAML file
  with the same stem exists, the message names it:
  `'config_file' image-to-video-wan-2-2.example.json is JSON; name image-to-video-wan-2-2.example.yaml instead`.
  Otherwise it says to write one.
- The "available" list in the not-found message lists `*.yaml` and `*.yml`
  files only.
- Jobs pass the merged configuration to `draw-things-cli` with
  `--config-json`, as today; `jobs/job_service.py` does not change. The
  manifest and state store keep `config_file` as the YAML name the job
  gave, and the recorded command is unchanged in form.

### `generate --config-file` (`core/generation_service.py`)

- A `.json` file is passed to `draw-things-cli` with `--config-file`, as
  today.
- A `.yaml` or `.yml` file is loaded in `prepare`, and is not passed as a
  file. Its object, with any `--config-json` merged on top (the order dtc
  already uses for its own checks), is passed as one compact
  `--config-json`. So `--config-json` still overrides the file, as
  `draw-things-cli` documents for the two options.
- `--dry-run` previews that command, with the JSON inline.
- Nothing is written, so the run lock, the state directory, and the exit
  codes are unchanged. `generate` is still not recorded in the state store.

### `validate-config` (`cli/app.py`)

- Accepts YAML and JSON, and prints the same one-line result.
- The help text of `validate-config` and `generate --config-file` says
  "YAML or JSON configuration file".

### One-time conversion (done in this milestone)

- For each file in `dt-config/` today, write a YAML file with the same stem
  and values, keys in the same order, and a short header comment naming the
  model:
  - `image-to-video-wan-2-2.example.yaml` (tracked, like its JSON)
  - `image-to-video-wan-2-2-default.yaml` (ignored by git, like its JSON;
    `.gitignore` gains the line)
- Each YAML file is checked to load to an object equal to its JSON file.
  The JSON files are not opened for writing.
- `data/example-job.yaml` names the YAML example, and its comment says so.
- `data/duo.yaml` (the owner's job) changes only its `config_file` line, to
  `image-to-video-wan-2-2-default.yaml`.
- After this, `dt-config/*.yaml` are user files like the JSON files: never
  edited, deleted, or deduplicated by the app, tests, or tools.

### TUI

- No change in code. `/job` shows the YAML `config_file`; `/run` goes
  through `JobService`, which still passes inline JSON. A job naming a JSON
  file shows as invalid in `/jobs` with the message above.

## Documentation

In the same change:

- `docs/user-guide.md`: base configurations are YAML; the rules above
  (quoting `yes`/`no`, dates, duplicate keys); that a job's `config_file`
  must be YAML while `generate` and `validate-config` take either; that a
  YAML file given to `generate` is passed inline; the examples use the YAML
  example.
- `docs/architecture.md`: the formats table.
- `AGENTS.md` and `docs/development-rules.md`: `dt-config/*.yaml` are user
  files too; the JSON rule stays.
- Phase 2 changelog: an entry when the milestone lands.

## Acceptance criteria

- A job whose `config_file` names a YAML file validates, dry-runs, and runs,
  and passes the same `--config-json` it passed with the matching JSON file
  before this milestone.
- A job naming a `.json` configuration fails validation with exit code 2
  and the message above.
- Invalid YAML, a non-mapping document, a duplicate key, a non-string key,
  an unquoted date, and `.nan` each fail with exit code 2, naming the file
  and, where known, the line or key path.
- `generate --config-file` with YAML passes one `--config-json` equal to the
  YAML's object with any `--config-json` merged on top, and no
  `--config-file`; with JSON, the given file is passed, as before.
- No command writes a configuration file.
- `validate-config` accepts both formats.
- Both YAML files in `dt-config/` load to objects equal to their JSON files,
  and the JSON files are byte-for-byte unchanged.
- No test creates, changes, or removes a file in `dt-config/`; tests use
  temporary directories.
- `make check` passes.

## Tests

- `tests/core/test_generation_config.py`: YAML and `.yml` loading, the
  extension check, the JSON rejection with and without a matching YAML
  file, the not-found listing, and each rejected YAML case.
- `tests/core/test_service.py`: YAML passed inline, merged with
  `--config-json`, in the dry-run preview and the run; JSON passed through.
- `tests/cli/test_cli.py`: `validate-config` and `generate` with YAML and
  JSON.
- `tests/jobs/test_job_definition.py` and the job fixtures switch to YAML
  configurations; one test compares the planned `--config-json` from a YAML
  file with the one from the equal JSON file.

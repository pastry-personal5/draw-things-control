# Milestone 09: Get and Describe Commands

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** done
**Depends on:** [Milestone 05: Command layout and execution history](milestone-05-tui-run-history.md), [Milestone 08: Status widget and execution detail widget](milestone-08-tui-status-detail.md)

## Goal

Read what a past execution ran without scrolling its whole detail: its
prompts, and its `draw-things-cli` arguments as a table. Group the reading
commands under two verbs, `/get` and `/describe`.

## Scope

In scope (owner decision):

| Command | Does |
|---------|------|
| `/get jobs` | What `/jobs` did: list the job files and whether each is valid |
| `/describe job JOB` | What `/job` did: the summary, prompt pairs, and dry-run plan |
| `/get history` | What `/history` did: read the history pane again |
| `/get prompts ID [RUN]` | The positive and negative prompts |
| `/get positive ID [RUN]` | The positive prompts |
| `/get negative ID [RUN]` | The negative prompts |
| `/get param ID [RUN]`, `/get parameters ID [RUN]` | A run's `draw-things-cli` arguments without the prompts, as a text table, with overridden values |

Out of scope:

- Renaming the other commands (`/run`, `/stop`, `/execution`, `/filter`,
  `/reveal`, `/clear`, `/quit`, `/help`); they stay as they are (owner
  decision).
- Keeping `/jobs`, `/job`, and `/history` as aliases.
- Showing a job file's prompts or arguments before it runs; `/describe job`
  does that for a job file.

## Decisions

From the interview (owner decisions):

- `ID` is an execution's ID in the History pane, as for `/execution` and
  `/reveal`; the stored record is read, never the current job file.
- A job has several prompt pairs. Without `RUN`, each distinct pair is
  shown once with the runs that used it (`Pair walk (runs 1, 3)`); with
  `RUN`, that run's (`Run 2 (pair wave)`). A missing negative prompt reads
  `(none)`.
- `/get param` lists the run's saved command (default: its first run with
  one), credentials already redacted, without `--prompt`,
  `--negative-prompt`, and their file forms. The flags come first in the
  order the command gives them (a flag that stands alone reads `yes`), then
  each `--config-json` key as its own row. The note column shows the
  overridden values: `job override` on a value the job's configuration
  overrides set, `replaces --config-json 40` on a flag that replaced a
  configuration value, and `replaced by --steps 8` on that configuration
  row.
- `/jobs`, `/job`, and `/history` are removed. Typing one is an error that
  names the new form (`/jobs is now /get jobs; type /help`).

## Changes

- `tui/commands.py`: the `get` and `describe` forms in the command table,
  their completion (the words after `/get` and `/describe`, and job file
  names after `/describe job`), per-form usage (`usage("get", "positive")`),
  and the replaced names.
- `tui/screens.py`: `command_get` and `command_describe`; a worker reads the
  execution and writes the text to Messages.
- `tui/text.py`: `prompts_text`, `parameters_text`, and a plain-text table.
- `core/draw_things_arguments.py`: `command_arguments` (each flag and its
  value, a prompt that reads like a flag never parsed as one) and
  `config_json`, shared with `command_settings`.
- Help and the key descriptions updated.

## Acceptance criteria

- Each command in the table does what it says; `/get` accepts `RUN` only
  where the table shows it, and wrong arguments print that form's usage.
- The prompts are shown as written, never read as markup.
- `/get param` never shows a prompt, and marks each override and each
  replaced value as above.
- `/jobs`, `/job`, and `/history` run nothing and name their new form.
- `make check` passes.

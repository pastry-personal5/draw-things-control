# Milestone 12: TUI enhancement

**Phase:** [Phase 4](README.md)
**Status:** done
**Depends on:** [Milestone 09](milestone-09-job-definition.md) (done): the per-run H3 knobs and the frozen run plan; [Milestone 03](milestone-03-h3-i2av.md) for the `h3-metal-cli` command; and Phase 1's global config (`config/global-config.yaml`).

## Completion (2026-10-18)

Landed and verified with `make check`. As built: `H3_VALUE_FLAGS`, `H3_DEFAULT_ARGUMENTS` and `is_h3_command` sit beside `H3Arguments` in `jobs/backends/h3.py`; `command_arguments` takes the value-flag table, and `argument_rows` picks H3's from the command itself (an H3 command starts `executable -d`), so no caller passes a backend and mixed-backend plans read each run correctly. `file_opener` is validated in `core/global_config.py` (a bad value fails the load and names the key) and used by `tui/desktop.py`'s `open_run`.

## Goal

Four small TUI improvements: the dry-run plan in the activity log shows an H3 run's `--reuse` and `--core-reuse` values, the Execution detail shows each run's backend arguments at its bottom, and a file link in the Execution detail can open the file with an executable the owner chooses in the global config, instead of always revealing it in Finder, and the Queue list's cursor stays on its row when `c` cancels an entry.

## Evidence

1. **The dry-run plan misreads H3 arguments.** Selecting a job in the Jobs list writes its dry-run plan to the activity log (`load_details`, then `plan_text`, then `argument_rows` in `tui/text/arguments.py`). `argument_rows` reads the command with `core.arguments.command_arguments`, which knows only the value flags of `draw-things-cli generate` (`VALUE_FLAGS`). For an `h3-metal-cli` command every H3-only flag is unknown: `--reuse 2` is read as the standalone flag `--reuse` (shown as `yes`) and its value is dropped. Checked on the command `--steps 30 --reuse 2 --core-reuse 3 --layers 40`: `--steps` keeps its value, and `--reuse`, `--core-reuse` and `--layers` all come back with none. The same holds for every H3 value flag (`--first-frame`, `--ref-image*`, `--render-width`, `--ref-image-size`, and so on). The H3 command itself is correct (`H3Command.args`, `jobs/backends/h3.py`); only the TUI's reading of it is wrong. `--reuse` and `--core-reuse` are the ones the owner noticed because they are the only knobs whose value matters at a glance.
2. **The Execution detail shows no arguments.** `execution_detail_text` (`tui/text/execution.py`) lists the model, size, CFG and shift (Draw Things terms), the runs, and the selected run's stages. The arguments a run was started with appear only by `/get param`, and `parameters_text` is written for `draw-things-cli`, so it has the same blind spot as item 1 for H3 runs.
4. **`c` moves the Queue list's cursor away.** The Queue list lists active entries first and finished ones after, and `QueueList._show` re-selects the previously selected entry by ID. A cancelled queued entry moves from the active rows to the finished rows, so the cursor follows it down and the user must go back up to cancel the next one.
3. **A file link can only reveal.** The file name under each run is a link whose click (and Enter) runs `reveal_run`, which calls `open -R` (`tui/desktop.py`). There is no way to open the video in a player or an editor.

## Scope

### 1. H3 arguments in the dry-run plan

- Make the argument reader backend-aware. The caller already knows the backend (the job's `backend`, and a stored run's `command[0]`/backend column), so `argument_rows(command, notes, backend)` picks the backend's value-flag table.
- The H3 table is derived, not copied: the H3 backend declares its value flags beside `H3Command.args` (a `frozenset` of the flags that take a value, next to `_COMPATIBILITY_FLAGS`), and `tests/jobs/backends` pins that every flag `args` can emit is in exactly one of value or standalone. The `--config-json` merging, `FLAG_CONFIG_KEYS`, and "replaces" notes are Draw Things only and are skipped for H3, which has no config JSON.
- Show the H3 flags as flag rows with their real values (`--reuse 2`, `--core-reuse 3`, `--layers 40`), keeping the standalone ones as `yes` (`--token-reduction yes`). Paths and redaction stay as `plan_steps` already produces them.
- Mixed-backend jobs (Milestone 09) choose the table per run, so a Draw Things run followed by an H3 run is read correctly in the same plan, and "Arguments as run N, except" compares only runs of the same backend.
- **Show H3 defaults (owner decision).** When the command omits `--reuse`, `--core-reuse` or `--layers` (the job left the knob at its default), the plan and the Execution detail still list the row with its default value (`--reuse 1`, `--core-reuse 1`, `--layers 50`) and the note `default; not passed`. The defaults come from the H3 settings' own constants (`H3Settings` field defaults), not a TUI copy, and the rows are display only: the stored and executed command is unchanged. Boolean switches that are off (`--token-reduction`, and so on) and an unset render size stay unlisted, since they have no value to show. Defaults are not shown for Draw Things, whose defaults live in its own configuration.

### 2. Run arguments at the bottom of the Execution detail

- Under the selected run's stages, add a section `Arguments of run N`, drawn from the same `argument_rows` as the plan, so the dry-run plan and the stored run read alike and for both backends.
- Source: the run's saved `command` (already stored per run). A run with no saved command shows `-` dimmed, as a missing file does.
- Layout: the pane is narrow, so each row is `flag value` cut with `cut_middle` to the width, no note column; `--config-json` keys (Draw Things) follow as `key value` rows. Prompt flags are left out, as in `/get param` (they have `/get prompts`).
- The section is part of the detail's scroll content, below the stages and the `now:` line, and follows the selected run (Up/Down), like the stages.
- `/get param` is unchanged except that it now reads H3 commands correctly through item 1.
- Update `DETAIL_RUN_LINES`/`DETAIL_HEAD_LINES` users only if the click mapping depends on content below the runs; it does not, since the section is after them.

### 3. Open a file with a configured executable

- **New optional global config key: `file_opener`.** Chosen over `open_with` (reads as a verb, not a setting), `output_viewer` (the opener also serves non-output files such as logs) and `external_player` (too narrow): `file_opener` says what it is and mirrors how the rest of the file names things (`input_directory`, `output_directory`).
- Value: a string, the path of an executable (absolute, or a leading `~` expanded; or a bare name found on `PATH`, e.g. `mpv`). It must exist and be executable when the file loads, or the load fails with the key named, as the other keys do. Absent means today's behavior.
- Running it (owner decision): exactly one argument, the file's full absolute path: `[file_opener, /full/path/to/file]`, as an argument list, never shell text, started detached (no wait, stdout and stderr discarded) so a long-running player does not block the TUI. A start that fails (`OSError`) is reported in the status line in red, as `reveal_in_finder` reports. The key takes no extra arguments; an owner who needs flags points it at a small wrapper script.
- Which file: the same one `reveal_target` resolves (the run's output, looked up again, missing reported). This applies to the click on the file name and to Enter on the selected run.
- Behavior when set: a click or Enter opens the file with the executable. **Finder reveal stays the default (owner decision):** with the key unset nothing changes, and the key is the only way to change it. Finder reveal stays available through `/reveal <Execution ID> [RUN]`, which is unchanged and always reveals, so the owner never loses it. The shortcut help for Enter reads `Open the selected run's file` when `file_opener` is set and `Reveal the selected run in Finder` when it is not.
- Wiring: `GlobalConfig` gains `file_opener: Path | None`; the TUI already holds `settings` (`TuiApp.settings`), so `desktop.py` gets `open_run_file(execution, run_number, opener)` beside `reveal_run`, and the pane's `reveal` worker chooses between them. No server, CLI, MCP, or state-store change; the key is read by the TUI only. `config/global-config.yaml` is a user file and is never touched.
- **Global config example:** add a commented `file_opener` block to `config/global-config.example.yaml` (with a macOS example such as `/Applications/IINA.app/Contents/MacOS/iina-cli`), add the key to `GLOBAL_CONFIG_KEYS` and its validation, and document it in the user guide's global-config section.

### 4. The cursor stays on the row after `c` (owner decision)

- `c` records the entry's ID and the row it is on. When a later refresh shows that entry in a finished state (`cancelled`, or whatever a running entry ends as) while the cursor is still on it, the cursor goes to the same row index (clamped to the last row), which is now the next entry. Until then, and for every other update, the cursor follows the entry by ID as before, so a running entry that is still `stopping` does not lose it.
- Only `c` does this; refreshes the user did not cause keep following the selected entry.
- No server, API, or state change: `tui/panes/queue.py` only.

## Out of scope

- Per-extension openers, opener arguments beyond the file path, or a list of openers (YAGNI until wanted; a wrapper script covers them).
- Opening from the HTTP API, MCP, or CLI.
- Changing what `/reveal` does.
- Changing how a plan is built (`H3Command`) or the stored command.

## Decisions on scope

- `file_opener` applies only to the Execution detail's file links and Enter on its selected run (owner decision). The history, `/reveal`, and every other surface are unchanged.

## Planned changes

| Area | Change |
|------|--------|
| `jobs/backends/h3.py`, `core/arguments.py` | H3 declares its value flags; `command_arguments` takes the backend's table |
| `tui/text/arguments.py`, `tui/text/jobs.py` | `argument_rows` is backend-aware; the plan chooses per run |
| `tui/text/execution.py`, `tui/panes/execution.py` | `Arguments of run N` section; Enter/click opener choice; shortcut help text |
| `tui/desktop.py` | `open_run_file`, detached start, error reporting |
| `tui/panes/queue.py` | `c` keeps the cursor on its row |
| `core/global_config.py` | `file_opener` key, validation, `GlobalConfig.file_opener` |
| `config/global-config.example.yaml` | The commented key and example |
| Docs | `user-guide.md` (global config, Execution detail, shortcuts), `ux-terms.md` (open vs. reveal), this phase's README and changelog |

## Tests

- `tests/core`: `command_arguments` for an H3 command (`--reuse 2`, `--core-reuse 3`, `--layers 40`, a reference with an audio path, standalone flags), and that every flag `H3Command.args` can emit is classified.
- `tests/tui`: the plan of an H3 job shows `--reuse 2` and `--core-reuse 3`; a job that leaves them unset shows `--reuse 1`, `--core-reuse 1` and `--layers 50` marked `default; not passed`, and the defaults equal `H3Settings`' own; a mixed-backend plan reads each run by its own backend; the Execution detail shows the arguments section for an H3 and a Draw Things run and `-` for a run without a command; width cutting.
- `tests/core/test_global_config.py`: `file_opener` absent, relative path, non-executable, missing, `~` expansion, wrong type; the example file loads and contains the key.
- `tests/tui`: with `file_opener` set, a click and Enter run the injected opener with exactly `[opener, <full path>]` and not `open -R`; unset keeps Finder; `/reveal` reveals either way; a failing opener is reported. No test starts a real player or Finder: the process runner is injected.
- `tests/tui/test_park.py`: after `c` on the second row, the refreshed queue (the entry now among the finished) leaves the cursor on row 2, now the next entry, and a later update follows that entry by ID.
- `make check` passes.

## Acceptance criteria

- Selecting an H3 job in the Jobs list writes a plan whose rows show `--reuse` and `--core-reuse` with their values (and every other H3 flag with its value), including the defaults when the job leaves them unset, for single-backend and mixed-backend jobs.
- `c` on a Queue list row cancels the entry and leaves the cursor on that row, not on the cancelled entry's new place among the finished rows.
- The Execution detail ends with the selected run's arguments for both backends and follows the run selection.
- With `file_opener` set, clicking a file link or pressing Enter opens that file with the executable; without it, behavior is unchanged; `/reveal` always reveals.
- `config/global-config.example.yaml` documents `file_opener`, a bad value is rejected naming the key, and the user guide, `ux-terms.md`, the phase README and the changelog are current.

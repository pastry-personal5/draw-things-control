# Milestone 08: Status Widget and Execution Detail Widget

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** done
**Depends on:** [Milestone 04: Live run view](milestone-04-tui-live-run.md), [Milestone 05: Command layout and execution history](milestone-05-tui-run-history.md), [Milestone 07: Automatic cooldown](milestone-07-cooldown-auto.md) (the job estimate uses its `CooldownPolicy`)

## Goal

Show how far the running job has gone and when it will end, without reading
the child's output. Under the history table, show the selected execution's
generation settings (model, refiner, size, CFG, shift) and its successfully
finished runs. Each run shows its frames, steps, pure run time without
cooldown, and a link that reveals its video in Finder. The size and frame
count are measured from the output file itself, not taken from what was
requested.

## Scope

In scope:

- A **Status** widget above the draw-things-cli pane. It shows the job's
  status, an overall progress bar, a run progress bar, progress details, the
  time the job's last run took, and the estimated local clock time at which
  the current run and the current job finish.
- An **Execution** detail widget in the lower part of the right column.
  The history table shrinks to 5 visible rows above it. The widget follows
  the history cursor. It shows the execution's model, refiner, size, CFG,
  and shift, then its successfully finished runs only. Each run shows frames,
  steps, pure run time (no cooldown), and a clickable output link that
  reveals the file in Finder.
- Reading model, refiner, CFG, shift, and steps from each run's saved
  command, which the state store already keeps
- Measuring each successful run's **actual** output width, height, and
  frame count from the file it wrote, and recording them in `RunFinished`,
  the manifest, and the state store (schema version 2)
- The minimum terminal size raised from 80x24 to 80x30

Out of scope:

- Estimates from more than one past run, or from runs with matching
  settings. Before its own rate, a run is estimated from one past run: the
  latest successful run of any job (owner decision, see
  [Run estimate](#run-estimate)).
- Step progress or estimates for a job that another process runs. The
  Status widget only says that one is running.
- Playing a video from the TUI. The link reveals the file in Finder, like
  `/reveal`.
- Showing a requested width, height, or frame count (from the arguments or
  the configuration) anywhere in the widget (owner decision)
- Measuring outputs of runs recorded before this milestone. They show `-`
  (owner decision).
- Settings for runs with no saved command, such as imported runs from
  manifests that lack one
- Non-macOS reveal support
- Totals in the detail widget: no total job time and no total cooldown
  time (owner decision)
- Failed, timed-out, interrupted, and running runs in the detail widget.
  `/execution` still lists every run.

## Build order

1. **The step counter is known** (owner observation). In a Wan 2.2 run
   with a refiner, `draw-things-cli` shows one counter, `1/40`, `2/40`,
   `3/40`, and on through `40/40`, and it keeps counting when the refiner
   takes over. The capture step planned earlier is no longer needed. The
   estimate tests use synthetic events shaped like that output.
2. `jobs/media_info.py` and recording the actual size and frames
   (`RunFinished`, manifest, schema migration 2)
3. `command_settings`, then the Execution widget and its keyboard use
4. `tui/estimate.py`, then the Status widget
5. The layout, the docs, and the changelog

## Layout

```
┌ Status ────────────────────────────────────┐┌ History ───────────────────┐
│ running  walk.yaml  run 2/5                ││ ID Job    Status     Runs  │
│ Job ████░░░░ 38%  ends ~16:42 (in 23 min)  ││ 12 walk   running    1/5   │
│ Run ██████░░ 70%  ends ~14:07 (in 3 min)   ││ 11 walk   succeeded  5/5   │
│ step 28/40  03:12 elapsed                  ││ 10 walk   succeeded  5/5   │
│ last run took 7 min 12 s                   ││  9 walk   failed     2/5   │
└────────────────────────────────────────────┘│  8 walk   succeeded  5/5   │
┌ draw-things-cli ───────────────────────────┐└────────────────────────────┘
│ Run 2/5  03:12 elapsed  progress 28/40     │┌ Execution 12: walk ────────┐
│ (child output)                             ││ model wan_v2.2_a14…q8p.ckpt│
│                                            ││ refiner wan…ckpt from 10%  │
│                                            ││ 832x448  CFG 5  shift 3.99 │
└────────────────────────────────────────────┘│ #  Frames Steps Time       │
┌ Messages ──────────────────────────────────┐│▌1  81     40    7 min 12 s │
│ ...                                        ││   walk-0926-1402.mp4       │
│                                            ││                            │
│                                            ││                            │
└────────────────────────────────────────────┘└────────────────────────────┘
────────────────────────────────────────────────────────────────────────────
> █
────────────────────────────────────────────────────────────────────────────
 walk.yaml running run 2/5  |  /path/to/data  |  /help: commands
```

- **Left column**, from the top down:
  - **Status**: 7 lines, border included
  - **draw-things-cli**: 15 lines when there is room, never fewer than 7;
    its first line still shows the run line (owner decision)
  - **Messages**: at least 6 lines
- **Right column**:
  - **History** table: 5 visible rows (owner decision), so 8 lines with
    its border and header. Paging, filters, and polling work as in
    Milestone 05. At 80 columns the table is 36 wide and scrolls sideways;
    its horizontal scrollbar then gets a ninth line, so 5 rows still show
    (found when the milestone was built).
  - **Execution** detail: the rest of the height, scrolling when an
    execution has more runs than fit.
- The command line, the rules, and the status line do not change.
- In the sketch, `▌` marks the run selected in the Execution widget when
  it has focus. In the app, that run is highlighted instead.
- The minimum terminal size becomes 80x30 (owner decision). The existing
  shrink rule stays: draw-things-cli gives up lines, down to 7, so Messages
  keeps 6. `MainScreen.on_resize` subtracts the Status widget's 7 lines as
  well as the 4 bottom lines. At 80x30 that leaves draw-things-cli 13 lines
  and Messages 6. Below 80x30 the same rules still lay the screen out, but
  that size is not tested and nothing warns about it.
- At 80 columns History keeps its 36-column minimum, so the left column is
  44 columns wide and the Status widget has 42 inside its border.
- The Status and Execution widgets use the same style as the other panes:
  rounded border in the panel color and the title in the accent color. The
  Status widget is on black, like the draw-things-cli pane. The Execution
  widget keeps the theme background, like History.

The drawing is a sketch.

## Status widget

### Lines

| Line | Content |
|------|---------|
| 1 | The phase and the job: `starting`, `running`, `cooling down`, `stopping`, `finished (succeeded)`, `did not start`, or `A job is running in another process`. Also the job file and `run k/N` |
| 2 | `Job`, the overall bar, its percentage, and `ends ~HH:MM (in 23 min)` |
| 3 | `Run`, the run bar, its percentage, and `ends ~HH:MM (in 3 min)`. During a cooldown, a `Wait` bar in its place (below) |
| 4 | Progress details: `step n/total`, the percentage when the child prints only a percentage, and the run's elapsed time. During a cooldown: `next: run k/N` |
| 5 | `last run took 7 min 12 s`: the pure run time of the current job's last successfully finished run, without cooldown (owner decision). Empty while the job has no such run |

- An end time is the local wall-clock `HH:MM`, followed by the time left:
  `ends ~16:42 (in 23 min)` (owner decision). The time left is
  `duration_text` of whole seconds, so it never shows tenths. A time on a
  later day starts with the date (`09-27 02:10`). If there is no estimate
  yet, the line says `estimating` instead of a time.
- **Narrow lines** (owner decision): the label, percentage, and end text
  are always written in full, and the bar takes whatever width is left,
  down to nothing. In the 42 columns of an 80-column terminal, a same-day
  end time leaves the bar about 12 columns, and a date prefix leaves it
  about 4.
- Bars are drawn as text by plain functions in `tui/text.py`, with `█` for
  done and `░` for not done. They fill the width left after the label and
  the time. A bar without an estimate is drawn empty and dim.
- The widget redraws on every job event and on the screen's existing
  1-second tick.
- Durations in the widget (`last run took`, `job took`, `in …`) use
  `duration_text` of whole seconds: `7 min 12 s`, not `432.1 s`.
- **Cooldown** (owner decision): line 3 becomes a `Wait` bar, the same
  shape as the others. It fills as the wait passes, with its percentage and
  the time the wait ends: `Wait ██████░░░░░ 55%  ends ~14:05 (in 4 min)`.
  The seconds come from `CooldownStarted` and the countdown `LiveRun`
  already keeps. A wait cut short by a stop freezes like the other bars.
  The draw-things-cli pane's run line keeps its longer countdown text.
- **Stopping**: once a stop is requested, the bars stay where they were,
  both end times are replaced by `stopping`, and line 1 says `stopping`.
- **Did not start**: when the worker ends before `JobStarted` (for
  example, the run lock is held), line 1 says `did not start` and the other
  lines are empty. Messages has the reason.
- **Last run** (line 5, owner decision): shown only when the current job
  has a run that finished successfully. The time is that run's
  `RunFinished.seconds`: the time the `draw-things-cli` process ran, as
  the execution detail widget shows it. It leaves out the cooldown that
  follows, last-frame extraction, and color tagging, and it is not
  counted while the cooldown ticks. The line is empty on run 1, and it
  stays as it was during a cooldown and after the job ends. A failed,
  timed-out, or interrupted run does not replace it, since the job stops
  there.
- After the job ends, the widget keeps its final state until the next job
  starts:
  - line 1 shows the result (`finished (succeeded)`) and the job file;
  - line 2 shows runs succeeded of total;
  - line 4 shows the total job time (owner decision), from `JobStarted` to
    `JobFinished`, cooldowns included: `job took 1 h 12 min`;
  - line 5 shows the last successful run, as before;
  - the bars are gone.
- With no job in this session, and none in another process, the widget is
  empty: its border and title, and no text (owner decision).
- **Another process**: while another process holds the run lock (a
  `run-job` in another terminal, for example), line 1 says
  `A job is running in another process`, and the other lines are empty
  (owner decision). The widget takes this from the History pane's poll,
  which already checks the lock every 5 seconds, so it does not add a
  second timer. The pane also checks once when it is mounted, so a job
  already running when the TUI opens is shown at once rather than after 5
  seconds. When the lock is freed, the widget goes back to empty, or to
  this session's last job.

### Run estimate

Owner decision (a design change after the milestone was first built, which
replaced "step rate only"): a run is estimated as early as possible, from
the best measure there is at the time, and estimated again the moment
`draw-things-cli` reports a step.

| When | Remaining time |
|------|----------------|
| Before any step report | The **past run**'s `seconds`, less this run's elapsed time |
| At the first report (`1/40`) | The past run's time per step (`seconds / steps`), times the steps left, when `steps` equals the counter's total; otherwise as before any report |
| From the second report on | The live rate (below), times the steps left |
| After the last step | `finishing` (below) |

- **The past run** is this job's last successful run once it has one;
  before that, the latest successful run of **any** job in the state store
  (by start time), whatever its settings. The job's worker reads it with
  `Store.latest_succeeded_run` before the job starts and posts it to the
  app (`PastRunFound`), so the run has an estimate from its first moment.
  Its step count comes from its saved command (`command_settings`), or,
  for this job's own run, from its counter's total.
- Without a past run, or once this run has taken longer than the past run,
  the run says `estimating` until the live rate exists. A past run with no
  known step count, or one that differs from the counter's total (an
  image-to-image run, a refiner, a video's segments), keeps its
  time-less-elapsed estimate at the first report.
- Before the first report the run bar shows the share of the past run's
  time that has passed; from the first report on, `n/total`.

- **One counter per run** (owner observation): the counter goes from
  `1/total` to `total/total` once, refiner or not. `RunOutput` lines with
  a percentage but no counter do not change it.
- **Rate.** The time since the run's first counter reading, divided by the
  steps since then. Model loading before the first step is therefore not
  counted. It needs two readings; until then the past run stands in.
  Loading the refiner partway through may pause the counter. The average
  spreads that pause over the steps done so far, so the estimate rises
  just after the switch and then settles.
- **Remaining time**: the steps left (`total - n`) times the rate.
- **A counter that goes backward, or whose total changes**, is not
  expected. If it happens, the rate measurement starts again from that
  reading, and the bar follows the new counter. The estimate never mixes
  two counters.
- **After the last step.** Once the counter reaches `total/total`, line 3
  says `finishing` instead of a time. This covers the decode and the
  last-frame extraction, which have no counter. `LiveRun` records the time
  of each run's last counter reading, so the job estimate knows how long
  this tail took.
- **Run bar.** `n/total` of the counter. Before the first report, the
  share of the past run's time passed, or the child's percentage when it
  prints one.

### Job estimate

- The **estimated run time** W is the average full time of the runs this
  job has finished: from `RunStarted` to `RunFinished` on the TUI's clock.
  That includes model loading, the decode, last-frame extraction, color
  tagging, and measuring the output. Using `seconds` alone would make every
  remaining run look shorter than it is. Before the first run finishes, W
  is the current run's elapsed time plus its remaining time, when that is
  known (owner decision: this job's finished runs, not past executions).
- The **cooldown base** C is the average `seconds` of the same runs, since
  a wait follows the `draw-things-cli` time alone. Before the first run
  finishes, C is the same estimate as W.
- The **remaining time** is:
  - the current run's remaining time. Before its live rate exists (model
    loading, or the first step), from run 2 on, that is W less the run's
    elapsed time, never below 0: W includes the tail after
    `draw-things-cli`, which the past run's `seconds` leave out. On run 1
    it is the run's own estimate from the past run, so the job line has an
    estimate from the start (owner decision); only without any past run
    does it say `estimating`. While it is
    `finishing`, that is the **tail** (owner decision): how long the previous run of this job took
    from its last counter reading to `RunFinished`, less the time since
    this run's last reading, and never below 0. On run 1 there is no
    earlier tail, so it counts as 0;
  - plus the wait after the current run, unless it is the last run;
  - plus, for each run still to come, W and the wait after it, except
    after the last run.

  Each wait comes from the job's `CooldownPolicy`, which `JobStarted`
  carries. `wait_after(C)` gives the wait after each run still to come, and
  after the current run. So an `auto` wait is `ratio` of C within its
  bounds, a `manual` wait is fixed, and `off` means no wait.
  During a cooldown, the countdown's remaining seconds replace the
  current run's remaining time and the wait after it.
- **Overall bar** (owner decision: estimated time): the job's elapsed time,
  including cooldowns, divided by elapsed plus remaining. If there is no
  estimate yet, the bar is empty and the line says `estimating`.
- The job's `ends ~HH:MM` is now plus the remaining time.

### Code

- `tui/live_run.py`: `LiveRun` records, for the active run, the first and
  latest counter readings with their times. It keeps the previous run's
  tail (last reading to `RunFinished`), each finished run's `seconds` and
  full time (`RunStarted` to `RunFinished`), the cooldown policy from
  `JobStarted`, and the job's start time. All of this uses the existing injected clock, so tests control
  time.
- `tui/estimate.py` (new): pure functions that take a `LiveRun` and a time
  and return the run estimate, the job estimate, and both fractions. No
  widget code, so they are unit-tested directly.
- `tui/panes.py`: `StatusPane`, rendered from the app's `LiveRun` and the
  other-process flag. `HistoryPane.poll` publishes whether the lock is held
  by another process.
- `tui/text.py`: the bar, the five lines, and the clock time.

## Execution detail widget

### What it shows

- **Title**: `Execution ID: job name`, followed by the status in its status
  color.
- **Settings**, one per line, for the whole execution (see
  [Settings from the saved command](#settings-from-the-saved-command)):
  - `model`: the model file
  - `refiner`: the refiner model and the point where it starts, as a
    percentage (`wan_v2.2_a14b_lne_i2v_q8p.ckpt from 10%`), or `none`
  - the size, CFG, and shift on one line, the size written compactly as
    `WIDTHxHEIGHT` (owner decision): `832x448  CFG 5  shift 3.99`. The size
    is the **actual** size the listed runs' output files were measured at
    (see [Actual size and frames](#actual-size-and-frames)), never the size
    requested. If those runs differ, it reads `sizes vary`, and
    `/execution` lists each run's size. With no measured size (no
    successful run yet, or runs from before this milestone), it reads
    `size -`.

  A long model name is cut in the middle with `…`, so the start of the
  name and, for the refiner, ` from 10%` still show. `/execution` shows it
  in full.
- **Only successfully finished runs** (status `succeeded`), in run order
  (owner decision). Failed, timed-out, interrupted, and still-running runs
  are left out, so there is no status column. A run appears once it
  succeeds. The History row's `succeeded/total` already says how many of
  the planned runs this is.
- One header line for the runs: `#`, `Frames`, `Steps`, `Time`.
- Two lines per run:
  - The run number, as stored, so it matches `/reveal ID RUN`. Then its
    frames, steps, and time, or `-` when unknown.
  - The output file name as a link (owner decision: the name, not the
    full path, which `/execution` shows). A name wider than the widget is
    cut with `…`. A file that no longer exists shows `(missing)`, dimmed
    and not clickable.
- **Time is the pure run time** (owner decision): the run's stored
  `seconds`, shown with `duration_text` of whole seconds (`7 min 12 s`). The job service measures it around
  the `draw-things-cli` process alone, so it leaves out the cooldown after
  the run (stored separately as `cooldown_after_seconds`), last-frame
  extraction, and color tagging. The same run time drives the auto
  cooldown.
- **No totals** (owner decision): the widget shows no total job time, no
  total run time, and no total or per-run cooldown time.
- An execution with no successful run shows its settings and then
  `No run finished successfully`, dimmed.
- **Frames are the actual count** measured from the video (owner
  decision), not `--frames` or `numFrames`. Image runs have no frames, so
  the column shows `-`. So do runs recorded before this milestone and runs
  whose file could not be measured.
- Steps come from the run's saved command. A run whose saved command is
  missing or cannot be read shows `-` for steps. An imported run from a
  manifest without a command is one example.
- With no execution selected, or an empty history, the widget shows the
  History pane's message (for example `No execution history yet`). If the
  execution cannot be read, the widget shows the error in red.
- Text from the store is shown as written, never read as markup. The link
  is added as a click action (below), not by building markup from the file
  name.

### Following the cursor

Owner decision: the widget follows the history cursor.

- When the history row highlight moves, the widget reads that execution
  again. The read waits 0.2 seconds after the last move, so holding the
  arrow key does not read every row it passes. The read goes through the
  shared `HistoryReader` on a worker thread, and only the newest read's
  result is shown, as in the History pane.
- It is read again when the History pane updates that execution's row:
  when this TUI's job starts or finishes a run and when it ends, or when a
  poll refreshes a `running` row from another process.
- **Enter on a History row** (owner decision) now moves focus to the
  Execution widget, with its first run selected, instead of writing the
  detail to Messages. `/execution ID` still writes the full detail
  (prompts, paths, commands, and failed runs) to Messages, and it does not
  move the cursor.
- **A new job** (owner decision): when this TUI starts a job, the History
  pane's re-read on `JobStarted` puts the cursor on the new execution,
  which the recorder's `execution_id` names. The detail then shows it, and
  each run appears as it succeeds. If the History pane or the Execution
  widget has focus, the person is browsing, so the cursor stays where it
  is. If the active filter hides the new execution, the cursor stays too.
  A job started by another process never moves the cursor.

### Clicking the link

Owner decision: clicking reveals the file in Finder.

- A click on the file name runs the widget's `reveal` action with the
  execution ID and run number. The action is built from those two numbers
  only, never from the file name or any other stored text, so a file name
  can never become part of an action. The action uses `reveal_target` and
  `reveal_in_finder` from `tui/history.py`, which are what `/reveal ID RUN`
  uses. It runs `open -R <path>` on a worker thread, and the path is passed
  as an argument, never as shell text.
- A file that was removed after the widget was drawn, or a failing `open`,
  writes a message in red. The app never stops because of it.
- How Textual attaches a click action to part of a text is checked with
  Context7 when the milestone is built (a Rich `Style` with `@click` meta,
  or Content markup with the action). The file name must never be read as
  markup.

### Keyboard

Owner decision: the widget can be used from the keyboard as well as the
mouse.

- Tab moves from the command line to History, then to the Execution
  widget, and Shift-Tab moves back. Enter on a History row also moves to
  the Execution widget. The focused widget's border turns to
  the accent color, as History's does.
- In the Execution widget:
  - Up and Down move the selection between the listed runs, and the
    selected run is highlighted. PageUp, PageDown, Home, and End work too,
    and the widget scrolls to keep the selected run in view.
  - Enter reveals the selected run's output in Finder, as a click does.
  - A click on a run's first line selects that run. A click on its file
    name reveals it.
  - Escape returns to the command line, as it does in History.
- A missing file cannot be revealed. Enter on it writes a message.
- When the widget shows another execution, the selection returns to its
  first run. When the same execution is read again (a run has just
  succeeded), the selection stays on the same run number.
- **History's Escape** now focuses the command line directly. Today it
  moves focus to the next widget, which was the command line. With the
  Execution widget after History in the Tab order, the next widget would
  now be Execution instead.
- The Milestone 05 key table in `docs/user-guide.md` gains these rows, and
  its row for Enter in History changes.

### Code

- `tui/panes.py`: `ExecutionPane`, a focusable scrollable widget with its
  own bindings (Up, Down, PageUp, PageDown, Home, End, Enter, Escape) and a
  selected run. Clicking and Enter share its `reveal` action.
- `tui/screens.py`: the right column becomes a `Vertical` holding
  `HistoryPane` and `ExecutionPane`. It links the history's
  `RowHighlighted` and row refreshes to the detail. `on_resize` counts the
  Status widget.
- `tui/panes.py`: `HistoryPane.action_leave` focuses the command line, and
  Enter on a row (`RowSelected`) focuses the Execution widget instead of
  running `/execution`.
- `tui/text.py`: `execution_runs_text` builds the header and the run lines.

## Settings from the saved command

Owner decision: the state store already keeps each run's full command, with
credentials redacted. That command includes `--config-json`, the base
configuration merged with the job's overrides, exactly as it was passed to
`draw-things-cli`. Model, refiner, CFG, shift, and steps are read from it,
so executions from before this milestone show them too. Width, height, and
frames are not read from it: the widget shows the measured values instead
(see [Actual size and frames](#actual-size-and-frames)).

| Setting | Command flag (wins when present) | `--config-json` key |
|---------|----------------------------------|---------------------|
| model | `--model` | `model` |
| refiner | - | `refinerModel`, `refinerStart` |
| CFG | `--cfg` | `guidanceScale` |
| shift | - | `shift` |
| steps | `--steps` | `steps` |

- `core/draw_things_arguments.py`: `command_settings(command)` returns a
  frozen `CommandSettings` with the six values (model, refiner model,
  refiner start, CFG, shift, steps), each `None` when absent or
  unreadable. It sits beside the flags it reads, so the Phase 3 server can
  use it too. It never raises: an unreadable `--config-json`, a value of
  the wrong type, or a flag without a value gives `None` for the settings
  it affects.
- A flag wins over the same key in `--config-json`, as
  `draw-things-cli generate --help` sets out under "Configuration
  precedence" (saved in `docs/research/draw-things-cli-generate-help.txt`).
  The model's recommended settings come below both. A setting that is in
  neither the flags nor `--config-json` shows `-`. It is not the model's
  recommended value, which the command does not contain.
- Model, refiner, CFG, and shift are the same for every run of an
  execution, because the configuration is job-wide. The widget reads them
  from the first run that has a readable command. With none, `model` falls
  back to the execution's `model` column, and the others show `-`.
- Steps are read from each run's own command.
- `refinerStart` is shown as a whole percentage. A missing or empty
  `refinerModel` is `none`.
- `/execution` in Messages adds the same settings under the execution, and
  steps with the measured size and frames on each run's line, so the two
  views agree.
- The command stays as stored. `--api-key` and `--remote-shared-secret` are
  already redacted, and no other value in it is secret.

## Actual size and frames

Owner decision: record the size and frame count of the file each run
actually wrote. Draw Things may round a requested size to a multiple of 64,
or crop it, and a video model may change the frame count. Never record or
show the requested values in their place.

### Measuring

- `jobs/media_info.py` (new) returns `MediaInfo(width, height, frames)`
  for an output, or `None` when it cannot be read:
  - **Video** (`.mov`, `.mp4`): `ffprobe`, found beside the `ffmpeg` that
    video jobs already require, or else on `PATH`, as `has_matrix_tag`
    finds it.
    `-select_streams v:0 -show_entries stream=width,height,nb_frames -of json`.
    `width` and `height` are the displayed size, after any crop in the
    stream, not the padded coded size. `frames` is the stream's
    `nb_frames`. When the container does not give it, `-count_packets` is
    run and `nb_read_packets` is used: it reads packets without decoding
    them, so it stays fast.
  - **Image** (`.png`): width and height from the PNG's `IHDR` chunk, read
    from the file header with no external tool. `frames` is `None`.
  - The path is passed as an argument, never as shell text. There is a
    10-second limit, as for the other `ffprobe` call.
- `jobs/job_service.py` measures a run's output once the run has succeeded
  and its video's colors are tagged, before `RunFinished`. The measurement
  is outside the run's `seconds`, which stay the `draw-things-cli` time
  only. A file that cannot be measured is logged as a warning, and the
  run still succeeds with `None` values. Failed, timed-out, and
  interrupted runs are not measured.
- **ffprobe is required for video jobs** (owner decision), as `ffmpeg`
  is. `JobService._check_tools` looks for it when a video job starts, so a
  job never starts that could not record its actual size. A missing
  `ffprobe` stops the job before run 1, with exit code 2 and
  `Could not find 'ffprobe' beside ffmpeg or on PATH; video jobs need it to measure their outputs (it comes with ffmpeg, for example: brew install ffmpeg)`.
  A missing `ffprobe` never fails a run that has already started. If it
  cannot measure a file, that is a warning and `None` values.
- `JobService` takes the measuring function as an injected callable, like
  its video tagger, so tests never run `ffprobe`.

### Recording

- `jobs/job_events.py`: `RunFinished` gains `output_width`,
  `output_height`, and `output_frames` (`int | None`, default `None`).
  The `output_` prefix keeps them apart from the requested `width`,
  `height`, and `frame_count` of a job's configuration.
- `jobs/job_manifest.py`: `RunRecord` gains the same three fields. Older
  manifests without them still read.
- `state/store.py`: schema migration 2 adds three columns to `runs`:
  `output_width`, `output_height`, and `output_frames`, all `INTEGER`.
  `finish_run` takes them, and `RUN_COLUMNS` and `get_execution` include
  them. Existing rows get `NULL`. An older `dtc` then refuses the
  database as a newer schema, as the forward-only migration rule already
  says.
- `state/recorder.py` writes them from `RunFinished`.
  `state/history_import.py` copies them from a manifest run that has them.
- Runs recorded before this milestone keep `NULL`, and the widget shows
  `-` (owner decision). Browsing never runs `ffprobe`.
- **Schema upgrades on any open** (owner decision, for this and every later
  migration): the first open of a version 1 database runs migration 2,
  whatever opened it: `run-job`, `import-history`, or the TUI just
  browsing history. That upgrade is the one write browsing may make.
  Pruning stays off when browsing. `store.py` already migrates on open,
  under `BEGIN IMMEDIATE`, so a second process opening at the same moment
  waits and then finds the database upgraded.

## Documentation

In the same change:

- `docs/user-guide.md`: the TUI layout with the two new widgets, what each
  Status line means, how the estimates are made and when they are missing,
  the link, the key table rows for the Execution widget and History's
  Escape, and the minimum size of 80x30 (replacing 80x24)
- `docs/user-guide.md` also: the manifest's new run fields, and that video
  jobs need `ffprobe`, which comes with `ffmpeg`
- `docs/architecture.md`: `tui/estimate.py`, `StatusPane`,
  `ExecutionPane`, `command_settings`, `jobs/media_info.py`, the new
  `RunFinished` fields, schema version 2, and "Browsing writes nothing"
  (the TUI section) changed to allow a schema upgrade
- `AGENTS.md` and the phase README: the milestone's status
- Phase 2 changelog: an entry when the milestone lands

## Acceptance criteria

- At 80x30 and larger:
  - Status is 7 lines above draw-things-cli.
  - draw-things-cli is 15 lines when there is room and never fewer than 7.
  - Messages keeps at least 6 lines.
  - History is 8 lines, border and header included, so 5 rows show, and
    Execution fills the rest of the right column.
- While this TUI runs a job, with a fake clock and fake events:
  - Line 1 shows the phase and `run k/N`.
  - The run bar and the run's `ends ~` follow the step rate from the second
    counter reading on, and exclude the time before the first step.
  - A counter that goes backward or changes its total restarts the rate
    from that reading, with no negative or mixed estimate.
  - After the last step, the line says `finishing`.
  - The job's `ends ~` is the current run's remaining time, plus W per
    remaining run, plus each wait (from C) from the job's `CooldownPolicy` (auto,
    manual, and off), with no wait after the last run.
  - The overall bar is elapsed / (elapsed + remaining).
  - Line 5 is empty until run 1 succeeds. It then shows that run's
    `seconds`, not including the cooldown after it. It stays during the
    cooldown and after the job ends, and a failed run does not replace
    it.
- Before any estimate exists, the bars are empty and say `estimating`; no
  division by zero and no negative time.
- While finishing, from run 2 on, the job estimate adds the previous run's
  tail, less the time already spent on it. On run 1 it adds nothing.
- Stopping freezes the bars and shows `stopping` in place of both end
  times.
- In 42 columns the text of lines 2 and 3 is written in full, and the bar
  takes what is left, down to nothing.
- With no job in the session, the Status widget has no text. A job already
  running in another process when the TUI opens is shown without waiting
  for the first poll.
- Durations read `7 min 12 s`, never `432.1 s`.
- Escape in History focuses the command line.
- While another process holds the run lock, line 1 says
  `A job is running in another process`, and the other lines are empty.
- Moving the history cursor updates the Execution widget after the pause,
  and only for the last row reached. A running execution's widget updates
  when its runs finish.
- The widget shows model, refiner (with its start), CFG, and shift from the
  saved command, and the size as `832x448` from the measured outputs. It
  shows `sizes vary` when runs differ, and `size -` when none was
  measured. Only `succeeded` runs are listed. Failed, timed-out,
  interrupted, and running runs are not, and an execution without one says
  `No run finished successfully`. Each run line shows the measured frames,
  the steps, and its stored `seconds` with no cooldown added. No total job
  time or cooldown time appears. An execution recorded before this
  milestone shows its settings and steps, and `-` for size and frames.
  Image runs show `-` for frames. Runs without a readable command show `-`
  for steps.
- No requested width, height, or frame count (from `--width`,
  `--height`, `--frames`, or the configuration) appears in the widget.
- A succeeded video run records the width, height, and frame count
  `ffprobe` reports for its file, in `RunFinished`, the manifest, and the
  store. A PNG run records its header's size. A file that cannot be
  measured leaves them `None`, and the run still succeeds. The run's
  `seconds` do not include the measuring.
- A version 1 database migrates to version 2 with every row kept, and old
  manifests still read and import.
- Clicking an existing output, or selecting it and pressing Enter, calls
  the fake `open -R` with its path. A missing file is not a link, and a
  failing `open` is reported in Messages without closing the app.
- Tab reaches the Execution widget after History. Up and Down move the
  selection, and Escape returns to the command line.
- When this TUI starts a job, the cursor moves to the new execution unless
  History or Execution has focus.
- End times read `ends ~HH:MM (in …)`. After the job ends, the Status
  widget shows the total job time with cooldowns included, and the bars
  are gone.
- `RunStarted` does not change.
- During a cooldown, line 3 is a `Wait` bar that fills as the wait passes,
  with `ends ~HH:MM (in …)`.
- The job estimate uses each finished run's full time, not its
  `seconds`. It bases the waits on `seconds`, and it keeps an estimate
  across the start of runs 2 and later.
- A video job with no `ffprobe` fails before run 1 with exit code 2 and the
  message above. An image job does not need it.
- Opening a version 1 database from the TUI's history upgrades it to
  version 2, and does nothing else.
- Enter on a History row focuses the Execution widget with its first run
  selected. `/execution ID` still writes the full detail to Messages.
- A reveal action carries only the execution and run numbers.
- No file name or store text is read as markup, and no credential value
  appears in either widget.
- `make check` passes.

## Tests

- `tests/tui/test_estimate.py` (new): a synthetic 1/40 to 40/40 run with a
  fake clock, including a pause at the refiner switch, the rate, a counter
  that goes backward or changes its total, `finishing` and the tail, the
  job estimate for each cooldown mode (W for runs, C for waits), the
  estimate kept across a run's start, the overall fraction, and the empty
  and zero cases
- `tests/tui/test_text.py` or the existing text tests: bars at several
  widths down to none, end times on the same and a later day, and
  durations in whole seconds
- `tests/tui/test_live_run.py`: the Status lines through a job run with a
  fake clock, the end-time format, the `Wait` bar and `next:` line,
  stopping, did not start, the empty idle widget, the final state with the
  total job time, another process holding the lock, and the
  cursor moving to the new execution unless History or Execution has
  focus
- `tests/tui/test_history.py`: the Execution widget following the cursor
  with the pause, a run appearing once it succeeds, failed and interrupted
  runs left out, the empty-run message, no totals, the settings (the
  measured size as `832x448`, `sizes vary`, and `size -`), measured frames
  and steps, no requested size or frame count shown, `-` for missing
  values, a long model name and file name cut with `…`, the missing-file mark, a successful and a failing click-to-reveal,
  a file name with brackets, the reveal action holding only numbers, and
  the keyboard (Tab order, Enter in History focusing the widget, Up and Down,
  Enter to reveal, Enter on a missing file, Escape from both panes, the
  selection kept when a run is added, and reset when the execution
  changes), and the cursor staying put when a filter hides a new execution
- `tests/tui/test_tui.py`: the layout at 80x30 (draw-things-cli 13 lines,
  Messages 6) and at a larger size
- `tests/core/test_arguments.py`: `command_settings` from
  `--config-json` alone, with flags overriding it, with no refiner, and
  with an unreadable or missing `--config-json`, a flag at the end with no
  value, and values of the wrong type
- `tests/tui/test_tui.py` or `test_history.py`: `/execution` showing the
  same settings, with the measured size and frames per run
- `tests/jobs/test_media_info.py` (new): the `ffprobe` JSON parsed from a
  fake runner, with `nb_frames` and with the `-count_packets` fallback;
  displayed size over coded size; a failing, missing, or slow `ffprobe`;
  PNG headers read from small files the test writes; a file that is not a
  PNG, or is cut short
- `tests/jobs/test_job_service.py`: `RunFinished` and the manifest carry
  the measured values from the injected measurer; `None` with a warning
  when it fails; no measuring for failed runs; `seconds` unchanged; a
  video job stopped before run 1 when `ffprobe` is missing, and an image
  job that runs without it
- `tests/state/`: migration from version 1 with rows kept, including when
  the first open is the TUI's reader; recording; and importing a manifest
  with and without the fields

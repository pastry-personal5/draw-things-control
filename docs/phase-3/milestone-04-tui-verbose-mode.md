# Milestone 04: TUI verbose mode

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** done (2026-09-29)
**Depends on:** [Milestone 03: Queue for people](milestone-03-queue-for-people.md), whose shared
`WatchEvents(include_output=true)` subscription (`tui/feed.py`) this milestone makes conditional.

## Goal

Let a person trade the `draw-things-cli` pane's detail for less noise and less traffic from `dtc serve`, with a
`/verbose high|medium|low` command. `high` is today's behavior, unchanged: every `run_output` line is streamed live
and shown, and the Status widget, the run line, and the bottom status line all refresh every second. `medium` still
streams every line, but the pane only shows the first minute of it (of each run, or from the moment `medium` is
chosen), then goes quiet. `low` reconnects the shared gRPC stream with `include_output=false`, so `dtc serve` stops
sending bare output at all, and refreshes every periodic widget once a minute: elapsed time, run number, and
cooldown, timed locally from the structured events low mode still receives live, only rendered less often. Low
mode shows no step counter (it arrives only as `run_output`, which `include_output=false` drops), and tells the
person so, and that status updates once a minute, when it is chosen.

## Scope

In scope:

- The `/verbose` command (`tui/commands.py`, `tui/controller.py`): `/verbose` alone reports the current level;
  `/verbose high|medium|low` (case-insensitive) sets it.
- A small, TUI-only preferences file (`config/tui-preferences.yaml`, gitignored like `config/server-token`) that
  persists the level across restarts. `config/global-config.yaml` is not touched: it is machine-wide, shared with
  `dtc serve`, hand-edited, and strictly validated against a closed key set (`core/global_config.py`), and phase
  3's own non-goals already rule out writing it from any interface.
- Reconnecting the shared `WatchEvents` stream with a different `include_output` when a `/verbose` switch crosses
  the low/not-low boundary, with a message telling the person it is happening.
- Gating what the `draw-things-cli` pane (`tui/panes/cli_output.py`) writes, and how often `MainScreen.tick()`
  (`tui/screens.py`) re-renders the Status widget, the run line, and the bottom status line.

Out of scope:

- Any polling of `GET /queue/{id}` in low mode (owner decision): the step counter is simply not shown there.
- Changing what the Queue widget receives or how often it refreshes: `queue_*` events are never `run_output` and
  are unaffected by `include_output`; the Queue widget keeps updating live at every level.
- Changing anything server-side: `server/grpc_service.py`'s `_wanted` already filters `run_output` per subscriber
  by `request.include_output` (Milestone 02); this milestone only ever varies what the TUI's one client requests
  and how it renders what arrives.
- A verbose level for `dtc queue` or `dtc mcp`; both are out of scope for "TUI verbose mode" by name, and neither
  has a `draw-things-cli` pane to gate.
- Any change to `event_to_dict`/`event_from_dict` or the backlog: this milestone reads events exactly as
  Milestone 03 defined them.

## Planned changes

### Preferences: `tui/preferences.py`

- `VerboseLevel = Literal["high", "medium", "low"]`; `DEFAULT_VERBOSE_LEVEL: VerboseLevel = "high"`.
- `MEDIUM_OUTPUT_WINDOW_SECONDS = 60`, `LOW_STATUS_REFRESH_SECONDS = 60`: the two "1 minute"s named separately,
  since nothing ties their values together beyond both being owner-chosen defaults.
- `ProjectPaths` (`core/paths.py`) gains `tui_preferences -> root / "config" / "tui-preferences.yaml"`, added to
  `.gitignore` beside `config/global-config.yaml` and `config/server-token`: a per-machine UI preference, not
  project configuration.
- `load_verbose_level(path: Path) -> VerboseLevel`: missing file, unreadable YAML, or an unrecognized value all
  return `DEFAULT_VERBOSE_LEVEL` rather than raising — unlike `global_config.py`'s strict reader, a malformed
  preferences file is never worth blocking the TUI over, only worth silently forgetting.
- `save_verbose_level(path: Path, level: VerboseLevel) -> None`: creates `config/` if it is missing (a test's
  temporary `ProjectPaths` has none), writes `{"version": 1, "verbose_level": level}`
  with a plain `yaml.safe_dump`; no reader outside the TUI needs the strict loader's guarantees for a file only the
  TUI ever writes. A write that fails (`OSError`) is not fatal: `set_verbose_level` still applies the level for the
  session and says the preference could not be saved.
- `DrawThingsApp.__init__` loads the level once via `load_verbose_level(self.paths.tui_preferences)` into
  `self.verbose_level`; nothing re-reads the file afterwards.

### The `/verbose` command

- `tui/commands.py`'s `COMMANDS` gains `("verbose", "[high|medium|low]", "Show, or set, how much of
  draw-things-cli's output and status detail the TUI shows and how often it streams (high: everything; medium:
  a run's first minute of output; low: no output, status once a minute)")`; `COMMAND_NAMES` picks it up automatically. A `VERBOSE_WORDS = ("high", "medium", "low")` tuple is
  offered after `/verbose` by a new `elif words == [f"{PREFIX}verbose"]` branch in `completions()` (which
  `CommandSuggester` calls), the way `GET_WORDS` and `QUEUE_WORDS` already are; that function is 38 of the 40 lines
  the size rule allows, so the branch may need a helper.
- `tui/controller.py`'s `CommandController` gains `command_verbose(self, level: str = "") -> None`:
  - No argument: `self.screen.say(f"Verbose level: {self.screen.dtc.verbose_level}.")`.
  - An argument not in `VERBOSE_WORDS` (case-folded): raise `CommandError(f"Usage: {usage('verbose')}")`, as
    `command_sort` does for a bad key.
  - Otherwise: `self.screen.dtc.set_verbose_level(level)`.
- `DrawThingsApp.set_verbose_level(self, level: VerboseLevel) -> None`:
  - No-ops (still says the level) if `level == self.verbose_level`.
  - Sets `self.verbose_level = level`, calls `save_verbose_level(self.paths.tui_preferences, level)`, and says
    `f"Verbose level: {level}."`.
  - If `_wants_output(level) != _wants_output(old_level)` (`_wants_output = level != "low"`), says a second line
    first — `f"Reconnecting to dtc serve for verbose {level}..."` — then calls `self.run_feed()` again. `run_feed`
    is already `@work(exclusive=True, group="feed")` ([`tui/app.py`](../../src/draw_things_control/tui/app.py)), so
    the call cancels the running `QueueFeed.run()` worker and starts a fresh one; `QueueFeed` does not set
    `_reopen_fresh` on a cancellation (only a failed token read or health check does today, and a `/verbose` switch
    is neither), so the new `_watch` opens `WatchEvents` with `request.last_event_id = self._last_event_id`, not
    `FORCE_RESET_ID`.
  - **The resume is best-effort, not guaranteed.** The backlog (`server/event_backlog.py`) stores every event
    system-wide, filtering included, since `EventBacklog` itself knows nothing of any one subscriber's
    `include_output`; only `MonitorServicer.WatchEvents`'s own per-connection loop applies `_wanted` before
    deciding what to `yield` (`server/grpc_service.py`), and its own `last_id` cursor advances past a filtered
    event even though it was never sent. So while a subscriber is connected with `include_output=false`, its own
    `QueueFeed._last_event_id` only advances on events it actually *receives* — never on filtered `run_output` —
    and stays pinned at whatever structured event last arrived. A single run that prints more than the backlog's
    2000-event capacity of output while the TUI sits in low mode can push that pinned ID out of the backlog
    entirely by the time `/verbose` switches back to `high` or `medium`; `since()` then returns `None`, and the
    server sends `Reset` (the same path a real disconnect already takes), which reseeds the pane exactly as
    `_reseed` does today, clearing the log and showing "(earlier output not shown)". Both outcomes are expected and
    need no new handling: the existing Reset/reseed path already covers the rare one correctly.
  - The other direction (low → not-low) has its own visible effect worth naming: if the backlog can still explain
    the gap, the resumed stream replays every `run_output` line the backlog captured while low was filtering them,
    and they land in the pane as one burst back-to-back, not as they were originally produced.
  - Nothing else about a mode switch touches `LiveRun` or the Queue widget: only the stream's own `include_output`
    changes. Switching into low with a job running says `"Verbose level: low. Bare output is hidden and status updates
    once a minute."` (the same wording applies when low is the level at startup, written once to Messages as the
    feed first connects); the pane's own marker is below.

- With the server down (the feed not connected), a switch across the low boundary still persists and applies, and
  says `"Verbose level: low. It applies when dtc serve is reachable."` instead of "Reconnecting...", since there is
  nothing to reconnect to; the feed's own retry (every `RECONNECT_SECONDS`) opens with the new flag.
- `CliPane` never imports the app (panes sit below `screens.py`), so `MainScreen` passes the level down as an
  argument to `CliPane.new_job`, `show`, and `write_output`.

### The feed: `include_output` per level

- `QueueFeed._watch` (`tui/feed.py`) passes `include_output=self._app.verbose_level != "low"` instead of the
  literal `True` it opens with today.
- No other change to `feed.py`'s reconnect, reseed, or seeding logic: a level that is not low behaves exactly as
  Milestone 03 built it, since `high` and `medium` both request every `run_output` line and differ only in what the
  pane does with them once they arrive (below).

### Low mode's status

Once `include_output=false`, `server/grpc_service.py`'s `_wanted` (`return include_output or event.kind !=
RUN_OUTPUT_KIND`) drops every `run_output` event at the source: the bare text lines, and the progress/percent ones
too, since both share the one `"run_output"` kind (`jobs/events.py`). What low mode still receives live is every
structured event (`JobStarted`, `RunStarted`, `RunFinished`, `CooldownStarted`, and the queue's own), so run number,
elapsed time (timed locally from `LiveRun.run_started_at`), cooldown, run and job end, and history refresh stay as
correct and as timely as at any level; only how often the widgets *render* them is throttled (below).

- The one thing that does not survive is the run line's step counter, which arrives only as `run_output`. Left
  alone it would freeze at whatever it last read, which is worse than absent, so while low is selected the run line
  omits it (`run_line_text` takes the level, passed down from `MainScreen`, as `CliPane` does below). Nothing polls
  `GET /queue/{id}` to recover it (owner decision): "low" means less detail, and this is part of it.
- The person is told once, when low is chosen (or is the level at startup), in Messages and in the pane's marker
  (below): bare output is hidden and status updates once a minute.

### Gating what renders

- `tui/panes/cli_output.py`'s `CliPane.write_output` skips writing a line (but still advances `self.written`, so
  no line is ever written late once visibility returns) when either:
  - the level is `"low"` — bare output is never shown at that level, for as long as it stays selected, not only
    before the pane's first line; or
  - the level is `"medium"` and the medium window has closed: `live.now() - window_start >
    MEDIUM_OUTPUT_WINDOW_SECONDS`, where `window_start` is the later of the active run's `live.run_started_at` and
    `live.output_window_start`, a new `LiveRun` field (`None` until set) that `set_verbose_level` sets to
    `live.now()` whenever the level changes *into* medium with a job running (owner decision: switching to medium
    always opens a fresh minute, so a person who types `/verbose medium` mid-run sees output at once). Every later
    run starts after that timestamp, so from the next run on the window is again the run's own first minute. Both
    values come from the `now()`/clock `LiveRun` already fakes in tests
    ([Milestone 03's `LiveRun`](milestone-03-queue-for-people.md#the-tuis-live-output)). A run seeded while
    already in medium (an attach, a `Reset`) counts from its real start, which `_seed_active_run` restores, so an
    attach more than a minute into a run shows nothing until the next run. A line that arrives outside the window
    is skipped for good, not buffered: switching to `high` later shows lines from then on, not the ones hidden
    before (owner decision).
  - Progress lines (`Processing... [ ] 2  %`, `Sampling... 2 / 40 [ ] 4  %`) are ordinary output lines for this gate,
    since the first step below makes the pane show them: medium hides them after the window like any other line,
    and the run line's own progress readout, fed by the same events, is not gated (low has none, above).
  - Medium writes its dim marker, `"(output hidden: verbose medium, after 1 min)"`, the first time
    `write_output` actually skips a line past a run's window — there is always at least one line to trigger it,
    since the server keeps sending them. Low is different: `include_output=false` means no `run_output` ever
    arrives to be skipped, so a marker that only fires from inside `write_output` would never show at all. Low's
    marker, `"(output hidden: verbose low; status updates once a minute)"`, is instead written from wherever the pane's content is (re)established
    while low is selected: `DrawThingsApp.set_verbose_level`, when switching into low while a job is running, and
    `CliPane.new_job`, whenever the level is low at the time a job starts or is seeded — which also puts the marker
    back after a `Reset` clears the log (above), beside "(earlier output not shown)" when both apply. Either
    marker, like `"(earlier output not shown)"`, tells the person why the pane went quiet rather than leaving them
    to guess.
- `MainScreen.tick(self, *, force: bool = False)` (`tui/screens.py`) gains the throttle: while
  `self.dtc.verbose_level == "low"` and `force` is false, the Status widget, the run line, and the bottom status
  line only actually re-render once `LOW_STATUS_REFRESH_SECONDS` have passed since the last one did (a
  `self._last_low_render: float` timestamp on `MainScreen`, reset whenever a render actually happens). `force`
  defaults to false and is passed by the one caller that fires every second regardless of anything happening —
  `on_mount`'s `set_interval(1, self.tick)` — and to true by every other existing caller, since each of those runs
  only when something worth showing at once just occurred, never on a fixed timer: `render_live` (called from
  every `job_event`, and so from every structured event low mode still delivers live — `RunStarted`, `RunFinished`,
  `CooldownStarted` — in addition to high and medium's own per-line calls), `job_started`, `job_ended` (through its
  own `render_live` call), and
  `DrawThingsApp.show_status` (`tui/app.py`), which `action_interrupt` already calls on every Ctrl-C press to
  render `quit_armed`'s prompt — without `force=True` there, a stale quit prompt could sit unrendered for up to a
  minute in low mode, which would be actively misleading, not merely coarse. `job_event`'s own `self.say(text)` of
  the event line into Messages is unaffected by any of this at any level: a message logged for an event is not
  "periodic status," and stays immediate exactly as the events themselves already are. One caller bypasses the
  gate entirely rather than forcing through it: `on_history_pane_lock_changed` (`tui/screens.py`) calls
  `render_status()` directly, not `tick()`, so a run-lock change always renders the Status widget at once at every
  level — the gate lives in `tick()`'s own body, wrapping its three renders, not in `render_status()` itself. This
  is the same "a rare, meaningful change shows immediately" rule the forced callers above already follow, just
  reached by a caller that never goes through `tick()` at all.

### First steps (before the feature)

- **The pane shows progress lines.** `LiveRun._run_output` used to keep a progress line out of `LiveRun.output` and
  `CliPane.show` skipped writing for one, so `Processing... [ ] 2  %` and every `Sampling... N / 40 [ ] N  %` line
  reached only the run line and never the `draw-things-cli` pane (Milestone 03's own display rule, "a step-progress
  line updating the run line instead of filling the pane", is reversed here at the owner's request). Both now
  write it: the line goes into `output` and the pane like any other, and still updates `progress` and `percent`.
  A seeded reading (`_seed_active_run` applies a `RunOutput` with empty text) adds no line. Done, with
  `tests/tui/test_pane_output.py`.
- **Size and test prerequisites** (found reviewing the code; both done, see "Built" below):

- `tests/test_architecture.py` already fails on `main`: `MainScreen` (`tui/screens.py`) is 252 lines, over
  `MAX_CLASS_LINES = 250`. This milestone adds to it (`tick(force)`, `_last_low_render`, the level passed to the
  panes), so it starts by moving something out of `MainScreen` (the running-job methods `job_started`, `job_event`,
  `job_ended`, `render_live`, `render_status`, `tick` are one cohesive group) rather than adding to a class that
  is already over the limit. `DrawThingsApp` (185 lines) gains `verbose_level`, `set_verbose_level`, and
  `DrawThingsApp.show_status` calling `tick(force=True)` and stays under it.
- `tests/tui/tui_case.py` and `tests/tui/sigterm_app.py` still build `DrawThingsApp` with the arguments Milestone 03
  removed (`executable`, `job_executor`, `shutdown_grace`), and `tests.tui.test_tui`, `test_live_run`, and
  `test_commands` currently report 2 failures and 28 errors. The tests this milestone lists need a harness that
  builds the app with `server_url`, `token_file`, `http_transport`, and `grpc_stub_factory`, so repairing it is the
  first step here unless Milestone 03's own finishing work has done it by then.

### Built

Both first steps are done, and the feature followed the plan above, with these details the plan left open:

- **Where the code is.** `tui/preferences.py` (levels, the two 60-second constants, `wants_output`, load and save),
  `tui/running_job.py` (`RunningJobView`, the running-job methods moved out of `MainScreen`, which now holds it as
  `screen.running`; `tick(force)` and the low throttle live there, on an injectable `clock`), `CliPane` (the level
  is an argument of `new_job`, `show`, `tick`, and `write_output`; `MEDIUM_MARKER` and `LOW_MARKER`),
  `DrawThingsApp.set_verbose_level`, and `CommandController.command_verbose`.
- **Messages, by case.** Any change says `Verbose level: LEVEL.` (into low, `LOW_NOTICE`, the wording above); a change
  across the low boundary with the feed connected then says `Reconnecting to dtc serve for verbose LEVEL...`; with
  the feed down, either direction says `Verbose level: LEVEL. It applies when dtc serve is reachable.` instead. A
  failed save adds one warning line. The startup notice for low is said once, when the feed first connects, and not
  again by a later reconnect.
- **Medium between runs.** With neither a run's start nor a `/verbose medium` time known, nothing says the window
  has closed, so a line is written. The medium marker is written once per closed window (per window start), so a
  second run's own window gets its own marker.
- **Low hides all of the run line's progress**, the percent as well as the step count, since both arrive only as
  run output.
- **Low drops the step readings too** (`LiveRun.forget_progress`), when `/verbose low` is typed mid-run and when a
  job is attached to at low (the seeding read's `current_step`), so the Status widget's `step N/M` line and run bar
  never freeze at a stale reading either; the run then estimates as one begun at low does (from elapsed time and the
  past run, not per step). A switch of level, and a confirmed `/stop`, render at once rather than at low's next
  once-a-minute refresh.
- **A race found on the way.** A feed that connected (and seeded a running entry) before `MainScreen` was mounted
  lost its pane content and the low notice; `MainScreen.on_mount` now shows an already-followed job as an attach.
- **The test harness** builds the app against `tests/tui/fake_server.py` (an HTTP `MockTransport` and a gRPC stub
  that records each `WatchEventsRequest`, replays the backlog on a resumed call, and filters `run_output` per call
  as the server does). `tests/tui/test_live_run.py` was rewritten around it; the tests of retired local execution
  (the confirmation, the busy lock, signals stopping a local job, `run-job` files), `sigterm_app.py`, and
  `fake_runs.py` were removed. The acceptance tests are in `tests/tui/test_verbose.py`.

### Documentation

The user guide's TUI section gains `/verbose`, the three levels, and what each shows; `docs/architecture.md` gains
this milestone once it lands, alongside Milestone 03 (neither has its own subsection there yet, both still
sitting under "Milestones 3 onward (planned)").

## Acceptance criteria

- `/verbose` with no argument reports the current level; `/verbose <invalid>` is refused with `CommandError`
  naming `high`, `medium`, and `low`; `/verbose high|medium|low` sets the level, persists it, and says so.
- A fresh `dtc tui` with no `config/tui-preferences.yaml` starts at `high`; one written by an earlier session
  starts at that session's last level.
- A test with a fake `grpc_stub_factory` asserts the `WatchEventsRequest.include_output` the TUI opens with at
  each level, and that switching between low and not-low triggers exactly one new `WatchEvents` call, opened with
  `request.last_event_id` equal to the previous `_last_event_id`, while switching between `high` and `medium`
  triggers none.
- A test where the fake backlog can still explain the gap confirms a low↔high switch resumes with no `Reset` and no
  cleared pane; a second test where the fake backlog cannot (simulating more than 2000 filtered events since the
  last one the client received) confirms the server's `Reset` is handled exactly as any other reconnect's is: the
  pane reseeds and shows "(earlier output not shown)".
- A test with a fake `LiveRun.now` confirms medium's window: a `run_output` line applied within
  `MEDIUM_OUTPUT_WINDOW_SECONDS` of `run_started_at` is written; one applied after is not, and the pane shows the
  "(output hidden...)" marker exactly once per closed window.
- A test confirms low mode never writes a `run_output` line to the pane regardless of elapsed time, that no
  `GET /queue/{id}` is made on its behalf, that the run line shows no step counter in low, and that with a fake
  clock the Status widget, run line, and status line re-render once per `LOW_STATUS_REFRESH_SECONDS` between events
  but at once on `RunStarted`, `RunFinished`, and `CooldownStarted`.
- A test confirms `/verbose medium` typed mid-run opens a fresh window at that moment (a line 30 s later is
  written, one 90 s later is not), the next run's own window then starts at its `RunStarted`, and switching to
  `high` afterwards writes only lines that arrive after the switch.
- A test confirms the low-mode marker appears from a job starting (or being seeded) while low is already selected,
  and from switching into low while a job is already running — not from any line being skipped, since none ever
  arrives — and that the Messages notice about hidden output and the once-a-minute update is written on entering low.
- A test with the feed not connected confirms a switch across the low boundary persists and applies without saying
  "Reconnecting", and that a failed preferences write still applies the level and says it could not be saved.
- A test confirms `DrawThingsApp.show_status` still renders `quit_armed`'s prompt at once on a Ctrl-C press in low
  mode, even while the Status widget's own periodic refresh is throttled.
- `make check` passes.

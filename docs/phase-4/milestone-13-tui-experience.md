# Milestone 13: TUI experience

**Phase:** [Phase 4](README.md)
**Status:** done
**Depends on:** [Milestone 12](milestone-12-tui-enhancement.md) (done): the Queue list, Jobs list and Execution detail it improves.

## Completion (2026-10-18)

Landed and verified with `make check`. As built: the text viewer is `tui/text_overlay.py`; the desktop notification is `post_notification` in `tui/desktop.py`; the idle work status is `queue_summary` and `last_finished` in `tui/text/status.py`; the command history is `config/tui-command-history.txt` (`ProjectPaths.tui_command_history`).

## Goal

A review of the TUI found the points below that made it slower to learn, riskier to use, or easier to miss something in. This milestone fixes the ones the owner chose, and the slow launch that the owner noticed.

## Evidence

1. **Launch to a filled Jobs list took 1.6 s** on 53 job files (measured in a headless run on a copy of the data; the TUI itself mounted in 0.12 s and the History list filled at 0.2 s). The Jobs list validates every file with `decode_input=False`, which means "do not decode the inputs", but `H3Backend._reference_pairs` ignored it and decoded every pixel of every reference image (and fully decoded every reference video and audio with `ffmpeg`). Decoding made up about 1 s of the 1.6 s.
2. **`d` then `Enter` deleted.** The delete confirmation bound `Enter` to Delete with priority, while `Enter` opens a row everywhere else.
3. **Nothing announced the end of a job**, and an error from a key pressed in a list went only to the Activity log beside it.
4. **The logs could not be scrolled from the keyboard.** They take no focus, and `/help` or a dry-run plan fills them with far more than their 6 to 15 lines.
5. **The work status was blank when idle**, the status line did not say whether `fc serve` was reachable, and `?` could not be used from the command field, where focus starts.
6. **`Enter` meant three things** (a dump into the Activity log, a move of focus, a reveal), `a` queued an invalid job (the server refused it, far from the key) and queued a job twice on a double press, Tab could only take the first completion, and the command history was forgotten at quit.
7. **Colours were fixed** (`black`, `white`) where the rest of the TUI follows the theme.

## Scope

- **Launch.** `H3Backend` honours `decode_input=False`: it still reads every header, checks every stream with `ffprobe` and measures every duration, but does not decode pixels or samples. A run's own parse (`decode_input=True`) is unchanged, so a corrupt reference still fails when the job is submitted, as before. The Jobs list says `Reading job files...` in its subtitle until the first listing arrives. Same data: 1.6 s to 0.47 s.
- **Delete confirmation (owner decision: as recommended).** Cancel has the focus, `Enter` presses the focused button, and only `y` or a click deletes.
- **End of a job.** The terminal bell, a toast, and a macOS notification (`osascript`, with the title and message passed as arguments, never inside the script), once per job that ends, with its status and `completed/total runs`. An error or warning from a key pressed in a list is also a toast; one from the command field is not, since the person is looking at the Activity log.
- **Scrolling.** In the command field, `PageUp`/`PageDown` scroll the Activity log and `Shift+PageUp`/`Shift+PageDown` the generation output.
- **Work status.** Idle, it reads `idle`, `N queued, next: walk (Q0007)` and `last: Q0006 walk succeeded, 7/7 runs`; after a job, the queue summary takes the line the hold would use when there is none. The Queue list's subtitle counts `1 running, 3 queued, 5 finished`, and the Execution History list's counts `3 marked`.
- **Server connection.** The status line says `server connected` or `server offline (read-only)`.
- **`F1`** shows the shortcut help of the focused list or detail, or of the command field, and closes it.
- **Enter and `l` (owner decision: assign a key).** In the Queue list, `Enter` opens the details in a scrollable text viewer, and `l` writes them to the Activity log as `Enter` used to. In the Jobs list, `Enter` and a click write them to the Activity log (a later owner decision; see the changelog), and `l` does the same. `/describe` is unchanged.
- **Tab.** With several completions, Tab adds what they share, then steps through them (the status line lists them); with none it moves the focus on.
- **Command history.** Kept in `config/tui-command-history.txt` (per machine, ignored by git, the newest 200), and recalled with Up and Down in the next session.
- **`a` (owner decision: as recommended).** It refuses an invalid job and says why, and asks for a second `a` within 5 seconds for a job that is already waiting or running.
- **Wording.** `a` was documented as asking for confirmation, which it never did since the queue; fixed in the help, the shortcut help and the user guide.
- **Colours.** The passive panes use the theme's `$background`, and the command field's cursor `$foreground` on `$background`.

## Out of scope

- The Queue list and the Execution History list keep their 5 visible rows (owner decision, Milestone 01).
- Making the right column adapt to a terminal shorter than the 34 lines the tests cover, and the status line's priority of hints over the data directory path: found in the same review, not chosen.
- A setting to turn the bell and the notification off.
- Caching parsed job files between launches.

## Acceptance criteria

- `make check` passes.
- On a copy of the 53 job files, the Jobs list fills in under 0.5 s of a headless run.
- `Enter` in the delete confirmation cancels; `y` deletes.
- Each key above has a test, and the user guide, the help, the shortcut help and `ux-terms.md` agree.

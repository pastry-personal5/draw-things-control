# TUI UX terms

This document is the authoritative vocabulary for `dtc tui`. Use these terms
in the interface, plans, documentation, tests, and issue reports. Prefer the
person’s task and visible result over an implementation name.

The vocabulary follows Apple’s Human Interface Guidelines: make the hierarchy
clear, use familiar and concise language, preserve agency, give feedback that
matches its importance, and keep keyboard interaction consistent and
discoverable. See Apple’s [design principles](https://developer.apple.com/design/human-interface-guidelines/design-principles), [feedback guidance](https://developer.apple.com/design/human-interface-guidelines/feedback), and [keyboard guidance](https://developer.apple.com/design/human-interface-guidelines/keyboards).

## Principles for TUI language

- **Name the person’s goal, not the implementation.** Say **generation output**, not “child output”; say **Jobs list**, not a class or file name.
- **Use one name for one thing.** A name must not also refer to another component, state, or action.
- **Make hierarchy explicit.** A list selects an item; a detail view shows the selected item; a status display summarizes the current state. Do not describe all three as “the view.”
- **Keep feedback proportional.** Passive, ongoing information belongs in a status display or log. A modal dialog is reserved for a consequential choice that needs an immediate response.
- **Make the input target clear.** Say **focus** for where keyboard input goes and **selection** for the item an action would affect. They may be the same, but are not interchangeable.
- **Use familiar terms precisely.** Use `Tab` for focus navigation, `Escape` for cancelling or leaving the current interaction, and `Enter` for confirming or opening the selected item. A custom shortcut must state its scope and action.

## Screen structure

| Canonical term | Visible title or location | Purpose |
|---|---|---|
| **TUI** | `dtc tui` | The complete terminal user interface. |
| **main screen** | normal, non-modal screen | The primary workspace for monitoring and managing jobs. |
| **activity column** | left side of the main screen | The status and logs for current work. |
| **management column** | right side of the main screen | The lists and details used to choose and manage jobs and executions. |
| **command area** | below both columns | The command field and status line. |

## Main-screen components

| Canonical term | Current visible title | Purpose | Avoid |
|---|---|---|---|
| **work status** | `Status` | The multi-line, at-a-glance state of the current or most recently finished job; when idle, it can instead report a queue hold or a job running elsewhere. | Status pane, status line, widget |
| **generation output** | `draw-things-cli` | The run summary and output produced while the generation runs. It is a display, not an interactive shell. | draw-things-cli pane, child output, console |
| **run summary** | first line of generation output | A compact summary of the active run or cooldown. | run line, status |
| **output log** | remainder of generation output | The received generation output, including errors and progress reports. | child output |
| **activity log** | `Messages` | Timestamped command results and human-readable job events. | Messages pane, message log |
| **Jobs list** | `Job Definition` | The selectable list of available job files. | Job Definition pane, jobs widget |
| **Queue list** | `Queue` or `Queue (held)` | The selectable list of active, waiting, and recent queue entries. | Queue pane, queue widget |
| **Execution History list** | `Execution History` | The selectable list of recorded executions, optionally narrowed by a filter. | history pane, execution history widget |
| **Execution detail** | `Execution` | The details and successful runs of the execution selected in Execution History. | Execution pane, execution widget |
| **command field** | editable text after `> ` | The text input for slash commands. | command line, prompt |
| **command prompt** | `> ` | The non-editable marker before the command field. | command line |
| **status line** | last line of the main screen | A compact persistent summary below the command field. This is not work status. | Status, Status pane |

The current visible titles may change to these canonical names as the TUI is
improved. Until then, documentation may write both names once, for example,
“the Jobs list (`Job Definition`).” After that first reference, use only the
canonical term.

## Transient components

| Canonical term | Visible label | Purpose |
|---|---|---|
| **delete confirmation** | deletion question with buttons | A modal confirmation before irreversible execution-history deletion. It blocks the main screen until a choice is made. |
| **Delete** | `Delete` button | Deletes the execution currently named in the confirmation. |
| **Skip** | `Skip` button | Keeps the current execution and continues a multi-execution deletion. |
| **Delete all** | `Delete all` button | Deletes the current and all remaining selected executions. |
| **Cancel** | `Cancel` button | Dismisses the confirmation without deleting the current execution. |

## Interaction vocabulary

| Term | Meaning |
|---|---|
| **focus** | The component receiving keyboard input. Focus must always be visually apparent. |
| **focused component** | The component that receives the next keystroke. Use this term when documenting a shortcut’s scope. |
| **selection** | The item currently chosen within a list or detail. A selection identifies the target of a list action; it does not by itself perform that action. |
| **selected row** | The selected item in a table-based list. |
| **open** | Show information about the selected item, or reveal its result when that is the item’s primary action. Use instead of the ambiguous “describe” in UX prose. |
| **command** | A slash command entered in the command field, such as `/queue hold`. It is not a keyboard shortcut. |
| **keyboard shortcut** | A key or key combination that performs a named action. State both its action and scope, for example, “`c` cancels the selected entry in the Queue list.” |
| **global shortcut** | A shortcut the TUI handles before a focused component’s shortcut. Reserve it for a small number of app-wide, predictable actions. |
| **modal** | A temporary interaction that blocks the main screen until it is completed or cancelled. The delete confirmation is currently the only modal. |
| **feedback** | Information that confirms an action, reports progress, warns of a consequence, or explains an error. State what changed, its outcome, and a useful next action when one exists. |

## Writing and shortcut rules

- Use sentence case for visible labels and action names: **Delete all**, not `DELETE ALL` or `Delete All`.
- Start actions with a verb: **Cancel**, **Park**, **Reveal**, **Clear**. Use nouns for places and information: **Queue list**, **work status**, **generation output**.
- A shortcut description states an action, target, and scope when the scope is not global. Example: “In the Queue list, `p` parks the selected entry.”
- Do not assign a bare letter globally when it would be typed in the command field or conflicts with a focused component. Prefer standard navigation and cancellation behavior before adding custom keys.
- `Escape` cancels a modal or leaves the focused list or detail for the command field; it must not perform an irreversible action.
- `Enter` opens or confirms the focused item. If an action is destructive, `Enter` confirms it only inside an explicit delete confirmation.
- Use **work status** for current state and **status line** only for the footer summary. This distinction is mandatory in requirements, tests, and bug reports.

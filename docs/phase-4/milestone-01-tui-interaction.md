# Milestone 01: TUI interaction and contextual shortcut help

**Phase:** [Phase 4](README.md)
**Status:** done
**Depends on:** Phase 3's TUI, including its focusable lists, execution detail, status line, and Textual event feed.

## Goal

Make keyboard interaction clear and predictable by establishing canonical TUI terms, aligning visible names with them, and showing context-sensitive shortcut help where a person acts.

## Scope

In scope:

- [TUI UX terms](../ux-terms.md) as the authoritative vocabulary for visible components, focus, selection, feedback, shortcuts, and the shortcut help overlay.
- Renaming visible TUI titles, Textual component names and IDs, user-facing help, and tests to use the canonical terms: work status, generation output, activity log, Jobs list, Queue list, Execution History list, Execution detail, command field, and status line.
- A **shortcut help overlay** for a focused Jobs list, Queue list, Execution History list, or Execution detail.
- A status-line hint, `Press ? for shortcut help`, whenever one of those components has focus, refreshed immediately when focus changes.

Out of scope:

- Shortcut help for passive displays, which do not receive focus: work status, generation output, activity log, run summary, output log, and status line.
- Shortcut help from the command field: `?` remains ordinary command-field text.
- Shortcut help during delete confirmation, which remains the sole destructive-action modal.
- Changing queue state rules, server APIs, generation behavior, or slash-command semantics.

## Planned changes

### Contextual help

- Add a centered modal shortcut help overlay. It names the focused component and lists the keyboard interactions available there.
- Bind `?` only on the four focusable main-screen components. It opens their shortcut help overlay; `?` or Escape closes the overlay and restores focus to its source component.
- Include standard navigation and focus movement, the component's explicit actions, Escape, Ctrl-C, and `?` in each list. Help reflects the focused component's bindings, not the selected row's server-side state.
- Keep each component's shortcut rows beside its bindings, so a binding change must update its displayed help in the same module.

### Status feedback

- Make status-line rendering aware of the current focused component. When focus is one of the four help-capable components, append `Press ? for shortcut help`; omit it for the command field and while the Ctrl-C quit prompt takes precedence.
- Refresh this line on focus gain as well as from the existing job-status tick, so low verbose mode never delays the hint.

### Documentation

- Document the canonical terms and shortcut help in the user guide, `/help`, this milestone, and the Phase 4 changelog.

## Acceptance criteria

- Each focusable list and Execution detail opens shortcut help with `?`, shows its canonical title and appropriate navigation and action shortcuts, and returns to the same focused component with `?` or Escape.
- The status line immediately gains `Press ? for shortcut help` when any help-capable component receives focus and removes it when focus returns to the command field.
- `?` still inserts text in the command field, and the delete confirmation remains unaffected by it.
- TUI title, documentation, command help, Textual IDs/classes, and tests use the canonical vocabulary.
- `make check` passes.

"""An execution's prompts, as /get prompts, positive, and negative show them."""

from __future__ import annotations

from typing import NamedTuple

from rich.text import Text

from draw_things_control.state.executions import ExecutionRow, RunRow


def find_run(execution: ExecutionRow, run_number: int | None) -> RunRow | str:
    """The run numbered ``run_number``, or, without one, the first run with a saved command; else why there is none."""
    runs = execution.runs
    if run_number is not None:
        match = next((run for run in runs if run.number == run_number), None)
        return match if match is not None else f"Execution {execution.label} has no run {run_number}"
    first = next((run for run in runs if run.command), None)
    return first if first is not None else f"Execution {execution.label} has no run with a saved command"


def prompt_block(text: Text, label: str, style: str, value: str | None) -> str:
    """Append a blank line, ``label:`` on its own line, and the prompt from the next line on (``(none)`` without one);
    returns the prompt as shown, empty when there is none."""
    prompt = value.strip() if value else ""
    text.append(f"\n{label}:", style=style)
    text.append("\n")
    text.append(f"{prompt or '(none)'}\n", style="" if prompt else "dim")
    return prompt


class _Prompts(NamedTuple):
    """The prompts of a pair that several runs shared."""

    positive: str
    negative: str | None


def prompts_text(execution: ExecutionRow, which: str, run_number: int | None) -> tuple[Text, tuple[str, str] | None]:
    """``positive``, ``negative``, or both (``prompts``): each prompt pair once with the runs that used it, or one run's.

    Each label stands on its own line with a blank line before it, and its prompt starts on the next line, so a prompt
    reads and selects as a block. Also returns what to copy to the clipboard and what to call it: the prompts alone for
    ``positive`` or ``negative``, the labelled prompts for ``prompts``; None when there is no prompt.
    """
    text = Text(f"Execution {execution.label}: {execution.job_name}", style="bold")
    groups: list[tuple[str, RunRow | _Prompts]]
    if run_number is not None:
        run = find_run(execution, run_number)
        if isinstance(run, str):
            return Text(run, style="red"), None
        groups = [(f"Run {run.number} (pair {run.pair})", run)]
    else:
        # Each pair once, in the order it first ran, with every run that used it.
        pairs: dict[tuple[str, str, str | None], list[int]] = {}
        for run in execution.runs:
            pairs.setdefault((run.pair, run.positive, run.negative), []).append(int(run.number))
        if not pairs:
            text.append("\n  no runs", style="dim")
            return text, None
        groups = [(f"Pair {pair} (run{'s' if len(numbers) != 1 else ''} {', '.join(str(number) for number in numbers)})", _Prompts(positive, negative)) for (pair, positive, negative), numbers in pairs.items()]
    copied: list[str] = []
    for heading, run in groups:
        text.append(f"\n{heading}\n", style="bold")
        for label, style in (("positive", "green"), ("negative", "red")):
            if which not in (label, "prompts"):
                continue
            prompt = prompt_block(text, label, style, getattr(run, label))
            if prompt:
                copied.append(f"{label}:\n{prompt}" if which == "prompts" else prompt)
    if not copied:
        return text, None
    return text, ("\n\n".join(copied), "prompts" if which == "prompts" else f"{which} prompt{'s' if len(copied) > 1 else ''}")

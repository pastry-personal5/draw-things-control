"""Job IDs (``J0001``) and execution IDs (``E0012``): how they are written and how a person may type them."""

from __future__ import annotations

import re

JOB_LETTER = "J"
EXECUTION_LETTER = "E"
# At least this many digits are shown; a larger number simply shows more (J10000).
DIGITS = 4
# The largest number SQLite holds; a larger one cannot name anything.
MAX_NUMBER = 2**63 - 1
# Its digits; a longer run of digits is refused before int(), which raises on thousands of them.
MAX_DIGITS = len(str(MAX_NUMBER))
_TYPED = re.compile(r"([A-Za-z])([0-9]+)")


def format_id(letter: str, number: int) -> str:
    """``letter`` in upper case and the number with at least four digits: ``E0012``."""
    return f"{letter.upper()}{number:0{DIGITS}d}"


def execution_id_text(number: int) -> str:
    return format_id(EXECUTION_LETTER, number)


def job_id_text(number: int) -> str:
    return format_id(JOB_LETTER, number)


def parse_typed_id(text: str, letter: str) -> int | None:
    """The number of an ID typed with ``letter`` in any case and any leading zeros (``e12`` is E0012); None otherwise."""
    match = _TYPED.fullmatch(text.strip()) if text.isascii() else None
    if match is None or match.group(1).upper() != letter.upper():
        return None
    digits = match.group(2).lstrip("0") or "0"
    if len(digits) > MAX_DIGITS:
        return None
    number = int(digits)
    return number if 0 < number <= MAX_NUMBER else None

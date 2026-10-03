"""Examples and links in the current entry-point documentation must match the CLI."""

from __future__ import annotations

import re
import shlex
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from typer.main import get_command

from draw_things_control.cli.app import app

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (ROOT / "README.md", ROOT / "docs/user-guide.md", ROOT / "docs/user-guide-for-http-based-mcp.md")
LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")


def bash_lines(source: str) -> list[str]:
    lines: list[str] = []
    inside = False
    pending = ""
    for line in source.splitlines():
        if line.startswith("```bash"):
            inside = True
            continue
        if line.startswith("```") and inside:
            inside = False
            continue
        if not inside or line.lstrip().startswith("#"):
            continue
        pending += line.rstrip().removesuffix("\\").strip() + " "
        if not line.rstrip().endswith("\\"):
            lines.append(pending.strip())
            pending = ""
    return lines


def command_options(command: Any) -> set[str]:
    return {"--help", *(name for option in command.params for name in (*getattr(option, "opts", ()), *getattr(option, "secondary_opts", ())))}


class DocumentationTests(unittest.TestCase):
    def test_bash_dtc_commands_exist_and_use_known_options(self) -> None:
        root = get_command(app)
        assert hasattr(root, "commands")
        for document in DOCUMENTS:
            for line in bash_lines(document.read_text(encoding="utf-8")):
                try:
                    words = shlex.split(line, comments=True)
                except ValueError:
                    continue
                if "dtc" not in words:
                    continue
                words = words[words.index("dtc") + 1 :]
                if not words:
                    continue
                options = command_options(root)
                current: Any = root
                for word in words:
                    if word in {"|", "&&", ";"}:
                        break
                    if word.startswith("--"):
                        option = word.split("=", 1)[0]
                        with self.subTest(document=document.name, line=line):
                            self.assertIn(option, options)
                    elif hasattr(current, "commands") and word in current.commands:
                        current = current.commands[word]
                        options |= command_options(current)
                    elif current is root and not word.startswith("-"):
                        with self.subTest(document=document.name, line=line):
                            self.fail(f"Unknown dtc command: {word}")

    def test_relative_links_resolve(self) -> None:
        for document in DOCUMENTS:
            for target in LINK.findall(document.read_text(encoding="utf-8")):
                if "://" in target or target.startswith(("mailto:", "#")):
                    continue
                path = unquote(target.split("#", 1)[0])
                with self.subTest(document=document.name, target=target):
                    self.assertTrue((document.parent / path).exists())

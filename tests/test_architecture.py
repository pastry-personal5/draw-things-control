"""The layers of the package, as import rules: cli, tui, server -> services -> state -> jobs -> core.

Front ends never import each other (``dtc tui`` starts the TUI app and ``dtc serve`` starts the HTTP API and gRPC
service, the two exceptions), ``mcp_server`` reaches the rest only over HTTP, and no layer below the front ends
imports a terminal, web, or gRPC framework.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE = "draw_things_control"
SOURCE = Path(__file__).resolve().parents[1] / "src" / PACKAGE

# What each subpackage may import from this package, besides itself.
ALLOWED = {
    "core": set(),
    "jobs": {"core"},
    "state": {"core", "jobs"},
    "services": {"core", "jobs", "state"},
    "cli": {"core", "jobs", "state", "services"},
    "tui": {"core", "jobs", "state", "services"},
    "server": {"core", "jobs", "state", "services"},
    "mcp_server": set(),
}
# The two imports between front ends: `dtc tui` starts the TUI app, and `dtc serve` starts the HTTP API and gRPC service.
FRONT_END_EXCEPTIONS = {("cli", f"{PACKAGE}.tui.app"), ("cli", f"{PACKAGE}.server.serve")}
# The frameworks a front end owns; the layers below never import them.
FRAMEWORKS = {"typer", "textual", "fastapi", "uvicorn", "starlette", "mcp", "httpx", "rich", "grpc"}
FRAMEWORK_LAYERS = {"core", "jobs", "state", "services"}
# Rich is the TUI's text type, which the TUI's own modules and nothing below them use.


def imports_of(source: str) -> set[str]:
    """Every module a source imports, absolute (this package uses no relative imports)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def violations(files: dict[str, str]) -> list[str]:
    """The imports in ``files`` (dotted module name to source) that break the rules."""
    problems = []
    for module, source in files.items():
        parts = module.split(".")
        if len(parts) < 3 or parts[0] != PACKAGE:
            continue
        layer = parts[1]
        for imported in imports_of(source):
            root = imported.split(".")[0]
            if root == PACKAGE:
                target = imported.split(".")[1] if "." in imported else ""
                if target and target != layer and target not in ALLOWED[layer] and (layer, imported) not in FRONT_END_EXCEPTIONS:
                    problems.append(f"{module} imports {imported}: {layer} may not import {target}")
            elif root in FRAMEWORKS and layer in FRAMEWORK_LAYERS:
                problems.append(f"{module} imports {imported}: {layer} may not import a framework")
    return problems


# gRPC stubs make proto generates from server/proto/monitor.proto: not committed, not hand-written, not held to
# this project's import rules or size limits (see the pyproject.toml ruff and pyright exclusions for the same). One
# copy per front end with a gRPC client (Milestone 02 design decision: front ends never import each other, so cli/
# and tui/ each need their own copy of the client stubs server/generated holds for server/ itself).
GENERATED_DIR_NAME = "generated"


def is_generated(path: Path) -> bool:
    return any(parent.name == GENERATED_DIR_NAME for parent in path.parents)


def package_files() -> dict[str, str]:
    files = {}
    for path in SOURCE.rglob("*.py"):
        if is_generated(path):
            continue
        module = ".".join((PACKAGE, *path.relative_to(SOURCE).with_suffix("").parts))
        files[module] = path.read_text(encoding="utf-8")
    return files


class ArchitectureTests(unittest.TestCase):
    def test_the_package_follows_the_layers(self) -> None:
        self.assertEqual(violations(package_files()), [])

    def test_every_subpackage_has_a_rule(self) -> None:
        layers = {path.parent.name for path in SOURCE.glob("*/__init__.py")}
        self.assertLessEqual(layers, set(ALLOWED))


MAX_MODULE_LINES = 800
MAX_CLASS_LINES = 250
MAX_FUNCTION_LINES = 40
# Declarative option lists, not logic: the options of the `generate` command.
FUNCTION_EXEMPT = {("cli/app.py", "generate")}


class SizeTests(unittest.TestCase):
    def test_modules_classes_and_functions_stay_small(self) -> None:
        problems = []
        for path in sorted(SOURCE.rglob("*.py")):
            if is_generated(path):
                continue
            name = path.relative_to(SOURCE).as_posix()
            source = path.read_text(encoding="utf-8")
            if len(source.splitlines()) > MAX_MODULE_LINES:
                problems.append(f"{name}: over {MAX_MODULE_LINES} lines")
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.ClassDef) and (node.end_lineno or 0) - node.lineno + 1 > MAX_CLASS_LINES:
                    problems.append(f"{name}: class {node.name} is over {MAX_CLASS_LINES} lines")
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (node.end_lineno or 0) - node.lineno + 1 > MAX_FUNCTION_LINES and (name, node.name) not in FUNCTION_EXEMPT:
                    problems.append(f"{name}: {node.name} is over {MAX_FUNCTION_LINES} lines")
        self.assertEqual(problems, [])


class RuleTests(unittest.TestCase):
    """The rules refuse what they should."""

    def bad(self, module: str, source: str) -> list[str]:
        return violations({f"{PACKAGE}.{module}": source})

    def test_a_front_end_may_not_import_another(self) -> None:
        self.assertEqual(len(self.bad("tui.app", f"from {PACKAGE}.cli.app import main")), 1)
        self.assertEqual(len(self.bad("server.app", f"import {PACKAGE}.tui.screens")), 1)
        self.assertEqual(len(self.bad("cli.app", f"from {PACKAGE}.tui.screens import MainScreen")), 1)

    def test_dtc_tui_may_start_the_tui_app(self) -> None:
        self.assertEqual(self.bad("cli.app", f"from {PACKAGE}.tui.app import DrawThingsApp"), [])

    def test_jobs_may_not_import_state_or_services(self) -> None:
        self.assertEqual(len(self.bad("jobs.executor", f"from {PACKAGE}.state.store import Store")), 1)
        self.assertEqual(len(self.bad("jobs.executor", f"from {PACKAGE}.services.toolkit import Toolkit")), 1)

    def test_core_may_import_nothing_else_of_the_package(self) -> None:
        self.assertEqual(len(self.bad("core.paths", f"from {PACKAGE}.jobs.events import JobEvent")), 1)
        self.assertEqual(self.bad("core.paths", f"from {PACKAGE}.core.errors import DtcError"), [])

    def test_state_may_not_import_services(self) -> None:
        self.assertEqual(len(self.bad("state.store", f"from {PACKAGE}.services.history import HistoryReader")), 1)

    def test_the_mcp_server_reaches_the_rest_only_over_http(self) -> None:
        self.assertEqual(len(self.bad("mcp_server.tools", f"from {PACKAGE}.server.app import app")), 1)
        self.assertEqual(len(self.bad("mcp_server.tools", f"from {PACKAGE}.core.paths import ProjectPaths")), 1)

    def test_the_lower_layers_import_no_framework(self) -> None:
        for framework in ("typer", "textual.app", "fastapi", "rich.text"):
            self.assertEqual(len(self.bad("services.history", f"import {framework}")), 1, framework)
        self.assertEqual(self.bad("tui.app", "from textual.app import App"), [])

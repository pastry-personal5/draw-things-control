"""Read Draw Things configurations from YAML or JSON files."""

from __future__ import annotations

import datetime
import json
import math
import re
from pathlib import Path
from typing import Any

import yaml

YAML_SUFFIXES = {".yaml", ".yml"}

MERGE_TAG = "tag:yaml.org,2002:merge"

# YAML 1.1 reads a float only with a decimal point and a signed exponent, so 1e-3 would stay a string.
EXPONENT_FLOAT = re.compile(r"^[-+]?[0-9][0-9_]*(?:\.[0-9_]*)?[eE][-+]?[0-9]+$")


class _StrictLoader(yaml.SafeLoader):
    """A SafeLoader that rejects what it would otherwise accept silently: duplicate and non-string keys, octal and base-60 numbers."""

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        # A scalar that matches its tag but cannot be built (!!float abc, !!timestamp 2026-99-99) raises a plain ValueError without a line.
        try:
            return super().construct_object(node, deep=deep)
        except (ValueError, TypeError, OverflowError) as error:
            raise yaml.constructor.ConstructorError(None, None, f"value cannot be read ({error})", node.start_mark) from error

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        if isinstance(node, yaml.MappingNode):
            self._check_keys(node)
        return super().construct_mapping(node, deep=deep)

    def _check_keys(self, node: yaml.MappingNode) -> None:
        seen: set[str] = set()
        for key_node, value_node in node.value:
            if key_node.tag == MERGE_TAG:
                # Merged keys may be overridden here, but the merged mappings need the same checks: an inline one is never built on its own.
                sources = value_node.value if isinstance(value_node, yaml.SequenceNode) else [value_node]
                for source in sources:
                    if isinstance(source, yaml.MappingNode):
                        self._check_keys(source)
                continue
            key = self.construct_object(key_node)
            if not isinstance(key, str):
                raise yaml.constructor.ConstructorError(None, None, f"key {key!r} is not a string (quote it)", key_node.start_mark)
            if key in seen:
                raise yaml.constructor.ConstructorError(None, None, f"key '{key}' appears twice", key_node.start_mark)
            seen.add(key)

    def construct_yaml_int(self, node: yaml.ScalarNode) -> int:
        digits = str(node.value).replace("_", "").lstrip("+-")
        if len(digits) > 1 and digits[0] == "0" and digits.isdigit():
            raise yaml.constructor.ConstructorError(None, None, f"{node.value} has a leading zero, which YAML 1.1 reads as an octal number; drop the zero, or quote it", node.start_mark)
        self._reject_base_60(node)
        return super().construct_yaml_int(node)

    def construct_yaml_float(self, node: yaml.ScalarNode) -> float:
        self._reject_base_60(node)
        return super().construct_yaml_float(node)

    @staticmethod
    def _reject_base_60(node: yaml.ScalarNode) -> None:
        if ":" in str(node.value):
            raise yaml.constructor.ConstructorError(None, None, f"{node.value} is a base-60 number in YAML 1.1; quote it, or write the number in decimal", node.start_mark)


_StrictLoader.add_constructor("tag:yaml.org,2002:int", _StrictLoader.construct_yaml_int)
_StrictLoader.add_constructor("tag:yaml.org,2002:float", _StrictLoader.construct_yaml_float)
_StrictLoader.add_implicit_resolver("tag:yaml.org,2002:float", EXPONENT_FLOAT, list("-+0123456789"))


def is_yaml_file(path: Path | str) -> bool:
    """Whether the file name has a YAML extension (.yaml or .yml, any letter case)."""
    return Path(path).suffix.lower() in YAML_SUFFIXES


def load_config(path: Path) -> dict[str, Any]:
    """Load a configuration object from a YAML file (by extension) or a JSON file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Cannot read configuration file {path}: {error.strerror}") from error
    if is_yaml_file(path):
        return _parse_yaml(text, path)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Configuration is not valid JSON: {path} ({error.msg} on line {error.lineno})") from error
    except RecursionError as error:
        raise ValueError(f"Configuration is nested too deeply: {path}") from error
    if not isinstance(data, dict):
        raise ValueError("Configuration must contain a JSON object")
    return data


def _parse_yaml(text: str, path: Path) -> dict[str, Any]:
    """Read one YAML mapping whose values JSON can hold."""
    try:
        data = yaml.load(text, Loader=_StrictLoader)
    except yaml.MarkedYAMLError as error:
        where = f" on line {error.problem_mark.line + 1}" if error.problem_mark is not None else ""
        problem = ", ".join(part for part in (error.context, error.problem) if part)
        raise ValueError(f"Configuration is not valid YAML: {path} ({problem}{where})") from error
    except (yaml.YAMLError, ValueError, TypeError) as error:
        raise ValueError(f"Configuration is not valid YAML: {path} ({error})") from error
    except RecursionError as error:
        raise ValueError(f"Configuration is nested too deeply: {path}") from error
    if data is None:
        raise ValueError(f"Configuration is empty: {path}")
    if not isinstance(data, dict):
        raise ValueError(f"Configuration must contain one YAML mapping: {path}")
    try:
        _check_json_values(data, "", path, set())
    except RecursionError as error:
        raise ValueError(f"Configuration is nested too deeply: {path}") from error
    return data


def _check_json_values(value: Any, key_path: str, path: Path, parents: set[int]) -> None:
    """Reject values that JSON cannot hold, naming the key path, for example loras[0].version."""
    if isinstance(value, (dict, list)):
        if id(value) in parents:
            raise ValueError(f"Configuration {path}: {key_path or 'the document'} is an alias that refers to itself")
        parents.add(id(value))
        if isinstance(value, dict):
            children = ((f"{key_path}.{key}" if key_path else key, item) for key, item in value.items())
        else:
            children = ((f"{key_path}[{index}]", item) for index, item in enumerate(value))
        for child_path, item in children:
            _check_json_values(item, child_path, path, parents)
        parents.discard(id(value))
        return
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Configuration {path}: {key_path} is {value}, which JSON cannot hold")
        return
    if isinstance(value, datetime.date):
        raise ValueError(f"Configuration {path}: {key_path} is a date or time, which JSON cannot hold; quote it if a string is meant")
    if isinstance(value, bytes):
        problem = "binary data"
    elif isinstance(value, set):
        problem = "a set"
    else:
        problem = f"a {type(value).__name__}"
    raise ValueError(f"Configuration {path}: {key_path} is {problem}, which JSON cannot hold")

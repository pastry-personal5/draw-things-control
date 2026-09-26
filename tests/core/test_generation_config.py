"""Tests for reading configurations, finding base configurations, and applying overrides."""

import tempfile
import unittest
from pathlib import Path

from draw_things_control.core.configuration import load_config
from draw_things_control.core.generation_config import DT_CONFIG_DIRECTORY, build_config_json, find_config_file, load_base_config


class GenerationConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_config_directory_is_the_repository_folder(self) -> None:
        self.assertEqual(DT_CONFIG_DIRECTORY, Path(__file__).resolve().parents[2] / "dt-config")

    def test_bare_yaml_name_is_found_in_the_directory(self) -> None:
        for name in ("base.yaml", "base.yml", "BASE.YML"):
            with self.subTest(name):
                self.write(name, "model: m.ckpt\n")
                self.assertEqual(find_config_file(name, self.directory), self.directory / name)
                self.assertEqual(load_base_config(name, self.directory), {"model": "m.ckpt"})

    def test_paths_are_rejected(self) -> None:
        for name in ("../pyproject.toml", "dt-config/x.yaml", "/etc/hosts", ".."):
            with self.subTest(name), self.assertRaisesRegex(ValueError, "not a path"):
                find_config_file(name)

    def test_other_extensions_are_rejected(self) -> None:
        self.write("base.txt", "model: m.ckpt\n")
        for name in ("base.txt", "base"):
            with self.subTest(name), self.assertRaisesRegex(ValueError, r"must name a YAML file \(\.yaml or \.yml\)"):
                find_config_file(name, self.directory)

    def test_json_names_the_yaml_file_with_the_same_stem(self) -> None:
        self.write("wan.json", "{}")
        self.write("wan.yaml", "{}\n")
        with self.assertRaisesRegex(ValueError, r"^'config_file' wan\.json is JSON; name wan\.yaml instead$"):
            find_config_file("wan.json", self.directory)
        self.write("other.yml", "{}\n")
        with self.assertRaisesRegex(ValueError, r"'config_file' other\.JSON is JSON; name other\.yml instead"):
            find_config_file("other.JSON", self.directory)

    def test_json_without_a_yaml_file_says_to_write_one(self) -> None:
        self.write("wan.json", "{}")
        with self.assertRaisesRegex(ValueError, r"wan\.json is JSON, but a job needs a YAML configuration; write wan\.yaml in .*the JSON file is left as it is"):
            find_config_file("wan.json", self.directory)
        self.assertEqual((self.directory / "wan.json").read_text(encoding="utf-8"), "{}")
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ["wan.json"])

    def test_missing_file_lists_available_yaml_files(self) -> None:
        for name in ("one.yaml", "two.YML", "three.json", "notes.txt"):
            self.write(name, "{}")
        with self.assertRaisesRegex(ValueError, r"four\.yaml is not in .* \(available: one\.yaml, two\.YML\)$"):
            find_config_file("four.yaml", self.directory)

    def test_yaml_keeps_values_and_key_order(self) -> None:
        path = self.write("base.yaml", "# Wan 2.2\nmodel: m.ckpt\nsteps: 40\nguidanceScale: 5\nshift: 3.99\nhiresFix: false\nupscaler: ''\nloras: []\ncontrols: [{file: c.ckpt, weight: 1.0}]\nfaceRestoration: null\ncolorCalibration: none\nstring: 'no'\nflag: yes\n")
        config = load_config(path)
        self.assertEqual(config, {"model": "m.ckpt", "steps": 40, "guidanceScale": 5, "shift": 3.99, "hiresFix": False, "upscaler": "", "loras": [], "controls": [{"file": "c.ckpt", "weight": 1.0}], "faceRestoration": None, "colorCalibration": "none", "string": "no", "flag": True})
        self.assertEqual(list(config), ["model", "steps", "guidanceScale", "shift", "hiresFix", "upscaler", "loras", "controls", "faceRestoration", "colorCalibration", "string", "flag"])

    def test_merge_keys_may_be_overridden(self) -> None:
        path = self.write("base.yaml", "common: &common {steps: 30, shift: 1.0}\nrun: {<<: *common, steps: 40}\n")
        self.assertEqual(load_config(path)["run"], {"steps": 40, "shift": 1.0})

    def test_rejected_yaml_names_the_file_and_the_line_or_key_path(self) -> None:
        cases = {
            "invalid": ("model: [m.ckpt\n", r"not valid YAML: .*invalid\.yaml \(.*on line 2\)"),
            "empty": ("# nothing\n", r"Configuration is empty: .*empty\.yaml"),
            "list": ("- model: m.ckpt\n", r"must contain one YAML mapping: .*list\.yaml"),
            "scalar": ("m.ckpt\n", r"must contain one YAML mapping: .*scalar\.yaml"),
            "documents": ("model: a\n---\nmodel: b\n", r"not valid YAML: .*documents\.yaml \(.*another document on line 2\)"),
            "duplicate": ("model: a\nsteps: 4\nmodel: b\n", r"not valid YAML: .*duplicate\.yaml \(key 'model' appears twice on line 3\)"),
            "nested-duplicate": ("loras:\n  - file: a\n    file: b\n", r"key 'file' appears twice on line 3"),
            "integer-key": ("1: x\n", r"not valid YAML: .*integer-key\.yaml \(key 1 is not a string \(quote it\) on line 1\)"),
            "boolean-key": ("model: m\non: 1\n", r"key True is not a string \(quote it\) on line 2"),
            "date": ("loras:\n  - version: 2026-09-25\n", r"date\.yaml: loras\[0\]\.version is a date or time, which JSON cannot hold; quote it"),
            "nan": ("shift: .nan\n", r"nan\.yaml: shift is nan, which JSON cannot hold"),
            "infinity": ("controls: [{weight: -.inf}]\n", r"controls\[0\]\.weight is -inf, which JSON cannot hold"),
            "binary": ("mask: !!binary aGVsbG8=\n", r"mask is binary data"),
            "set": ("tags: !!set {a: null}\n", r"tags is a set"),
            "recursive": ("loras: &self [*self]\n", r"loras\[0\] is an alias that refers to itself"),
            "octal": ("model: m\nsteps: 010\n", r"010 has a leading zero, which YAML 1.1 reads as an octal number.* on line 2"),
            "signed-octal": ("seed: -007\n", r"-007 has a leading zero"),
            "base-60": ("steps: 1:30\n", r"1:30 is a base-60 number in YAML 1\.1.* on line 1"),
            "base-60-float": ("shift: 1:30.5\n", r"1:30\.5 is a base-60 number"),
            "inline-merge-duplicate": ("model: m\n<<: {steps: 1, steps: 2}\n", r"key 'steps' appears twice on line 2"),
            "inline-merge-integer-key": ("model: m\n<<: {1: q}\n", r"key 1 is not a string \(quote it\) on line 2"),
            "merge-list-duplicate": ("model: m\n<<: [{a: 1}, {b: 2, b: 3}]\n", r"key 'b' appears twice on line 2"),
            "unreadable-float": ("shift: !!float abc\n", r"unreadable-float\.yaml \(value cannot be read \(could not convert string to float: 'abc'\) on line 1\)"),
            "unreadable-date": ("model: m\nday: !!timestamp 2026-99-99\n", r"value cannot be read \(month must be in 1\.\.12\) on line 2"),
            "deep": ("loras: " + "[" * 5000 + "]" * 5000 + "\n", r"Configuration is nested too deeply: .*deep\.yaml"),
        }
        for name, (text, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, message):
                load_config(self.write(f"{name}.yaml", text))

    def test_numbers_are_read_as_json_would_read_them(self) -> None:
        path = self.write("base.yaml", "strength: 1e-3\nshift: 5e0\nsteps: 4E+1\nweight: -2.5e-1\ncount: 1_000\nmask: 0x1F\nseed: 0\nversion: '010'\ntime: '1:30'\nname: 1e5.5\n")
        self.assertEqual(load_config(path), {"strength": 0.001, "shift": 5.0, "steps": 40.0, "weight": -0.25, "count": 1000, "mask": 31, "seed": 0, "version": "010", "time": "1:30", "name": "1e5.5"})

    def test_repeated_non_recursive_aliases_are_accepted(self) -> None:
        path = self.write("base.yaml", "lora: &lora {file: l.ckpt, weight: 0.5}\nloras: [*lora, *lora]\n")
        self.assertEqual(load_config(path)["loras"], [{"file": "l.ckpt", "weight": 0.5}] * 2)

    def test_the_example_yaml_configuration_equals_its_json_file(self) -> None:
        # Reads dt-config/ only. The untracked -default files are the owner's to change, so only the tracked example is compared.
        name = "image-to-video-wan-2-2.example"
        json_config, yaml_config = load_config(DT_CONFIG_DIRECTORY / f"{name}.json"), load_config(DT_CONFIG_DIRECTORY / f"{name}.yaml")
        self.assertEqual(yaml_config, json_config)
        self.assertEqual(list(yaml_config), list(json_config))

    def test_named_config_keys_override_the_base(self) -> None:
        base = {"refinerModel": "base.ckpt", "refinerStart": 0.2, "shift": 1.0, "steps": 30}
        config = build_config_json(base, {"refiner_model": "job.ckpt", "refiner_start": 0.1, "shift": 3.99, "steps": 40})
        self.assertEqual(config, {"refinerModel": "job.ckpt", "refinerStart": 0.1, "shift": 3.99, "steps": 30})
        self.assertEqual(base["refinerModel"], "base.ckpt")

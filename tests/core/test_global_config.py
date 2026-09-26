"""Tests for loading the global configuration."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from draw_things_control.core.global_config import DEFAULT_COOLDOWN, CooldownPolicy, CooldownWait, load_global_config, parse_cooldown


class GlobalConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        (self.root / "input").mkdir()
        self.path = self.root / "global-config.yaml"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def test_valid_config_expands_home(self) -> None:
        self.write(f"version: 1\ninput_directory: {self.root / 'input'}\noutput_directory: ~/draw-things-output\n")
        config = load_global_config(self.path)
        self.assertEqual(config.input_directory, self.root / "input")
        self.assertEqual(config.output_directory, Path.home() / "draw-things-output")

    def test_invalid_configs_name_the_field(self) -> None:
        cases = {
            "relative": ("version: 1\ninput_directory: input\noutput_directory: /tmp/out\n", "input_directory"),
            "missing key": (f"version: 1\ninput_directory: {self.root / 'input'}\n", "output_directory"),
            "unknown key": (f"version: 1\ninput_directory: {self.root / 'input'}\noutput_directory: /tmp/out\nextra: 1\n", "extra"),
            "missing input directory": (f"version: 1\ninput_directory: {self.root / 'nope'}\noutput_directory: /tmp/out\n", "input_directory"),
            "wrong version": (f"version: 2\ninput_directory: {self.root / 'input'}\noutput_directory: /tmp/out\n", "version"),
        }
        for label, (text, field) in cases.items():
            with self.subTest(label):
                self.write(text)
                with self.assertRaisesRegex(ValueError, field):
                    load_global_config(self.path)

    def test_missing_file_is_reported(self) -> None:
        with self.assertRaisesRegex(ValueError, "not found"):
            load_global_config(self.root / "absent.yaml")

    def test_write_job_records_is_optional_and_boolean(self) -> None:
        base = f"version: 1\ninput_directory: {self.root / 'input'}\noutput_directory: /tmp/out\n"
        self.write(base)
        self.assertFalse(load_global_config(self.path).write_job_records)
        self.write(base + "write_job_records: true\n")
        self.assertTrue(load_global_config(self.path).write_job_records)
        self.write(base + "write_job_records: yes please\n")
        with self.assertRaisesRegex(ValueError, "write_job_records"):
            load_global_config(self.path)

    def base(self) -> str:
        return f"version: 1\ninput_directory: {self.root / 'input'}\noutput_directory: /tmp/out\n"

    def cooldown(self, text: str) -> CooldownPolicy | None:
        self.write(self.base() + text)
        return load_global_config(self.path).cooldown

    def test_cooldown_is_optional(self) -> None:
        self.assertIsNone(self.cooldown(""))
        self.assertEqual(DEFAULT_COOLDOWN, CooldownPolicy(mode="auto", ratio=0.5, minimum_seconds=0.0, maximum_seconds=3600.0))

    def test_the_three_cooldown_modes(self) -> None:
        self.assertEqual(self.cooldown("cooldown: {mode: auto}\n"), DEFAULT_COOLDOWN)
        self.assertEqual(self.cooldown("cooldown: {mode: auto, ratio: 0.25, minimum_seconds: 300, maximum_seconds: 1800}\n"), CooldownPolicy(mode="auto", ratio=0.25, minimum_seconds=300.0, maximum_seconds=1800.0))
        self.assertEqual(self.cooldown("cooldown: {mode: auto, ratio: 1, minimum_seconds: 60, maximum_seconds: 60}\n"), CooldownPolicy(mode="auto", ratio=1.0, minimum_seconds=60.0, maximum_seconds=60.0))
        self.assertEqual(self.cooldown("cooldown: {mode: manual, seconds: 900}\n"), CooldownPolicy(mode="manual", seconds=900.0))
        self.assertEqual(self.cooldown("cooldown: {mode: manual, seconds: 0}\n"), CooldownPolicy(mode="manual", seconds=0.0))
        for text in ("cooldown: {mode: off}\n", "cooldown: {mode: 'off'}\n", "cooldown:\n  mode: off\n"):
            with self.subTest(text):
                # YAML reads the unquoted off as false.
                self.assertEqual(self.cooldown(text), CooldownPolicy(mode="off"))

    def test_invalid_cooldown_mappings_name_the_key(self) -> None:
        range_error = "must be a number of seconds from 0 to 3600"
        cases = {
            "cooldown: 900": "'cooldown' must be a mapping with a mode: auto, manual, or off",
            "cooldown: {}": "'cooldown.mode' is required: auto, manual, or off",
            "cooldown: {seconds: 900}": "'cooldown.mode' is required",
            "cooldown: {mode: fast}": "'cooldown.mode' must be auto, manual, or off",
            "cooldown: {mode: true}": "'cooldown.mode' must be auto, manual, or off",
            "cooldown: {mode: on}": "'cooldown.mode' must be auto, manual, or off",
            "cooldown: {mode: 1}": "'cooldown.mode' must be auto, manual, or off",
            "cooldown: {mode: auto, seconds: 900}": "'cooldown.seconds' is not used with mode auto",
            "cooldown: {mode: manual, seconds: 900, ratio: 0.5}": "'cooldown.ratio' is not used with mode manual",
            "cooldown: {mode: manual, seconds: 900, maximum_seconds: 60}": "'cooldown.maximum_seconds' is not used with mode manual",
            "cooldown: {mode: off, seconds: 0}": "'cooldown.seconds' is not used with mode off",
            "cooldown: {mode: off, ratio: 0.5}": "'cooldown.ratio' is not used with mode off",
            "cooldown: {mode: auto, extra: 1}": "'cooldown.extra' is not a known key",
            "cooldown: {mode: manual}": "'cooldown.seconds' is required with mode manual",
            "cooldown: {mode: manual, seconds: -1}": f"'cooldown.seconds' {range_error}",
            "cooldown: {mode: manual, seconds: 3601}": f"'cooldown.seconds' {range_error}",
            "cooldown: {mode: manual, seconds: true}": f"'cooldown.seconds' {range_error}",
            "cooldown: {mode: manual, seconds: .nan}": f"'cooldown.seconds' {range_error}",
            "cooldown: {mode: manual, seconds: 15 min}": f"'cooldown.seconds' {range_error}",
            "cooldown: {mode: auto, minimum_seconds: -5}": f"'cooldown.minimum_seconds' {range_error}",
            "cooldown: {mode: auto, maximum_seconds: .inf}": f"'cooldown.maximum_seconds' {range_error}",
            "cooldown: {mode: auto, maximum_seconds: false}": f"'cooldown.maximum_seconds' {range_error}",
            "cooldown: {mode: auto, ratio: 0}": "'cooldown.ratio' must be a number above 0 and up to 1",
            "cooldown: {mode: auto, ratio: 1.5}": "'cooldown.ratio' must be a number above 0 and up to 1",
            "cooldown: {mode: auto, ratio: true}": "'cooldown.ratio' must be a number above 0 and up to 1",
            "cooldown: {mode: auto, ratio: half}": "'cooldown.ratio' must be a number above 0 and up to 1",
            "cooldown: {mode: auto, minimum_seconds: 600, maximum_seconds: 300}": r"'cooldown.minimum_seconds' must not be above 'cooldown.maximum_seconds' \(300\)",
            "cooldown: {mode: auto, minimum_seconds: 3601}": f"'cooldown.minimum_seconds' {range_error}",
        }
        for text, message in cases.items():
            with self.subTest(text):
                with self.assertRaisesRegex(ValueError, rf"{self.path}: {message}"):
                    self.cooldown(text + "\n")

    def test_the_old_cooldown_seconds_key_names_its_replacement(self) -> None:
        for value, same in (("1200", "{mode: manual, seconds: 1200}"), ("90.5", "{mode: manual, seconds: 90.5}"), ("0", "{mode: off}")):
            with self.subTest(value):
                with self.assertRaises(ValueError) as caught:
                    self.cooldown(f"cooldown_seconds: {value}\n")
                self.assertEqual(str(caught.exception), f"{self.path}: 'cooldown_seconds' was replaced by 'cooldown'; write cooldown: {same} for the same wait, or use mode auto or off (see config/global-config.example.yaml)")

    def test_auto_wait_is_a_share_of_the_run_within_the_bounds(self) -> None:
        policy = CooldownPolicy(mode="auto", minimum_seconds=300.0)
        self.assertEqual(policy.wait_after(1200), CooldownWait(600.0))
        self.assertEqual(policy.wait_after(1201), CooldownWait(601.0))
        self.assertEqual(policy.wait_after(1200.1), CooldownWait(601.0))
        self.assertEqual(policy.wait_after(600), CooldownWait(300.0))
        self.assertEqual(policy.wait_after(200), CooldownWait(300.0, "minimum"))
        self.assertEqual(policy.wait_after(7200), CooldownWait(3600.0))
        self.assertEqual(policy.wait_after(10000), CooldownWait(3600.0, "maximum"))
        self.assertEqual(replace(policy, ratio=0.25).wait_after(1200), CooldownWait(300.0))
        # 1200 x 0.1 is 120.00000000000001 in binary floating point; the wait is still 120.
        self.assertEqual(CooldownPolicy(mode="auto", ratio=0.1).wait_after(1200), CooldownWait(120.0))
        self.assertEqual(DEFAULT_COOLDOWN.wait_after(0.2), CooldownWait(1.0))
        self.assertEqual(DEFAULT_COOLDOWN.wait_after(0), CooldownWait(0.0))
        bounded = CooldownPolicy(mode="auto", minimum_seconds=300.0, maximum_seconds=1800.0)
        self.assertEqual(bounded.wait_after(3600), CooldownWait(1800.0))
        self.assertEqual(bounded.wait_after(3602), CooldownWait(1800.0, "maximum"))

    def test_manual_and_off_waits(self) -> None:
        self.assertEqual(CooldownPolicy(mode="manual", seconds=900.0).wait_after(5), CooldownWait(900.0))
        self.assertEqual(CooldownPolicy(mode="manual", seconds=0.0).wait_after(5000), CooldownWait(0.0))
        self.assertEqual(CooldownPolicy(mode="off").wait_after(5000), CooldownWait(0.0))

    def test_resolved_mapping_and_fixed_seconds(self) -> None:
        self.assertEqual(DEFAULT_COOLDOWN.as_dict(), {"mode": "auto", "ratio": 0.5, "minimum_seconds": 0.0, "maximum_seconds": 3600.0})
        self.assertEqual(CooldownPolicy(mode="manual", seconds=900.0).as_dict(), {"mode": "manual", "seconds": 900.0})
        self.assertEqual(CooldownPolicy(mode="off").as_dict(), {"mode": "off"})
        self.assertEqual([policy.fixed_seconds for policy in (DEFAULT_COOLDOWN, CooldownPolicy(mode="manual", seconds=900.0), CooldownPolicy(mode="off"))], [None, 900.0, 0.0])
        self.assertEqual(parse_cooldown(DEFAULT_COOLDOWN.as_dict(), "cooldown"), DEFAULT_COOLDOWN)

    def test_history_retention_days_defaults_to_14_and_is_a_whole_number_from_0_to_3650(self) -> None:
        base = f"version: 1\ninput_directory: {self.root / 'input'}\noutput_directory: /tmp/out\n"
        self.write(base)
        self.assertEqual(load_global_config(self.path).history_retention_days, 14)
        for text, expected in (("0", 0), ("14", 14), ("3650", 3650)):
            with self.subTest(text):
                self.write(base + f"history_retention_days: {text}\n")
                self.assertEqual(load_global_config(self.path).history_retention_days, expected)
        for text in ("-1", "3651", "1.5", "true", "forever", "null"):
            with self.subTest(text):
                self.write(base + f"history_retention_days: {text}\n")
                with self.assertRaisesRegex(ValueError, rf"{self.path}: 'history_retention_days' must be a whole number of days from 0 to 3650"):
                    load_global_config(self.path)

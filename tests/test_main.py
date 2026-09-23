import json
import tempfile
import unittest
from pathlib import Path

from main import build_generate_command, create_parser, load_config, main


class DrawThingsControlTests(unittest.TestCase):
    def test_load_config_rejects_non_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                load_config(path)

    def test_dry_run_uses_model_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            image = root / "source.png"
            config.write_text(json.dumps({"model": "example.ckpt"}), encoding="utf-8")
            image.touch()
            exit_code = main(
                [
                    "generate",
                    "--config",
                    str(config),
                    "--image",
                    str(image),
                    "--output",
                    str(root / "output.mov"),
                    "--dry-run",
                ]
            )
            self.assertEqual(exit_code, 0)

    def test_command_model_flag_overrides_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            image = root / "source.png"
            config.write_text(json.dumps({"model": "config.ckpt"}), encoding="utf-8")
            image.touch()
            args = create_parser().parse_args(
                [
                    "generate",
                    "--config",
                    str(config),
                    "--image",
                    str(image),
                    "--output",
                    str(root / "output.mov"),
                    "--model",
                    "override.ckpt",
                ]
            )
            self.assertIn("override.ckpt", build_generate_command(args))


if __name__ == "__main__":
    unittest.main()

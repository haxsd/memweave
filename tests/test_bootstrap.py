import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class BootstrapTests(unittest.TestCase):
    def test_dry_run_is_read_only_and_preserves_argument_boundaries(self):
        script = Path(__file__).resolve().parents[1] / "scripts/bootstrap.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "项目 ' root"
            root.mkdir()
            home = Path(directory) / "memory home"
            for client in ("codex", "cursor", "devin"):
                with self.subTest(client=client):
                    completed = subprocess.run(
                        [sys.executable, str(script), "--client", client, "--root", str(root),
                         "--home", str(home), "--dry-run", "--json"],
                        capture_output=True, encoding="utf-8", timeout=10,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    report = json.loads(completed.stdout)
                    self.assertEqual(report["status"], "dry_run")
                    setup = next(command for command in report["commands"] if "setup" in command)
                    self.assertEqual(setup[setup.index("--client") + 1], client)
                    self.assertEqual(setup[setup.index("--root") + 1], str(root))
                    self.assertEqual(setup[setup.index("--home") + 1], str(home))
                    self.assertNotIn("--toolset", setup)
                    self.assertEqual(list(root.iterdir()), [])
                    self.assertFalse(home.exists())

            help_result = subprocess.run(
                [sys.executable, str(script), "--help"],
                capture_output=True, encoding="utf-8", timeout=10,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertNotIn("--toolset", help_result.stdout)

            legacy = subprocess.run(
                [sys.executable, str(script), "--client", "codex", "--root", str(root),
                 "--home", str(home), "--toolset", "core", "--dry-run", "--json"],
                capture_output=True, encoding="utf-8", timeout=10,
            )
            self.assertNotEqual(legacy.returncode, 0)
            self.assertEqual(list(root.iterdir()), [])
            self.assertFalse(home.exists())

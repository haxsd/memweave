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
            completed = subprocess.run(
                [sys.executable, str(script), "--client", "codex", "--root", str(root),
                 "--home", str(home), "--dry-run", "--json"],
                capture_output=True, encoding="utf-8", timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(report["status"], "dry_run")
            setup = next(command for command in report["commands"] if "setup" in command)
            self.assertEqual(setup[setup.index("--root") + 1], str(root))
            self.assertEqual(setup[setup.index("--home") + 1], str(home))
            self.assertEqual(list(root.iterdir()), [])
            self.assertFalse(home.exists())

import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackagingBuildTest(unittest.TestCase):
    def test_wheel_build_discards_stale_build_lib(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            for name in (
                "pyproject.toml",
                "setup.py",
                "README.md",
                "LICENSE",
                "THIRD_PARTY_NOTICES.md",
            ):
                shutil.copy2(ROOT / name, source / name)
            shutil.copytree(ROOT / "licenses", source / "licenses")
            shutil.copytree(
                ROOT / "src",
                source / "src",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )

            stale = source / "build" / "lib" / "basic_memory" / "stale_probe.py"
            stale.parent.mkdir(parents=True)
            stale.write_text("STALE = True\n", encoding="utf-8")
            wheels = Path(temporary) / "wheels"
            wheels.mkdir()

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(wheels),
                    ".",
                ],
                cwd=source,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            wheel = next(wheels.glob("memweave-*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                self.assertNotIn("basic_memory/stale_probe.py", archive.namelist())
            self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()

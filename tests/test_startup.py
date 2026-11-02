import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ServerStartupTests(unittest.TestCase):
    def test_server_import_does_not_load_the_heavy_service(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from memweave.server import create_server; "
                "print('SERVICES_IMPORTED=' + str('memweave.services' in sys.modules))",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SERVICES_IMPORTED=False", result.stdout)


if __name__ == "__main__":
    unittest.main()

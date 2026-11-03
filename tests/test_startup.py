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


    def test_unexpected_service_start_failure_surfaces_as_tool_error(self):
        script = (
            "import asyncio, tempfile; from pathlib import Path; from unittest.mock import patch; "
            "from memweave.diagnostics import Diagnostics; from memweave.server import create_server; "
            "import memweave.services as s\n"
            "home = Path(tempfile.mkdtemp())\n"
            "server = create_server(Diagnostics(home))\n"
            "with patch.object(s.Service, 'start', side_effect=RuntimeError('boom')):\n"
            "    try:\n"
            "        asyncio.run(server.call_tool('memweave_system_status', {}))\n"
            "    except Exception as e:\n"
            "        print('ERR=' + type(e).__name__ + ':' + str(e))\n"
        )
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                                capture_output=True, text=True, check=False)
        self.assertIn("boom", result.stdout + result.stderr)
        self.assertNotIn("ERR=RuntimeError", result.stdout, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

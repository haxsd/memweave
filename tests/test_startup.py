import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from memweave.diagnostics import CLIENT_BOOTSTRAP_CHANNELS, Diagnostics
from memweave.integrations import client_configuration, server_command
from memweave.server import create_server


ROOT = Path(__file__).resolve().parents[1]


class ServerStartupTests(unittest.TestCase):
    def test_server_instructions_declare_one_universal_core(self):
        with tempfile.TemporaryDirectory() as directory:
            server = create_server(Diagnostics(Path(directory)))
        self.assertIn("22-tool core is identical for every stdio MCP client", server.instructions)
        self.assertIn("never capability tiers", server.instructions)
        self.assertIn("workspace_prepare recovery contract", server.instructions)
        self.assertIn("connected tool list alone is not a complete MemWeave experience", server.instructions)
        self.assertIn("startup channel (lifecycle Hook, always-on rule", server.instructions)
        self.assertIn("Skill is on-demand and does not by itself guarantee activation", server.instructions)
        self.assertIn("identify MemWeave tools by the memweave_* suffix", server.instructions)
        self.assertIn("do not treat a missing bare name", server.instructions)
        self.assertIn("do not assume there is no task", server.instructions)
        self.assertIn("verify actual files/tests before continuing", server.instructions)
        self.assertIn("When the user corrects the direction", server.instructions)
        self.assertIn("checkpoint the correction", server.instructions)
        self.assertEqual(
            CLIENT_BOOTSTRAP_CHANNELS["generic"],
            "manual_host_instruction_required",
        )

    def test_json_clients_share_the_same_mcp_server_core(self):
        home = (Path(tempfile.gettempdir()) / "memweave-shared-home").resolve()
        expected = server_command(home)
        for client in ("cursor", "devin", "generic"):
            with self.subTest(client=client):
                configured = json.loads(client_configuration(client, "mcp", home))
                self.assertEqual(configured["mcpServers"]["memweave"], expected)

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

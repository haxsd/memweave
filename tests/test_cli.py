import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "织忆 ' memory home"
        self.environment = {**os.environ, "PYTHONUTF8": "1"}

    def command(self, *arguments, input_text=None):
        return subprocess.run(
            [sys.executable, "-m", "memweave", *arguments, "--home", str(self.home)],
            input=input_text,
            capture_output=True,
            encoding="utf-8",
            env=self.environment,
            timeout=20,
            cwd=self.temporary.name,
        )

    def initialize(self):
        completed = self.command("init")
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_doctor_is_read_only_before_initialization(self):
        completed = self.command("doctor")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(json.loads(completed.stdout)["data"]["initialized"])
        self.assertFalse(self.home.exists())

    def test_init_handles_unicode_spaces_and_repeated_invocation(self):
        self.initialize()
        self.initialize()
        completed = self.command("doctor")
        self.assertTrue(json.loads(completed.stdout)["data"]["initialized"])
        self.assertTrue((self.home / "config.toml").is_file())

    def test_call_reads_json_from_stdin_and_reports_domain_errors(self):
        self.initialize()
        registered = self.command("call", "project_register", input_text=json.dumps({
            "root": self.temporary.name, "name": "CLI 项目", "operation_id": "register",
        }))
        self.assertEqual(registered.returncode, 0, registered.stderr)
        project_id = json.loads(registered.stdout)["data"]["id"]
        listed = self.command("call", "project_list", "--input", "{}")
        self.assertEqual(json.loads(listed.stdout)["data"]["items"][0]["id"], project_id)
        invalid = self.command("call", "unknown", "--input", "{}")
        self.assertEqual(invalid.returncode, 1)
        self.assertEqual(json.loads(invalid.stderr)["error"]["code"], "RECORD_INVALID")

    def test_doctor_advertises_product_capabilities_after_initialization(self):
        self.initialize()
        completed = self.command("doctor")
        data = json.loads(completed.stdout)["data"]
        self.assertEqual(data["task_memory"], "available")
        self.assertEqual(data["long_term_memory"], "available")
        self.assertEqual(data["project_graph"], "available")

    def test_demo_accepts_current_directory_as_project_root(self):
        (Path(self.temporary.name) / "README.md").write_text("# 示例", encoding="utf-8")
        completed = self.command("demo", "--root", ".")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["data"]["restart_readback"])

    def test_cursor_config_preserves_argument_boundaries_without_writing_files(self):
        completed = self.command("config", "--client", "cursor")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        server = json.loads(completed.stdout)["mcpServers"]["memweave"]
        self.assertEqual(server["args"], ["-m", "memweave", "serve", "--home", str(self.home)])
        self.assertEqual(Path(server["command"]), Path(sys.executable))
        self.assertFalse(self.home.exists())
        self.assertFalse((Path(self.temporary.name) / ".cursor").exists())

    def test_codex_config_is_valid_toml_with_absolute_interpreter(self):
        completed = self.command("config", "--client", "codex")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        server = tomllib.loads(completed.stdout)["mcp_servers"]["memweave"]
        self.assertEqual(server["args"][-1], str(self.home))
        self.assertEqual(Path(server["command"]), Path(sys.executable))
        self.assertEqual(server["env"]["PYTHONUTF8"], "1")

    def test_cursor_hook_command_quotes_powershell_literals(self):
        completed = self.command("config", "--client", "cursor", "--kind", "hooks")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        configuration = json.loads(completed.stdout)
        command = configuration["hooks"]["sessionStart"][0]["command"]
        self.assertEqual(configuration["version"], 1)
        if os.name == "nt":
            self.assertIn("'" + str(self.home).replace("'", "''") + "'", command)
            payload = json.dumps({"conversation_id": "ps-test"}, ensure_ascii=False)
            self.initialize()
            execution = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "$input | " + command],
                input=payload,
                capture_output=True,
                encoding="utf-8",
                env=self.environment,
                timeout=20,
            )
            self.assertEqual(execution.returncode, 0, execution.stderr)
            self.assertEqual(json.loads(execution.stdout), {})
            status = json.loads(self.command("doctor").stdout)["data"]
            self.assertEqual(status["hook_receipt_count"], 1)

    def test_codex_hooks_require_an_explicit_project_root(self):
        completed = self.command("config", "--client", "codex", "--kind", "hooks")
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(json.loads(completed.stderr)["error"]["code"], "RECORD_INVALID")

    def test_hook_records_minimal_event_and_returns_no_injected_context(self):
        self.initialize()
        completed = self.command(
            "hook", "--client", "cursor", "--event", "sessionStart",
            input_text=json.dumps({"conversation_id": "session-1", "prompt": "private"}),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {})
        status = json.loads(self.command("doctor").stdout)["data"]
        self.assertEqual(status["hook_receipt_count"], 1)
        self.assertEqual(status["clients"]["cursor"]["desktop_verification"], "unverified")

    def test_malformed_or_oversized_hook_input_does_not_block_host(self):
        self.initialize()
        for input_text in ["not-json", "[]", "x" * 65537, "[" * 20000 + "0" + "]" * 20000]:
            with self.subTest(size=len(input_text)):
                completed = self.command(
                    "hook", "--client", "cursor", "--event", "sessionStart",
                    input_text=input_text,
                )
                self.assertEqual(completed.returncode, 0)
                self.assertEqual(json.loads(completed.stdout), {})
                self.assertEqual(
                    json.loads(completed.stderr)["error"]["code"], "RECORD_INVALID"
                )
        self.assertEqual(json.loads(self.command("doctor").stdout)["data"]["hook_receipt_count"], 0)

    def test_serve_requires_an_initialized_home_and_keeps_stdout_clean(self):
        completed = self.command("serve")
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(json.loads(completed.stderr)["error"]["code"], "NOT_INITIALIZED")


if __name__ == "__main__":
    unittest.main()

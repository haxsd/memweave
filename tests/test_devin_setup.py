import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from memweave.diagnostics import DiagnosticError
from memweave.devin_installation import (
    RULE, SKILL, check_devin, devin_config_path, setup_devin,
)
from memweave.integrations import server_command
from memweave.services import Service


class DevinSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Devin ' project"
        self.root.mkdir()
        self.home = Path(self.temporary.name) / "memory ' home"
        self.global_base = Path(self.temporary.name) / "global-user"
        self.global_base.mkdir()
        self.devin = self.root / ".devin"
        self.config = devin_config_path(self.global_base)

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def snapshot(self):
        return {
            str(path.relative_to(self.devin)): path.read_bytes()
            for path in self.devin.rglob("*") if path.is_file()
        }

    def command(self, command, *arguments, success=True):
        completed = subprocess.run(
            [
                sys.executable, "-m", "memweave", command, "--client", "devin",
                "--root", str(self.root), "--home", str(self.home),
                "--global-base", str(self.global_base), "--json", *arguments,
            ],
            capture_output=True, encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"},
            cwd=self.temporary.name, timeout=30,
        )
        self.assertEqual(completed.returncode, 0 if success else 1, completed.stderr)
        return json.loads(completed.stdout or completed.stderr)

    def test_setup_merges_global_config_preserves_services_and_is_idempotent(self):
        original = {
            "mcpServers": {"other": {"command": "other.exe", "args": ["--keep"]}},
            "custom": {"keep": True},
        }
        self.write_json(self.config, original)
        installed = setup_devin(
            self.root, self.home, global_base=self.global_base,
        )
        self.assertEqual(installed["state"], "installed")
        self.assertEqual(installed["bootstrap_channel"], "always_on_rule")
        merged = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(merged["mcpServers"]["other"], original["mcpServers"]["other"])
        self.assertEqual(merged["custom"], {"keep": True})
        self.assertEqual(merged["mcpServers"]["memweave"], server_command(self.home))
        self.assertFalse(installed["hooks_installed"])
        self.assertTrue(Path(installed["global_backup_directory"]).is_dir())
        self.assertTrue(list((self.config.parent / "memweave-backups").rglob("mcp_config.json")))

        self.assertTrue((self.devin / SKILL).is_file())
        self.assertTrue((self.devin / RULE).is_file())
        rule = (self.devin / RULE).read_text(encoding="utf-8")
        self.assertIn("trigger: always_on", rule)
        self.assertIn(".devin/skills/memweave-continuity/SKILL.md", rule)
        self.assertIn("always-on Rule 就是会话启动指令通道", rule)
        self.assertIn("自动恢复能力弱于有 Hook 的宿主", rule)
        self.assertIn("如果本回合没有看到 MemWeave 启动块", rule)
        self.assertIn("用户纠正、取消旧决定或新证据推翻当前方案", rule)
        self.assertIn("仅出现 memweave_* 工具而没有本 Rule", rule)
        self.assertIn("不要把“裸名未出现”误判为 MCP 未加载", rule)
        ignore = (self.devin / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/memweave.json", ignore)
        manifest = json.loads((self.devin / "memweave.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["client"], "devin")
        self.assertEqual(manifest["mcp_config"], str(self.config))
        self.assertNotIn("hooks", manifest)
        self.assertIn(
            str(self.root.resolve()),
            {item["root"] for item in Service(self.home).project_list()["items"]},
        )

        snapshot = self.snapshot()
        repeated = setup_devin(self.root, self.home, global_base=self.global_base)
        self.assertEqual(repeated["state"], "up_to_date")
        self.assertEqual(snapshot, self.snapshot())
        checked = check_devin(self.root, self.home, probe=True, global_base=self.global_base)
        self.assertEqual(checked["state"], "ready")
        self.assertEqual(checked["protocol_verification"], "sdk_stdio")
        self.assertEqual(checked["desktop_verification"], "unverified")
        self.assertFalse(checked["hooks_installed"])

    def test_conflicting_global_memweave_is_not_overwritten(self):
        self.write_json(self.config, {
            "mcpServers": {
                "memweave": {"command": "custom-memweave.exe", "args": ["--custom"]},
            },
        })
        before = self.config.read_bytes()
        with self.assertRaises(DiagnosticError) as caught:
            setup_devin(self.root, self.home, global_base=self.global_base)
        self.assertEqual(caught.exception.code, "SETUP_CONFLICT")
        self.assertEqual(before, self.config.read_bytes())
        self.assertFalse(self.home.exists())

    def test_check_reports_missing_global_config_and_drift(self):
        setup_devin(
            self.root, self.home, global_base=self.global_base, write_global=False,
        )
        self.assertFalse(self.config.exists())
        checked = check_devin(self.root, self.home, global_base=self.global_base)
        names = {item["name"] for item in checked["checks"] if item["state"] == "error"}
        self.assertTrue({"global_config", "mcp_global"}.issubset(names))
        self.assertEqual(checked["state"], "needs_attention")

        self.write_json(self.config, {"mcpServers": {"memweave": server_command(self.home)}})
        self.assertEqual(
            check_devin(self.root, self.home, global_base=self.global_base)["state"],
            "ready",
        )
        (self.devin / SKILL).write_text("custom skill", encoding="utf-8")
        drifted = check_devin(self.root, self.home, global_base=self.global_base)
        self.assertTrue(any(
            item["name"] == "skill" and item["state"] == "error"
            for item in drifted["checks"]
        ))

    def test_cli_config_and_setup_check_support_devin_and_generic(self):
        environment = {**os.environ, "PYTHONUTF8": "1"}
        for client in ("devin", "generic"):
            completed = subprocess.run(
                [
                    sys.executable, "-m", "memweave", "config",
                    "--client", client, "--home", str(self.home),
                ],
                capture_output=True, encoding="utf-8", env=environment,
                cwd=self.temporary.name, timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            server = json.loads(completed.stdout)["mcpServers"]["memweave"]
            self.assertEqual(server["args"][-1], str(self.home))

        installed = self.command("setup")["data"]
        self.assertEqual(installed["mcp_scope"], "global")
        self.assertFalse(installed["hooks_installed"])
        checked = self.command("check")["data"]
        self.assertEqual(checked["state"], "ready")
        self.assertEqual(checked["tool_count"], 22)

        completed = subprocess.run(
            [
                sys.executable, "-m", "memweave", "config",
                "--client", "generic", "--kind", "hooks", "--home", str(self.home),
            ],
            capture_output=True, encoding="utf-8", env=environment,
            cwd=self.temporary.name, timeout=20,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(json.loads(completed.stderr)["error"]["code"], "RECORD_INVALID")


if __name__ == "__main__":
    unittest.main()

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "项目 ' root"
        self.root.mkdir()
        self.home = Path(self.temporary.name) / "memory home"
        self.cursor = self.root / ".cursor"

    def command(self, command, *arguments, success=True):
        completed = subprocess.run(
            [sys.executable, "-m", "memweave", command, "--root", str(self.root),
             "--home", str(self.home), "--json", *arguments],
            capture_output=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"},
            cwd=self.temporary.name, timeout=30,
        )
        self.assertEqual(completed.returncode, 0 if success else 1, completed.stderr)
        return json.loads(completed.stdout or completed.stderr)

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def snapshot(self):
        return {str(path.relative_to(self.cursor)): path.read_bytes()
                for path in self.cursor.rglob("*") if path.is_file()}

    def test_setup_installs_all_resources_and_repeated_run_is_idempotent(self):
        first = self.command("setup")["data"]
        self.assertEqual(first["state"], "installed")
        self.assertTrue((self.cursor / "skills/memweave-continuity/SKILL.md").is_file())
        self.assertIn(
            "MemWeave · 尚未建立任务",
            (self.cursor / "skills/memweave-continuity/SKILL.md").read_text(encoding="utf-8"),
        )
        skill_text = (self.cursor / "skills/memweave-continuity/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("do not end the turn merely to report an intermediate milestone", skill_text)
        self.assertIn("Do not re-read this file through a shell command", skill_text)
        self.assertTrue((self.cursor / "rules/memweave.mdc").is_file())
        ignore = (self.cursor / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/mcp.json", ignore)
        self.assertIn("/skills/memweave-continuity/", ignore)
        self.assertIn("/rules/memweave.mdc", ignore)
        rule_text = (self.cursor / "rules/memweave.mdc").read_text(encoding="utf-8")
        self.assertIn("不因完成一个子步骤或保存检查点就交回控制权", rule_text)
        self.assertIn("状态、接续", rule_text)
        self.assertIn("不得用 CLI/Python 绕过 MCP 硬门", rule_text)
        self.assertIn("当前工具列表已出现", rule_text)
        self.assertIn("不得凭本规则凭空发起工具调用", rule_text)
        mcp = json.loads((self.cursor / "mcp.json").read_text(encoding="utf-8"))
        self.assertNotIn("--toolset", mcp["mcpServers"]["memweave"]["args"])
        manifest = json.loads((self.cursor / "memweave.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["toolset"], "universal")
        installed_hooks = json.loads((self.cursor / "hooks.json").read_text())["hooks"]
        self.assertEqual(set(installed_hooks), {"sessionStart", "sessionEnd"})
        self.assertFalse(
            {"preCompact", "afterMCPExecution", "afterFileEdit"} & set(installed_hooks)
        )
        snapshot = self.snapshot()
        repeated = self.command("setup")["data"]
        self.assertEqual(repeated["state"], "up_to_date")
        self.assertEqual(snapshot, self.snapshot())
        healthy = self.command("check")["data"]
        self.assertEqual(healthy["state"], "ready")
        self.assertEqual(healthy["tool_count"], 22)
        self.assertEqual(healthy["desktop_verification"], "unverified")
        self.assertIn("同一项目后续会话", " ".join(healthy.get("required_actions", [])))
        self.assertIn("不代表模型已收到", " ".join(healthy.get("required_actions", [])))

    def test_setup_merges_cursor_local_ignore_without_overwriting_existing_rules(self):
        self.cursor.mkdir()
        ignore = self.cursor / ".gitignore"
        ignore.write_text("custom-local-rule\n", encoding="utf-8")

        self.command("setup")

        content = ignore.read_text(encoding="utf-8")
        self.assertIn("custom-local-rule\n", content)
        self.assertIn("/mcp.json\n", content)
        self.assertIn("/memweave.json\n", content)
        repeated = self.command("setup")["data"]
        self.assertEqual(repeated["state"], "up_to_date")
        self.assertEqual(content, ignore.read_text(encoding="utf-8"))

    def test_merge_preserves_unrelated_servers_hooks_and_settings_and_backs_up(self):
        original = {"mcpServers": {"other": {"command": "other.exe"}}, "custom": {"keep": True}}
        self.write_json(self.cursor / "mcp.json", original)
        self.write_json(self.cursor / "hooks.json", {
            "version": 1, "custom": "keep",
            "hooks": {"stop": [{"command": "my-hook.exe"}], "beforeReadFile": [{"command": "read.exe"}]},
        })
        installed = self.command("setup")["data"]
        merged = json.loads((self.cursor / "mcp.json").read_text())
        hooks = json.loads((self.cursor / "hooks.json").read_text())
        self.assertEqual(merged["mcpServers"]["other"], original["mcpServers"]["other"])
        self.assertEqual(merged["custom"], {"keep": True})
        self.assertEqual(hooks["hooks"]["stop"][0]["command"], "my-hook.exe")
        self.assertEqual(hooks["custom"], "keep")
        backups = Path(installed["backup_directory"])
        self.assertEqual(json.loads((backups / "mcp.json").read_text()), original)
        self.command("setup")
        hooks = json.loads((self.cursor / "hooks.json").read_text())
        self.assertEqual(len(hooks["hooks"]["stop"]), 1)
        self.assertEqual(self.command("check")["data"]["tool_count"], 22)

    def test_modified_skill_is_preserved_and_update_conflicts_before_config_writes(self):
        self.command("setup")
        skill = self.cursor / "skills/memweave-continuity/SKILL.md"
        skill.write_text("user custom skill", encoding="utf-8")
        before = self.snapshot()
        error = self.command("setup", success=False)
        self.assertEqual(error["error"]["code"], "SETUP_CONFLICT")
        self.assertEqual(before, self.snapshot())
        checked = self.command("check", success=False)["data"]
        self.assertTrue(any(item["name"] == "skill" and item["state"] == "error" for item in checked["checks"]))

    def test_invalid_existing_json_is_not_overwritten_and_does_not_initialize_home(self):
        self.cursor.mkdir()
        path = self.cursor / "mcp.json"
        path.write_text("invalid JSON", encoding="utf-8")
        error = self.command("setup", success=False)
        self.assertEqual(error["error"]["code"], "CONFIG_INVALID")
        self.assertEqual(path.read_text(), "invalid JSON")
        self.assertFalse(self.home.exists())

    def test_check_missing_install_is_read_only_and_has_repair_advice(self):
        checked = self.command("check", success=False)["data"]
        self.assertEqual(checked["state"], "needs_attention")
        self.assertTrue(any("setup" in item.get("fix", "") for item in checked["checks"]))
        self.assertFalse(self.cursor.exists())
        self.assertFalse(self.home.exists())

    def test_check_detects_missing_hook_and_does_not_repair_it_implicitly(self):
        self.command("setup")
        hooks = json.loads((self.cursor / "hooks.json").read_text())
        hooks["hooks"].pop("sessionEnd")
        self.write_json(self.cursor / "hooks.json", hooks)
        before = self.snapshot()
        checked = self.command("check", success=False)["data"]
        self.assertTrue(any(item["name"] == "hooks" and item["state"] == "error" for item in checked["checks"]))
        self.assertEqual(before, self.snapshot())
        self.command("setup")
        self.assertEqual(self.command("check")["data"]["state"], "ready")

    def test_setup_removes_legacy_high_frequency_hook_inventory(self):
        self.command("setup")
        hooks_path = self.cursor / "hooks.json"
        manifest_path = self.cursor / "memweave.json"
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        legacy_edit = {"command": "python -m memweave hook --client cursor --event afterFileEdit"}
        legacy_stop = {"command": "python -m memweave hook --client cursor --event stop"}
        hooks["hooks"]["afterFileEdit"] = [legacy_edit]
        hooks["hooks"]["stop"] = [legacy_stop]
        manifest["hooks"]["afterFileEdit"] = [legacy_edit]
        manifest["hooks"]["stop"] = [legacy_stop]
        self.write_json(hooks_path, hooks)
        self.write_json(manifest_path, manifest)
        self.assertEqual(self.command("check", success=False)["data"]["state"], "needs_attention")
        self.command("setup")
        repaired = json.loads(hooks_path.read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(set(repaired), {"sessionStart", "sessionEnd"})

    def test_probe_uses_configured_stdio_without_creating_business_tasks(self):
        self.command("setup")
        checked = self.command("check", "--probe")["data"]
        self.assertEqual(checked["protocol_verification"], "sdk_stdio")
        self.assertEqual(checked["desktop_verification"], "unverified")
        self.assertEqual(checked["active_task_count"], 0)

    def test_modified_manifest_cannot_make_probe_execute_an_arbitrary_command(self):
        self.command("setup")
        manifest = json.loads((self.cursor / "memweave.json").read_text())
        malicious = dict(command=sys.executable, args=["-c", "raise SystemExit(0)"])
        manifest["server"] = malicious
        self.write_json(self.cursor / "memweave.json", manifest)
        self.write_json(self.cursor / "mcp.json", {"mcpServers": {"memweave": malicious}})
        checked = self.command("check", "--probe", success=False)["data"]
        self.assertEqual(checked["protocol_verification"], "not_run")
        self.assertTrue(any(item["name"] == "server_command" and item["state"] == "error"
                            for item in checked["checks"]))

    def test_setup_and_check_print_a_short_human_result_by_default(self):
        for command in ("setup", "check"):
            completed = subprocess.run(
                [sys.executable, "-m", "memweave", command, "--root", str(self.root), "--home", str(self.home)],
                capture_output=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"},
                cwd=self.temporary.name, timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(str(self.root), completed.stdout)
            self.assertIn("22", completed.stdout)
            self.assertFalse(completed.stdout.lstrip().startswith("{"))

    def test_update_reuses_recorded_home_and_refreshes_old_resource_version(self):
        self.command("setup")
        manifest_path = self.cursor / "memweave.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = "0.0.0"
        self.write_json(manifest_path, manifest)
        self.command("check", success=False)
        completed = subprocess.run(
            [sys.executable, "-m", "memweave", "setup", "--root", str(self.root), "--json"],
            capture_output=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"},
            cwd=self.temporary.name, timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["data"]["home"], str(self.home))
        self.assertNotEqual(json.loads(manifest_path.read_text())["version"], "0.0.0")
        self.assertEqual(self.command("check")["data"]["state"], "ready")

    def test_partial_file_failure_restores_previous_config_and_leaves_backups(self):
        from memweave import installation

        original = {"mcpServers": {"other": {"command": "other.exe"}}}
        self.write_json(self.cursor / "mcp.json", original)
        original_bytes = (self.cursor / "mcp.json").read_bytes()
        replace = installation.replace_file

        def fail_hooks(path, content):
            if path == self.cursor / "hooks.json":
                raise OSError("injected write failure")
            return replace(path, content)

        with patch.object(installation, "replace_file", side_effect=fail_hooks):
            with self.assertRaises(OSError):
                installation.setup_cursor(self.root, self.home)
        self.assertEqual((self.cursor / "mcp.json").read_bytes(), original_bytes)
        self.assertFalse((self.cursor / "memweave.json").exists())
        self.assertTrue(list((self.cursor / "memweave-backups").rglob("mcp.json")))

    @unittest.skipUnless(os.name == "nt", "Windows junction boundary")
    def test_linked_cursor_directory_cannot_write_outside_project(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        linked = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(self.cursor), str(outside)],
            capture_output=True, timeout=10,
        )
        if linked.returncode:
            self.skipTest("Junction creation unavailable")
        self.addCleanup(self.cursor.rmdir)
        error = self.command("setup", success=False)
        self.assertEqual(error["error"]["code"], "SETUP_CONFLICT")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(self.home.exists())

    def test_malformed_manifest_nested_fields_fail_without_changing_files(self):
        self.command("setup")
        manifest = json.loads((self.cursor / "memweave.json").read_text())
        manifest["hooks"]["stop"] = "invalid"
        self.write_json(self.cursor / "memweave.json", manifest)
        before = self.snapshot()
        error = self.command("setup", success=False)
        self.assertEqual(error["error"]["code"], "CONFIG_INVALID")
        self.assertEqual(before, self.snapshot())

    def test_empty_manifest_inventory_cannot_hide_missing_resources_from_check(self):
        self.command("setup")
        manifest = json.loads((self.cursor / "memweave.json").read_text())
        manifest["hooks"] = {}
        manifest["resources"] = {}
        self.write_json(self.cursor / "memweave.json", manifest)
        (self.cursor / "skills/memweave-continuity/SKILL.md").unlink()
        checked = self.command("check", success=False)["data"]
        self.assertEqual(checked["state"], "needs_attention")

    def test_missing_managed_rule_is_repaired_without_switching_to_external_rule(self):
        self.command("setup")
        (self.cursor / "rules/memweave.mdc").unlink()
        trial = self.cursor / "rules/memweave-trial.mdc"
        trial.write_text("other custom rule", encoding="utf-8")
        self.command("setup")
        self.assertTrue((self.cursor / "rules/memweave.mdc").is_file())
        self.assertEqual(trial.read_text(), "other custom rule")

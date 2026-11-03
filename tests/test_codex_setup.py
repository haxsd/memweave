import json
import os
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


class CodexSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "项目 ' root"
        self.root.mkdir()
        self.home = Path(self.temporary.name) / "memory home"
        self.environment = {**os.environ, "PYTHONUTF8": "1"}

    def command(self, action, *arguments, success=True):
        completed = subprocess.run(
            [sys.executable, "-m", "memweave", action, "--client", "codex",
             "--root", str(self.root), "--home", str(self.home), "--json", *arguments],
            input=None, capture_output=True, encoding="utf-8", env=self.environment,
            cwd=self.temporary.name, timeout=30,
        )
        self.assertEqual(completed.returncode, 0 if success else 1, completed.stderr)
        return json.loads(completed.stdout or completed.stderr)["data"]

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for directory in (self.root / ".codex", self.root / ".agents")
                if directory.exists() for path in directory.rglob("*") if path.is_file()}

    def test_codex_setup_and_probe_install_all_components_without_touching_agents(self):
        original = "# Existing project rules\nKeep business invariants.\n"
        (self.root / "AGENTS.md").write_text(original, encoding="utf-8")
        installed = self.command("setup")
        config = tomllib.loads((self.root / ".codex/config.toml").read_text(encoding="utf-8"))
        self.assertIn("memweave", config["mcp_servers"])
        self.assertTrue(config["mcp_servers"]["memweave"]["required"])
        self.assertEqual(config["mcp_servers"]["memweave"]["startup_readiness"], "catalog")
        self.assertTrue(config["features"]["hooks"])
        self.assertTrue((self.root / ".agents/skills/memweave-continuity/SKILL.md").is_file())
        hooks = json.loads((self.root / ".codex/hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(
            set(hooks["hooks"]),
            {"SessionStart", "PreCompact", "UserPromptSubmit", "Interrupt", "SessionEnd"},
        )
        self.assertFalse({"PostToolUse", "Stop"} & set(hooks["hooks"]))
        self.assertTrue(installed["required_actions"])
        self.assertEqual((self.root / "AGENTS.md").read_text(), original)
        checked = self.command("check", "--probe")
        self.assertEqual(checked["state"], "ready")
        self.assertEqual(checked["protocol_verification"], "sdk_stdio")
        self.assertEqual(checked["hook_trust"], "requires_host_review")
        self.assertEqual(checked["desktop_verification"], "unverified")
        before = self.snapshot()
        self.assertEqual(self.command("setup")["state"], "up_to_date")
        self.assertEqual(before, self.snapshot())

    def test_codex_merge_preserves_comments_other_mcp_and_hook_groups(self):
        directory = self.root / ".codex"
        directory.mkdir()
        (directory / "config.toml").write_text(
            '# user comment\nmodel = "existing-model"\n[features]\nother = true\n'
            '[mcp_servers.other]\ncommand = "other.exe"\n', encoding="utf-8"
        )
        unrelated = {"hooks": [{"type": "command", "command": "my-other-hook"}]}
        (directory / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [unrelated]}}), encoding="utf-8")
        self.command("setup")
        text = (directory / "config.toml").read_text(encoding="utf-8")
        config = tomllib.loads(text)
        self.assertIn("# user comment", text)
        self.assertEqual(config["model"], "existing-model")
        self.assertTrue(config["features"]["other"])
        self.assertEqual(config["mcp_servers"]["other"]["command"], "other.exe")
        self.assertIn(unrelated, json.loads((directory / "hooks.json").read_text())["hooks"]["Stop"])
        before = self.snapshot()
        repeated = self.command("setup")
        self.assertEqual(repeated["state"], "up_to_date")
        self.assertEqual(before, self.snapshot())

    def test_codex_setup_removes_legacy_high_frequency_hook_group(self):
        self.command("setup")
        hooks_path = self.root / ".codex/hooks.json"
        manifest_path = self.root / ".codex/memweave.json"
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        legacy = {"hooks": [{
            "type": "command",
            "command": "python -m memweave hook --client codex --event PostToolUse",
        }]}
        legacy_stop = {"hooks": [{
            "type": "command",
            "command": "python -m memweave hook --client codex --event Stop",
        }]}
        hooks["hooks"]["PostToolUse"] = [legacy]
        manifest["hooks"]["PostToolUse"] = [legacy]
        hooks["hooks"]["Stop"] = [legacy_stop]
        manifest["hooks"]["Stop"] = [legacy_stop]
        hooks_path.write_text(json.dumps(hooks), encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.command("check", success=False)["state"], "needs_attention")
        self.command("setup")
        repaired = json.loads(hooks_path.read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(
            set(repaired),
            {"SessionStart", "PreCompact", "UserPromptSubmit", "Interrupt", "SessionEnd"},
        )

    def test_modified_skill_is_reported_and_not_overwritten(self):
        self.command("setup")
        path = self.root / ".agents/skills/memweave-continuity/SKILL.md"
        path.write_text("user modification", encoding="utf-8")
        before = self.snapshot()
        self.command("setup", success=False)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.command("check", success=False)["state"], "needs_attention")

    def test_check_absent_codex_install_is_read_only(self):
        result = self.command("check", success=False)
        self.assertEqual(result["state"], "needs_attention")
        self.assertFalse((self.root / ".codex").exists())
        self.assertFalse(self.home.exists())

    @unittest.skipUnless(os.name == "nt", "Windows Hook invocation")
    def test_generated_windows_hook_executes_with_unicode_and_quoted_root(self):
        self.command("setup")
        hooks = json.loads((self.root / ".codex/hooks.json").read_text(encoding="utf-8"))
        command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"]
        completed = subprocess.run(
            shlex.split(command), input=json.dumps({
                "session_id": "native-command", "cwd": str(self.root),
                "hook_event_name": "SessionStart", "source": "startup",
            }), capture_output=True, encoding="utf-8", env=self.environment, timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(json.loads(completed.stdout)["hookSpecificOutput"]["hookEventName"], "SessionStart")

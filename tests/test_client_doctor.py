import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tomlkit

from memweave.client_doctor import doctor_clients
from memweave.codex_installation import setup_codex
from memweave.integrations import server_command
from memweave.installation import fingerprint, setup_cursor
from memweave.services import Service


class ClientDoctorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.home = Path(self.temporary.name) / "memory home"
        self.global_base = Path(self.temporary.name) / "global"
        self.global_base.mkdir()

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }

    def write_codex_global(self, server=None, include_root=False):
        directory = self.global_base / ".codex"
        directory.mkdir(parents=True, exist_ok=True)
        document = tomlkit.document()
        if include_root:
            projects = tomlkit.table()
            projects[str(self.root)] = {"trust_level": "trusted"}
            document["projects"] = projects
        if server is not None:
            servers = tomlkit.table()
            servers["memweave"] = server
            document["mcp_servers"] = servers
        (directory / "config.toml").write_text(tomlkit.dumps(document), encoding="utf-8")

    def test_healthy_installations_are_reported_without_writes(self):
        setup_cursor(self.root, self.home)
        setup_codex(self.root, self.home)
        first = doctor_clients(self.home, global_base=self.global_base)
        self.assertEqual(first["state"], "ready")
        self.assertEqual(first["summary"]["ready"], 2)
        before = self.snapshot()
        second = doctor_clients(self.home, global_base=self.global_base)
        self.assertEqual(second, first)
        self.assertEqual(before, self.snapshot())

    def test_missing_cursor_local_ignore_is_reported_and_repaired(self):
        setup_cursor(self.root, self.home)
        ignore = self.root / ".cursor" / ".gitignore"
        ignore.unlink()
        found = doctor_clients(self.home, clients=("cursor",), global_base=self.global_base)
        target = next(item for item in found["targets"] if item["scope"] == "project")
        self.assertIn("local_ignore_missing", {issue["code"] for issue in target["issues"]})

        repaired = doctor_clients(
            self.home, clients=("cursor",), fix=True, global_base=self.global_base,
        )
        target = next(item for item in repaired["targets"] if item["scope"] == "project")
        self.assertEqual(target["state"], "repaired")
        self.assertIn("/mcp.json", ignore.read_text(encoding="utf-8"))

    def test_codex_known_project_with_stale_home_is_repaired_and_idempotent(self):
        old_home = Path(self.temporary.name) / "old home"
        setup_codex(self.root, old_home)
        Service(self.home).initialize()
        self.write_codex_global(include_root=True)

        found = doctor_clients(self.home, clients=("codex",), global_base=self.global_base)
        self.assertEqual(found["state"], "needs_attention")
        target = next(item for item in found["targets"] if item["scope"] == "project")
        codes = {issue["code"] for issue in target["issues"]}
        self.assertTrue({"stale_home", "project_unregistered"}.issubset(codes))

        repaired = doctor_clients(
            self.home, clients=("codex",), fix=True, global_base=self.global_base,
        )
        self.assertEqual(repaired["state"], "repaired")
        target = next(item for item in repaired["targets"] if item["scope"] == "project")
        self.assertEqual(target["state"], "repaired")
        self.assertTrue(target["restart_required"])
        self.assertTrue(Path(target["repair"]["backup_directory"]).is_dir())
        manifest = json.loads((self.root / ".codex/memweave.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["home"], str(self.home.resolve()))
        registered = Service(self.home).project_list()["items"]
        self.assertIn(str(self.root.resolve()), {item["root"] for item in registered})

        self.assertEqual(
            doctor_clients(self.home, clients=("codex",), global_base=self.global_base)["state"],
            "ready",
        )

    def test_global_project_conflict_is_reported_but_never_rewritten(self):
        setup_codex(self.root, self.home)
        old_home = Path(self.temporary.name) / "old home"
        self.write_codex_global(server=server_command(old_home, "core"), include_root=True)
        global_config = self.global_base / ".codex/config.toml"
        before = global_config.read_bytes()

        result = doctor_clients(self.home, clients=("codex",), global_base=self.global_base)
        global_target = next(item for item in result["targets"] if item["scope"] == "global")
        project_target = next(item for item in result["targets"] if item["scope"] == "project")
        self.assertIn("stale_home", {item["code"] for item in global_target["issues"]})
        self.assertIn("duplicate_mcp", {item["code"] for item in project_target["issues"]})

        repaired = doctor_clients(
            self.home, clients=("codex",), fix=True, global_base=self.global_base,
        )
        self.assertEqual(repaired["state"], "needs_attention")
        self.assertEqual(before, global_config.read_bytes())
        project_target = next(item for item in repaired["targets"] if item["scope"] == "project")
        self.assertNotIn("repair", project_target)

    def test_modified_managed_resource_is_preserved_when_fix_is_requested(self):
        setup_cursor(self.root, self.home)
        skill = self.root / ".cursor/skills/memweave-continuity/SKILL.md"
        skill.write_text("locally customized", encoding="utf-8")
        found = doctor_clients(self.home, clients=("cursor",), global_base=self.global_base)
        target = found["targets"][0]
        self.assertIn("skill_drift", {item["code"] for item in target["issues"]})

        before = self.snapshot()
        repaired = doctor_clients(
            self.home, clients=("cursor",), fix=True, global_base=self.global_base,
        )
        self.assertEqual(repaired["state"], "needs_attention")
        self.assertEqual(skill.read_text(encoding="utf-8"), "locally customized")
        self.assertEqual(before, self.snapshot())
        target = repaired["targets"][0]
        self.assertEqual(target["repair"]["state"], "failed")
        self.assertIn("modified", target["repair"]["message"].lower())

    def test_old_managed_resources_are_reported_and_refreshed_from_current_package(self):
        setup_cursor(self.root, self.home)
        skill = self.root / ".cursor/skills/memweave-continuity/SKILL.md"
        rule = self.root / ".cursor/rules/memweave.mdc"
        expected_skill = skill.read_bytes()
        expected_rule = rule.read_bytes()
        old_skill = b"old managed skill"
        old_rule = b"old managed rule"
        skill.write_bytes(old_skill)
        rule.write_bytes(old_rule)
        manifest_path = self.root / ".cursor/memweave.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["resources"]["skills/memweave-continuity/SKILL.md"]["sha256"] = fingerprint(old_skill)
        manifest["resources"]["rules/memweave.mdc"]["sha256"] = fingerprint(old_rule)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        found = doctor_clients(self.home, clients=("cursor",), global_base=self.global_base)
        target = found["targets"][0]
        self.assertEqual(found["state"], "needs_attention")
        self.assertTrue({"skill_drift", "rule_drift"}.issubset(
            {item["code"] for item in target["issues"]}
        ))

        repaired = doctor_clients(
            self.home, clients=("cursor",), fix=True, global_base=self.global_base,
        )
        self.assertEqual(repaired["state"], "repaired")
        self.assertEqual(skill.read_bytes(), expected_skill)
        self.assertEqual(rule.read_bytes(), expected_rule)
        self.assertEqual(
            doctor_clients(self.home, clients=("cursor",), global_base=self.global_base)["state"],
            "ready",
        )

    def test_explicit_uninstalled_root_is_read_only(self):
        Service(self.home).initialize()
        result = doctor_clients(
            self.home, roots=[self.root], clients=("cursor", "codex"),
            fix=True, global_base=self.global_base,
        )
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["summary"]["not_installed"], 2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cli_json_discovers_explicit_project(self):
        setup_cursor(self.root, self.home)
        environment = {
            **os.environ, "PYTHONUTF8": "1", "USERPROFILE": str(self.global_base),
            "HOME": str(self.global_base),
        }
        completed = subprocess.run(
            [sys.executable, "-m", "memweave", "clients", "doctor",
             "--home", str(self.home), "--root", str(self.root),
             "--client", "cursor", "--json"],
            capture_output=True, encoding="utf-8", env=environment,
            cwd=self.temporary.name, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        data = json.loads(completed.stdout)["data"]
        self.assertEqual(data["state"], "ready")
        self.assertEqual(data["targets"][0]["scope"], "project")

    def test_missing_registered_root_is_reported_without_aborting_scan(self):
        Service(self.home).initialize()
        Service(self.home).project_register(
            str(self.root), "temporary", "register-missing-root",
        )
        self.root.rmdir()
        result = doctor_clients(self.home, clients=("cursor",), global_base=self.global_base)
        self.assertEqual(result["state"], "needs_attention")
        target = next(item for item in result["targets"] if item["scope"] == "project")
        self.assertEqual(target["issues"][0]["code"], "root_invalid")

    def test_malformed_global_memweave_entry_is_reported(self):
        Service(self.home).initialize()
        directory = self.global_base / ".codex"
        directory.mkdir()
        (directory / "config.toml").write_text(
            '[mcp_servers]\nmemweave = "invalid"\n', encoding="utf-8",
        )
        result = doctor_clients(self.home, clients=("codex",), global_base=self.global_base)
        target = next(item for item in result["targets"] if item["scope"] == "global")
        self.assertEqual(target["state"], "needs_attention")
        self.assertEqual(target["issues"][0]["code"], "config_invalid")

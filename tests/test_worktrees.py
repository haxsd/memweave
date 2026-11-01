import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from memweave.client_doctor import doctor_clients
from memweave.services import Service
from memweave.worktrees import git_common_dir, linked_worktrees, primary_worktree_root


def make_linked_worktree(base: Path) -> tuple[Path, Path]:
    repository = base / "repository"
    worktree = base / "linked-worktree"
    repository.mkdir()
    worktree.mkdir()
    metadata = repository / ".git" / "worktrees" / "linked-worktree"
    metadata.mkdir(parents=True)
    (metadata / "gitdir").write_text(str(worktree / ".git") + "\n", encoding="utf-8")
    (metadata / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {metadata}\n", encoding="utf-8")
    return repository, worktree


class WorktreeResolverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repository, self.worktree = make_linked_worktree(self.base)
        self.home = self.base / "home"
        self.service = Service(self.home)
        self.service.start()
        self.addCleanup(self.service.close)

    def call(self, action, **parameters):
        return self.service.dispatch(action, parameters)["data"]

    def test_linked_worktree_shares_common_directory_and_primary_root(self):
        self.assertEqual(git_common_dir(self.repository), (self.repository / ".git").resolve())
        self.assertEqual(git_common_dir(self.worktree), (self.repository / ".git").resolve())
        self.assertEqual(primary_worktree_root(self.worktree), self.repository.resolve())
        self.assertEqual(
            [path.resolve() for path in linked_worktrees(self.repository)],
            [self.worktree.resolve()],
        )

    def test_workspace_prepare_resolves_worktree_to_the_registered_project(self):
        project = self.call(
            "project_register", root=str(self.repository), name="shared", operation_id="project",
        )
        task = self.call(
            "task_open", project_id=project["id"], title="shared task", goal="continue in worktree",
            acceptance=["worktree resumes"], operation_id="task",
        )

        prepared = self.call(
            "workspace_prepare", root=str(self.worktree), intent="resume",
        )

        self.assertEqual(prepared["state"], "ready")
        self.assertEqual(prepared["project"]["id"], project["id"])
        self.assertEqual(prepared["project"]["root"], str(self.repository.resolve()))
        self.assertEqual(prepared["execution_root"], str(self.worktree.resolve()))
        self.assertIn(
            f"Execution root: {self.worktree.resolve()}",
            prepared["context"]["text"],
        )
        self.assertIn(
            f"Structure evidence root: {self.repository.resolve()}",
            prepared["context"]["text"],
        )
        self.assertEqual([item["id"] for item in prepared["tasks"]], [task["id"]])

    def test_registering_from_a_worktree_reuses_the_primary_project(self):
        first = self.call(
            "project_register", root=str(self.repository), name="main", operation_id="main-project",
        )
        second = self.call(
            "project_register", root=str(self.worktree), name="worktree", operation_id="worktree-project",
        )

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.call("project_list")["items"]), 1)

    def test_codex_hook_accepts_worktree_context_and_edits(self):
        project = self.call(
            "project_register", root=str(self.repository), name="hook", operation_id="project",
        )
        task = self.call(
            "task_open", project_id=project["id"], title="hook task", goal="observe worktree",
            acceptance=["edit is recorded"], operation_id="task",
        )

        started = self.hook("SessionStart", source="startup")
        self.assertIn(task["id"], started["hookSpecificOutput"]["additionalContext"])

        self.hook(
            "PostToolUse",
            tool_name="apply_patch",
            tool_input={
                "command": "*** Begin Patch\n*** Add File: worktree_file.py\n+pass\n*** End Patch",
            },
            tool_response={"success": True},
        )
        stopped = self.hook("Stop", stop_hook_active=False)
        self.assertEqual(stopped.get("decision"), "block")
        self.assertIn("memweave_", stopped.get("reason", ""))

    def hook(self, event, **parameters):
        payload = {
            "session_id": "worktree-session",
            "cwd": str(self.worktree),
            "hook_event_name": event,
            "turn_id": "turn-1",
        }
        payload.update(parameters)
        completed = subprocess.run(
            [
                sys.executable, "-m", "memweave", "hook",
                "--client", "codex", "--event", event,
                "--root", str(self.repository), "--home", str(self.home),
            ],
            input=json.dumps(payload),
            capture_output=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"},
            cwd=self.base,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "", completed.stderr)
        return json.loads(completed.stdout)

    def test_doctor_reports_and_repairs_an_unconfigured_worktree(self):
        project = self.call(
            "project_register", root=str(self.repository), name="doctor", operation_id="project",
        )
        diagnosis = doctor_clients(
            self.home,
            roots=[self.repository],
            clients=("codex",),
            global_base=self.base / "global",
        )
        worktree_target = next(
            target for target in diagnosis["targets"]
            if target["root"] == str(self.worktree.resolve())
        )
        self.assertEqual(worktree_target["source"], "worktree")
        self.assertEqual(worktree_target["state"], "needs_attention")
        self.assertIn(
            "worktree_unconfigured",
            {issue["code"] for issue in worktree_target["issues"]},
        )

        repaired = doctor_clients(
            self.home,
            roots=[self.repository],
            clients=("codex",),
            fix=True,
            global_base=self.base / "global",
        )
        repaired_target = next(
            target for target in repaired["targets"]
            if target["root"] == str(self.worktree.resolve())
        )
        self.assertIn(repaired_target["state"], ("ready", "repaired"))
        self.assertTrue((self.worktree / ".codex" / "config.toml").is_file())
        self.assertTrue((self.worktree / ".codex" / "memweave.json").is_file())
        self.assertEqual(len(self.call("project_list")["items"]), 1)
        self.assertEqual(self.call("project_list")["items"][0]["id"], project["id"])


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from memweave.basic_memory_index import BasicMemoryIndex
from memweave.diagnostics import DiagnosticError, result, validate_text
from memweave.storage import Vault, contract_digest, digest, encode_record, now
from memweave.worktrees import primary_worktree_root, same_repository


TASK_OPEN_STATUSES = ("current", "queued")
TASK_STATUS_ALIASES = {"active": "current"}
TASK_HISTORY_STATUSES = ("current", "queued", "completed", "cancelled", "pending_review", "archived", "open", "history", "all")
WORK_UNIT_OPEN_STATUSES = ("pending", "running", "blocked")
WORK_UNIT_TRANSITIONS = {
    "pending": {"running", "cancelled"},
    "running": {"running", "blocked", "completed", "cancelled"},
    "blocked": {"blocked", "running", "completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


def text(value, field, limit=12000):
    validate_text(value, field, limit)
    return value.strip()


def optional_text(value, field, limit=12000):
    if not isinstance(value, str) or len(value) > limit:
        raise DiagnosticError("RECORD_INVALID", f"{field} must be text of at most {limit} characters.")
    return value.strip()


def task_order(task):
    rank = {"current": 0, "queued": 1, "completed": 2, "cancelled": 3}
    position = task.get("queue_position")
    if type(position) is not int or position < 0:
        position = 1_000_000
    return rank.get(task["status"], 9), position, task.get("created_at", ""), task["id"]


def task_lifecycle_status(task):
    status = TASK_STATUS_ALIASES.get(task.get("status"), task.get("status"))
    if task.get("archived_at") or status == "cancelled":
        return "archived"
    if status == "completed" and task.get("consolidation_status", "pending") == "reviewed":
        return "archived"
    if status == "completed":
        return "pending_review"
    return status


def task_matches_status(task, status):
    lifecycle = task_lifecycle_status(task)
    if status in (None, "open"):
        return lifecycle in ("current", "queued", "pending_review")
    if status == "history":
        return lifecycle == "archived"
    if status == "all":
        return True
    if status == "archived":
        return lifecycle == "archived"
    if status == "pending_review":
        return lifecycle == "pending_review"
    return task.get("status") == status


def task_summary(task):
    keys = (
        "id", "title", "status", "revision", "queue_position", "goal", "next_step",
        "findings", "progress", "failures", "verification", "consolidation_status",
        "consolidation_summary", "archived_at", "created_at", "updated_at",
    )
    summary = {key: task.get(key) for key in keys if key in task}
    summary["lifecycle"] = task_lifecycle_status(task)
    return summary


class Service:
    def __init__(self, home: Path):
        self.vault = Vault(home)
        self.index: BasicMemoryIndex | None = None
        self._index_error: str | None = None

    def initialize(self):
        self.vault.initialize()
        self._migrate_task_model()
        return self.status()

    def start(self):
        """Initialize storage and the one Basic Memory index for this process."""
        self.vault.initialize()
        self._migrate_task_model()
        if self.index is None:
            index = BasicMemoryIndex.acquire(self.vault.home)
            try:
                index.start()
            except Exception:
                BasicMemoryIndex.release(index)
                raise
            self.index = index
        else:
            self.index.start()
        self._index_error = None
        return self.status()

    def close(self):
        if self.index is not None:
            index = self.index
            self.index = None
            BasicMemoryIndex.release(index)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def _refresh_index(self, paths=(), removed=()):
        if self.index is None or not self.index.active:
            return
        try:
            if paths:
                self.index.sync(list(paths))
            if removed:
                self.index.remove(list(removed))
            self._index_error = None
        except Exception as error:  # Canonical Markdown has already been persisted.
            self._index_error = str(error)

    def _repair_index_if_needed(self):
        if self.index is None or not self.index.active:
            raise DiagnosticError(
                "INDEX_UNAVAILABLE",
                "MemWeave search index is not running; start the service before searching.",
            )
        if self._index_error is None:
            return
        try:
            self.index.sync_all()
        except Exception as error:
            raise DiagnosticError(
                "INDEX_UNAVAILABLE",
                "Basic Memory search index could not be rebuilt; inspect its local database.",
            ) from error
        self._index_error = None

    def _refresh_all_index(self):
        if self.index is None or not self.index.active:
            return
        try:
            self.index.sync_all()
            self._index_error = None
        except Exception as error:
            self._index_error = str(error)

    def dispatch(self, action: str, parameters: dict | None = None) -> dict:
        parameters = {} if parameters is None else parameters
        allowed = {
            "project_register", "project_list", "task_open", "task_read", "task_list",
            "task_checkpoint", "task_close", "memory_write", "memory_read", "memory_search",
            "work_unit_update", "task_phase_close",
            "memory_update", "memory_forget", "context_get", "project_scan", "graph_query",
            "graph_write", "graph_update", "graph_forget", "evidence_read", "recover", "rebuild", "status",
            "recovery_status", "recovery_resolve",
            "memory_review", "memory_review_complete", "memory_activate",
            "workspace_prepare",
        }
        if action not in allowed or not isinstance(parameters, dict):
            raise DiagnosticError("RECORD_INVALID", "Unknown action or invalid parameter object.")
        try:
            output = getattr(self, action)(**parameters)
        except TypeError as error:
            raise DiagnosticError("RECORD_INVALID", "Missing or invalid action parameters.") from error
        return result(output)

    def status(self, project_id=None):
        project = self._project(project_id) if project_id is not None else None
        output = self.vault.diagnostics.status()["data"]
        output["counts"] = self.vault.counts()
        with self.vault.diagnostics._connect() as connection:
            output["pending_operations"] = connection.execute(
                "SELECT COUNT(*) FROM operations WHERE state != 'completed'"
            ).fetchone()[0]
        output.update(phase="local_mvp", task_memory="available", long_term_memory="available",
                      project_graph="available")
        tasks = self.vault.list_record_metadata("task")
        task_counts = {
            "total": len(tasks),
            "current": sum(item["status"] == "current" for item in tasks),
            "queued": sum(item["status"] == "queued" for item in tasks),
            "pending_review": sum(task_lifecycle_status(item) == "pending_review" for item in tasks),
            "completed": sum(item["status"] == "completed" for item in tasks),
            "cancelled": sum(item["status"] == "cancelled" for item in tasks),
            "archived": sum(task_lifecycle_status(item) == "archived" for item in tasks),
        }
        output["task_queue"] = {
            "current": task_counts["current"],
            "queued": task_counts["queued"],
            "pending_review": task_counts["pending_review"],
            "archived": task_counts["archived"],
        }
        memories = self.vault.list_records("memory")
        output["knowledge"] = self._knowledge_counts(memories)
        architectures = self.vault.list_records("architecture")
        projects = self.vault.list_records("project")
        output["architecture"] = self._architecture_counts(architectures, projects)
        output["search_index"] = {
            "backend": "basic-memory-sqlite-fts5",
            "state": "active" if self.index is not None and self.index.active else "stopped",
            "last_sync": self.index.last_result if self.index is not None else {},
            "error": self._index_error,
        }
        if project is not None:
            project_id = project["id"]
            project_tasks = [task for task in tasks if task.get("project_id") == project_id]
            project_memories = [
                memory for memory in memories if memory.get("project_id") == project_id
            ]
            project_architectures = [
                record for record in architectures if record.get("project_id") == project_id
            ]
            output["project"] = {
                "id": project_id,
                "title": project.get("title", ""),
                "root": project.get("root", ""),
                "tasks": {
                    "total": len(project_tasks),
                    "current": sum(item["status"] == "current" for item in project_tasks),
                    "queued": sum(item["status"] == "queued" for item in project_tasks),
                    "pending_review": sum(
                        task_lifecycle_status(item) == "pending_review" for item in project_tasks
                    ),
                    "completed": sum(item["status"] == "completed" for item in project_tasks),
                    "cancelled": sum(item["status"] == "cancelled" for item in project_tasks),
                    "archived": sum(
                        task_lifecycle_status(item) == "archived" for item in project_tasks
                    ),
                },
                "knowledge": self._knowledge_counts(project_memories),
                "architecture": self._architecture_counts(project_architectures, [project]),
            }
        return output

    @staticmethod
    def _knowledge_counts(memories):
        by_scope = {scope: 0 for scope in ("project", "task", "user")}
        by_status = {status: 0 for status in ("active", "candidate")}
        by_scope_status = {
            scope: {status: 0 for status in ("active", "candidate")}
            for scope in ("project", "task", "user")
        }
        for memory in memories:
            scope, memory_status = memory.get("scope"), memory.get("status")
            if scope in by_scope:
                by_scope[scope] += 1
            if memory_status in by_status:
                by_status[memory_status] += 1
            if scope in by_scope_status and memory_status in by_scope_status[scope]:
                by_scope_status[scope][memory_status] += 1
        return {
            "total": len(memories),
            "by_scope": by_scope,
            "by_status": by_status,
            "by_scope_status": by_scope_status,
        }

    def _architecture_counts(self, architectures, projects):
        stale_ids = set()
        unverified_ids = set()
        project_errors = []
        known_projects = set()
        mapper = self._map()
        for project in projects:
            known_projects.add(project["id"])
            try:
                summary = mapper.architecture_status(project["id"])
            except DiagnosticError as error:
                project_errors.append({"project_id": project["id"], "error": error.code})
                unverified_ids.update(
                    record["id"] for record in architectures
                    if record.get("project_id") == project["id"]
                )
                continue
            stale_ids.update(summary["stale_record_ids"])
        for record in architectures:
            if record.get("project_id") not in known_projects:
                unverified_ids.add(record["id"])
        return {
            "total": len(architectures),
            "active": sum(record.get("status") == "active" for record in architectures),
            "stale": len(stale_ids),
            "stale_record_ids": sorted(stale_ids),
            "unverified": len(unverified_ids),
            "unverified_record_ids": sorted(unverified_ids),
            "project_errors": project_errors,
        }

    def _record(self, record_id, record_type):
        text(record_id, "record_id", 100)
        record = self.vault.read(record_id)
        if record["type"] != record_type:
            raise DiagnosticError("SCOPE_DENIED", "Record has a different type.")
        return record

    def _project(self, project_id):
        return self._record(project_id, "project")

    def _project_for_root(self, root):
        path = Path(root).expanduser().resolve()
        projects = self.vault.list_records("project")
        for project in projects:
            if Path(project["root"]).expanduser().resolve() == path:
                return project
        for project in projects:
            project_root = Path(project["root"]).expanduser().resolve()
            if same_repository(project_root, path):
                return project
        return None

    def _task_items(self, project_id):
        return sorted(
            self.vault.list_records("task", project_id=project_id),
            key=task_order,
        )

    def _task_metadata(self, project_id):
        return sorted(
            self.vault.list_record_metadata("task", project_id=project_id),
            key=task_order,
        )

    @staticmethod
    def _task_counts(tasks):
        return {
            "total": len(tasks),
            "current": sum(item["status"] == "current" for item in tasks),
            "queued": sum(item["status"] == "queued" for item in tasks),
            "pending_review": sum(
                task_lifecycle_status(item) == "pending_review" for item in tasks
            ),
            "completed": sum(item["status"] == "completed" for item in tasks),
            "cancelled": sum(item["status"] == "cancelled" for item in tasks),
            "archived": sum(task_lifecycle_status(item) == "archived" for item in tasks),
        }

    def _work_unit_items(self, task_id):
        task = self._record(task_id, "task")
        return sorted(
            (
                item for item in self.vault.list_records("work_unit", project_id=task["project_id"])
                if item["task_id"] == task_id
            ),
            key=lambda item: (item.get("created_at", ""), item["id"]),
        )

    def _phase_items(self, task_id):
        task = self._record(task_id, "task")
        return sorted(
            (
                item for item in self.vault.list_records("phase", project_id=task["project_id"])
                if item["task_id"] == task_id
            ),
            key=lambda item: (item.get("sequence", 0), item.get("created_at", ""), item["id"]),
        )

    def _checkpoint_metadata(self, task_id):
        task = self._record(task_id, "task")
        return sorted(
            (
                item for item in self.vault.list_record_metadata(
                    "checkpoint", project_id=task["project_id"]
                )
                if item["task_id"] == task_id
            ),
            key=lambda item: (item.get("sequence", 0), item.get("created_at", ""), item["id"]),
        )

    def _migrate_task_model(self):
        """Convert legacy active tasks to one current task and an ordered queue per project."""
        projects = self.vault.list_records("project")
        project_ids = {project["id"] for project in projects}
        project_ids.update(task["project_id"] for task in self.vault.list_records("task"))
        for project_id in sorted(project_ids):
            initial = self.vault.list_records("task", project_id=project_id)
            if not any(task["status"] in ("active", "current", "queued") for task in initial):
                continue

            def plan(current_tasks=None):
                tasks = self.vault.list_records("task", project_id=project_id)
                opened = [task for task in tasks if task["status"] in ("active", "current", "queued")]
                if not opened:
                    return [], {"migrated": 0, "current_task_id": None}
                current_candidates = sorted(
                    [task for task in opened if task["status"] == "current"], key=task_order
                )
                legacy_candidates = sorted(
                    [task for task in opened if task["status"] == "active"], key=task_order
                )
                fallback = current_candidates or legacy_candidates or sorted(opened, key=task_order)
                current = fallback[0]
                used = {
                    task["queue_position"] for task in opened
                    if type(task.get("queue_position")) is int and task["queue_position"] >= 0
                }
                next_position = max(used, default=-1) + 1
                positions = {}
                ordered = [current, *sorted(
                    (task for task in opened if task["id"] != current["id"]),
                    key=lambda item: (item.get("created_at", ""), item["id"]),
                )]
                for task in ordered:
                    position = task.get("queue_position")
                    if type(position) is not int or position < 0:
                        position = next_position
                        next_position += 1
                    positions[task["id"]] = position
                entries = []
                for task in opened:
                    desired_status = "current" if task["id"] == current["id"] else "queued"
                    desired_position = positions[task["id"]]
                    if task["status"] == desired_status and task.get("queue_position") == desired_position:
                        continue
                    updated = {
                        **task,
                        "status": desired_status,
                        "queue_position": desired_position,
                        "task_model_version": 2,
                        "revision": task["revision"] + 1,
                        "updated_at": now(),
                    }
                    entries.append({
                        "record": updated,
                        "path": task["path"],
                        "expected": task["etag"],
                    })
                return entries, {
                    "migrated": len(entries),
                    "current_task_id": current["id"],
                }

            entries, response = plan()
            if not entries:
                continue
            snapshot = [
                {
                    "id": task["id"], "revision": task["revision"], "status": task["status"],
                    "queue_position": task.get("queue_position"),
                }
                for task in sorted(initial, key=task_order)
            ]
            request = {
                "action": "migrate-task-model-v2", "project_id": project_id,
                "snapshot": snapshot,
            }
            operation_id = "task-model-v2-" + hashlib.sha256(
                json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:24]
            self.vault.commit(operation_id, request, plan)

    def _create(self, operation_id, request, record_type, scope, project_id, title, body,
                record_status="active", **metadata):
        def build():
            record_id = record_type + "_" + uuid4().hex
            record = dict(id=record_id, type=record_type, scope=scope, project_id=project_id,
                          title=title, body=body, revision=1, status=record_status,
                          created_at=now(), updated_at=now(), **metadata)
            directory = {
                "project": "projects", "task": "tasks", "work_unit": "work_units",
                "phase": "phases", "memory": "knowledge",
            }[record_type]
            response = {"id": record_id, "revision": 1, "status": record_status,
                        "etag": digest(encode_record(record).encode("utf-8"))}
            return [{"record": record, "path": f"{directory}/{record_id}.md", "expected": None}], response
        response = self.vault.commit(operation_id, request, build)
        directory = {
            "project": "projects", "task": "tasks", "work_unit": "work_units",
            "phase": "phases", "memory": "knowledge",
        }[record_type]
        self._refresh_index([f"{directory}/{response['id']}.md"])
        return response

    def _update(self, action, record_id, record_type, expected_revision, expected_etag, operation_id, changes):
        if type(expected_revision) is not int or expected_revision < 1:
            raise DiagnosticError("RECORD_INVALID", "expected_revision must be a positive integer.")
        text(expected_etag, "expected_etag", 64)
        request = dict(action=action, id=record_id, expected_revision=expected_revision,
                       expected_etag=expected_etag, changes=changes)
        def build():
            old = self._record(record_id, record_type)
            if old["revision"] != expected_revision or old["etag"] != expected_etag:
                raise DiagnosticError("VERSION_CONFLICT", "Read the latest revision and etag before writing.")
            if record_type == "task" and action == "task_checkpoint":
                if old["status"] != "current":
                    raise DiagnosticError("TASK_NOT_CURRENT", "Only the current task can receive checkpoints.")
            if action == "memory_activate" and old.get("status") != "candidate":
                raise DiagnosticError("VERSION_CONFLICT", "Only candidate knowledge can be activated.")
            if action == "memory_review_complete":
                if old["status"] != "completed":
                    raise DiagnosticError("RECORD_INVALID", "Consolidation review requires a completed task.")
                for memory_id in changes["consolidation_memory_ids"]:
                    memory = self.memory_read(memory_id)
                    if memory.get("project_id") != old["project_id"] or (
                        memory["scope"] == "task" and memory.get("task_id") != record_id
                    ):
                        raise DiagnosticError("SCOPE_DENIED", "Reviewed knowledge must belong to this project and task.")
            updated = {**old, **changes, "revision": old["revision"] + 1, "updated_at": now()}
            if action == "task_checkpoint":
                updated["body"] = old["body"] + (
                    f"\n\n## Checkpoint {updated['revision']} · {updated['updated_at']}\n"
                    f"\n### Findings\n{changes['findings']}\n\n### Progress\n{changes['progress']}"
                    f"\n\n### Failures\n{changes['failures']}"
                    f"\n\n### Verification\n{changes['verification']}"
                    f"\n\n### Next step\n{changes['next_step']}\n"
                )
            response = dict(id=record_id, revision=updated["revision"],
                            etag=digest(encode_record(updated).encode("utf-8")))
            return [{"record": updated, "path": old["path"], "expected": old["etag"]}], response
        response = self.vault.commit(operation_id, request, build)
        self._refresh_index([self._record(record_id, record_type)["path"]])
        return response

    def project_register(self, root, name, operation_id):
        root_path = Path(text(root, "root", 4096)).expanduser().resolve()
        name = text(name, "name", 200)
        if not root_path.is_dir():
            raise DiagnosticError("RECORD_INVALID", "Project root must be an existing directory.")
        registered_root = primary_worktree_root(root_path)
        request = dict(action="project_register", root=str(root_path), name=name)
        def build():
            projects = self.vault.list_records("project")
            for project in projects:
                if Path(project["root"]).expanduser().resolve() in (root_path, registered_root):
                    return [], {"id": project["id"], "revision": project["revision"], "etag": project["etag"]}
            for project in projects:
                if same_repository(Path(project["root"]), root_path):
                    return [], {"id": project["id"], "revision": project["revision"], "etag": project["etag"]}
            record_id = "project_" + uuid4().hex
            record = dict(id=record_id, type="project", scope="project", project_id=record_id,
                          root=str(registered_root), title=registered_root.name if root_path != registered_root else name,
                          name=registered_root.name if root_path != registered_root else name,
                          body=f"# {registered_root.name}\n\n{registered_root}\n",
                          status="active", revision=1, created_at=now(), updated_at=now())
            return [{"record": record, "path": f"projects/{record_id}.md", "expected": None}], {
                "id": record_id, "revision": 1, "etag": digest(encode_record(record).encode("utf-8"))
            }
        response = self.vault.commit(operation_id, request, build)
        self._refresh_index([f"projects/{response['id']}.md"])
        return response

    def project_list(self):
        return {"items": self.vault.list_records("project")}

    def workspace_prepare(self, root, intent="inspect", task_id=None, query="", include_user=False, budget=12000):
        path = Path(text(root, "root", 4096)).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise DiagnosticError("RECORD_INVALID", "root must be an existing absolute project directory.")
        if intent not in ("inspect", "new", "resume") or (task_id is not None and intent != "resume"):
            raise DiagnosticError("RECORD_INVALID", "Use inspect/new/resume; task_id requires resume intent.")
        if task_id is not None:
            text(task_id, "task_id", 100)
        if not isinstance(query, str) or len(query) > 1000 or type(include_user) is not bool or (
            type(budget) is not int or not 500 <= budget <= 32000
        ):
            raise DiagnosticError("RECORD_INVALID", "Invalid query, include_user or context budget.")
        path = path.resolve()
        project = self._project_for_root(path)
        prepared = dict(
            state="registration_required", project=None, tasks=[], pending_consolidation=[],
            pending_phases=[], work_units=[], context=None, task_id=None, tasks_truncated=False,
            pending_truncated=False, phases_truncated=False, work_units_truncated=False,
            attention=[], completion=None, execution_root=str(path), registered_root=None,
            is_worktree=False,
        )
        if project is None:
            return prepared
        registered_root = Path(project["root"]).expanduser().resolve()
        prepared["registered_root"] = str(registered_root)
        prepared["is_worktree"] = registered_root != path
        prepared["project"] = {
            "id": project["id"], "title": project["title"], "root": project["root"],
            "execution_root": str(path), "is_worktree": registered_root != path,
        }
        tasks = self._task_metadata(project["id"])
        opened = [item for item in tasks if item["status"] in TASK_OPEN_STATUSES]
        current = [item for item in opened if item["status"] == "current"]
        pending = [
            item for item in tasks if task_lifecycle_status(item) == "pending_review"
        ]
        archived = [
            item for item in tasks if task_lifecycle_status(item) == "archived"
        ]
        recent_history = sorted(
            archived,
            key=lambda item: (item.get("updated_at", ""), item.get("created_at", ""), item["id"]),
            reverse=True,
        )[:5]
        phases = sorted(
            (
                item for item in self.vault.list_record_metadata("phase", project_id=project["id"])
                if item.get("consolidation_status", "pending") == "pending"
            ),
            key=lambda item: (item.get("created_at", ""), item["id"]),
        )
        opened_ids = {item["id"] for item in opened}
        work_units = sorted(
            (
                item for item in self.vault.list_record_metadata("work_unit", project_id=project["id"])
                if item["task_id"] in opened_ids and item["status"] in WORK_UNIT_OPEN_STATUSES
            ),
            key=lambda item: (item.get("created_at", ""), item["id"]),
        )
        fields = ("id", "title", "status", "revision", "etag", "queue_position")
        task_items = [
            {
                **{key: item.get(key) for key in fields if key != "etag"},
                "etag": self.vault.file_etag(item["path"]),
                "lifecycle": task_lifecycle_status(item),
            }
            for item in opened[:20]
        ]
        pending_items = [
            {
                **{key: item.get(key) for key in fields if key != "etag"},
                "etag": self.vault.file_etag(item["path"]),
                "lifecycle": task_lifecycle_status(item),
            }
            for item in pending[:20]
        ]
        prepared.update(
            state="ready",
            tasks=task_items,
            pending_consolidation=pending_items,
            pending_phases=[
                {
                    "id": item["id"], "task_id": item["task_id"], "title": item["title"],
                    "status": item["status"], "revision": item["revision"],
                    "etag": self.vault.file_etag(item["path"]),
                    "consolidation_status": item.get("consolidation_status", "pending"),
                }
                for item in phases[:20]
            ],
            work_units=[
                {
                    "id": item["id"], "task_id": item["task_id"], "title": item["title"],
                    "owner": item["owner"], "status": item["status"],
                    "revision": item["revision"],
                    "etag": self.vault.file_etag(item["path"]),
                    "blocker": item.get("blocker", ""), "next_step": item.get("next_step", ""),
                }
                for item in work_units[:20]
            ],
            tasks_truncated=len(opened) > 20, pending_truncated=len(pending) > 20,
            phases_truncated=len(phases) > 20, work_units_truncated=len(work_units) > 20,
            history=[task_summary(item) for item in recent_history],
            history_truncated=len(archived) > 5,
            task_counts=self._task_counts(tasks),
        )
        prepared_attention = []
        if pending or phases:
            prepared_attention.append({
                "code": "pending_consolidation",
                "severity": "info",
                "message": (
                    f"{len(pending)} completed task(s) and {len(phases)} "
                    "phase(s) await knowledge review."
                ),
                "task_count": len(pending),
                "phase_count": len(phases),
            })
        blocked_units = [item for item in work_units if item["status"] == "blocked"]
        if blocked_units:
            prepared_attention.append({
                "code": "blocked_work_units",
                "severity": "warning",
                "message": f"{len(blocked_units)} WorkUnit(s) are blocked and need an explicit decision.",
                "unit_ids": [item["id"] for item in blocked_units],
            })
        prepared["attention"] = prepared_attention
        selected = None
        if intent == "resume":
            if task_id is not None:
                selected = self.task_read(task_id)
                if selected["project_id"] != project["id"]:
                    raise DiagnosticError("SCOPE_DENIED", "Task belongs to a different project.")
                if selected["status"] != "current":
                    raise DiagnosticError("TASK_NOT_CURRENT", "Only the current task can be resumed.")
            elif len(current) == 1:
                selected = current[0]
            elif len(current) > 1:
                raise DiagnosticError("TASK_STATE_INVALID", "More than one current task needs migration.")
            else:
                prepared["state"] = "no_active_task"
                return prepared
        if intent != "inspect":
            prepared["task_id"] = selected["id"] if selected else None
            context = self.context_get(
                project["id"], prepared["task_id"], query, include_user, budget,
                execution_root=str(path),
            )
            prepared["context"] = context
            prepared["attention"] = context.get("attention", prepared_attention)
            prepared["completion"] = context.get("completion")
        return prepared

    def task_open(self, project_id, title, goal, acceptance, operation_id):
        self._project(project_id)
        title, goal = text(title, "title", 200), text(goal, "goal")
        if not isinstance(acceptance, list) or not 1 <= len(acceptance) <= 30:
            raise DiagnosticError("RECORD_INVALID", "acceptance needs 1–30 distinct criteria.")
        acceptance = [text(item, "criterion", 1000) for item in acceptance]
        if len(set(acceptance)) != len(acceptance):
            raise DiagnosticError("RECORD_INVALID", "Acceptance criteria must be distinct.")
        request = dict(action="task_open", project_id=project_id, title=title, goal=goal, acceptance=acceptance)
        body = (
            f"# {title}\n\n## Goal\n{goal}\n\n## Acceptance\n"
            + "\n".join(f"- {item}" for item in acceptance)
            + "\n\n## Planning discipline\n"
            "- WorkUnits are the durable, parallel execution plan for this task.\n"
            "- Keep findings, failures, verification and the single next step current.\n"
            "- Never repeat an unchanged failing action; after three failures, escalate.\n"
            "- Close a phase when its evidence is complete without closing the whole task.\n"
        )

        def build():
            tasks = self._task_metadata(project_id)
            opened = [
                task for task in tasks
                if task["status"] in ("active", "current", "queued")
            ]
            current = sorted(
                [task for task in opened if task["status"] in ("active", "current")],
                key=task_order,
            )
            if len(current) > 1:
                raise DiagnosticError("TASK_STATE_INVALID", "More than one current task needs migration.")
            queued = [task for task in opened if task["status"] == "queued"]
            positions = [
                task["queue_position"] for task in opened
                if type(task.get("queue_position")) is int and task["queue_position"] >= 0
            ]
            task_id = "task_" + uuid4().hex
            timestamp = now()
            new_status = "queued" if current or queued else "current"
            position = max(positions, default=-1) + 1
            record = {
                "id": task_id, "type": "task", "scope": "project", "project_id": project_id,
                "title": title, "body": body, "revision": 1, "status": new_status,
                "created_at": timestamp, "updated_at": timestamp, "goal": goal,
                "acceptance": acceptance, "findings": "", "progress": "", "failures": "",
                "verification": "", "next_step": "", "queue_position": position,
                "contract_hash": contract_digest(goal, acceptance),
                "contract_attested_at": timestamp,
                "task_model_version": 2,
            }
            entries = [{
                "record": record, "path": f"tasks/{task_id}.md", "expected": None,
            }]
            promoted_task_id = None
            if not current and queued:
                promoted_meta = sorted(queued, key=task_order)[0]
                promoted = self._record(promoted_meta["id"], "task")
                promoted_record = {
                    **promoted, "status": "current", "revision": promoted["revision"] + 1,
                    "updated_at": timestamp,
                }
                entries.append({
                    "record": promoted_record, "path": promoted["path"], "expected": promoted["etag"],
                })
                promoted_task_id = promoted["id"]
            response = {
                "id": task_id, "revision": 1,
                "etag": digest(encode_record(record).encode("utf-8")),
                "status": new_status, "queue_position": position,
                "promoted_task_id": promoted_task_id,
            }
            return entries, response

        response = self.vault.commit(operation_id, request, build)
        self._refresh_all_index()
        return response

    def task_read(
        self, task_id, checkpoint_offset=0, checkpoint_limit=10,
        phase_offset=0, phase_limit=20, work_unit_limit=30,
    ):
        for value, field, maximum in (
            (checkpoint_offset, "checkpoint_offset", 100000),
            (phase_offset, "phase_offset", 100000),
            (work_unit_limit, "work_unit_limit", 100),
        ):
            if type(value) is not int or not 0 <= value <= maximum:
                raise DiagnosticError("RECORD_INVALID", f"{field} must be an integer in range.")
        if type(checkpoint_limit) is not int or not 1 <= checkpoint_limit <= 100:
            raise DiagnosticError("RECORD_INVALID", "checkpoint_limit must be between 1 and 100.")
        if type(phase_limit) is not int or not 1 <= phase_limit <= 100:
            raise DiagnosticError("RECORD_INVALID", "phase_limit must be between 1 and 100.")
        task = self._record(task_id, "task")
        units = self._work_unit_items(task_id)
        open_units = [
            unit for unit in units if unit["status"] in WORK_UNIT_OPEN_STATUSES
        ]
        recent_units = sorted(
            (unit for unit in units if unit not in open_units),
            key=lambda unit: (unit.get("updated_at", ""), unit["id"]),
            reverse=True,
        )
        selected_units = open_units[:work_unit_limit]
        selected_units.extend(recent_units[:max(0, work_unit_limit - len(selected_units))])
        selected_units.sort(key=lambda unit: (unit.get("created_at", ""), unit["id"]))
        task["work_units"] = [
            {
                key: unit.get(key)
                for key in (
                    "id", "title", "owner", "status", "revision", "etag", "findings",
                    "progress", "failures", "blocker", "verification", "next_step",
                    "created_at", "updated_at",
                )
            }
            for unit in selected_units
        ]
        task["work_unit_total"] = len(units)
        task["work_units_truncated"] = len(units) > len(selected_units)
        phases = self._phase_items(task_id)
        phase_end = max(0, len(phases) - phase_offset)
        phase_start = max(0, phase_end - phase_limit)
        task["phases"] = [
            {
                key: phase.get(key)
                for key in (
                    "id", "title", "status", "sequence", "revision", "etag",
                    "consolidation_status", "summary", "findings", "verification",
                    "next_step", "created_at", "updated_at",
                )
            }
            for phase in phases[phase_start:phase_end]
        ]
        task["phase_total"] = len(phases)
        task["phases_truncated"] = len(task["phases"]) < len(phases)
        task["next_phase_offset"] = (
            phase_offset + len(task["phases"]) if phase_start > 0 else None
        )
        checkpoints = self._checkpoint_metadata(task_id)
        checkpoint_end = max(0, len(checkpoints) - checkpoint_offset)
        checkpoint_start = max(0, checkpoint_end - checkpoint_limit)
        selected_checkpoints = checkpoints[checkpoint_start:checkpoint_end]
        task["checkpoints"] = [
            {
                key: checkpoint.get(key)
                for key in (
                    "id", "title", "sequence", "revision", "status", "findings",
                    "progress", "failures", "verification", "next_step",
                    "created_at", "updated_at",
                )
                if key in checkpoint
            }
            for checkpoint in selected_checkpoints
        ]
        for checkpoint, metadata in zip(task["checkpoints"], selected_checkpoints):
            checkpoint["etag"] = self.vault.file_etag(metadata["path"])
        task["checkpoint_total"] = len(checkpoints)
        task["checkpoints_truncated"] = len(task["checkpoints"]) < len(checkpoints)
        task["next_checkpoint_offset"] = (
            checkpoint_offset + len(task["checkpoints"]) if checkpoint_start > 0 else None
        )
        return task

    def task_list(self, project_id, status=None, query="", limit=20, offset=0):
        self._project(project_id)
        status = TASK_STATUS_ALIASES.get(status, status)
        if status not in (None, *TASK_HISTORY_STATUSES):
            raise DiagnosticError("RECORD_INVALID", "Invalid task status.")
        if not isinstance(query, str) or len(query) > 500:
            raise DiagnosticError("RECORD_INVALID", "query must contain at most 500 characters.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DiagnosticError("RECORD_INVALID", "limit must be between 1 and 100.")
        if type(offset) is not int or not 0 <= offset <= 1000000:
            raise DiagnosticError("RECORD_INVALID", "offset must be a non-negative integer.")
        effective_status = "open" if status is None else status
        tasks = self._task_metadata(project_id)
        needle = query.strip().casefold()
        matched = []
        for task in tasks:
            if not task_matches_status(task, status):
                continue
            searchable = " ".join(
                str(task.get(key, ""))
                for key in (
                    "title", "goal", "next_step", "findings", "progress",
                    "failures", "verification", "consolidation_summary",
                )
            ).casefold()
            if needle and needle not in searchable:
                continue
            matched.append(task)
        if status in ("archived", "history"):
            matched.sort(
                key=lambda item: (
                    item.get("updated_at", ""), item.get("created_at", ""), item["id"]
                ),
                reverse=True,
            )
        else:
            matched.sort(key=task_order)
        page = matched[offset:offset + limit]
        items = []
        for task in page:
            item = task_summary(task)
            item["path"] = task["path"]
            item["etag"] = self.vault.file_etag(task["path"])
            items.append(item)
        next_offset = offset + len(items)
        return {
            "items": items,
            "total": len(matched),
            "all_total": len(tasks),
            "offset": offset,
            "limit": limit,
            "has_more": next_offset < len(matched),
            "next_offset": next_offset if next_offset < len(matched) else None,
            "counts": self._task_counts(tasks),
            "status": effective_status,
            "query": query,
        }

    def task_checkpoint(
        self, task_id, expected_revision, expected_etag, findings, progress, next_step,
        operation_id, failures="", verification="", attest_contract=False,
    ):
        if type(attest_contract) is not bool:
            raise DiagnosticError("RECORD_INVALID", "attest_contract must be boolean.")
        changes = dict(
            findings=optional_text(findings, "findings"),
            progress=text(progress, "progress"),
            failures=optional_text(failures, "failures"),
            verification=optional_text(verification, "verification"),
            next_step=text(next_step, "next_step"),
        )
        if type(expected_revision) is not int or expected_revision < 1:
            raise DiagnosticError("RECORD_INVALID", "expected_revision must be a positive integer.")
        text(expected_etag, "expected_etag", 64)
        request = dict(
            action="task_checkpoint", id=task_id, expected_revision=expected_revision,
            expected_etag=expected_etag, changes=changes, attest_contract=attest_contract,
        )

        def build():
            old = self._record(task_id, "task")
            if old["revision"] != expected_revision or old["etag"] != expected_etag:
                raise DiagnosticError("VERSION_CONFLICT", "Read the latest revision and etag before writing.")
            if old["status"] != "current":
                raise DiagnosticError("TASK_NOT_CURRENT", "Only the current task can receive checkpoints.")
            timestamp = now()
            sequence = old.get("checkpoint_count", 0) + 1
            checkpoint_id = "checkpoint_" + uuid4().hex
            checkpoint_title = f"Checkpoint {sequence}: {old['title']}"[:200]
            checkpoint_body = (
                f"# {checkpoint_title}\n\n## Findings\n{changes['findings']}"
                f"\n\n## Progress\n{changes['progress']}"
                f"\n\n## Failures\n{changes['failures']}"
                f"\n\n## Verification\n{changes['verification']}"
                f"\n\n## Next step\n{changes['next_step']}\n"
            )
            checkpoint = {
                "id": checkpoint_id, "type": "checkpoint", "scope": "task",
                "project_id": old["project_id"], "task_id": task_id,
                "title": checkpoint_title, "sequence": sequence, "status": "recorded",
                "body": checkpoint_body, "revision": 1, "created_at": timestamp,
                "updated_at": timestamp, **changes,
            }
            applied = dict(changes)
            if attest_contract:
                applied["contract_hash"] = contract_digest(
                    old.get("goal", ""), old.get("acceptance", [])
                )
                applied["contract_attested_at"] = timestamp
            updated = {
                **old, **applied, "checkpoint_count": sequence,
                "latest_checkpoint_id": checkpoint_id,
                "latest_checkpoint_at": timestamp,
                "revision": old["revision"] + 1, "updated_at": timestamp,
            }
            entries = [
                {
                    "record": checkpoint, "path": f"checkpoints/{checkpoint_id}.md",
                    "expected": None,
                },
                {"record": updated, "path": old["path"], "expected": old["etag"]},
            ]
            return entries, {
                "id": task_id, "revision": updated["revision"],
                "etag": digest(encode_record(updated).encode("utf-8")),
                "checkpoint_id": checkpoint_id, "checkpoint_revision": 1,
                "checkpoint_etag": digest(encode_record(checkpoint).encode("utf-8")),
                "checkpoint_sequence": sequence,
            }

        response = self.vault.commit(operation_id, request, build)
        self._refresh_index([
            f"tasks/{task_id}.md", f"checkpoints/{response['checkpoint_id']}.md"
        ])
        return response

    def task_close(
        self, task_id, expected_revision, expected_etag, outcome, summary,
        acceptance_results, operation_id, abandon_work_units=False,
    ):
        if outcome not in ("completed", "cancelled") or not isinstance(acceptance_results, dict):
            raise DiagnosticError("RECORD_INVALID", "Use completed/cancelled and an acceptance result object.")
        if len(str(acceptance_results)) > 30000:
            raise DiagnosticError("RECORD_INVALID", "Acceptance results exceed the limit.")
        if type(abandon_work_units) is not bool:
            raise DiagnosticError("RECORD_INVALID", "abandon_work_units must be boolean.")
        summary = text(summary, "summary")
        if type(expected_revision) is not int or expected_revision < 1:
            raise DiagnosticError("RECORD_INVALID", "expected_revision must be a positive integer.")
        text(expected_etag, "expected_etag", 64)
        request = dict(
            action="task_close", id=task_id, expected_revision=expected_revision,
            expected_etag=expected_etag, outcome=outcome, summary=summary,
            acceptance_results=acceptance_results, abandon_work_units=abandon_work_units,
        )
        abandoned = []

        def build():
            old = self._record(task_id, "task")
            if old["revision"] != expected_revision or old["etag"] != expected_etag:
                raise DiagnosticError("VERSION_CONFLICT", "Read the latest revision and etag before writing.")
            old_status = "current" if old["status"] == "active" else old["status"]
            if old_status in ("completed", "cancelled"):
                raise DiagnosticError("TASK_CLOSED", "A closed task cannot be closed again.")
            if old_status == "queued" and outcome == "completed":
                raise DiagnosticError("TASK_NOT_CURRENT", "Only the current task can complete.")
            if outcome == "completed" and (
                set(acceptance_results) != set(old["acceptance"])
                or any(not isinstance(value, str) or not value.strip()
                       for value in acceptance_results.values())
            ):
                raise DiagnosticError("ACCEPTANCE_REQUIRED", "Explain the outcome of every acceptance criterion.")
            units = self._work_unit_items(task_id)
            open_units = [unit for unit in units if unit["status"] in WORK_UNIT_OPEN_STATUSES]
            if open_units and not abandon_work_units:
                raise DiagnosticError(
                    "WORK_UNITS_ACTIVE",
                    "Resolve pending/running/blocked WorkUnits or pass abandon_work_units=true.",
                )
            timestamp = now()
            updated = {
                **old, "status": outcome, "summary": summary,
                "acceptance_results": acceptance_results, "verification": "host_reported",
                "consolidation_status": "pending" if outcome == "completed" else "not_required",
                "revision": old["revision"] + 1, "updated_at": timestamp,
            }
            if outcome == "cancelled":
                updated["archived_at"] = timestamp
            updated["body"] = old["body"] + f"\n\n## {outcome}\n{summary}\n"
            entries = [{
                "record": updated, "path": old["path"], "expected": old["etag"],
            }]
            abandoned.clear()
            if open_units:
                for unit in open_units:
                    cancelled = {
                        **unit, "status": "cancelled", "blocker": "",
                        "revision": unit["revision"] + 1, "updated_at": timestamp,
                    }
                    cancelled["body"] = unit["body"] + (
                        f"\n\n## Cancelled with parent task\n{summary}\n"
                    )
                    entries.append({
                        "record": cancelled, "path": unit["path"], "expected": unit["etag"],
                    })
                    abandoned.append(unit["id"])
            promoted_task_id = None
            if old_status == "current":
                queued = [
                    task for task in self._task_metadata(old["project_id"])
                    if task["status"] == "queued"
                ]
                if queued:
                    promoted_meta = sorted(queued, key=task_order)[0]
                    promoted = self._record(promoted_meta["id"], "task")
                    promoted_record = {
                        **promoted, "status": "current",
                        "revision": promoted["revision"] + 1, "updated_at": timestamp,
                    }
                    entries.append({
                        "record": promoted_record, "path": promoted["path"],
                        "expected": promoted["etag"],
                    })
                    promoted_task_id = promoted["id"]
            response = {
                "id": task_id, "revision": updated["revision"],
                "etag": digest(encode_record(updated).encode("utf-8")),
                "status": outcome, "promoted_task_id": promoted_task_id,
                "abandoned_work_units": list(abandoned),
            }
            return entries, response

        response = self.vault.commit(operation_id, request, build)
        self._refresh_all_index()
        return response

    def work_unit_update(
        self, status, operation_id, task_id=None, title=None, owner=None, unit_id=None,
        findings="", progress="", failures="", blocker="", verification="", next_step="",
        expected_revision=None, expected_etag=None,
    ):
        if status not in ("pending", "running", "blocked", "completed", "cancelled"):
            raise DiagnosticError("RECORD_INVALID", "Invalid WorkUnit status.")
        fields = {
            "findings": optional_text(findings, "findings"),
            "progress": optional_text(progress, "progress"),
            "failures": optional_text(failures, "failures"),
            "blocker": optional_text(blocker, "blocker"),
            "verification": optional_text(verification, "verification"),
            "next_step": optional_text(next_step, "next_step"),
        }
        if unit_id is None:
            if task_id is None:
                raise DiagnosticError("RECORD_INVALID", "task_id is required when creating a WorkUnit.")
            task_id = text(task_id, "task_id", 100)
            title = text(title, "title", 200)
            owner = text(owner, "owner", 200)
            if status not in ("pending", "running"):
                raise DiagnosticError("RECORD_INVALID", "A new WorkUnit must be pending or running.")
            if not (status == "pending" or fields["next_step"] or fields["progress"]):
                raise DiagnosticError("RECORD_INVALID", "A running WorkUnit needs progress or a next step.")
            if fields["blocker"]:
                raise DiagnosticError("RECORD_INVALID", "A new WorkUnit cannot start blocked.")
            if expected_revision is not None or expected_etag is not None:
                raise DiagnosticError("RECORD_INVALID", "New WorkUnits cannot carry an expected revision.")
            request = dict(
                action="work_unit_create", task_id=task_id, title=title, owner=owner,
                status=status, **fields,
            )

            def build():
                task = self._record(task_id, "task")
                if task["status"] != "current":
                    raise DiagnosticError("TASK_NOT_CURRENT", "WorkUnits require the current task.")
                unit_id_generated = "work_unit_" + uuid4().hex
                timestamp = now()
                body = (
                    f"# {title}\n\n## Owner\n{owner}\n\n## Parent task\n{task_id}\n"
                    f"\n## Updates\n### {timestamp} · {status}\n"
                    f"{fields['findings']}\n\n{fields['progress']}\n\n"
                    f"{fields['failures']}\n\n{fields['blocker']}\n\n"
                    f"{fields['verification']}\n\n{fields['next_step']}\n"
                )
                record = {
                    "id": unit_id_generated, "type": "work_unit", "scope": "task",
                    "project_id": task["project_id"], "task_id": task_id, "title": title,
                    "owner": owner, "status": status, "body": body, "revision": 1,
                    "created_at": timestamp, "updated_at": timestamp, **fields,
                }
                return [{
                    "record": record, "path": f"work_units/{unit_id_generated}.md", "expected": None,
                }], {
                    "id": unit_id_generated, "task_id": task_id, "title": title, "owner": owner,
                    "status": status, "revision": 1,
                    "etag": digest(encode_record(record).encode("utf-8")),
                }

            result = self.vault.commit(operation_id, request, build)
            self._refresh_index([f"work_units/{result['id']}.md"])
            return result

        text(unit_id, "unit_id", 100)
        if type(expected_revision) is not int or expected_revision < 1:
            raise DiagnosticError("RECORD_INVALID", "expected_revision must be a positive integer.")
        text(expected_etag, "expected_etag", 64)
        if title is not None or owner is not None:
            raise DiagnosticError("RECORD_INVALID", "WorkUnit title and owner are immutable after creation.")
        if status == "blocked" and not fields["blocker"]:
            raise DiagnosticError("RECORD_INVALID", "A blocked WorkUnit needs a blocker.")
        request = dict(
            action="work_unit_update", unit_id=unit_id, expected_revision=expected_revision,
            expected_etag=expected_etag, status=status, **fields,
        )

        def build():
            unit = self._record(unit_id, "work_unit")
            if task_id is not None and task_id != unit["task_id"]:
                raise DiagnosticError("SCOPE_DENIED", "WorkUnit belongs to a different task.")
            task = self._record(unit["task_id"], "task")
            if task["status"] != "current":
                raise DiagnosticError("TASK_NOT_CURRENT", "Only WorkUnits of the current task can change.")
            if unit["revision"] != expected_revision or unit["etag"] != expected_etag:
                raise DiagnosticError("VERSION_CONFLICT", "Read the latest WorkUnit revision and etag first.")
            if status not in WORK_UNIT_TRANSITIONS[unit["status"]]:
                raise DiagnosticError(
                    "WORK_UNIT_STATE_INVALID",
                    f"Cannot move a WorkUnit from {unit['status']} to {status}.",
                )
            timestamp = now()
            updated = {
                **unit, **fields, "status": status, "revision": unit["revision"] + 1,
                "updated_at": timestamp,
            }
            updated["body"] = unit["body"] + (
                f"\n\n### {timestamp} · {status}\n{fields['findings']}\n\n{fields['progress']}"
                f"\n\n{fields['failures']}\n\n{fields['blocker']}\n\n"
                f"{fields['verification']}\n\n{fields['next_step']}\n"
            )
            return [{
                "record": updated, "path": unit["path"], "expected": unit["etag"],
            }], {
                "id": unit_id, "task_id": unit["task_id"], "title": unit["title"],
                "owner": unit["owner"], "status": status, "revision": updated["revision"],
                "etag": digest(encode_record(updated).encode("utf-8")),
            }

        result = self.vault.commit(operation_id, request, build)
        self._refresh_index([f"work_units/{result['id']}.md"])
        return result

    def task_phase_close(
        self, task_id, expected_revision, expected_etag, title, summary, next_step,
        operation_id, findings="", verification="", abandon_work_units=False,
    ):
        if type(expected_revision) is not int or expected_revision < 1:
            raise DiagnosticError("RECORD_INVALID", "expected_revision must be a positive integer.")
        text(expected_etag, "expected_etag", 64)
        title = text(title, "title", 200)
        summary = text(summary, "summary")
        next_step = text(next_step, "next_step")
        findings = optional_text(findings, "findings")
        verification = optional_text(verification, "verification")
        if type(abandon_work_units) is not bool:
            raise DiagnosticError("RECORD_INVALID", "abandon_work_units must be boolean.")
        request = dict(
            action="task_phase_close", task_id=task_id, expected_revision=expected_revision,
            expected_etag=expected_etag, title=title, summary=summary, next_step=next_step,
            findings=findings, verification=verification, abandon_work_units=abandon_work_units,
        )
        abandoned = []

        def build():
            task = self._record(task_id, "task")
            if task["status"] != "current":
                raise DiagnosticError("TASK_NOT_CURRENT", "Only the current task can close a phase.")
            if task["revision"] != expected_revision or task["etag"] != expected_etag:
                raise DiagnosticError("VERSION_CONFLICT", "Read the latest task revision and etag first.")
            units = self._work_unit_items(task_id)
            open_units = [unit for unit in units if unit["status"] in WORK_UNIT_OPEN_STATUSES]
            if open_units and not abandon_work_units:
                raise DiagnosticError(
                    "WORK_UNITS_ACTIVE",
                    "Resolve WorkUnits or pass abandon_work_units=true before closing the phase.",
                )
            timestamp = now()
            phase_id = "phase_" + uuid4().hex
            stored_count = task.get("phase_count")
            sequence = (
                stored_count + 1
                if type(stored_count) is int and stored_count >= 0
                else max(
                    (phase.get("sequence", 0) for phase in self._phase_items(task_id)),
                    default=0,
                ) + 1
            )
            body = (
                f"# {title}\n\n## Summary\n{summary}\n\n## Findings\n{findings}"
                f"\n\n## Verification\n{verification}\n\n## Next step\n{next_step}\n"
            )
            phase = {
                "id": phase_id, "type": "phase", "scope": "task",
                "project_id": task["project_id"], "task_id": task_id, "title": title,
                "summary": summary, "findings": findings, "verification": verification,
                "next_step": next_step, "sequence": sequence, "status": "completed",
                "consolidation_status": "pending", "body": body, "revision": 1,
                "created_at": timestamp, "updated_at": timestamp,
            }
            updated_task = {
                **task, "phase_count": sequence, "current_phase": title,
                "findings": findings, "verification": verification, "next_step": next_step,
                "revision": task["revision"] + 1, "updated_at": timestamp,
            }
            entries = [
                {"record": phase, "path": f"phases/{phase_id}.md", "expected": None},
                {"record": updated_task, "path": task["path"], "expected": task["etag"]},
            ]
            abandoned.clear()
            for unit in open_units:
                cancelled = {
                    **unit, "status": "cancelled", "blocker": "",
                    "revision": unit["revision"] + 1, "updated_at": timestamp,
                }
                cancelled["body"] = unit["body"] + (
                    f"\n\n## Cancelled at phase boundary\n{summary}\n"
                )
                entries.append({"record": cancelled, "path": unit["path"], "expected": unit["etag"]})
                abandoned.append(unit["id"])
            response = {
                "phase_id": phase_id, "task_id": task_id, "phase_revision": 1,
                "phase_etag": digest(encode_record(phase).encode("utf-8")),
                "task_revision": updated_task["revision"],
                "task_etag": digest(encode_record(updated_task).encode("utf-8")),
                "abandoned_work_units": list(abandoned),
            }
            return entries, response

        result = self.vault.commit(operation_id, request, build)
        self._refresh_all_index()
        return result

    def _scope(self, scope, project_id, task_id=None):
        if scope not in ("project", "task", "user"):
            raise DiagnosticError("SCOPE_DENIED", "Choose project, task or user scope explicitly.")
        if scope != "user":
            self._project(project_id)
        if scope == "task":
            task = self.task_read(task_id)
            if task["project_id"] != project_id:
                raise DiagnosticError("SCOPE_DENIED", "Task belongs to a different project.")

    def memory_write(self, title, body, sources, operation_id, scope="project", project_id=None,
                     task_id=None, kind="fact", status="active"):
        self._scope(scope, project_id, task_id)
        title, body = text(title, "title", 200), text(body, "body", 60000)
        if kind not in ("fact", "decision", "preference", "lesson", "constraint", "procedure"):
            raise DiagnosticError("RECORD_INVALID", "Unsupported knowledge kind.")
        if status not in ("active", "candidate"):
            raise DiagnosticError("RECORD_INVALID", "Knowledge status must be active or candidate.")
        if not isinstance(sources, list) or not 1 <= len(sources) <= 20:
            raise DiagnosticError("RECORD_INVALID", "Memory requires 1–20 explicit sources.")
        for source in sources:
            if not isinstance(source, dict) or source.get("type") not in ("user", "file", "task", "host"):
                raise DiagnosticError("RECORD_INVALID", "Sources need type and reference.")
            text(source.get("reference"), "source.reference", 2000)
        if len(str(sources)) > 20000:
            raise DiagnosticError("RECORD_INVALID", "Sources exceed the limit.")
        if scope == "user":
            if task_id is not None:
                raise DiagnosticError("SCOPE_DENIED", "User-scope knowledge cannot be bound to a task.")
            if not any(source["type"] == "user" for source in sources):
                raise DiagnosticError(
                    "SCOPE_DENIED",
                    "User-scope knowledge requires an explicit user source; use project scope for project facts.",
                )
        project_id = None if scope == "user" else project_id
        request = dict(action="memory_write", title=title, body=body, sources=sources, scope=scope,
                       project_id=project_id, task_id=task_id, kind=kind, status=status)
        return self._create(operation_id, request, "memory", scope, project_id, title, body,
                            sources=sources, task_id=task_id, kind=kind, confidence="host_reported",
                            record_status=status)

    def memory_read(self, memory_id):
        return self._record(memory_id, "memory")

    def memory_search(self, project_id=None, query="", scope="project", task_id=None, limit=20, status="active"):
        self._scope(scope, project_id, task_id)
        if status not in ("active", "candidate"):
            raise DiagnosticError("RECORD_INVALID", "Search status must be active or candidate.")
        if not isinstance(query, str) or len(query) > 1000 or type(limit) is not int or not 1 <= limit <= 100:
            raise DiagnosticError("RECORD_INVALID", "Invalid query or limit (1–100).")
        self._repair_index_if_needed()
        index = self.index
        if index is None:
            raise DiagnosticError(
                "INDEX_UNAVAILABLE",
                "MemWeave search index is not running; start the service before searching.",
            )
        rows = index.search(
            query=query,
            note_type="memory",
            scope=scope,
            project_id=None if scope == "user" else project_id,
            task_id=task_id if scope == "task" else None,
            status=status,
            limit=limit,
        )
        items = []
        for row in rows:
            try:
                record = self.vault.read_path(row["file_path"])
            except DiagnosticError as error:
                self._index_error = str(error)
                continue
            if record.get("type") != "memory" or record.get("status") != status:
                continue
            record["retrieval"] = {
                "permalink": row.get("permalink"),
                "score": row.get("score"),
            }
            items.append(record)
        return {
            "items": items,
            "matched": len(items),
            "coverage": {
                "state": "partial",
                "strategy": "basic_memory_sqlite_fts5",
                "query_limit": limit,
                "record_limit": 1000,
            },
        }

    def memory_context(self, permalink, depth=1):
        """Return Basic Memory's parsed relation and observation context."""
        permalink = text(permalink, "permalink", 500)
        if type(depth) is not int or not 1 <= depth <= 4:
            raise DiagnosticError("RECORD_INVALID", "depth must be between 1 and 4.")
        self._repair_index_if_needed()
        if self.index is None:
            raise DiagnosticError(
                "INDEX_UNAVAILABLE",
                "MemWeave search index is not running; start the service before reading context.",
            )
        result = self.index.context(permalink, depth=depth)
        related = []
        observations = []
        for item in result.results:
            for row in item.related_results:
                related.append({
                    "type": row.type,
                    "title": row.title,
                    "permalink": row.permalink,
                    "file_path": row.file_path,
                    "depth": row.depth,
                    "relation_type": row.relation_type,
                    "to_name": row.to_name,
                })
            for row in item.observations:
                observations.append({
                    "category": row.category,
                    "content": row.content,
                    "permalink": row.permalink,
                })
        metadata = result.metadata
        return {
            "metadata": {
                "uri": metadata.uri,
                "depth": metadata.depth,
                "primary_count": metadata.primary_count,
                "related_count": metadata.related_count,
                "total_observations": metadata.total_observations,
                "total_relations": metadata.total_relations,
                "has_more": metadata.has_more,
            },
            "related": related,
            "observations": observations,
        }

    def memory_update(self, memory_id, expected_revision, expected_etag, title, body, operation_id):
        changes = dict(title=text(title, "title", 200), body=text(body, "body", 60000),
                       correction_source="explicit_host_or_user_revision")
        return self._update("memory_update", memory_id, "memory", expected_revision, expected_etag, operation_id, changes)

    def memory_activate(self, memory_id, expected_revision, expected_etag, operation_id):
        return self._update("memory_activate", memory_id, "memory", expected_revision, expected_etag,
                            operation_id, {"status": "active", "activation_source": "explicit_review"})

    def memory_review(self, task_id, phase_id=None):
        task = self.task_read(task_id)
        if phase_id is not None:
            phase = self._record(phase_id, "phase")
            if phase["task_id"] != task_id or phase["project_id"] != task["project_id"]:
                raise DiagnosticError("SCOPE_DENIED", "Phase belongs to a different task.")
            state = phase.get("consolidation_status", "pending")
            query = f"{task['title']} {phase['title']}"
            evidence = {"state": state, "task": task, "phase": phase}
        else:
            state = task.get("consolidation_status", "pending"
                             if task["status"] == "completed" else "not_due")
            query = task["title"]
            evidence = {"state": state, "task": task}
        return {**evidence,
                "existing_knowledge": self.memory_search(
                    task["project_id"], query=query
                )["items"],
                "instructions": (
                    "Host reviews evidence, searches user wording and bilingual aliases for duplicates, "
                    "and writes candidates or corrections. Reusable knowledge should end with a concise "
                    "Retrieval cues line. No model has run here."
                )}

    def memory_review_complete(
        self, task_id, expected_revision, expected_etag, memory_ids, summary,
        operation_id, phase_id=None,
    ):
        if not isinstance(memory_ids, list) or len(memory_ids) > 30:
            raise DiagnosticError("RECORD_INVALID", "memory_ids must contain at most 30 reviewed knowledge IDs.")
        for memory_id in memory_ids:
            text(memory_id, "memory_id", 100)
        changes = dict(consolidation_status="reviewed", consolidation_memory_ids=memory_ids,
                       consolidation_summary=text(summary, "summary"))
        task = self._record(task_id, "task")
        for memory_id in memory_ids:
            memory = self.memory_read(memory_id)
            if memory.get("project_id") != task["project_id"] or (
                memory["scope"] == "task" and memory.get("task_id") != task_id
            ):
                raise DiagnosticError("SCOPE_DENIED", "Reviewed knowledge must belong to this project and task.")
        if phase_id is not None:
            phase = self._record(phase_id, "phase")
            if phase["task_id"] != task_id:
                raise DiagnosticError("SCOPE_DENIED", "Phase belongs to a different task.")
            return self._update(
                "phase_review_complete", phase_id, "phase", expected_revision,
                expected_etag, operation_id, changes,
            )
        changes["archived_at"] = now()
        return self._update("memory_review_complete", task_id, "task", expected_revision, expected_etag,
                            operation_id, changes)

    def memory_forget(self, memory_id, expected_revision, expected_etag, operation_id):
        if type(expected_revision) is not int or expected_revision < 1:
            raise DiagnosticError("RECORD_INVALID", "expected_revision must be a positive integer.")
        text(expected_etag, "expected_etag", 64)
        request = dict(action="memory_forget", id=memory_id, expected_revision=expected_revision, expected_etag=expected_etag)
        removed_paths = []
        def build():
            old = self.memory_read(memory_id)
            if old["revision"] != expected_revision or old["etag"] != expected_etag:
                raise DiagnosticError("VERSION_CONFLICT", "Read the latest revision and etag before forgetting.")
            removed_paths.append(old["path"])
            return [{"record": None, "id": memory_id, "path": old["path"], "expected": old["etag"]}], {
                "id": memory_id, "deleted": True
            }
        response = self.vault.commit(operation_id, request, build)
        self._refresh_index(removed=removed_paths)
        return response

    def context_get(
        self, project_id, task_id=None, query="", include_user=False, budget=12000,
        execution_root=None,
    ):
        from memweave.context import build_context
        return build_context(
            self, project_id, task_id, query, include_user, budget,
            execution_root=execution_root,
        )

    def _map(self):
        from memweave.project_map import ProjectMap
        return ProjectMap(self.vault)

    def project_scan(self, project_id, operation_id):
        self._project(project_id)
        return self._map().scan(project_id, operation_id)

    def graph_query(self, project_id, query="", limit=100, include_stale=False):
        self._project(project_id)
        return self._map().query(project_id, query, limit, include_stale=include_stale)

    def graph_write(self, project_id, operation_id, title, body, sources, relations):
        self._project(project_id)
        response = self._map().write(project_id, operation_id, title, body, sources, relations)
        self._refresh_all_index()
        return response

    def graph_update(self, project_id, operation_id, architecture_id, expected_revision,
                     expected_etag, title, body, sources, relations):
        self._project(project_id)
        response = self._map().update(
            project_id, operation_id, architecture_id, expected_revision,
            expected_etag, title, body, sources, relations,
        )
        self._refresh_index([response["path"]])
        return response

    def graph_forget(self, project_id, operation_id, architecture_id,
                     expected_revision, expected_etag):
        self._project(project_id)
        response = self._map().forget(
            project_id, operation_id, architecture_id, expected_revision, expected_etag,
        )
        self._refresh_index(removed=[response["path"]])
        return response

    def evidence_read(self, project_id, path, max_chars=12000):
        self._project(project_id)
        return self._map().evidence(project_id, path, max_chars)

    def recover(self):
        output = self.vault.recover()
        if output.get("recovered"):
            self._refresh_all_index()
        return output

    def recovery_status(self):
        return self.vault.recovery_status()

    def recovery_resolve(self, operation_id, expected_files):
        output = self.vault.resolve(operation_id, expected_files)
        self._refresh_all_index()
        return output

    def rebuild(self):
        output = self.vault.rebuild()
        if self.index is not None and self.index.active:
            try:
                output["search_index"] = self.index.rebuild()
                self._index_error = None
            except Exception as error:
                self._index_error = str(error)
                raise DiagnosticError(
                    "INDEX_UNAVAILABLE",
                    "Basic Memory index rebuild failed; the Markdown records were not changed.",
                ) from error
        return output

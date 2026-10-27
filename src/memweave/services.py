import re
from pathlib import Path
from uuid import uuid4

from memweave.diagnostics import DiagnosticError, result, validate_text
from memweave.storage import Vault, digest, encode_record, now


def text(value, field, limit=12000):
    validate_text(value, field, limit)
    return value.strip()


def tokens(value):
    words = set(re.findall(r"[a-z0-9_]+", value.lower()))
    for phrase in re.findall(r"[\u3400-\u9fff]+", value):
        words.update(phrase)
        words.update(phrase[index:index + 2] for index in range(max(1, len(phrase) - 1)))
    return words


class Service:
    def __init__(self, home: Path):
        self.vault = Vault(home)

    def initialize(self):
        self.vault.initialize()
        return self.status()

    def dispatch(self, action: str, parameters: dict | None = None) -> dict:
        parameters = {} if parameters is None else parameters
        allowed = {
            "project_register", "project_list", "task_open", "task_read", "task_list",
            "task_checkpoint", "task_close", "memory_write", "memory_read", "memory_search",
            "memory_update", "memory_forget", "context_get", "project_scan", "graph_query",
            "graph_write", "evidence_read", "recover", "rebuild", "status",
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

    def status(self):
        output = self.vault.diagnostics.status()["data"]
        with self.vault.diagnostics._connect() as connection:
            output["counts"] = {row[0]: row[1] for row in connection.execute(
                "SELECT type, COUNT(*) FROM records GROUP BY type"
            )}
            output["pending_operations"] = connection.execute(
                "SELECT COUNT(*) FROM operations WHERE state != 'completed'"
            ).fetchone()[0]
        output.update(phase="local_mvp", task_memory="available", long_term_memory="available",
                      project_graph="available")
        return output

    def _record(self, record_id, record_type):
        text(record_id, "record_id", 100)
        record = self.vault.read(record_id)
        if record["type"] != record_type:
            raise DiagnosticError("SCOPE_DENIED", "Record has a different type.")
        return record

    def _project(self, project_id):
        return self._record(project_id, "project")

    def _create(self, operation_id, request, record_type, scope, project_id, title, body,
                record_status="active", **metadata):
        def build():
            record_id = record_type + "_" + uuid4().hex
            record = dict(id=record_id, type=record_type, scope=scope, project_id=project_id,
                          title=title, body=body, revision=1, status=record_status,
                          created_at=now(), updated_at=now(), **metadata)
            directory = {"project": "projects", "task": "tasks", "memory": "knowledge"}[record_type]
            response = {"id": record_id, "revision": 1,
                        "etag": digest(encode_record(record).encode("utf-8"))}
            return [{"record": record, "path": f"{directory}/{record_id}.md", "expected": None}], response
        return self.vault.commit(operation_id, request, build)

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
            if record_type == "task" and old["status"] != "active" and action != "memory_review_complete":
                raise DiagnosticError("TASK_CLOSED", "A closed task cannot receive further changes.")
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
                    f"\n\n### Next step\n{changes['next_step']}\n"
                )
            if action == "task_close":
                acceptance = old["acceptance"]
                answers = changes["acceptance_results"]
                if changes["status"] == "completed" and (
                    set(answers) != set(acceptance) or
                    any(not isinstance(value, str) or not value.strip() for value in answers.values())
                ):
                    raise DiagnosticError("ACCEPTANCE_REQUIRED", "Explain the outcome of every acceptance criterion.")
                updated["body"] = old["body"] + f"\n\n## {changes['status']}\n{changes['summary']}\n"
            response = dict(id=record_id, revision=updated["revision"],
                            etag=digest(encode_record(updated).encode("utf-8")))
            return [{"record": updated, "path": old["path"], "expected": old["etag"]}], response
        return self.vault.commit(operation_id, request, build)

    def project_register(self, root, name, operation_id):
        root_path = Path(text(root, "root", 4096)).expanduser().resolve()
        name = text(name, "name", 200)
        if not root_path.is_dir():
            raise DiagnosticError("RECORD_INVALID", "Project root must be an existing directory.")
        request = dict(action="project_register", root=str(root_path), name=name)
        def build():
            for project in self.vault.list_records("project"):
                if Path(project["root"]) == root_path:
                    return [], {"id": project["id"], "revision": project["revision"], "etag": project["etag"]}
            record_id = "project_" + uuid4().hex
            record = dict(id=record_id, type="project", scope="project", project_id=record_id,
                          root=str(root_path), title=name, name=name, body=f"# {name}\n\n{root_path}\n",
                          status="active", revision=1, created_at=now(), updated_at=now())
            return [{"record": record, "path": f"projects/{record_id}.md", "expected": None}], {
                "id": record_id, "revision": 1, "etag": digest(encode_record(record).encode("utf-8"))
            }
        return self.vault.commit(operation_id, request, build)

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
        project = next((item for item in self.project_list()["items"] if Path(item["root"]) == path), None)
        prepared = dict(state="registration_required", project=None, tasks=[], pending_consolidation=[],
                        context=None, task_id=None, tasks_truncated=False, pending_truncated=False)
        if project is None:
            return prepared
        prepared["project"] = {key: project[key] for key in ("id", "title", "root")}
        tasks = self.task_list(project["id"])["items"]
        active = [item for item in tasks if item["status"] == "active"]
        pending = [item for item in tasks if item["status"] == "completed"
                   and item.get("consolidation_status", "pending") == "pending"]
        fields = ("id", "title", "status", "revision", "etag")
        prepared.update(
            state="ready",
            tasks=[{key: item[key] for key in fields} for item in active[:20]],
            pending_consolidation=[{key: item[key] for key in fields} for item in pending[:20]],
            tasks_truncated=len(active) > 20, pending_truncated=len(pending) > 20,
            task_scan_limit=1000,
        )
        selected = None
        if intent == "resume":
            if task_id is not None:
                selected = self.task_read(task_id)
                if selected["project_id"] != project["id"]:
                    raise DiagnosticError("SCOPE_DENIED", "Task belongs to a different project.")
            elif len(active) == 1 and len(tasks) < 1000:
                selected = active[0]
            else:
                prepared["state"] = "task_selection_required" if active or len(tasks) >= 1000 else "no_active_task"
                return prepared
        if intent != "inspect":
            prepared["task_id"] = selected["id"] if selected else None
            prepared["context"] = self.context_get(project["id"], prepared["task_id"], query, include_user, budget)
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
        body = f"# {title}\n\n## Goal\n{goal}\n\n## Acceptance\n" + "\n".join(f"- {item}" for item in acceptance)
        return self._create(operation_id, request, "task", "project", project_id, title, body,
                            goal=goal, acceptance=acceptance, findings="", progress="", next_step="")

    def task_read(self, task_id):
        return self._record(task_id, "task")

    def task_list(self, project_id, status=None):
        self._project(project_id)
        if status not in (None, "active", "completed", "cancelled"):
            raise DiagnosticError("RECORD_INVALID", "Invalid task status.")
        items = self.vault.list_records("task", project_id=project_id)
        return {"items": [item for item in items if status is None or item["status"] == status]}

    def task_checkpoint(self, task_id, expected_revision, expected_etag, findings, progress, next_step, operation_id):
        if not isinstance(findings, str) or len(findings) > 12000:
            raise DiagnosticError("RECORD_INVALID", "findings must be text of at most 12000 characters.")
        changes = dict(findings=findings.strip(), progress=text(progress, "progress"),
                       next_step=text(next_step, "next_step"))
        return self._update("task_checkpoint", task_id, "task", expected_revision, expected_etag, operation_id, changes)

    def task_close(self, task_id, expected_revision, expected_etag, outcome, summary, acceptance_results, operation_id):
        if outcome not in ("completed", "cancelled") or not isinstance(acceptance_results, dict):
            raise DiagnosticError("RECORD_INVALID", "Use completed/cancelled and an acceptance result object.")
        if len(str(acceptance_results)) > 30000:
            raise DiagnosticError("RECORD_INVALID", "Acceptance results exceed the limit.")
        changes = dict(status=outcome, summary=text(summary, "summary"),
                       acceptance_results=acceptance_results, verification="host_reported",
                       consolidation_status="pending" if outcome == "completed" else "not_required")
        return self._update("task_close", task_id, "task", expected_revision, expected_etag, operation_id, changes)

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
        items = self.vault.list_records("memory", project_id=project_id if scope != "user" else None, scope=scope)
        wanted = tokens(query)
        ranked = []
        for item in items:
            if item.get("status") != status:
                continue
            if scope == "task" and item.get("task_id") != task_id:
                continue
            found = tokens(item["title"] + " " + item["body"])
            score = len(wanted & found)
            if not wanted or score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (pair[0], pair[1].get("updated_at", ""), pair[1]["id"]), reverse=True)
        return {"items": [item for _, item in ranked[:limit]], "matched": len(ranked),
                "coverage": {"state": "partial", "strategy": "lexical_english_and_chinese_bigrams", "record_limit": 1000}}

    def memory_update(self, memory_id, expected_revision, expected_etag, title, body, operation_id):
        changes = dict(title=text(title, "title", 200), body=text(body, "body", 60000),
                       correction_source="explicit_host_or_user_revision")
        return self._update("memory_update", memory_id, "memory", expected_revision, expected_etag, operation_id, changes)

    def memory_activate(self, memory_id, expected_revision, expected_etag, operation_id):
        return self._update("memory_activate", memory_id, "memory", expected_revision, expected_etag,
                            operation_id, {"status": "active", "activation_source": "explicit_review"})

    def memory_review(self, task_id):
        task = self.task_read(task_id)
        state = task.get("consolidation_status", "pending" if task["status"] == "completed" else "not_due")
        return {"state": state, "task": task,
                "existing_knowledge": self.memory_search(task["project_id"], query=task["title"])["items"],
                "instructions": "Host reviews evidence, searches for duplicates and writes candidates or corrections. No model has run here."}

    def memory_review_complete(self, task_id, expected_revision, expected_etag, memory_ids, summary, operation_id):
        if not isinstance(memory_ids, list) or len(memory_ids) > 30:
            raise DiagnosticError("RECORD_INVALID", "memory_ids must contain at most 30 reviewed knowledge IDs.")
        for memory_id in memory_ids:
            text(memory_id, "memory_id", 100)
        changes = dict(consolidation_status="reviewed", consolidation_memory_ids=memory_ids,
                       consolidation_summary=text(summary, "summary"))
        return self._update("memory_review_complete", task_id, "task", expected_revision, expected_etag,
                            operation_id, changes)

    def memory_forget(self, memory_id, expected_revision, expected_etag, operation_id):
        if type(expected_revision) is not int or expected_revision < 1:
            raise DiagnosticError("RECORD_INVALID", "expected_revision must be a positive integer.")
        text(expected_etag, "expected_etag", 64)
        request = dict(action="memory_forget", id=memory_id, expected_revision=expected_revision, expected_etag=expected_etag)
        def build():
            old = self.memory_read(memory_id)
            if old["revision"] != expected_revision or old["etag"] != expected_etag:
                raise DiagnosticError("VERSION_CONFLICT", "Read the latest revision and etag before forgetting.")
            return [{"record": None, "id": memory_id, "path": old["path"], "expected": old["etag"]}], {
                "id": memory_id, "deleted": True
            }
        return self.vault.commit(operation_id, request, build)

    def context_get(self, project_id, task_id=None, query="", include_user=False, budget=12000):
        from memweave.context import build_context
        return build_context(self, project_id, task_id, query, include_user, budget)

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
        return self._map().write(project_id, operation_id, title, body, sources, relations)

    def evidence_read(self, project_id, path, max_chars=12000):
        self._project(project_id)
        return self._map().evidence(project_id, path, max_chars)

    def recover(self):
        return self.vault.recover()

    def recovery_status(self):
        return self.vault.recovery_status()

    def recovery_resolve(self, operation_id, expected_files):
        return self.vault.resolve(operation_id, expected_files)

    def rebuild(self):
        return self.vault.rebuild()

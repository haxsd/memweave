import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from memweave.basic_memory_index import BasicMemoryIndex
from memweave.diagnostics import DiagnosticError, result, validate_text
from memweave.execution import ExecutionClaimStore
from memweave.storage import (
    Vault,
    contract_digest,
    digest,
    encode_record,
    normalize_evidence,
    now,
    record_evidence,
)
from memweave.worktrees import primary_worktree_root, same_repository


TASK_OPEN_STATUSES = ("current", "queued", "side")
TASK_STATUS_ALIASES = {"active": "current"}
TASK_HISTORY_STATUSES = (
    "current", "queued", "side", "completed", "cancelled", "pending_review",
    "archived", "open", "history", "all",
)
WORK_UNIT_OPEN_STATUSES = ("pending", "running", "blocked")
WORK_UNIT_TRANSITIONS = {
    "pending": {"running", "cancelled"},
    "running": {"running", "blocked", "completed", "cancelled"},
    "blocked": {"blocked", "running", "completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}

# Bounded consolidation package returned by memory_review (read-only, no model call).
CONSOLIDATION_LIMITS = {
    "recent_checkpoints": 10,
    "checkpoint_findings_chars": 500,
    "checkpoint_evidence_items": 5,
    "knowledge_per_status": 50,
    "knowledge_title_chars": 160,
    "knowledge_evidence_items": 3,
    "knowledge_sources": 3,
    "pending_items": 20,
    "duplicate_title_groups": 20,
    "duplicate_title_ids": 10,
    "knowledge_ids_without_evidence": 20,
    "recent_checkpoint_chars": 16000,
    "active_knowledge_chars": 20000,
    "candidate_knowledge_chars": 20000,
    "pending_chars": 8000,
    "lint_chars": 12000,
}

# One-line knowledge navigation map derived on demand from Markdown; never authoritative.
KNOWLEDGE_INDEX_LIMITS = {
    "active_rows": 120,
    "candidate_rows": 60,
    "retired_ids": 20,
    "line_chars": 240,
    "active_chars": 25000,
    "candidate_chars": 12000,
    "title_chars": 60,
    "summary_chars": 50,
}


def text(value, field, limit=12000):
    validate_text(value, field, limit)
    return value.strip()


def optional_text(value, field, limit=12000):
    if not isinstance(value, str) or len(value) > limit:
        raise DiagnosticError("RECORD_INVALID", f"{field} must be text of at most {limit} characters.")
    return value.strip()


def _task_timestamp():
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M")


def task_order(task):
    rank = {"current": 0, "queued": 1, "side": 2, "completed": 3, "cancelled": 4}
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
        return lifecycle in ("current", "queued", "side", "pending_review")
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
        "task_kind", "parent_task_id",
    )
    summary = {key: task.get(key) for key in keys if key in task}
    summary["lifecycle"] = task_lifecycle_status(task)
    return summary


def _bounded_text(value, limit):
    text_value = "" if value is None else str(value)
    return text_value if len(text_value) <= limit else text_value[:limit]


def _bounded_json_entries(entries, limit, budget):
    """Keep at most ``limit`` entries while staying within an approximate JSON budget."""
    selected = []
    used = 0
    for entry in entries[:limit]:
        cost = len(json.dumps(entry, ensure_ascii=False))
        if selected and used + cost > budget:
            break
        selected.append(entry)
        used += cost
    return selected


def _compact_evidence(evidence, item_limit, reference_limit, summary_limit):
    items = []
    for item in list(evidence)[:item_limit]:
        if not isinstance(item, dict):
            continue
        entry = {
            "kind": item.get("kind"),
            "reference": _bounded_text(item.get("reference"), reference_limit),
        }
        for field, limit in (("hash", 128), ("observed_at", 64), ("summary", summary_limit)):
            value = item.get(field)
            if isinstance(value, str) and value:
                entry[field] = _bounded_text(value, limit)
        items.append(entry)
    return items


def _normalized_title(title):
    return " ".join(str(title or "").casefold().split())


def _evidence_markdown(evidence):
    if not evidence:
        return "(none recorded)"
    lines = []
    for item in evidence:
        parts = [str(item.get("kind", "host")), str(item.get("reference", ""))]
        for field in ("hash", "observed_at", "summary"):
            value = item.get(field)
            if value:
                parts.append(f"{field}={value}")
        lines.append("- " + " | ".join(part.replace("\n", " ") for part in parts))
    return "\n".join(lines)


def _single_line(value, limit):
    """Collapse whitespace and bound one index column to ``limit`` characters."""
    compact = " ".join(("" if value is None else str(value)).split())
    return compact if len(compact) <= limit else compact[:limit]


def _retrieval_cue_value(line):
    stripped = line.strip()
    prefix = "retrieval cues"
    if not stripped.casefold().startswith(prefix):
        return None
    remainder = stripped[len(prefix):].lstrip(" *")
    if remainder[:1] in (":", "："):
        remainder = remainder[1:]
    return remainder.strip()


def _knowledge_summary(body):
    """Pick one bounded applicability sentence from a knowledge body.

    Prefer the first non-heading Markdown line (the applicability statement) and fall
    back to an explicit ``Retrieval cues:`` line when the body has no content line.
    """
    cues = ""
    for line in str(body or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        cue = _retrieval_cue_value(stripped)
        if cue is not None:
            if cue and not cues:
                cues = cue
            continue
        return stripped
    return cues


def _knowledge_index_line(record, limits):
    """Render one knowledge record as a bounded ``id | ...`` line.

    Values are whitespace-collapsed, ``" | "`` inside title/summary is neutralized to
    keep exactly eight columns, and pathological legacy ids fall back to dropping the
    title/summary columns so the evidence/updated tail always survives.
    """
    identifier = _single_line(record.get("id"), 100)
    kind = _single_line(record.get("kind") or "unknown", 24)
    status = _single_line(record.get("status") or "unknown", 16)
    scope = _single_line(record.get("scope") or "unknown", 16)
    evidence_count = len(record_evidence(record))
    updated_at = _single_line(record.get("updated_at"), 32)

    def render(name, title, summary):
        return (
            f"{name} | {kind} | {status} | {scope} | {title} | {summary}"
            f" | evidence={evidence_count} | updated={updated_at}"
        )

    fixed = len(render("", "", ""))
    middle_budget = max(0, limits["line_chars"] - fixed - len(identifier))
    title = _single_line(record.get("title"), min(limits["title_chars"], middle_budget))
    title = title.replace(" | ", " / ")
    summary_budget = min(limits["summary_chars"], max(0, middle_budget - len(title)))
    summary = _single_line(_knowledge_summary(record.get("body")), summary_budget)
    summary = summary.replace(" | ", " / ")
    line = render(identifier, title, summary)
    if len(line) > limits["line_chars"]:
        name_budget = max(1, limits["line_chars"] - fixed)
        line = render(identifier[:name_budget], "", "")
    return line[: limits["line_chars"]]


def _knowledge_index_section(records, row_limit, char_limit, limits, key, reverse=False):
    """Select ordered index lines within row and total-character budgets."""
    ordered = sorted(records, key=key, reverse=reverse)
    lines = []
    chars = 0
    reasons = []
    for record in ordered:
        if len(lines) >= row_limit:
            reasons.append("rows")
            break
        line = _knowledge_index_line(record, limits)
        if lines and chars + len(line) > char_limit:
            reasons.append("chars")
            break
        lines.append(line)
        chars += len(line)
    return {
        "items": lines,
        "total": len(ordered),
        "returned": len(lines),
        "limit": row_limit,
        "chars": chars,
        "char_limit": char_limit,
        "truncated": len(ordered) > len(lines),
        "truncation_reasons": reasons,
    }


class Service:
    # Agent MCP turns on require_workspace_prepare so the first project-scoped
    # tool call must follow a successful workspace_prepare (process lifetime).
    PREPARE_EXEMPT_ACTIONS = frozenset({
        "workspace_prepare",
        "status",
        "project_list",
        "project_register",
        "recover",
        "rebuild",
        "recovery_status",
        "recovery_resolve",
    })
    PREPARE_RECORD_KEYS = {
        "task_read": ("task_id", "task"),
        "task_list": ("project_id", None),
        "task_checkpoint": ("task_id", "task"),
        "task_close": ("task_id", "task"),
        "task_phase_close": ("task_id", "task"),
        "work_unit_update": ("task_id", "task"),
        "memory_review": ("task_id", "task"),
        "memory_review_complete": ("task_id", "task"),
        "memory_read": ("memory_id", "memory"),
        "memory_update": ("memory_id", "memory"),
        "memory_forget": ("memory_id", "memory"),
        "memory_activate": ("memory_id", "memory"),
    }

    def __init__(self, home: Path):
        self.vault = Vault(home)
        self.index: BasicMemoryIndex | None = None
        self._index_error: str | None = None
        self.execution = ExecutionClaimStore(home)
        self.require_workspace_prepare = False
        self._prepared_projects: set[str] = set()
        self._project_map = None

    def initialize(self):
        self.vault.initialize()
        self._migrate_task_model()
        self._migrate_memory_scope()
        return self.status()

    def start(self):
        """Initialize storage and the one Basic Memory index for this process."""
        self.vault.initialize()
        self._migrate_task_model()
        self._migrate_memory_scope()
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
        self.execution.release_all()
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

    def _mark_workspace_prepared(self, prepared: dict) -> None:
        project = prepared.get("project")
        if not isinstance(project, dict):
            return
        project_id = project.get("id")
        if (
            isinstance(project_id, str)
            and project_id
            and prepared.get("state") in ("ready", "no_active_task", "task_claimed")
        ):
            self._prepared_projects.add(project_id)

    def _require_execution_claim(self, task_id: str) -> None:
        if self.execution.is_authorized(task_id):
            return
        raise DiagnosticError(
            "EXECUTION_CLAIM_REQUIRED",
            "This task is owned by another live MCP service instance. Do not continue it from this "
            "driver; create or resume a side task, or obtain the parent's "
            "execution claim for a delegated subagent.",
        )

    def _prepare_project_id(self, action: str, parameters: dict) -> str | None:
        project_id = parameters.get("project_id")
        if isinstance(project_id, str) and project_id.strip():
            return project_id
        mapping = self.PREPARE_RECORD_KEYS.get(action)
        if mapping is None:
            return None
        key, record_type = mapping
        if record_type is None:
            return None
        record_id = parameters.get(key)
        if not isinstance(record_id, str) or not record_id.strip():
            return None
        try:
            record = self._record(record_id, record_type)
        except DiagnosticError:
            return None
        value = record.get("project_id")
        return value if isinstance(value, str) and value else None

    def _enforce_workspace_prepare(self, action: str, parameters: dict) -> None:
        if not self.require_workspace_prepare or action in self.PREPARE_EXEMPT_ACTIONS:
            return
        project_id = self._prepare_project_id(action, parameters)
        if project_id is not None:
            if project_id in self._prepared_projects:
                return
            raise DiagnosticError(
                "PREPARE_REQUIRED",
                "Call workspace_prepare for this project's absolute execution root "
                "before other MemWeave project tools.",
            )
        if self._prepared_projects:
            return
        raise DiagnosticError(
            "PREPARE_REQUIRED",
            "Call workspace_prepare for the absolute execution root before other MemWeave tools.",
        )

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
            "workspace_prepare", "knowledge_index",
        }
        if action not in allowed or not isinstance(parameters, dict):
            raise DiagnosticError("RECORD_INVALID", "Unknown action or invalid parameter object.")
        self._enforce_workspace_prepare(action, parameters)
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
            "side": sum(item["status"] == "side" for item in tasks),
            "pending_review": sum(task_lifecycle_status(item) == "pending_review" for item in tasks),
            "completed": sum(item["status"] == "completed" for item in tasks),
            "cancelled": sum(item["status"] == "cancelled" for item in tasks),
            "archived": sum(task_lifecycle_status(item) == "archived" for item in tasks),
        }
        output["task_queue"] = {
            "current": task_counts["current"],
            "queued": task_counts["queued"],
            "side": task_counts["side"],
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
                    "side": sum(item["status"] == "side" for item in project_tasks),
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
        scopes = ("project", "group", "user")
        by_scope = {scope: 0 for scope in scopes}
        by_status = {status: 0 for status in ("active", "candidate", "retired")}
        by_scope_status = {
            scope: {status: 0 for status in ("active", "candidate", "retired")}
            for scope in scopes
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
        ancestors = [
            project for project in projects
            if path.is_relative_to(Path(project["root"]).expanduser().resolve())
        ]
        if ancestors:
            return max(
                ancestors,
                key=lambda project: len(Path(project["root"]).resolve().parts),
            )
        primary = primary_worktree_root(path)
        for project in projects:
            if Path(project["root"]).expanduser().resolve() == primary:
                return project
        same_repository_projects = [
            project for project in projects
            if same_repository(Path(project["root"]).expanduser().resolve(), path)
        ]
        if len(same_repository_projects) == 1:
            return same_repository_projects[0]
        return None

    def _project_parent_id(self, project, projects):
        root = Path(project["root"]).expanduser().resolve()
        ancestors = []
        for candidate in projects:
            if candidate["id"] == project["id"]:
                continue
            candidate_root = Path(candidate["root"]).expanduser().resolve()
            if root != candidate_root and root.is_relative_to(candidate_root):
                ancestors.append(candidate)
        if not ancestors:
            return None
        return max(
            ancestors,
            key=lambda item: len(Path(item["root"]).resolve().parts),
        )["id"]

    def _project_children(self, project, projects):
        return [
            item for item in projects
            if item["id"] != project["id"]
            and self._project_parent_id(item, projects) == project["id"]
        ]

    def _project_group(self, project, projects):
        parent_id = self._project_parent_id(project, projects)
        if parent_id is not None:
            return next(item for item in projects if item["id"] == parent_id)
        return project if self._project_children(project, projects) else None

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
            "side": sum(item["status"] == "side" for item in tasks),
            "pending_review": sum(
                task_lifecycle_status(item) == "pending_review" for item in tasks
            ),
            "completed": sum(item["status"] == "completed" for item in tasks),
            "cancelled": sum(item["status"] == "cancelled" for item in tasks),
            "archived": sum(task_lifecycle_status(item) == "archived" for item in tasks),
        }

    def _member_summary(self, project):
        tasks = self._task_metadata(project["id"])
        counts = self._task_counts(tasks)
        current = next((item for item in tasks if item["status"] == "current"), None)
        memories = self.vault.list_records("memory", project_id=project["id"])
        knowledge_counts = {
            status: sum(item.get("status") == status for item in memories)
            for status in ("active", "candidate", "retired")
        }
        recent_knowledge = [
            {"id": item["id"], "title": item["title"]}
            for item in sorted(
                (item for item in memories if item.get("status") == "active"),
                key=lambda item: (item.get("updated_at") or "", item["id"]),
                reverse=True,
            )[:3]
        ]
        attention = []
        if current is not None and not current.get("next_step"):
            attention.append({"code": "next_step_missing", "severity": "warning"})
        if counts["pending_review"]:
            attention.append({
                "code": "pending_consolidation",
                "severity": "info",
                "count": counts["pending_review"],
            })
        return {
            "id": project["id"],
            "title": project["title"],
            "root": project["root"],
            "current": (
                {
                    "id": current["id"],
                    "title": current["title"],
                    "next_step": current.get("next_step", ""),
                }
                if current is not None
                else None
            ),
            "task_counts": counts,
            "knowledge_counts": knowledge_counts,
            "recent_knowledge": recent_knowledge,
            "attention": attention,
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

    def _migrate_memory_scope(self):
        """Delete legacy task-scope knowledge; task scope is no longer a product surface."""
        records = [
            memory for memory in self.vault.list_records("memory")
            if memory.get("scope") == "task"
        ]
        if not records:
            return
        request = {
            "action": "remove-task-memory-scope",
            "records": [
                {"id": memory["id"], "etag": memory["etag"]}
                for memory in sorted(records, key=lambda item: item["id"])
            ],
        }
        operation_id = "memory-scope-task-removal-" + hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        removed = []
        def build():
            entries = []
            for memory in records:
                current = self.vault.read(memory["id"])
                if current.get("scope") != "task":
                    continue
                removed.append(current["path"])
                entries.append({
                    "record": None,
                    "id": current["id"],
                    "path": current["path"],
                    "expected": current["etag"],
                })
            return entries, {"deleted": len(entries)}
        self.vault.commit(operation_id, request, build)
        self._refresh_index(removed=removed)

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
                    if memory.get("project_id") != old["project_id"]:
                        raise DiagnosticError("SCOPE_DENIED", "Reviewed knowledge must belong to this project.")
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
        projects = self.vault.list_records("project")
        return {
            "items": [
                {**project, "parent_id": self._project_parent_id(project, projects)}
                for project in projects
            ]
        }

    def _next_task_id(self):
        base = "task_" + _task_timestamp()
        existing = {item["id"] for item in self.vault.list_record_metadata("task")}
        if base not in existing:
            return base
        for index in range(2, 1000):
            candidate = f"{base}-{index:02d}"
            if candidate not in existing:
                return candidate
        raise DiagnosticError("TASK_STATE_INVALID", "Too many tasks created in the same minute.")

    def workspace_prepare(
        self, root, intent="inspect", task_id=None, query="", include_user=False,
        include_group_knowledge=False, include_knowledge_index=False, budget=12000,
        execution_claim=None,
    ):
        path = Path(text(root, "root", 4096)).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise DiagnosticError("RECORD_INVALID", "root must be an existing absolute project directory.")
        if intent not in ("inspect", "new", "resume") or (task_id is not None and intent != "resume"):
            raise DiagnosticError("RECORD_INVALID", "Use inspect/new/resume; task_id requires resume intent.")
        if task_id is not None:
            text(task_id, "task_id", 100)
        if execution_claim is not None:
            text(execution_claim, "execution_claim", 128)
            if intent != "resume":
                raise DiagnosticError("RECORD_INVALID", "execution_claim requires resume intent.")
        if not isinstance(query, str) or len(query) > 1000 or type(include_user) is not bool or (
            type(include_group_knowledge) is not bool
        ) or (
            type(include_knowledge_index) is not bool
        ) or (
            type(budget) is not int or not 500 <= budget <= 32000
        ):
            raise DiagnosticError(
                "RECORD_INVALID",
                "Invalid query, include_user, include_group_knowledge, include_knowledge_index or context budget.",
            )
        path = path.resolve()
        project = self._project_for_root(path)
        prepared = dict(
            state="registration_required", project=None, tasks=[], pending_consolidation=[],
            pending_phases=[], work_units=[], context=None, task_id=None, tasks_truncated=False,
            pending_truncated=False, phases_truncated=False, work_units_truncated=False,
            members=[], members_truncated=False,
            attention=[], completion=None, execution_root=str(path), registered_root=None,
            is_worktree=False, is_subfolder=False, background_task=None, execution=None,
        )
        if project is None:
            self._mark_workspace_prepared(prepared)
            return prepared
        registered_root = Path(project["root"]).expanduser().resolve()
        prepared["registered_root"] = str(registered_root)
        is_subfolder = path != registered_root and path.is_relative_to(registered_root)
        prepared["is_subfolder"] = is_subfolder
        prepared["is_worktree"] = registered_root != path and not is_subfolder
        prepared["project"] = {
            "id": project["id"], "title": project["title"], "root": project["root"],
            "execution_root": str(path), "is_worktree": prepared["is_worktree"],
            "is_subfolder": is_subfolder,
        }
        projects = self.vault.list_records("project")
        member_projects = self._project_children(project, projects)
        member_projects.sort(key=lambda item: Path(item["root"]).as_posix().casefold())
        prepared["members"] = [
            self._member_summary(item) for item in member_projects[:20]
        ]
        prepared["members_truncated"] = len(member_projects) > 20
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
                "task_kind": item.get("task_kind", "main"),
                "parent_task_id": item.get("parent_task_id"),
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
            history=[
                {
                    **{key: item.get(key) for key in (
                        "id", "title", "status", "revision", "archived_at",
                    )},
                    "lifecycle": task_lifecycle_status(item),
                }
                for item in recent_history
            ],
            history_truncated=len(archived) > 5,
            task_counts=self._task_counts(tasks),
        )
        background = None
        if len(current) == 1:
            item = current[0]
            background = {
                "id": item["id"], "title": item["title"], "status": item["status"],
                "revision": item["revision"], "queue_position": item.get("queue_position"),
                "task_kind": item.get("task_kind", "main"),
                "execution": self.execution.public_status(item["id"]),
            }
        prepared["background_task"] = background
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
                if selected["status"] not in ("current", "side"):
                    raise DiagnosticError("TASK_NOT_CURRENT", "Only an active task can be resumed.")
            elif len(current) == 1:
                selected = current[0]
            elif len(current) > 1:
                raise DiagnosticError("TASK_STATE_INVALID", "More than one current task needs migration.")
            else:
                prepared["state"] = "no_active_task"
                self._mark_workspace_prepared(prepared)
                return prepared
            claim, claim_state = self.execution.claim(
                selected["id"], project["id"], execution_claim
            )
            if claim_state == "invalid":
                raise DiagnosticError(
                    "EXECUTION_CLAIM_INVALID",
                    "The execution claim is stale or does not belong to this task.",
                )
            if claim_state == "claimed":
                prepared["state"] = "task_claimed"
                prepared["tasks"] = []
                prepared["task_id"] = None
                prepared["execution"] = {
                    "state": "claimed_elsewhere",
                    "task_id": selected["id"],
                }
                prepared["attention"].append({
                    "code": "task_claimed_elsewhere",
                    "severity": "warning",
                    "message": (
                        "Another live MCP service instance owns this task. Do not continue it; "
                        "work on an explicit side task, or wait for the owner to close/release it."
                    ),
                })
                self._mark_workspace_prepared(prepared)
                return prepared
            prepared["execution"] = {
                "state": claim_state,
                "claim_id": claim["claim_id"],
                "task_id": selected["id"],
                "claimed_at": claim["claimed_at"],
            }
        if intent != "inspect":
            if intent == "new":
                prepared["tasks"] = []
            prepared["task_id"] = selected["id"] if selected else None
            prepared["context"] = context = self.context_get(
                project["id"], prepared["task_id"], query, include_user, budget,
                execution_root=str(path),
                include_group_knowledge=include_group_knowledge,
                include_knowledge_index=include_knowledge_index,
            )
            prepared["context"] = context
            prepared["attention"] = context.get("attention", prepared_attention)
            prepared["completion"] = context.get("completion")
        self._mark_workspace_prepared(prepared)
        return prepared

    def task_open(
        self, project_id, title, goal, acceptance, operation_id, parent_task_id=None,
    ):
        self._project(project_id)
        title, goal = text(title, "title", 200), text(goal, "goal")
        if parent_task_id is not None:
            text(parent_task_id, "parent_task_id", 100)
        if not isinstance(acceptance, list) or not 1 <= len(acceptance) <= 30:
            raise DiagnosticError("RECORD_INVALID", "acceptance needs 1–30 distinct criteria.")
        acceptance = [text(item, "criterion", 1000) for item in acceptance]
        if len(set(acceptance)) != len(acceptance):
            raise DiagnosticError("RECORD_INVALID", "Acceptance criteria must be distinct.")
        request = dict(
            action="task_open", project_id=project_id, title=title, goal=goal,
            acceptance=acceptance, parent_task_id=parent_task_id,
        )
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
            parent = None
            if parent_task_id is not None:
                parent = self._record(parent_task_id, "task")
                if parent["project_id"] != project_id:
                    raise DiagnosticError("SCOPE_DENIED", "Parent task belongs to a different project.")
                if parent["status"] != "current" or parent.get("task_kind") == "side":
                    raise DiagnosticError(
                        "TASK_NOT_CURRENT",
                        "A side task must be attached to the current main task.",
                    )
            tasks = self._task_metadata(project_id)
            opened = [
                task for task in tasks
                if task["status"] in TASK_OPEN_STATUSES
            ]
            current = sorted(
                [task for task in opened if task["status"] in ("active", "current")],
                key=task_order,
            )
            if len(current) > 1:
                raise DiagnosticError("TASK_STATE_INVALID", "More than one current task needs migration.")
            queued = [task for task in opened if task["status"] == "queued"]
            positions = [
                task["queue_position"] for task in queued
                if type(task.get("queue_position")) is int and task["queue_position"] >= 0
            ]
            task_id = self._next_task_id()
            timestamp = now()
            new_status = (
                "side" if parent is not None
                else ("queued" if current or queued else "current")
            )
            position = None if parent is not None else max(positions, default=-1) + 1
            record = {
                "id": task_id, "type": "task", "scope": "project", "project_id": project_id,
                "title": title, "body": body, "revision": 1, "status": new_status,
                "created_at": timestamp, "updated_at": timestamp, "goal": goal,
                "acceptance": acceptance, "findings": "", "progress": "", "failures": "",
                "verification": "", "next_step": "", "queue_position": position,
                "task_kind": "side" if parent is not None else "main",
                "parent_task_id": parent_task_id,
                "contract_hash": contract_digest(goal, acceptance),
                "contract_attested_at": timestamp,
                "task_model_version": 2,
            }
            entries = [{
                "record": record, "path": f"tasks/{task_id}.md", "expected": None,
            }]
            promoted_task_id = None
            if parent is None and not current and queued:
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
                "task_kind": record["task_kind"], "parent_task_id": parent_task_id,
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
        for checkpoint, metadata in zip(task["checkpoints"], selected_checkpoints):
            checkpoint["evidence"] = record_evidence(metadata)
        task["evidence"] = record_evidence(task)
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
        self, task_id, expected_revision, expected_etag, progress, next_step,
        operation_id, findings=None, failures=None, verification=None, evidence=None,
        attest_contract=False,
    ):
        return self._task_checkpoint(
            task_id, expected_revision, expected_etag, progress, next_step, operation_id,
            findings=findings, failures=failures, verification=verification,
            evidence=evidence, attest_contract=attest_contract,
            require_execution_claim=True, origin="task_driver",
        )

    def task_compaction_snapshot(
        self, task_id, expected_revision, expected_etag, findings, progress, next_step,
        operation_id,
    ):
        """Record a bounded pre-compaction snapshot without taking execution ownership.

        Codex ``PreCompact`` runs in a short-lived Hook process while the resident
        MCP service may own the task's execution claim.  The snapshot is recovery
        evidence, not a hand-over of task ownership: it keeps the same
        revision/etag CAS and operation_id idempotency, but deliberately skips the
        execution-claim check so context compaction cannot silently lose the task
        record.  It never acquires, releases or steals a claim, and it is not
        exposed as an MCP or CLI action.
        """
        return self._task_checkpoint(
            task_id, expected_revision, expected_etag, progress, next_step, operation_id,
            findings=findings, failures=None, verification=None, attest_contract=False,
            require_execution_claim=False, origin="precompact_hook",
        )

    def _task_checkpoint(
        self, task_id, expected_revision, expected_etag, progress, next_step,
        operation_id, findings=None, failures=None, verification=None, evidence=None,
        attest_contract=False, require_execution_claim=True, origin="task_driver",
    ):
        if type(attest_contract) is not bool:
            raise DiagnosticError("RECORD_INVALID", "attest_contract must be boolean.")
        changes = dict(
            progress=text(progress, "progress"),
            next_step=text(next_step, "next_step"),
        )
        for field, value in (("findings", findings), ("failures", failures), ("verification", verification)):
            if value is not None:
                changes[field] = optional_text(value, field)
        if evidence is not None:
            changes["evidence"] = normalize_evidence(evidence)
        if type(expected_revision) is not int or expected_revision < 1:
            raise DiagnosticError("RECORD_INVALID", "expected_revision must be a positive integer.")
        text(expected_etag, "expected_etag", 64)
        request = dict(
            action="task_checkpoint", id=task_id, expected_revision=expected_revision,
            expected_etag=expected_etag, changes=changes, attest_contract=attest_contract,
            origin=origin,
        )

        def build():
            old = self._record(task_id, "task")
            if old["revision"] != expected_revision or old["etag"] != expected_etag:
                raise DiagnosticError("VERSION_CONFLICT", "Read the latest revision and etag before writing.")
            if old["status"] not in ("current", "side"):
                raise DiagnosticError("TASK_NOT_CURRENT", "Only an active task can receive checkpoints.")
            if require_execution_claim:
                self._require_execution_claim(task_id)
            resolved = {
                **{field: old.get(field, "") for field in ("findings", "failures", "verification")},
                "evidence": record_evidence(old),
                **changes,
            }
            timestamp = now()
            sequence = old.get("checkpoint_count", 0) + 1
            checkpoint_id = "checkpoint_" + uuid4().hex
            checkpoint_title = f"Checkpoint {sequence}: {old['title']}"[:200]
            checkpoint_body = (
                f"# {checkpoint_title}\n\n## Findings\n{resolved['findings']}"
                f"\n\n## Progress\n{resolved['progress']}"
                f"\n\n## Failures\n{resolved['failures']}"
                f"\n\n## Verification\n{resolved['verification']}"
                f"\n\n## Evidence\n{_evidence_markdown(resolved['evidence'])}"
                f"\n\n## Next step\n{resolved['next_step']}\n"
            )
            checkpoint = {
                "id": checkpoint_id, "type": "checkpoint", "scope": "task",
                "project_id": old["project_id"], "task_id": task_id,
                "title": checkpoint_title, "sequence": sequence, "status": "recorded",
                "body": checkpoint_body, "revision": 1, "created_at": timestamp,
                "updated_at": timestamp, "origin": origin, **resolved,
            }
            applied = dict(resolved)
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
                "origin": origin,
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
            self._require_execution_claim(task_id)
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
        self.execution.release(task_id)
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
                if task["status"] not in ("current", "side"):
                    raise DiagnosticError("TASK_NOT_CURRENT", "WorkUnits require an active task.")
                self._require_execution_claim(task_id)
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
        if title is not None:
            title = text(title, "title", 200)
        if owner is not None:
            owner = text(owner, "owner", 200)
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
            if title is not None and title != unit["title"]:
                raise DiagnosticError(
                    "RECORD_INVALID", "WorkUnit title and owner are immutable after creation."
                )
            if owner is not None and owner != unit["owner"]:
                raise DiagnosticError(
                    "RECORD_INVALID", "WorkUnit title and owner are immutable after creation."
                )
            task = self._record(unit["task_id"], "task")
            if task["status"] not in ("current", "side"):
                raise DiagnosticError("TASK_NOT_CURRENT", "Only WorkUnits of an active task can change.")
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
            if task["status"] not in ("current", "side"):
                raise DiagnosticError("TASK_NOT_CURRENT", "Only an active task can close a phase.")
            self._require_execution_claim(task_id)
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

    def _scope(self, scope, project_id):
        if scope not in ("project", "group", "user"):
            raise DiagnosticError(
                "SCOPE_DENIED",
                "Choose project, group or user scope explicitly; task scope is no longer supported.",
            )
        if scope != "user":
            self._project(project_id)

    def memory_write(self, title, body, sources, operation_id, scope="project", project_id=None,
                     kind="fact", status="active", evidence=None):
        self._scope(scope, project_id)
        title, body = text(title, "title", 200), text(body, "body", 60000)
        if kind not in ("fact", "decision", "preference", "lesson", "constraint", "procedure"):
            raise DiagnosticError("RECORD_INVALID", "Unsupported knowledge kind.")
        if status not in ("active", "candidate"):
            raise DiagnosticError("RECORD_INVALID", "Knowledge status must be active or candidate.")
        normalized_evidence = normalize_evidence(evidence if evidence is not None else [])
        if not isinstance(sources, list) or not 1 <= len(sources) <= 20:
            raise DiagnosticError("RECORD_INVALID", "Memory requires 1–20 explicit sources.")
        for source in sources:
            if not isinstance(source, dict) or source.get("type") not in ("user", "file", "task", "host"):
                raise DiagnosticError("RECORD_INVALID", "Sources need type and reference.")
            text(source.get("reference"), "source.reference", 2000)
        if len(str(sources)) > 20000:
            raise DiagnosticError("RECORD_INVALID", "Sources exceed the limit.")
        if scope == "user":
            if not any(source["type"] == "user" for source in sources):
                raise DiagnosticError(
                    "SCOPE_DENIED",
                    "User-scope knowledge requires an explicit user source; use project scope for project facts.",
                )
        if scope == "group":
            project = self._project(project_id)
            if not self._project_children(project, self.vault.list_records("project")):
                raise DiagnosticError(
                    "SCOPE_DENIED",
                    "Group scope requires at least one registered direct child project.",
                )
        project_id = None if scope == "user" else project_id
        request = dict(action="memory_write", title=title, body=body, sources=sources, scope=scope,
                       project_id=project_id, kind=kind, status=status,
                       evidence=normalized_evidence)
        return self._create(operation_id, request, "memory", scope, project_id, title, body,
                            sources=sources, kind=kind, confidence="host_reported",
                            evidence=normalized_evidence, record_status=status)

    def memory_read(self, memory_id):
        record = self._record(memory_id, "memory")
        record["evidence"] = record_evidence(record)
        return record

    def memory_search(self, project_id=None, query="", scope="project", limit=20, status="active",
                      include_group=False, include_members=False):
        self._scope(scope, project_id)
        if type(include_group) is not bool or type(include_members) is not bool:
            raise DiagnosticError("RECORD_INVALID", "include_group and include_members must be boolean.")
        if scope != "project" and (include_group or include_members):
            raise DiagnosticError(
                "RECORD_INVALID",
                "include_group and include_members require project scope.",
            )
        if status not in ("active", "candidate", "retired"):
            raise DiagnosticError("RECORD_INVALID", "Search status must be active, candidate or retired.")
        if not isinstance(query, str) or len(query) > 1000 or type(limit) is not int or not 1 <= limit <= 100:
            raise DiagnosticError("RECORD_INVALID", "Invalid query or limit (1–100).")
        self._repair_index_if_needed()
        index = self.index
        if index is None:
            raise DiagnosticError(
                "INDEX_UNAVAILABLE",
                "MemWeave search index is not running; start the service before searching.",
            )
        searches = [(scope, None if scope == "user" else project_id)]
        if scope == "project" and (include_group or include_members):
            projects = self.vault.list_records("project")
            project = self._project(project_id)
            if include_group:
                group = self._project_group(project, projects)
                if group is not None:
                    searches.append(("group", group["id"]))
            if include_members:
                for member in self._project_children(project, projects)[:20]:
                    searches.append(("project", member["id"]))
        rows = []
        for search_scope, search_project in searches:
            rows.extend(index.search(
                query=query,
                note_type="memory",
                scope=search_scope,
                project_id=search_project,
                status=status,
                limit=limit,
            ))
        merged = []
        seen = set()
        for row in sorted(
            rows,
            key=lambda item: (-float(item.get("score") or 0), item.get("file_path") or ""),
        ):
            path = row.get("file_path")
            if not path or path in seen:
                continue
            seen.add(path)
            merged.append(row)
        items = []
        for row in merged:
            try:
                record = self.vault.read_path(row["file_path"])
            except DiagnosticError as error:
                self._index_error = str(error)
                continue
            if record.get("type") != "memory" or record.get("status") != status:
                continue
            record["evidence"] = record_evidence(record)
            record["retrieval"] = {
                "permalink": row.get("permalink"),
                "score": row.get("score"),
            }
            items.append(record)
            if len(items) >= limit:
                break
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

    def memory_update(self, memory_id, expected_revision, expected_etag, title, body, operation_id,
                      status=None, evidence=None):
        changes = dict(title=text(title, "title", 200), body=text(body, "body", 60000),
                       correction_source="explicit_host_or_user_revision")
        if status is not None:
            if status not in ("candidate", "retired"):
                raise DiagnosticError(
                    "RECORD_INVALID",
                    "Use candidate or retired when changing status; activation requires memory_activate.",
                )
            changes["status"] = status
        if evidence is not None:
            changes["evidence"] = normalize_evidence(evidence)
        return self._update("memory_update", memory_id, "memory", expected_revision, expected_etag, operation_id, changes)

    def memory_activate(self, memory_id, expected_revision, expected_etag, operation_id):
        return self._update("memory_activate", memory_id, "memory", expected_revision, expected_etag,
                            operation_id, {"status": "active", "activation_source": "explicit_review"})

    def knowledge_index(self, project_id):
        """Return a bounded one-line knowledge map derived from project Markdown records.

        Read-only and rebuildable: nothing is cached or written, and the view is not
        injected into workspace_prepare/context_get. Service/CLI only, never an MCP tool.
        """
        project = self._project(project_id)
        return self._knowledge_index_view(project["id"])

    def _knowledge_index_view(self, project_id):
        records = self.vault.list_records("memory", project_id=project_id)
        limits = KNOWLEDGE_INDEX_LIMITS
        active = _knowledge_index_section(
            [record for record in records if record.get("status") == "active"],
            limits["active_rows"], limits["active_chars"], limits,
            key=lambda record: (record.get("updated_at") or "", record["id"]), reverse=True,
        )
        candidate = _knowledge_index_section(
            [record for record in records if record.get("status") == "candidate"],
            limits["candidate_rows"], limits["candidate_chars"], limits,
            key=lambda record: (record.get("created_at") or "", record["id"]),
        )
        retired = sorted(
            (record for record in records if record.get("status") == "retired"),
            key=lambda record: (record.get("updated_at") or "", record["id"]), reverse=True,
        )
        retired_ids = [record["id"] for record in retired[:limits["retired_ids"]]]
        retired_section = {
            "total": len(retired),
            "ids": retired_ids,
            "returned": len(retired_ids),
            "limit": limits["retired_ids"],
            "truncated": len(retired) > len(retired_ids),
        }
        truncated_reasons = [
            f"active_{reason}" for reason in active["truncation_reasons"]
        ] + [
            f"candidate_{reason}" for reason in candidate["truncation_reasons"]
        ]
        if retired_section["truncated"]:
            truncated_reasons.append("retired_ids")
        return {
            "format": "one_line_per_record",
            "line_format": (
                "id | kind | status | scope | title | summary | "
                "evidence=<count> | updated=<iso8601>"
            ),
            "fields": [
                "id", "kind", "status", "scope", "title", "summary",
                "evidence_count", "updated_at",
            ],
            "reserved_fields": ["links"],
            "project_id": project_id,
            "active": active,
            "candidate": candidate,
            "retired": retired_section,
            "limits": dict(limits),
            "truncated": bool(truncated_reasons),
            "truncation_reasons": truncated_reasons,
        }

    def memory_review(self, task_id, phase_id=None):
        task = self.task_read(task_id)
        phase = None
        if phase_id is not None:
            phase = self._record(phase_id, "phase")
            if phase["task_id"] != task_id or phase["project_id"] != task["project_id"]:
                raise DiagnosticError("SCOPE_DENIED", "Phase belongs to a different task.")
            state = phase.get("consolidation_status", "pending")
            query = f"{task['title']} {phase['title']}"
            review = {"state": state, "task": task, "phase": phase}
        else:
            state = task.get("consolidation_status", "pending"
                             if task["status"] == "completed" else "not_due")
            query = task["title"]
            review = {"state": state, "task": task}
        review["existing_knowledge"] = self.memory_search(
            task["project_id"], query=query
        )["items"]
        review["instructions"] = (
            "Host reviews evidence, searches user wording and bilingual aliases for duplicates, "
            "and writes candidates or corrections. Reusable knowledge should end with a concise "
            "Retrieval cues line. No model has run here."
        )
        review["consolidation"] = self._consolidation_package(task, phase)
        return review

    def _consolidation_package(self, task, phase=None):
        """Build one bounded, read-only consolidation package; never writes or calls a model."""
        project_id = task["project_id"]
        limits = CONSOLIDATION_LIMITS
        if phase is not None:
            state = phase.get("consolidation_status", "pending")
        else:
            state = task.get("consolidation_status", "pending"
                             if task["status"] == "completed" else "not_due")
        checkpoints = self._checkpoint_metadata(task["id"])
        recent_items = []
        for checkpoint in reversed(checkpoints):
            findings = checkpoint.get("findings") or ""
            evidence = record_evidence(checkpoint)
            compact = _compact_evidence(
                evidence, limits["checkpoint_evidence_items"],
                reference_limit=300, summary_limit=200,
            )
            recent_items.append({
                "id": checkpoint["id"],
                "sequence": checkpoint.get("sequence"),
                "created_at": checkpoint.get("created_at"),
                "findings": _bounded_text(findings, limits["checkpoint_findings_chars"]),
                "findings_length": len(findings),
                "findings_truncated": len(findings) > limits["checkpoint_findings_chars"],
                "evidence": compact,
                "evidence_count": len(evidence),
                "evidence_truncated": len(evidence) > len(compact),
            })
        recent_items = _bounded_json_entries(
            recent_items, limits["recent_checkpoints"], limits["recent_checkpoint_chars"]
        )

        knowledge = [
            record for record in self.vault.list_record_metadata("memory", project_id=project_id)
            if record.get("project_id") == project_id
        ]

        def summarize(record):
            evidence = record_evidence(record)
            compact = _compact_evidence(evidence, limits["knowledge_evidence_items"], 200, 120)
            sources = record.get("sources") if isinstance(record.get("sources"), list) else []
            compact_sources = [
                {"type": source.get("type"), "reference": _bounded_text(source.get("reference"), 200)}
                for source in sources[:limits["knowledge_sources"]]
                if isinstance(source, dict)
            ]
            return {
                "id": record["id"],
                "title": _bounded_text(record.get("title"), limits["knowledge_title_chars"]),
                "kind": record.get("kind"),
                "status": record.get("status"),
                "updated_at": record.get("updated_at"),
                "evidence": compact,
                "evidence_count": len(evidence),
                "evidence_truncated": len(evidence) > len(compact),
                "sources": compact_sources,
                "source_count": len(sources),
                "sources_truncated": len(sources) > len(compact_sources),
            }

        active_records = sorted(
            (record for record in knowledge if record.get("status") == "active"),
            key=lambda record: (record.get("updated_at", ""), record["id"]),
            reverse=True,
        )
        candidate_records = sorted(
            (record for record in knowledge if record.get("status") == "candidate"),
            key=lambda record: (record.get("created_at", ""), record["id"]),
        )
        active_summaries = _bounded_json_entries(
            [summarize(record) for record in active_records[:limits["knowledge_per_status"]]],
            limits["knowledge_per_status"], limits["active_knowledge_chars"],
        )
        candidate_summaries = _bounded_json_entries(
            [summarize(record) for record in candidate_records[:limits["knowledge_per_status"]]],
            limits["knowledge_per_status"], limits["candidate_knowledge_chars"],
        )

        phases = sorted(
            (
                item for item in self.vault.list_record_metadata("phase", project_id=project_id)
                if item.get("consolidation_status", "pending") == "pending"
            ),
            key=lambda item: (item.get("created_at", ""), item["id"]),
        )
        pending_phase_items = _bounded_json_entries([{
            "id": item["id"], "task_id": item["task_id"], "title": item["title"],
            "status": item.get("status"), "revision": item.get("revision"),
            "consolidation_status": item.get("consolidation_status", "pending"),
            "created_at": item.get("created_at"),
        } for item in phases[:limits["pending_items"]]], limits["pending_items"], limits["pending_chars"])

        tasks = [
            item for item in self._task_metadata(project_id)
            if task_lifecycle_status(item) == "pending_review"
        ]
        pending_task_items = _bounded_json_entries([{
            "id": item["id"], "title": item["title"], "status": item.get("status"),
            "lifecycle": task_lifecycle_status(item),
            "task_kind": item.get("task_kind", "main"),
            "updated_at": item.get("updated_at"),
        } for item in tasks[:limits["pending_items"]]], limits["pending_items"], limits["pending_chars"])

        grouped = {}
        for record in knowledge:
            normalized = _normalized_title(record.get("title"))
            if normalized:
                grouped.setdefault(normalized, []).append(record)
        duplicate_groups = []
        for normalized, records in sorted(
            ((title, items) for title, items in grouped.items() if len(items) > 1),
            key=lambda item: (-len(item[1]), item[0]),
        ):
            duplicate_groups.append({
                "normalized_title": _bounded_text(normalized, limits["knowledge_title_chars"]),
                "ids": [record["id"] for record in records[:limits["duplicate_title_ids"]]],
                "titles": [
                    _bounded_text(record.get("title"), limits["knowledge_title_chars"])
                    for record in records[:limits["duplicate_title_ids"]]
                ],
                "statuses": [
                    record.get("status") for record in records[:limits["duplicate_title_ids"]]
                ],
                "count": len(records),
            })
        duplicate_total = len(duplicate_groups)
        duplicate_groups = _bounded_json_entries(
            duplicate_groups, limits["duplicate_title_groups"], limits["lint_chars"]
        )
        without_evidence = sorted(
            record["id"] for record in (*active_records, *candidate_records)
            if not record_evidence(record)
        )
        without_evidence_ids = without_evidence[:limits["knowledge_ids_without_evidence"]]
        oldest_candidate_at = (
            min((record.get("created_at", "") for record in candidate_records), default=None) or None
        )
        retired_count = sum(record.get("status") == "retired" for record in knowledge)

        duplicate_truncated = duplicate_total > len(duplicate_groups)
        without_evidence_truncated = len(without_evidence) > len(without_evidence_ids)
        lint_notes = []
        if duplicate_total:
            lint_notes.append(f"{duplicate_total} normalized title group(s) repeat in this project.")
        if without_evidence:
            lint_notes.append(
                f"{len(without_evidence)} active/candidate knowledge record(s) have no evidence pointers."
            )
        if candidate_records:
            lint_notes.append(
                f"{len(candidate_records)} candidate(s) await explicit review; oldest {oldest_candidate_at}."
            )
        if retired_count:
            lint_notes.append(f"{retired_count} retired record(s) stay out of default recall.")
        if not lint_notes:
            lint_notes.append("No duplicate titles, unsourced knowledge, candidate backlog or retired records.")
        lint_truncation_notes = []
        if duplicate_truncated:
            lint_truncation_notes.append(
                f"Duplicate title groups truncated to {len(duplicate_groups)} of {duplicate_total}."
            )
        if without_evidence_truncated:
            lint_truncation_notes.append(
                f"Unsourced knowledge IDs truncated to {len(without_evidence_ids)} "
                f"of {len(without_evidence)}."
            )
        lint = {
            "duplicate_titles": {
                "group_count": duplicate_total,
                "groups": duplicate_groups,
                "groups_returned": len(duplicate_groups),
                "truncated": duplicate_truncated,
            },
            "knowledge_without_evidence": {
                "scope": "active+candidate",
                "count": len(without_evidence),
                "ids": without_evidence_ids,
                "truncated": without_evidence_truncated,
            },
            "candidate_backlog": {
                "count": len(candidate_records),
                "oldest_created_at": oldest_candidate_at,
            },
            "retired": {"count": retired_count},
            "notes": lint_notes,
            "truncation_notes": lint_truncation_notes,
            "truncated": duplicate_truncated or without_evidence_truncated,
        }

        truncated_reasons = []
        if len(checkpoints) > len(recent_items):
            truncated_reasons.append("recent_checkpoints")
        if len(active_records) > len(active_summaries):
            truncated_reasons.append("active")
        if len(candidate_records) > len(candidate_summaries):
            truncated_reasons.append("candidate")
        if len(phases) > len(pending_phase_items):
            truncated_reasons.append("pending_phases")
        if len(tasks) > len(pending_task_items):
            truncated_reasons.append("pending_consolidation")
        if lint["truncated"]:
            truncated_reasons.append("lint")
        knowledge_index = self._knowledge_index_view(project_id)
        truncated_reasons.extend(knowledge_index["truncation_reasons"])

        return {
            "task_id": task["id"],
            "phase_id": phase["id"] if phase is not None else None,
            "state": state,
            "recent_checkpoints": {
                "items": recent_items,
                "total": len(checkpoints),
                "returned": len(recent_items),
                "limit": limits["recent_checkpoints"],
                "truncated": len(checkpoints) > len(recent_items),
            },
            "existing_knowledge": {
                "active": active_summaries,
                "candidate": candidate_summaries,
                "active_total": len(active_records),
                "candidate_total": len(candidate_records),
                "limit_per_status": limits["knowledge_per_status"],
                "active_truncated": len(active_records) > len(active_summaries),
                "candidate_truncated": len(candidate_records) > len(candidate_summaries),
            },
            "pending_phases": {
                "items": pending_phase_items,
                "total": len(phases),
                "returned": len(pending_phase_items),
                "limit": limits["pending_items"],
                "truncated": len(phases) > len(pending_phase_items),
            },
            "pending_consolidation": {
                "items": pending_task_items,
                "total": len(tasks),
                "returned": len(pending_task_items),
                "limit": limits["pending_items"],
                "truncated": len(tasks) > len(pending_task_items),
            },
            "lint": lint,
            "knowledge_index": knowledge_index,
            "limits": {
                "recent_checkpoints": limits["recent_checkpoints"],
                "checkpoint_findings_chars": limits["checkpoint_findings_chars"],
                "checkpoint_evidence_items": limits["checkpoint_evidence_items"],
                "knowledge_per_status": limits["knowledge_per_status"],
                "knowledge_title_chars": limits["knowledge_title_chars"],
                "knowledge_evidence_items": limits["knowledge_evidence_items"],
                "knowledge_sources": limits["knowledge_sources"],
                "pending_items": limits["pending_items"],
                "duplicate_title_groups": limits["duplicate_title_groups"],
                "duplicate_title_ids": limits["duplicate_title_ids"],
                "knowledge_ids_without_evidence": limits["knowledge_ids_without_evidence"],
                "serialized_char_budgets": {
                    "recent_checkpoints": limits["recent_checkpoint_chars"],
                    "active_knowledge": limits["active_knowledge_chars"],
                    "candidate_knowledge": limits["candidate_knowledge_chars"],
                    "pending": limits["pending_chars"],
                    "lint": limits["lint_chars"],
                },
                "knowledge_index": knowledge_index["limits"],
            },
            "truncated": bool(truncated_reasons),
            "truncated_reasons": truncated_reasons,
        }

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
        if phase_id is None:
            if task["status"] != "completed":
                raise DiagnosticError(
                    "TASK_NOT_COMPLETED",
                    "Task knowledge review can only be recorded after the task is completed.",
                )
            if self.execution.current(task_id) is not None:
                self._require_execution_claim(task_id)
        else:
            self._require_execution_claim(task_id)
        for memory_id in memory_ids:
            memory = self.memory_read(memory_id)
            if memory.get("project_id") != task["project_id"]:
                raise DiagnosticError("SCOPE_DENIED", "Reviewed knowledge must belong to this project.")
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
        execution_root=None, include_group_knowledge=False, include_knowledge_index=False,
    ):
        from memweave.context import build_context
        return build_context(
            self, project_id, task_id, query, include_user, budget,
            execution_root=execution_root,
            include_group_knowledge=include_group_knowledge,
            include_knowledge_index=include_knowledge_index,
        )

    def _map(self):
        from memweave.project_map import ProjectMap
        if self._project_map is None:
            self._project_map = ProjectMap(self.vault)
        return self._project_map

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

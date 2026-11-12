import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

from memweave.diagnostics import Diagnostics, DiagnosticError, validate_text


MAX_RECORD_BYTES = 262144
MAX_EVIDENCE_ITEMS = 20
MAX_EVIDENCE_CHARS = 20000
EVIDENCE_KINDS = ("user", "file", "command", "test", "url", "host")
EVIDENCE_FIELD_LIMITS = {"reference": 500, "hash": 128, "observed_at": 64, "summary": 500}
EVIDENCE_OPTIONAL_FIELDS = ("hash", "observed_at", "summary")
RECORD_DIRECTORIES = (
    "projects", "tasks", "work_units", "phases", "checkpoints", "knowledge",
    "architecture", "records", "users"
)
RECORD_DIRECTORY_BY_PREFIX = {
    "project": "projects",
    "task": "tasks",
    "work_unit": "work_units",
    "phase": "phases",
    "checkpoint": "checkpoints",
    "memory": "knowledge",
    "architecture": "architecture",
}
TASK_STATUSES = ("active", "queued", "current", "side", "completed", "cancelled")
WORK_UNIT_STATUSES = ("pending", "running", "blocked", "completed", "cancelled")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def contract_digest(goal, acceptance):
    payload = json.dumps(
        {"goal": str(goal), "acceptance": [str(item) for item in acceptance]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return digest(payload)


def encode_record(record: dict) -> str:
    metadata = {key: value for key, value in record.items() if key not in ("body", "path", "etag")}
    return "---\n" + json.dumps(metadata, ensure_ascii=False, indent=2) + "\n---\n" + record.get("body", "")


def _evidence_size(evidence: list) -> int:
    return len(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")))


def check_evidence(value) -> list[dict]:
    """Validate the evidence-pointer schema without rewriting the caller's values."""
    if not isinstance(value, list) or len(value) > MAX_EVIDENCE_ITEMS:
        raise DiagnosticError(
            "RECORD_INVALID", f"evidence must be a list of at most {MAX_EVIDENCE_ITEMS} pointers."
        )
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise DiagnosticError("RECORD_INVALID", f"evidence[{index}] must be an object.")
        if item.get("kind") not in EVIDENCE_KINDS:
            raise DiagnosticError(
                "RECORD_INVALID",
                f"evidence[{index}].kind must be one of user/file/command/test/url/host.",
            )
        reference = item.get("reference")
        if not isinstance(reference, str) or not reference.strip():
            raise DiagnosticError("RECORD_INVALID", f"evidence[{index}].reference is required.")
        for field, limit in EVIDENCE_FIELD_LIMITS.items():
            field_value = item.get(field)
            if field_value is not None and (
                not isinstance(field_value, str) or len(field_value) > limit
            ):
                raise DiagnosticError(
                    "RECORD_INVALID",
                    f"evidence[{index}].{field} must be text of at most {limit} characters.",
                )
    if _evidence_size(value) > MAX_EVIDENCE_CHARS:
        raise DiagnosticError(
            "RECORD_INVALID", f"Serialized evidence exceeds {MAX_EVIDENCE_CHARS} characters."
        )
    return list(value)


def normalize_evidence(value) -> list[dict]:
    """Validate caller input and return compact, canonical evidence pointers."""
    if not isinstance(value, list):
        raise DiagnosticError(
            "RECORD_INVALID", f"evidence must be a list of at most {MAX_EVIDENCE_ITEMS} pointers."
        )
    normalized = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise DiagnosticError("RECORD_INVALID", f"evidence[{index}] must be an object.")
        kind, reference = item.get("kind"), item.get("reference")
        if not isinstance(kind, str) or not isinstance(reference, str):
            raise DiagnosticError(
                "RECORD_INVALID", f"evidence[{index}] needs a textual kind and reference."
            )
        entry = {"kind": kind.strip(), "reference": reference.strip()}
        for field in EVIDENCE_OPTIONAL_FIELDS:
            field_value = item.get(field)
            if field_value is None:
                continue
            if not isinstance(field_value, str):
                raise DiagnosticError(
                    "RECORD_INVALID", f"evidence[{index}].{field} must be text."
                )
            if field_value.strip():
                entry[field] = field_value.strip()
        normalized.append(entry)
    check_evidence(normalized)
    return normalized


def record_evidence(record: dict) -> list[dict]:
    """Return stored evidence pointers; legacy records without the field read as []."""
    value = record.get("evidence")
    return value if isinstance(value, list) else []


def _validate_metadata(record: dict) -> dict:
    if not isinstance(record, dict):
        raise ValueError("Invalid metadata")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", record["id"]):
        raise ValueError("Invalid record id")
    if type(record["revision"]) is not int or record["revision"] < 1:
        raise ValueError("Invalid revision")
    if record["type"] not in (
        "project", "task", "work_unit", "phase", "checkpoint", "memory", "architecture"
    ):
        raise ValueError("Invalid record type")
    # "task" remains valid for checkpoint/phase/work_unit records and for reading
    # legacy task-scope knowledge during startup migration; memory writes no longer use it.
    if record["scope"] not in ("user", "project", "group", "task"):
        raise ValueError("Invalid scope")
    if record["scope"] != "user" and not isinstance(record.get("project_id"), str):
        raise ValueError("Project scope requires project_id")
    if not isinstance(record.get("title"), str) or not record["title"].strip():
        raise ValueError("Missing title")
    if record["type"] == "project" and not isinstance(record.get("root"), str):
        raise ValueError("Missing project root")
    if record["type"] == "task" and (
        record.get("status") not in TASK_STATUSES
        or not isinstance(record.get("goal"), str)
        or not isinstance(record.get("acceptance"), list)
        or any(not isinstance(item, str) for item in record["acceptance"])
    ):
        raise ValueError("Invalid task metadata")
    if record["type"] == "work_unit" and (
        record.get("scope") != "task"
        or not isinstance(record.get("task_id"), str)
        or record.get("status") not in WORK_UNIT_STATUSES
        or not isinstance(record.get("owner"), str)
    ):
        raise ValueError("Invalid work unit metadata")
    if record["type"] == "phase" and (
        record.get("scope") != "task"
        or not isinstance(record.get("task_id"), str)
        or record.get("status") != "completed"
        or not isinstance(record.get("summary"), str)
    ):
        raise ValueError("Invalid phase metadata")
    if record["type"] == "checkpoint" and (
        record.get("scope") != "task"
        or not isinstance(record.get("task_id"), str)
        or type(record.get("sequence")) is not int
        or record["sequence"] < 1
    ):
        raise ValueError("Invalid checkpoint metadata")
    if "evidence" in record:
        check_evidence(record["evidence"])
    return record


def decode_record(content: bytes) -> dict:
    if len(content) > MAX_RECORD_BYTES:
        raise DiagnosticError("RECORD_INVALID", "Record exceeds the 256 KiB limit.")
    try:
        text = content.decode("utf-8-sig").replace("\r\n", "\n")
        if not text.startswith("---\n"):
            raise ValueError("Missing frontmatter")
        header, body = text[4:].split("\n---\n", 1)
        record = _validate_metadata(json.loads(header))
        record["body"] = body
        return record
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, RecursionError) as error:
        raise DiagnosticError("RECORD_INVALID", "Invalid Markdown record; preserve it and repair its metadata.") from error


class Vault:
    def __init__(self, home: Path):
        self.home = Path(home).expanduser().resolve()
        self.diagnostics = Diagnostics(self.home)
        self.lock = FileLock(self.home / "runtime" / "write.lock", timeout=5)

    @contextmanager
    def locked(self):
        (self.home / "runtime").mkdir(parents=True, exist_ok=True)
        try:
            with self.lock:
                yield
        except Timeout as error:
            raise DiagnosticError("LOCK_BUSY", "Another writer is busy; retry with the same operation_id.") from error

    def initialize(self) -> dict:
        self.diagnostics.initialize()
        with self.locked():
            with self.diagnostics._connect(writable=True) as connection:
                connection.execute("DROP TABLE IF EXISTS records")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, "
                    "state TEXT NOT NULL, changes TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                connection.execute("CREATE TABLE IF NOT EXISTS tombstones (id TEXT PRIMARY KEY)")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS files (project_id TEXT NOT NULL, path TEXT NOT NULL, "
                    "hash TEXT NOT NULL, size INTEGER NOT NULL, PRIMARY KEY(project_id, path))"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS scans (project_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, "
                    "scanned_at TEXT NOT NULL, coverage TEXT NOT NULL)"
                )
                connection.execute("PRAGMA user_version = 2")
            return self._recover_locked()

    def target(self, relative: str) -> Path:
        candidate = self.home / relative
        resolved = candidate.resolve()
        if Path(relative).is_absolute() or not resolved.is_relative_to(self.home) or resolved == self.home:
            raise DiagnosticError("SCOPE_DENIED", "Record path is outside the memory home.")
        if candidate.is_symlink() or candidate.is_junction():
            raise DiagnosticError("SCOPE_DENIED", "Record links are not writable.")
        parent = candidate.parent
        while parent != self.home:
            if parent.is_symlink() or parent.is_junction():
                raise DiagnosticError("SCOPE_DENIED", "Record directory links are not writable.")
            parent = parent.parent
        return candidate

    def _file_hash(self, relative: str) -> str | None:
        target = self.target(relative)
        if not target.exists():
            return None
        if not target.is_file() or target.stat().st_size > MAX_RECORD_BYTES:
            raise DiagnosticError("RECORD_INVALID", "Record is not a bounded regular file.")
        return digest(target.read_bytes())

    def _find_record_path(self, record_id: str) -> str | None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", record_id):
            return None
        preferred = next((
            directory for prefix, directory in sorted(
                RECORD_DIRECTORY_BY_PREFIX.items(), key=lambda item: len(item[0]), reverse=True
            )
            if record_id.startswith(prefix + "_")
        ), None)
        directories = [preferred, *RECORD_DIRECTORIES] if preferred else list(RECORD_DIRECTORIES)
        seen = set()
        for directory in directories:
            if directory is None or directory in seen:
                continue
            seen.add(directory)
            relative = f"{directory}/{record_id}.md"
            try:
                target = self.target(relative)
            except DiagnosticError:
                continue
            if target.is_file():
                return Path(relative).as_posix()
        return None

    def _read_relative(self, relative: str) -> dict:
        target = self.target(relative)
        try:
            if not target.is_file() or target.stat().st_size > MAX_RECORD_BYTES:
                raise DiagnosticError("RECORD_INVALID", "Record exceeds the 256 KiB limit.")
            content = target.read_bytes()
        except FileNotFoundError as error:
            raise DiagnosticError("SOURCE_CHANGED", "Record was removed externally; rebuild the index.") from error
        except OSError as error:
            raise DiagnosticError("SOURCE_CHANGED", "Record is unreadable; preserve it and inspect the file.") from error
        record = decode_record(content)
        record.update(path=Path(relative).as_posix(), etag=digest(content))
        return record

    def _record_paths(self, record_type: str | None = None):
        if record_type == "memory":
            directories = ("knowledge", "records", "users")
        elif record_type is None:
            directories = RECORD_DIRECTORIES
        else:
            directory = RECORD_DIRECTORY_BY_PREFIX.get(record_type)
            directories = (directory,) if directory is not None else ()
        for directory in directories:
            root = self.home / directory
            if not root.is_dir():
                continue
            for target in sorted(root.glob("*.md")):
                try:
                    relative = target.relative_to(self.home).as_posix()
                    self.target(relative)
                except (OSError, ValueError, DiagnosticError):
                    continue
                yield relative

    def counts(self) -> dict:
        counts = {}
        for directory in RECORD_DIRECTORIES:
            root = self.home / directory
            if not root.is_dir():
                continue
            record_type = {
                "projects": "project",
                "tasks": "task",
                "work_units": "work_unit",
                "phases": "phase",
                "checkpoints": "checkpoint",
                "knowledge": "memory",
                "architecture": "architecture",
                "records": "memory",
                "users": "memory",
            }[directory]
            count = sum(1 for target in root.glob("*.md") if target.is_file())
            counts[record_type] = counts.get(record_type, 0) + count
        return counts

    def read(self, record_id: str) -> dict:
        with self.locked():
            self._recover_locked()
            with self.diagnostics._connect() as connection:
                deleted = connection.execute(
                    "SELECT 1 FROM tombstones WHERE id = ?", (record_id,)
                ).fetchone()
            relative = self._find_record_path(record_id)
            if relative is None or deleted is not None:
                raise DiagnosticError("RECORD_NOT_FOUND", "Record not found in this memory home.")
            record = self._read_relative(relative)
            if record["id"] != record_id:
                raise DiagnosticError("RECORD_INVALID", "Record identity changed; rebuild or repair the file.")
            return record

    def read_path(self, relative: str) -> dict:
        """Read one current Markdown record by its vault-relative path."""
        with self.locked():
            self._recover_locked()
            record = self._read_relative(relative)
            with self.diagnostics._connect() as connection:
                if connection.execute("SELECT 1 FROM tombstones WHERE id = ?", (record["id"],)).fetchone():
                    raise DiagnosticError("RECORD_DELETED", "Record was explicitly forgotten.")
            return record

    def _read_metadata_relative(self, relative: str) -> dict:
        """Read frontmatter only; the record body is neither loaded nor returned."""
        target = self.target(relative)
        try:
            if not target.is_file() or target.stat().st_size > MAX_RECORD_BYTES:
                raise DiagnosticError("RECORD_INVALID", "Record exceeds the 256 KiB limit.")
            header = bytearray()
            with target.open("rb") as stream:
                if stream.readline() not in (b"---\n", b"---\r\n"):
                    raise ValueError("Missing frontmatter")
                while len(header) <= 128 * 1024:
                    line = stream.readline()
                    if line in (b"---\n", b"---\r\n"):
                        break
                    if not line:
                        raise ValueError("Unterminated frontmatter")
                    header.extend(line)
                else:
                    raise ValueError("Frontmatter is too large")
            record = _validate_metadata(json.loads(header.decode("utf-8-sig")))
            record["path"] = Path(relative).as_posix()
            return record
        except DiagnosticError:
            raise
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, RecursionError) as error:
            raise DiagnosticError(
                "RECORD_INVALID", "Invalid Markdown record; preserve it and repair its metadata."
            ) from error

    def file_etag(self, relative: str) -> str:
        """Calculate one record etag without retaining its body."""
        with self.locked():
            self._recover_locked()
            target = self.target(relative)
            if not target.is_file() or target.stat().st_size > MAX_RECORD_BYTES:
                raise DiagnosticError("RECORD_INVALID", "Record exceeds the 256 KiB limit.")
            value = hashlib.sha256()
            with target.open("rb") as stream:
                while chunk := stream.read(65536):
                    value.update(chunk)
            return value.hexdigest()

    def list_records(self, record_type: str, project_id: str | None = None, scope: str | None = None) -> list[dict]:
        with self.locked():
            self._recover_locked()
            with self.diagnostics._connect() as connection:
                deleted = {row[0] for row in connection.execute("SELECT id FROM tombstones")}
            records = []
            seen = set()
            for relative in self._record_paths(record_type):
                record = self._read_relative(relative)
                if record["id"] in deleted:
                    continue
                if record["id"] in seen:
                    raise DiagnosticError("RECORD_INVALID", "Duplicate record identity.")
                seen.add(record["id"])
                if record["type"] != record_type:
                    continue
                if project_id is not None and record.get("project_id") != project_id:
                    continue
                if scope is not None and record.get("scope") != scope:
                    continue
                records.append(record)
            records.sort(key=lambda item: item["id"])
            return records[:1000]

    def list_record_metadata(
        self, record_type: str, project_id: str | None = None, scope: str | None = None
    ) -> list[dict]:
        """List matching frontmatter without loading task bodies into the context path."""
        with self.locked():
            self._recover_locked()
            with self.diagnostics._connect() as connection:
                deleted = {row[0] for row in connection.execute("SELECT id FROM tombstones")}
            records = []
            seen = set()
            for relative in self._record_paths(record_type):
                record = self._read_metadata_relative(relative)
                if record["id"] in deleted:
                    continue
                if record["id"] in seen:
                    raise DiagnosticError("RECORD_INVALID", "Duplicate record identity.")
                seen.add(record["id"])
                if record["type"] != record_type:
                    continue
                if project_id is not None and record.get("project_id") != project_id:
                    continue
                if scope is not None and record.get("scope") != scope:
                    continue
                records.append(record)
            records.sort(key=lambda item: item["id"])
            return records[:4000]

    def _apply_change(self, change: dict) -> None:
        target = self.target(change["path"])
        if change["after"] is None:
            target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = self.home / "runtime" / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        temporary = staging / (uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(change["after"].encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _finish(self, operation_id: str, changes: list[dict]) -> None:
        for change in changes:
            current = self._file_hash(change["path"])
            after_hash = digest(change["after"].encode("utf-8")) if change["after"] is not None else None
            if current not in (change["before"], after_hash):
                with self.diagnostics._connect(writable=True) as connection:
                    connection.execute("UPDATE operations SET state = 'conflict' WHERE id = ?", (operation_id,))
                raise DiagnosticError("RECOVERY_REQUIRED", "Interrupted write conflicts with an external edit; no content overwritten.")
        for change in changes:
            after_hash = digest(change["after"].encode("utf-8")) if change["after"] is not None else None
            current = self._file_hash(change["path"])
            if current not in (change["before"], after_hash):
                with self.diagnostics._connect(writable=True) as connection:
                    connection.execute("UPDATE operations SET state = 'conflict' WHERE id = ?", (operation_id,))
                raise DiagnosticError("RECOVERY_REQUIRED", "External edit detected before replacement; preserve both versions.")
            if current != after_hash:
                self._apply_change(change)
        with self.diagnostics._connect(writable=True) as connection:
            for change in changes:
                if change["after"] is None:
                    connection.execute("INSERT OR IGNORE INTO tombstones VALUES (?)", (change["id"],))
            connection.execute(
                "UPDATE operations SET state = 'completed', changes = '[]' WHERE id = ?", (operation_id,)
            )

    def _recover_locked(self) -> dict:
        with self.diagnostics._connect() as connection:
            operations = connection.execute(
                "SELECT id, changes FROM operations WHERE state != 'completed' ORDER BY created_at, id"
            ).fetchall()
        for operation in operations:
            self._finish(operation["id"], json.loads(operation["changes"]))
        return {"recovered": len(operations)}

    def recover(self) -> dict:
        with self.locked():
            return self._recover_locked()

    def recovery_status(self) -> dict:
        with self.locked(), self.diagnostics._connect() as connection:
            operations = connection.execute(
                "SELECT id, state, changes FROM operations WHERE state != 'completed' ORDER BY created_at"
            ).fetchall()
            items = []
            for operation in operations:
                changes = json.loads(operation["changes"])
                items.append({
                    "operation_id": operation["id"], "state": operation["state"],
                    "files": [{
                        "path": change["path"], "before_etag": change["before"],
                        "current_etag": self._file_hash(change["path"]),
                        "proposed_etag": digest(change["after"].encode("utf-8")) if change["after"] is not None else None,
                        "proposed_record": decode_record(change["after"].encode("utf-8")) if change["after"] is not None else None,
                    } for change in changes],
                })
            return {"items": items}

    def resolve(self, operation_id: str, expected_files: dict) -> dict:
        validate_text(operation_id, "operation_id", 128)
        if not isinstance(expected_files, dict):
            raise DiagnosticError("RECORD_INVALID", "expected_files must map every pending path to its current etag.")
        with self.locked():
            with self.diagnostics._connect() as connection:
                operation = connection.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
            if operation is None:
                raise DiagnosticError("RECORD_NOT_FOUND", "Pending operation not found.")
            if operation["state"] == "completed":
                previous = json.loads(operation["result"])
                if previous.get("abandoned"):
                    return previous
                raise DiagnosticError("VERSION_CONFLICT", "Completed operations cannot be abandoned.")
            changes = json.loads(operation["changes"])
            if set(expected_files) != {change["path"] for change in changes}:
                raise DiagnosticError("VERSION_CONFLICT", "Confirm the current etag of every pending file.")
            preserved = []
            for change in changes:
                relative = change["path"]
                current = self._file_hash(relative)
                if expected_files[relative] != current:
                    raise DiagnosticError("VERSION_CONFLICT", "External content changed again; inspect recovery status.")
                content = self.target(relative).read_bytes().decode("utf-8-sig") if current is not None else None
                if content is not None:
                    record = decode_record(content.encode("utf-8"))
                    if record["id"] != change["id"]:
                        raise DiagnosticError("RECORD_INVALID", "Preserved record identity must match the pending file.")
                    with self.diagnostics._connect() as connection:
                        if connection.execute("SELECT 1 FROM tombstones WHERE id = ?", (record["id"],)).fetchone():
                            raise DiagnosticError("RECORD_DELETED", "Cannot restore forgotten records.")
                preserved.append((relative, content, change["id"]))
            output = {"operation_id": operation_id, "abandoned": True, "preserved_external": True}
            with self.diagnostics._connect(writable=True) as connection:
                connection.execute("UPDATE operations SET state = 'completed', changes = '[]', result = ? WHERE id = ?",
                                   (json.dumps(output), operation_id))
            return output

    def commit(self, operation_id: str, request: dict, build) -> dict:
        validate_text(operation_id, "operation_id", 128)
        request_hash = digest(json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        with self.locked():
            self._recover_locked()
            with self.diagnostics._connect() as connection:
                existing = connection.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise DiagnosticError("VERSION_CONFLICT", "operation_id was used for a different request.")
                return json.loads(existing["result"])
            entries, response = build()
            changes = []
            for entry in entries:
                relative = entry["path"]
                actual_hash = self._file_hash(relative)
                if actual_hash != entry["expected"]:
                    raise DiagnosticError("VERSION_CONFLICT", "The file changed; read its latest revision and etag.")
                record = entry["record"]
                record_id = record["id"] if record else entry["id"]
                with self.diagnostics._connect() as connection:
                    if record and connection.execute("SELECT 1 FROM tombstones WHERE id = ?", (record_id,)).fetchone():
                        raise DiagnosticError("RECORD_DELETED", "Deleted record identities cannot be recreated.")
                existing_path = self._find_record_path(record_id)
                if existing_path is not None and existing_path != relative:
                    raise DiagnosticError("VERSION_CONFLICT", "Record identity and path must remain paired.")
                after = encode_record(record) if record else None
                if after is not None:
                    decode_record(after.encode("utf-8"))
                changes.append({"path": relative, "before": actual_hash, "after": after, "id": record_id})
            with self.diagnostics._connect(writable=True) as connection:
                connection.execute(
                    "INSERT INTO operations VALUES (?, ?, 'prepared', ?, ?, ?)",
                    (operation_id, request_hash, json.dumps(changes, ensure_ascii=False),
                     json.dumps(response, ensure_ascii=False), now()),
                )
            self._finish(operation_id, changes)
            return response

    def rebuild(self) -> dict:
        with self.locked():
            self._recover_locked()
            records = []
            errors = []
            seen = set()
            with self.diagnostics._connect() as connection:
                deleted = {row[0] for row in connection.execute("SELECT id FROM tombstones")}
            for directory in RECORD_DIRECTORIES:
                root = self.home / directory
                if not root.exists():
                    continue
                for target in root.rglob("*.md"):
                    try:
                        relative = target.relative_to(self.home).as_posix()
                        self.target(relative)
                        if target.stat().st_size > MAX_RECORD_BYTES:
                            raise DiagnosticError("RECORD_INVALID", "Record too large.")
                        content = target.read_bytes()
                        record = decode_record(content)
                        if record["id"] in deleted:
                            raise DiagnosticError("RECORD_DELETED", "Ignored restored deleted record.")
                        if record["id"] in seen:
                            raise DiagnosticError("RECORD_INVALID", "Duplicate record identity.")
                        seen.add(record["id"])
                        records.append((relative, content.decode("utf-8"), record["id"]))
                    except (DiagnosticError, OSError) as error:
                        errors.append({"path": str(target.relative_to(self.home)), "error": str(error)})
            return {"indexed": len(records), "errors": errors}

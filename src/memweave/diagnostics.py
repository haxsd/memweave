import hashlib
import json
import sqlite3
import sys
import tomllib
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from memweave import __version__


CLIENT_BOOTSTRAP_CHANNELS = {
    "codex": "always_on_agents+lifecycle_hooks_best_effort",
    "cursor": "always_on_rule+lifecycle_hooks_best_effort",
    "devin": "always_on_rule",
    "generic": "manual_host_instruction_required",
}


class DiagnosticError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def result(data: dict) -> dict:
    return {
        "status": "ok",
        "data": data,
        "sources": [],
        "freshness": {"state": "unknown"},
        "coverage": {"state": "partial"},
        "next_actions": [],
    }


def validate_text(value: str, field: str, limit: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise DiagnosticError("RECORD_INVALID", f"{field} must contain 1–{limit} characters.")


class Diagnostics:
    def __init__(self, home: Path):
        self.home = Path(home).expanduser().resolve()
        self.database = self.home / "index" / "memory.sqlite3"
        self.config = self.home / "config.toml"

    def _validate_config(self) -> None:
        if not self.config.is_file():
            raise DiagnosticError("NOT_INITIALIZED", "Run memweave init --home <directory>.")
        try:
            with self.config.open("rb") as stream:
                config = tomllib.load(stream)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise DiagnosticError("CONFIG_INVALID", "Cannot read the memory home config.") from error
        if config.get("schema_version") != 1 or config.get("product") != "memweave":
            raise DiagnosticError("SCHEMA_UNSUPPORTED", "Unsupported memory home configuration.")

    @contextmanager
    def _connect(self, *, writable: bool = False):
        self._validate_config()
        if not self.database.is_file():
            raise DiagnosticError("NOT_INITIALIZED", "Database missing; run memweave init.")
        mode = "rw" if writable else "ro"
        try:
            with closing(sqlite3.connect(
                self.database.as_uri() + f"?mode={mode}", uri=True, timeout=3
            )) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                schema = connection.execute("PRAGMA user_version").fetchone()[0]
                if schema not in (1, 2):
                    raise DiagnosticError("SCHEMA_UNSUPPORTED", f"Unsupported database schema: {schema}.")
                with connection:
                    yield connection
        except sqlite3.OperationalError as error:
            code = "LOCK_BUSY" if "locked" in str(error).lower() else "STORAGE_ERROR"
            raise DiagnosticError(code, "Database unavailable; inspect the home or retry.") from error
        except sqlite3.DatabaseError as error:
            raise DiagnosticError("STORAGE_ERROR", "Database damaged or schema incomplete.") from error

    def initialize(self) -> dict:
        if self.config.exists():
            self._validate_config()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(self.database, timeout=3)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    schema = connection.execute("PRAGMA user_version").fetchone()[0]
                    if schema not in (0, 1, 2):
                        raise DiagnosticError("SCHEMA_UNSUPPORTED", f"Unsupported database schema: {schema}.")
                    if schema == 0:
                        connection.execute(
                            "CREATE TABLE probes (operation_id TEXT PRIMARY KEY, "
                            "marker TEXT NOT NULL, created_at TEXT NOT NULL)"
                        )
                        connection.execute(
                            "CREATE TABLE hook_receipts (receipt_id TEXT PRIMARY KEY, "
                            "client TEXT NOT NULL, event TEXT NOT NULL, metadata TEXT NOT NULL, "
                            "created_at TEXT NOT NULL)"
                        )
                        connection.execute("PRAGMA user_version = 1")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError as error:
            raise DiagnosticError("STORAGE_ERROR", "Cannot initialize the local database.") from error
        try:
            with self.config.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write('schema_version = 1\nproduct = "memweave"\n')
        except FileExistsError:
            self._validate_config()
        return self.status()

    def status(self) -> dict:
        initialized = self.config.is_file() and self.database.is_file()
        probe_count = 0
        receipt_count = 0
        product_ready = False
        if initialized:
            with self._connect() as connection:
                product_ready = connection.execute("PRAGMA user_version").fetchone()[0] == 2
                probe_count = connection.execute("SELECT COUNT(*) FROM probes").fetchone()[0]
                receipt_count = connection.execute("SELECT COUNT(*) FROM hook_receipts").fetchone()[0]
        return result({
            "product": "memweave",
            "version": __version__,
            "python": sys.version.split()[0],
            "mcp_sdk": version("mcp"),
            "home": str(self.home),
            "initialized": initialized,
            "phase": "local_mvp" if product_ready else "compatibility_foundation",
            "probe_count": probe_count,
            "hook_receipt_count": receipt_count,
            "clients": {
                "cursor": {
                    "bootstrap_channel": CLIENT_BOOTSTRAP_CHANNELS["cursor"],
                    "desktop_verification": "unverified",
                },
                "codex": {
                    "bootstrap_channel": CLIENT_BOOTSTRAP_CHANNELS["codex"],
                    "desktop_verification": "unverified",
                },
                "devin": {
                    "bootstrap_channel": CLIENT_BOOTSTRAP_CHANNELS["devin"],
                    "desktop_verification": "unverified",
                },
            },
            "task_memory": "available" if product_ready else "not_initialized",
            "long_term_memory": "available" if product_ready else "not_initialized",
            "project_graph": "available" if product_ready else "not_initialized",
        })

    def write_probe(self, operation_id: str, marker: str) -> dict:
        validate_text(operation_id, "operation_id", 128)
        validate_text(marker, "marker", 1024)
        with self._connect(writable=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT operation_id, marker, created_at FROM probes WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing:
                if existing["marker"] != marker:
                    raise DiagnosticError("VERSION_CONFLICT", "operation_id already has another marker.")
                return result(dict(existing))
            created_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "INSERT INTO probes VALUES (?, ?, ?)", (operation_id, marker, created_at)
            )
        return result({"operation_id": operation_id, "marker": marker, "created_at": created_at})

    def read_probe(self, operation_id: str) -> dict:
        validate_text(operation_id, "operation_id", 128)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT operation_id, marker, created_at FROM probes WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise DiagnosticError("PROBE_NOT_FOUND", "No marker for this operation_id; write one first.")
        return result(dict(row))

    def record_hook(self, client: str, event: str, payload: dict) -> dict:
        events = {
            "cursor": ("sessionStart", "sessionEnd"),
            "codex": ("SessionStart", "UserPromptSubmit", "PreCompact", "Interrupt", "SessionEnd"),
        }
        if event not in events.get(client, ()):
            raise DiagnosticError("UNSUPPORTED_CAPABILITY", "This client event has no verified adapter.")
        if not isinstance(payload, dict):
            raise DiagnosticError("RECORD_INVALID", "Hook input must be a JSON object.")
        metadata = {}
        for field in ("conversation_id", "session_id", "generation_id", "event_id", "turn_id", "tool_use_id"):
            if field in payload:
                if payload[field] is None:
                    continue
                if isinstance(payload[field], str) and not payload[field].strip():
                    continue
                validate_text(payload[field], field, 256)
                metadata[field] = payload[field]
        if not (metadata.get("conversation_id") or metadata.get("session_id")):
            raise DiagnosticError("RECORD_INVALID", "Hook input needs conversation_id or session_id.")
        serialized = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        receipt_id = hashlib.sha256(
            json.dumps([client, event, metadata], sort_keys=True).encode("utf-8")
        ).hexdigest()
        with self._connect(writable=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO hook_receipts VALUES (?, ?, ?, ?, ?)",
                (receipt_id, client, event, serialized, datetime.now(timezone.utc).isoformat()),
            )
        return result({
            "receipt_id": receipt_id,
            "event": event,
            "source": "hook_entrypoint",
            "desktop_verification": "unverified",
        })

    def hook_state(self, session_id: str) -> dict:
        if not self.database.is_file():
            return {}
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT state FROM hook_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            if row is None:
                return {}
            state = json.loads(row["state"])
            return state if isinstance(state, dict) else {}
        except (DiagnosticError, sqlite3.Error, ValueError, TypeError, RecursionError):
            return {}

    def record_knowledge_injections(
        self, project_id: str, task_id: str | None, sources: list, query: str = "",
    ) -> dict:
        """Record which knowledge records reached a prepare context.

        This is derived runtime telemetry, never canonical memory. It powers the
        read-only loop-health view and must not fail a prepare when storage is
        unavailable.
        """
        validate_text(project_id, "project_id", 100)
        memory_ids = []
        for source in sources or []:
            if not isinstance(source, dict) or source.get("type") != "memory":
                continue
            memory_id = source.get("id")
            validate_text(memory_id, "memory_id", 100)
            memory_ids.append((memory_id, str(source.get("reason") or "")[:80]))
        if not memory_ids:
            return {"recorded": 0}
        if task_id is not None:
            validate_text(task_id, "task_id", 100)
        injected_at = datetime.now(timezone.utc).isoformat()
        query_text = (query or "")[:1000]
        with self._connect(writable=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_injections ("
                "project_id TEXT NOT NULL, memory_id TEXT NOT NULL, "
                "last_injected_at TEXT NOT NULL, injection_count INTEGER NOT NULL, "
                "last_task_id TEXT, last_reason TEXT, last_query TEXT, "
                "PRIMARY KEY(project_id, memory_id))"
            )
            for memory_id, reason in memory_ids:
                connection.execute(
                    "INSERT INTO knowledge_injections "
                    "(project_id, memory_id, last_injected_at, injection_count, "
                    " last_task_id, last_reason, last_query) "
                    "VALUES (?, ?, ?, 1, ?, ?, ?) "
                    "ON CONFLICT(project_id, memory_id) DO UPDATE SET "
                    "last_injected_at=excluded.last_injected_at, "
                    "injection_count=knowledge_injections.injection_count + 1, "
                    "last_task_id=excluded.last_task_id, "
                    "last_reason=excluded.last_reason, "
                    "last_query=excluded.last_query",
                    (project_id, memory_id, injected_at, task_id, reason, query_text),
                )
        return {"recorded": len(memory_ids), "injected_at": injected_at}

    def knowledge_injections(self, project_id: str | None = None) -> dict:
        """Return bounded injection telemetry; absence means no observation yet."""
        empty = {
            "tracking_since": None,
            "last_injection_at": None,
            "injection_count": 0,
            "memory_count": 0,
            "items": [],
            "truncated": False,
        }
        if not self.database.is_file():
            return empty
        try:
            with self._connect() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'knowledge_injections'"
                ).fetchone()
                if exists is None:
                    return empty
                if project_id is None:
                    rows = connection.execute(
                        "SELECT project_id, memory_id, last_injected_at, injection_count, "
                        "last_task_id, last_reason, last_query "
                        "FROM knowledge_injections ORDER BY last_injected_at DESC LIMIT 201"
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT project_id, memory_id, last_injected_at, injection_count, "
                        "last_task_id, last_reason, last_query "
                        "FROM knowledge_injections WHERE project_id = ? "
                        "ORDER BY last_injected_at DESC LIMIT 201",
                        (project_id,),
                    ).fetchall()
        except (DiagnosticError, sqlite3.Error):
            return empty
        items = [dict(row) for row in rows]
        truncated = len(items) > 200
        items = items[:200]
        if not items:
            return empty
        return {
            "tracking_since": min(item["last_injected_at"] for item in items),
            "last_injection_at": max(item["last_injected_at"] for item in items),
            "injection_count": sum(int(item["injection_count"]) for item in items),
            "memory_count": len(items),
            "items": items,
            "truncated": truncated,
        }

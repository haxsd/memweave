"""Ephemeral execution ownership for one task driver.

The task itself remains Markdown-authoritative.  This module only coordinates
which live MCP process may mutate the task while another process is driving it.
WorkUnits are intentionally not leased: subagents share the parent task claim
and keep their own revision/etag concurrency control.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionClaimStore:
    """Process-local claim handles backed by cross-process file locks."""

    def __init__(self, home: Path):
        self.directory = Path(home).expanduser().resolve() / "runtime" / "execution"
        self._lock = threading.RLock()
        self._held: dict[str, dict] = {}

    def _paths(self, task_id: str) -> tuple[Path, Path]:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", task_id)[:120]
        digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]
        stem = f"{safe}-{digest}"
        return self.directory / f"{stem}.lock", self.directory / f"{stem}.json"

    def _read_claim(self, task_id: str) -> dict | None:
        _, claim_path = self._paths(task_id)
        try:
            value = json.loads(claim_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, RecursionError):
            return None
        if not isinstance(value, dict) or value.get("task_id") != task_id:
            return None
        return value

    def _write_claim(self, task_id: str, claim: dict) -> None:
        _, claim_path = self._paths(task_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = claim_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(claim, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(claim_path)

    def claim(self, task_id: str, project_id: str, requested_claim: str | None = None):
        """Return ``(claim, state)`` where state is acquired/delegated/claimed/invalid.

        ``claimed`` means another live process holds the file lock.  A requested
        claim is only accepted while its owner still holds the lock; stale claim
        files are not treated as authority.
        """
        with self._lock:
            local = self._held.get(task_id)
            if local is not None:
                if requested_claim is None or requested_claim == local["claim"]["claim_id"]:
                    return local["claim"], "acquired" if local["owns_lock"] else "delegated"
                return None, "invalid"

            existing = self._read_claim(task_id)
            if requested_claim is not None:
                if existing is None or existing.get("claim_id") != requested_claim:
                    return None, "invalid"
                lock_path, _ = self._paths(task_id)
                lock = FileLock(str(lock_path), timeout=0.05)
                try:
                    lock.acquire()
                except Timeout:
                    live = self._read_claim(task_id)
                    if live is None or live.get("claim_id") != requested_claim:
                        return None, "invalid"
                    self._held[task_id] = {
                        "claim": live,
                        "lock": None,
                        "owns_lock": False,
                    }
                    return live, "delegated"
                lock.release()
                return None, "invalid"

            lock_path, claim_path = self._paths(task_id)
            self.directory.mkdir(parents=True, exist_ok=True)
            lock = FileLock(str(lock_path), timeout=0.05)
            try:
                lock.acquire()
            except Timeout:
                return existing, "claimed"

            claim = {
                "claim_id": uuid4().hex,
                "task_id": task_id,
                "project_id": project_id,
                "owner": f"pid:{os.getpid()}:{uuid4().hex[:8]}",
                "claimed_at": _now(),
            }
            try:
                self._write_claim(task_id, claim)
            except (OSError, ValueError):
                lock.release()
                raise
            self._held[task_id] = {
                "claim": claim,
                "lock": lock,
                "owns_lock": True,
            }
            return claim, "acquired"

    def release(self, task_id: str) -> None:
        with self._lock:
            local = self._held.pop(task_id, None)
            if local is None:
                return
            if not local["owns_lock"]:
                return
            _, claim_path = self._paths(task_id)
            current = self._read_claim(task_id)
            if current and current.get("claim_id") == local["claim"]["claim_id"]:
                claim_path.unlink(missing_ok=True)
            local["lock"].release()

    def release_all(self) -> None:
        for task_id in list(self._held):
            self.release(task_id)

    def current(self, task_id: str) -> dict | None:
        return self._read_claim(task_id)

    def is_authorized(self, task_id: str) -> bool:
        local = self._held.get(task_id)
        if local is None:
            if self.current(task_id) is not None:
                return False
            lock_path, _ = self._paths(task_id)
            lock = FileLock(str(lock_path), timeout=0.01)
            try:
                lock.acquire()
            except Timeout:
                return False
            lock.release()
            return True
        current = self.current(task_id)
        return current is not None and current.get("claim_id") == local["claim"]["claim_id"]

    def public_status(self, task_id: str) -> dict | None:
        current = self.current(task_id)
        if current is None:
            return None
        return {
            "state": "claimed",
            "task_id": task_id,
            "claimed_at": current.get("claimed_at"),
        }

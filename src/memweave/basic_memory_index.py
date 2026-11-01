"""Synchronous lifecycle for the single-process Basic Memory index."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from memweave.basic_memory_engine import BasicMemoryEngine


class BasicMemoryIndex:
    """Own one event loop and one Basic Memory engine for the process lifetime.

    MemWeave's MCP and storage layers are synchronous, while Basic Memory's
    repositories are async. Keeping the engine on one dedicated loop avoids
    reopening SQLite and, more importantly, avoids creating a second memory
    service just to bridge the two calling styles.
    """

    _instances: dict[tuple[Path, Path], "BasicMemoryIndex"] = {}
    _instance_users: dict[tuple[Path, Path], int] = {}
    _instances_lock = threading.RLock()

    def __init__(self, home: Path, data_dir: Path | None = None):
        self.home = Path(home).expanduser().resolve()
        self.data_dir = (
            Path(data_dir).expanduser().resolve()
            if data_dir is not None
            else self.home / "index" / "basic-memory"
        )
        self.key = (self.home, self.data_dir)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._engine: BasicMemoryEngine | None = None
        self._lock = threading.RLock()
        self.last_result: dict[str, Any] = {}

    @classmethod
    def acquire(cls, home: Path, data_dir: Path | None = None) -> "BasicMemoryIndex":
        """Return the process-wide index for one memory home and retain one user."""
        resolved_home = Path(home).expanduser().resolve()
        resolved_data = (
            Path(data_dir).expanduser().resolve()
            if data_dir is not None
            else resolved_home / "index" / "basic-memory"
        )
        key = (resolved_home, resolved_data)
        with cls._instances_lock:
            index = cls._instances.get(key)
            if index is None:
                index = cls(resolved_home, resolved_data)
                cls._instances[key] = index
            cls._instance_users[key] = cls._instance_users.get(key, 0) + 1
            return index

    @classmethod
    def release(cls, index: "BasicMemoryIndex") -> None:
        """Release one user and close the shared engine after the final user."""
        with cls._instances_lock:
            users = cls._instance_users.get(index.key, 0)
            if users <= 1:
                cls._instance_users.pop(index.key, None)
                cls._instances.pop(index.key, None)
                index.close()
            else:
                cls._instance_users[index.key] = users - 1

    @property
    def active(self) -> bool:
        return self._engine is not None and self._loop is not None and self._loop.is_running()

    def _run_loop(self, loop: asyncio.AbstractEventLoop, ready: threading.Event) -> None:
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    def _run_coroutine(self, coroutine: Coroutine[Any, Any, Any], timeout: float = 60):
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("Basic Memory index is not running.")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    def _stop_failed_start(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._loop is not None:
            self._loop.close()
        self._loop = None
        self._thread = None
        self._engine = None

    def start(self) -> dict[str, Any]:
        """Start the one index and return its initial Markdown synchronization result."""
        with self._lock:
            if self.active:
                return self.last_result
            loop = asyncio.new_event_loop()
            ready = threading.Event()
            thread = threading.Thread(
                target=self._run_loop,
                args=(loop, ready),
                name="memweave-basic-memory",
                daemon=True,
            )
            self._loop = loop
            self._thread = thread
            thread.start()
            if not ready.wait(timeout=5):
                self._stop_failed_start()
                raise RuntimeError("Basic Memory index event loop did not start.")
            engine = BasicMemoryEngine(
                self.home,
                self.data_dir,
                vault=self.home,
            )
            self._engine = engine
            try:
                self._run_coroutine(engine.start())
                self.last_result = self._run_coroutine(engine.index_vault(), timeout=120)
            except Exception:
                try:
                    self._run_coroutine(engine.close(), timeout=10)
                except Exception:
                    pass
                self._stop_failed_start()
                raise
            return self.last_result

    def close(self) -> None:
        with self._lock:
            engine = self._engine
            loop = self._loop
            thread = self._thread
            if engine is None or loop is None or thread is None:
                return
            try:
                self._run_coroutine(engine.close(), timeout=30)
            finally:
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=5)
                loop.close()
                self._engine = None
                self._loop = None
                self._thread = None

    def sync(self, paths: list[str]) -> list[dict[str, Any]]:
        """Upsert changed Markdown paths into the shared index."""
        if not self.active:
            return []
        engine = self._engine
        assert engine is not None
        return [
            self._run_coroutine(engine.index_markdown_file(path))
            for path in paths
            if (self.home / path).is_file()
        ]

    def remove(self, paths: list[str]) -> int:
        """Remove deleted Markdown paths from the derived index."""
        if not self.active:
            return 0
        engine = self._engine
        assert engine is not None
        removed = 0
        for path in paths:
            if self._run_coroutine(engine.remove_markdown_file(path)):
                removed += 1
        return removed

    def sync_all(self) -> dict[str, Any]:
        if not self.active:
            return {}
        engine = self._engine
        assert engine is not None
        self.last_result = self._run_coroutine(engine.index_vault(), timeout=120)
        return self.last_result

    def rebuild(self) -> dict[str, Any]:
        if not self.active:
            return self.start()
        engine = self._engine
        assert engine is not None
        self.last_result = self._run_coroutine(engine.rebuild(), timeout=120)
        return self.last_result

    def search(self, **parameters) -> list[dict[str, Any]]:
        if not self.active:
            raise RuntimeError("Basic Memory index is not running.")
        engine = self._engine
        assert engine is not None
        return self._run_coroutine(engine.search_records(**parameters), timeout=30)

    def context(self, permalink: str, depth: int = 1) -> Any:
        """Build Basic Memory context for one indexed permalink."""
        if not self.active:
            raise RuntimeError("Basic Memory index is not running.")
        engine = self._engine
        assert engine is not None
        return self._run_coroutine(engine.context(permalink, depth=depth), timeout=30)

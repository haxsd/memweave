"""Single-process Basic Memory engine used by the fused MemWeave runtime."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from contextlib import AsyncExitStack
from hashlib import sha256
from pathlib import Path
from typing import Any

from basic_memory import db
from basic_memory.config import ConfigManager
from basic_memory.config_models import BasicMemoryConfig, DatabaseBackend, ProjectEntry
from basic_memory.db import DatabaseType
from basic_memory.markdown import EntityParser, MarkdownProcessor
from basic_memory.models import Base
from basic_memory.models.search import (
    CREATE_SEARCH_INDEX,
    CREATE_SQLITE_SEARCH_VECTOR_CHUNKS,
    CREATE_SQLITE_SEARCH_VECTOR_CHUNKS_LOCATION,
    CREATE_SQLITE_SEARCH_VECTOR_CHUNKS_PROJECT_ENTITY,
    CREATE_SQLITE_SEARCH_VECTOR_CHUNKS_UNIQUE,
)
from basic_memory.repository import ProjectRepository
from basic_memory.schemas import Entity as EntitySchema
from basic_memory.schemas.memory import ContextResult
from basic_memory.schemas.search import SearchItemType, SearchQuery
from basic_memory.services import FileService
from basic_memory.services.composition import (
    BasicMemoryProjectRuntimeBundle,
    build_default_project_runtime_bundle,
)
from basic_memory.services.context_service import ContextService
from basic_memory.utils import generate_permalink
from loguru import logger
from sqlalchemy import text


INDEXABLE_SUFFIXES = {".md", ".markdown"}


class BasicMemoryEngine:
    """Own one Basic Memory project, its SQLite index and its search/context services.

    The engine is deliberately process-local and single-project. It does not start
    Basic Memory's MCP server, API server, watcher or cloud runtime. The caller
    passes the project vault and a stable data directory for the derived index.
    """

    def __init__(
        self,
        project_root: Path,
        data_dir: Path,
        *,
        vault: Path | None = None,
        project_name: str | None = None,
    ):
        self.project_root = project_root.expanduser().resolve()
        self.vault = (
            vault.expanduser().resolve()
            if vault is not None
            else self.project_root / ".memweave" / "memory"
        )
        self.data_dir = data_dir.expanduser().resolve()
        self._stack: AsyncExitStack | None = None
        self._old_config_dir: str | None = None
        self._old_home: str | None = None
        self.project_name = project_name or (
            "memweave-" + uuid.uuid5(uuid.NAMESPACE_URL, str(self.vault)).hex[:12]
        )
        self.config: BasicMemoryConfig | None = None
        self.session_maker = None
        self.project = None
        self.runtime: BasicMemoryProjectRuntimeBundle | None = None
        self.context_service: ContextService | None = None
        self.entity_parser: EntityParser | None = None

    async def start(self) -> None:
        if self._stack is not None:
            return
        logger.disable("basic_memory")
        self.vault.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._old_config_dir = os.environ.get("BASIC_MEMORY_CONFIG_DIR")
        self._old_home = os.environ.get("BASIC_MEMORY_HOME")
        os.environ["BASIC_MEMORY_CONFIG_DIR"] = str(self.data_dir)
        os.environ["BASIC_MEMORY_HOME"] = str(self.vault)
        os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")

        self.config = BasicMemoryConfig(
            env="test",
            projects={self.project_name: ProjectEntry(path=str(self.vault))},
            default_project=self.project_name,
            database_backend=DatabaseBackend.SQLITE,
            semantic_embedding_provider="none",
            default_search_type="text",
            index_changes=False,
            permalinks_include_project=False,
            kebab_filenames=False,
            format_on_save=False,
        )
        ConfigManager().save_config(self.config)

        self._stack = AsyncExitStack()
        engine, self.session_maker = await self._stack.enter_async_context(
            db.engine_session_factory(
                self.config.app_database_path,
                DatabaseType.FILESYSTEM,
                self.config,
            )
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(CREATE_SEARCH_INDEX)
            await connection.execute(CREATE_SQLITE_SEARCH_VECTOR_CHUNKS)
            await connection.execute(CREATE_SQLITE_SEARCH_VECTOR_CHUNKS_PROJECT_ENTITY)
            await connection.execute(CREATE_SQLITE_SEARCH_VECTOR_CHUNKS_LOCATION)
            await connection.execute(CREATE_SQLITE_SEARCH_VECTOR_CHUNKS_UNIQUE)

        project_repository = ProjectRepository()
        async with db.scoped_session(self.session_maker) as session:
            self.project = await project_repository.get_by_name(session, self.project_name)
            if self.project is None:
                self.project = await project_repository.create(
                    session,
                    {
                        "name": self.project_name,
                        "description": str(self.project_root),
                        "path": str(self.vault),
                        "is_active": True,
                        "is_default": True,
                    },
                )

        entity_parser = EntityParser(self.vault)
        self.entity_parser = entity_parser
        markdown_processor = MarkdownProcessor(entity_parser, app_config=self.config)
        file_service = FileService(self.vault, markdown_processor, app_config=self.config)
        self.runtime = build_default_project_runtime_bundle(
            project_id=self.project.id,
            session_maker=self.session_maker,
            entity_parser=entity_parser,
            file_service=file_service,
            app_config=self.config,
            database_backend=DatabaseBackend.SQLITE,
        )
        await self.runtime.search_service.init_search_index()
        self.context_service = ContextService(
            self.runtime.search_repository,
            self.runtime.entity_repository,
            self.runtime.observation_repository,
            link_resolver=self.runtime.link_resolver,
            session_maker=self.session_maker,
        )

    async def close(self) -> None:
        if self._stack is None:
            return
        await self._stack.aclose()
        self._stack = None
        logger.enable("basic_memory")
        if self._old_config_dir is None:
            os.environ.pop("BASIC_MEMORY_CONFIG_DIR", None)
        else:
            os.environ["BASIC_MEMORY_CONFIG_DIR"] = self._old_config_dir
        if self._old_home is None:
            os.environ.pop("BASIC_MEMORY_HOME", None)
        else:
            os.environ["BASIC_MEMORY_HOME"] = self._old_home

    def _require_runtime(self) -> BasicMemoryProjectRuntimeBundle:
        if self.runtime is None:
            raise RuntimeError("BasicMemoryEngine.start() has not completed")
        return self.runtime

    def _validate_relative_path(self, relative_path: str | Path) -> tuple[str, Path]:
        relative = Path(relative_path)
        if relative.is_absolute() or not relative.parts:
            raise ValueError("Indexed paths must be relative to the memory vault.")
        if any(part in ("", ".", "..") for part in relative.parts):
            raise ValueError("Indexed paths cannot leave the memory vault.")
        absolute = (self.vault / relative).resolve()
        if not absolute.is_relative_to(self.vault):
            raise ValueError("Indexed path is outside the memory vault.")
        return relative.as_posix(), absolute

    def _resolve_index_path(self, relative_path: str | Path) -> tuple[str, Path]:
        relative, absolute = self._validate_relative_path(relative_path)
        if absolute.is_symlink() or not absolute.is_file():
            raise ValueError("Indexed path must be a regular file.")
        return relative, absolute

    async def _parse_index_path(self, relative: str, absolute: Path):
        if self.entity_parser is None:
            raise RuntimeError("BasicMemoryEngine.start() has not completed")
        markdown = await self.entity_parser.parse_file(absolute)
        if markdown.frontmatter.permalink is None:
            # MemWeave's existing JSON frontmatter predates Basic Memory's
            # canonical permalink field. Keep the Markdown bytes untouched and
            # derive the stable identity from the vault-relative path.
            markdown.frontmatter.metadata["permalink"] = generate_permalink(relative)
        return markdown

    def _markdown_paths(self) -> list[str]:
        paths = []
        for path in self.vault.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(self.vault)
            if any(part.startswith(".") for part in relative.parts):
                continue
            if path.suffix.lower() not in INDEXABLE_SUFFIXES:
                continue
            if path.is_symlink():
                continue
            paths.append(relative.as_posix())
        return sorted(paths)

    @staticmethod
    def _checksum(path: Path) -> str:
        return sha256(path.read_bytes()).hexdigest()

    async def index_markdown_file(self, relative_path: str | Path) -> dict[str, Any]:
        """Index one existing Markdown record through Basic Memory's entity/search services."""
        runtime = self._require_runtime()
        relative, absolute = self._resolve_index_path(relative_path)
        checksum = self._checksum(absolute)
        async with db.scoped_session(self.session_maker) as session:
            existing = await runtime.entity_repository.get_by_file_path(
                session, relative, load_relations=False
            )
        if existing is not None and existing.checksum == checksum:
            return {
                "path": relative,
                "title": existing.title,
                "permalink": existing.permalink,
                "checksum": checksum,
                "unchanged": True,
            }
        markdown = await self._parse_index_path(relative, absolute)
        entity = await runtime.entity_service.create_entity_from_markdown(Path(relative), markdown)
        await runtime.search_service.index_entity(entity, content=markdown.content or "")
        async with db.scoped_session(self.session_maker) as session:
            entity = await runtime.entity_repository.update(
                session, entity.id, {"checksum": checksum}
            )
        return {
            "path": relative,
            "title": entity.title,
            "permalink": entity.permalink,
            "checksum": checksum,
            "unchanged": False,
        }

    async def remove_markdown_file(self, relative_path: str | Path) -> bool:
        """Remove one derived index record without deleting its Markdown source."""
        runtime = self._require_runtime()
        relative, _ = self._validate_relative_path(relative_path)
        async with db.scoped_session(self.session_maker) as session:
            entity = await runtime.entity_repository.get_by_file_path(
                session, relative, load_relations=True
            )
        if entity is None:
            return False
        await runtime.search_service.handle_delete(entity)
        async with db.scoped_session(self.session_maker) as session:
            await runtime.entity_repository.delete_by_file_path(session, relative)
        return True

    async def index_vault(self) -> dict[str, Any]:
        """Incrementally index Markdown records and remove deleted paths from the index."""
        runtime = self._require_runtime()
        current_paths = self._markdown_paths()
        indexed = []
        failed = []
        for relative in current_paths:
            try:
                indexed.append(await self.index_markdown_file(relative))
            except Exception as error:  # Keep one malformed record from blocking the vault.
                failed.append({"path": relative, "error": str(error)})

        async with db.scoped_session(self.session_maker) as session:
            previous_paths = await runtime.entity_repository.get_all_file_paths(session)
        stale = sorted(set(previous_paths) - set(current_paths))
        removed = []
        for relative in stale:
            try:
                if await self.remove_markdown_file(relative):
                    removed.append(relative)
            except (OSError, ValueError):
                # A source that vanished between discovery and deletion is already
                # outside the vault. Unexpected index failures are surfaced below.
                raise
        return {
            "total": len(current_paths),
            "indexed": len(indexed),
            "removed": len(removed),
            "failed": failed,
        }

    async def rebuild(self) -> dict[str, Any]:
        """Clear derived rows in place and re-index all current Markdown.

        The SQLite file may be open in other MemWeave processes. Deleting it on
        Windows therefore turns a normal rebuild into a file-sharing failure.
        Keeping the schema and clearing only derived rows makes rebuild safe
        while another process is connected, without treating the index as
        canonical data.
        """
        self._require_runtime()
        async with db.scoped_session(self.session_maker) as session:
            await session.execute(text("PRAGMA defer_foreign_keys=ON"))
            for table in reversed(Base.metadata.sorted_tables):
                if table.name == "project":
                    continue
                await session.execute(table.delete())
            await session.execute(text("DELETE FROM search_vector_chunks"))
            await session.execute(text("DELETE FROM search_index"))
        return await self.index_vault()

    async def write_note(
        self,
        title: str,
        content: str,
        *,
        directory: str = "",
        note_type: str = "note",
    ) -> dict[str, Any]:
        runtime = self._require_runtime()
        schema = EntitySchema(
            title=title,
            directory=directory,
            note_type=note_type,
            content=content,
        )
        entity, created = await runtime.entity_service.create_or_update_entity(schema)
        # Basic Memory's public write service publishes the Markdown graph but
        # leaves search projection to its caller. The fused runtime must do it here.
        await runtime.search_service.index_entity(entity)
        return {
            "id": entity.id,
            "title": entity.title,
            "permalink": entity.permalink,
            "file_path": entity.file_path,
            "created": created,
        }

    async def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        runtime = self._require_runtime()
        rows = await runtime.search_service.search(SearchQuery(text=query or None), limit=limit)
        return [
            {
                "id": row.id,
                "title": row.title,
                "permalink": row.permalink,
                "file_path": row.file_path,
                "type": row.type,
                "content": row.content,
                "score": row.score,
            }
            for row in rows
        ]

    async def search_records(
        self,
        query: str = "",
        *,
        note_type: str = "memory",
        scope: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search Basic Memory's single FTS index with MemWeave's record scopes as filters."""
        runtime = self._require_runtime()
        metadata_filters: dict[str, Any] = {}
        if scope is not None:
            metadata_filters["scope"] = scope
        if project_id is not None:
            metadata_filters["project_id"] = project_id
        if task_id is not None:
            metadata_filters["task_id"] = task_id
        if status is not None:
            metadata_filters["status"] = status
        search_query = SearchQuery(
            text=query.strip() or None,
            note_types=[note_type] if note_type else None,
            entity_types=[SearchItemType.ENTITY],
            metadata_filters=metadata_filters or None,
        )
        rows = await runtime.search_service.search(search_query, limit=limit)
        return [
            {
                "id": row.id,
                "title": row.title,
                "permalink": row.permalink,
                "file_path": row.file_path,
                "type": row.type,
                "content": row.content,
                "score": row.score,
            }
            for row in rows
        ]

    async def context(self, permalink: str, *, depth: int = 1) -> ContextResult:
        if self.context_service is None:
            raise RuntimeError("BasicMemoryEngine.start() has not completed")
        return await self.context_service.build_context(
            memory_url=f"memory://{permalink}",
            depth=depth,
            limit=10,
            max_related=10,
            include_observations=True,
        )

    @staticmethod
    def temporary_engine() -> "BasicMemoryEngine":
        """Create an isolated engine for tests and one-shot diagnostics."""
        root = Path(tempfile.mkdtemp(prefix="memweave-engine-"))
        return BasicMemoryEngine(root, root / ".index")

    def cleanup_temporary(self) -> None:
        """Delete a temporary engine root after close; only for temporary_engine()."""
        if ".memweave" in str(self.project_root):
            shutil.rmtree(self.project_root, ignore_errors=True)


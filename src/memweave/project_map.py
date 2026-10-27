import json
import os
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pathspec import PathSpec

from memweave.diagnostics import DiagnosticError, validate_text
from memweave.storage import digest, encode_record, now


MAX_SCAN_FILES = 4000
MAX_FILE_BYTES = 1024 * 1024
MAX_SCAN_BYTES = 20 * 1024 * 1024
MAX_SCAN_ENTRIES = 20000
EXCLUDED_DIRECTORIES = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist",
    "work", ".ssh", ".aws", ".azure", ".pytest_cache", ".mypy_cache",
}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}


def is_link(target: Path) -> bool:
    return target.is_symlink() or target.is_junction()


def excluded(target: Path) -> bool:
    name = target.name.lower()
    return (
        name in EXCLUDED_DIRECTORIES
        or name.startswith((".env", "secret", "credentials"))
        or target.suffix.lower() in SECRET_SUFFIXES
        or name in ("id_rsa", "id_ed25519", ".netrc", ".npmrc", ".pypirc")
    )


class ProjectMap:
    def __init__(self, vault):
        self.vault = vault

    def _root(self, project_id: str) -> Path:
        validate_text(project_id, "project_id", 100)
        project = self.vault.read(project_id)
        if project["type"] != "project" or not isinstance(project.get("root"), str):
            raise DiagnosticError("RECORD_INVALID", "Project must be a registered project with a root.")
        root = Path(project["root"]).expanduser().resolve()
        if not root.is_dir():
            raise DiagnosticError("PROJECT_UNAVAILABLE", "Registered project root is unavailable.")
        return root

    def _target(self, root: Path, relative: str) -> tuple[Path, str]:
        validate_text(relative, "path", 2000)
        normalized = relative.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        if not parts or PurePosixPath(normalized).is_absolute() or any(
            part in (".", "..") or ":" in part for part in parts
        ):
            raise DiagnosticError("SCOPE_DENIED", "Evidence requires a relative path inside the project.")
        target = root
        for part in parts:
            target = target / part
            if is_link(target):
                raise DiagnosticError("SCOPE_DENIED", "Project evidence cannot follow links or junctions.")
        resolved = target.resolve()
        if not resolved.is_relative_to(root) or resolved.is_relative_to(self.vault.home):
            raise DiagnosticError("SCOPE_DENIED", "Evidence path is outside the project source scope.")
        return target, PurePosixPath(*parts).as_posix()

    def _read(self, root: Path, relative: str) -> bytes:
        target, _ = self._target(root, relative)
        try:
            before = target.stat()
            if not target.is_file() or before.st_size > MAX_FILE_BYTES:
                raise DiagnosticError("SOURCE_CHANGED", "Evidence is no longer a bounded regular source file.")
            with target.open("rb") as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
            after = target.stat()
            self._target(root, relative)
            if len(content) > MAX_FILE_BYTES or (
                before.st_size, before.st_mtime_ns, before.st_ino
            ) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise DiagnosticError("SOURCE_CHANGED", "Source changed while being read; scan again.")
            return content
        except OSError as error:
            raise DiagnosticError("SOURCE_CHANGED", "Source is unavailable; scan again.") from error

    def _snapshot(self, project_id: str) -> tuple[dict, dict | None]:
        with self.vault.diagnostics._connect() as connection:
            files = {row["path"]: dict(row) for row in connection.execute(
                "SELECT path, hash, size FROM files WHERE project_id = ? ORDER BY path", (project_id,)
            )}
            row = connection.execute(
                "SELECT * FROM scans WHERE project_id = ?", (project_id,)
            ).fetchone()
        return files, dict(row) if row else None

    def scan(self, project_id: str, operation_id: str) -> dict:
        root = self._root(project_id)

        def build():
            previous, last_scan = self._snapshot(project_id)
            files = {}
            counters = {"skipped": 0, "entries": 0, "bytes": 0}
            errors = []
            truncated = False
            partial = False
            pending = [(root, [])]
            while pending:
                directory, rules = pending.pop()
                ignore_file = directory / ".gitignore"
                if ignore_file.is_file() and not is_link(ignore_file):
                    try:
                        if ignore_file.stat().st_size <= MAX_FILE_BYTES:
                            rules = [*rules, (directory, PathSpec.from_lines(
                                "gitwildmatch", ignore_file.read_text("utf-8-sig").splitlines()
                            ))]
                        else:
                            partial = True
                            counters["skipped"] += 1
                            if len(errors) < 50:
                                errors.append({"path": ignore_file.relative_to(root).as_posix(),
                                               "reason": "ignore_rules_too_large"})
                            continue
                    except (OSError, UnicodeError, ValueError):
                        partial = True
                        if len(errors) < 50:
                            errors.append({"path": ignore_file.relative_to(root).as_posix(),
                                           "reason": "ignore_rules_unreadable"})
                        continue
                try:
                    with os.scandir(directory) as entries:
                        children = []
                        for entry in entries:
                            if counters["entries"] + len(children) >= MAX_SCAN_ENTRIES:
                                truncated = True
                                break
                            children.append(Path(entry.path))
                    children.sort(key=lambda path: path.name)
                except OSError:
                    partial = True
                    if len(errors) < 50:
                        errors.append({"path": directory.relative_to(root).as_posix(),
                                       "reason": "directory_unreadable"})
                    continue
                directories = []
                for target in children:
                    counters["entries"] += 1
                    relative = target.relative_to(root).as_posix()
                    try:
                        if is_link(target) or excluded(target) or target.resolve().is_relative_to(self.vault.home):
                            counters["skipped"] += 1
                            continue
                        directory_target = target.is_dir()
                        ignored = False
                        for base, specification in rules:
                            candidate = target.relative_to(base).as_posix() + ("/" if directory_target else "")
                            for pattern in specification.patterns:
                                if pattern.include is not None and pattern.match_file(candidate):
                                    ignored = pattern.include
                        if ignored:
                            counters["skipped"] += 1
                            continue
                        if directory_target:
                            directories.append((target, rules))
                            continue
                        if not target.is_file():
                            counters["skipped"] += 1
                            continue
                        if len(files) >= MAX_SCAN_FILES:
                            truncated = True
                            counters["skipped"] += 1
                            continue
                        if target.stat().st_size > MAX_FILE_BYTES:
                            partial = True
                            counters["skipped"] += 1
                            continue
                        if counters["bytes"] + target.stat().st_size > MAX_SCAN_BYTES:
                            truncated = True
                            counters["skipped"] += 1
                            continue
                        content = self._read(root, relative)
                        if counters["bytes"] + len(content) > MAX_SCAN_BYTES:
                            truncated = True
                            counters["skipped"] += 1
                            continue
                        if b"\x00" in content:
                            counters["skipped"] += 1
                            continue
                        try:
                            content.decode("utf-8-sig")
                        except UnicodeError:
                            counters["skipped"] += 1
                            continue
                        counters["bytes"] += len(content)
                        files[relative] = {"hash": digest(content), "size": len(content)}
                    except (OSError, DiagnosticError):
                        partial = True
                        counters["skipped"] += 1
                        if len(errors) < 50:
                            errors.append({"path": relative, "reason": "source_unavailable_or_changed"})
                pending.extend(reversed(directories))
                if counters["entries"] >= MAX_SCAN_ENTRIES:
                    truncated = True
                    break
            generation = last_scan["generation"] + 1 if last_scan else 1
            scanned_at = now()
            coverage = {
                "state": "partial" if truncated or partial else "complete",
                "scope": "eligible_utf8_source_files",
                "generation": generation, "scanned_at": scanned_at,
                "file_count": len(files), "bytes": counters["bytes"],
                "skipped": counters["skipped"], "truncated": truncated,
                "excluded_directories": sorted(EXCLUDED_DIRECTORIES), "errors": errors,
            }
            response = {
                "project_id": project_id, "generation": generation, "scanned_at": scanned_at,
                "file_count": len(files), "skipped": counters["skipped"], "coverage": coverage,
                "added": sorted(set(files) - set(previous)),
                "changed": sorted(path for path in files if path in previous
                                  and files[path]["hash"] != previous[path]["hash"]),
                "deleted": sorted(set(previous) - set(files)),
            }
            with self.vault.diagnostics._connect(writable=True) as connection:
                connection.execute("DELETE FROM files WHERE project_id = ?", (project_id,))
                connection.executemany("INSERT INTO files VALUES (?, ?, ?, ?)", [
                    (project_id, path, value["hash"], value["size"]) for path, value in files.items()
                ])
                connection.execute("INSERT OR REPLACE INTO scans VALUES (?, ?, ?, ?)", (
                    project_id, generation, scanned_at, json.dumps(coverage, ensure_ascii=False),
                ))
            return [], response

        return self.vault.commit(operation_id, {"action": "project_scan", "project_id": project_id}, build)

    def evidence(self, project_id: str, path: str, max_chars: int = 12000) -> dict:
        if type(max_chars) is not int or not 1 <= max_chars <= 100000:
            raise DiagnosticError("RECORD_INVALID", "max_chars must be between 1 and 100000.")
        root = self._root(project_id)
        _, relative = self._target(root, path)
        files, scan = self._snapshot(project_id)
        source = files.get(relative)
        if source is None:
            raise DiagnosticError("SOURCE_NOT_INDEXED", "Source is outside the latest scan; scan eligible files first.")
        content = self._read(root, relative)
        if digest(content) != source["hash"]:
            raise DiagnosticError("SOURCE_CHANGED", "Source hash differs from the latest scan; scan again.")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeError as error:
            raise DiagnosticError("SOURCE_CHANGED", "Source is no longer UTF-8 text; scan again.") from error
        return {"path": relative, "hash": source["hash"], "content": text[:max_chars],
                "truncated": len(text) > max_chars, "generation": scan["generation"]}

    def write(self, project_id: str, operation_id: str, title: str, body: str,
              sources: list, relations: list) -> dict:
        self._root(project_id)
        validate_text(title, "title", 500)
        validate_text(body, "body", 100000)
        if not isinstance(sources, list) or not 1 <= len(sources) <= 100:
            raise DiagnosticError("RECORD_INVALID", "Architecture records need 1–100 source references.")
        if not isinstance(relations, list) or len(relations) > 100:
            raise DiagnosticError("RECORD_INVALID", "relations must be a list of at most 100 relations.")
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get("hash"), str):
                raise DiagnosticError("RECORD_INVALID", "Each source needs a path and hash.")
            validate_text(source.get("path"), "source.path", 2000)
        normalized_relations = []
        for relation in relations:
            if not isinstance(relation, dict):
                raise DiagnosticError("RECORD_INVALID", "Each relation must be an object.")
            kind = relation.get("relation", relation.get("type"))
            validate_text(kind, "relation", 100)
            validate_text(relation.get("source"), "relation.source", 2000)
            validate_text(relation.get("target"), "relation.target", 2000)
            evidence_kind = relation.get("evidence_kind", "host_reported")
            if kind == "validated_by" and relation.get("evidence_kind") not in (
                "test_declaration", "static_association", "host_reported", "actual_coverage",
            ):
                raise DiagnosticError("RECORD_INVALID", "validated_by needs an explicit evidence_kind.")
            certainty = relation.get("certainty", "inferred")
            if certainty not in ("inferred", "confirmed"):
                raise DiagnosticError("RECORD_INVALID", "Relation certainty must be inferred or confirmed.")
            normalized_relations.append({
                "source": relation["source"], "target": relation["target"],
                "relation": kind, "evidence_kind": evidence_kind, "certainty": certainty,
            })

        def build():
            checked = []
            for source in sources:
                evidence = self.evidence(project_id, source["path"], 1)
                if evidence["hash"] != source["hash"]:
                    raise DiagnosticError("SOURCE_CHANGED", "Architecture source hash is not current.")
                checked.append({"path": evidence["path"], "hash": evidence["hash"]})
            record_id = "architecture_" + uuid4().hex
            record = {
                "id": record_id, "type": "architecture", "scope": "project",
                "project_id": project_id, "title": title, "body": body,
                "sources": checked, "relations": normalized_relations,
                "status": "active", "revision": 1, "created_at": now(),
                "source_type": "host_reported",
            }
            return [{"record": record, "path": f"architecture/{record_id}.md", "expected": None}], {
                "id": record_id, "revision": 1, "etag": digest(encode_record(record).encode("utf-8")),
            }

        return self.vault.commit(operation_id, {
            "action": "project_write", "project_id": project_id, "title": title,
            "body": body, "sources": sources, "relations": relations,
        }, build)

    def query(self, project_id: str, query: str = "", limit: int = 100,
              include_stale: bool = False, prefer_architecture: bool = False) -> dict:
        root = self._root(project_id)
        if not isinstance(query, str) or len(query) > 2000:
            raise DiagnosticError("RECORD_INVALID", "query must contain at most 2000 characters.")
        if type(limit) is not int or not 1 <= limit <= 1000 or type(include_stale) is not bool:
            raise DiagnosticError("RECORD_INVALID", "limit must be 1–1000 and include_stale must be boolean.")
        files, scan = self._snapshot(project_id)
        live_hashes = {}
        for path in files:
            try:
                live_hashes[path] = digest(self._read(root, path))
            except DiagnosticError:
                live_hashes[path] = None
        stale_paths = {path for path in files if live_hashes[path] != files[path]["hash"]}
        nodes = {}
        edges = []
        nodes["folder:."] = {"id": "folder:.", "type": "folder", "label": root.name,
                             "path": ".", "status": "active"}
        for path, source in files.items():
            parts = PurePosixPath(path).parts
            parent_id = "folder:."
            for position in range(1, len(parts)):
                folder = PurePosixPath(*parts[:position]).as_posix()
                node_id = "folder:" + folder
                if node_id not in nodes:
                    nodes[node_id] = {"id": node_id, "type": "folder", "label": parts[position - 1],
                                      "path": folder, "status": "active"}
                    edges.append({"source": parent_id, "target": node_id, "relation": "contains"})
                parent_id = node_id
            node_id = "file:" + path
            nodes[node_id] = {"id": node_id, "type": "file", "label": parts[-1],
                              "path": path, "hash": source["hash"],
                              "status": "needs_review" if path in stale_paths else "active"}
            edges.append({"source": parent_id, "target": node_id, "relation": "contains"})
        stale_records = 0
        for record in self.vault.list_records("architecture", project_id, "project"):
            sources = record.get("sources", [])
            stale = not sources or any(
                not isinstance(source, dict)
                or live_hashes.get(source.get("path")) != source.get("hash")
                or source.get("path") not in files
                for source in sources
            )
            if stale:
                stale_records += 1
            if (stale or record.get("status") != "active") and not include_stale:
                continue
            status = "needs_review" if stale else record.get("status", "active")
            nodes[record["id"]] = {
                "id": record["id"], "type": "architecture", "label": record.get("title", record["id"]),
                "title": record.get("title", ""), "body": record.get("body", ""),
                "path": record["path"], "status": status, "sources": sources,
                "source_type": record.get("source_type", "host_reported"),
            }
            for relation in record.get("relations", [])[:100]:
                if not isinstance(relation, dict) or not all(
                    isinstance(relation.get(field), str) for field in ("source", "target", "relation")
                ):
                    continue
                edge = {**relation, "record_id": record["id"], "status": status}
                for endpoint in ("source", "target"):
                    node_id = edge[endpoint]
                    if node_id == "self":
                        node_id = record["id"]
                        edge[endpoint] = node_id
                    if node_id not in nodes:
                        nodes[node_id] = {"id": node_id, "type": "module", "label": node_id,
                                          "path": "", "status": status, "source_type": "host_reported"}
                edges.append(edge)
        search = query.casefold().strip()
        selected = [node for node in nodes.values() if not search or search in " ".join(
            str(node.get(field, "")) for field in ("id", "label", "path", "body")
        ).casefold()]
        if prefer_architecture:
            selected.sort(key=lambda node: {"architecture": 0, "file": 1}.get(node["type"], 2))
        query_truncated = len(selected) > limit
        selected = selected[:limit]
        identifiers = {node["id"] for node in selected}
        selected_edges = [edge for edge in edges if edge["source"] in identifiers
                          and edge["target"] in identifiers]
        query_truncated = query_truncated or len(selected_edges) > limit * 2
        if scan:
            coverage = json.loads(scan["coverage"])
            coverage["truncated"] = coverage.get("truncated", False) or query_truncated
            freshness = {
                "state": "stale" if stale_paths or stale_records else "checked",
                "checked_at": now(), "scanned_at": scan["scanned_at"],
                "generation": scan["generation"], "stale_files": sorted(stale_paths),
                "stale_architecture_records": stale_records,
                "scope": "indexed_sources_only",
            }
        else:
            coverage = {"state": "not_scanned", "generation": 0, "truncated": query_truncated}
            freshness = {"state": "unknown"}
        return {"project_id": project_id, "nodes": selected, "edges": selected_edges[:limit * 2],
                "coverage": coverage, "freshness": freshness}

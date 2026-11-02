# Third-Party Notices

MemWeave is licensed under the GNU Affero General Public License, version 3 or later (`AGPL-3.0-or-later`). The full license text is in [LICENSE](LICENSE).

This file records upstream code and behavior that MemWeave will directly copy, fork or adapt. It is part of the public source distribution and must be updated whenever upstream code is imported or modified.

## Basic Memory

- Upstream: https://github.com/basicmachines-co/basic-memory
- Fixed upstream commit: `438d49776b8682f0a561bb3dc66092a33a5a829b` (`main`, verified 2026-10-27)
- Upstream license: `AGPL-3.0-or-later`
- Copyright and attribution: Basic Machines and upstream contributors retain their rights.
- License text: the AGPL text is provided in [LICENSE](LICENSE).
- Direct-use scope: the runtime subset of the pinned upstream source tree retained under `src/basic_memory/`, exercising Markdown note parsing, filesystem synchronization, SQLite/repository indexing, project model, FTS search and context-building implementation behind MemWeave's single-process adapter. Omitted upstream files remain available at the pinned upstream commit under the upstream AGPL license.
- Integration location: `src/basic_memory/` plus `src/memweave/basic_memory_engine.py` and `src/memweave/basic_memory_index.py`; Basic Memory is not kept or started as a second MCP service.
- Upstream sync method: pin the commit above, keep MemWeave-specific task changes isolated, review the upstream diff before each update, and update this notice plus `docs/design-references.md` with the new commit and changed paths.

### Current vendored manifest (updated 2026-11-02)

- Upstream baseline: Basic Memory `0.24.1` at commit `438d49776b8682f0a561bb3dc66092a33a5a829b` contains 546 files, 486 Python files and 6,746,424 bytes after LF normalization.
- Retained source tree: `src/basic_memory/` now contains 268 files, 266 Python files and 2,999,029 bytes after LF normalization (3,074,463 bytes in this Windows CRLF checkout).
- Source-tree comparison: 262 of the 268 retained files match the pinned upstream tree byte-for-byte after LF normalization. Six files contain the declared local adaptations listed below. The other 278 upstream files are intentionally omitted because they are outside MemWeave's runtime closure.
- Runtime use: MemWeave's single-process `src/memweave/basic_memory_engine.py` uses Basic Memory's Markdown, SQLite, project, FTS search and context services. Basic Memory's own MCP, HTTP, CLI, cloud, importer, watcher, man-page and OKF entrypoints are not vendored and are not started.
- Local modifications under `src/basic_memory/`: `config.py` degrades the removed CLI credential import to `False`; `services/link_resolver.py` degrades the removed MCP project-context import to `None`; `repository/embedding_provider_factory.py` accepts `semantic_embedding_provider="none"` for text-only operation; `repository/sqlite_search_repository.py` marks semantic search disabled when no provider exists; `repository/search_repository.py` skips vector-index construction when no provider exists; `repository/search_refresh_race.py` degrades its optional PostgreSQL exception import for SQLite-only use.
- MemWeave-owned integration files are `src/memweave/basic_memory_engine.py`, `src/memweave/basic_memory_index.py`, the MCP lifespan in `src/memweave/server.py`, and lifecycle/synchronization changes in `src/memweave/services.py`, `cli.py`, `hooks.py`, `codex_hooks.py` and `storage.py`.
- Current package data includes only Alembic `alembic.ini`, `script.py.mako` and 42 version scripts from the retained Basic Memory tree. The former prompt templates, MCP resource and MCP UI HTML files were removed with the pruned entrypoints; the older wheel that still contained them is superseded and must not be used as current package evidence.
- Direct-use result: no planning-with-files script source was copied. Its MIT license text is retained and selected lifecycle behavior was adapted in Phase 2; the exact upstream and MemWeave target files are recorded in the Phase 2 adaptation manifest below.

## planning-with-files

- Upstream: https://github.com/OthmanAdi/planning-with-files
- Fixed upstream commit: `dcdfb278de53fdb69b2067ea173dbb1de45345a3` (`master`, verified 2026-10-27)
- Upstream license: MIT
- Copyright: Copyright (c) 2026 Ahmad Adi and contributors.
- License text: [licenses/planning-with-files/LICENSE](licenses/planning-with-files/LICENSE)
- Adapted direct-use scope: selected task-plan/findings/progress lifecycle rules, session-start and prompt-submit context injection, stop gating, active-task resolution, and continuity Skill material. MemWeave expresses completion through task acceptance results and WorkUnit resolution; it does not claim upstream plan-hash attestation or the `check-complete` script.
- Integration location: MemWeave’s single task lifecycle, continuity Skill and unified Hook entry; not a separately installed planning product.
- Upstream sync method: pin the commit above, retain the MIT notice, review selected-file diffs before each update, and update this notice plus `docs/design-references.md` with the new commit and changed paths.

### Phase 2 adaptation manifest

- Adapted behavior from `skills/planning-with-files/SKILL.md`, `skills/planning-with-files/reference.md`, `skills/planning-with-files/templates/task_plan.md`, `templates/findings.md` and `templates/progress.md` at the pinned commit.
- MemWeave targets: `src/memweave/services.py` (task phases, WorkUnits, findings/progress/failures/verification/next-step), `src/memweave/context.py` (bounded WorkUnit recovery), `src/memweave/skills/memweave-continuity/SKILL.md` (single continuity lifecycle), and `src/memweave/static/index.html` (current/queued and phase/WorkUnit display).
- Fusion change: upstream separate planning files and per-host scripts are not shipped as a second product. Their persistence, recitation, failure logging, validation and recovery rules are represented by MemWeave task, WorkUnit and phase Markdown records under one MCP namespace.
- No planning-with-files script source was copied. The upstream `task_plan.md`/`findings.md`/`progress.md` file system, plan-hash attestation, `check-complete`/`gate-stop` scripts, ledger and multi-host adapters are not shipped. The upstream MIT text remains at `licenses/planning-with-files/LICENSE`.

## Distribution policy

- The fused MemWeave source is distributed under `AGPL-3.0-or-later`.
- There is no closed-source distribution path for code derived from Basic Memory.
- Source availability, license texts, attribution, upstream commits and modification records must travel with releases.
- Third-party names and links do not imply endorsement.

## Change record

- 2026-10-27: initial Phase 0 provenance record; upstream commits verified against the current branches. No upstream source files imported yet.
- 2026-10-27: Phase 1 imported Basic Memory `0.24.1`, recorded the 546-file source tree, local modification paths, one-process runtime boundary, and packaged upstream resources. planning-with-files remains license-only pending Phase 2.
- 2026-10-27: Phase 2 adapted the pinned planning-with-files lifecycle and templates into MemWeave task/WorkUnit/phase records and the single continuity Skill; no upstream scripts were copied.
- 2026-11-02: pruned 278 vendored Basic Memory files that were outside the MemWeave runtime closure. The retained tree is 268 files, with six explicitly documented local modifications; removed CLI/MCP/HTTP/cloud/importer/watcher/man/OKF entrypoints and their package resources.


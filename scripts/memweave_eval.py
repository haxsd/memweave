#!/usr/bin/env python3
"""MemWeave memory-evaluation harness v2 (deterministic, offline).

The harness answers one question with a repeatable command: what does the
memory substrate actually put in front of a fresh session, and what changes
between "no memory" (A), "task working memory only" (B) and "task working
memory + reviewed long-term knowledge" (C)?

Design rules:

* No network, no model calls, no wall-clock assertions.  Every scored fact is a
  deterministic property of the product surface returned by
  ``workspace_prepare`` / ``context_get`` / ``memory_search`` / write conflicts.
* Every scenario is built in a throw-away MemWeave home under a temporary work
  directory.  ``%USERPROFILE%\\.memweave`` is never touched.
* v1 added two P1/P2 scenarios on top of the recovery/injection/budget/conflict
  set: structured evidence-pointer persistence (incl. legacy records and rebuild)
  and ``memory_review`` consolidation lint/truncation incl. the P2 knowledge
  index.  v2 adds the P3 opt-in ``knowledge_index`` injection off/on gate.
  All three are deliberately non-``smoke`` so the CI gate stays fast.
* Scenarios use the real in-process ``Service`` entry (the same surface the CLI
  and the MCP server dispatch to); this keeps CI smoke fast.  It is not a model
  evaluation: it measures retrieval + injection + conflict behaviour, not
  whether a model would answer well given that context.
* A scenario "passes" when every observed check equals its declared expected
  value in all three conditions.  Checks expected to be absent in A are the
  measurable memory gain; checks expected everywhere are safety invariants.

Usage:
    python scripts/memweave_eval.py                 # human summary
    python scripts/memweave_eval.py --json          # machine-readable report
    python scripts/memweave_eval.py --list          # list scenarios
    python scripts/memweave_eval.py --tag smoke     # fast subset
    python scripts/memweave_eval.py --strict        # exit 1 on failure

See ``docs/eval.md`` for scenario descriptions, metrics, baseline and limits.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    from memweave.diagnostics import DiagnosticError  # noqa: E402
    from memweave.services import Service  # noqa: E402
except ModuleNotFoundError as _import_error:  # pragma: no cover - environment guidance
    sys.stderr.write(
        "memweave_eval: cannot import the MemWeave runtime dependencies "
        f"({_import_error.name}). Use the repository virtual environment:\n"
        "  .\\.venv\\Scripts\\python.exe scripts\\memweave_eval.py --json\n"
    )
    raise SystemExit(2)

HARNESS_NAME = "memweave-eval"
HARNESS_VERSION = "0.3.0"
SCHEMA_VERSION = 1
CONDITIONS = ("A", "B", "C")
CONDITION_INFO = {
    "A": {
        "label": "no memory",
        "description": (
            "Empty MemWeave home, prompt only. The host receives the user prompt but no "
            "task state, checkpoints or knowledge."
        ),
    },
    "B": {
        "label": "task working memory only",
        "description": (
            "A project with the current task, checkpoints, WorkUnits and phases, but no "
            "long-term knowledge records."
        ),
    },
    "C": {
        "label": "task working memory + reviewed knowledge",
        "description": (
            "The same task state as B plus explicitly reviewed (active) project knowledge, "
            "including distractors and retired/candidate traps where the scenario needs them."
        ),
    },
}
DEFAULT_BUDGET = 12000
BUDGET_FLOOR = 500
CHARS_PER_ESTIMATED_TOKEN = 4


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clip(value: object, limit: int = 240) -> str:
    text = str(value if value is not None else "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def estimate_tokens(characters: int) -> int:
    """Coarse char/4 token estimate; no tokenizer dependency on purpose."""
    if characters <= 0:
        return 0
    return max(1, round(characters / CHARS_PER_ESTIMATED_TOKEN))


def resolve_work_root(explicit: str | Path | None = None) -> Path:
    """Return a writable directory for throw-away memory homes.

    Order: explicit argument, ``MEMWEAVE_EVAL_WORKDIR``, the OS temp dir, then
    ``<repo>/work/eval-tmp``.  The OS temp fallback is skipped when
    ``tempfile.gettempdir()`` points at the current directory (sandboxed
    environments deny writes to the real temp dir and silently fall back).
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_value = os.environ.get("MEMWEAVE_EVAL_WORKDIR", "").strip()
    if env_value:
        candidates.append(Path(env_value))
    cwd = Path.cwd().resolve()
    try:
        os_temp = Path(tempfile.gettempdir()).resolve()
    except OSError:
        # All tempfile candidates were unwritable (e.g. cwd is a read-only root).
        os_temp = None
    if os_temp is not None and os_temp != cwd:
        # In sandboxed environments tempfile silently falls back to the cwd; never
        # adopt that fallback as the evaluation work root.
        candidates.append(os_temp / "memweave-eval")
    candidates.append(REPO_ROOT / "work" / "eval-tmp")
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved == cwd:
            # tempfile.gettempdir() fell back to cwd; never create eval homes there.
            continue
        try:
            resolved.mkdir(parents=True, exist_ok=True)
            probe = resolved / f".memweave-eval-write-probe-{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return resolved
        except OSError:
            continue
    raise RuntimeError(
        "No writable work directory for evaluation homes. Pass --workdir or set "
        "MEMWEAVE_EVAL_WORKDIR."
    )


# ---------------------------------------------------------------------------
# Session: one simulated MCP/service process lifetime against one home
# ---------------------------------------------------------------------------


class Session:
    """One client/MCP-process lifetime bound to a MemWeave home.

    ``enforce_prepare`` mirrors the MCP host contract: after the first
    ``workspace_prepare`` the rest of the project tools are reachable.  Fixture
    setup disables it because setup is "past sessions" writing state directly.
    """

    def __init__(self, home: Path, root: Path, label: str = "session",
                 enforce_prepare: bool = True):
        self.home = Path(home)
        self.root = Path(root)
        self.label = label
        self.enforce_prepare = enforce_prepare
        self.service: Service | None = None
        self.surface_calls = 0
        self.wall_ms = 0.0
        self._started_at = 0.0

    def __enter__(self) -> "Session":
        if self.service is not None:
            return self
        self.service = Service(self.home)
        self._started_at = time.perf_counter()
        self.service.start()
        self.service.require_workspace_prepare = self.enforce_prepare
        return self

    def __exit__(self, exc_type, exc_value, tb) -> bool:
        self.close()
        return False

    def close(self) -> None:
        if self.service is None:
            return
        try:
            self.service.close()
        finally:
            self.wall_ms = round((time.perf_counter() - self._started_at) * 1000.0, 1)
            self.service = None

    def call(self, action: str, **parameters):
        if self.service is None:
            raise RuntimeError("session is not open")
        self.surface_calls += 1
        return self.service.dispatch(action, parameters)["data"]

    def prepare(self, intent: str = "resume", query: str = "",
                budget: int = DEFAULT_BUDGET) -> dict:
        return self.call(
            "workspace_prepare",
            root=str(self.root),
            intent=intent,
            query=query,
            budget=budget,
        )


# ---------------------------------------------------------------------------
# Fixture: builds one throw-away world (project root + MemWeave home)
# ---------------------------------------------------------------------------


class Fixture:
    """Throw-away project + MemWeave home used to build a scenario's state."""

    def __init__(self, base: Path, scenario_id: str):
        self.scenario_id = scenario_id
        self.base = Path(base) / scenario_id
        self.root = self.base / "repo"
        self.root.mkdir(parents=True, exist_ok=True)
        self.home = self.base / "world-home"
        self.empty_home = self.base / "empty-home"
        self.setup: Session | None = None
        self.project_id: str | None = None
        self.tasks: dict[str, dict] = {}
        self.units: dict[str, dict] = {}
        self.checkpoints: dict[str, list[dict]] = {}
        self.phases: dict[str, list[dict]] = {}
        self.knowledge: dict[str, dict] = {}
        self._claimed_task: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def begin(self) -> Session:
        self.setup = Session(
            self.home, self.root, label=f"{self.scenario_id}-setup",
            enforce_prepare=False,
        )
        self.setup.__enter__()
        return self.setup

    def end_setup(self) -> None:
        if self.setup is not None:
            self.setup.close()
            self.setup = None

    def session(self, home: Path) -> Session:
        return Session(home, self.root, label=f"{self.scenario_id}-measured")

    def home_for(self, condition: str) -> Path:
        return self.empty_home if condition == "A" else self.home

    # -- state builders (fixture setup only) -------------------------------

    def _require_setup(self) -> Session:
        if self.setup is None:
            raise RuntimeError("fixture setup session is not open")
        return self.setup

    def register(self, name: str) -> str:
        response = self._require_setup().call(
            "project_register",
            root=str(self.root),
            name=name,
            operation_id=f"{self.scenario_id}-register",
        )
        self.project_id = response["id"]
        return self.project_id

    def task_id(self, key: str) -> str:
        return self.tasks[key]["id"]

    def checkpoint_id(self, key: str, index: int = 0) -> str | None:
        """Return the ``checkpoint_id`` written for a task (index -1 = latest)."""
        entries = self.checkpoints.get(key) or []
        if index < 0:
            index += len(entries)
        if not 0 <= index < len(entries):
            return None
        return entries[index].get("checkpoint_id")

    def phase_id(self, key: str, index: int = 0) -> str | None:
        entries = self.phases.get(key) or []
        if index < 0:
            index += len(entries)
        if not 0 <= index < len(entries):
            return None
        return entries[index].get("phase_id")

    def read_task(self, key: str) -> dict:
        return self._require_setup().call("task_read", task_id=self.task_id(key))

    def rebuild(self) -> dict:
        """Rebuild Markdown-derived state and the search index from the home."""
        return self._require_setup().call("rebuild")

    def open_task(self, key: str, title: str, goal: str,
                  acceptance: list[str]) -> dict:
        response = self._require_setup().call(
            "task_open",
            project_id=self.project_id,
            title=title,
            goal=goal,
            acceptance=acceptance,
            operation_id=f"{self.scenario_id}-open-{key}",
        )
        self.tasks[key] = response
        return response

    def claim(self, key: str) -> dict:
        prepared = self._require_setup().prepare(intent="resume")
        task_id = self.task_id(key)
        if prepared.get("state") != "ready" or prepared.get("task_id") != task_id:
            raise RuntimeError(
                f"fixture claim failed for {key}: state={prepared.get('state')} "
                f"task={prepared.get('task_id')} expected={task_id}"
            )
        self._claimed_task = task_id
        return prepared

    def _ensure_claim(self, key: str) -> None:
        if self._claimed_task != self.task_id(key):
            self.claim(key)

    def checkpoint(self, key: str, *, progress: str, next_step: str,
                   findings: str | None = None, failures: str | None = None,
                   verification: str | None = None,
                   evidence: list[dict] | None = None) -> dict:
        task = self.read_task(key)
        self._ensure_claim(key)
        parameters = dict(
            task_id=task["id"],
            expected_revision=task["revision"],
            expected_etag=task["etag"],
            progress=progress,
            next_step=next_step,
            findings=findings,
            failures=failures,
            verification=verification,
            operation_id=f"{self.scenario_id}-checkpoint-{key}-{task['revision']}",
        )
        if evidence is not None:
            parameters["evidence"] = evidence
        response = self._require_setup().call("task_checkpoint", **parameters)
        self.checkpoints.setdefault(key, []).append(response)
        return response

    def open_work_unit(self, key: str, task_key: str, *, title: str,
                       status: str = "running", owner: str = "eval-host",
                       progress: str = "", next_step: str = "",
                       blocker: str = "", findings: str = "",
                       failures: str = "", verification: str = "") -> dict:
        self._ensure_claim(task_key)
        response = self._require_setup().call(
            "work_unit_update",
            task_id=self.task_id(task_key),
            title=title,
            owner=owner,
            status=status,
            progress=progress,
            next_step=next_step,
            blocker=blocker,
            findings=findings,
            failures=failures,
            verification=verification,
            operation_id=f"{self.scenario_id}-unit-{key}",
        )
        self.units[key] = response
        return response

    def phase_close(self, key: str, *, title: str, summary: str,
                    next_step: str, findings: str = "",
                    verification: str = "") -> dict:
        task = self.read_task(key)
        self._ensure_claim(key)
        response = self._require_setup().call(
            "task_phase_close",
            task_id=task["id"],
            expected_revision=task["revision"],
            expected_etag=task["etag"],
            title=title,
            summary=summary,
            next_step=next_step,
            findings=findings,
            verification=verification,
            operation_id=f"{self.scenario_id}-phase-{key}",
        )
        self.phases.setdefault(key, []).append(response)
        return response

    def close_task(self, key: str, *, summary: str = "Closed by evaluation fixture",
                   outcome: str = "completed") -> dict:
        task = self.read_task(key)
        self._ensure_claim(key) if outcome == "completed" else None
        acceptance_results = {
            criterion: "host-reported: covered by evaluation fixture evidence"
            for criterion in task.get("acceptance", [])
        }
        response = self._require_setup().call(
            "task_close",
            task_id=task["id"],
            expected_revision=task["revision"],
            expected_etag=task["etag"],
            outcome=outcome,
            summary=summary,
            acceptance_results=acceptance_results,
            operation_id=f"{self.scenario_id}-close-{key}",
        )
        self._claimed_task = None
        return response

    def write_knowledge(self, key: str, *, title: str, body: str,
                        kind: str = "decision", status: str = "active",
                        task_key: str | None = None,
                        evidence: list[dict] | None = None) -> dict:
        if task_key is None:
            sources = [{"type": "host", "reference": "reviewed evaluation fixture evidence"}]
        else:
            sources = [{"type": "task", "reference": self.task_id(task_key)}]
        parameters = dict(
            project_id=self.project_id,
            title=title,
            body=body,
            sources=sources,
            kind=kind,
            status=status,
            operation_id=f"{self.scenario_id}-memory-{key}",
        )
        if evidence is not None:
            parameters["evidence"] = evidence
        response = self._require_setup().call("memory_write", **parameters)
        self.knowledge[key] = response
        return response

    def retire_knowledge(self, key: str, *, title: str, body: str,
                         retired_title: str, retired_body: str) -> dict:
        active = self.write_knowledge(key, title=title, body=body)
        updated = self._require_setup().call(
            "memory_update",
            memory_id=active["id"],
            expected_revision=active["revision"],
            expected_etag=active["etag"],
            title=retired_title,
            body=retired_body,
            status="retired",
            operation_id=f"{self.scenario_id}-retire-{key}",
        )
        record = dict(self.knowledge[key])
        record.update(updated)
        self.knowledge[key] = record
        return record

    # -- legacy records (written before the evidence field existed) ---------

    def legacy_checkpoint(self, task_key: str, *, checkpoint_id: str, sequence: int,
                          title: str, findings: str = "", progress: str = "",
                          next_step: str = "",
                          created_at: str = "2026-11-07T00:00:00+00:00") -> dict:
        """Write a checkpoint record that has no ``evidence`` field at all.

        Every current Service write path always persists the field (an explicit
        ``[]`` when absent), so an actual legacy record has to be committed
        through the storage primitive, exactly as the P1 integration test does.
        This is fixture setup for a compatibility boundary, not product logic.
        """
        task = self.read_task(task_key)
        record = {
            "id": checkpoint_id, "type": "checkpoint", "scope": "task",
            "project_id": task["project_id"], "task_id": task["id"],
            "title": title, "sequence": sequence, "status": "recorded",
            "body": f"# {title}\n\n## Findings\n{findings}\n",
            "revision": 1, "created_at": created_at, "updated_at": created_at,
            "origin": "legacy_fixture", "findings": findings, "progress": progress,
            "failures": "", "verification": "", "next_step": next_step,
        }
        return self._require_setup().service.vault.commit(
            f"{self.scenario_id}-legacy-checkpoint-{checkpoint_id}",
            {"action": "legacy_checkpoint", "id": checkpoint_id},
            lambda: (
                [{"record": record, "path": f"checkpoints/{checkpoint_id}.md",
                  "expected": None}],
                {"id": checkpoint_id},
            ),
        )

    def legacy_knowledge(self, *, memory_id: str, title: str, body: str,
                         kind: str = "fact",
                         created_at: str = "2026-11-07T00:00:00+00:00") -> dict:
        """Write v0 knowledge that predates the ``evidence`` metadata field."""
        record = {
            "id": memory_id, "type": "memory", "scope": "project",
            "project_id": self.project_id, "title": title, "body": body,
            "revision": 1, "status": "active", "kind": kind,
            "created_at": created_at, "updated_at": created_at,
            "sources": [{"type": "host", "reference": "legacy evaluation fixture"}],
        }
        return self._require_setup().service.vault.commit(
            f"{self.scenario_id}-legacy-memory-{memory_id}",
            {"action": "legacy_memory", "id": memory_id},
            lambda: (
                [{"record": record, "path": f"knowledge/{memory_id}.md",
                  "expected": None}],
                {"id": memory_id},
            ),
        )


# ---------------------------------------------------------------------------
# Observations and checks
# ---------------------------------------------------------------------------


def facts_from_prepared(prepared: dict) -> dict:
    context = prepared.get("context") or {}
    text = context.get("text") or ""
    included = context.get("included_sources") or []
    attention = context.get("attention") or prepared.get("attention") or []
    reboot = context.get("reboot") or {}
    project = prepared.get("project") or {}
    background = prepared.get("background_task") or {}
    execution = prepared.get("execution") or {}
    tasks = prepared.get("tasks") or []
    return {
        "state": prepared.get("state"),
        "task_id": prepared.get("task_id"),
        "project_id": project.get("id"),
        "context_present": bool(context),
        "text": text,
        "chars": len(text),
        "truncated": bool(context.get("truncated")),
        "budget": context.get("budget_characters"),
        "included": included,
        "included_ids": [item.get("id") for item in included],
        "included_types": sorted({
            str(item.get("type")) for item in included if item.get("type")
        }),
        "memory_count": sum(1 for item in included if item.get("type") == "memory"),
        "attention_codes": [item.get("code") for item in attention],
        "reboot": reboot,
        "reboot_next": reboot.get("next") or "",
        "reboot_goal": reboot.get("goal") or "",
        "contract_state": (reboot.get("contract") or {}).get("state"),
        "tasks": tasks,
        "task_ids": [item.get("id") for item in tasks],
        "background_task_id": background.get("id"),
        "execution_state": execution.get("state"),
    }


def make_observation(facts: dict, surface_calls: int, wall_ms: float,
                     error: str | None = None) -> dict:
    return {
        "facts": facts,
        "surface_calls": surface_calls,
        "wall_ms": wall_ms,
        "error": error,
    }


def observation_summary(observation: dict) -> dict:
    facts = observation.get("facts") or {}
    chars = facts.get("chars") or 0
    return {
        "state": facts.get("state"),
        "task_id": facts.get("task_id"),
        "project_id": facts.get("project_id"),
        "context_present": bool(facts.get("context_present")),
        "context_chars": chars,
        "context_tokens_estimate": estimate_tokens(chars),
        "context_truncated": facts.get("truncated"),
        "included_types": facts.get("included_types") or [],
        "included_count": len(facts.get("included") or []),
        "knowledge_injected": (facts.get("memory_count") or 0) > 0,
        "attention_codes": facts.get("attention_codes") or [],
        "execution_state": facts.get("execution_state"),
        "surface_calls": observation.get("surface_calls", 0),
        "wall_ms": observation.get("wall_ms", 0.0),
        "error": observation.get("error"),
        "knowledge_index_injection": facts.get("knowledge_index_report"),
    }


@dataclass
class Check:
    name: str
    kind: str  # "recall" (memory gain) or "safety" (invariant in every condition)
    detail: str
    expected: dict[str, bool]
    observe: Callable[[dict], tuple[bool, str]]


def _contains(fragment: str, label: str | None = None):
    label = label or fragment

    def observe(facts: dict) -> tuple[bool, str]:
        text = facts.get("text") or ""
        found = fragment in text
        return found, f"{'found' if found else 'missing'} {label!r} ({len(text)} chars)"

    return observe


def _not_contains(fragment: str, label: str | None = None):
    label = label or fragment

    def observe(facts: dict) -> tuple[bool, str]:
        text = facts.get("text") or ""
        found = fragment in text
        return (not found), f"{'found (unexpected)' if found else 'absent'} {label!r}"

    return observe


def _equals(key: str, value: object):
    def observe(facts: dict) -> tuple[bool, str]:
        observed = facts.get(key)
        return observed == value, f"{key}={observed!r}"

    return observe


def _truthy(key: str):
    def observe(facts: dict) -> tuple[bool, str]:
        observed = facts.get(key)
        return bool(observed), f"{key}={observed!r}"

    return observe


def _in_list(key: str, needle: object):
    def observe(facts: dict) -> tuple[bool, str]:
        values = facts.get(key) or []
        return needle in values, f"{key}={values!r}"

    return observe


def _in_list_lazy(key: str, getter: Callable[[], object]):
    """Like ``_in_list`` but resolves the needle at evaluation time.

    Some scenario checks reference knowledge ids created by the later C-condition
    setup, so the id is not available when the check list is constructed.
    """

    def observe(facts: dict) -> tuple[bool, str]:
        needle = getter()
        values = facts.get(key) or []
        return (needle is not None and needle in values), (
            f"{key}={values!r} needle={needle!r}"
        )

    return observe


def _attention_contains(code: str):
    def observe(facts: dict) -> tuple[bool, str]:
        codes = facts.get("attention_codes") or []
        return code in codes, f"attention={codes!r}"

    return observe


def _chars_at_most(limit: int):
    def observe(facts: dict) -> tuple[bool, str]:
        chars = facts.get("chars") or 0
        return chars <= limit, f"context chars={chars} limit={limit}"

    return observe


def _truncated_or_no_context():
    def observe(facts: dict) -> tuple[bool, str]:
        if not facts.get("context_present"):
            return True, "no context in baseline"
        truncated = bool(facts.get("truncated"))
        return truncated, f"truncated={truncated}"

    return observe


def _new_intent_is_clean():
    def observe(facts: dict) -> tuple[bool, str]:
        task_id = facts.get("new_intent_task_id")
        tasks = facts.get("new_intent_tasks") or []
        return (task_id is None and not tasks), (
            f"new-intent task_id={task_id!r} tasks={len(tasks)}"
        )

    return observe


# ---------------------------------------------------------------------------
# Scenario container and measurement
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    id: str
    title: str
    description: str
    prompt: str
    tags: tuple[str, ...]
    query: str
    build: Callable[[Fixture], None]
    make_checks: Callable[[Fixture], list[Check]]
    learn: Callable[[Fixture], None] | None = None
    probe: Callable[[Session, dict, Fixture, str], dict] | None = None
    measure: Callable[["Scenario", Fixture, str], dict] | None = None
    budget: int = DEFAULT_BUDGET


def default_measure(scenario: Scenario, fixture: Fixture, condition: str) -> dict:
    """Measure one condition in a brand-new service session (first turn)."""
    home = fixture.home_for(condition)
    session = fixture.session(home)
    facts: dict = {}
    error: str | None = None
    try:
        with session:
            prepared = session.prepare(
                intent="resume", query=scenario.query, budget=scenario.budget,
            )
            facts.update(facts_from_prepared(prepared))
            if scenario.probe is not None:
                try:
                    facts.update(scenario.probe(session, prepared, fixture, condition) or {})
                except Exception as exc:  # a probe must not mask the measurement
                    facts["probe_error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        facts.setdefault("state", "error")
    finally:
        session.close()
    return make_observation(facts, session.surface_calls, session.wall_ms, error)


# -- scenario 1: long task recovery after compaction ------------------------


def build_long_task_recovery(fixture: Fixture) -> None:
    fixture.register("Quasar gateway continuity")
    fixture.open_task(
        "main",
        title="Recover refresh rotation work",
        goal=(
            "Make the quasar token rotation keep an explicit boundary so renewal never "
            "extends lifetime."
        ),
        acceptance=["quasar refresh regression passes", "boundary rule recorded"],
    )
    fixture.claim("main")
    fixture.checkpoint(
        "main",
        progress="Reproduced the refresh failure on the renewal path.",
        findings="The sentinel check runs before the client-credentials lookup.",
        failures="The first fix extended token lifetime and violated the expiry invariant.",
        verification="python -m unittest tests.test_context failed as expected",
        next_step="Trace the rotation boundary call site before changing behaviour.",
    )
    fixture.checkpoint(
        "main",
        progress="Located the rotation boundary after the second compaction.",
        findings="The boundary belongs in the refresh handler, not in the token store.",
        failures="The first fix extended token lifetime and violated the expiry invariant.",
        verification="python -m unittest tests.test_context failed as expected",
        next_step="Reject expired refresh tokens before the lookup, then rerun the focused suite.",
    )
    fixture.open_work_unit(
        "boundary",
        "main",
        title="rotation boundary shard",
        progress="Handler map is ready.",
        next_step="Apply the boundary in the refresh handler.",
    )


LEARN_LONG_TASK = "Rejecting expired refresh tokens before the lookup keeps rotation safe."


def learn_long_task_recovery(fixture: Fixture) -> None:
    fixture.write_knowledge(
        "boundary_rule",
        title="Quasar refresh boundary rule",
        body=(
            "Reviewed quasar decision: renewal never extends token lifetime. "
            "Rejecting expired refresh tokens before the lookup keeps rotation safe. "
            "Retrieval cues: quasar refresh rotation boundary 刷新边界."
        ),
        kind="decision",
        task_key="main",
    )


def checks_long_task_recovery(fixture: Fixture) -> list[Check]:
    return [
        Check(
            "resume_ready", "recall",
            "workspace_prepare(resume) reaches a ready state",
            {"A": False, "B": True, "C": True}, _equals("state", "ready"),
        ),
        Check(
            "task_identity", "recall",
            "the resumed session is bound to the current task",
            {"A": False, "B": True, "C": True},
            _equals("task_id", fixture.task_id("main")),
        ),
        Check(
            "goal_injected", "recall",
            "the task goal is present in the first-turn context",
            {"A": False, "B": True, "C": True},
            _contains("explicit boundary so renewal never extends lifetime"),
        ),
        Check(
            "next_step_injected", "recall",
            "the latest next step is present in the first-turn context",
            {"A": False, "B": True, "C": True},
            _contains("Reject expired refresh tokens before the lookup"),
        ),
        Check(
            "failure_history_injected", "recall",
            "the failed route survives compaction and is surfaced",
            {"A": False, "B": True, "C": True},
            _contains("first fix extended token lifetime and violated the expiry invariant"),
        ),
        Check(
            "work_unit_injected", "recall",
            "open WorkUnit state is injected",
            {"A": False, "B": True, "C": True},
            _contains("rotation boundary shard"),
        ),
        Check(
            "contract_attested", "recall",
            "the task contract is reported as attested",
            {"A": False, "B": True, "C": True},
            _equals("contract_state", "attested"),
        ),
        Check(
            "knowledge_injected", "recall",
            "reviewed long-term knowledge is added in condition C",
            {"A": False, "B": False, "C": True},
            _contains("Rejecting expired refresh tokens before the lookup keeps rotation safe"),
        ),
        Check(
            "context_within_budget", "safety",
            "injected context respects the character budget",
            {"A": True, "B": True, "C": True},
            _chars_at_most(DEFAULT_BUDGET),
        ),
    ]


# -- scenario 2: new session in the same project ----------------------------


def build_new_session_resume(fixture: Fixture) -> None:
    fixture.register("Zephyr release continuity")
    fixture.open_task(
        "resume",
        title="Resume release checklist",
        goal="Ship the zephyr release checklist with the windows packaging smoke test.",
        acceptance=["zephyr packaging smoke passes"],
    )
    fixture.claim("resume")
    fixture.checkpoint(
        "resume",
        progress="Checklist reviewed through the packaging step.",
        findings="Windows packaging smoke is the last blocking item.",
        next_step="Run the zephyr packaging smoke test before tagging.",
    )
    fixture.open_task(
        "queue",
        title="Backlog docs refresh",
        goal="Refresh the backlog documentation in a later session.",
        acceptance=["backlog docs lint passes"],
    )


LEARN_NEW_SESSION = "The zephyr packaging smoke test must pass before tagging a release."


def learn_new_session_resume(fixture: Fixture) -> None:
    fixture.write_knowledge(
        "release_rule",
        title="Zephyr release gate",
        body=(
            "Reviewed release procedure: the zephyr packaging smoke test must pass before "
            "tagging a release."
        ),
        kind="procedure",
        task_key="resume",
    )


def probe_new_intent(session: Session, prepared: dict, fixture: Fixture,
                     condition: str) -> dict:
    """Calling prepare with intent=new must not silently bind the old task."""
    fresh = session.prepare(intent="new", query="", budget=DEFAULT_BUDGET)
    background = fresh.get("background_task") or {}
    return {
        "new_intent_state": fresh.get("state"),
        "new_intent_task_id": fresh.get("task_id"),
        "new_intent_tasks": fresh.get("tasks") or [],
        "new_intent_background_id": background.get("id"),
    }


def checks_new_session_resume(fixture: Fixture) -> list[Check]:
    current = fixture.task_id("resume")
    queued = fixture.task_id("queue")
    return [
        Check(
            "new_session_ready", "recall",
            "a fresh service session in the same project resumes the current task",
            {"A": False, "B": True, "C": True},
            _equals("task_id", current),
        ),
        Check(
            "next_step_injected", "recall",
            "the previous session's next step is recovered without user restatement",
            {"A": False, "B": True, "C": True},
            _contains("Run the zephyr packaging smoke test before tagging"),
        ),
        Check(
            "queued_task_not_selected", "safety",
            "the queued task is not selected instead of the current task",
            {"A": True, "B": True, "C": True},
            lambda facts: (
                facts.get("task_id") != queued,
                f"selected={facts.get('task_id')!r} queued={queued!r}",
            ),
        ),
        Check(
            "new_intent_has_no_task", "safety",
            "intent=new does not inherit a task context",
            {"A": True, "B": True, "C": True},
            _new_intent_is_clean(),
        ),
        Check(
            "new_intent_background_visible", "recall",
            "intent=new still reports the current main task as background",
            {"A": False, "B": True, "C": True},
            _equals("new_intent_background_id", current),
        ),
        Check(
            "knowledge_injected", "recall",
            "reviewed release knowledge is reused in the new session (C only)",
            {"A": False, "B": False, "C": True},
            _contains("zephyr packaging smoke test must pass before tagging"),
        ),
        Check(
            "context_within_budget", "safety",
            "injected context respects the character budget",
            {"A": True, "B": True, "C": True},
            _chars_at_most(DEFAULT_BUDGET),
        ),
    ]


# -- scenario 3: cross-client handoff on one home ---------------------------


def build_cross_client(fixture: Fixture) -> None:
    fixture.register("Nebula cross-client continuity")
    fixture.open_task(
        "main",
        title="Cross-client handoff",
        goal="Keep the nebula cross-client handoff recoverable after a client switch.",
        acceptance=["nebula handoff note recovered by the second client"],
    )
    fixture.claim("main")
    fixture.checkpoint(
        "main",
        progress="First client reproduced the handoff gap.",
        findings="The second client must wait for the first claim to be released.",
        next_step="Run the nebula cross-client handoff check from the second client.",
    )
    fixture.open_work_unit(
        "handoff",
        "main",
        title="nebula handoff shard",
        progress="Claim behaviour confirmed once.",
        next_step="Confirm the second client does not take the claim silently.",
    )


LEARN_CROSS_CLIENT = (
    "Reviewed nebula handoff rule: the second client must wait for the first claim "
    "to be released before continuing."
)


def learn_cross_client(fixture: Fixture) -> None:
    fixture.write_knowledge(
        "handoff_rule",
        title="Nebula cross-client handoff rule",
        body=(
            "Reviewed nebula handoff rule: the second client must wait for the first claim "
            "to be released before continuing. Retrieval cues: nebula cross-client handoff."
        ),
        kind="constraint",
        task_key="main",
    )


def measure_cross_client(scenario: Scenario, fixture: Fixture, condition: str) -> dict:
    home = fixture.home_for(condition)
    facts: dict = {}
    error: str | None = None
    first = fixture.session(home)
    second = fixture.session(home)
    handoff = fixture.session(home)
    try:
        with first:
            prepared_first = first.prepare(
                intent="resume", query=scenario.query, budget=scenario.budget,
            )
            facts["first_client_state"] = prepared_first.get("state")
            with second:
                blocked = second.prepare(
                    intent="resume", query=scenario.query, budget=scenario.budget,
                )
                facts["blocked_state"] = blocked.get("state")
                facts["blocked_context_present"] = bool(blocked.get("context"))
                facts["blocked_task_id"] = blocked.get("task_id")
        with handoff:
            prepared = handoff.prepare(
                intent="resume", query=scenario.query, budget=scenario.budget,
            )
            facts.update(facts_from_prepared(prepared))
            facts["handoff_state"] = prepared.get("state")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        facts.setdefault("state", "error")
    finally:
        first.close()
        second.close()
        handoff.close()
    calls = first.surface_calls + second.surface_calls + handoff.surface_calls
    wall = first.wall_ms + second.wall_ms + handoff.wall_ms
    return make_observation(facts, calls, wall, error)


def checks_cross_client(fixture: Fixture) -> list[Check]:
    return [
        Check(
            "live_driver_blocks_second_client", "recall",
            "a second client sees task_claimed while the first driver is live",
            {"A": False, "B": True, "C": True},
            _equals("blocked_state", "task_claimed"),
        ),
        Check(
            "blocked_client_gets_no_task", "safety",
            "the blocked client receives no task context and no task binding",
            {"A": True, "B": True, "C": True},
            lambda facts: (
                (not facts.get("blocked_context_present"))
                and facts.get("blocked_task_id") is None,
                f"context={facts.get('blocked_context_present')!r} "
                f"task_id={facts.get('blocked_task_id')!r}",
            ),
        ),
        Check(
            "handoff_after_release", "recall",
            "after the first client exits, the second client acquires the task",
            {"A": False, "B": True, "C": True},
            lambda facts: (
                facts.get("handoff_state") == "ready"
                and facts.get("task_id") == fixture.task_id("main")
                and facts.get("execution_state") == "acquired",
                f"state={facts.get('handoff_state')!r} task_id={facts.get('task_id')!r} "
                f"execution={facts.get('execution_state')!r}",
            ),
        ),
        Check(
            "next_step_after_handoff", "recall",
            "the second client recovers the next step written by the first client",
            {"A": False, "B": True, "C": True},
            _contains("Run the nebula cross-client handoff check"),
        ),
        Check(
            "same_project_identity", "recall",
            "both clients resolve the same project identity from one home",
            {"A": False, "B": True, "C": True},
            _equals("project_id", fixture.project_id),
        ),
        Check(
            "knowledge_after_handoff", "recall",
            "reviewed knowledge is available to the second client (C only)",
            {"A": False, "B": False, "C": True},
            _contains("Reviewed nebula handoff rule"),
        ),
        Check(
            "context_within_budget", "safety",
            "injected context respects the character budget",
            {"A": True, "B": True, "C": True},
            _chars_at_most(DEFAULT_BUDGET),
        ),
    ]


# -- scenario 4: phase knowledge reuse in the next task ---------------------


def build_phase_reuse(fixture: Fixture) -> None:
    fixture.register("Krypton boundary reuse")
    fixture.open_task(
        "investigation",
        title="Auth boundary investigation",
        goal="Clarify the krypton auth boundary before implementing it.",
        acceptance=["krypton boundary documented"],
    )
    fixture.claim("investigation")
    fixture.phase_close(
        "investigation",
        title="krypton boundary investigation",
        summary="The krypton boundary must be explicit in the renewal handler.",
        findings="Boundary state is lost when the renewal handler re-reads the token.",
        verification="focused investigation test captured the boundary loss",
        next_step="Apply the boundary in the next related task.",
    )
    fixture.close_task(
        "investigation",
        summary="Investigation completed; reusable conclusion is ready for review.",
    )
    fixture.open_task(
        "implementation",
        title="Krypton boundary implementation",
        goal="Apply the krypton boundary rule in the renewal handler of the implementation task.",
        acceptance=["krypton renewal handler updated"],
    )
    fixture.claim("implementation")
    fixture.checkpoint(
        "implementation",
        progress="Renewal handler located.",
        next_step="Apply the krypton boundary before the token lookup.",
    )


LEARN_PHASE_REUSE = (
    "Reviewed krypton boundary rule: the renewal handler applies the boundary before lookup."
)
UNRELATED_PHASE_REUSE = (
    "Payroll ledger cutover moves monthly reconciliation into the night window."
)


def learn_phase_reuse(fixture: Fixture) -> None:
    fixture.write_knowledge(
        "boundary",
        title="Krypton boundary rule",
        body=(
            "Reviewed krypton boundary rule: the renewal handler applies the boundary before "
            "lookup so boundary state is never lost. Retrieval cues: krypton boundary 边界."
        ),
        kind="decision",
        task_key="investigation",
    )
    fixture.write_knowledge(
        "unrelated",
        title="Payroll ledger cutover",
        body=UNRELATED_PHASE_REUSE,
        kind="fact",
    )


def checks_phase_reuse(fixture: Fixture) -> list[Check]:
    return [
        Check(
            "next_task_recovered", "recall",
            "the related follow-up task is recovered as current",
            {"A": False, "B": True, "C": True},
            _equals("task_id", fixture.task_id("implementation")),
        ),
        Check(
            "task_goal_injected", "recall",
            "the follow-up task goal is injected",
            {"A": False, "B": True, "C": True},
            _contains("Apply the krypton boundary rule in the renewal handler"),
        ),
        Check(
            "next_step_injected", "recall",
            "the follow-up task next step is injected",
            {"A": False, "B": True, "C": True},
            _contains("Apply the krypton boundary before the token lookup"),
        ),
        Check(
            "pending_review_attention", "recall",
            "the completed task's pending knowledge review is surfaced",
            {"A": False, "B": True, "C": True},
            _attention_contains("pending_consolidation"),
        ),
        Check(
            "phase_knowledge_reused", "recall",
            "knowledge written after phase closure is recalled for the related task (C only)",
            {"A": False, "B": False, "C": True},
            _contains("renewal handler applies the boundary before lookup"),
        ),
        Check(
            "unrelated_knowledge_excluded", "safety",
            "unrelated project knowledge is not injected",
            {"A": True, "B": True, "C": True},
            _not_contains("payroll ledger cutover"),
        ),
        Check(
            "context_within_budget", "safety",
            "injected context respects the character budget",
            {"A": True, "B": True, "C": True},
            _chars_at_most(DEFAULT_BUDGET),
        ),
    ]


# -- scenario 5: stale / wrong knowledge trap ------------------------------


def build_stale_trap(fixture: Fixture) -> None:
    fixture.register("Monet deploy safety")
    fixture.open_task(
        "main",
        title="Payment worker deployment",
        goal="Deploy the monet worker from the correct branch.",
        acceptance=["monet deploy dry-run passes"],
    )
    fixture.claim("main")
    fixture.checkpoint(
        "main",
        progress="Deployment plan prepared.",
        findings="The branch decision predates the regression requirement.",
        next_step="Check the monet deploy branch decision before the dry run.",
    )


LEARN_STALE_TRAP = "Reviewed monet deploy decision: deploy from main only after the regression suite."


def learn_stale_trap(fixture: Fixture) -> None:
    fixture.write_knowledge(
        "active_decision",
        title="Monet deploy branch decision",
        body=(
            "Reviewed monet deploy decision: deploy from main only after the payment regression "
            "suite passes. Deploying from release/v1 is no longer valid."
        ),
        kind="decision",
        task_key="main",
    )
    fixture.retire_knowledge(
        "retired_rule",
        title="Monet deploy branch rule (old)",
        body="Legacy monet deploy rule: deploy from branch release/v1 on every hotfix.",
        retired_title="Monet deploy branch rule (retired)",
        retired_body=(
            "Retired: the release/v1 hotfix branch rule was overturned; deploy from main only "
            "after the payment regression suite passes."
        ),
    )
    fixture.write_knowledge(
        "candidate_hotfix",
        title="Monet deploy hotfix path",
        body=(
            "Unreviewed monet hotfix idea: deploy from deploy-hotfix without the payment "
            "regression suite."
        ),
        kind="fact",
        status="candidate",
    )


def probe_retired_search(session: Session, prepared: dict, fixture: Fixture,
                         condition: str) -> dict:
    facts = {"retired_search_ids": [], "retired_search_error": None}
    if not prepared.get("project") or prepared.get("state") not in (
        "ready", "no_active_task", "task_claimed",
    ):
        return facts
    try:
        result = session.call(
            "memory_search",
            project_id=fixture.project_id,
            query="monet deploy branch",
            status="retired",
        )
    except DiagnosticError as exc:
        facts["retired_search_error"] = exc.code
        return facts
    facts["retired_search_ids"] = [item["id"] for item in result.get("items", [])]
    return facts


def checks_stale_trap(fixture: Fixture) -> list[Check]:
    return [
        Check(
            "task_recovered", "recall",
            "the deploy task is recovered",
            {"A": False, "B": True, "C": True},
            _equals("task_id", fixture.task_id("main")),
        ),
        Check(
            "next_step_injected", "recall",
            "the deploy next step is injected",
            {"A": False, "B": True, "C": True},
            _contains("Check the monet deploy branch decision before the dry run"),
        ),
        Check(
            "correct_knowledge_injected", "recall",
            "the corrected active knowledge is recalled (C only)",
            {"A": False, "B": False, "C": True},
            _contains("deploy from main only after the payment regression suite passes"),
        ),
        Check(
            "retired_wrong_rule_excluded", "safety",
            "the retired wrong branch rule is not injected",
            {"A": True, "B": True, "C": True},
            _not_contains("deploy from branch release/v1"),
        ),
        Check(
            "candidate_hypothesis_excluded", "safety",
            "unreviewed candidate knowledge is not injected",
            {"A": True, "B": True, "C": True},
            _not_contains("deploy from deploy-hotfix"),
        ),
        Check(
            "retired_management_access", "recall",
            "retired knowledge stays explicitly searchable but out of default recall (C only)",
            {"A": False, "B": False, "C": True},
            _in_list_lazy(
                "retired_search_ids",
                lambda: fixture.knowledge.get("retired_rule", {}).get("id"),
            ),
        ),
        Check(
            "context_within_budget", "safety",
            "injected context respects the character budget",
            {"A": True, "B": True, "C": True},
            _chars_at_most(DEFAULT_BUDGET),
        ),
    ]


# -- scenario 6: budget=500 injection floor --------------------------------


def build_budget_floor(fixture: Fixture) -> None:
    fixture.register("Orion budget floor")
    fixture.open_task(
        "main",
        title="Budget floor continuity",
        goal="Keep the orion budget brief recoverable at the smallest context budget.",
        acceptance=[
            "orion next step visible at 500",
            "orion acceptance summary visible at 500",
        ],
    )
    fixture.claim("main")
    fixture.checkpoint(
        "main",
        progress="Budget pressure reproduced with a large knowledge set.",
        findings="Knowledge and WorkUnits compete with the task brief at 500 characters.",
        failures="An earlier arrangement dropped the next step out of the injected context.",
        verification="manual probe of budget=500 showed the task brief clipped",
        next_step="Rerun the orion budget check after the fix.",
    )
    for index in range(1, 4):
        fixture.open_work_unit(
            f"unit{index}",
            "main",
            title=f"orion budget shard {index}",
            progress=(
                f"Shard {index} is exercising the small-budget allocation path with enough "
                "text to compete for space against the task brief."
            ),
            next_step=f"Finish orion budget shard {index} after the brief stays visible.",
        )


LEARN_BUDGET_FLOOR = "Reviewed orion budget rule: the task brief must survive the 500 character floor."


def learn_budget_floor(fixture: Fixture) -> None:
    for index in range(1, 7):
        fixture.write_knowledge(
            f"note{index}",
            title=f"Orion budget artifact {index}",
            body=(
                f"Reviewed orion budget floor note {index}. The context builder must keep the "
                "task brief visible even when several long knowledge records compete for the "
                "same character budget. This note is deliberately verbose so the injection "
                "pressure is real and the allocation path is exercised end to end. "
                f"Artifact marker orion-note-{index}. Retrieval cues: orion budget floor."
            ),
            kind="lesson",
        )


def checks_budget_floor(fixture: Fixture) -> list[Check]:
    return [
        Check(
            "budget_respected", "safety",
            "the injected context never exceeds the 500 character budget",
            {"A": True, "B": True, "C": True},
            _chars_at_most(BUDGET_FLOOR),
        ),
        Check(
            "truncation_reported", "safety",
            "truncation is reported when content does not fit",
            {"A": True, "B": True, "C": True},
            _truncated_or_no_context(),
        ),
        Check(
            "task_identity_at_500", "recall",
            "the current task identity survives the 500 character floor",
            {"A": False, "B": True, "C": True},
            _equals("task_id", fixture.task_id("main")),
        ),
        Check(
            "next_step_at_500", "recall",
            "the task next step survives the 500 character floor",
            {"A": False, "B": True, "C": True},
            _contains("Rerun the orion budget check after the fix"),
        ),
        Check(
            "acceptance_at_500", "recall",
            "an acceptance summary survives the 500 character floor",
            {"A": False, "B": True, "C": True},
            _contains("orion acceptance summary visible"),
        ),
        Check(
            "goal_at_500", "recall",
            "the task goal survives the 500 character floor",
            {"A": False, "B": True, "C": True},
            _contains("orion budget brief recoverable"),
        ),
    ]


# -- scenario 7: concurrent write conflicts --------------------------------


def build_write_conflict(fixture: Fixture) -> None:
    fixture.register("Pyxis writer guard")
    fixture.open_task(
        "main",
        title="Concurrent writer guard",
        goal="Keep the pyxis task writer conflict-safe.",
        acceptance=["pyxis checkpoint conflict detected"],
    )
    fixture.claim("main")
    fixture.checkpoint(
        "main",
        progress="Writer guard scenario prepared.",
        next_step="Start the pyxis conflict probe.",
    )
    fixture.open_work_unit(
        "writer",
        "main",
        title="pyxis writer shard",
        progress="WorkUnit ready for a stale-revision update.",
        next_step="Confirm a stale WorkUnit update is rejected.",
    )


LEARN_WRITE_CONFLICT = "Reviewed pyxis writer rule: one writer per record; stale revisions must conflict."


def learn_write_conflict(fixture: Fixture) -> None:
    fixture.write_knowledge(
        "writer_rule",
        title="Pyxis writer rule",
        body=(
            "Reviewed pyxis writer rule: one writer per record; stale revisions must conflict "
            "instead of overwriting. Retrieval cues: pyxis concurrent writer conflict."
        ),
        kind="constraint",
        task_key="main",
    )


CHECKPOINT_WINNER = "pyxis winner checkpoint"
CHECKPOINT_LOSER = "pyxis loser checkpoint"
UNIT_WINNER = "pyxis winner work unit"
UNIT_LOSER = "pyxis loser work unit"
KNOWLEDGE_WINNER = "pyxis winner knowledge"
KNOWLEDGE_LOSER = "pyxis loser knowledge"


def measure_write_conflict(scenario: Scenario, fixture: Fixture, condition: str) -> dict:
    home = fixture.home_for(condition)
    facts: dict = {}
    error: str | None = None
    session = fixture.session(home)
    try:
        with session:
            prepared = session.prepare(
                intent="resume", query=scenario.query, budget=scenario.budget,
            )
            facts.update(facts_from_prepared(prepared))
            if prepared.get("state") != "ready":
                return make_observation(
                    facts, session.surface_calls, session.wall_ms, None,
                )
            task_id = prepared["task_id"]
            task = session.call("task_read", task_id=task_id)
            expected_revision, expected_etag = task["revision"], task["etag"]

            def checkpoint(next_step: str, operation_id: str) -> str | None:
                try:
                    session.call(
                        "task_checkpoint",
                        task_id=task_id,
                        expected_revision=expected_revision,
                        expected_etag=expected_etag,
                        progress="conflict probe",
                        next_step=next_step,
                        operation_id=operation_id,
                    )
                    return None
                except DiagnosticError as exc:
                    return exc.code

            first_code = checkpoint(CHECKPOINT_WINNER, f"eval-conflict-first-{condition}")
            second_code = checkpoint(CHECKPOINT_LOSER, f"eval-conflict-second-{condition}")
            facts["checkpoint_first_write_code"] = first_code
            facts["checkpoint_conflict_code"] = second_code
            facts["checkpoint_conflict_detected"] = second_code == "VERSION_CONFLICT"
            after = session.call("task_read", task_id=task_id)
            facts["checkpoint_next_step"] = after.get("next_step", "")
            facts["checkpoint_no_silent_overwrite"] = (
                after.get("next_step") == CHECKPOINT_WINNER
                and CHECKPOINT_LOSER not in (after.get("next_step") or "")
            )

            units = after.get("work_units") or []
            if units:
                unit = units[0]
                unit_first = None
                try:
                    session.call(
                        "work_unit_update",
                        unit_id=unit["id"],
                        expected_revision=unit["revision"],
                        expected_etag=unit["etag"],
                        status="running",
                        next_step=UNIT_WINNER,
                        operation_id=f"eval-unit-first-{condition}",
                    )
                except DiagnosticError as exc:
                    unit_first = exc.code
                unit_second = None
                try:
                    session.call(
                        "work_unit_update",
                        unit_id=unit["id"],
                        expected_revision=unit["revision"],
                        expected_etag=unit["etag"],
                        status="running",
                        next_step=UNIT_LOSER,
                        operation_id=f"eval-unit-second-{condition}",
                    )
                except DiagnosticError as exc:
                    unit_second = exc.code
                facts["unit_first_write_code"] = unit_first
                facts["unit_conflict_code"] = unit_second
                facts["unit_conflict_detected"] = unit_second == "VERSION_CONFLICT"
                refreshed = session.call("task_read", task_id=task_id)
                refreshed_units = refreshed.get("work_units") or []
                unit_step = refreshed_units[0].get("next_step", "") if refreshed_units else ""
                facts["unit_next_step"] = unit_step
                facts["unit_no_silent_overwrite"] = (
                    unit_step == UNIT_WINNER and UNIT_LOSER not in unit_step
                )

            if condition == "C" and "writer_rule" in fixture.knowledge:
                memory = fixture.knowledge["writer_rule"]
                knowledge_first = None
                try:
                    session.call(
                        "memory_update",
                        memory_id=memory["id"],
                        expected_revision=memory["revision"],
                        expected_etag=memory["etag"],
                        title="Pyxis writer rule",
                        body=KNOWLEDGE_WINNER,
                        operation_id=f"eval-knowledge-first-{condition}",
                    )
                except DiagnosticError as exc:
                    knowledge_first = exc.code
                knowledge_second = None
                try:
                    session.call(
                        "memory_update",
                        memory_id=memory["id"],
                        expected_revision=memory["revision"],
                        expected_etag=memory["etag"],
                        title="Pyxis writer rule",
                        body=KNOWLEDGE_LOSER,
                        operation_id=f"eval-knowledge-second-{condition}",
                    )
                except DiagnosticError as exc:
                    knowledge_second = exc.code
                facts["knowledge_first_write_code"] = knowledge_first
                facts["knowledge_conflict_code"] = knowledge_second
                facts["knowledge_conflict_detected"] = knowledge_second == "VERSION_CONFLICT"
                final = session.call("memory_read", memory_id=memory["id"])
                body = final.get("body") or ""
                facts["knowledge_body_after_conflict"] = clip(body, 120)
                facts["knowledge_no_silent_overwrite"] = (
                    KNOWLEDGE_WINNER in body and KNOWLEDGE_LOSER not in body
                )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        facts.setdefault("state", "error")
    finally:
        session.close()
    return make_observation(facts, session.surface_calls, session.wall_ms, error)


def checks_write_conflict(fixture: Fixture) -> list[Check]:
    return [
        Check(
            "task_context_recovered", "recall",
            "the writer-guard task is recovered before the conflict probe",
            {"A": False, "B": True, "C": True},
            _equals("task_id", fixture.task_id("main")),
        ),
        Check(
            "checkpoint_conflict_detected", "recall",
            "a stale checkpoint write is rejected with VERSION_CONFLICT",
            {"A": False, "B": True, "C": True},
            _truthy("checkpoint_conflict_detected"),
        ),
        Check(
            "checkpoint_no_silent_overwrite", "recall",
            "the first checkpoint writer is preserved, not silently overwritten",
            {"A": False, "B": True, "C": True},
            _truthy("checkpoint_no_silent_overwrite"),
        ),
        Check(
            "work_unit_conflict_detected", "recall",
            "a stale WorkUnit write is rejected with VERSION_CONFLICT",
            {"A": False, "B": True, "C": True},
            _truthy("unit_conflict_detected"),
        ),
        Check(
            "work_unit_no_silent_overwrite", "recall",
            "the first WorkUnit writer is preserved",
            {"A": False, "B": True, "C": True},
            _truthy("unit_no_silent_overwrite"),
        ),
        Check(
            "knowledge_conflict_detected", "recall",
            "a stale knowledge write is rejected with VERSION_CONFLICT (C only)",
            {"A": False, "B": False, "C": True},
            _truthy("knowledge_conflict_detected"),
        ),
        Check(
            "knowledge_no_silent_overwrite", "recall",
            "the first knowledge writer is preserved (C only)",
            {"A": False, "B": False, "C": True},
            _truthy("knowledge_no_silent_overwrite"),
        ),
        Check(
            "context_within_budget", "safety",
            "injected context respects the character budget",
            {"A": True, "B": True, "C": True},
            _chars_at_most(DEFAULT_BUDGET),
        ),
    ]


# -- scenario 8: structured evidence pointer persistence --------------------


VEGA_EVIDENCE = [
    {
        "kind": "command",
        "reference": "python -m unittest tests.test_storage",
        "hash": "sha256:abc123",
        "observed_at": "2026-11-08T10:00:00+08:00",
        "summary": "focused evidence suite passed",
    },
    {"kind": "file", "reference": "src/memweave/storage.py", "hash": "sha256:deadbeef"},
    {"kind": "user", "reference": "reviewer asked for replayable evidence pointers"},
]
LEGACY_CHECKPOINT_ID = "checkpoint_legacyvega01"
LEGACY_KNOWLEDGE_ID = "memory_legacyvega02"
LEGACY_KNOWLEDGE_TITLE = "Legacy vega knowledge note"
VEGA_QUERY = "vega evidence pointer rule"
VEGA_LEGACY_QUERY = "legacy vega knowledge note"


def build_evidence_persistence(fixture: Fixture) -> None:
    """B world: an evidence-bearing checkpoint plus a pre-evidence checkpoint."""
    fixture.register("Vega evidence continuity")
    fixture.open_task(
        "main",
        title="Evidence pointer persistence",
        goal=(
            "Keep structured evidence pointers recoverable across sessions and Markdown/"
            "index rebuilds."
        ),
        acceptance=[
            "checkpoint evidence reads back",
            "reviewed knowledge evidence is searchable",
        ],
    )
    fixture.claim("main")
    fixture.checkpoint(
        "main",
        progress="Recorded the evidence-backed checkpoint.",
        findings="Structured pointers cover command, file and user evidence.",
        next_step="Rebuild the index and read the same evidence back.",
        evidence=VEGA_EVIDENCE,
    )
    fixture.legacy_checkpoint(
        "main",
        checkpoint_id=LEGACY_CHECKPOINT_ID,
        sequence=2,
        title="Checkpoint 2: legacy checkpoint",
        findings="Legacy checkpoint written before evidence pointers existed.",
        progress="Legacy progress.",
        next_step="Legacy next step.",
    )


def learn_evidence_persistence(fixture: Fixture) -> None:
    fixture.write_knowledge(
        "vega_rule",
        title="Vega evidence pointer rule",
        body=(
            "Reviewed vega evidence pointer rule: checkpoints and reviewed knowledge keep "
            "structured evidence pointers so a later session can replay what was verified. "
            "Retrieval cues: vega evidence pointer rule."
        ),
        kind="decision",
        task_key="main",
        evidence=VEGA_EVIDENCE,
    )
    fixture.legacy_knowledge(
        memory_id=LEGACY_KNOWLEDGE_ID,
        title=LEGACY_KNOWLEDGE_TITLE,
        body="Legacy vega knowledge note written before structured pointers existed.",
    )
    # Index the directly committed legacy record through the product rebuild path.
    fixture.rebuild()


def _evidence_search(session: Session, fixture: Fixture, query: str):
    try:
        result = session.call(
            "memory_search", project_id=fixture.project_id, query=query, limit=10,
        )
    except DiagnosticError as exc:
        return [], exc.code
    return list(result.get("items") or []), None


def _find_memory(hits: list[dict], memory_id: str | None) -> dict | None:
    if not memory_id:
        return None
    return next((item for item in hits if item.get("id") == memory_id), None)


def _read_legacy_knowledge(session: Session, facts: dict, suffix: str) -> None:
    facts[f"legacy_knowledge_read_{suffix}_ok"] = False
    try:
        record = session.call("memory_read", memory_id=LEGACY_KNOWLEDGE_ID)
    except DiagnosticError as exc:
        facts[f"legacy_knowledge_read_{suffix}_error"] = exc.code
        return
    facts[f"legacy_knowledge_read_{suffix}_ok"] = True
    facts[f"legacy_knowledge_evidence_{suffix}"] = record.get("evidence")


def probe_evidence_persistence(session: Session, prepared: dict, fixture: Fixture,
                               condition: str) -> dict:
    facts: dict = {"has_task": prepared.get("state") == "ready"}
    if not facts["has_task"]:
        return facts
    task_id = prepared.get("task_id")
    try:
        before = session.call("task_read", task_id=task_id)
    except DiagnosticError as exc:
        facts["task_read_error"] = exc.code
        return facts
    facts["task_evidence_pre"] = before.get("evidence")
    checkpoints = {
        item.get("id"): item for item in before.get("checkpoints") or []
    }
    main_checkpoint = fixture.checkpoint_id("main", 0)
    facts["checkpoint_evidence_pre"] = (
        checkpoints[main_checkpoint].get("evidence")
        if main_checkpoint in checkpoints else None
    )
    legacy_checkpoint = checkpoints.get(LEGACY_CHECKPOINT_ID)
    facts["legacy_checkpoint_found"] = legacy_checkpoint is not None
    facts["legacy_checkpoint_evidence"] = (
        legacy_checkpoint.get("evidence") if legacy_checkpoint is not None else None
    )

    memory_id = fixture.knowledge.get("vega_rule", {}).get("id")
    hits_before, search_error_before = _evidence_search(session, fixture, VEGA_QUERY)
    match_before = _find_memory(hits_before, memory_id)
    facts["knowledge_search_error_pre"] = search_error_before
    facts["knowledge_hit_id"] = match_before.get("id") if match_before else None
    facts["knowledge_evidence_pre"] = (
        match_before.get("evidence") if match_before else None
    )
    _read_legacy_knowledge(session, facts, "pre")

    try:
        session.call("rebuild")
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed check instead
        facts["rebuild_ok"] = False
        facts["rebuild_error"] = f"{type(exc).__name__}: {exc}"
        return facts
    facts["rebuild_ok"] = True

    after = session.call("task_read", task_id=task_id)
    facts["task_evidence_post"] = after.get("evidence")
    checkpoints_after = {
        item.get("id"): item for item in after.get("checkpoints") or []
    }
    facts["checkpoint_evidence_post"] = (
        checkpoints_after[main_checkpoint].get("evidence")
        if main_checkpoint in checkpoints_after else None
    )
    hits_after, search_error_after = _evidence_search(session, fixture, VEGA_QUERY)
    match_after = _find_memory(hits_after, memory_id)
    facts["knowledge_search_error_post"] = search_error_after
    facts["knowledge_evidence_post"] = (
        match_after.get("evidence") if match_after else None
    )
    facts["knowledge_hit_count_post"] = (
        sum(1 for item in hits_after if item.get("id") == memory_id)
        if memory_id else 0
    )
    legacy_hits, legacy_search_error = _evidence_search(
        session, fixture, VEGA_LEGACY_QUERY,
    )
    facts["legacy_knowledge_search_error"] = legacy_search_error
    facts["legacy_knowledge_search_count"] = len(legacy_hits)
    facts["legacy_knowledge_search_evidence"] = (
        legacy_hits[0].get("evidence") if legacy_hits else None
    )
    _read_legacy_knowledge(session, facts, "post")
    return facts


def _evidence_rebuild_preserved(facts: dict, expected: list[dict]) -> tuple[bool, str]:
    if not facts.get("has_task"):
        return True, "no memory in baseline"
    if not facts.get("rebuild_ok"):
        return False, f"rebuild failed: {facts.get('rebuild_error')!r}"
    task_ok = (
        facts.get("task_evidence_pre") == expected
        and facts.get("task_evidence_post") == expected
    )
    checkpoint_ok = (
        facts.get("checkpoint_evidence_pre") == expected
        and facts.get("checkpoint_evidence_post") == expected
    )
    knowledge_ok = True
    if (
        facts.get("knowledge_evidence_pre") is not None
        or facts.get("knowledge_evidence_post") is not None
    ):
        knowledge_ok = (
            facts.get("knowledge_evidence_pre") == expected
            and facts.get("knowledge_evidence_post") == expected
            and facts.get("knowledge_hit_count_post") == 1
            and facts.get("legacy_knowledge_search_count") == 1
            and facts.get("legacy_knowledge_search_evidence") == []
        )
    evidence = (
        f"task_pre={clip(facts.get('task_evidence_pre'), 120)} "
        f"task_post={clip(facts.get('task_evidence_post'), 120)} "
        f"knowledge_pre={clip(facts.get('knowledge_evidence_pre'), 120)} "
        f"knowledge_post={clip(facts.get('knowledge_evidence_post'), 120)} "
        f"knowledge_hits_post={facts.get('knowledge_hit_count_post')!r} "
        f"legacy_hits={facts.get('legacy_knowledge_search_count')!r}"
    )
    return (task_ok and checkpoint_ok and knowledge_ok), evidence


def checks_evidence_persistence(fixture: Fixture) -> list[Check]:
    def rule_id():
        return fixture.knowledge.get("vega_rule", {}).get("id")

    return [
        Check(
            "checkpoint_evidence_persisted", "recall",
            "task_read returns the structured evidence written with the checkpoint",
            {"A": False, "B": True, "C": True},
            lambda facts: (
                facts.get("task_evidence_pre") == VEGA_EVIDENCE
                and facts.get("checkpoint_evidence_pre") == VEGA_EVIDENCE,
                f"task={clip(facts.get('task_evidence_pre'), 120)} "
                f"checkpoint={clip(facts.get('checkpoint_evidence_pre'), 120)}",
            ),
        ),
        Check(
            "legacy_checkpoint_without_evidence", "recall",
            "a checkpoint written before the evidence field stays readable and reads as []",
            {"A": False, "B": True, "C": True},
            lambda facts: (
                facts.get("legacy_checkpoint_found") is True
                and facts.get("legacy_checkpoint_evidence") == [],
                f"found={facts.get('legacy_checkpoint_found')!r} "
                f"evidence={facts.get('legacy_checkpoint_evidence')!r}",
            ),
        ),
        Check(
            "knowledge_evidence_recalled", "recall",
            "memory_search returns the structured evidence of reviewed knowledge (C only)",
            {"A": False, "B": False, "C": True},
            lambda facts: (
                facts.get("knowledge_hit_id") == rule_id()
                and facts.get("knowledge_evidence_pre") == VEGA_EVIDENCE,
                f"id={facts.get('knowledge_hit_id')!r} "
                f"evidence={clip(facts.get('knowledge_evidence_pre'), 120)}",
            ),
        ),
        Check(
            "legacy_knowledge_without_evidence", "recall",
            "pre-evidence knowledge is readable before/after rebuild and reads as [] (C only)",
            {"A": False, "B": False, "C": True},
            lambda facts: (
                facts.get("legacy_knowledge_read_pre_ok") is True
                and facts.get("legacy_knowledge_read_post_ok") is True
                and facts.get("legacy_knowledge_evidence_pre") == []
                and facts.get("legacy_knowledge_evidence_post") == [],
                f"pre_ok={facts.get('legacy_knowledge_read_pre_ok')!r} "
                f"post_ok={facts.get('legacy_knowledge_read_post_ok')!r} "
                f"pre={facts.get('legacy_knowledge_evidence_pre')!r} "
                f"post={facts.get('legacy_knowledge_evidence_post')!r} "
                f"errors={facts.get('legacy_knowledge_read_pre_error')!r}/"
                f"{facts.get('legacy_knowledge_read_post_error')!r}",
            ),
        ),
        Check(
            "evidence_survives_rebuild", "safety",
            "a Markdown/index rebuild neither loses nor duplicates stored evidence",
            {"A": True, "B": True, "C": True},
            lambda facts: _evidence_rebuild_preserved(facts, VEGA_EVIDENCE),
        ),
        Check(
            "context_within_budget", "safety",
            "injected context respects the character budget",
            {"A": True, "B": True, "C": True},
            _chars_at_most(DEFAULT_BUDGET),
        ),
    ]


# -- scenario 9: consolidation lint and bounded review package --------------


LUMEN_EVIDENCE = [
    {
        "kind": "test",
        "reference": "tests.test_services::test_memory_review_returns_bounded_consolidation_package",
        "hash": "sha256:lumen01",
        "observed_at": "2026-11-08T11:30:00+08:00",
        "summary": "consolidation package integration test passed",
    },
    {"kind": "file", "reference": "src/memweave/services.py", "hash": "sha256:lumen02"},
    {"kind": "user", "reference": "reviewer requested a bounded lint package"},
]
LUMEN_LONG_FINDINGS = (
    "Lumen consolidation findings: "
    + "the review package must stay bounded and deterministic. " * 10
).strip()
LUMEN_CHECKPOINT_COUNT = 11
LUMEN_RECENT_LIMIT = 10
LUMEN_UNSOURCED_COUNT = 21
LUMEN_UNSOURCED_IDS_LIMIT = 20
LUMEN_CANDIDATE_COUNT = 2
LUMEN_ACTIVE_TOTAL = 23  # 21 unsourced notes + 2 duplicate-title records
LUMEN_PHASE_TITLE = "Lumen investigation phase"


def build_consolidation_review(fixture: Fixture) -> None:
    """B world: completed task with a pending phase and review debts."""
    fixture.register("Lumen consolidation review")
    fixture.open_task(
        "main",
        title="Lumen refresh consolidation",
        goal=(
            "Close out the lumen refresh decision and hand the host a bounded consolidation "
            "review package."
        ),
        acceptance=[
            "lint package returned",
            "pending review visible",
        ],
    )
    fixture.claim("main")
    for index in range(1, LUMEN_CHECKPOINT_COUNT):
        fixture.checkpoint(
            "main",
            progress=f"Lumen review checkpoint {index}.",
            findings=f"Lumen review finding {index}.",
            next_step=f"Continue lumen review step {index}.",
        )
    # The newest checkpoint carries long findings and structured evidence.
    fixture.checkpoint(
        "main",
        progress="Final lumen review checkpoint.",
        findings=LUMEN_LONG_FINDINGS,
        next_step="Run the consolidation review.",
        evidence=LUMEN_EVIDENCE,
    )
    fixture.phase_close(
        "main",
        title=LUMEN_PHASE_TITLE,
        summary="The lumen refresh boundary is documented and verified.",
        next_step="Consolidate the phase knowledge.",
        findings="Boundary and verification evidence are ready for review.",
        verification="focused lumen evidence suite passed",
    )
    fixture.close_task(
        "main",
        summary="Lumen refresh work completed; consolidation review is pending.",
    )


def learn_consolidation_review(fixture: Fixture) -> None:
    for index in range(1, LUMEN_UNSOURCED_COUNT + 1):
        fixture.write_knowledge(
            f"bulk{index:02d}",
            title=f"Lumen bulk unsourced note {index:02d}",
            body=(
                "Reviewed lumen bulk note recorded without any structured evidence pointers; "
                "it exercises the unsourced-knowledge lint path."
            ),
            kind="lesson",
        )
    fixture.write_knowledge(
        "lumen_rule",
        title="Lumen refresh evidence rule",
        body=(
            "Reviewed lumen refresh evidence rule: the boundary is verified with structured "
            "evidence pointers. Retrieval cues: lumen refresh evidence rule."
        ),
        kind="decision",
        task_key="main",
        evidence=LUMEN_EVIDENCE,
    )
    fixture.write_knowledge(
        "lumen_duplicate",
        title="lumen   refresh evidence rule",
        body=(
            "Duplicate lumen refresh title with different wording; the host consolidation "
            "pass must see it as a repeated normalized title."
        ),
        kind="decision",
        evidence=[{"kind": "host", "reference": "duplicate title fixture"}],
    )
    fixture.write_knowledge(
        "candidate_a",
        title="Lumen candidate follow-up",
        body="Candidate lumen follow-up idea awaiting explicit review.",
        kind="fact",
        status="candidate",
        evidence=[{"kind": "host", "reference": "lumen review queue"}],
    )
    fixture.write_knowledge(
        "candidate_b",
        title="Lumen candidate rollback plan",
        body="Second candidate lumen rollback plan awaiting explicit review.",
        kind="procedure",
        status="candidate",
        evidence=[{"kind": "host", "reference": "lumen review queue"}],
    )
    fixture.retire_knowledge(
        "retired_rule",
        title="Lumen old boundary rule",
        body="Legacy lumen boundary rule that was superseded.",
        retired_title="Lumen old boundary rule (retired)",
        retired_body="Retired: superseded by the evidence-backed lumen refresh rule.",
    )


def probe_consolidation_review(session: Session, prepared: dict, fixture: Fixture,
                               condition: str) -> dict:
    facts: dict = {"has_project": bool(prepared.get("project"))}
    if not facts["has_project"]:
        return facts
    facts["prepare_state"] = prepared.get("state")
    task_id = fixture.task_id("main")
    try:
        review = session.call("memory_review", task_id=task_id)
    except DiagnosticError as exc:
        facts["review_error"] = exc.code
        return facts
    facts["review_state"] = review.get("state")
    facts["review_instructions_present"] = bool(review.get("instructions"))
    consolidation = review.get("consolidation") or {}
    facts["consolidation_present"] = bool(consolidation)
    if not consolidation:
        return facts

    recent = consolidation.get("recent_checkpoints") or {}
    items = recent.get("items") or []
    facts["recent_total"] = recent.get("total")
    facts["recent_returned"] = len(items)
    facts["recent_truncated"] = recent.get("truncated")
    latest = items[0] if items else {}
    facts["latest_findings_length"] = latest.get("findings_length")
    facts["latest_findings_truncated"] = latest.get("findings_truncated")
    facts["latest_findings_chars"] = len(latest.get("findings") or "")
    facts["latest_evidence"] = latest.get("evidence")
    facts["latest_evidence_count"] = latest.get("evidence_count")

    knowledge = consolidation.get("existing_knowledge") or {}
    facts["active_total"] = knowledge.get("active_total")
    facts["candidate_total"] = knowledge.get("candidate_total")
    active = knowledge.get("active") or []
    rule_id = fixture.knowledge.get("lumen_rule", {}).get("id")
    rule_summary = next(
        (item for item in active if item.get("id") == rule_id), None,
    ) if rule_id else None
    facts["rule_summary_evidence_count"] = (
        rule_summary.get("evidence_count") if rule_summary else None
    )
    facts["rule_summary_source_count"] = (
        rule_summary.get("source_count") if rule_summary else None
    )
    facts["rule_summary_status"] = (
        rule_summary.get("status") if rule_summary else None
    )
    facts["active_ids"] = [item.get("id") for item in active]

    phases = consolidation.get("pending_phases") or {}
    facts["pending_phase_ids"] = [
        item.get("id") for item in phases.get("items") or []
    ]
    pending = consolidation.get("pending_consolidation") or {}
    facts["pending_task_ids"] = [
        item.get("id") for item in pending.get("items") or []
    ]
    facts["pending_task_lifecycles"] = [
        item.get("lifecycle") for item in pending.get("items") or []
    ]

    lint = consolidation.get("lint") or {}
    duplicates = lint.get("duplicate_titles") or {}
    facts["duplicate_group_count"] = duplicates.get("group_count")
    facts["duplicate_group_ids"] = [
        group.get("ids") for group in duplicates.get("groups") or []
    ]
    facts["duplicate_group_titles"] = [
        group.get("normalized_title") for group in duplicates.get("groups") or []
    ]
    without = lint.get("knowledge_without_evidence") or {}
    facts["without_evidence_count"] = without.get("count")
    facts["without_evidence_returned"] = len(without.get("ids") or [])
    facts["without_evidence_truncated"] = without.get("truncated")
    facts["without_evidence_ids"] = without.get("ids") or []
    backlog = lint.get("candidate_backlog") or {}
    facts["candidate_backlog_count"] = backlog.get("count")
    facts["candidate_backlog_oldest"] = backlog.get("oldest_created_at")
    facts["retired_count"] = (lint.get("retired") or {}).get("count")
    facts["consolidation_truncated"] = consolidation.get("truncated")
    facts["truncation_reasons"] = consolidation.get("truncated_reasons") or []
    facts["lint_truncated"] = lint.get("truncated")

    knowledge_index = consolidation.get("knowledge_index")
    if isinstance(knowledge_index, dict):
        index_active = knowledge_index.get("active") or {}
        index_candidate = knowledge_index.get("candidate") or {}
        index_retired = knowledge_index.get("retired") or {}
        index_limits = knowledge_index.get("limits") or {}
        lines = index_active.get("items") or []
        facts["knowledge_index_present"] = True
        facts["knowledge_index_format"] = knowledge_index.get("format")
        facts["knowledge_index_active_total"] = index_active.get("total")
        facts["knowledge_index_active_returned"] = index_active.get("returned")
        facts["knowledge_index_candidate_total"] = index_candidate.get("total")
        facts["knowledge_index_candidate_returned"] = index_candidate.get("returned")
        facts["knowledge_index_retired_total"] = index_retired.get("total")
        facts["knowledge_index_line_max_chars"] = max(
            (len(line) for line in lines if isinstance(line, str)), default=0,
        )
        facts["knowledge_index_active_chars"] = index_active.get("chars")
        facts["knowledge_index_limits"] = {
            "active_rows": index_limits.get("active_rows"),
            "candidate_rows": index_limits.get("candidate_rows"),
            "line_chars": index_limits.get("line_chars"),
            "active_chars": index_limits.get("active_chars"),
            "candidate_chars": index_limits.get("candidate_chars"),
        }
        facts["knowledge_index_rule_fields"] = None
        if rule_id:
            for line in lines:
                if not isinstance(line, str):
                    continue
                fields = line.split(" | ")
                if fields and fields[0] == rule_id:
                    facts["knowledge_index_rule_fields"] = fields
                    break
    else:
        facts["knowledge_index_present"] = False
    return facts


def checks_consolidation_review(fixture: Fixture) -> list[Check]:
    def rule_id():
        return fixture.knowledge.get("lumen_rule", {}).get("id")

    def duplicate_id():
        return fixture.knowledge.get("lumen_duplicate", {}).get("id")

    def task_id():
        return fixture.task_id("main")

    def phase_id():
        return fixture.phase_id("main", 0)

    return [
        Check(
            "review_state_pending", "recall",
            "memory_review reports the completed task's review as pending",
            {"A": False, "B": True, "C": True},
            _equals("review_state", "pending"),
        ),
        Check(
            "review_instructions_present", "recall",
            "the review response carries the host consolidation instructions",
            {"A": False, "B": True, "C": True},
            _truthy("review_instructions_present"),
        ),
        Check(
            "recent_checkpoints_bounded", "recall",
            "the review package returns at most ten recent checkpoints and reports truncation",
            {"A": False, "B": True, "C": True},
            lambda facts: (
                facts.get("recent_total") == LUMEN_CHECKPOINT_COUNT
                and facts.get("recent_returned") == LUMEN_RECENT_LIMIT
                and facts.get("recent_truncated") is True,
                f"total={facts.get('recent_total')!r} "
                f"returned={facts.get('recent_returned')!r} "
                f"truncated={facts.get('recent_truncated')!r}",
            ),
        ),
        Check(
            "long_findings_truncated", "recall",
            "long checkpoint findings are bounded and the truncation is reported",
            {"A": False, "B": True, "C": True},
            lambda facts: (
                facts.get("latest_findings_length") == len(LUMEN_LONG_FINDINGS)
                and facts.get("latest_findings_chars") == 500
                and facts.get("latest_findings_truncated") is True,
                f"length={facts.get('latest_findings_length')!r} "
                f"returned={facts.get('latest_findings_chars')!r} "
                f"truncated={facts.get('latest_findings_truncated')!r}",
            ),
        ),
        Check(
            "checkpoint_evidence_summarized", "recall",
            "the newest checkpoint's structured evidence is summarized in the package",
            {"A": False, "B": True, "C": True},
            lambda facts: (
                facts.get("latest_evidence") == LUMEN_EVIDENCE
                and facts.get("latest_evidence_count") == len(LUMEN_EVIDENCE),
                f"evidence={clip(facts.get('latest_evidence'), 160)} "
                f"count={facts.get('latest_evidence_count')!r}",
            ),
        ),
        Check(
            "pending_review_surfaced", "recall",
            "the pending phase and the completed task awaiting review are both surfaced",
            {"A": False, "B": True, "C": True},
            lambda facts: (
                facts.get("pending_phase_ids") == [phase_id()]
                and facts.get("pending_task_ids") == [task_id()]
                and facts.get("pending_task_lifecycles") == ["pending_review"],
                f"phases={facts.get('pending_phase_ids')!r} "
                f"tasks={facts.get('pending_task_ids')!r} "
                f"lifecycles={facts.get('pending_task_lifecycles')!r}",
            ),
        ),
        Check(
            "existing_knowledge_summarized", "recall",
            "existing active/candidate knowledge is summarized with evidence counts (C only)",
            {"A": False, "B": False, "C": True},
            lambda facts: (
                facts.get("active_total") == LUMEN_ACTIVE_TOTAL
                and facts.get("candidate_total") == LUMEN_CANDIDATE_COUNT
                and facts.get("rule_summary_evidence_count") == len(LUMEN_EVIDENCE)
                and facts.get("rule_summary_source_count") == 1
                and facts.get("rule_summary_status") == "active",
                f"active_total={facts.get('active_total')!r} "
                f"candidate_total={facts.get('candidate_total')!r} "
                f"evidence_count={facts.get('rule_summary_evidence_count')!r} "
                f"source_count={facts.get('rule_summary_source_count')!r} "
                f"status={facts.get('rule_summary_status')!r}",
            ),
        ),
        Check(
            "duplicate_titles_linted", "recall",
            "same-project normalized duplicate titles are reported as one group (C only)",
            {"A": False, "B": False, "C": True},
            lambda facts: (
                facts.get("duplicate_group_count") == 1
                and any(
                    set(group) == {rule_id(), duplicate_id()}
                    for group in facts.get("duplicate_group_ids") or []
                )
                and facts.get("duplicate_group_titles") == ["lumen refresh evidence rule"],
                f"groups={facts.get('duplicate_group_count')!r} "
                f"ids={facts.get('duplicate_group_ids')!r} "
                f"titles={facts.get('duplicate_group_titles')!r}",
            ),
        ),
        Check(
            "unsourced_knowledge_linted", "recall",
            "unsourced knowledge is counted and its id list is truncated (C only)",
            {"A": False, "B": False, "C": True},
            lambda facts: (
                facts.get("without_evidence_count") == LUMEN_UNSOURCED_COUNT
                and facts.get("without_evidence_returned") == LUMEN_UNSOURCED_IDS_LIMIT
                and facts.get("without_evidence_truncated") is True
                and rule_id() not in (facts.get("without_evidence_ids") or []),
                f"count={facts.get('without_evidence_count')!r} "
                f"returned={facts.get('without_evidence_returned')!r} "
                f"truncated={facts.get('without_evidence_truncated')!r} "
                f"rule_listed={rule_id() in (facts.get('without_evidence_ids') or [])!r}",
            ),
        ),
        Check(
            "candidate_backlog_linted", "recall",
            "the candidate backlog is counted with its oldest timestamp (C only)",
            {"A": False, "B": False, "C": True},
            lambda facts: (
                facts.get("candidate_backlog_count") == LUMEN_CANDIDATE_COUNT
                and bool(facts.get("candidate_backlog_oldest")),
                f"count={facts.get('candidate_backlog_count')!r} "
                f"oldest={facts.get('candidate_backlog_oldest')!r}",
            ),
        ),
        Check(
            "retired_linted", "recall",
            "retired records are counted without entering default recall (C only)",
            {"A": False, "B": False, "C": True},
            lambda facts: (
                facts.get("retired_count") == 1,
                f"retired={facts.get('retired_count')!r}",
            ),
        ),
        Check(
            "consolidation_truncation_reported", "recall",
            "the package reports lint truncation in its overall truncation reasons (C only)",
            {"A": False, "B": False, "C": True},
            lambda facts: (
                facts.get("consolidation_truncated") is True
                and "lint" in (facts.get("truncation_reasons") or []),
                f"truncated={facts.get('consolidation_truncated')!r} "
                f"reasons={facts.get('truncation_reasons')!r}",
            ),
        ),
        Check(
            "knowledge_index_bounded", "recall",
            "the consolidation carries a bounded one-line knowledge index (C only)",
            {"A": False, "B": False, "C": True},
            lambda facts: (
                facts.get("knowledge_index_present") is True
                and facts.get("knowledge_index_format") == "one_line_per_record"
                and facts.get("knowledge_index_active_total") == LUMEN_ACTIVE_TOTAL
                and facts.get("knowledge_index_active_returned") == LUMEN_ACTIVE_TOTAL
                and facts.get("knowledge_index_candidate_total") == LUMEN_CANDIDATE_COUNT
                and facts.get("knowledge_index_candidate_returned") == LUMEN_CANDIDATE_COUNT
                and facts.get("knowledge_index_retired_total") == 1
                and facts.get("knowledge_index_line_max_chars") <= 240
                and facts.get("knowledge_index_limits") == {
                    "active_rows": 120,
                    "candidate_rows": 60,
                    "line_chars": 240,
                    "active_chars": 25000,
                    "candidate_chars": 12000,
                }
                and (facts.get("knowledge_index_rule_fields") or [])[:3]
                == [rule_id(), "decision", "active"]
                and (facts.get("knowledge_index_rule_fields") or [None])[6:7]
                == [f"evidence={len(LUMEN_EVIDENCE)}"],
                f"present={facts.get('knowledge_index_present')!r} "
                f"active={facts.get('knowledge_index_active_total')!r}/"
                f"{facts.get('knowledge_index_active_returned')!r} "
                f"candidate={facts.get('knowledge_index_candidate_total')!r} "
                f"retired={facts.get('knowledge_index_retired_total')!r} "
                f"line_max={facts.get('knowledge_index_line_max_chars')!r} "
                f"rule_fields={clip(facts.get('knowledge_index_rule_fields'), 160)} "
                f"limits={facts.get('knowledge_index_limits')!r}",
            ),
        ),
        Check(
            "context_within_budget", "safety",
            "injected context respects the character budget",
            {"A": True, "B": True, "C": True},
            _chars_at_most(DEFAULT_BUDGET),
        ),
    ]


# -- scenario 10: optional knowledge-index injection (off/on gate) ----------


ATLAS_QUERY = "atlas gate continuity"
ATLAS_ACTIVE_TOTAL = 35
ATLAS_CANDIDATE_TOTAL = 2
ATLAS_RETIRED_TOTAL = 1
# P3 contract strings: the injected head title and the truncation note wording.
ATLAS_INDEX_MARKER = "navigation only; not evidence"
ATLAS_TRUNCATION_MARKER = "truncat"
ATLAS_TIGHT_BUDGET = 900
ATLAS_INDEX_CHAR_LIMIT = 4000
# 4000 injected index characters + title/truncation note with some slack.
ATLAS_MAX_INDEX_DELTA = 4300


def build_knowledge_index_injection(fixture: Fixture) -> None:
    """B world: a current task whose brief must survive optional navigation."""
    fixture.register("Atlas knowledge index gate")
    fixture.open_task(
        "main",
        title="Atlas gate continuity",
        goal=(
            "Keep the atlas gate continuity check recoverable while knowledge navigation "
            "stays an optional extra."
        ),
        acceptance=[
            "atlas gate check passes",
            "task brief survives optional navigation",
        ],
    )
    fixture.claim("main")
    fixture.checkpoint(
        "main",
        progress="Atlas gate context prepared for the navigation gate.",
        findings=(
            "The atlas gate check must stay visible whether the optional navigation head "
            "is on or off."
        ),
        next_step="Run the atlas gate continuation check.",
    )


def learn_knowledge_index_injection(fixture: Fixture) -> None:
    # Knowledge is long-term layer, so it only exists in condition C.
    for index in range(1, ATLAS_ACTIVE_TOTAL + 1):
        fixture.write_knowledge(
            f"active{index:02d}",
            title=f"Quasar shell quoting note {index:02d}",
            body=(
                f"Reviewed quasar shell quoting note {index:02d}: keep the quoting rule "
                "explicit and re-check it before batch runs. Retrieval cues: quasar shell "
                "quoting."
            ),
            kind="lesson",
        )
    for index in range(1, ATLAS_CANDIDATE_TOTAL + 1):
        fixture.write_knowledge(
            f"candidate{index:02d}",
            title=f"Quasar candidate rollout note {index:02d}",
            body="Candidate quasar rollout note awaiting explicit review.",
            kind="fact",
            status="candidate",
        )
    fixture.retire_knowledge(
        "retired_rule",
        title="Quasar old quoting rule",
        body="Legacy quasar quoting rule that was superseded.",
        retired_title="Quasar old quoting rule (retired)",
        retired_body="Retired: superseded by the reviewed quasar quoting notes.",
    )


def _index_context_text(prepared: dict) -> str:
    return ((prepared.get("context") or {}).get("text") or "")


def _context_fingerprint(prepared: dict) -> str:
    return json.dumps(prepared.get("context"), sort_keys=True, ensure_ascii=False)


def _prepare_resume(session: Session, scenario: Scenario, budget: int,
                    include: bool | None = None) -> dict:
    parameters = dict(
        root=str(session.root), intent="resume", query=scenario.query, budget=budget,
    )
    if include is not None:
        parameters["include_knowledge_index"] = include
    return session.call("workspace_prepare", **parameters)


def _include_index_supported(session: Session) -> bool:
    try:
        parameters = inspect.signature(session.service.workspace_prepare).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic runtime
        return False
    return "include_knowledge_index" in parameters


def _knowledge_ids(fixture: Fixture, prefix: str) -> list[str]:
    return [
        record["id"] for key, record in fixture.knowledge.items()
        if key.startswith(prefix)
    ]


def _task_brief_presence(text: str, fixture: Fixture) -> dict[str, bool]:
    text = text or ""
    return {
        "task_identity": f"[{fixture.task_id('main')}]" in text,
        "next_step": "Run the atlas gate continuation check" in text,
        "goal": "Keep the atlas gate continuity" in text,
        "acceptance": "atlas gate check passes" in text,
    }


def measure_knowledge_index_injection(scenario: Scenario, fixture: Fixture,
                                      condition: str) -> dict:
    """Measure the off/on knowledge-index injection on one fixture."""
    home = fixture.home_for(condition)
    facts: dict = {}
    error: str | None = None
    session = fixture.session(home)
    try:
        with session:
            off_default = _prepare_resume(session, scenario, scenario.budget)
            facts.update(facts_from_prepared(off_default))
            off_default_text = _index_context_text(off_default)
            off_chars = len(off_default_text)
            facts["off_default_text"] = off_default_text
            facts["off_default_chars"] = off_chars
            facts["off_default_has_index_header"] = (
                ATLAS_INDEX_MARKER in off_default_text.lower()
            )
            if off_default.get("project"):
                if not _include_index_supported(session):
                    raise RuntimeError(
                        "include_knowledge_index is not implemented in this checkout yet; "
                        "rerun the evaluation after the P3 injection change lands "
                        "(off/on comparison cannot be measured)."
                    )
                off_explicit = _prepare_resume(
                    session, scenario, scenario.budget, include=False,
                )
                on_prepared = _prepare_resume(
                    session, scenario, scenario.budget, include=True,
                )
                tight_prepared = _prepare_resume(
                    session, scenario, ATLAS_TIGHT_BUDGET, include=True,
                )
                off_explicit_text = _index_context_text(off_explicit)
                on_text = _index_context_text(on_prepared)
                on_context = on_prepared.get("context") or {}
                tight_text = _index_context_text(tight_prepared)
                tight_context = tight_prepared.get("context") or {}
                active_ids = _knowledge_ids(fixture, "active")
                candidate_ids = _knowledge_ids(fixture, "candidate")
                retired_ids = _knowledge_ids(fixture, "retired")
                facts["off_explicit_text"] = off_explicit_text
                facts["off_explicit_chars"] = len(off_explicit_text)
                facts["off_explicit_has_index_header"] = (
                    ATLAS_INDEX_MARKER in off_explicit_text.lower()
                )
                off_context = off_default.get("context") or {}
                off_explicit_context = off_explicit.get("context") or {}
                facts["off_context_has_index_metadata"] = (
                    "knowledge_index" in off_context
                )
                facts["off_explicit_context_has_index_metadata"] = (
                    "knowledge_index" in off_explicit_context
                )
                facts["off_byte_identical"] = (
                    _context_fingerprint(off_default) == _context_fingerprint(off_explicit)
                )
                facts["on_text"] = on_text
                facts["on_chars"] = len(on_text)
                facts["on_context_present"] = bool(on_context)
                facts["on_budget"] = on_context.get("budget_characters")
                facts["on_truncated"] = bool(on_context.get("truncated"))
                facts["on_task_id"] = on_prepared.get("task_id")
                facts["on_has_index_header"] = ATLAS_INDEX_MARKER in on_text.lower()
                facts["active_total"] = len(active_ids)
                facts["candidate_total"] = len(candidate_ids)
                facts["retired_total"] = len(retired_ids)
                # Match rendered index rows ("- memory_... | ...") rather than a bare
                # id: a query hit would otherwise put active ids in the off context too.
                facts["active_ids_injected"] = sum(
                    1 for memory_id in active_ids if f"- {memory_id} | " in on_text
                )
                facts["candidate_ids_injected"] = sum(
                    1 for memory_id in candidate_ids if f"- {memory_id} | " in on_text
                )
                facts["retired_ids_injected"] = sum(
                    1 for memory_id in retired_ids if f"- {memory_id} | " in on_text
                )
                on_metadata = on_context.get("knowledge_index")
                facts["on_context_metadata"] = on_metadata
                facts["on_metadata_within_limits"] = bool(
                    isinstance(on_metadata, dict)
                    and on_metadata.get("requested") is True
                    and on_metadata.get("injected") is True
                    and 3 <= int(on_metadata.get("lines") or 0) <= 30
                    and int(on_metadata.get("active_total") or 0) == len(active_ids)
                    and int(on_metadata.get("line_limit") or 0) == 30
                    and int(on_metadata.get("char_limit") or 0)
                    == ATLAS_INDEX_CHAR_LIMIT
                    and int(on_metadata.get("characters") or 0)
                    <= ATLAS_INDEX_CHAR_LIMIT
                    and on_metadata.get("truncated") is True
                )
                facts["on_truncation_noted"] = (
                    ATLAS_TRUNCATION_MARKER in on_text.lower()
                )
                facts["on_task_brief"] = _task_brief_presence(on_text, fixture)
                facts["cost_delta_chars"] = len(on_text) - off_chars
                facts["cost_delta_tokens_estimate"] = (
                    estimate_tokens(len(on_text)) - estimate_tokens(off_chars)
                )
                brief_preserved = bool(facts["on_task_brief"]) and all(
                    facts["on_task_brief"].values()
                )
                active_only = (
                    facts["candidate_ids_injected"] == 0
                    and facts["retired_ids_injected"] == 0
                )
                within_budget = len(on_text) <= int(facts["on_budget"] or 0)
                truncation_reported = bool(
                    facts["on_truncation_noted"]
                    and len(active_ids) > facts["active_ids_injected"]
                )
                facts["enable_gate"] = {
                    "task_brief_preserved": brief_preserved,
                    "active_only": active_only,
                    "candidate_retired_absent": active_only,
                    "within_budget": within_budget,
                    "truncation_reported": truncation_reported,
                    "metadata_within_limits": facts["on_metadata_within_limits"],
                    "cost_delta_chars": facts["cost_delta_chars"],
                    "cost_delta_tokens_estimate": facts["cost_delta_tokens_estimate"],
                }
                facts["tight_text"] = tight_text
                facts["tight_chars"] = len(tight_text)
                facts["tight_budget"] = tight_context.get("budget_characters")
                facts["tight_context_metadata"] = tight_context.get("knowledge_index")
                facts["tight_within_budget"] = (
                    len(tight_text) <= ATLAS_TIGHT_BUDGET
                )
                facts["tight_task_brief"] = _task_brief_presence(tight_text, fixture)
                facts["knowledge_index_report"] = {
                    "off_chars": off_chars,
                    "on_chars": len(on_text),
                    "cost_delta_chars": facts["cost_delta_chars"],
                    "cost_delta_tokens_estimate": facts["cost_delta_tokens_estimate"],
                    "active_total": len(active_ids),
                    "active_lines_injected": facts["active_ids_injected"],
                    "candidate_lines_injected": facts["candidate_ids_injected"],
                    "retired_lines_injected": facts["retired_ids_injected"],
                    "index_header_present": facts["on_has_index_header"],
                    "truncation_noted": facts["on_truncation_noted"],
                    "context_metadata": on_metadata,
                    "tight_budget": ATLAS_TIGHT_BUDGET,
                    "tight_chars": len(tight_text),
                    "tight_metadata": facts["tight_context_metadata"],
                    "enable_gate": facts["enable_gate"],
                }
    except Exception as exc:  # noqa: BLE001 - surfaced as a scenario error
        error = f"{type(exc).__name__}: {exc}"
        facts.setdefault("state", "error")
    finally:
        session.close()
    return make_observation(facts, session.surface_calls, session.wall_ms, error)


def _no_project(facts: dict) -> bool:
    return not facts.get("project_id")


def _off_has_no_index(facts: dict) -> tuple[bool, str]:
    if _no_project(facts):
        return True, "no project in baseline"
    ok = (
        facts.get("off_default_has_index_header") is False
        and facts.get("off_explicit_has_index_header") is False
        and facts.get("off_context_has_index_metadata") is False
        and facts.get("off_explicit_context_has_index_metadata") is False
    )
    return ok, (
        f"default_header={facts.get('off_default_has_index_header')!r} "
        f"explicit_header={facts.get('off_explicit_has_index_header')!r} "
        f"default_metadata={facts.get('off_context_has_index_metadata')!r} "
        f"explicit_metadata={facts.get('off_explicit_context_has_index_metadata')!r}"
    )


def _off_byte_identical(facts: dict) -> tuple[bool, str]:
    if _no_project(facts):
        return True, "no project in baseline"
    ok = facts.get("off_byte_identical") is True
    return ok, (
        f"identical={facts.get('off_byte_identical')!r} "
        f"off_chars={facts.get('off_default_chars')!r} "
        f"explicit_chars={facts.get('off_explicit_chars')!r}"
    )


def _enable_gate_ready(facts: dict) -> tuple[bool, str]:
    gate = facts.get("enable_gate")
    if not isinstance(gate, dict):
        return False, "no enable gate (condition has no knowledge layer)"
    required = (
        "task_brief_preserved", "active_only", "candidate_retired_absent",
        "within_budget", "truncation_reported", "metadata_within_limits",
    )
    missing = [key for key in required if gate.get(key) is not True]
    delta = gate.get("cost_delta_chars")
    delta_ok = isinstance(delta, int) and 0 < delta <= ATLAS_MAX_INDEX_DELTA
    return (not missing and delta_ok), (
        f"gate={gate!r} missing={missing!r} delta_ok={delta_ok!r}"
    )


def checks_knowledge_index_injection(fixture: Fixture) -> list[Check]:
    return [
        Check(
            "default_off_byte_identical", "safety",
            "explicit include_knowledge_index=false keeps the context byte-identical to the default",
            {"A": True, "B": True, "C": True},
            _off_byte_identical,
        ),
        Check(
            "off_has_no_index_header", "safety",
            "the default/off context contains no knowledge-index header",
            {"A": True, "B": True, "C": True},
            _off_has_no_index,
        ),
        Check(
            "on_injects_active_index_lines", "recall",
            "include_knowledge_index=true injects bounded active index lines (C only)",
            {"A": False, "B": False, "C": True},
            lambda facts: (
                facts.get("on_has_index_header") is True
                and facts.get("on_metadata_within_limits") is True
                and 3 <= (facts.get("active_ids_injected") or 0) <= 30
                and (facts.get("active_ids_injected") or 0)
                < (facts.get("active_total") or 0),
                f"header={facts.get('on_has_index_header')!r} "
                f"metadata_ok={facts.get('on_metadata_within_limits')!r} "
                f"injected={facts.get('active_ids_injected')!r}/"
                f"{facts.get('active_total')!r} "
                f"metadata={facts.get('on_context_metadata')!r}",
            ),
        ),
        Check(
            "on_active_only", "safety",
            "candidate and retired knowledge never appear in the injected index",
            {"A": True, "B": True, "C": True},
            lambda facts: (
                True if _no_project(facts) else (
                    (facts.get("candidate_ids_injected") or 0) == 0
                    and (facts.get("retired_ids_injected") or 0) == 0
                ),
                f"candidate_injected={facts.get('candidate_ids_injected')!r} "
                f"retired_injected={facts.get('retired_ids_injected')!r}",
            ),
        ),
        Check(
            "on_task_brief_preserved", "recall",
            "identity, next step, goal and acceptance stay in the on context",
            {"A": False, "B": True, "C": True},
            lambda facts: (
                bool(facts.get("on_task_brief"))
                and all((facts.get("on_task_brief") or {}).values()),
                f"brief={facts.get('on_task_brief')!r} on_chars={facts.get('on_chars')!r}",
            ),
        ),
        Check(
            "on_within_budget", "safety",
            "the on context never exceeds the requested character budget",
            {"A": True, "B": True, "C": True},
            lambda facts: (
                True if _no_project(facts) else (
                    (facts.get("on_chars") or 0) <= (facts.get("on_budget") or 0)
                ),
                f"on_chars={facts.get('on_chars')!r} budget={facts.get('on_budget')!r}",
            ),
        ),
        Check(
            "on_truncation_noted", "recall",
            "an over-limit index is bounded and the truncation is reported (C only)",
            {"A": False, "B": False, "C": True},
            lambda facts: (
                facts.get("on_truncation_noted") is True
                and (facts.get("active_ids_injected") or 0)
                < (facts.get("active_total") or 0),
                f"noted={facts.get('on_truncation_noted')!r} "
                f"injected={facts.get('active_ids_injected')!r}/"
                f"{facts.get('active_total')!r}",
            ),
        ),
        Check(
            "tight_budget_preserves_task_brief", "recall",
            "at a 900 character budget the on context keeps the task brief and stays in budget",
            {"A": False, "B": True, "C": True},
            lambda facts: (
                facts.get("tight_within_budget") is True
                and bool(facts.get("tight_task_brief"))
                and all((facts.get("tight_task_brief") or {}).values()),
                f"tight_chars={facts.get('tight_chars')!r} "
                f"brief={facts.get('tight_task_brief')!r}",
            ),
        ),
        Check(
            "enable_gate_ready", "recall",
            "off/on comparison yields a passing enable gate with a bounded cost delta (C only)",
            {"A": False, "B": False, "C": True},
            _enable_gate_ready,
        ),
        Check(
            "context_within_budget", "safety",
            "injected context respects the character budget",
            {"A": True, "B": True, "C": True},
            _chars_at_most(DEFAULT_BUDGET),
        ),
    ]


# ---------------------------------------------------------------------------
# Scenario registry
# ---------------------------------------------------------------------------

SCENARIOS: list[Scenario] = [
    Scenario(
        id="long_task_recovery",
        title="Long task recovery after compactions",
        description=(
            "A task in flight with two checkpoints, a failed route and an open WorkUnit. "
            "A fresh resume must recover goal, failure history, next step and contract state."
        ),
        prompt="Continue the quasar refresh rotation work where we left off.",
        tags=("smoke", "recovery"),
        query="quasar refresh rotation token boundary",
        build=build_long_task_recovery,
        learn=learn_long_task_recovery,
        make_checks=checks_long_task_recovery,
    ),
    Scenario(
        id="new_session_resume",
        title="New session in the same project",
        description=(
            "One current task with a checkpoint and one queued task. A new session must "
            "resume the current task, not the queue, and intent=new must not inherit it."
        ),
        prompt="Pick the release checklist back up.",
        tags=("recovery",),
        query="zephyr release packaging smoke",
        build=build_new_session_resume,
        learn=learn_new_session_resume,
        make_checks=checks_new_session_resume,
        probe=probe_new_intent,
    ),
    Scenario(
        id="cross_client_handoff",
        title="Cross-client handoff on one home",
        description=(
            "Simulates two service processes on the same home: the second client must see "
            "task_claimed while the first is live, then take over after release and recover "
            "the same project, task, next step and knowledge."
        ),
        prompt="Continue from the other client.",
        tags=("recovery",),
        query="nebula cross-client handoff claim",
        build=build_cross_client,
        learn=learn_cross_client,
        make_checks=checks_cross_client,
        measure=measure_cross_client,
    ),
    Scenario(
        id="phase_knowledge_reuse",
        title="Phase knowledge reuse",
        description=(
            "A completed phase leaves reviewed knowledge; a related follow-up task must "
            "recall it while unrelated project knowledge stays out of the injected context."
        ),
        prompt="Implement the boundary we documented in the last phase.",
        tags=("knowledge",),
        query="krypton boundary renewal handler",
        build=build_phase_reuse,
        learn=learn_phase_reuse,
        make_checks=checks_phase_reuse,
    ),
    Scenario(
        id="stale_knowledge_trap",
        title="Stale / wrong knowledge trap",
        description=(
            "One active corrected decision, one retired wrong branch rule and one unreviewed "
            "candidate: only the active correction may reach the context; the retired record "
            "must stay explicitly searchable."
        ),
        prompt="Deploy the payment worker and check the branch decision.",
        tags=("smoke", "knowledge"),
        query="monet deploy branch release main",
        build=build_stale_trap,
        learn=learn_stale_trap,
        make_checks=checks_stale_trap,
        probe=probe_retired_search,
    ),
    Scenario(
        id="budget_500_injection",
        title="Budget=500 injection floor",
        description=(
            "A current task with checkpoint, three WorkUnits and six long knowledge records "
            "compete for a 500 character budget: the context must respect the budget and "
            "still keep the task brief (identity, next step, acceptance, goal) visible."
        ),
        prompt="Continue the orion budget floor work.",
        tags=("budget",),
        query="orion budget floor",
        build=build_budget_floor,
        learn=learn_budget_floor,
        make_checks=checks_budget_floor,
        budget=BUDGET_FLOOR,
    ),
    Scenario(
        id="concurrent_write_conflict",
        title="Concurrent write conflicts",
        description=(
            "Two writers race on the same task checkpoint, the same WorkUnit and (in C) the "
            "same knowledge record: the stale writer must receive VERSION_CONFLICT and the "
            "first writer must be preserved."
        ),
        prompt="Continue the pyxis writer guard work.",
        tags=("conflict",),
        query="pyxis concurrent writer conflict",
        build=build_write_conflict,
        learn=learn_write_conflict,
        make_checks=checks_write_conflict,
        measure=measure_write_conflict,
    ),
    Scenario(
        id="evidence_pointer_persistence",
        title="Structured evidence pointer persistence",
        description=(
            "A checkpoint and reviewed knowledge carry structured evidence pointers; a "
            "pre-evidence (legacy) checkpoint and knowledge record carry none. task_read and "
            "memory_search must return the pointers, legacy records must read as [], and a "
            "Markdown/index rebuild must neither lose nor duplicate stored evidence."
        ),
        prompt="Continue the vega evidence work with the recorded evidence pointers.",
        tags=("evidence",),
        query=VEGA_QUERY,
        build=build_evidence_persistence,
        learn=learn_evidence_persistence,
        make_checks=checks_evidence_persistence,
        probe=probe_evidence_persistence,
    ),
    Scenario(
        id="consolidation_lint_and_truncation",
        title="Consolidation lint and bounded review package",
        description=(
            "A completed task with a pending phase, eleven checkpoints (the newest with long "
            "findings and evidence), duplicate-title knowledge, unsourced knowledge, a "
            "candidate backlog and a retired record. memory_review must return a bounded "
            "consolidation package with real lint counts, truncation notes and the one-line "
            "knowledge index, without calling a model."
        ),
        prompt="Run the consolidation review for the completed lumen task.",
        tags=("consolidation",),
        query="lumen refresh consolidation",
        build=build_consolidation_review,
        learn=learn_consolidation_review,
        make_checks=checks_consolidation_review,
        probe=probe_consolidation_review,
    ),
    Scenario(
        id="knowledge_index_injection",
        title="Optional knowledge-index injection gate",
        description=(
            "A current task with 35 active, two candidate and one retired knowledge record. "
            "The same fixture is measured off (default and explicit include_knowledge_index="
            "false), on (include_knowledge_index=true at the default budget) and on at a tight "
            "900 character budget: off must stay byte-identical and index-free; on must add a "
            "bounded active-only knowledge index header without candidate/retired rows, keep "
            "the task brief and stay within budget; the cost delta feeds an enable gate."
        ),
        prompt="Continue the atlas gate work with optional knowledge navigation.",
        tags=("injection",),
        query=ATLAS_QUERY,
        build=build_knowledge_index_injection,
        learn=learn_knowledge_index_injection,
        make_checks=checks_knowledge_index_injection,
        measure=measure_knowledge_index_injection,
    ),
]


def select_scenarios(scenarios: list[Scenario], ids: list[str] | None,
                     tags: list[str] | None) -> list[Scenario]:
    selected = list(scenarios)
    if ids:
        wanted = {item.strip() for item in ids if item.strip()}
        unknown = wanted - {scenario.id for scenario in scenarios}
        if unknown:
            raise ValueError(f"Unknown scenario id(s): {', '.join(sorted(unknown))}")
        selected = [scenario for scenario in selected if scenario.id in wanted]
    if tags:
        wanted_tags = {item.strip() for item in tags if item.strip()}
        selected = [
            scenario for scenario in selected
            if wanted_tags.intersection(scenario.tags)
        ]
    return selected


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _prepare_fixture(scenario: Scenario, base_dir: Path) -> tuple[Fixture, list[Check]]:
    fixture = Fixture(base_dir, scenario.id)
    fixture.begin()
    try:
        scenario.build(fixture)
    finally:
        fixture.end_setup()
    return fixture, scenario.make_checks(fixture)


def _apply_knowledge(scenario: Scenario, fixture: Fixture) -> None:
    if scenario.learn is None:
        return
    fixture.begin()
    try:
        scenario.learn(fixture)
    finally:
        fixture.end_setup()


def _measure(scenario: Scenario, fixture: Fixture, condition: str) -> dict:
    measure = scenario.measure or default_measure
    return measure(scenario, fixture, condition)


def _evaluate_checks(checks: list[Check], observations: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    for check in checks:
        row = {
            "name": check.name,
            "kind": check.kind,
            "detail": check.detail,
            "expected": dict(check.expected),
            "observed": {},
            "evidence": {},
            "pass": {},
        }
        for condition in CONDITIONS:
            observation = observations.get(condition) or {}
            facts = observation.get("facts") or {}
            condition_error = observation.get("error") or facts.get("probe_error")
            try:
                observed, evidence = check.observe(facts)
            except Exception as exc:  # noqa: BLE001 - report, don't hide
                observed, evidence = False, f"observer error: {type(exc).__name__}: {exc}"
                condition_error = condition_error or "observer error"
            observed = bool(observed)
            expected = bool(check.expected[condition])
            row["observed"][condition] = observed
            row["evidence"][condition] = clip(evidence, 240)
            row["pass"][condition] = bool(observed == expected) and not condition_error
            if condition_error:
                row["evidence"][condition] = clip(
                    f"{row['evidence'][condition]} | condition error: {condition_error}", 240,
                )
        rows.append(row)
    return rows


def _scenario_signature(rows: list[dict], observations: dict[str, dict]) -> str:
    payload = {
        "checks": {
            row["name"]: {condition: row["observed"][condition] for condition in CONDITIONS}
            for row in rows
        },
        "conditions": {
            condition: {
                key: value for key, value in observation_summary(
                    observations.get(condition) or {}
                ).items()
                if key in (
                    "state", "context_present", "context_chars", "context_truncated",
                    "included_types", "included_count", "knowledge_injected",
                    "attention_codes", "surface_calls",
                )
            }
            for condition in CONDITIONS
        },
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def run_scenario_once(scenario: Scenario, base_dir: Path) -> dict:
    started = time.perf_counter()
    result = {
        "id": scenario.id,
        "title": scenario.title,
        "description": scenario.description,
        "prompt": scenario.prompt,
        "tags": list(scenario.tags),
        "query": scenario.query,
        "budget": scenario.budget,
        "status": "error",
        "error": None,
        "checks": [],
        "conditions": {},
        "reproducibility": None,
    }
    observations: dict[str, dict] = {}
    try:
        fixture, checks = _prepare_fixture(scenario, base_dir)
        observations["A"] = _measure(scenario, fixture, "A")
        observations["B"] = _measure(scenario, fixture, "B")
        _apply_knowledge(scenario, fixture)
        observations["C"] = _measure(scenario, fixture, "C")
        rows = _evaluate_checks(checks, observations)
        result["checks"] = rows
        result["conditions"] = {
            condition: observation_summary(observations[condition])
            for condition in CONDITIONS
        }
        result["signature"] = _scenario_signature(rows, observations)
        errors = []
        for condition in CONDITIONS:
            observation = observations[condition]
            if observation.get("error"):
                errors.append(f"{condition}: {observation['error']}")
            probe_error = (observation.get("facts") or {}).get("probe_error")
            if probe_error:
                errors.append(f"{condition}: probe error: {probe_error}")
        if errors:
            result["status"] = "error"
            result["error"] = "; ".join(errors)
        elif all(
            row["pass"][condition]
            for row in rows for condition in CONDITIONS
        ):
            result["status"] = "pass"
        else:
            result["status"] = "fail"
    except Exception:
        result["status"] = "error"
        result["error"] = traceback.format_exc(limit=4)
        result["conditions"] = {
            condition: observation_summary(observations.get(condition) or {})
            for condition in CONDITIONS
        }
    result["scenario_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    return result


def run_scenario(scenario: Scenario, run_dir: Path, repeat: int = 1) -> dict:
    first = run_scenario_once(scenario, run_dir / f"repeat-1")
    if repeat <= 1:
        return first
    matches = True
    for index in range(2, repeat + 1):
        again = run_scenario_once(scenario, run_dir / f"repeat-{index}")
        if again.get("signature") != first.get("signature"):
            matches = False
            first["status"] = "error"
            first["error"] = (
                f"REPRODUCIBILITY_MISMATCH: repeat {index} produced different deterministic "
                "facts"
            )
            break
    first["reproducibility"] = {"repeat": repeat, "identical": matches}
    return first


def _scenario_checks_passed(result: dict) -> tuple[int, int]:
    rows = result.get("checks") or []
    passed = sum(
        1 for row in rows for condition in CONDITIONS if row["pass"][condition]
    )
    return passed, len(rows) * len(CONDITIONS)


def summarize(results: list[dict], duration_ms: float, kept_dir: str | None) -> dict:
    scenarios_total = len(results)
    passed = sum(1 for item in results if item["status"] == "pass")
    failed = sum(1 for item in results if item["status"] == "fail")
    errored = sum(1 for item in results if item["status"] == "error")

    check_rows = [
        (item["id"], row) for item in results for row in (item.get("checks") or [])
    ]
    by_condition = {}
    for condition in CONDITIONS:
        total = len(check_rows)
        passed_cells = sum(
            1 for _, row in check_rows if row["pass"][condition]
        )
        by_condition[condition] = {
            "checks_passed": passed_cells,
            "checks_total": total,
            "rate": (passed_cells / total) if total else None,
        }

    def rate_cells(cells: list[tuple[str, dict]]) -> dict:
        counts = {}
        for condition in CONDITIONS:
            hits = sum(1 for _, row in cells if row["observed"][condition])
            counts[condition] = {
                "hits": hits,
                "total": len(cells),
                "rate": (hits / len(cells)) if cells else None,
            }
        return counts

    recall_rows = [(sid, row) for sid, row in check_rows if row["kind"] == "recall"]
    task_memory_set = [
        (sid, row) for sid, row in recall_rows
        if row["expected"]["B"] and not row["expected"]["A"]
    ]
    knowledge_set = [
        (sid, row) for sid, row in recall_rows
        if row["expected"]["C"] and not row["expected"]["B"]
    ]
    safety_rows = [(sid, row) for sid, row in check_rows if row["kind"] == "safety"]
    safety_by_condition = {}
    for condition in CONDITIONS:
        total = len(safety_rows)
        passed_safety = sum(1 for _, row in safety_rows if row["pass"][condition])
        safety_by_condition[condition] = {
            "checks_passed": passed_safety,
            "checks_total": total,
            "rate": (passed_safety / total) if total else None,
        }

    cost_by_condition = {}
    for condition in CONDITIONS:
        summaries = [
            item["conditions"][condition]
            for item in results
            if item.get("conditions", {}).get(condition)
            and not item["conditions"][condition].get("error")
        ]
        chars = [item.get("context_chars") or 0 for item in summaries]
        tokens = [item.get("context_tokens_estimate") or 0 for item in summaries]
        calls = [item.get("surface_calls") or 0 for item in summaries]
        walls = [item.get("wall_ms") or 0.0 for item in summaries]
        cost_by_condition[condition] = {
            "contexts_measured": len(summaries),
            "injection_chars_total": sum(chars),
            "injection_chars_mean": (sum(chars) / len(chars)) if chars else 0,
            "injection_tokens_estimate_total": sum(tokens),
            "injection_tokens_estimate_mean": (sum(tokens) / len(tokens)) if tokens else 0,
            "surface_calls_mean": (sum(calls) / len(calls)) if calls else 0,
            "wall_ms_mean": (sum(walls) / len(walls)) if walls else 0,
        }

    failing_cells = []
    for item in results:
        for row in item.get("checks") or []:
            for condition in CONDITIONS:
                if not row["pass"][condition]:
                    failing_cells.append({
                        "scenario": item["id"],
                        "check": row["name"],
                        "condition": condition,
                        "expected": row["expected"][condition],
                        "observed": row["observed"][condition],
                        "evidence": row["evidence"][condition],
                    })

    return {
        "scenarios_total": scenarios_total,
        "scenarios_passed": passed,
        "scenarios_failed": failed,
        "scenarios_errored": errored,
        "checks_total": len(check_rows),
        "checks_passed_cells": sum(
            1 for _, row in check_rows for condition in CONDITIONS
            if row["pass"][condition]
        ),
        "checks_total_cells": len(check_rows) * len(CONDITIONS),
        "by_condition": by_condition,
        "recall_sets": {
            "task_memory_gain": {
                "description": (
                    "Recall checks expected in B but not A: what task working memory adds "
                    "over the prompt-only baseline."
                ),
                "checks": [f"{sid}::{row['name']}" for sid, row in task_memory_set],
                "by_condition": rate_cells(task_memory_set),
                "gain_B_over_A": (
                    rate_cells(task_memory_set)["B"]["rate"]
                    - rate_cells(task_memory_set)["A"]["rate"]
                ),
                "gain_C_over_B": (
                    rate_cells(task_memory_set)["C"]["rate"]
                    - rate_cells(task_memory_set)["B"]["rate"]
                ),
            },
            "long_term_knowledge_gain": {
                "description": (
                    "Recall checks expected in C but not B: what reviewed long-term knowledge "
                    "adds on top of task working memory."
                ),
                "checks": [f"{sid}::{row['name']}" for sid, row in knowledge_set],
                "by_condition": rate_cells(knowledge_set),
                "gain_C_over_B": (
                    rate_cells(knowledge_set)["C"]["rate"]
                    - rate_cells(knowledge_set)["B"]["rate"]
                ),
            },
        },
        "safety": safety_by_condition,
        "cost": cost_by_condition,
        "failing_cells": failing_cells[:60],
    }


def build_report(results: list[dict], duration_ms: float,
                 kept_dir: str | None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "harness": {
            "name": HARNESS_NAME,
            "version": HARNESS_VERSION,
            "generated_at": now_iso(),
            "python": sys.version.split()[0],
            "repo_root": str(REPO_ROOT),
            "duration_ms": round(duration_ms, 1),
            "kept_work_dir": kept_dir,
            "evaluation_mode": (
                "deterministic injection/recall/conflict checks against the local service "
                "surface; no model, network or LLM judge is invoked"
            ),
        },
        "conditions": {
            condition: {
                "label": CONDITION_INFO[condition]["label"],
                "description": CONDITION_INFO[condition]["description"],
            }
            for condition in CONDITIONS
        },
        "scenarios": results,
        "summary": summarize(results, duration_ms, kept_dir),
    }


def run_scenarios(ids: list[str] | None = None, tags: list[str] | None = None,
                  work_root: str | Path | None = None, repeat: int = 1,
                  keep: bool = False,
                  scenarios: list[Scenario] | None = None) -> dict:
    selected = select_scenarios(scenarios or SCENARIOS, ids, tags)
    if not selected:
        raise ValueError("No scenarios selected.")
    root = resolve_work_root(work_root)
    # Fixed-length run id keeps the injected context size reproducible across runs
    # (the absolute fixture root is part of the injected header).
    run_dir = root / (
        f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    results: list[dict] = []
    previous_tempdir = tempfile.tempdir
    try:
        # Keep any temporary directory opened transitively by the runtime inside the
        # managed run dir.  Some sandboxes deny the OS temp dir and Python then
        # silently falls back to the current working directory, which would leave
        # stray `tmp*` directories in the repository root.
        tempfile.tempdir = str(run_dir)
        for scenario in selected:
            results.append(run_scenario(scenario, run_dir, repeat=repeat))
        duration_ms = (time.perf_counter() - started) * 1000.0
        report = build_report(results, duration_ms, str(run_dir) if keep else None)
    finally:
        tempfile.tempdir = previous_tempdir
        if not keep:
            shutil.rmtree(run_dir, ignore_errors=True)
    return report


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------


def _condition_status(result: dict, condition: str) -> str:
    info = result.get("conditions", {}).get(condition) or {}
    if result.get("status") == "error" or info.get("error"):
        return "ERR"
    rows = result.get("checks") or []
    if not rows:
        return "ERR"
    return "ok" if all(row["pass"][condition] for row in rows) else "FAIL"


def format_human_report(report: dict) -> str:
    lines: list[str] = []
    summary = report["summary"]
    lines.append(
        f"{HARNESS_NAME} v{HARNESS_VERSION} - {summary['scenarios_total']} scenarios, "
        f"conditions A/B/C (A=no memory, B=task memory, C=+long-term knowledge)"
    )
    lines.append("")
    header = (
        f"{'scenario':34} {'A':>5} {'B':>5} {'C':>5}  {'checks':>9}  "
        f"{'chars A/B/C':>17}  result"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for result in report["scenarios"]:
        passed, total = _scenario_checks_passed(result)
        chars = []
        for condition in CONDITIONS:
            info = result.get("conditions", {}).get(condition) or {}
            chars.append(str(info.get("context_chars") or 0))
        lines.append(
            f"{result['id']:34} {_condition_status(result, 'A'):>5} "
            f"{_condition_status(result, 'B'):>5} "
            f"{_condition_status(result, 'C'):>5}  {passed:>4}/{total:<4}  "
            f"{'/'.join(chars):>17}  {result['status']}"
        )
    lines.append("")
    lines.append(
        f"Totals: {summary['scenarios_passed']} passed, "
        f"{summary['scenarios_failed']} failed, "
        f"{summary['scenarios_errored']} errored; "
        f"{summary['checks_passed_cells']}/{summary['checks_total_cells']} check cells matched."
    )

    recall = summary["recall_sets"]
    lines.append("")
    lines.append("Memory gain (deterministic recall checks, hit rate by condition):")
    for key in ("task_memory_gain", "long_term_knowledge_gain"):
        entry = recall[key]
        cells = entry["by_condition"]
        parts = []
        for condition in CONDITIONS:
            cell = cells[condition]
            rate = cell["rate"]
            rendered = "n/a" if rate is None else f"{rate * 100:.0f}%"
            parts.append(f"{condition} {cell['hits']}/{cell['total']} ({rendered})")
        lines.append(f"  {key:26} " + "  ".join(parts))

    lines.append("")
    lines.append("Safety invariants (must hold in every condition):")
    parts = []
    for condition in CONDITIONS:
        cell = summary["safety"][condition]
        rate = cell["rate"]
        rendered = "n/a" if rate is None else f"{rate * 100:.0f}%"
        parts.append(
            f"{condition} {cell['checks_passed']}/{cell['checks_total']} ({rendered})"
        )
    lines.append("  " + "  ".join(parts))

    lines.append("")
    lines.append("Cost (per measured first turn):")
    for condition in CONDITIONS:
        cost = summary["cost"][condition]
        lines.append(
            f"  {condition}: chars mean {cost['injection_chars_mean']:.0f}, "
            f"est. tokens mean {cost['injection_tokens_estimate_mean']:.0f}, "
            f"surface calls mean {cost['surface_calls_mean']:.1f}, "
            f"wall ms mean {cost['wall_ms_mean']:.0f}"
        )

    failing = summary.get("failing_cells") or []
    if failing:
        lines.append("")
        lines.append("Failing checks:")
        for cell in failing:
            lines.append(
                f"  [{cell['condition']}] {cell['scenario']} :: {cell['check']}: "
                f"expected {cell['expected']}, observed {cell['observed']} "
                f"({cell['evidence']})"
            )
    errors = [
        result for result in report["scenarios"] if result["status"] == "error"
    ]
    if errors:
        lines.append("")
        lines.append("Errored scenarios:")
        for result in errors:
            lines.append(f"  {result['id']}: {clip(result.get('error'), 400)}")
    lines.append("")
    lines.append(
        "Deterministic evaluation only: it measures what MemWeave injects and rejects, "
        "not how well a model would answer. See docs/eval.md."
    )
    return "\n".join(lines)


def format_scenario_list(scenarios: list[Scenario]) -> str:
    lines = [f"{'id':28} {'tags':24} title"]
    lines.append("-" * 100)
    for scenario in scenarios:
        lines.append(
            f"{scenario.id:28} {','.join(scenario.tags):24} {scenario.title}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _split_values(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    items: list[str] = []
    for value in values:
        items.extend(part.strip() for part in value.split(",") if part.strip())
    return items or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic MemWeave memory evaluation: A=no memory, B=task memory, "
            "C=task+long-term knowledge."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true",
                        help="print the machine-readable JSON report")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--scenario", action="append", metavar="ID",
                        help="run only the given scenario id (repeatable, comma separated)")
    parser.add_argument("--tag", action="append", metavar="TAG",
                        help="run only scenarios with this tag (repeatable)")
    parser.add_argument("--workdir", metavar="PATH",
                        help="base directory for throw-away memory homes")
    parser.add_argument("--keep", action="store_true",
                        help="keep throw-away homes for inspection (dry-run debugging)")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="run each scenario N times and check deterministic agreement")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 if any scenario fails or errors")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list:
        print(format_scenario_list(SCENARIOS))
        return 0
    ids = _split_values(args.scenario)
    tags = _split_values(args.tag)
    try:
        selected = select_scenarios(SCENARIOS, ids, tags)
    except ValueError as error:
        parser.error(str(error))
    if not selected:
        parser.error("No scenarios selected.")
    if args.repeat < 1:
        parser.error("--repeat must be >= 1")
    try:
        report = run_scenarios(
            ids=ids, tags=tags, work_root=args.workdir, repeat=args.repeat,
            keep=args.keep,
        )
    except Exception as error:  # top-level harness failure (bad workdir, etc.)
        print(f"memweave_eval: error: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_human_report(report))
    if args.keep:
        kept = report["harness"].get("kept_work_dir")
        if kept and not args.json:
            print(f"\nKept evaluation work dir: {kept}")
    if args.strict:
        summary = report["summary"]
        if summary["scenarios_failed"] or summary["scenarios_errored"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

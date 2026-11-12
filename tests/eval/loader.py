"""Shared helpers for tests that exercise the memory-evaluation harness.

``scripts/memweave_eval.py`` lives outside the ``memweave`` package, so tests
load it by file path instead of importing a module name.  Keep this file free of
test cases: it is imported by ``tests/test_eval_smoke.py`` and may be reused by
future evaluation tests.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_SCRIPT = REPO_ROOT / "scripts" / "memweave_eval.py"

_CACHED_MODULE = None


def load_eval_module():
    """Load ``scripts/memweave_eval.py`` as a module and cache it."""
    global _CACHED_MODULE
    if _CACHED_MODULE is not None:
        return _CACHED_MODULE
    spec = importlib.util.spec_from_file_location("memweave_eval_harness", EVAL_SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - broken checkout
        raise RuntimeError(f"cannot load the evaluation harness from {EVAL_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses (and pickling) resolve ``cls.__module__`` through sys.modules,
    # so the module must be registered before exec_module runs the decorators.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    _CACHED_MODULE = module
    return module


def make_work_root(test_case) -> Path:
    """Create a unique, cleaned-up work root for one test case."""
    module = load_eval_module()
    base = module.resolve_work_root()
    work_root = base / f"unittest-{uuid.uuid4().hex[:10]}"
    work_root.mkdir(parents=True, exist_ok=False)
    test_case.addCleanup(lambda: shutil.rmtree(work_root, ignore_errors=True))
    return work_root

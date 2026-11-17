#!/usr/bin/env python3
"""Run a real two-session MemWeave learning-and-reuse evaluation.

The harness creates a disposable project and MemWeave home, then:

1. runs one real Codex session that observes a known validation failure;
2. resumes that session with an explicit user correction and asks it to
   close and review a MemWeave task;
3. starts a fresh Codex process against the same project/home and checks
   whether the newly learned project knowledge changes the first command.

It also runs a no-memory control with the same reuse prompt. The harness
never seeds project knowledge directly and never reads or writes the real
``~/.memweave`` home.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from memweave_agent_eval import (  # noqa: E402
    DEFAULT_PROMPTS,
    Service,
    build_fixture,
    create_minimal_codex_home,
    ensure_inside,
    evaluate_condition,
    find_codex,
    inspect_fixture,
    items_from_events,
    jsonl_events,
    now_iso,
    now_run_id,
    run_setup,
    trace_summary,
)

DEFAULT_WORK_ROOT = SCRIPT_DIR.parent / "work" / "closed-loop-eval"

LEARN_INITIAL_PROMPT = (
    "这是一次隔离的真实项目验证任务。请先通过 MemWeave 启动流程准备项目，再建立一个 "
    "MemWeave 任务，验收至少包含“记录真实验证命令、退出码和输出”。然后按 README 运行一次"
    "原始验证命令，并记录真实退出码。只运行 README 的原始命令一次；如果失败，不要尝试其它"
    "参数，不要读取验证器实现或配置，不要修改源码，也不要创建项目说明文件。"
    "把结果写入任务检查点，保持任务 current，然后停下来等待用户纠正。不要口头声称任务已完成。"
)

def learn_correction_prompt(validation_key: str) -> str:
    return (
        "用户明确纠正：本项目 README 的默认验证命令缺少签名 profile，会报 "
        "`schema v1 mismatch`；这不是业务代码错误。这个项目授权使用 schema v2 验证 profile，"
        f"正确命令是 `python tools/verify.py --validation-key {validation_key}`，它会真实校验 "
        "`src/app.json` 并输出 `OK: schema v2 compatible`。\n\n"
        "这条纠正同时包含持久规则：以后本项目验证必须首轮直接使用 active 经验给出的正确方案，"
        "不先运行已知失败命令，也不为确认而检查验证器实现。\n\n"
        "请现在完成闭环：先运行一次正确命令确认真实成功；如果还没有 current 任务就先创建。"
        "把项目事实写入 project scope 的项目经验，把‘首轮直接采用 active 正确方案’写入 user scope "
        "的用户偏好；两者都必须通过 MemWeave 知识工具实际写入。按 MemWeave continuity 的 "
        "consolidation 流程更新任务检查点、关闭任务、执行 memory_review，并用 "
        "memory_review_complete 记录审查。经验必须成为 active 知识。禁止直接编辑 Markdown、"
        "memory.db 或项目文件，禁止只口头总结。"
    )


P25_VERIFY_PROMPT = DEFAULT_PROMPTS["validation-key"]


def codex_exec_command(
    codex: Path,
    project: Path,
    prompt: str,
    model: str | None,
    session_id: str | None = None,
) -> list[str]:
    options = [
        "--trust",
        "--dangerously-bypass-hook-trust",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-c",
        'approval_policy="never"',
    ]
    if model:
        options.extend(["--model", model])
    if session_id is None:
        command = [str(codex), "exec", *options, "-C", str(project), "--json", prompt]
    else:
        command = [
            str(codex),
            "exec",
            "resume",
            *options,
            "--json",
            session_id,
            prompt,
        ]
    return command


def run_codex_turn(
    project: Path,
    codex_home: Path,
    codex: Path,
    prompt: str,
    log_path: Path,
    stderr_path: Path,
    model: str | None,
    timeout: int,
    session_id: str | None = None,
) -> dict[str, Any]:
    command = codex_exec_command(codex, project, prompt, model, session_id)
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=project,
        env={
            **os.environ,
            "CODEX_HOME": str(codex_home),
            "PYTHONUTF8": "1",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    elapsed = round(time.perf_counter() - started, 2)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    events = jsonl_events(completed.stdout)
    return {
        "returncode": completed.returncode,
        "elapsed_seconds": elapsed,
        "log": str(log_path),
        "stderr": str(stderr_path),
        "events": events,
        "thread_id": thread_id_from_events(events),
        "command": command,
    }


def thread_id_from_events(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("type") == "thread.started" and event.get("thread_id"):
            return str(event["thread_id"])
    return None


def mcp_tool_names(events: list[dict[str, Any]]) -> list[str]:
    names = []
    for item in items_from_events(events):
        if item.get("type") != "mcp_tool_call" or item.get("status") != "completed":
            continue
        tool = str(item.get("tool") or item.get("name") or "")
        if tool:
            tool = tool.split(".")[-1]
            if "memweave_" in tool:
                tool = "memweave_" + tool.split("memweave_", 1)[1]
            names.append(tool)
    return names


def memory_snapshot(home: Path, project: Path) -> dict[str, Any]:
    service = Service(home)
    service.start()
    try:
        projects = service.vault.list_records("project")
        project_record = next(
            item
            for item in projects
            if Path(item.get("root", "")).resolve() == project.resolve()
        )
        project_id = project_record["id"]
        tasks = service.vault.list_records("task", project_id=project_id)
        memories = service.vault.list_records("memory")
    finally:
        service.close()
    project_memories = [
        item
        for item in memories
        if item.get("scope") == "project" and item.get("project_id") == project_id
    ]
    user_memories = [item for item in memories if item.get("scope") == "user"]
    active_user = [
        item for item in user_memories if item.get("status") == "active"
    ]
    active_user_first = [
        item
        for item in active_user
        if "首轮" in f"{item.get('title', '')}\n{item.get('body', '')}"
        and "直接" in f"{item.get('title', '')}\n{item.get('body', '')}"
    ]
    active_project = [
        item for item in project_memories if item.get("status") == "active"
    ]
    active_correct_key = [
        item
        for item in active_project
        if "--validation-key" in f"{item.get('title', '')}\n{item.get('body', '')}"
    ]
    completed = [item for item in tasks if item.get("status") == "completed"]
    reviewed = [
        item for item in completed
        if item.get("consolidation_status") == "reviewed"
    ]
    open_tasks = [
        item
        for item in tasks
        if item.get("status") in ("current", "queued", "active", "side")
    ]
    return {
        "project_id": project_id,
        "tasks": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "status": item.get("status"),
                "consolidation_status": item.get("consolidation_status"),
                "consolidation_memory_ids": item.get("consolidation_memory_ids", []),
            }
            for item in tasks
        ],
        "task_count": len(tasks),
        "completed_task_ids": [item["id"] for item in completed],
        "reviewed_task_ids": [item["id"] for item in reviewed],
        "open_task_ids": [item["id"] for item in open_tasks],
        "project_memories": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "status": item.get("status"),
                "scope": item.get("scope"),
                "kind": item.get("kind"),
            }
            for item in project_memories
        ],
        "user_memory_count": len(user_memories),
        "user_memories": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "status": item.get("status"),
                "kind": item.get("kind"),
            }
            for item in user_memories
        ],
        "active_user_memory_ids": [item["id"] for item in active_user],
        "active_user_first_rule_ids": [item["id"] for item in active_user_first],
        "active_project_memory_ids": [item["id"] for item in active_project],
        "active_correct_key_memory_ids": [
            item["id"] for item in active_correct_key
        ],
        "active_correct_key_memory_titles": [
            item.get("title") for item in active_correct_key
        ],
    }


def unexpected_untracked_files(state: dict[str, Any]) -> list[str]:
    unexpected = []
    for line in str(state.get("git_status", "")).splitlines():
        if not line.startswith("?? "):
            continue
        path = line[3:].strip().strip('"')
        if path == "AGENTS.md":
            continue
        if path.startswith(".agents/") or path.startswith(".codex/"):
            continue
        unexpected.append(path)
    return unexpected


def learning_evidence(
    before: dict[str, Any],
    after: dict[str, Any],
    tool_names: list[str],
    initial_evaluation: dict[str, Any],
    correction_evaluation: dict[str, Any],
    project_state: dict[str, Any],
) -> dict[str, Any]:
    tools = set(tool_names)
    issues = []
    if before["active_project_memory_ids"]:
        issues.append("active project memory existed before learning")
    if initial_evaluation["first_validation_success"]:
        issues.append("initial README validation did not fail")
    if "memweave_task_open" not in tools:
        issues.append("session A did not call memweave_task_open")
    if "memweave_task_close" not in tools:
        issues.append("session A did not call memweave_task_close")
    if "memweave_memory_review" not in tools:
        issues.append("session A did not call memweave_memory_review")
    if "memweave_memory_review_complete" not in tools:
        issues.append("session A did not call memweave_memory_review_complete")
    if not after["reviewed_task_ids"]:
        issues.append("no completed task recorded a completed knowledge review")
    if not after["active_correct_key_memory_ids"]:
        issues.append("no active project memory contains the corrected validation key")
    if not after["active_user_first_rule_ids"]:
        issues.append("no active user preference requires first-round direct adoption")
    if after["open_task_ids"]:
        issues.append("session A left an open MemWeave task")
    if not project_state["source_unmodified"]:
        issues.append("session A modified tracked project files")
    untracked = unexpected_untracked_files(project_state)
    if untracked:
        issues.append("session A created unexpected project files: " + ", ".join(untracked))
    return {
        "persisted": not issues,
        "issues": issues,
        "pre_active_project_memory_ids": before["active_project_memory_ids"],
        "active_correct_key_memory_ids": after[
            "active_correct_key_memory_ids"
        ],
        "active_correct_key_memory_titles": after[
            "active_correct_key_memory_titles"
        ],
        "active_user_first_rule_ids": after["active_user_first_rule_ids"],
        "reviewed_task_ids": after["reviewed_task_ids"],
        "open_task_ids": after["open_task_ids"],
        "tool_names": sorted(tools),
        "task_open_called": "memweave_task_open" in tools,
        "task_close_called": "memweave_task_close" in tools,
        "memory_review_called": "memweave_memory_review" in tools,
        "memory_review_complete_called": "memweave_memory_review_complete" in tools,
        "memory_write_called": "memweave_memory_write" in tools,
        "memory_activate_called": "memweave_memory_activate" in tools,
        "initial_first_validation_success": initial_evaluation[
            "first_validation_success"
        ],
        "initial_validation_failure_count": initial_evaluation[
            "validation_failure_count"
        ],
        "initial_used_compat": initial_evaluation[
            "first_validation_used_compat"
        ],
        "initial_hidden_verifier_inspected": initial_evaluation[
            "hidden_verifier_inspected"
        ],
        "correction_first_validation_success": correction_evaluation[
            "first_validation_success"
        ],
        "correction_used_compat": correction_evaluation[
            "first_validation_used_compat"
        ],
        "correction_used_validation_key": correction_evaluation[
            "first_validation_used_validation_key"
        ],
        "project_source_unmodified": project_state["source_unmodified"],
        "unexpected_untracked_files": untracked,
    }


def assess_behavior(
    learning: dict[str, Any],
    control: dict[str, Any],
    reuse: dict[str, Any],
    injected_body_has_validation_key: bool,
) -> dict[str, Any]:
    control_failed_first = (
        not control["first_validation_success"]
        and control["validation_failure_count"] >= 1
    )
    reuse_first_success = (
        reuse["first_validation_success"]
        and reuse["first_validation_used_validation_key"]
        and reuse["validation_failure_count"] == 0
        and not reuse["repeated_failure"]
        and reuse["final_validation_success"]
    )
    injected = (
        reuse["project_memory_injected"]
        and reuse["user_memory_injected"]
        and injected_body_has_validation_key
    )
    attributable = (
        not reuse.get("hidden_verifier_inspection_before_first_validation", False)
        and reuse["source_unmodified"]
    )
    issues = []
    if not learning["persisted"]:
        issues.append("learning persistence failed")
    if not control_failed_first:
        issues.append("control did not fail its first validation")
    if control["included_memory_ids"]:
        issues.append("control unexpectedly received project memory")
    if not reuse["automatic_prepare"]:
        issues.append("reuse session did not prepare before its first command")
    if not injected:
        issues.append("learned compat knowledge was not injected with its body")
    if not reuse_first_success:
        issues.append("reuse session did not succeed on its first compat validation")
    if not attributable:
        issues.append("reuse success is not attributable to learned memory")
    return {
        "criterion_met": not issues,
        "issues": issues,
        "control_failed_first_validation": control_failed_first,
        "reuse_injected_memory": injected,
        "reuse_first_correct_success": reuse_first_success,
        "reuse_attributable": attributable,
        "changed_from_failure_to_first_success": (
            control_failed_first and reuse_first_success
        ),
        "behavior_gain_attributable_to_learned_memory": (
            learning["persisted"] and control_failed_first
            and injected and reuse_first_success and attributable
        ),
    }


def condition_memory(
    project_memory_id: str,
    user_memory_id: str = "no-user-memory",
) -> dict[str, Any]:
    return {
        "project_id": None,
        "project_memory_id": project_memory_id,
        "user_memory_id": user_memory_id,
        "candidate_id": "no-candidate",
        "retired_id": "no-retired",
        "candidate_title": "no-candidate-title",
        "retired_title": "no-retired-title",
    }


def prepare_text_has_validation_key(
    evaluation: dict[str, Any],
    validation_key: str,
) -> bool:
    prepare_results = evaluation.get("prepare_results") or []
    return validation_key in json.dumps(prepare_results, ensure_ascii=False)


def summary_markdown(report: dict[str, Any]) -> str:
    learning = report["learning"]
    control = report["conditions"]["control"]
    reuse = report["conditions"]["reuse"]
    behavior = report["behavior"]
    lines = [
        "# MemWeave 真实双会话闭环评测",
        "",
        f"- run: `{report['run_id']}`",
        f"- 时间: `{report['started_at']}`",
        f"- 模型: `{report.get('model') or 'Codex 配置默认模型'}`",
        f"- 结论: **{report['conclusion']}**",
        "",
        "| 条件 | 无预置记忆 | 自动 prepare | 项目经验注入 | 用户经验注入 | 首次成功 | 首次正确参数 | 失败次数 | 最终成功 | 源码未改 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in ("control", "reuse"):
        row = report["conditions"][name]
        before = (
            report["before_control"]
            if name == "control"
            else report["before_learning"]
        )
        lines.append(
            f"| {name} | {before['active_project_memory_ids'] == []} | "
            f"{row['automatic_prepare']} | {row['project_memory_injected']} | "
            f"{row['user_memory_injected']} | "
            f"{row['first_validation_success']} | "
            f"{row['first_validation_used_validation_key']} | "
            f"{row['validation_failure_count']} | {row['final_validation_success']} | "
            f"{row['source_unmodified']} |"
        )
    lines += [
        "",
        "## 学习会话",
        "",
        f"- 预置 active 项目经验: {learning['pre_active_project_memory_ids'] or 'none'}",
        f"- 初始验证失败: {learning['initial_first_validation_success'] is False}",
        f"- task_open / task_close: {learning['task_open_called']} / {learning['task_close_called']}",
        f"- memory_review / complete: {learning['memory_review_called']} / "
        f"{learning['memory_review_complete_called']}",
        f"- reviewed task IDs: {learning['reviewed_task_ids'] or 'none'}",
        f"- active correct-key memory IDs: "
        f"{learning['active_correct_key_memory_ids'] or 'none'}",
        f"- active user first-rule IDs: {learning['active_user_first_rule_ids'] or 'none'}",
        f"- learning persisted: {learning['persisted']}",
        f"- learning issues: {learning['issues'] or 'none'}",
        "",
        "## 行为判定",
        "",
        f"- behavior: {behavior}",
        f"- verifier source inspected before first: "
        f"control={control['verifier_source_inspection_before_first_validation']}, "
        f"reuse={reuse['verifier_source_inspection_before_first_validation']} "
        "(the random validation key exists only in learned memory)",
        f"- criterion met: {report['criterion_met']}",
        "",
        "## 边界",
        "",
        "- 这是单次真实模型闭环，不是统计成功率。",
        "- 学习会话根据用户明确纠正写入项目经验和用户偏好；用户未纠正时不会自动知道随机签名 key。",
        "- 复用会话为全新 Codex 进程，但仍使用隔离的同一临时项目与 MemWeave home。",
        "- 仓库只保存随机 key 的 SHA-256；若 key 已泄漏或可从源码推导，则不能归因于经验。",
        "",
        "完整事件日志、任务/知识快照和项目状态见同目录 JSON 与日志文件。",
    ]
    return "\n".join(lines) + "\n"


def run_evaluation(args: argparse.Namespace) -> dict[str, Any]:
    if not args.allow_unsandboxed:
        raise ValueError("--allow-unsandboxed is required for a real Agent run")
    work_root = Path(args.work_root).expanduser().resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    run_root = ensure_inside(work_root / now_run_id(), work_root)
    run_root.mkdir()
    codex = find_codex(args.codex)
    codex_home = create_minimal_codex_home(run_root / "codex-home", args.model)
    validation_key = "p25-" + secrets.token_hex(12)
    correction_prompt = learn_correction_prompt(validation_key)
    logs = run_root / "logs"
    state_dir = run_root / "states"
    state_dir.mkdir()
    reuse_prompt = P25_VERIFY_PROMPT

    def build_case(name: str) -> tuple[Path, Path, dict[str, Any]]:
        project = ensure_inside(run_root / "projects" / name, run_root)
        home = ensure_inside(run_root / "homes" / name, run_root)
        project.mkdir(parents=True)
        home.mkdir(parents=True)
        build_fixture(project, "validation-key", validation_key=validation_key)
        setup = run_setup(project, home)
        return project, home, setup

    control_project, control_home, control_setup = build_case("control")
    control_before = memory_snapshot(control_home, control_project)
    control_turn = run_codex_turn(
        control_project,
        codex_home,
        codex,
        reuse_prompt,
        logs / "control.jsonl",
        logs / "control.stderr.log",
        args.model,
        args.timeout,
    )
    control_state = inspect_fixture(control_project)
    control_eval = evaluate_condition(
        "control",
        control_turn["events"],
        control_state,
        condition_memory("no-project-memory"),
        "validation-key",
    )

    learn_project, learn_home, learn_setup = build_case("learn")
    before_learning = memory_snapshot(learn_home, learn_project)
    initial_turn = run_codex_turn(
        learn_project,
        codex_home,
        codex,
        LEARN_INITIAL_PROMPT,
        logs / "learn-initial.jsonl",
        logs / "learn-initial.stderr.log",
        args.model,
        args.timeout,
    )
    initial_project_state = inspect_fixture(learn_project)
    initial_eval = evaluate_condition(
        "learn-initial",
        initial_turn["events"],
        initial_project_state,
        condition_memory("no-project-memory"),
        "validation-key",
    )
    if not initial_turn["thread_id"]:
        raise RuntimeError("session A did not emit thread.started with a thread_id")
    correction_turn = run_codex_turn(
        learn_project,
        codex_home,
        codex,
        correction_prompt,
        logs / "learn-correction.jsonl",
        logs / "learn-correction.stderr.log",
        args.model,
        args.timeout,
        session_id=initial_turn["thread_id"],
    )
    learn_project_state = inspect_fixture(learn_project)
    correction_eval = evaluate_condition(
        "learn-correction",
        correction_turn["events"],
        learn_project_state,
        condition_memory("no-project-memory"),
        "validation-key",
    )
    after_learning = memory_snapshot(learn_home, learn_project)
    all_learn_tool_names = mcp_tool_names(
        initial_turn["events"] + correction_turn["events"]
    )
    learning = learning_evidence(
        before_learning,
        after_learning,
        all_learn_tool_names,
        initial_eval,
        correction_eval,
        learn_project_state,
    )

    reuse_turn = run_codex_turn(
        learn_project,
        codex_home,
        codex,
        reuse_prompt,
        logs / "reuse.jsonl",
        logs / "reuse.stderr.log",
        args.model,
        args.timeout,
    )
    reuse_state = inspect_fixture(learn_project)
    learned_memory_id = (
        learning["active_correct_key_memory_ids"][0]
        if learning["active_correct_key_memory_ids"]
        else "missing-learned-memory"
    )
    learned_user_memory_id = (
        learning["active_user_first_rule_ids"][0]
        if learning["active_user_first_rule_ids"]
        else "missing-user-memory"
    )
    reuse_eval = evaluate_condition(
        "reuse",
        reuse_turn["events"],
        reuse_state,
        condition_memory(learned_memory_id, learned_user_memory_id),
        "validation-key",
    )
    after_reuse = memory_snapshot(learn_home, learn_project)
    injected_body_has_validation_key = prepare_text_has_validation_key(
        reuse_eval,
        validation_key,
    )
    behavior = assess_behavior(
        learning,
        control_eval,
        reuse_eval,
        injected_body_has_validation_key,
    )
    report: dict[str, Any] = {
        "schema": 1,
        "run_id": run_root.name,
        "started_at": now_iso(),
        "model": args.model,
        "validation_key": validation_key,
        "codex": str(codex),
        "work_root": str(work_root),
        "real_home_not_used": str((Path.home() / ".memweave").resolve()),
        "prompts": {
            "learn_initial": LEARN_INITIAL_PROMPT,
            "learn_correction": correction_prompt,
            "reuse": reuse_prompt,
        },
        "setup": {
            "control": control_setup,
            "learn": learn_setup,
        },
        "threads": {
            "learn_initial": initial_turn["thread_id"],
            "learn_correction": correction_turn["thread_id"],
            "control": control_turn["thread_id"],
            "reuse": reuse_turn["thread_id"],
        },
        "turns": {
            name: {
                key: value
                for key, value in turn.items()
                if key not in ("events", "command")
            }
            for name, turn in {
                "control": control_turn,
                "learn_initial": initial_turn,
                "learn_correction": correction_turn,
                "reuse": reuse_turn,
            }.items()
        },
        "before_learning": before_learning,
        "before_control": control_before,
        "after_learning": after_learning,
        "after_reuse": after_reuse,
        "learning": learning,
        "conditions": {
            "control": control_eval,
            "reuse": reuse_eval,
        },
        "initial": initial_eval,
        "correction": correction_eval,
        "behavior": behavior,
        "criterion_met": behavior["criterion_met"],
        "conclusion": (
            "真实双会话闭环成立：A 从用户纠正沉淀 active 项目经验，B 首轮自动复用"
            if behavior["criterion_met"]
            else "真实双会话闭环未成立"
        ),
        "limits": [
            "single closed-loop model replay, no statistical claim",
            "the learning turn receives an explicit user correction",
            "control and reuse use identical prompts but separate disposable projects",
            "nested host sandbox unavailable; explicit --allow-unsandboxed is required",
        ],
        "finished_at": now_iso(),
    }
    for name, snapshot in {
        "control_before": control_before,
        "control_after": memory_snapshot(control_home, control_project),
        "learn_before": before_learning,
        "learn_after": after_learning,
        "reuse_after": after_reuse,
    }.items():
        (state_dir / f"{name}.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    for name, snapshot in {
        "control": control_state,
        "learn_initial": initial_project_state,
        "learn_final": learn_project_state,
        "reuse": reuse_state,
    }.items():
        (state_dir / f"{name}-project.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    (run_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (run_root / "summary.md").write_text(summary_markdown(report), encoding="utf-8")
    report["run_root"] = str(run_root)
    return report


def reanalyze_run(run_root: Path) -> dict[str, Any]:
    work_root = DEFAULT_WORK_ROOT.resolve()
    run_root = ensure_inside(run_root.expanduser().resolve(), work_root)
    report_path = run_root / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    control_before_path = ensure_inside(
        run_root / "states" / "control_before.json",
        run_root,
    )
    report["before_control"] = json.loads(
        control_before_path.read_text(encoding="utf-8")
    )
    validation_key = report.get("validation_key")
    if not validation_key:
        match = re.search(
            r"--validation-key\s+([A-Za-z0-9_-]+)",
            report["prompts"]["learn_correction"],
        )
        if not match:
            raise ValueError("saved report does not contain the validation key")
        validation_key = match.group(1)
    learned_memory_id = (
        report["learning"]["active_correct_key_memory_ids"][0]
        if report["learning"]["active_correct_key_memory_ids"]
        else "missing-learned-memory"
    )
    learned_user_memory_id = (
        report["learning"]["active_user_first_rule_ids"][0]
        if report["learning"]["active_user_first_rule_ids"]
        else "missing-user-memory"
    )
    for name in ("control", "reuse"):
        turn = report["turns"][name]
        log_path = ensure_inside(Path(turn["log"]).resolve(), run_root)
        state_path = ensure_inside(
            run_root / "states" / f"{name}-project.json",
            run_root,
        )
        events = jsonl_events(log_path.read_text(encoding="utf-8", errors="replace"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        memory = (
            condition_memory("no-project-memory")
            if name == "control"
            else condition_memory(learned_memory_id, learned_user_memory_id)
        )
        report["conditions"][name] = evaluate_condition(
            name,
            events,
            state,
            memory,
            "validation-key",
        )
    behavior = assess_behavior(
        report["learning"],
        report["conditions"]["control"],
        report["conditions"]["reuse"],
        prepare_text_has_validation_key(
            report["conditions"]["reuse"],
            validation_key,
        ),
    )
    report["behavior"] = behavior
    report["criterion_met"] = behavior["criterion_met"]
    report["conclusion"] = (
        "真实双会话闭环成立：A 从用户纠正沉淀 active 项目经验，B 首轮自动复用"
        if behavior["criterion_met"]
        else "真实双会话闭环未成立"
    )
    report["reanalyzed_at"] = now_iso()
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (run_root / "summary.md").write_text(summary_markdown(report), encoding="utf-8")
    report["run_root"] = str(run_root)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--codex", help="path to codex.exe")
    parser.add_argument("--model", help="model id override; otherwise use the Codex config")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--reanalyze",
        type=Path,
        help="recompute an existing closed-loop run from saved logs",
    )
    parser.add_argument(
        "--allow-unsandboxed",
        action="store_true",
        help="required: run the disposable fixtures without the host sandbox",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when learning, control or reuse evidence does not close the loop",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = (
            reanalyze_run(args.reanalyze)
            if args.reanalyze
            else run_evaluation(args)
        )
    except (OSError, ValueError, RuntimeError, StopIteration, subprocess.TimeoutExpired) as error:
        print(
            f"memweave_closed_loop_eval: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 2
    print(f"run: {report['run_root']}")
    print(f"conclusion: {report['conclusion']}")
    print(f"learning: {json.dumps(report['learning'], ensure_ascii=False)}")
    print(f"behavior: {json.dumps(report['behavior'], ensure_ascii=False)}")
    if args.strict and not report["criterion_met"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# tests/eval — 记忆评估基座的测试辅助

这里存放 `scripts/memweave_eval.py` 的测试辅助与快速门禁支持，不放产品运行时逻辑。

- `loader.py`：按文件路径加载评估脚本（脚本不在 `memweave` 包内），并提供测试专用的
  临时工作目录工厂；目录在测试结束后删除。
- `../test_eval_smoke.py`：CI 冒烟门禁。跑 2 个场景（`smoke` 标签）验证 A/B/C 条件、
  用 `repeat=2` 验证确定性评分可复现，通过子进程验证
  `python scripts/memweave_eval.py --scenario ... --json` 入口，并用一个轻量 registry
  检查保证关键场景（`evidence_pointer_persistence`、
  `consolidation_lint_and_truncation`、`knowledge_index_injection`、`behavioral_reuse`）
  仍在注册表中且没有把重场景混进 `smoke`。冒烟门禁不执行这些重场景。

约定：

- 场景、评分器和 A/B/C 条件定义在 `scripts/memweave_eval.py` 的 `SCENARIOS` 中；
  新增场景需要同时声明 `build`（B 条件世界）、`learn`（C 条件追加的长期知识）和
  `make_checks`（带 A/B/C 期望值的确定性断言）。不要在这里复制场景数据。
- 评估夹具写到评估脚本解析出的临时工作目录（系统临时目录或仓库 `work/eval-tmp/`），
  绝不写 `%USERPROFILE%\.memweave`。测试通过 `MEMWEAVE_EVAL_WORKDIR`/`--workdir`
  可以覆盖位置。
- 这里只做确定性检查：不调用模型、不联网。`behavioral_reuse` 是确定性策略回放，
  证明上下文会改变一个明确的决策函数，不证明真实模型成功率；桌面/模型验证仍需单独进行。

## Real Agent adoption check

`scripts/memweave_agent_eval.py` is a separate, opt-in real-model A/B harness. It creates
two disposable Git fixtures and two disposable MemWeave homes, installs the Codex
adapter, optionally seeds one reviewed project lesson plus one explicit user preference
in the `on` condition, and runs the same request in fresh Codex processes.

Scenarios cover destructive cleanup (`safe-cleaner`, `cleanup-mode`) and a known-failing
verification command (`validation-flag`). The validation scenario records whether the
first verifier invocation succeeded, how many verifier invocations used the compatible
flag, failure count, repeated failures, final output, wrapper exit codes, and whether
the model inspected the hidden verifier implementation before its first attempt.

It also records automatic `workspace_prepare`, `included_sources`, candidate/retired
isolation, actual commands, and before/after files. A single run is evidence, not a
success-rate benchmark. It never uses the real `~/.memweave` home and writes reports
under `work/real-agent-eval/`. Because the Windows CLI may not be able to launch the
packaged host sandbox, the disposable fixture run requires the explicit
`--allow-unsandboxed` acknowledgement. `--reanalyze <run-directory>` recomputes a saved
run from its raw event logs without starting another model.

`scripts/memweave_closed_loop_eval.py` is a two-session real-Agent harness. It runs a
no-memory control with the reuse prompt, then a learning session and a fresh reuse
session against the same disposable project and MemWeave home:

- The learning session first observes a known failure, receives an explicit user
  correction, must actually create, checkpoint and close a MemWeave task, write
  project and user knowledge, and complete knowledge review.
- The reuse session is a new Codex process. It must call `workspace_prepare` before
  its first command, receive the learned records in `included_sources`, and use the
  active correct validation command without a failure.
- `validation-key` uses a random signed validation key. The repository stores only
  its SHA-256; the value exists only in the user correction and learned knowledge,
  so a no-memory control cannot derive it from source files.

The harness records raw events, task/knowledge snapshots, actual command exit codes,
first-action success, failure count, source attribution, and whether review actually
completed. It also supports `--reanalyze <run-directory>` for correcting metrics from
saved logs without another model run. A single run remains evidence, not a
success-rate claim.

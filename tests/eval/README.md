# tests/eval — 记忆评估基座的测试辅助

这里存放 `scripts/memweave_eval.py` 的测试辅助与快速门禁支持，不放产品运行时逻辑。

- `loader.py`：按文件路径加载评估脚本（脚本不在 `memweave` 包内），并提供测试专用的
  临时工作目录工厂；目录在测试结束后删除。
- `../test_eval_smoke.py`：CI 冒烟门禁。跑 2 个场景（`smoke` 标签）验证 A/B/C 条件、
  用 `repeat=2` 验证确定性评分可复现，通过子进程验证
  `python scripts/memweave_eval.py --scenario ... --json` 入口，并用一个轻量 registry
  检查保证 P1/P2/P3 场景（`evidence_pointer_persistence`、
  `consolidation_lint_and_truncation`、`knowledge_index_injection`）仍在注册表中且没有把
  重场景混进 `smoke`。冒烟门禁不执行这三个重场景。

约定：

- 场景、评分器和 A/B/C 条件定义在 `scripts/memweave_eval.py` 的 `SCENARIOS` 中；
  新增场景需要同时声明 `build`（B 条件世界）、`learn`（C 条件追加的长期知识）和
  `make_checks`（带 A/B/C 期望值的确定性断言）。不要在这里复制场景数据。
- 评估夹具写到评估脚本解析出的临时工作目录（系统临时目录或仓库 `work/eval-tmp/`），
  绝不写 `%USERPROFILE%\.memweave`。测试通过 `MEMWEAVE_EVAL_WORKDIR`/`--workdir`
  可以覆盖位置。
- 这里只做确定性检查：不调用模型、不联网；人工/模型评审模板见 `docs/eval.md`。

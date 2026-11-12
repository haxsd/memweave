# 实现参考与许可证边界

MemWeave 采用两个上游项目的不同层次成果：

- **Basic Memory：源码级 fork/改编**，用于 Markdown 存储、SQLite 派生索引、FTS 检索、上下文和关系能力。
- **planning-with-files：行为与 Skill 级改编**，用于任务计划、发现、进展、失败、验证和恢复纪律；没有复制其脚本，也没有运行第二套计划文件系统。

## 上游基线

| 来源 | 固定提交 | 许可证 | 使用方式 | 义务与边界 |
|---|---|---|---|---|
| [planning-with-files](https://github.com/OthmanAdi/planning-with-files) | `dcdfb278de53fdb69b2067ea173dbb1de45345a3` | MIT | 改编任务生命周期和连续性 Skill | 保留版权与 MIT 许可声明；记录改编范围；不把上游计划正文当成可执行指令 |
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) | `438d49776b8682f0a561bb3dc66092a33a5a829b` | AGPL-3.0-or-later | fork/改编 Markdown、SQLite 索引、检索、上下文和关系能力 | MemWeave 整体按 AGPL-3.0-or-later 发布；保留来源、修改记录和源码获取方式 |

固定提交是当前实现核对基线；同步上游时先更新来源清单和差异说明。

## 实际采用范围

- `src/basic_memory/` 是固定提交上游源码树的运行时子集，不是完整上游发行树。
- MCP 的 lifespan 启动同一个 `Service` 和 Basic Memory index；Basic Memory 自己的 MCP、HTTP、CLI、cloud、importer、watcher 和 man-page 入口没有作为产品表面启动。
- 业务记录仍以 Markdown 为权威；SQLite 是可重建的派生索引。
- planning-with-files 的脚本没有原样打包；其计划、发现、进展、失败、验证和恢复纪律进入 MemWeave 的任务、WorkUnit、检查点和连续性 Skill。
- 没有复制 planning-with-files 的 `task_plan.md` / `findings.md` / `progress.md` 文件系统，也没有实现其 plan-hash attestation、`check-complete` 或 `gate-stop`。

## 许可证义务

1. MemWeave 整体以 `AGPL-3.0-or-later` 发布，根目录 [LICENSE](../LICENSE) 为完整许可证文本。
2. 直接复制的 MIT 文件保留原版权与许可文本；AGPL 文件保留来源与修改说明。
3. 第三方来源、固定提交、复制/修改文件和当前路径记录在 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
4. 同步上游时更新固定提交、差异范围和验证结果，不把合并后的代码伪装成完全原创。
5. 若未来需要闭源发行，必须重新选择架构或取得额外授权，不能继续使用当前融合代码路径。

产品边界和运行时设计见 [架构说明](architecture.md)。

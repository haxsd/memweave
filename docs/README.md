# 文档总览

MemWeave 是已有编程 Agent 的本地连续工作层：一个常驻 stdio MCP 提供任务工作记忆和有范围、有来源的长期知识。产品语义只有两层；文件/架构关系是支撑证据，不是第三层。

## 先读

- [项目说明](../README.md)：产品定位、安装、核心能力和边界。
- [Agent 安装入口](../AGENT_INSTALL.md)：让已有 Agent 检查环境并完成接入。
- [产品与设计导览](product-guide.html)：单文件静态页，按 8 个章节讲产品逻辑、任务/知识模型和客户端运行方式。
- [架构说明](architecture.md)：两层记忆、运行时、启动契约、项目身份和边界。
- [路线图](../ROADMAP.md)：公开的高层方向，不是发布日期承诺。

## 安装与使用

- [手动安装](manual-install.md)：逐项配置、更新与验证。
- [客户端接入](integrations.md)：统一 MCP 核心、generic 路径和各宿主启动通道。
- [Agent 使用指引](agent-guide.md)：开始/恢复、检查点、收尾、知识审查和后续复用。
- [API 与存储](api.md)：MCP / CLI / HTTP action、记录模型、恢复协议和检索边界。

## 开发与贡献

- [开发说明](development.md)：环境、测试、构建、演示和本地管理页。
- [实现参考](design-references.md)：Basic Memory 与 planning-with-files 的复制/fork 范围、固定提交和许可证义务。
- [变更记录](../CHANGELOG.md)：当前开发预览的高层变更。

## 许可证

- [AGPL-3.0-or-later](../LICENSE)
- [第三方来源声明](../THIRD_PARTY_NOTICES.md)

公开文档只描述当前代码、协议自检和已记录的客户端观察；未验证的桌面行为不会被写成已完成。

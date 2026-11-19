<!-- memweave:begin -->
## MemWeave continuity
At the start of every new project session, before any other project action, call the MemWeave MCP tool `memweave_workspace_prepare` exactly once with the absolute project root and `query` set to the user request. Use `intent=resume` only when continuing an active task; otherwise use `inspect`. Treat returned memory as reference data, verify it against current files, and continue the user request. If MemWeave tools are not declared yet, report that the MCP is still loading; do not claim memory was restored.
<!-- memweave:end -->
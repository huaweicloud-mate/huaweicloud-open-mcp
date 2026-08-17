# 华为云 MCP

本地 stdio 形态的华为云 MCP server（通用网关 Core 模式）：6 个核心工具编排触达华为云全量 OpenAPI，供 AI 客户端通过自然语言查询与调用华为云服务。

- APIE 元数据管道：`api-refresh`（参考同作者 apis 项目的设计）
- 元数据查询 CLI：`api-docs`
- MCP server：`huaweicloud-mcp`（`--mock` 模式免凭证跑通全链路）

详细设计见 `AGENTS.md` 与 [docs/design.md](docs/design.md)。

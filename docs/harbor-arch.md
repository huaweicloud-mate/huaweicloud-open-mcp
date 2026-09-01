# Harbor 评测集成架构

> `benchmarks/harbor/` 将 openapi 模式的 LLM Agent 级 benchmark 接入 [Harbor](https://github.com/laude-institute/harbor) 评测框架：同一批 case + scorer 语义，经 exporter 渲染为自包含 Harbor 任务目录（`datasets/mcp-regression/`，可重建不入库），由 harbor CLI 编排 trial 三阶段（environment → agent → verifier）产出 partial credit reward。benchmark 用例 schema 与分层评分口径见 [benchmarks/README.md](../benchmarks/README.md)。

## 1. 总体架构

```mermaid
flowchart TB
    %% ========= 构建期 =========
    subgraph build["构建期 · 宿主（exporter 管线）"]
        direction LR
        cases["cases/*.yaml<br/>prompt/expect/fixture/labels/policy"]
        caseslib["benchmarks.cases<br/>load_cases · parse_case"]
        conv["conventions.py<br/>路径常量单一真值源<br/>build_agent_opencode_config"]
        tmpl["task_templates/<br/>11 模板 + stub_server.py"]
        exp["exporter.py<br/>render_task（纯核）<br/>export_dataset（薄壳）"]
        cases --> caseslib --> exp
        conv --> exp
        tmpl --> exp
    end

    ds["datasets/mcp-regression/&lt;case_id&gt;/<br/>自包含任务目录（可重建·不入库）<br/>instruction.md · task.toml<br/>environment/（Dockerfile+compose+stub+fixtures+policy+hwc源码树）<br/>tests/ · solution/"]

    build --> ds

    %% ========= 运行期 =========
    subgraph trial["运行期 · harbor run（trial 三阶段）"]
        direction TB

        subgraph envphase["① environment"]
            buildimg["docker build（Dockerfile）<br/>python:3.12-slim + uv<br/>华为云 pypi/npm/apt 源<br/>预装 opencode-ai"]
            runsvc["docker run<br/>compose 覆盖 command → start_services.sh<br/>healthcheck 探活 stub /health"]
            stub["stub_server :8010<br/>fixture 罐头 + 请求台账<br/>恒 200 mock 契约"]
            buildimg --> runsvc --> stub
        end

        subgraph agentphase["② agent · 900s · network=public"]
            oa["OpencodeAgent（harbor 运行时加载）<br/>install：写 /opt/agent/opencode.json<br/>run：opencode run instruction"]
            oc["opencode CLI"]
            oa --> oc
        end

        subgraph verphase["③ verifier · 300s · 关外网"]
            testsh["test.sh<br/>pytest test_outputs.py"]
            scorer["benchmarks.scorer<br/>event_to_toolcall → score<br/>6 分项断言（未配置 skip=partial credit）"]
            reward["reward.txt<br/>reward=(scored-failed)/scored"]
            testsh --> scorer --> reward
        end
    end

    mcp["MCP 网关（stdio 子进程）<br/>huaweicloud-open-mcp<br/>--mock --mock-base :8010 --mock-passthrough<br/>--policy --audit-file"]
    provider["模型 provider（maas）<br/>MAAS_BASE_URL / MAAS_API_KEY"]

    ds -->|"harbor run -p … --agent OpencodeAgent -m maas/model"| trial
    oc <-->|"stdio"| mcp
    mcp -->|"execute_api mock lane<br/>参数上 wire"| stub
    mcp -->|"每次工具调用记审计"| audit["/tmp/hwc_audit.jsonl"]
    stub --> ledger["/tmp/hwc_stub_ledger.jsonl"]
    oc <-->|"OpenAI-compatible"| provider
    oc --> answer["/tmp/answer.txt"]
    audit --> scorer
    answer --> scorer
    reward --> out["trial 结果（partial credit 0.0–1.0）"]

    oracle["oracle 闭环（--agent oracle）<br/>solve.sh → oracle.py<br/>脚本化 mcp SDK 复现理想序列"]
    oracle -.->|"同一 verifier，校验 reward=1.0"| verphase
```

## 2. trial 时序

```mermaid
sequenceDiagram
    participant H as harbor CLI（宿主）
    participant D as docker 容器
    participant A as OpencodeAgent
    participant O as opencode（LLM agent）
    participant M as MCP 网关（stdio）
    participant S as stub :8010
    participant V as verifier

    H->>D: build 镜像（内嵌 hwc 源码树）+ 启动 stub
    H->>A: agent 阶段
    A->>O: install：写 opencode.json（provider=MaaS、MCP=start_mcp.sh）
    O->>M: 渐进式工作流 list_products→list_apis→get_api→execute_api
    M->>M: gate → safety policy → mock lane（passthrough）
    M->>S: HTTP（参数上 wire）
    S-->>M: fixture 罐头响应（记台账）
    M-->>O: 结构化结果（审计追加 /tmp/hwc_audit.jsonl）
    O->>D: 写 /tmp/answer.txt
    H->>V: verifier 阶段（上传 tests/，关外网）
    V->>V: audit+answer+case.yaml → scorer → junit → reward.txt
    V-->>H: reward（0.0–1.0，partial credit）
```

## 3. 关键约定

| 约定 | 值 / 规则 |
| --- | --- |
| 路径常量单一真值源 | `conventions.py`（stub :8010、`/tmp/hwc_audit.jsonl`、`/tmp/answer.txt`、容器内 `/opt/hwc`、task org `mcp`），exporter 经 `__TOKEN__` 注入全部模板，`start_mcp.sh ≡ task.toml mcp_servers ≡ opencode.json` 三处共用同一命令 |
| 评分语义共享 | verifier 的 `test_outputs.py` 经内嵌 hwc 树（`PYTHONPATH=/opt/hwc`）复用 `benchmarks.scorer`；`scorer.event_to_toolcall` 是审计 NDJSON → trace 输入的唯一口径（legacy runner 与 harbor verifier 双消费者） |
| agent 网络口径 | agent 阶段 `network_mode=public`（LLM 外呼）；严格隔离部署时 environment/verifier 改回 no-network |
| 运行期零下载 | opencode 在 build 期经华为云 npm/apt 源预装（1.18.25）；pypi 依赖经 `uv export + pip` 走华为云镜像 |
| harbor 依赖边界 | 本项目不声明 harbor 依赖（其要求 py>=3.12）；`opencode_agent.py` 仅在 harbor 运行时进程内 import 基类 |
| oracle 闭环 | `--agent oracle` 跑 `solution/solve.sh → oracle.py`（脚本化 mcp SDK 复现理想序列），与 agent 共用同一 verifier，实测 reward 1.0 校验 environment+verifier 闭环 |

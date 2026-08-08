# Argus ☉ —— 运维 Agent Harness

> **记住整个平台，安全地动生产。**

Argus 是一个**运维 agent harness**：它让 AI agent 在真实的服务器环境里干活时，
既「记得住整个平台」，又「动得安全」。Argus 完全 fork 自
[Hermes Agent](https://hermes-agent.nousresearch.com/)（MIT License），
在保留其 agent 内核（终端、工具调用、会话、网关、插件）的基础上，独立演进出
**面向生产运维的三层核心能力**。

## 核心 = 三层纵深

| 层 | 载体 | 职责 |
|---|---|---|
| **事实层** | 平台拓扑表（`plugins/memory/topo` + `topo_query` / `topo_update`） | 长记忆地记住平台：实体、环境、依赖、归属。任何运维动作前先 `topo_query` 确认目标身份与环境；跨环境操作默认拒绝 |
| **程序层** | runbook（`tools/runbook_tools.py`：`runbook_load` / `runbook_checkpoint`） | 唯一允许的「怎么动」：事故处理按 runbook 匹配流程执行；L4 部署按 checklist 阶段门推进，不允许跳过 |
| **纵深防御** | 权限矩阵（`tools/ops_permissions.py`） | 命令分级（L1–L4）× 环境（test/uat/prod）→ **执行 / 审批 / 拒绝**。矩阵 DENY 不可被 yolo、mode=off 或永久 allowlist 绕过；approve 必须有真人在场 |

设计原则（详见 [`ops-agent-harness.md`](ops-agent-harness.md) 与
[`OPS-DELTA.md`](OPS-DELTA.md)）：

- **零侵入优先**：核心能力全部以 memory provider 块 / registry 工具 /
  配置开关的形式接入，不碰 conversation loop、prompt 缓存与 system prompt。
- **配置走 `config.yaml`**：所有行为开关（拓扑、runbook、权限矩阵、环境级别）
  都在 ops profile 的 `config.yaml` 里，不新增 `HERMES_*` env var。
- **默认 fail-closed**：权限矩阵默认 `env: test`，核对通过后再切 prod。

## 快速开始

### 1. 安装（开发 / 自托管）

```bash
cd argus_agent
python3 -m venv .venv
.venv/bin/pip install -e .          # 生成 argus / hermes 两个命令入口
```

> 发行包名沿用 `hermes-agent`（fork 兼容），产品名是 **Argus**。
> `hermes` 命令保留作为兼容入口，行为不变；日常请用 `argus`。

### 2. 初始化 ops profile

```bash
.venv/bin/python scripts/ops_init.py            # 建 ops profile + 铺样例拓扑/runbook
.venv/bin/argus -p ops                          # 进入 ops profile 会话
```

`ops_init.py` 会写入 `~/.hermes/profiles/ops/` 下的配置、样例拓扑
（`topology.yaml` + `entities/`）与样例 runbook（`runbooks/`），全程幂等。

### 3. 验证（对应 [OPS-VERIFY.md](OPS-VERIFY.md)）

```bash
argus --version        # Argus v0.1.0
argus -p ops chat      # 起会话后核对：TOPO 段注入 / topo_query / topo_update / 权限矩阵 / runbook_load
```

## 开发

```bash
source .venv/bin/activate
scripts/run_tests.sh               # 优先 .venv，其次 venv
```

重点测试套件：

- `tests/tools/test_topo_tools.py` —— 拓扑表数据契约 + `topo_query` / `topo_update`
- `tests/tools/test_ops_permissions.py` / `test_ops_permissions_guard.py` —— 权限矩阵
- `tests/tools/test_runbook_tools.py` —— runbook 加载 / 匹配 / L4 阶段门
- `tests/plugins/memory/test_topo_provider.py` —— TOPO 段注入
- `tests/scripts/test_ops_init.py` —— ops profile 初始化

## 项目结构与品牌说明

- 代码目录沿用 fork 的 `hermes_*` 命名（`hermes_cli/`、`hermes_state.py` 等），
  这是内核兼容性的一部分，不改名。
- 产品外壳已品牌化为 Argus：CLI 入口 `argus`、启动 banner、`--version`、
  help 文本、README 与 ops profile 的 SOUL.md。
- 运维专属改动全部登记在 [`OPS-DELTA.md`](OPS-DELTA.md)，季度体检核销。

## License

MIT。Argus 是 Hermes Agent 的独立 fork，遵守上游 [LICENSE](LICENSE)。

# OPS-VERIFY —— 用户亲自验证步骤（一期：拓扑注入 + topo 工具 + 权限矩阵）

> 对应 ops-agent-harness.md §6.3「每步验证」与 §3 权限矩阵。请**亲自**按顺序操作；
> 全部通过后在会话里告诉 Codex「验证通过」，才开工 runbook（程序层，二期）。

## 0. 前置：初始化 ops profile

```bash
cd /home/wpwang/projects/argus_agent
source .venv/bin/activate
python3 scripts/ops_init.py            # 默认写到 ~/.hermes/profiles/ops
```

输出应包含：创建 profile、写入 config.yaml、写入 topology.yaml、写入 entities/（8 个实体档案）。
想装到别处用 `--root <path>`；初始权限环境默认 `test`（安全默认），可用 `--env prod` 覆盖。

确认 `hermes` 可用（不在 PATH 就用 `./hermes`）：

```bash
hermes --version   # 或：./hermes --version
```

## 1. 起 session，看 TOPO 段

```bash
hermes -p ops chat
```

第一句话：

> TOPO 段里列出了哪些核心实体和关键链路？逐条念给我。

**预期**：agent 报出 8 个核心实体（harbor / k3s-prod / argocd / gateway-svc / order-db /
postgres / ingress / test-web），两个环境（prod=strict / test=relaxed），关键链路
`ingress → gateway-svc → order-db`，并复述行为约束「执行任何运维操作前，先 topo_query
确认目标实体的身份和环境」。

可选硬校验（不依赖 LLM，直接渲染 TOPO 段）：

```bash
HERMES_HOME=$HOME/.hermes/profiles/ops .venv/bin/python -c \
  "import sys; sys.path.insert(0,'.'); from pathlib import Path; \
   from plugins.memory.topo import render_topo_block; \
   print(render_topo_block(Path.home()/'.hermes/profiles/ops'))"
```

输出以 `## TOPO — 平台拓扑总览` 开头、以行为约束结尾。

## 2. topo_query 两条命令

在同一个会话里依次说：

1. **按实体查 + 第二层**
   > 用 topo_query 查一下 harbor，detail=True，告诉我它的版本、存储位置、依赖和健康检查命令。

   **预期**：返回 harbor 档案 JSON——`attrs.version=v2.11`、`attrs.storage=/data/harbor`、
   `depends_on=[postgres]`、`ops.healthcheck=curl .../api/v2.0/health`、`stale=false`。

2. **按类型 + 环境过滤**
   > 用 topo_query 列出 prod 环境所有 db 类型实体。

   **预期**：返回 `order-db` 和 `postgres` 两条。

可再加一条负例：查不存在的实体（如 `xxx`）应报「拓扑表中不存在实体」，而不是瞎编。

## 3. topo_update：test 实体（无审批）+ prod 实体（有审批）

1. **test 实体，自动带 source=agent**
   > 用 topo_update 把 test-web 的 status 改成 degraded，reason 写「验证」。

   **预期**：返回 `source=agent`、`last_verified=今天`；打开
   `~/.hermes/profiles/ops/entities/test-web.yaml` 能看到 `status: degraded` +
   `source: agent` + `last_verified: 2026-08-06`（test 环境不弹审批）。

2. **prod 实体，必须审批**
   > 用 topo_update 把 harbor 的 version 改成 v2.12，reason 写「验证」。

   **预期**：弹出审批确认（PROD 拓扑变更）。选择拒绝后返回「未获审批，已取消」，
   且 `entities/harbor.yaml` 不被改动。

## 4. 权限矩阵：切到 prod 验证（L2 审批 / L3-L4 拒绝）

编辑 `~/.hermes/profiles/ops/config.yaml`，把
`ops.permissions.env` 与 `ops.permissions.role` 改为 `prod`，重新 `hermes -p ops chat`。

然后依次让 agent 执行：

1. **L2 → 审批**
   > 帮我重启 myapp 服务。

   **预期**：agent 执行 `systemctl restart myapp` 时弹出审批提示（L2 / prod）。
   看到提示即可选**拒绝**（myapp 是演示用服务，不必真跑）。

2. **L4 → 硬拒（yolo 也拦不住）**
   > 把 namespace foo 删掉。

   **预期**：agent 执行 `kubectl delete namespace foo` 被硬拒，返回 BLOCKED
   （L4 / prod，权限矩阵拒绝）。再试 `rm -rf /var/log`，同样 BLOCKED（L3 / prod）。

3. **L1 → 放行**
   > 看一下磁盘使用情况。

   **预期**：`df -h` 直接执行，无审批。

验证完把环境切回 `test`（或按真实目标环境保留）：
`ops.permissions.env: test`。

## 5. 通过标准与下一步

- 第 1–4 节全部符合预期 → 告诉 Codex **「验证通过」**，开工 runbook（程序层：
  结构化 runbook 目录 + 按需加载工具 + L4 部署 checklist，ops-agent-harness.md §1/§3）。
- 任何一项不符合 → 把会话输出原样贴给 Codex，先修一期问题再继续。

> 注意：第 4 节的所有命令都只做「判定演示」——审批的选拒绝，被拒的本来就不会执行，
> 不会对集群产生任何真实影响。

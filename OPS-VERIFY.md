# OPS-VERIFY —— 用户亲自验证步骤（一期：拓扑注入 + topo 工具 + 权限矩阵）

> 对应 ops-agent-harness.md §6.3「每步验证」、§3 权限矩阵与 §1/§3 L4 程序层。
> 请**亲自**按顺序操作；一期（拓扑/工具/矩阵）通过后开工了 runbook（§6），
> §6 也通过后即可进入正式使用。

## 0. 前置：初始化 ops profile

```bash
cd /home/your-name/projects/vigil-agent
source .venv/bin/activate
vigil ops-init                        # 默认写到 ~/.vigil/profiles/ops（旧入口：python3 scripts/ops_init.py 同效）
```

输出应包含：创建 profile、写入 config.yaml、写入 topology.yaml（schema v0.2 第一层：
hosts + cross_host）、写入 hosts/（第二层服务索引）、写入 entities/（第三层实体档案）、
写入 runbooks/（3 个 runbook）。
想装到别处用 `--root <path>`；初始权限环境默认 `test`（安全默认），可用 `--env prod` 覆盖。

确认 `vigil` 可用（不在 PATH 就用 `./hermes`）：

```bash
vigil --version   # 或：./hermes --version
```

## 1. 起 session，看 TOPO 段

```bash
vigil -p ops chat
```

第一句话：

> TOPO 段里列出了哪些主机、跨主机实体和关键链路？逐条念给我。

**预期**：agent 报出第一层总览——主机（node1 / node2 / test-host，含 runtime）、
跨主机实体（k3s-prod / ingress），两个环境（prod=strict / test=relaxed），关键链路
`ingress → gateway-svc → order-db`，并复述行为约束「执行任何运维操作前，先 topo_query
确认目标实体的身份和环境」。

> 第二层服务（harbor / gateway-svc / order-db / postgres …）**不在** TOPO 段——
> 总览只注入第一层（token 成本恒定），服务走 topo_query 展开。

可选硬校验（不依赖 LLM，直接渲染 TOPO 段）：

```bash
VIGIL_HOME=$HOME/.vigil/profiles/ops .venv/bin/python -c \
  "import sys; sys.path.insert(0,'.'); from pathlib import Path; \
   from plugins.memory.topo import render_topo_block; \
   print(render_topo_block(Path.home()/'.hermes/profiles/ops'))"
```

输出以 `## TOPO — 平台拓扑总览` 开头、以行为约束结尾。

## 2. topo_query 两条命令

在同一个会话里依次说：

1. **按实体查 + 第三层详情**
   > 用 topo_query 查一下 harbor，detail=True，告诉我它的版本、存储位置、依赖和健康检查命令。

   **预期**：返回 harbor 档案 JSON——`attrs.version=v2.11`、`attrs.storage=/data/harbor`、
   `depends_on=[postgres]`、`ops.healthcheck=curl .../api/v2.0/health`、`stale=false`。

2. **按类型 + 环境过滤**
   > 用 topo_query 列出 prod 环境所有 db 类型实体。

   **预期**：返回 `order-db` 和 `postgres` 两条。

3. **按 host 展开第二层服务索引（v0.2）**
   > 用 topo_query 查一下 host=node1，列出它的服务。

   **预期**：返回 node1 主机行 + `services` 列表（harbor / argocd / order-db / postgres）。

4. **跨层名解析**
   > 用 topo_query 查一下 entity=node1（第一层 host 命中应附带服务列表），再查 entity=harbor。

   **预期**：`entity=node1` 返回 node1 + 其 services 列表；`entity=harbor` 返回
   第二层服务行 + detail 路径。

可再加一条负例：查不存在的实体（如 `xxx`）应报「拓扑表中不存在实体」，而不是瞎编。

## 3. topo_update：test 实体（无审批）+ prod 实体（有审批）

1. **test 实体，自动带 source=agent**
   > 用 topo_update 把 test-web 的 status 改成 degraded，reason 写「验证」。

   **预期**：返回 `source=agent`、`last_verified=今天`；打开
   `~/.vigil/profiles/ops/entities/test-web.yaml` 能看到 `status: degraded` +
   `source: agent` + `last_verified: 2026-08-06`（test 环境不弹审批）。

2. **prod 实体，必须审批**
   > 用 topo_update 把 harbor 的 version 改成 v2.12，reason 写「验证」。

   **预期**：弹出审批确认（PROD 拓扑变更）。选择拒绝后返回「未获审批，已取消」，
   且 `entities/harbor.yaml` 不被改动。

## 4. 权限矩阵：切到 prod 验证（L2 审批 / L3-L4 拒绝）

会话内切换操作环境（OPS-DELTA #11）：

> /env prod

**预期**：返回切换确认 + banner ENV badge 变为 `[prod]`，权限矩阵随之按 prod 判定
（后续命令的 `ops_matrix.env` 为 `prod`）。`/env` 无参可查看当前环境与可用列表
（`ops.environments` 已定义；可用 `vigil ops-init --env <自定义名>` 初始化自定义环境，
如 `bare_metal_prod`）。老方式（手动编辑 `config.yaml` 的 `ops.permissions.env` /
`ops.permissions.role` 后重开 session）仍兼容。

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

验证完把环境切回 `test`（或按真实目标环境保留）：`/env test`。

## 5. 通过标准与下一步

- 第 1–4 节全部符合预期 → 一期验证通过，Codex 已开工 runbook（程序层），继续 §6。
- 任何一项不符合 → 把会话输出原样贴给 Codex，先修一期问题再继续。

> 注意：第 4 节的所有命令都只做「判定演示」——审批的选拒绝，被拒的本来就不会执行，
> 不会对集群产生任何真实影响。

## 6. runbook（程序层）验证

**前置**：初始化脚本已升级（runbook toolset + `ops.runbooks.enabled` + 样例 runbooks）。
已有 ops profile 需要先补上新配置，二选一：

- 没改过样例拓扑：`vigil ops-init --force`（重铺 config/拓扑/runbooks 样例）。
- 改过拓扑/实体（保留你的修改）：手工在 `~/.vigil/profiles/ops/config.yaml` 加两处——
  `platform_toolsets.cli` 改为 `[hermes-cli, topo, runbook]`，并在 `ops:` 下加
  `runbooks: {enabled: true}`（OPS-DELTA #1 起可选：新版本 runbook 工具默认按
  `runbooks/` 数据存在性启用，`enabled` 只是显式声明/关闭开关）；然后
  `vigil ops-init`（不带 --force，会自动铺缺失的 runbooks/）。

重新 `vigil -p ops chat`，依次验证：

1. **列表**：问「列出可用的 runbook」。**预期**：`runbook_load` 返回 3 个——
   `harbor-restart`、`gateway-svc-restart`（事故）+ `deploy-gateway-svc`（L4 部署 checklist）。
2. **按触发关键字加载**：说「harbor 健康检查失败，按 runbook 处理」。**预期**：agent
   加载 `harbor-restart`，按 诊断 → 重启（L2，prod 弹审批，选拒绝即可）→ 真实验证 推进。
3. **L4 部署 checklist 阶段门**：说「按 runbook 发布 gateway-svc」。**预期**：
   - 先跑 `runbook_checkpoint` 记录 deploy 会被告知「前置步骤未全部通过」（阶段门拒绝跳序）；
   - 前置核对通过后按 preflight → deploy（`kubectl set image`，L2 弹审批，选拒绝）→ verify 推进。
   - 发布命令停在审批处即可，**不要真发版**；回滚预案在 runbook 的 rollback 段（`rollout undo`
     属 L3，prod 仅人工）。
4. **跨环境提醒**：当前会话 `ops.permissions.env=test` 时加载 prod runbook，
   **预期**：返回里带 `env_mismatch: true` + 跨环境默认拒绝的提示。

**通过**：以上 4 条符合预期 → 告诉 Codex **「runbook 验证通过」**，进入正式使用；
有不符合就把输出贴回来。

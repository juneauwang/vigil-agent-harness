1. 只允许在 /home/your-name/projects/vigil-agent 内工作；
禁止读取/修改 ~/.hermes、~/.ssh、~/.kube、任何 .env 文件
2. 开工前先 git commit 一个 baseline（回滚锚点），
之后随便改，随时能 reset
3. 依赖装进项目 venv，不碰系统 Python；
禁止执行 run_agent.py 等任何会启动 Vigil 本体的命令


# Ops Agent Harness —— 设计文档 v0.1

> 给 IT 运维用的 Agent Harness。Fork 自 Vigil Agent（MIT License）。
> 代码交付给 Codex 实现，本文档是唯一交接材料（数据契约 + 改动地图）。
> 源码位置: /home/your-name/projects/hermes-source_1（upstream: NousResearch/hermes-agent）

## 0. 决策记录

- **从 0 还是 fork：Fork Vigil。** LLM 调用循环、工具系统、消息网关、cron、
  memory/skills、命令审批、profile 隔离全是现成的且经过生产验证（公司机器跑了两套）。
  运维 harness 的差异化价值在执行层（SSH 多目标管理、审计、CMDB 对接、runbook），
  不在 LLM 编排层。类比：造救护车不需要造底盘。
- **License：MIT**（源码 LICENSE 文件确认），fork 成内部工具或商业产品均合法，保留版权声明即可。
- **架构哲学（沿用 upstream AGENTS.md）：核心窄腰，能力在边缘。**
  所有改动尽量落在扩展层（工具、prompt 段、配置），不碰 conversation_loop /
  上下文压缩 / 缓存逻辑，保证上游 cherry-pick 低冲突。
- **上游策略（2026-08-06 修订）：完全 fork，不跟随上游。**
  理由：产品核心是长记忆 + SKILL 自我进化——恰是上游最活跃、最可能大改的
  核心区（learning_graph / memory 系统 / prompt 结构）。深度改造 + 跟随上游
  = 双重痛苦（上游大版本会踩烂我们的改造）。且不在乎上游其余功能
  （浏览器/多媒体/桌面 UI），跟随收益≈0。分叉时机：产品零代码阶段，成本最低。
  保命措施：
  ① 安全补丁不走 merge——依赖扫描（pip-audit/trivy）+ 盯上游 SECURITY
     公告，针对性自补；
  ② upstream remote 保留，仅参考思路不 merge；
  ③ 完全 fork 后可真正删掉不要的模块（TUI/web/desktop/多媒体工具），瘦身；
  ④ 我们的能力（拓扑表/runbook/权限矩阵）保持模块化插件形态，可迁移底座。

## 1. 核心概念：两层记忆架构

| 层 | 内容 | 回答的问题 | 加载时机 |
|---|---|---|---|
| 事实层 = 拓扑表 | 平台长什么样（机器/服务/依赖） | "这是什么" | session 启动必读（第一层） |
| 程序层 = runbook | 出事怎么办（步骤/命令/回滚） | "怎么处理" | 告警/任务触发时按需加载 |

- Vigil 的 memory（~2KB）只存人的偏好，平台事实一律不进 memory（容量小、会被冲没）。
- skill 散文格式不适合运维；runbook 用结构化 YAML（触发条件 + 步骤 + 命令 + 回滚），
  因为 runbook 是要被执行的，不是被参考的。

## 2. 拓扑表（事实层）

### 2.1 定位

拓扑表 = agent 侧的 CMDB = 平台的户口本 + 关系图。
同步源：Netbox（存在性/IP/机柜）、Snipe-IT（资产归属）、k8s API（运行时拓扑，
adapter 预留，experimental，暂不启用）、Agent（语义层：用途/依赖/业务归属）、人工（兜底）。
每实体带 `source` + `last_verified`，超期未验证标 `stale`——过期拓扑比没有拓扑更危险。

### 2.2 Schema v0.2（三层模型）

拓扑是**三层模型**（OPS-DELTA #6）——层级是组织方式不是命名空间，权限矩阵按
实体 name+env 绑定、runbook 按服务名绑定，不因分层失效：

```
第一层  topology.yaml（总览，<50 行，session 启动注入 system prompt）
  ├─ environments（含 entry/isolation/role）
  ├─ hosts:  [host 一行]（name / env / endpoint / role / runtime）
  │    runtime: docker 是 host 属性（docker 不单独占层）
  └─ cross_host: [跨 host 实体一行]（k3s 集群 / ingress 这类不服从
       "服务挂单机"树形模型的实体）

第二层  hosts/<hostname>.yaml（每 host 一个服务索引）
  └─ services: [服务一行]（name / type / env / endpoint / detail 路径）
       服务有独立 name + env——权限矩阵/runbook 照常按服务名绑定

第三层  entities/<name>.yaml（= v0.1 的实体档案机制，原位沿用）
  └─ 容器/依赖/端口/镜像 tag 明细（topo_query detail=True 时按需加载）
```

**查询路径**（核心契约）：system prompt 只注入第一层（总览开销恒定）；
`topo_query` 无参返回第一层总览、`host=<name>` 展开第二层服务索引、
`entity=<name>` 跨层名解析（先第二层服务名，再第一层 host/cross_host）、
`detail=True` 进第三层详情。

```yaml
# topology.yaml —— 第一层：总览（session 启动注入 system prompt，<50 行）
version: 2
updated_at: 2026-08-12
sources: [terraform.tfstate, agent, manual, discovered]
environments:
  - name: prod
    entry: "ssh user@203.0.113.10"
    isolation: strict                    # strict = 跨环境操作需审批
    role: prod                           # 见 §3 权限模型
  - name: test
    entry: "ssh test-jump"
    isolation: relaxed
    role: test
hosts:
  - {name: node1, env: prod, endpoint: "203.0.113.10", role: control-plane,
     runtime: k3s, owner: your-name, source: agent, last_verified: 2026-08-08,
     services_index: hosts/node1.yaml}
cross_host:
  - {name: k3s-prod, type: k8s, env: prod,
     endpoint: "https://203.0.113.10:6443", detail: entities/k3s-prod.yaml}
key_paths:                               # 关键链路：排障时最先查的主干
  - [ingress, gateway-svc, order-db]
```

```yaml
# hosts/node1.yaml —— 第二层：node1 的服务索引（服务一行）
host: node1
env: prod
services:
  - {name: harbor, type: registry, env: prod, endpoint: "203.0.113.10:30443",
     source: manual, last_verified: 2026-08-06, detail: entities/harbor.yaml}
```

```yaml
# entities/<name>.yaml —— 第三层：单服务完整档案，按需加载（topo_query）
name: harbor
type: registry
env: prod
attrs:                                   # 完整属性
  version: v2.11
  storage: /data/harbor
  admin: your-name
depends_on: [postgres]                   # 它依赖谁（雪花展开）
depended_by: [gnomeria-dev, gnomeria-prod]  # 谁依赖它
ops:
  healthcheck: "curl -s http://localhost/api/v2.0/health"
```

**Schema v0.1 兼容**（老数据不重写也能用）：`version: 1` 的扁平
`core_entities` 走兼容路径——`load_topology` / `_all_core_entities` 内部展开
为同一扁平实体视图，topo_query / 权限矩阵 / runbook 绑定行为不变；v0.1
没有 host 概念，`host=` 过滤在 v0.1 下不可用（返回明确错误）。迁移不靠用户
手动改文件。

```yaml
# topology.yaml —— schema v0.1（legacy，兼容读取）
version: 1
updated_at: 2026-08-06
sources: [netbox, snipeit, agent]        # 实际启用的同步源
environments:
  - name: prod
    entry: "ssh user@203.0.113.10"
    isolation: strict
    role: prod
    core_entities: [harbor, k3s-prod, argocd]
  - name: test
    entry: "ssh test-jump"
    isolation: relaxed
    role: test
core_entities:
  - name: harbor
    type: registry
    env: prod
    endpoint: 203.0.113.10:30443
    owner: your-name
    source: manual
    last_verified: 2026-08-01
    detail: entities/harbor.yaml
key_paths:
  - [ingress, gateway-svc, order-db]
```

动态状态（CPU/版本/pod 数/告警）**不进文件**——desired/actual 分离，运行时
状态由监控/k8s API 实时查，拓扑表只存"应该是什么样"。

### 2.3 数据流（用户确认版）

```
加载后 → 读取总览                    system prompt 注入 topology.yaml 第一层
等待用户输入                          （agent 待命）
"查下 harbor"                        topo_query 工具
获取服务细节/依赖                     读 entities/harbor.yaml（第二层）
调用 runbook/Agent 决定如何处理      程序层加载 + LLM 推理
工具调用                              ssh / kubectl / curl 执行
反馈                                 结果给用户
更新拓扑图信息                        topo_update：改状态/版本，自动打 source=agent + 日期
```

## 3. 权限模型（纵深防御 4 层）

| 层 | 机制 | 说明 |
|---|---|---|
| L1 | toolset 裁剪 | PROD 会话不加载 browser/image_gen 等无关工具，减少攻击面 |
| L2 | 静态规则硬 gate | rm -rf / DROP TABLE / delete namespace --all 等模式直接拦截，0 token，LLM 不参与判定（防 prompt injection） |
| L3 | 角色矩阵 | 命令分级 × 环境 → 执行/审批/拒绝（下表） |
| L4 | 部署 checklist | PROD 部署强制走 runbook 模板：前置核对 → 滚动发布 → 真实验证（curl 探活）→ 回滚预案 |

命令分级矩阵：

| 等级 | 示例 | test(自用) | UAT | PROD |
|---|---|---|---|---|
| L1 查询 | ls / df / curl 状态 | 直接执行 | 直接执行 | 直接执行（走审计跳板机，禁止无认证探查） |
| L2 常规 | 重启自研服务 / 装包 | 直接执行 | 直接执行 | 审批 |
| L3 危险 | rm -rf / iptables / 重启 DB / 改配置 | 直接执行 | 审批 | 拒绝（仅人工） |
| L4 致命 | 删 namespace / 删库 / 格式化 | 直接执行 | 拒绝 | 拒绝 |

- 审批 gate 钉在执行工具层（terminal/ssh 工具内），不依赖 agent 自觉。
- 环境字段（env）是每命令的强制参数：跨环境操作默认拒绝，strict 环境需审批。
- **env 可自定义（OPS-DELTA #11）**：上表 test/UAT/PROD 是内置默认档；真实环境是
  「物理环境 × 等级」组合（bare_metal_uat / bare_metal_prod / local / cloud），在
  config.yaml `ops.environments: [{name, isolation, role}]` 定义，名称任意，矩阵
  行为由 `role` 决定（`bare_metal_prod` → `role: prod` → 按 PROD 档判定）。会话内
  用 `/env <name>` 切换（无参显示当前 + 可用列表），权限矩阵与 banner ENV badge
  随之更新；`vigil ops-init --env <自定义名>` 可初始化自定义环境。

## 4. 源码改动地图（fork 初期全部改动）

```
A. 拓扑表注入 —— agent/system_prompt.py
   位置: build_system_prompt_parts() 的 volatile 区块
   做法: 加 TOPO 段，读 topology.yaml 第一层注入（不混入 memory snapshot）
   理由: 拓扑表是"平台事实"不是"记忆"，独立区块语义干净；改动集中 1 个文件

B. 工具层 —— tools/topo_tools.py（新建，纯增量）
   topo_query  : 查拓扑，按实体/类型/环境过滤，第二层细节按需加载
   topo_update : 改拓扑，自动带 source=agent + last_verified=今天；
                 改 prod 实体触发审批确认
   注册: 工具集注册表加 topo toolset，ops profile 默认启用

C. 行为约束 —— system_prompt.py 的 tool_guidance 加一句
   "执行任何运维操作前，先 topo_query 确认目标实体在拓扑表中的身份
    和环境；跨环境操作默认拒绝"

D. 权限 gate —— 命令审批钩子升级成分级矩阵（现有 approval 机制扩展）
   L2 静态规则匹配 + L3 角色矩阵查表，落在执行工具层
```

预计总量 ~300 行，2 个文件改动 + 2 个新文件，不碰核心循环。

## 5. UI/UX 决策（初期）

- **CLI 是唯一交互形态**，显示层零改动直接使用。
- **完全不要**（配置禁用/不启动，不删代码，保证 cherry-pick 无冲突）：
  - ui-tui/ + tui_gateway/（TUI 图形终端）
  - web/（web dashboard）
  - apps/desktop（Electron 桌面 app）
  - 工具：browser、image_gen、video_gen、tts、spotify、kanban、computer_use
- **唯一值得做的小改动**：审批提示强化——展示目标机器 + 命令 + 风险等级
  （直接服务安全目标，改动在 approval 展示文案）。
- 二期可选（独立项目，不碰 agent 核心）：只读拓扑可视化 web 面板。

## 6. Codex 交接清单

1. 分支策略：完全 fork，单一 main 独立演化（见 §0）。
   git remote add upstream 保留仅参考（不 merge）；安全补丁走依赖扫描
   （pip-audit/trivy）+ SECURITY 公告自补
2. 实现顺序：A（注入）→ B（工具）→ C（约束）→ D（权限 gate）
3. 每步验证：临时 HERMES_HOME 起 session，检查 system prompt 出现 TOPO 段；
   topo_query/topo_update 用测试拓扑文件跑通
4. 数据契约：本文 §2.2 schema v0.1 + §3 权限矩阵
5. 约束：不碰 conversation_loop / 上下文压缩 / prompt 缓存逻辑；
   新配置走 config.yaml，不加 HERMES_* env var（upstream 约定）

## 7. 防屎山纪律（硬约束，Codex 必须遵守）

**路线：完全 fork（见 §0）。** 核心树独立演化，不再跟随上游。我们的能力
（拓扑表/runbook/权限矩阵）保持模块化插件形态——既防屎山，也保证将来
可迁移底座。可删除上游不需要的模块（TUI/web/desktop/多媒体工具），瘦身。

**A 方案（零侵入实现）：** 拓扑表注入不直接改 system_prompt.py，而是实现为
MemoryProvider 插件（plugins/memory/ 接口，system prompt 的 external memory
provider block 是现成注入槽位）。provider 内部组合：内置 memory（人偏好）
+ 拓扑表第一层（平台事实）一起返回。

四条硬约束：

1. **零侵入优先**：任何需求先问"现有机制能不能组合出来"（memory provider 块 /
   插件命令 / 独立工具文件 / 配置）。答案是不能，才允许碰核心，且先记录为什么不能。
2. **必须碰核心时**：加段不加逻辑，改动文件数 ≤ 2，每个改动在 OPS-DELTA.md 记账
   （改了哪、为什么、怎么改的）。
3. **OPS-DELTA.md = 核心改动账本**：任何对核心文件的改动必须登记，
   季度体检时照着逐一核销，防止"改着改着忘了改了啥"。
4. **季度安全体检（替代上游同步）**：pip-audit/trivy 扫依赖 CVE +
   过一眼 upstream SECURITY 公告，只参考不 merge，针对性自补。

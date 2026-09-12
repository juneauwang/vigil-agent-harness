# Vigil ☉ —— 运维 Agent Harness

![Vigil](assets/banner.png)

> **省排查时间，决策交给人。**
> AI 做认知劳动（发现 / 定位 / 方案 / 证据打包），人做决策劳动（批准 / 否决 / 改方案）。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![PyPI](https://img.shields.io/pypi/v/vigil-agent-harness)

Vigil 是一个**面向运维场景的 AI agent harness**：让 agent 在真实服务器环境里干活时，
既「记得住整个平台」，又「动得安全」。Vigil 完全 fork 自
[Hermes Agent](https://github.com/NousResearch/hermes-agent)（MIT License），
保留其成熟的 agent 内核（终端、工具调用、会话、记忆、插件），独立演进出面向
生产运维的完整能力：拓扑事实层、runbook 程序层、权限矩阵、受控执行器、契约编译。

---

## 目录

- [为什么需要 Vigil](#为什么需要-vigil)
- [核心特性](#核心特性)
- [和「人工 CMDB + RAG 文档」有什么不同](#和人工-cmdb--rag-文档有什么不同)
- [工作原理](#工作原理)
- [快速开始](#快速开始)
- [Web 控制台](#web-控制台)
- [一次会话长什么样](#一次会话长什么样)
- [安全模型](#安全模型)
- [常见问题](#常见问题)
- [Roadmap](#roadmap)
- [License](#license)

---

## 为什么需要 Vigil

很多小团队**没有专职运维**：服务器是开发顺手管的，CMDB 不存在，故障手册不存在，
半夜出事靠回忆。Vigil 给这类团队三样最缺的东西：

- **记忆** —— 平台的户口本和关系图（拓扑表），agent 不会「换了会话就失忆」
- **经验** —— 出过的故障沉淀成可执行的 runbook，下次不再从头查
- **护栏** —— 权限矩阵硬性拦截危险命令，手滑删生产这件事从「靠自觉」变成「靠机制」

如果你有专职 SRE、完整 CMDB 和工单系统——Vigil 对你可能是锦上添花；
如果你一个人扛着几台服务器——Vigil 就是给你做的。

**Vigil 的边界（2026-08-24 定）**：AI 自动做认知劳动，但决策交给人——
有 runbook 的例行操作走预审 SOP（批准后执行），没有 runbook 的异常情况通知人介入。
Vigil 不做全自动修复——AI 代替运维决策还不是现在的正确形态。

## 核心特性

| 层 | 载体 | 职责 |
|---|---|---|
| **事实层** | 平台拓扑表（`topo_query` / `topo_update` / `topo-discover`） | 长记忆地记住平台：实体、环境、依赖、归属。`topo-discover` 自动发现（SSH 扫 docker/k8s/helm/端口 + 硬件探针）生成，任何运维动作前先确认目标身份与环境；跨环境操作默认拒绝 |
| **程序层** | runbook（`runbook_load` / `runbook_create` / 执行器） | 唯一允许的「怎么动」：事故处理按 runbook 匹配流程执行；部署按 checklist 阶段门推进；跑通的处置流程可沉淀为结构化 YAML，越用越厚。支持嵌套编排（runbook 调 runbook）、范围继承、回滚联动、变量显式传值 |
| **受控执行器** | 23 动作命令生成 + 脚本资产库 | 执行引擎统一生成命令（restart/backup/rollback/scale…），变量替换 + target 解析 + 矩阵审批门 + expect 声明式验证 + on_failure 回滚。长任务 SSE 进度流主动播报，同名/同目标并发锁防重复执行 |
| **权限矩阵** | 操作矩阵（`matrix.yaml` + `vigil matrix`） | 动作 × 环境 → **执行 / 审批 / 拒绝** 三态裁决，无 deny（deny 诱发 LLM 绕行）。矩阵是唯一裁决者，覆盖 CLI/Web/定时/LLM 全部路径；高危动作覆盖缺口有仪表盘 |
| **契约编译** | YAML 契约 → LLM 生成薄包装（`vigil contract compile`） | 给 LLM 加工具不写代码：写一份 YAML 契约（参数/返回/测试），编译管线在沙箱里自测、过内容审批后注册成工具。凭据纪律：契约参数拒绝密码/密钥类参数 |
| **审计层** | 运行轨迹（`vigil trajectory` / Web 审计页） | append-only 事件级日志：谁在什么时间执行了什么命令、结果如何；可查询、可回放、可裁剪——运维审计合规的底账 |
| **监控** | 健康探测 + PromQL 查询 + 告警快照 + 告警人工处置 + 告警→runbook 处置建议（Web 监控页 / 告警页） | 拓扑实体健康探测（并发 + 缓存，不依赖 Prometheus 也能用），PromQL 兼容查询 + Alertmanager 告警汇总，连接配置可在 UI 里改了直接测；告警快照可逐条**确认 / 清除**（状态落本地库，不写回快照——采集层再报到也不会复活）；活跃告警自动匹配处置 SOP——建议给人，人批准后走受控执行链 |
| **用量透明** | token 用量面板 + 价格三级来源 | 当前会话/今天/30 天 token 与费用实时可见，价格手动覆盖 > 在线拉取 > 内置兜底，零网络依赖底线 |

## 和「人工 CMDB + RAG 文档」有什么不同

传统做法（包括不少 AI 运维产品）是：**让 AI 读文档（RAG）来理解平台**。
Vigil 的路线相反：**让 AI 填表单来沉淀平台事实**。

| | Vigil（表单哲学） | 人工 CMDB + RAG |
|---|---|---|
| 平台事实怎么进系统 | agent 填表单，代码生成 YAML；LLM 永不直接写 YAML 语法 | 人手动录 CMDB；AI 靠读文档猜 |
| 沉淀成本 | 跑通一次 = 表单里点几下，成本≈0 | 写文档/录条目，成本高，容易欠账 |
| 事实可信度 | 拓扑表 + 校验器兜底（「默认 LLM 是傻子」），结构化可校验 | 文档靠人维护，过期了 AI 也不知道 |
| 未知判断 | 结构化表单约束 → 校验器拒绝，不会瞎编 | RAG 召回不到就自由发挥 |

核心差异一句话：**Vigil 信结构（可校验的执行规格），RAG 路线信模型（prompt 引导不可校验）。**

## 工作原理

```text
               ┌─────────────────────────────────────┐
               │        Vigil 会话（每个 session）      │
               │                                     │
  启动时注入 ───▶  TOPO 段（拓扑表第一层）              │
               │        │                           │
  用户指令 ────▶  topo_query 确认目标身份/环境 ──▶ 权限矩阵
               │        │                    (动作×env 裁决)
               │   runbook_load 匹配处置流程          │
               │        │                    execute / approve / deny
               │   执行器（23 动作 / 嵌套 runbook）   │
               │        │                           │
  执行后 ─────▶  topo_update 更新事实 + 审计戳        │
               │        │                           │
               │   经验沉淀：runbook / 契约自动成长    │
               └─────────────────────────────────────┘
```

两条铁律贯穿始终：

- **先确认，再动手**：任何运维操作前先查拓扑确认目标；命令的目标是谁，就用谁的
  环境来裁决（test 会话操作 prod 节点？矩阵直接拦）
- **默认 fail-closed**：不确定就拒绝。权限矩阵默认启用；矩阵未初始化时非 prod
  惰性放行、prod 仍门控——宁可拦错，不可放过

### 基于使用历史的权限自进化（v0.1，命令级）

操作矩阵之外，Vigil 会把**你反复批准的只读命令**沉淀成命令级白名单（原创机制：
不是 skills 式的任务经验，而是命令级权限自学习）——同一命令被批准 3 次后，下次
同形态直接执行不再弹审批；从"枚举规则补不完"转向"规则表兜底 + 使用历史补漏"。
管理入口：`vigil approvals list`（全量查看，种子与自进化分开标注）/ `forget
<模板>`（撤销，回到弹审批）/ `export`（一键导出备份）；数据文件
`~/.vigil/approval_memory.yaml`（默认不存在 = 空白名单 + 内置只读种子表兜底）。

三条红线（焊死，不放松）：

- **只看用户信号，不看执行成功**：沉淀只来自"用户批准"事件。执行成功不算数；
  被用户**拒过一次的命令 → banned，永不自动沉淀**（解禁只能 `forget` 显式操作）
- **沉淀模板不是原始串**：`lscpu -e` / `sudo lscpu` 归一为 `lscpu`，`cat
  /etc/hosts` 归一为 `cat`；带动态参数（URL/IP/值 flag/操作数/管道/链式）与
  `rm`/`dd`/`mkfs`/重定向/`tee`/`shred`/`-rf` 形态**永不进白名单**
- **可查看 / 可撤销 / 可审计**：每条沉淀记录模板、沉淀时间与来源任务，一条命令
  撤销，沉淀/撤销/导出全部记轨迹

安全边界：只作用于矩阵 `approve`/`unknown→approve` 档——`{approve: required}`
强制人工门、prod 变更硬门、矩阵缺失 deny、高危目标解析 deny **永不被白名单跳过**；
tirith/危险命令等无条件层每条命令执行前照跑。内置只读种子表（lscpu/free/cat/ls/
df/ps/... 共 18 个，冷启动起点，默认 active）顺带修复了"裸只读命令在 prod 弹
审批"的漏配；`lsblk` 这类未登记只读命令仍先弹审批、批准 3 次后自进化。v0.1 的
"以后不用问"opt-in 在 CLI 审批面提供（web/gateway 审批不喂自进化信号，保守）。

### 告警处置闭环（batch87）：告警 → SOP 建议 → 人确认执行

活跃告警进来自动匹配最可能的 runbook（Alertmanager 触发词精确命中 > 模糊评分，
复用 runbook_load 同款评分器）：返回处置建议（runbook/置信度/匹配依据/次优 SOP），
每条标注 `matched_by`（trigger/fuzzy）与命中关键词——透明，防误导。定位是**建议
闭环，不是全自动修复**——AI 做发现/定位/建议，人做批准/否决：执行永远走既有
runbook 执行链（操作矩阵逐动作裁决 + 审批门 + 审计），匹配器零直通通道。

入口：`alert_triage`（agent 工具，一次调用拿"活跃告警全景 + 建议"，描述里写明
"仅供建议、执行需用户确认后调 runbook_execute"）；Web 监控页告警行「建议处置」
（有匹配 → runbook 名 + 置信徽标 + 查看/确认执行；无匹配 → 灰字提示）；CLI
`vigil alerts`（列表，`--json` 机器可读）。每次 triage 调用落一条轻量审计
`runtime/alert_triage.jsonl`（`vigil alerts history` 回看）——dogfood 调触发词
与匹配阈值的数据来源。runbook 的 `triggers` 字段（含 v0.2 结构化
`{alertname, severity}` 精确匹配）是命中依据；本闭环只做匹配建议、不改变既有
执行/裁决语义（矩阵/审批门/高危门照旧）。

## 快速开始

**30 秒安装（Linux / macOS，自动建 venv + 预装 tirith 安全扫描器）：**

```bash
curl -fsSL https://raw.githubusercontent.com/juneauwang/vigil-agent-harness/v1.0/install.sh | bash
```

> 国内网络若 GitHub 下载 tirith 失败：脚本会提示，Vigil 本体照常可用（首次扫描自动重试补装），或开 VPN 后重跑脚本（幂等）。

**或手动装（已有 Python 3.11+ 环境）：**

```bash
pip install vigil-agent-harness
```

**前置**：Python 3.11+（3.12 推荐）

```bash
# 1. 安装（生成 vigil 命令入口）
pip install vigil-agent-harness

# 2. 直接进入 Vigil——ops 能力默认加载（topology / runbooks / 权限矩阵），
#    全新安装的 default profile 自带 ops 配置与样例拓扑，无需任何初始化
vigil

# 3. 配置模型（如 DeepSeek）——在 ~/.vigil/config.yaml 添加 model 段，
#    并在同目录 .env 放 API key
```

> 如需手动重建样例 profile，可用 `vigil ops-init`（可选——老用户/自托管
> 显式重建或迁移仍可用，首装已不需要）。

首次进入后，让 Vigil 摸清你的平台：**自动发现拓扑**——把每台机器的 IP 和
SSH 凭据告诉它，它会 SSH 进去扫 docker / k8s / systemd 服务 / 端口，生成实体
清单供你确认后落盘：

```bash
vigil topo-discover          # 引导式：填 IP + 凭据 → 扫描 → 人工确认 → 写入 topology.yaml
```

也可以手动编辑拓扑表（`topology.yaml` + `entities/`，高级用法）。然后让它干
第一件真活：**「检查拓扑里哪些实体 last_verified 过期了」**。

## Web 控制台

v0.1.15 起带 Web 控制台（`vigil dashboard`，默认 http://127.0.0.1:9119）：

- **对话页**（`/chat`）：浏览器里和 Vigil 对话，流式回复、工具调用折叠展示、
  多会话并行互不干扰；输入框多行自适应（Enter 发送 / Shift+Enter 换行，中文输入法
  组词中不误发）；token 用量面板实时显示当前会话/累计费用
- **拓扑图**（`/topology`）：可交互拓扑图（缩放/平移/点击节点看详情抽屉），
  状态着色 + 活性显示；力导向布局
- **监控页**（`/monitoring`）：服务健康表格 + 告警列表 + 趋势 sparkline，
  监控连接配置可直接在页面上改并测试连通性
- **告警页**（`/incidents`）：告警快照列表（按 `alertname|instance` 去重保留最新），
  逐条**确认 / 清除**（清除两步确认）；处置状态落本地库，清掉的告警不会因为采集层
  再次上报而复活，只有恢复后复发（`startsAt` 变化）才重新提醒
- **审批全局弹窗**：agent 请求审批时任何页面弹出决策框（批准/拒绝/忽略），
  顶栏铃铛角标实时；Web 上批准 = 与 CLI 同一个审批流，prod 变更确认门照样拦
- **Runbook 页**：执行入口 + 实时进度面板（SSE 流式）+ 历史记录 + 并发锁标识 +
  高危动作覆盖率区块
- 其余页面：状态 / 审计 / 矩阵等可视化

```bash
vigil dashboard        # 启动控制台，浏览器打开 http://127.0.0.1:9119
```

### 从源码安装（开发者 / 自托管）

想改代码、跑测试、自定义 fork 时用这条路。一键脚本 `setup-vigil.sh`
（clone → 装依赖 → 初始化）：

```bash
# 方式 A：已 clone 仓库，在仓库根目录直接跑
git clone https://github.com/juneauwang/vigil-agent-harness.git
cd vigil-agent-harness
./setup-vigil.sh

# 方式 B：未 clone，远程一键（自动 clone 到 ~/.vigil/vigil-agent-harness）
bash <(curl -fsSL https://raw.githubusercontent.com/juneauwang/vigil-agent-harness/main/setup-vigil.sh)
```

脚本会创建 `.venv`、安装依赖、把 `vigil` 软链到 `~/.local/bin`，并询问是否
执行 `vigil ops-init`。也可以手动装：

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/vigil ops-init          # 或 .venv/bin/python scripts/ops_init.py（同入口）
.venv/bin/vigil -p ops
```

**Windows 用户**：原生 Windows 支持安装脚本 `scripts/install.ps1`（PowerShell
一键装依赖并把 `vigil` 加入 PATH）。WSL2 内可直接用上面的 Linux 路径。

## 一次会话长什么样

启动 `vigil -p ops` 后，控制台头部长这样（真实输出）：

```text
╭──────────────────── Vigil v1.0.0 (2026.8.24) ────────────────╮
│                PROFILE     ops                                │
│       /\_/\    ENV         [test]                             │
│      ( ◉.◉ )   GATES       matrix ON · 动作×env               │
│       > ^ <                → execute / approve / deny         │
│                TOPOLOGY    20 entities                        │
│                RUNBOOKS    6 loaded                           │
│ deepseek-v4-flash          HOME  ~/.vigil/profiles/ops        │
│ /home/your-name                                              │
│ Session: 20260824_171603   ◈ topo_query · runbook_load        │
│                                · permission matrix · /help    │
╰───────────────────────────────────────────────────────────────╯

Welcome to Vigil — topology loaded, runbooks ready, permission gates armed.

你：帮我排查 node2 的 sshd 为什么连不上
Vigil：先确认目标身份 → topo_query node2 → runbook_load 匹配
       "ssh-idle-hang" → 诊断（sshd -T / 保活配置）→ 修复 → 验证
```

左边是模型/工作目录/会话锚点，右边是运维能力实时状态——在哪个环境、
权限门开没开、记住了多少实体、有哪些 runbook，一眼可见。

## 安全模型

| 层 | 机制 | 说明 |
|---|---|---|
| L1 | toolset 裁剪 | 会话按角色加载最小工具集，减少攻击面 |
| L2 | 静态规则硬 gate | 危险命令模式（rm -rf / drop table / delete namespace）直接拦截，0 token |
| L3 | 目标级权限矩阵 | 动作目标（拓扑实体）的 env 决定裁决：prod 的高危动作硬拒/审批；矩阵无 deny，未知动作默认放行但记审计（deny 诱发 LLM 绕行） |
| L4 | 部署 checklist | 生产部署强制走 runbook 阶段门：前置核对 → 发布 → 真实验证 → 回滚预案 |
| 凭据纪律 | 凭据注入链 | 密码/密钥只走拓扑 credentials + askpass 注入，不进命令串/argv/日志；契约参数拒绝凭据类参数 |

## 常见问题

**国内镜像装不到？**
新发布的包，国内镜像（阿里云/清华/中科大）同步有延迟（几小时到一天），装不到先换 pypi.org 直连：

```bash
pip install vigil-agent-harness -i https://pypi.org/simple/
# 或：主包走 pypi.org，依赖走国内镜像（更快）
pip install vigil-agent-harness \
  --index-url https://pypi.org/simple/ \
  --extra-index-url https://mirrors.aliyun.com/pypi/simple/
```

**报 "from versions: none"？**
Vigil 要求 **Python >= 3.11 且 < 3.14**。Python 版本过低时，pip 会报
`Could not find a version that satisfies the requirement ... (from versions: none)`——
这是版本门槛，不是网络问题。先查 `python3 --version`，装 Python 3.11+ 再试。
推荐用 uv 一步到位：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.12 .venv && source .venv/bin/activate
pip install vigil-agent-harness -i https://pypi.org/simple/
```

**数据目录**
Vigil 的数据目录为 `~/.vigil`（可用 `VIGIL_HOME` 覆盖）。

## Roadmap

- [x] 拓扑表（事实层）+ topo_query / topo_update / topo-discover（v0.4 分层：clusters/services/hardware/entities + 词表层）
- [x] runbook 程序层 + 嵌套编排（第 24 动作：runbook 调 runbook，范围继承/回滚联动/环状防护）
- [x] 操作矩阵（动作×环境三态裁决，无 deny）+ `vigil matrix` CLI/UI
- [x] 受控执行器（23 动作命令生成 + 脚本资产库 + 调度器 + 并发锁 + 进度流）
- [x] YAPL 契约编译（`vigil contract compile`：YAML 契约 → LLM 生成薄包装 → 沙箱自测 → 审批注册）
- [x] 监控页（健康探测 / PromQL / 告警）
- [x] 告警人工处置（告警页逐条确认 / 清除，状态落本地库，采集层零改动）
- [x] token 用量面板（价格三级来源，零网络兜底）
- [x] Web 控制台（对话/拓扑/监控/runbook/审批/审计）
- [x] 运行轨迹审计（append-only 事件日志 + 查询/回放/裁剪）
- [x] 反馈闭环（`runbook_create`：跑通的任务沉淀为结构化 runbook）
- [x] 同步 adapter（terraform.tfstate / k8s API——`vigil topo-sync` + SSH kubectl 发现）
- [x] 周期性拓扑发现（topo-sync 漂移报告 + cron 周期重扫，opt-in 默认关）

## License

MIT。Vigil 是 [Hermes Agent](https://github.com/juneauwang/vigil-agent-harness) 的独立 fork，版权声明见 [LICENSE](LICENSE)。

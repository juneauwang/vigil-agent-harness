# Vigil ☉ —— 运维 Agent Harness

![Vigil](assets/banner.png)

> **记住整个平台，安全地动生产。**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)

Vigil 是一个**面向运维场景的 AI agent harness**：让 agent 在真实服务器环境里干活时，
既「记得住整个平台」，又「动得安全」。Vigil 完全 fork 自
[Hermes Agent](https://hermes-agent.nousresearch.com/)（MIT License），
保留其成熟的 agent 内核（终端、工具调用、会话、记忆、插件），独立演进出面向
生产运维的三层核心能力。

---

## 目录

- [为什么需要 Vigil](#为什么需要-vigil)
- [核心特性](#核心特性)
- [工作原理](#工作原理)
- [快速开始](#快速开始)
- [一次会话长什么样](#一次会话长什么样)
- [安全模型](#安全模型)
- [Roadmap](#roadmap)
- [License](#license)

---

## 为什么需要 Vigil

很多小团队**没有专职运维**：服务器是开发顺手管的，CMDB 不存在，故障手册不存在，
半夜出事靠回忆。Vigil 给这类团队三样最缺的东西：

- **记忆** —— 平台的户口本和关系图（拓扑表），agent 不会"换了会话就失忆"
- **经验** —— 出过的故障沉淀成可执行的 runbook，下次不再从头查
- **护栏** —— 权限矩阵硬性拦截危险命令，手滑删生产这件事从"靠自觉"变成"靠机制"

如果你有专职 SRE、完整 CMDB 和工单系统——Vigil 对你可能是锦上添花；
如果你一个人扛着几台服务器——Vigil 就是给你做的。

## 核心特性

| 层 | 载体 | 职责 |
|---|---|---|
| **事实层** | 平台拓扑表（`topo_query` / `topo_update`） | 长记忆地记住平台：实体、环境、依赖、归属。任何运维动作前先确认目标身份与环境；跨环境操作默认拒绝 |
| **程序层** | runbook（`runbook_load` / `runbook_checkpoint`） | 唯一允许的「怎么动」：事故处理按 runbook 匹配流程执行；部署按 checklist 阶段门推进，不允许跳过 |
| **纵深防御** | 权限矩阵（命令分级 L1–L4 × 环境 test/uat/prod） | 命令 → **执行 / 审批 / 拒绝** 三态裁决。矩阵 DENY 不可被 yolo、mode=off 或 allowlist 绕过；approve 必须有真人在场 |

额外两个让它"越用越强"的机制：

- **自进化**：每次真实排障会沉淀成 runbook / skill——Vigil 用一天，强一点
- **会话记忆**：平台事实（拓扑）与人的偏好（memory）分离存储，互不挤占

## 工作原理

```text
               ┌─────────────────────────────────────┐
               │        Vigil 会话（每个 session）      │
               │                                     │
  启动时注入 ───▶  TOPO 段（拓扑表第一层）              │
               │        │                           │
  用户指令 ────▶  topo_query 确认目标身份/环境 ──▶ 权限矩阵
               │        │                    (目标级 env 判定)
               │   runbook_load 匹配处置流程          │
               │        │                    execute / approve / deny
               │   工具执行（ssh / kubectl / docker）│
               │        │                           │
  执行后 ─────▶  topo_update 更新事实 + 审计戳        │
               │        │                           │
               │   经验沉淀：runbook / skill 自动成长  │
               └─────────────────────────────────────┘
```

两条铁律贯穿始终：

- **先确认，再动手**：任何运维操作前先查拓扑确认目标；命令的目标是谁，就用谁的
  环境来裁决（test 会话操作 prod 节点？矩阵直接拦）
- **默认 fail-closed**：不确定就拒绝。权限矩阵默认 `env: test`，核对通过后再切 prod

## 快速开始

**前置**：Python 3.11+、git

```bash
# 1. 安装（生成 vigil 命令入口）
pip install vigil-agent-harness

# 2. 初始化 ops profile（拓扑表 + 样例 runbook）
vigil ops-init         # 生成 ~/.hermes/profiles/ops 下的配置 + 样例拓扑 + 样例 runbook

# 3. 配置模型（如 DeepSeek）——在 ~/.hermes/profiles/ops/config.yaml
#    添加 model 段，并在同目录 .env 放 API key

# 4. 进入 Vigil
vigil -p ops
```

首次进入后，把拓扑表改成你自己的平台（`topology.yaml` + `entities/`），
然后让它干第一件真活：**"检查拓扑里哪些实体 last_verified 过期了"**。

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

## 一次会话长什么样

启动 `vigil -p ops` 后,控制台头部长这样(真实输出):

```text
╭──────────────────── Vigil v0.1.0 (2026.8.8) ────────────────────╮
│                PROFILE     ops                                   │
│       /\_/\    ENV         [test]                                │
│      ( ◉.◉ )   GATES       matrix ON · L1–L4 × env               │
│       > ^ <                → execute / approve / deny            │
│                TOPOLOGY    20 entities                           │
│                RUNBOOKS    6 loaded                              │
│ deepseek-v4-flash          HOME  ~/.hermes/profiles/ops          │
│ /home/wpwang                                                     │
│ Session: 20260809_171603   ◈ topo_query · runbook_load           │
│                                · permission matrix · /help       │
╰──────────────────────────────────────────────────────────────────╯

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
| L3 | 目标级权限矩阵 | 命令目标（拓扑实体）的 env 决定裁决：prod 的 L3/L4 硬拒、L2 审批 |
| L4 | 部署 checklist | 生产部署强制走 runbook 阶段门：前置核对 → 发布 → 真实验证 → 回滚预案 |

## Roadmap

- [x] 拓扑表（事实层）+ topo_query / topo_update
- [x] runbook 程序层 + L4 部署阶段门
- [x] 目标级权限矩阵（跨环境硬约束）
- [x] 品牌化（Vigil 入口 / banner / 皮肤）
- [x] PyPI 分发（pip install vigil-agent-harness）
- [x] 一键安装脚本 `setup-vigil.sh`（源码安装/自托管路径：clone → 装依赖 → 初始化）
- [ ] 同步 adapter（terraform.tfstate / k8s API）
- [ ] 拓扑体检 cron（自动检查实体 freshness）
- [ ] 数据目录独立（脱离 hermes profile 体系）

## License

MIT。Vigil 是 [Hermes Agent](https://hermes-agent.nousresearch.com/) 的独立 fork，版权声明见 [LICENSE](LICENSE)。

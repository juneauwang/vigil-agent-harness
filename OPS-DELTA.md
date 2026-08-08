# OPS-DELTA —— 核心改动账本

> ops-agent-harness.md §7.3：任何对核心文件的改动必须在此登记（改了哪、为什么、
> 怎么改的），季度体检时逐一核销。零侵入能力（新增文件）不在此账本，见文件自身。

## 路线与原则

- 完全 fork（§0），不 merge upstream；安全补丁走依赖扫描 + SECURITY 公告自补。
- 零侵入优先（§7.1）：先用现有机制组合（memory provider 块 / 插件 / 独立工具 /
  配置），不能才碰核心；碰核心时"加段不加逻辑"，本账本登记。

## 核心改动登记

### 1. `toolsets.py` — 新增 `topo` toolset 定义（纯数据，加段）

- **为什么**：ops-agent-harness.md §4 B 明确要求"工具集注册表加 topo toolset，
  ops profile 默认启用"。`topo_query`/`topo_update` 通过 registry 注册进
  `topo` toolset；静态 TOOLSETS 条目让 `validate_toolset("topo")`、
  help/autocomplete 等派生面都认识它。
- **怎么改**：在 `TOOLSETS` 字典中 `kanban` 之前插入一个 `"topo"` 条目
  （description + tools 列表 + includes），无任何逻辑改动。

### 2. `tools/approval.py` — `check_all_command_guards` 增加权限矩阵钩子（加段）

- **为什么**：ops-agent-harness.md §3/§4 D —— 命令分级矩阵（L1-L4 ×
  test/uat/prod → 执行/审批/拒绝）必须钉在执行工具层，不依赖 agent 自觉；
  矩阵 DENY 不能被 yolo / mode=off / 永久 allowlist 绕过。
- **怎么改**（两处插入，均不改动既有分支行为）：
  1. 在 user-deny 规则之后、yolo bypass 之前：调用
     `tools.ops_permissions.check_ops_command_permission(command)`。
     - `deny` → 直接返回硬阻断结果（带 `ops_matrix` 字段），先于一切 bypass。
     - `approve` 且无人在场（非 CLI/gateway/ask）→ fail-closed 阻断。
     - `approve` 且有人在场 → 记入 `ops_decision`，走既有审批流。
  2. Phase 2 warnings 收集处：`ops_decision["action"] == "approve"` 时追加一条
     `ops_matrix:<grade>:<env>` 警告，复用 smart-approval / gateway / CLI
     审批 / session/permanent allowlist 全套机制。
- **核销方式**：`ops.permissions.enabled` 为 false（默认）时钩子零影响；
  季度体检检查两处插入点的行为是否仍与上游意图一致。

### 3. `toolsets.py` — 新增 `runbook` toolset 定义（纯数据，加段）

- **为什么**：ops-agent-harness.md §1/§3 L4 —— 程序层 runbook 按需加载
  （runbook_load）+ L4 部署 checklist 阶段门（runbook_checkpoint）需要注册为
  一个独立 toolset，ops profile 默认启用（config.yaml 的 `platform_toolsets.cli`
  含 `runbook`）。
- **怎么改**：在 `TOOLSETS` 字典 `topo` 之后、`kanban` 之前插入一个 `"runbook"`
  条目（description + tools 列表 + includes），无任何逻辑改动。
- **核销方式**：`ops.runbooks.enabled` 为 false（默认）时工具零影响（check_fn 门控）。


## 产品外壳品牌化（Argus 一期，2026-08-08）

- **为什么**：产品是 Argus（运维 agent harness），但外壳还是 Hermes（CLI 入口 /
  banner / 版本号 / README）。本次只动产品外壳表面层（入口、banner、help、README、
  SOUL 措辞），**零逻辑改动**：topo/runbook/权限矩阵代码未动，`hermes -p ops`
  行为不变。
- **怎么改**（逐条）：

| 文件 | 改了哪 | 为什么 / 怎么改 |
|---|---|---|
| `pyproject.toml` | `version 0.20.0→0.1.0`、`description` 改为 Argus 定位；`[project.scripts]` 增加 `argus = hermes_cli.main:main` | 产品一期完成（拓扑+runbook+权限矩阵），版本号归 Argus 所有。发行包名**沿用** `hermes-agent`（fork 兼容：importlib.metadata、dist-info、release 脚本均按此名解析；产品名是 Argus）。`hermes` 入口保留，行为不变 |
| `hermes_cli/__init__.py` | `__version__ = "0.1.0"`、`__release_date__ = "2026.8.8"`、模块 docstring 改 Argus | 运行时版本单一来源；CLI/gateway/dashboard/ACP 的版本串都从这里取 |
| `hermes_cli/_startup_fast.py` | 快速 `--version` 输出 `Hermes Agent v…` → `Argus v…`，提示命令 `hermes version` → `argus version` | `argus --version` 必须显示 Argus（快速路径先于重导入执行，是首屏） |
| `hermes_cli/banner.py` | `HERMES_AGENT_LOGO`（ASCII 大图）→ `ARGUS_LOGO`（简洁文字：名字 + 「记住整个平台，安全地动生产」）；`HERMES_CADUCEUS` → `ARGUS_HERO`（简洁标识）；`format_banner_version_label()` 版本标签 `Hermes Agent v…` → `Argus v…` | 任务硬约束：banner 换 Argus 标识，风格简洁，不要花哨 ASCII 大图 |
| `cli.py` | 紧凑 banner 文案（`NOUS HERMES` / `Hermes Agent` / `Nous Research`）→ Argus + 运维定位；欢迎语 → 中文「欢迎使用 Argus——记住整个平台，安全地动生产」；class/module docstring；dead 的 ASCII 常量换成 Argus 简洁标识 | 交互 CLI 首屏与欢迎语品牌化；纯字符串，无逻辑改动 |
| `hermes_cli/skin_engine.py` | 默认皮肤 branding：`agent_name: Hermes Agent`→`Argus`，`welcome`→中文欢迎语，`response_label` 去 Hermes | 皮肤引擎的默认品牌串是 banner/状态栏的回退来源 |
| `hermes_cli/_parser.py` | `prog="hermes"`→`"argus"`；顶层 description 改为 Argus 运维定位；epilogue 示例命令 `hermes`→`argus`；chat 子命令 description | `argus --help` 显示 Argus；`hermes --help` 同样显示 argus（同一产品） |
| `hermes_cli/main.py` | 模块 docstring 用法示例 `hermes`→`argus`；`cmd_uninstall`/`cmd_update`/ACP docstring 与 WhatsApp 引导 Tip 改 Argus | 入口文档与命令名一致 |
| `hermes_cli/config.py` | `recommended_update_command()` 的 git 安装回退 `hermes update`→`argus update` | `argus version` 的更新提示指向正确命令 |
| `hermes_cli/commands.py` | `/update`、`/version` CommandDef 描述 `Hermes Agent`→`Argus` | help/命令描述品牌化 |
| `hermes_cli/profiles.py` | `create_wrapper_script()` 生成的 profile 包装命令从 `hermes -p` → `argus -p`（优先解析 `argus`，回退 `hermes`）；`build_alias_map()` / `remove_wrapper_script()` 的 wrapper 识别针同步兼容 `argus -p ` 与 `hermes -p ` | 包装命令不再依赖 hermes 命令名；反向识别（alias→profile）与删除校验跟上新命令名 |
| `hermes_cli/default_soul.py` | `DEFAULT_SOUL_MD` 改为 Argus 视角 persona | 新 profile 的 SOUL 从出生就是 Argus；不影响 `_LEGACY_TEMPLATE_SOULS` 旧模板迁移匹配 |
| `acp_adapter/server.py` | ACP `version` 能力返回 `Hermes Agent v…`→`Argus v…` | 编辑器集成侧的版本串 |
| `gateway/slash_commands.py`、`gateway/platforms/api_server.py` | /version、/update 相关文案与 docstring 改 Argus | 网关侧命令文案（/version 正文走 banner 版本标签，自动变 Argus） |
| `hermes_cli/uninstall.py` | 卸载器标题与致谢文案 `Hermes Agent`→`Argus`（shell rc 的 `# Hermes Agent` 注释匹配保持不动） | 卸载界面品牌化；注释匹配是清理逻辑的一部分，不能动 |
| `scripts/ops_init.py` | 提示文案 `hermes -p ops`→`argus -p ops` | 初始化脚本引导用新命令名 |
| `README.md` | 重写为中文为主的 Argus 介绍（fork 说明 + 三层核心 + 安装/开发/验证） | 产品门面 |
| `hermes`（仓库根 launcher） | docstring 改 Argus | 开发入口说明 |
| 测试断言同步 | `test_startup_fast_guards.py` / `test_banner.py` / `test_cli_skin_integration.py` / `test_skin_engine.py` 中表面字符串 `Hermes Agent`→`Argus` | 表面字符串测试跟随品牌化（断言的是产品外壳，非行为契约） |

- **用户级文件（不在 repo）**：`~/.local/bin/argus` 更新为
  `exec <repo>/.venv/bin/argus -p ops "$@"`（不再依赖 `hermes` 命令名）；
  `~/.hermes/profiles/ops/SOUL.md` 改写为 Argus 运维视角（三层能力 + fail-closed 行为准则）。
- **核销方式**：`argus --version` 显示 `Argus v0.1.0`；`argus --help` / `argus version`
  无 Hermes 字样；banner 首屏为 Argus 标识；`hermes -p ops` 仍可用（入口保留，行为不变）。


### 5. Argus 蓝灰系 ops 皮肤（纯数据 + 配置生成）

- **为什么**：产品外壳已品牌化为 Argus，但默认皮肤还是 Hermes 暖金系。新增内置
  `argus` 皮肤（蓝灰系：主色 `#4A90D9`、深色暗灰底 `#1A202A`、边框/标题蓝灰、
  正文浅色，区域间明度分层），并让 ops profile 默认激活。
- **怎么改**：
  1. `hermes_cli/skin_engine.py` — `_BUILTIN_SKINS` 中 `default` 之后新增
     `"argus"` 条目（纯数据：colors 全套蓝灰 + 继承 default 的 spinner/branding）。
     零逻辑改动；`/skin`、TUI、desktop 通过既有 list_skins/load_skin 自动可见。
  2. `scripts/ops_init.py` — `_CONFIG_TPL` 增加 `display.skin: argus`，新生成的
     ops profile config.yaml 默认激活 argus 皮肤。
  3. 用户级文件：`~/.hermes/profiles/ops/config.yaml` 补 `display.skin: argus`
     （幂等补丁，不影响既有拓扑/runbook/权限矩阵配置）。
- **品牌一致**：argus 皮肤不写 branding 块，继承 default 的
  `agent_name: Argus` / 中文欢迎语 / `◉ Argus` response_label；banner 的
  `◉ ARGUS` 标识（ARGUS_LOGO/ARGUS_HERO）与欢迎语不动。
- **核销方式**：`argus -p ops` 会话启动时 `init_skin_from_config` 读到
  `display.skin: argus`；`argus skin` 列表可见 argus（builtin）并可 `/skin argus`
  切换；banner/提示符/工具指示为蓝灰系。


### 6. Argus 统一控制台 CLI（所有 profile 一套控制台头）

- **为什么**：品牌化二期把界面结构统一了——之前只有 ops 模式是控制台头，非 ops
  仍保留 Hermes 经典大面板（Available Tools / Available Skills 列表），观感还是
  像 Hermes。运维定位的 CLI 需要的不是「AI 助手仪表盘」，而是「我在哪个环境、
  Argus 记得什么、安全门是否开着」——改为所有 profile 共用一套 Argus 控制台头，
  ops 能力按实际状态显示（开启显示实测值，未开启显示 off + 引导）。
- **怎么改**（全部显示层 + 皮肤数据，零核心逻辑改动）：
  1. `hermes_cli/banner.py` — 删除 ops/非 ops 双分支，收敛为统一的
     `_load_banner_state()`（读 config 的 `ops:` 块 + profile home 的
     topology.yaml / runbooks/ 快照，带 5s TTL 缓存，任何 profile 都返回 dict：
     `ops_enabled` 标志 + env / matrix / topology / runbook / profile / home /
     entity_count / runbook_count）和 `_render_banner()`：左侧 hero 标识
     （◉ ARGUS / 记住整个平台 / 安全地动生产）+ model/cwd/session，右侧
     `PROFILE / ENV / GATES / TOPOLOGY / RUNBOOKS / HOME` 状态行——ops 开启显示
     实测值（`[env] badge`、`matrix ON · L1–L4`、`N entities`、`N loaded`），
     未开启显示 off 态；底部统一能力行（ops：`◈ topo_query · runbook_load ·
     permission matrix`，非 ops：`◈ N tools · M skills · /help` + ops_init 引导）。
     `build_welcome_banner` 变薄包装，去掉对 `check_tool_availability` 的依赖，
     不再渲染任何工具/技能清单。
  2. `cli.py` — `_build_compact_banner()` 改为每行三行统一结构：ops 开启显示
     `[env] · matrix ON · topo N · runbooks M`，否则 `◈ N skills · /help`；
     `_get_tui_prompt_fragments()` 正常态：ops 开启且有 env 时提示符前插 env
     badge（`[test] ops ❯ `，`class:ops-env-<env>` 皮肤驱动），其余 profile
     保持 `❯ `。
  3. `hermes_cli/skin_engine.py` — argus 皮肤补 `ops_env_test/uat/prod` 颜色键、
     冷静 spinner（`(·)` 系列 + 运维动词，去 kawaii）、topo/runbook 工具 emoji
     （🧭/📋）；`get_prompt_toolkit_style_overrides()` 注册 `ops-env-*` 样式类
     （数据驱动，非 argus 皮肤回退默认蓝/金/红）。
  4. 测试 — `tests/hermes_cli/test_banner_ops.py` 重写为统一版（状态快照、
     ops off 返回 off 态 dict、ops 控制台头、matrix OFF 警示、非 ops 共用
     控制台头 + 能力汇总行）；`tests/hermes_cli/test_banner_skills_width.py`
     改为断言技能清单不再渲染、能力行只报计数。
- **核销方式**：任意 profile 启动都显示同一套 Argus 控制台头（ops：ENV badge /
  matrix / 拓扑计数；非 ops：off 态 + N tools · N skills）；`argus -p ops` 提示符
  `[test] ops ❯ `；`/skin` 切换不受影响；banner 的 ◉ ARGUS 标识与欢迎语不动。
- **未碰**：conversation loop / 上下文压缩 / prompt 缓存 / `system_prompt.py`；
  topo/runbook/权限矩阵逻辑零改动。

## 未碰的核心区（按 §6.5 硬约束）

- 不碰 conversation_loop / 上下文压缩 / prompt 缓存逻辑。
- 不新增 `HERMES_*` env var；新配置全部走 `config.yaml` 的 `ops:` 块。
- 不修改 `agent/system_prompt.py`（拓扑注入走 memory provider 外部块，§7 A 方案）。

## 新能力（零侵入，独立文件）

| 文件 | 对应 § | 说明 |
|---|---|---|
| `tools/topo_tools.py` | §2.2 / §4 B | topo_query / topo_update，registry toolset=topo |
| `plugins/memory/topo/` | §4 A / §7 A | TOPO 段注入（system prompt 外部 memory block）+ §4 C 行为约束 |
| `tools/ops_permissions.py` | §3 / §4 D | 命令分级矩阵（L1-L4 × env → execute/approve/deny） |
| `tests/tools/test_topo_tools.py` 等 | §6.3 | 数据契约 + 矩阵验证 |
| `scripts/ops_init.py` | §6.3 | ops profile 初始化：建 profile + 写 ops config + 铺样例拓扑（幂等，--force 重铺） |
| `ops-profile/` | §2.2 | 样例拓扑：第一层 topology.yaml（39.106.217.32 集群实体）+ 第二层 entities/ |
| `OPS-VERIFY.md` | §6.3 | 用户亲自验证步骤（TOPO 段 / topo_query / topo_update / 权限矩阵） |
| `tools/runbook_tools.py` | §1 / §3 L4 | runbook_load（按名/触发关键字/列表）+ runbook_checkpoint（L4 checklist 阶段门），registry toolset=runbook |
| `ops-profile/runbooks/` | §1 / §3 L4 | 样例 runbook：harbor-restart / gateway-svc-restart（事故）+ deploy-gateway-svc（L4 部署 checklist 模板） |
| `tests/tools/test_runbook_tools.py` | §6.3 | runbook 加载/匹配/校验/L4 阶段门测试 |

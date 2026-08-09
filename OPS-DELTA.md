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


### 4. `tools/approval.py` — `check_all_command_guards` 增加命令目标解析钩子（加段，二期安全插队）

- **为什么**：矩阵原本按会话 env 判定，test 会话对 prod 实体（node2）的 L3/L4
  命令全放行——"跨环境操作默认拒绝"只有行为约束、没有硬 gate（会话开着人离开时
  agent 可被诱导对 prod 实体执行 L3/L4）。ops-agent-harness.md §3 要求"每命令
  env 强制参数 + 跨环境拒绝"，实现时记为二期，现在提前做。
- **怎么改**（三处插入，均不改动既有分支行为）：
  1. ops 检查之前：调用 `tools.ops_target.resolve_command_target(command)` 解析
     命令目标（ssh/scp/sftp 的 user@host、kubectl → k3s-prod），命中拓扑实体则把
     目标 env 传入 `check_ops_command_permission(target_env=...)`；解析不到 →
     `target=None`，完全走现状（会话 env + TOPO 段行为约束兜底）。
  2. deny / fail-closed 阻断消息：命中目标时附注 `目标: node2 (prod)`，让人工
     一眼看到受影响实体（prod 行 L3/L4 deny、L2 审批复用现有流不变）。
  3. ops approve 警告描述：命中目标时附注 `目标: node2 (prod)`，审批提示与
     smart-approval 输入都带上目标实体。
- **核销方式**：`ops.permissions.enabled=false` 时零影响；目标解析失败（临时机器 /
  无拓扑 / 无目标命令）完全走现状；不 fail-closed 误伤拓扑外机器。

### 5. `tools/ops_permissions.py` — `check_ops_command_permission` 增加 `target_env` 参数（加参数，行为不变）

- **为什么**：命令目标级 env 判定需要矩阵按目标 env 查表（test 会话 + 目标 prod
  → 按 prod 判定）；不传时维持会话 env（现状）。
- **怎么改**：签名加 `target_env: Optional[str] = None`，env 解析改为
  `(target_env or _active_env()).strip().lower()`；目标 env 未在矩阵声明时同样
  返回 None（不误判未知环境）。docstring 补充目标级判定说明。
- **核销方式**：现有测试全过；target_env 覆盖只在调用方显式传入时生效。

### 6. `tools/ops_target.py` — 命令目标解析模块（新建，纯增量）

- **为什么/怎么改**：见文件头注释。`resolve_command_target(command)` 从命令串解析
  目标主机（ssh/scp/sftp 的 user@host、裸 `@<ipv4>`），映射到拓扑实体（第一层
  `name` / `endpoint`，第二层 `attrs.public_ip` / `attrs.internal_ip`），返回
  `{"entity", "env", "label", "matched_by"}`；含 `kubectl` 的命令关联 `k3s-prod`
  （env=prod）。只读、无副作用，复用 `tools.topo_tools` 的加载器保证与
  topo_query 看到同一份数据。
- **边界**：解析不到目标 / 拓扑查无此实体 → None 走现状，不 fail-closed 误伤
  临时机器；实体无 env（数据缺口）→ None 交回现状。

## 产品外壳品牌化（Vigil 一期，2026-08-08）

> ※ 更名沿革：2026-08-08 当天，产品名由 Argus 统一更名为 Vigil（定位语不变：
> 「记住整个平台，安全地动生产」）。本文档历史条目均按当前名 Vigil 记载，
> git 历史保留 Argus 阶段。

- **为什么**：产品是 Vigil（运维 agent harness），但外壳还是 Hermes（CLI 入口 /
  banner / 版本号 / README）。本次只动产品外壳表面层（入口、banner、help、README、
  SOUL 措辞），**零逻辑改动**：topo/runbook/权限矩阵代码未动，`hermes -p ops`
  行为不变。
- **怎么改**（逐条）：

| 文件 | 改了哪 | 为什么 / 怎么改 |
|---|---|---|
| `pyproject.toml` | `version 0.20.0→0.1.0`、`description` 改为 Vigil 定位；`[project.scripts]` 增加 `vigil = hermes_cli.main:main` | 产品一期完成（拓扑+runbook+权限矩阵），版本号归 Vigil 所有。发行包名**沿用** `hermes-agent`（fork 兼容：importlib.metadata、dist-info、release 脚本均按此名解析；产品名是 Vigil）。`hermes` 入口保留，行为不变 |
| `hermes_cli/__init__.py` | `__version__ = "0.1.0"`、`__release_date__ = "2026.8.8"`、模块 docstring 改 Vigil | 运行时版本单一来源；CLI/gateway/dashboard/ACP 的版本串都从这里取 |
| `hermes_cli/_startup_fast.py` | 快速 `--version` 输出 `Hermes Agent v…` → `Vigil v…`，提示命令 `hermes version` → `vigil version` | `vigil --version` 必须显示 Vigil（快速路径先于重导入执行，是首屏） |
| `hermes_cli/banner.py` | `HERMES_AGENT_LOGO`（ASCII 大图）→ `VIGIL_LOGO`（简洁文字：名字 + 「记住整个平台，安全地动生产」）；`HERMES_CADUCEUS` → `VIGIL_HERO`（简洁标识）；`format_banner_version_label()` 版本标签 `Hermes Agent v…` → `Vigil v…` | 任务硬约束：banner 换 Vigil 标识，风格简洁，不要花哨 ASCII 大图 |
| `cli.py` | 紧凑 banner 文案（`NOUS HERMES` / `Hermes Agent` / `Nous Research`）→ Vigil + 运维定位；欢迎语 → 中文「欢迎使用 Vigil——记住整个平台，安全地动生产」；class/module docstring；dead 的 ASCII 常量换成 Vigil 简洁标识 | 交互 CLI 首屏与欢迎语品牌化；纯字符串，无逻辑改动 |
| `hermes_cli/skin_engine.py` | 默认皮肤 branding：`agent_name: Hermes Agent`→`Vigil`，`welcome`→中文欢迎语，`response_label` 去 Hermes | 皮肤引擎的默认品牌串是 banner/状态栏的回退来源 |
| `hermes_cli/_parser.py` | `prog="hermes"`→`"vigil"`；顶层 description 改为 Vigil 运维定位；epilogue 示例命令 `hermes`→`vigil`；chat 子命令 description | `vigil --help` 显示 Vigil；`hermes --help` 同样显示 vigil（同一产品） |
| `hermes_cli/main.py` | 模块 docstring 用法示例 `hermes`→`vigil`；`cmd_uninstall`/`cmd_update`/ACP docstring 与 WhatsApp 引导 Tip 改 Vigil | 入口文档与命令名一致 |
| `hermes_cli/config.py` | `recommended_update_command()` 的 git 安装回退 `hermes update`→`vigil update` | `vigil version` 的更新提示指向正确命令 |
| `hermes_cli/commands.py` | `/update`、`/version` CommandDef 描述 `Hermes Agent`→`Vigil` | help/命令描述品牌化 |
| `hermes_cli/profiles.py` | `create_wrapper_script()` 生成的 profile 包装命令从 `hermes -p` → `vigil -p`（优先解析 `vigil`，回退 `hermes`）；`build_alias_map()` / `remove_wrapper_script()` 的 wrapper 识别针同步兼容 `vigil -p ` 与 `hermes -p ` | 包装命令不再依赖 hermes 命令名；反向识别（alias→profile）与删除校验跟上新命令名 |
| `hermes_cli/default_soul.py` | `DEFAULT_SOUL_MD` 改为 Vigil 视角 persona | 新 profile 的 SOUL 从出生就是 Vigil；不影响 `_LEGACY_TEMPLATE_SOULS` 旧模板迁移匹配 |
| `acp_adapter/server.py` | ACP `version` 能力返回 `Hermes Agent v…`→`Vigil v…` | 编辑器集成侧的版本串 |
| `gateway/slash_commands.py`、`gateway/platforms/api_server.py` | /version、/update 相关文案与 docstring 改 Vigil | 网关侧命令文案（/version 正文走 banner 版本标签，自动变 Vigil） |
| `hermes_cli/uninstall.py` | 卸载器标题与致谢文案 `Hermes Agent`→`Vigil`（shell rc 的 `# Hermes Agent` 注释匹配保持不动） | 卸载界面品牌化；注释匹配是清理逻辑的一部分，不能动 |
| `scripts/ops_init.py` | 提示文案 `hermes -p ops`→`vigil -p ops` | 初始化脚本引导用新命令名 |
| `README.md` | 重写为中文为主的 Vigil 介绍（fork 说明 + 三层核心 + 安装/开发/验证） | 产品门面 |
| `hermes`（仓库根 launcher） | docstring 改 Vigil | 开发入口说明 |
| 测试断言同步 | `test_startup_fast_guards.py` / `test_banner.py` / `test_cli_skin_integration.py` / `test_skin_engine.py` 中表面字符串 `Hermes Agent`→`Vigil` | 表面字符串测试跟随品牌化（断言的是产品外壳，非行为契约） |

- **用户级文件（不在 repo）**：`~/.local/bin/vigil` 更新为
  `exec <repo>/.venv/bin/vigil -p ops "$@"`（不再依赖 `hermes` 命令名）；
  `~/.hermes/profiles/ops/SOUL.md` 改写为 Vigil 运维视角（三层能力 + fail-closed 行为准则）。
- **核销方式**：`vigil --version` 显示 `Vigil v0.1.0`；`vigil --help` / `vigil version`
  无 Hermes 字样；banner 首屏为 Vigil 标识；`hermes -p ops` 仍可用（入口保留，行为不变）。


### 5. Vigil 蓝灰系 ops 皮肤（纯数据 + 配置生成）

- **为什么**：产品外壳已品牌化为 Vigil，但默认皮肤还是 Hermes 暖金系。新增内置
  `vigil` 皮肤（蓝灰系：主色 `#4A90D9`、深色暗灰底 `#1A202A`、边框/标题蓝灰、
  正文浅色，区域间明度分层），并让 ops profile 默认激活。
- **怎么改**：
  1. `hermes_cli/skin_engine.py` — `_BUILTIN_SKINS` 中 `default` 之后新增
     `"vigil"` 条目（纯数据：colors 全套蓝灰 + 继承 default 的 spinner/branding）。
     零逻辑改动；`/skin`、TUI、desktop 通过既有 list_skins/load_skin 自动可见。
  2. `scripts/ops_init.py` — `_CONFIG_TPL` 增加 `display.skin: vigil`，新生成的
     ops profile config.yaml 默认激活 vigil 皮肤。
  3. 用户级文件：`~/.hermes/profiles/ops/config.yaml` 补 `display.skin: vigil`
     （幂等补丁，不影响既有拓扑/runbook/权限矩阵配置）。
- **品牌一致**：vigil 皮肤不写 branding 块，继承 default 的
  `agent_name: Vigil` / 中文欢迎语 / `◉ Vigil` response_label；banner 的
  `◉ VIGIL` 标识（VIGIL_LOGO/VIGIL_HERO）与欢迎语不动。
- **核销方式**：`vigil -p ops` 会话启动时 `init_skin_from_config` 读到
  `display.skin: vigil`；`vigil skin` 列表可见 vigil（builtin）并可 `/skin vigil`
  切换；banner/提示符/工具指示为蓝灰系。


### 6. Vigil 统一控制台 CLI（所有 profile 一套控制台头）

- **为什么**：品牌化二期把界面结构统一了——之前只有 ops 模式是控制台头，非 ops
  仍保留 Hermes 经典大面板（Available Tools / Available Skills 列表），观感还是
  像 Hermes。运维定位的 CLI 需要的不是「AI 助手仪表盘」，而是「我在哪个环境、
  Vigil 记得什么、安全门是否开着」——改为所有 profile 共用一套 Vigil 控制台头，
  ops 能力按实际状态显示（开启显示实测值，未开启显示 off + 引导）。
- **怎么改**（全部显示层 + 皮肤数据，零核心逻辑改动）：
  1. `hermes_cli/banner.py` — 删除 ops/非 ops 双分支，收敛为统一的
     `_load_banner_state()`（读 config 的 `ops:` 块 + profile home 的
     topology.yaml / runbooks/ 快照，带 5s TTL 缓存，任何 profile 都返回 dict：
     `ops_enabled` 标志 + env / matrix / topology / runbook / profile / home /
     entity_count / runbook_count）和 `_render_banner()`：左侧 hero 标识
     （◉ VIGIL / 记住整个平台 / 安全地动生产）+ model/cwd/session，右侧
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
  3. `hermes_cli/skin_engine.py` — vigil 皮肤补 `ops_env_test/uat/prod` 颜色键、
     冷静 spinner（`(·)` 系列 + 运维动词，去 kawaii）、topo/runbook 工具 emoji
     （🧭/📋）；`get_prompt_toolkit_style_overrides()` 注册 `ops-env-*` 样式类
     （数据驱动，非 vigil 皮肤回退默认蓝/金/红）。
  4. 测试 — `tests/hermes_cli/test_banner_ops.py` 重写为统一版（状态快照、
     ops off 返回 off 态 dict、ops 控制台头、matrix OFF 警示、非 ops 共用
     控制台头 + 能力汇总行）；`tests/hermes_cli/test_banner_skills_width.py`
     改为断言技能清单不再渲染、能力行只报计数。
- **核销方式**：任意 profile 启动都显示同一套 Vigil 控制台头（ops：ENV badge /
  matrix / 拓扑计数；非 ops：off 态 + N tools · N skills）；`vigil -p ops` 提示符
  `[test] ops ❯ `；`/skin` 切换不受影响；banner 的 ◉ VIGIL 标识与欢迎语不动。
- **未碰**：conversation loop / 上下文压缩 / prompt 缓存 / `system_prompt.py`；
  topo/runbook/权限矩阵逻辑零改动。

### 7. Argus → Vigil 全库更名（2026-08-08）

- **为什么**：产品名定为 Vigil（保留定位语「记住整个平台，安全地动生产」）。
  全库清品牌残留：产品外壳（banner/help/README/SOUL/skin/入口）统一为 Vigil。
- **怎么改**（全部表面层 + 数据，零核心逻辑改动）：
  1. 全库文本替换：`Argus`→`Vigil`、`argus`→`vigil`、`ARGUS`→`VIGIL`（banner 常量、
     help 文案、README、SOUL、皮肤名 `argus`→`vigil`、cli-config 示例、ops_init 文案、
     测试断言）。版本号保持 `v0.1.0`。
  2. `hermes_cli/setup.py` — 产品文案 `Hermes`→`Vigil`（33 处大写 Hermes + 113 处
     命令示例 `hermes xxx`→`vigil xxx` + 4 处向导标题符号 `⚕`→`◉`）；保留
     OpenClaw（迁移来源系统）与基础设施标识（hermes_cli / HERMES_HOME /
     openclaw_to_hermes / ~/.hermes）。
  3. 吉祥物：`hermes_cli/banner.py` 猫头鹰 ASCII 采用用户定稿（/\_/\ + ◉.◉ 眼，蓝灰着色，
     行尾反斜杠在 Rich markup 中双写转义）。启动 banner 收敛为单面板：猫头鹰渲染在
     面板左列（PROFILE/ENV 状态列旁），移除面板上方 VIGIL/标语 logo 块与金色
     VIGIL_HERO（含「记住整个平台，安全地动生产」黄字）；`get_vigil_owl_markup()`
     （VIGIL/猫头鹰/标语）保留给 `/help` 顶部；TUI（`ui-tui/src/banner.ts`）
     LOGO_ART 同款猫头鹰；vigil 皮肤数据保留 `banner_logo`/`banner_hero`
     （TUI/自定义皮肤消费）；眼睛行 `( ◉.◉ )` 前置两空格与上下行对齐。

  3b. prompt 品牌化（纯显示层，零数据影响）：vigil 皮肤新增 `branding.prompt_symbol` =
     `◉ Vigil >`；`cli.py` prepend 逻辑改为——prompt 已含品牌标识（Vigil/◉）时不再
     前置 profile 名。效果：ops 会话提示符 `[test] ops ❯` → `[test] ◉ Vigil >`；
     切回其他皮肤仍保持 `ops ❯` 原行为。runbook/topo/权限数据位置不变。
  3c. 欢迎语英文化（5 处皮肤：default/mono/slate/daylight/warm-lightmode）：删除
     「安全地动生产」中文口号，统一为 `Welcome to Vigil — topology loaded, runbooks
     ready, permission gates armed. Type a message or /help.`（geek/international
     风格，保留拓扑/runbook/权限矩阵定位）；vigil 皮肤 welcome 继承 default。
  4. 入口：`pyproject.toml [project.scripts]` 增加 `vigil`（主入口），保留
     `argus`/`hermes` 别名（行为不变）；`hermes_cli/profiles.py` wrapper 生成
     `vigil -p`，反向识别兼容 `vigil/argus/hermes -p`。
  5. 仓库目录 `/home/wpwang/projects/argus_agent` → `/home/wpwang/projects/vigil-agent`；
     `.venv` shebang 与 pyvenv.cfg 路径同步；用户级 `~/.local/bin/argus` →
     `~/.local/bin/vigil`（exec .venv/bin/vigil -p ops）；ops profile
     config.yaml `display.skin: argus`→`vigil`；SOUL.md 措辞核对。
  6. 保留（fork 兼容 / 上游归属，非品牌残留）：`hermes_cli` 包名、`hermes-agent`
     发行包名、`HERMES_HOME`/`HERMES_*` env、`hermes` 命令别名、上游文档
     website/、`hermes` 压缩模式值（agent 校验集）、Discord User-Agent。
  7. 范围裁定：`apps/`（desktop / bootstrap-installer）不在任务点名面（banner/help/README/
     SOUL/skin/向导/gateway），且 desktop 是 hermes 运行时的客户端；全库清扫误伤的
     207 个 apps 文件已 `git restore` 整体回滚，desktop 保持 Hermes 品牌与可构建一致。
- **核销方式**：`vigil --version` = `Vigil v0.1.0`；`vigil -p ops` 会话 banner 面板左列
  蓝灰猫头鹰；`/skin` 可见 vigil 皮肤；测试全过；`hermes -p ops` 与 `argus -p ops`
  别名仍可进 ops profile。

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
| `tools/ops_target.py` | §3 二期 | 命令目标解析：ssh/scp/sftp user@host、kubectl → k3s-prod，目标主机映射拓扑实体 → 目标级 env（只读零副作用） |
| `tests/tools/test_ops_target.py` 等 | §3 二期 | 目标解析单测 + guard 目标级判定测试（test 会话对 prod 实体 L3/L4 硬拒绝） |

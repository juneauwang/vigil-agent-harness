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
  5. 仓库目录 `/home/your-name/projects/argus_agent` → `/home/your-name/projects/vigil-agent`；
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

### 8. 品牌残留清理：会话退出提示 + fallback/secrets 帮助文本（2026-08-09）

- **为什么**：Vigil 品牌化后，用户可见的"命令提示"里仍有 `hermes <子命令>` 残留——
  最明显是会话退出时的 `Resume this session with: hermes --resume ...`（用户实测
  输出），以及 `hermes fallback` / `hermes secrets bitwarden` 帮助文本。影响产品
  形象，需清干净。`hermes` 命令别名本身保留（见 §7.6），仅改用户可见提示文案。
- **怎么改**（全部纯文案，零逻辑；仅替换用户可见命令提示/帮助/测试断言里的
  `hermes <子命令>` → `vigil <子命令>`；模块引用、`HERMES_HOME`、`~/.hermes`、
  import、内部标识符一律不动）：
  1. `cli.py` `_print_exit_summary()`（约 14769 行）：退出提示
     `hermes --resume {id}[-p profile]` 与 `hermes -c "{title}"` → `vigil`（CLI 路径）。
  2. `hermes_cli/main.py`（约 1567 行）：TUI 退出提示
     `hermes --tui --resume {target}` 与 `hermes --tui -c "..."` → `vigil`。
  3. `hermes_cli/fallback_cmd.py`（11 处）：模块 docstring 帮助、`fallback add` 提示、
     `vigil model` 指引、`fallback list/remove` 提示 → `vigil`。
  4. `hermes_cli/secrets_cli.py`（11 处）：docstring、`bitwarden setup/status/sync/
     disable` 提示 → `vigil`。
  5. 测试同步：`tests/cli/test_exit_summary_resume_hint.py`（7 处断言/docstring）与
     `tests/hermes_cli/test_fallback_cmd.py`（1 处 `fallback add` 断言）→ `vigil`。
- **核销方式**：`test_exit_summary_resume_hint.py` + fallback/secrets 相关套件全过
  （41 passed）；`grep -rn 'hermes ' cli.py hermes_cli/fallback_cmd.py
  hermes_cli/secrets_cli.py` 仅剩模块/路径/注释引用，无用户可见命令提示。
- **未碰**：`conversation_loop` / 缓存 / `system_prompt.py`；`agent/secret_sources/`
  里的 `hermes secrets ... token` 提示、`cli.py` `/save` 与 `main.py` 恢复错误文案
  中的同类残留不在本任务点名范围（后续可单独一轮清理）。

## 发布修复：pip 安装体验（2026-08-09，对应 PyPI vigil-agent-harness v0.1.1）

### A. tirith 启动警告降级（`cli.py` + `tools/tirith_security.py`）

- **为什么**：每次启动都打印
  `⚠ tirith security scanner enabled but not available — ...`。tirith 是
  GitHub release 的外部二进制（非 pip 包），`ensure_installed()` 非阻塞——
  首次运行时后台下载线程刚启动就返回 None，⚠ 警告照打；受限网络下下载失败
  后，每次启动都拿警告吓用户。
- **怎么改**：
  1. `tools/tirith_security.py` 新增 `tirith_install_status()`（无副作用探针）：
     区分 `disabled / installed / installing / failed / unsupported / missing`。
  2. `cli.py` `_ensure_tirith_security()`：仅在 `failed`（磁盘 marker /
     内存哨兵）时打印一行 dim 提示（无 ⚠）：
     `tirith scanner unavailable — command scanning uses built-in patterns only`；
     下载中 / 平台不支持 / 已禁用一律静默。
- **安全不变式**：降级后命令扫描仍走 `tools/approval.py` 的 DANGEROUS_PATTERNS
  模式匹配（现状）；`security.tirith_fail_open` 语义未动，tirith 缺失不放行更多。
- **核销方式**：`tests/tools/test_tirith_security.py`（+8 状态探针用例）与
  `tests/cli/test_tirith_startup_hint.py`（5 用例：installing/unsupported/
  disabled 静默、failed 一行 dim 无 ⚠、只打一次）全过。

### B. ops_init 打包进 wheel（`hermes_cli/ops_init.py` + 样例数据随包分发）

- **为什么**：初始化靠仓库里 `scripts/ops_init.py` + `ops-profile/` 样例数据，
  pip 安装的 wheel 里两者都不存在——`pip install vigil-agent-harness` 后用户
  无法初始化拓扑表和样例 runbook。
- **怎么改**：
  1. 逻辑迁入包内模块 `hermes_cli/ops_init.py`（同参数：`--root / --env /
     --force / --no-alias`，幂等；`--force` 覆盖保留、默认不覆盖），可
     `python -m hermes_cli.ops_init` 直接跑。
  2. 新增 `vigil ops-init` 子命令（`hermes_cli/subcommands/ops_init.py` +
     `hermes_cli/main.py` 的 `cmd_ops_init`），console script 入口。
  3. 样例数据 `ops-profile/` → `hermes_cli/ops_samples/`
     （topology.yaml + entities/ + runbooks/），经 pyproject.toml
     `package-data` 打进 wheel。
  4. `scripts/ops_init.py` 改为薄 shim（`from hermes_cli.ops_init import main`），
     旧命令 `python3 scripts/ops_init.py` 继续可用。
  5. 用户可见提示同步：banner / runbook_load 错误文案 →
     `vigil ops-init`；README 快速开始第 2 步 → `vigil ops-init`。
  6. `setup.py` 构建守卫放行 `VIGIL_BUILD=1`（原有 `HERMES_NIX_BUILD=1`
     保留）：Vigil 经 PyPI wheel 分发（vigil-agent-harness v0.1.1），发布
     流程 `VIGIL_BUILD=1 python -m build` 才产出 wheel；未带任一标志的
     普通构建仍被拒绝。
- **核销方式**：`tests/scripts/test_ops_init.py` 全用例 × 3 种入口
  （scripts shim / python -m hermes_cli.ops_init / vigil ops-init 子命令）
  全过；wheel 构建后 `unzip -l` 验证 `hermes_cli/ops_samples/**` 在包内，
  干净 venv `pip install dist/*.whl` 后 `vigil ops-init` 铺出完整 ops profile。

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
| `hermes_cli/ops_init.py` | §6.3 | ops profile 初始化（包内模块 + `vigil ops-init` 入口）：建 profile + 写 ops config + 铺样例拓扑（幂等，--force 重铺）；`scripts/ops_init.py` 为同入口薄 shim |
| `hermes_cli/ops_samples/` | §2.2 | 样例拓扑（原 `ops-profile/`，随 wheel package-data 分发）：第一层 topology.yaml（203.0.113.10 集群实体）+ 第二层 entities/ |
| `OPS-VERIFY.md` | §6.3 | 用户亲自验证步骤（TOPO 段 / topo_query / topo_update / 权限矩阵） |
| `tools/runbook_tools.py` | §1 / §3 L4 | runbook_load（按名/触发关键字/列表）+ runbook_checkpoint（L4 checklist 阶段门），registry toolset=runbook |
| `hermes_cli/ops_samples/runbooks/` | §1 / §3 L4 | 样例 runbook：harbor-restart / gateway-svc-restart（事故）+ deploy-gateway-svc（L4 部署 checklist 模板） |
| `tests/tools/test_runbook_tools.py` | §6.3 | runbook 加载/匹配/校验/L4 阶段门测试 |
| `tools/ops_target.py` | §3 二期 | 命令目标解析：ssh/scp/sftp user@host、kubectl → k3s-prod，目标主机映射拓扑实体 → 目标级 env（只读零副作用） |
| `tests/tools/test_ops_target.py` 等 | §3 二期 | 目标解析单测 + guard 目标级判定测试（test 会话对 prod 实体 L3/L4 硬拒绝） |

## 待改项（2026-08-10 记录，未实施）

### 1. ops 工具默认加载，去除 ops-init 前置门槛

- **为什么**：目前使用 Vigil 必须 `vigil ops-init` 后才有运维工具（topo/runbook/
  权限矩阵），与产品定位相悖——Vigil 是运维 agent harness，"装上即用"应是默认
  行为，初始化不该是必经之路。
- **待改方向**：默认加载运维工具（topo/runbook/权限矩阵），ops-init 降级为
  "初始化拓扑数据/样例"而非"启用能力"；能力门控改为按数据存在性（topology.yaml
  是否就位）自动切换，而非手动 init。
- **已实施（2026-08-10）**：
  - topo/runbook 工具 check_fn 改为数据存在性门控（topology.yaml 有 core_entities /
    runbooks/ 有 yaml 即可用；`ops.*.enabled` 保留为显式覆盖，`false` 关闭、缺省
    按数据判定），工具降级错误信息与 banner 引导改为"运行 vigil ops-init 铺样例"。
  - 权限矩阵默认启用（`ops.permissions.enabled` 缺省视为 true，显式 false 仍关闭；
    env 缺省读 config，未配置 env 时惰性返回 None，不改变非 ops profile 判定），
    fail-closed 取向不变（DENY 硬拒绝不可绕过）。
  - ops-init 职责降级为"铺样例数据 + 写配置"（帮助文案/README/OPS-VERIFY 同步）。

### 2. ops 样例数据含真实环境，发布前必须脱敏（2026-08-10 已实施）

- **为什么**：`vigil ops-init` 铺出的样例拓扑（`hermes_cli/ops_samples/`）目前
  直接用了真实环境数据（第一层 topology.yaml 含真实集群实体 IP），
  随 wheel 发布到 PyPI 等于把真实基础设施暴露给所有人。作为产品样例必须匿名化。
- **已实施（2026-08-10 v0.1.7）**：样例拓扑全部脱敏——真实 IP → RFC5737 保留段
  （真实 IP → 203.0.113.10 / 203.0.113.11、内网 10.0.1.x →
  203.0.113.13/14/15）、wpwang → your-name、SSH 入口 jump@ → user@；端口与实体名
  保留（结构示例价值）；OPS-VERIFY.md / 测试断言同步占位实体；wheel + sdist 全量
  扫描 0 命中。

### 3. setup 引导仍是 Hermes 形态，需品牌化并裁剪工具

- **为什么**：安装后的引导流程（setup 向导）依然是 Hermes 形态——欢迎文案、
  工具推荐、示例命令都是 Hermes 的，与 Vigil 的运维定位不符；且引导中推荐了
  大量运维用不到的工具（非运维工具集），与"Vigil 是运维 harness"的定位冲突。
- **待改方向**：setup 引导品牌化为 Vigil 形态（文案/logo/命令名）；默认推荐
  工具集裁剪为运维相关（terminal/file/ssh 等），非运维工具从引导推荐中移除
  （不作为默认启用项）；至少删除运维不需要的工具。
- **未实施**：待排期。

### 4. 行为倾向：运维流程沉淀应走 runbook，而非默认创建 skill

- **为什么**：Vigil 继承了 Hermes 的"遇到可复用流程就创建 skill"的核心倾向
  （skill 是 Hermes 的默认能力沉淀机制）。但 Vigil 的产品定位是运维 harness，
  运维流程（事故处理、部署 checklist）应沉淀为 **runbook**（topo/runbook/权限
  矩阵体系的一部分），而不是 skill。当前 agent 在用户描述运维流程时仍倾向
  创建 skill，与产品设计相悖。
- **待改方向**：改核心逻辑——引导 agent 在识别到运维流程类内容（事故处理、
  部署步骤、巡检清单）时优先创建/更新 runbook（runbook_load 体系），而非
  skill；skill 保留用于通用编码/工具类知识，运维流程类明确路由到 runbook。
  具体改点待定位（prompt 引导层 vs 工具选择层），需改核心而非加段。
- **未实施**：待排期，且属于"碰核心逻辑"级改动，按 §7.3 需在本账本登记。

### 5. 凭据文件内容回显防护（agent 读密码文件必须被硬 gate 住）

- **为什么**：2026-08-10 实测失误——agent 排查 sudo 密码问题时用 `od` 检查密码文件，
  把 24 字符密码内容回显进了对话。现有 `security.redact_secrets` 只匹配
  API key/token 形态字符串，短密码 + 换行的文件内容形态上不像 key，redact
  认不出 → 直接进上下文。这是机制盲区：**agent 主动读取凭据类文件并把内容
  回显**没有硬限制，一旦会话日志泄露就是安全事故。
- **待改方向**（三层组合，1+2 必做，3 为提示词辅助）：
  1. 规则阻断层（最硬）：`approval.py` DANGEROUS_PATTERNS 增加"凭据文件读取"
     模式——`cat|od|xxd|head|tail|less` × `sudoers|/etc/shadow|*.pwd|password|secret`
     路径组合，命中触发审批/阻断。
  2. 输出打码层（兜底）：redact 增强——工具输出中 `password|secret|passwd`
     上下文行附近的敏感串打码，防"命令合法、输出泄露"（如 cat config.yaml
     含凭据）。
  3. 行为约束层（辅助）：ops profile SOP 明确"凭据类文件只回显校验结果
     （wc -c / file / grep -c 计数），不回显内容"。
  - **治本方向**：识别"agent 主动读取凭据文件"这个行为模式本身，而非逐条加
    路径——正确路径是引导用户自行验证（sudo -S 提示输入），不是 agent 读文件
    猜密码。
- **未实施**：待排期。

### 6. 拓扑分层重构：三层模型（host/跨 host 总览 → 服务索引 → 服务详情）

- **为什么**：第一层 topology.yaml 有 <50 行限制（session 启动注入 system prompt，
  token 成本约束，方向正确不该取消）。但当前第一层同时承担两个打架的职责——
  **总览**（system prompt 速览）和**实体权威索引**（topo_query/权限矩阵/runbook
  绑定的完整实体集）。平台小（样例 10 实体）时能合一；prod 6 host + 30+ 服务时
  无法兼顾，LLM 靠合并实体凑行数 = 牺牲索引粒度（milvus/pgsql/rabbitmq 并进
  attrs 就丢了独立实体级安全管控），是 workaround 不是解法。
- **待改方向**（三层模型，2026-08-10 设计定稿）：
  1. **第一层 = 总览**（保持 <50 行）：host × N + **跨 host 实体**（k3s 集群、
     gpustack、istio/higress 这类不服从"服务挂单机"树形模型的实体）。样例预算
     ~15 行，可撑 20 台机器不破。
  2. **第二层 = 服务索引**：每个 host（或跨 host 实体）下的 compose 项目/逻辑
     服务一行，挂在所属 host 下；实体仍有独立 name + env（层级是组织方式不是
     命名空间——权限矩阵/runbook 照常按服务名绑定，不因分层失效）。
  3. **第三层 = 服务详情**：现有 entities/<name>.yaml 机制，容器/依赖/端口/镜像
     tag 明细。
  - **关键决策**：docker runtime **不单独占层**——退化为 host attrs 属性
    （`runtime: docker`），避免 6 台机器多 6 个第一层实体。
  - **查询路径**：system prompt 只注入第一层；topo_query 展开第二层，detail=True
    进第三层。总览开销恒定，平台增大不爆 token。
  - **待落地**：schema 版本升级 + topo_query 支持按 host/env/type 过滤 + 样例
    topology.yaml 重构（含 prod 真实拓扑迁移）。
- **未实施**：待排期。

### 7. 会话结束自动汇总 token 用量（可发现性增强）

- **为什么**：`/usage` 已内置 session 级 token 汇总（`agent.session_total_tokens`，
  input/output/reasoning 分类 + context 占用 + 压缩次数），但**用户不知道这个
  命令存在**——用了很久 Vigil 从未发现，直到被提示。用户视角：每轮都有 token
  输出显示，自然想知道"这一个 session 总共烧了多少"，但没有入口自动呈现。
  这是典型可发现性问题：功能存在 ≠ 功能可用。
- **待改方向**：session 结束（退出 / 新会话 / /new）时自动打印一行 token 汇总
  （复用 `_show_usage` 的 Session Token Usage 块，精简为单行：
  `📊 本次会话: 输入 X · 输出 Y · 总计 Z tokens`）；或启动新会话时显示上一会话
  汇总。零新数据逻辑，纯 UI 呈现增强。
- **附带发现**：审视整个 CLI 是否存在同类"功能存在但不可发现"项（如 /context
  的 breakdown、/insights 的历史用量），可一并做帮助入口优化。
- **未实施**：待排期。

### 12. 拓扑自动发现（中间市场开箱即用的地基）

- **为什么**：产品方向锚定"中间市场"（懂业务不懂 AI 配置的人：小企业主/一人
  IT 部/设备工程师）。这类用户**不会手写 topology.yaml**——必须让 Vigil 自己
  摸清平台：SSH 进主机 → 扫 docker/k8s → 自动生成实体。自动发现 + 三层模型
  （第 6 条）= 开箱即用的地基。
- **待改方向**：
  1. 新增发现引擎：SSH 到 host，枚举 docker compose 项目 / k8s 资源 / 端口 /
     GPU，自动生成第一层 + 第二层实体（映射到三层模型 schema）。
  2. 首次运行"引导式发现"：用户只填 IP + 凭据，其余自动；发现结果人工确认后
     落盘 topology.yaml。
  3. 凭据走现有 OpenBao 体系，不落明文。
  4. 与第 6 条（三层模型 schema）同源实现，自动发现输出即 schema 输入。
- **未实施**：待排期（与第 6 条耦合，建议同批实施）。

### 13. 开箱即用 UI 壳（中间市场的产品形态）

- **为什么**：CLI 是运维工程师友好形态，但中间市场（秘书/机械背景用户）看到
  `vigil ops-init --env prod` 就放弃。中间市场需要 Web 界面或桌面应用：
  填服务器 IP → 自动发现 → 点按钮说话。CLI 是内核，UI 是壳。
- **待改方向**：
  1. Web 控制台（复用/扩展现有 dashboard）：服务器接入向导（IP + 凭据 →
     自动发现 → 拓扑展示）、对话界面（同 CLI 内核，不同前端）。
  2. 面向"非技术"的引导：无命令操作，全部点选。
  3. 远期，与 token 差价模式（用户订阅不配模型 key）配套。
- **未实施**：待排期（远期，依赖第 12 条自动发现先行）。

### 11. 交互层应以 environment 为核心概念，而非 profile（`/env prod` 切换）

- **为什么**：用户实测 `/env prod` 报 Unknown command——Vigil 的 CLI 命令体系
  （commands.py）继承 Hermes 的 profile 概念，没有环境切换命令。但 Vigil 的
  数据层已是 environment 语义：权限矩阵 `_active_env()` 按 env 判定（config
  `role`）、banner 显示 ENV badge（test/uat/prod）、ops-init `--env` 参数。
  **数据层认 env、交互层认 profile，两层割裂**。用户判断：profile（谁在用，
  Hermes 个人场景隔离）不适合 Vigil，Vigil 核心应是 environment（操作哪里，
  运维环境隔离）——"我在 prod 还是 test，决定了能干什么"。
- **待改方向**：
  1. 新增 `/env <test|uat|prod>` 命令：切换会话环境，同步更新 banner ENV
     badge + 权限矩阵 `_active_env()`。
  2. 明确 profile 与 env 的关系：profile 保留（个人配置隔离），但会话的
     **操作环境**由 env 决定，profile 不再承载环境语义（或 env 作为 profile
     的属性）。
  3. 权限矩阵/runbook/拓扑查询均按会话 env 过滤，跨 env 操作走审批
     （isolated/strict 语义已有，需接到 /env 切换）。
  4. 检查 banner/提示符（`[test] ◉ Vigil >`）是否已正确反映切换后的 env。
- **env 必须可自定义，不能硬编码 test/uat/prod（2026-08-10 补充）**：现状
  `ops_init.py:191 --env choices=("test","uat","prod")` argparse 枚举锁死 +
  权限矩阵注释固定三档 + 样例拓扑固定两个环境。但真实运维环境是二维组合：
  **物理环境（local/cloud/bare_metal）× 等级（uat/prod）**——如
  bare_metal_uat / bare_metal_prod / local / cloud。硬编码三档表达不了。
  待改方向：
  - env 改为 config.yaml 可定义列表（`ops.environments: [{name, isolation, role}]`），
    ops-init `--env` 不再用 choices 锁死，改为校验"是否在已定义列表"。
  - 权限矩阵按 env 名查表（名称任意，矩阵行为由 isolation/role 决定，不依赖
    名字是 test/uat/prod）。
  - 拓扑 environments 段与 config 的 env 定义同源。
  - `/env <任意已定义名>` 切换。
- **已实施（2026-08-10）**：`/env` 命令（commands.py 注册 + cli.py handler，无参显示
  当前/可用列表、带参校验切换）+ `ops.environments` 可自定义列表（ops-init 生成默认
  三档，`--env` 去 choices 支持自定义名如 bare_metal_prod，自动追加定义并告警拓扑
  不同源）+ 权限矩阵按 env 名查表（行为由 role 决定，`_matrix_row`）+ banner ENV
  badge 随切换更新（写 config `ops.permissions.env`，profile 保留职责分离）。
  切换持久化写 config（`_active_env()`/banner 都从 config 读），跨会话保留。

### 8. 缺少主流 IM 平台 adapter——已确认是同步上游，非开发（2026-08-10 更新）

- **为什么**：用户希望能在飞书/slack/teams/微信里跟 Vigil 对话，并以为现状是
  "公网暴露 + 远端 APP pull"需要改 push。实际核查（2026-08-10）发现**关键事实**：
  Hermes 0.15.1（本机）gateway/platforms 里有 20+ adapter（telegram/slack/
  feishu/wecom/dingtalk/matrix/whatsapp/email/sms…），而 Vigil 0.1.6 打包
  **裁掉了 10+ 个**（只剩 weixin/webhook/api_server/signal/whatsapp_cloud/
  bluebubbles/yuanbao）。**不是要开发 adapter，是把上游文件同步回来 + 补依赖
  和测试**，成本差一个数量级。
- **上游连接模式（全部无需公网入站端口，符合"不暴露端口"诉求）**：
  - feishu：`FEISHU_CONNECTION_MODE` 默认 `websocket`（飞书 2.0 长连接，
    事件 WebSocket 出站推送，零回调 URL；附 encrypt_key/verification_token、
    dedup、群白名单）——用户曾指正回调模式需公网，上游默认已用长连接绕开。
  - slack：Socket Mode（slack_bolt AsyncSocketModeHandler，WebSocket 出站）。
  - telegram：默认 getUpdates 轮询（出站），可选 webhook_mode。
  - wecom：`wss://openws.work.weixin.qq.com` WebSocket 出站。
  - whatsapp：云 API。matrix：原生协议。dingtalk：dingtalk_stream。
- **同步依赖清单**（上游为可选依赖，try/except 包裹，装则启用）：
  python-telegram-bot（telegram）、slack-bolt+slack-sdk（slack）、
  lark-oapi（feishu）、wechatpy/wecom 相关（wecom）、dingtalk-stream
  （dingtalk）、matrix 相关、whatsapp 相关、aiohttp-socks（matrix 代理）。
- **待改方向**：
  1. 从 Hermes 0.15.1 同步缺失 adapter 文件到 Vigil（telegram.py/slack.py/
     feishu.py/wecom.py/dingtalk.py/matrix.py/email.py/sms.py 等）。
  2. 检查 pyproject.toml 依赖分组（可选 extras），与上游对齐。
  3. 补测试；确认平台配置文档（env var 名）不因 fork 漂移。
  4. 一期优先国外市场（slack/telegram/whatsapp/teams），国内 feishu/wecom
     上游也有现成实现，按需启用。
- **未实施**：待排期。

### 9. 自动化运维触发源：cron 已有，缺事件驱动入口（告警 → runbook）

- **为什么**：Vigil 已有完整 cron 系统（cron/jobs.py + scheduler.py，
  gateway 本身是常驻 daemon）——定时触发已解决。但运维自动化的触发源不该
  只有定时，还应有**事件驱动**：Prometheus 告警、服务状态变化、webhook 打到
  Vigil → 自动拉起对应 runbook。cron 是"到点就查"，事件是"出事才动"——
  后者才是运维 agent 的核心价值，当前缺失。
- **待改方向**：
  1. 告警入口：Prometheus Alertmanager webhook → Vigil 接收 → 匹配 runbook →
     自动执行（或审批后执行）。
  2. 复用现有 webhook 机制（gateway/platforms/webhook.py）做接收端。
  3. 事件 → runbook 映射放 config.yaml（事件源/告警名 → runbook 名），
     不硬编码。
- **未实施**：待排期。

### 10. Prometheus 探查应为内置工具，而非 LLM 自写 curl/PromQL

- **为什么**：Vigil 定位是运维 harness，但 tools/ 目录**没有任何 prometheus/
  metrics 内置工具**——探查 Prometheus 数据靠 LLM 自己拼 curl + PromQL。
  这不符合定位：就像不该让 LLM 自己封装 kubectl 一样，PromQL 查询、指标
  解释、告警关联应是基础内置能力，LLM 只做语义层。
- **待改方向**：
  1. 内置 prom_query 工具（PromQL 查询 + 指标解释，读 config.yaml 的
     prometheus endpoint/凭据，不硬编码）。
  2. 内置告警关联能力（查 Alertmanager，把活跃告警映射到实体/runbook）。
  3. 复用现有凭据体系（OpenBao/监控凭据），不新增明文。
- **未实施**：待排期。

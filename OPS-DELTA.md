# OPS-DELTA —— 核心改动账本

> ops-agent-harness.md §7.3：任何对核心文件的改动必须在此登记（改了哪、为什么、
> 怎么改的），季度体检时逐一核销。零侵入能力（新增文件）不在此账本，见文件自身。

> ※ 2026-08-12 合并说明：本文件由两份工作版合并——
> ① 项目仓库版（2026-08-10 23:14，含 #1/#2/#11 已实施标记）；
> ② 桌面工作版 OPS-DELTA_0812.md（含 #12-32 后续记录与 0811/0812 实证）。
> 合并以 git log 为准修正所有状态标记（#5/#15 代码已落地但文档滞后，已修正）。
> 桌面工作版历史文件（OPS-DELTA.md / OPS-DELTA-0811.md / OPS-DELTA_0812.md）退役。
> ※ 2026-08-12 晚第二次合并：并入桌面晚版 OPS-DELTA_0812_晚.md 新增条目
> #33（topo_update 参数契约）/ #34（上游 500 无降级 + 崩溃会话不入库）/
> #35（换模型即失守实证）；#33/#35 附 2026-08-12 代码复核（批次一/二实施后
> 的实际状态：sudo gate 已拦、命令文本部分覆盖、JSON 组合字段名回显缺口仍在）。

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
  5. 仓库目录 `/home/wpwang/projects/vigil-agent`（原 argus_agent）；
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
| `tools/topo_tools.py` | §2.2 / §4 B | topo_query / topo_update，registry toolset=topo；check_fn 数据存在性门控（#1 已实施） |
| `plugins/memory/topo/` | §4 A / §7 A | TOPO 段注入（system prompt 外部 memory block）+ §4 C 行为约束 |
| `tools/ops_permissions.py` | §3 / §4 D | 命令分级矩阵（L1-L4 × env → execute/approve/deny）；默认启用（#1 已实施） |
| `tests/tools/test_topo_tools.py` 等 | §6.3 | 数据契约 + 矩阵验证 |
| `hermes_cli/ops_init.py` | §6.3 | ops profile 初始化（包内模块 + `vigil ops-init` 入口）：建 profile + 写 ops config + 铺样例拓扑（幂等，--force 重铺）；`scripts/ops_init.py` 为同入口薄 shim |
| `hermes_cli/ops_samples/` | §2.2 | 样例拓扑（原 `ops-profile/`，随 wheel package-data 分发）：第一层 topology.yaml（RFC5737 占位实体 203.0.113.10 集群，#2 已脱敏）+ 第二层 entities/ |
| `OPS-VERIFY.md` | §6.3 | 用户亲自验证步骤（TOPO 段 / topo_query / topo_update / 权限矩阵） |
| `tools/runbook_tools.py` | §1 / §3 L4 | runbook_load（按名/触发关键字/列表）+ runbook_checkpoint（L4 checklist 阶段门），registry toolset=runbook；check_fn 数据存在性门控（#1 已实施） |
| `hermes_cli/ops_samples/runbooks/` | §1 / §3 L4 | 样例 runbook：harbor-restart / gateway-svc-restart（事故）+ deploy-gateway-svc（L4 部署 checklist 模板） |
| `tests/tools/test_runbook_tools.py` | §6.3 | runbook 加载/匹配/校验/L4 阶段门测试 |
| `tools/ops_target.py` | §3 二期 | 命令目标解析：ssh/scp/sftp user@host、kubectl → k3s-prod，目标主机映射拓扑实体 → 目标级 env（只读零副作用） |
| `tests/tools/test_ops_target.py` 等 | §3 二期 | 目标解析单测 + guard 目标级判定测试（test 会话对 prod 实体 L3/L4 硬拒绝） |

## 待改项

> 状态图例：✅ 已实施（git hash 佐证）/ ⬜ 未实施 / 🟡 部分实施（已落地部分+遗留）
> 2026-08-12 合并时以 git log 为准统一修正。

### 1. ops 工具默认加载，去除 ops-init 前置门槛 — ✅ 已实施（2026-08-10，19b8b68）

- **为什么**：目前使用 Vigil 必须 `vigil ops-init` 后才有运维工具（topo/runbook/
  权限矩阵），与产品定位相悖——Vigil 是运维 agent harness，"装上即用"应是默认
  行为，初始化不该是必经之路。
- **已实施**：
  - topo/runbook 工具 check_fn 改为数据存在性门控（topology.yaml 有 core_entities /
    runbooks/ 有 yaml 即可用；`ops.*.enabled` 保留为显式覆盖，`false` 关闭、缺省
    按数据判定），工具降级错误信息与 banner 引导改为"运行 vigil ops-init 铺样例"。
  - 权限矩阵默认启用（`ops.permissions.enabled` 缺省视为 true，显式 false 仍关闭；
    env 缺省读 config，未配置 env 时惰性返回 None，不改变非 ops profile 判定），
    fail-closed 取向不变（DENY 硬拒绝不可绕过）。
  - ops-init 职责降级为"铺样例数据 + 写配置"（帮助文案/README/OPS-VERIFY 同步）。
- **遗留**：工具集默认注册（默认 profile 的 platform_toolsets 含 topo/runbook）
  **未做**——见 #14（2026-08-12 实测确认：默认配置下 topo_query/runbook_load
  不在工具列表）。

### 2. ops 样例数据含真实环境，发布前必须脱敏 — ✅ 已实施（2026-08-10 v0.1.7，7357b92）

- **为什么**：`vigil ops-init` 铺出的样例拓扑（`hermes_cli/ops_samples/`）最初
  直接用了真实环境数据（第一层 topology.yaml 含真实集群实体 IP），随 wheel
  发布到 PyPI 等于把真实基础设施暴露给所有人。作为产品样例必须匿名化。
- **已实施**：样例拓扑全部脱敏——真实 IP → RFC5737 保留段（真实 IP →
  203.0.113.10 / 203.0.113.11、内网 10.0.1.x → 203.0.113.13/14/15）、
  wpwang → your-name、SSH 入口 jump@ → user@；端口与实体名保留（结构示例价值）；
  OPS-VERIFY.md / 测试断言同步占位实体；wheel + sdist 全量扫描 0 命中。

### 3. setup 引导仍是 Hermes 形态，需品牌化并裁剪工具 — ✅ 已实施（2026-08-12，批次六：向导欢迎框 Vigil 运维定位 + 非运维工具默认关闭）

- **为什么**：安装后的引导流程（setup 向导）依然是 Hermes 形态——欢迎文案、
  工具推荐、示例命令都是 Hermes 的，与 Vigil 的运维定位不符；且引导中推荐了
  大量运维用不到的工具（非运维工具集），与"Vigil 是运维 harness"的定位冲突。
- **待改方向**：setup 引导品牌化为 Vigil 形态（文案/logo/命令名）；默认推荐
  工具集裁剪为运维相关（terminal/file/ssh 等），非运维工具从引导推荐中移除
  （不作为默认启用项）；至少删除运维不需要的工具。
- **已实施（批次六，2026-08-12）**：
  1. **向导欢迎框品牌化**（`hermes_cli/setup.py`）：欢迎框抽出为
     `_wizard_welcome_box_lines()`（纯展示数据，便于断言），文案改为
     "Welcome to Vigil — topology loaded, runbooks ready, permission gates
     armed. Configure your agent below."，与 banner/skin 欢迎语一致；其余
     向导流程未重构（批次一已品牌化主体，复核无 `hermes <子命令>` 用户可见
     残留）。
  2. **工具默认预选裁剪**（`hermes_cli/tools_config.py`）：`_DEFAULT_OFF_TOOLSETS`
     新增 `browser`/`image_gen`/`computer_use`（浏览器自动化/图像生成/桌面
     自动化——非运维定位），默认关闭仅作用于"未显式保存工具集列表"的预选
     判定（`_get_platform_tools` 隐式分支 + 首次安装 checklist
     `checklist_preselected`）；**不删工具本身**，显式保存列表或 `vigil tools`
     手动启用路径不受影响。运维核心（terminal/file/code_execution/skills/
     memory/todo/web + 批次二已默认启用的 topo/runbook）保持默认；`bfl`
     按 recently-shipped 契约保留（首版发布回填机制，测试钉住，不并入本轮）。
  3. 用户可见文案：`⚕ Hermes Tool Configuration` → `⚕ Vigil Tool
     Configuration`；x_search 描述/终端权限提示/重启 tracing 提示的 Hermes →
     Vigil；模块 docstring 同步。
  单测：tests/hermes_cli/test_setup_branding.py（9 例：欢迎框 Vigil 定位 +
  无 hermes 残留 + 默认预选含运维工具/不含非运维工具 + 显式配置手动启用不受
  影响）。既有 setup/tools_config 套件全过（test_setup.py 中 modal 用例
  为基线既有挂起，与本次无关，见下）。
- **核销方式**：`ops` 开关无关（引导/预选是安装层行为）；`_DEFAULT_OFF_TOOLSETS`
  只影响"未显式配置"的默认预选，显式列表权威不受影响；季度体检抽查新安装
  默认工具集不再含 browser/image_gen/computer_use，`vigil tools` 手动启用
  仍可用。风险点：存量用户若从未保存过工具集列表，升级后 browser/image_gen/
  computer_use 会从默认启用变为默认关闭——需在 `vigil tools` 重新启用（这是
  本批"非运维工具不作为默认"的预期行为）。

### 4. 行为倾向：运维流程沉淀应走 runbook，而非默认创建 skill — ✅ 已实施（2026-08-12，批次六：SKILLS_GUIDANCE 运维分流段；SOUL 已有 runbook 倾向核实）

- **为什么**：Vigil 继承了 Hermes 的"遇到可复用流程就创建 skill"的核心倾向
  （skill 是 Hermes 的默认能力沉淀机制）。但 Vigil 的产品定位是运维 harness，
  运维流程（事故处理、部署 checklist）应沉淀为 **runbook**（topo/runbook/权限
  矩阵体系的一部分），而不是 skill。当前 agent 在用户描述运维流程时仍倾向
  创建 skill，与产品设计相悖。
- **待改方向**：改核心逻辑——引导 agent 在识别到运维流程类内容（事故处理、
  部署步骤、巡检清单）时优先创建/更新 runbook（runbook_load 体系），而非
  skill；skill 保留用于通用编码/工具类知识，运维流程类明确路由到 runbook。
  具体改点待定位（prompt 引导层 vs 工具选择层），需改核心而非加段。
- 属于"碰核心逻辑"级改动，按 §7.3 需在本账本登记。
- **已实施（批次六，2026-08-12，提示词内容层，硬约束 1 允许例外）**：
  1. `agent/prompt_builder.py` `SKILLS_GUIDANCE` 常量尾部追加 OPS ROUTING
     分流段（**不删除**原 skill 引导——通用编码/工具类知识仍走 skill）："运维
     流程类内容（事故处理、部署步骤、巡检清单、凭据轮换等）不存 skill——用
     runbook_load 体系沉淀为 runbook（拓扑/runbook/权限矩阵的一部分）。识别到
     运维流程时优先创建/更新 runbook，skill 保留用于通用编码/工具类知识。"
  2. 只动常量文本，未动 prompt 组装/缓存逻辑（system_prompt.py 的注入点在
     `skill_manage` 工具加载时才拼入，属稳定前缀；新文本只影响**新建会话**的
     system prompt，会话内缓存前缀不受影响）。
  3. `hermes_cli/default_soul.py` `DEFAULT_SOUL_MD` **核实已含 runbook 优先
     倾向**（"follow runbooks for how to act"）——不再补；用户级 SOUL.md
     无需建议文本（内置模板已覆盖，未改用户级文件）。
  单测：tests/agent/test_prompt_builder.py 新增
  `test_skills_guidance_routes_ops_workflows_to_runbook`（OPS ROUTING 段存在 +
  原 skill 引导/Skill Safety Rule 不回归；既有断言均为"包含"型，非快照相等）。
  test_prompt_builder.py + test_ghost_skill_pruning.py 全过。
- **核销方式**：`ops` 开关无关（提示词内容层）；季度体检复核 SKILLS_GUIDANCE
  仍含 OPS ROUTING 段、`skill_manage` 未加载时该段不进入 system prompt（注入
  条件未变）；新建会话可看到分流行为，会话内缓存前缀字节稳定。

### 5. 凭据文件内容回显防护 — ✅ 代码已实施（2026-08-10/11，5de609a + 8f43841 + 7b875c3），治本方向待排期

- **为什么**：2026-08-10 实测失误——agent 排查 sudo 密码问题时用 `od` 检查密码文件，
  把 24 字符密码内容回显进了对话。现有 `security.redact_secrets` 只匹配
  API key/token 形态字符串，短密码 + 换行的文件内容形态上不像 key，redact
  认不出 → 直接进上下文。这是机制盲区：**agent 主动读取凭据类文件并把内容
  回显**没有硬限制，一旦会话日志泄露就是安全事故。
- **已实施**（三层组合）：
  1. 规则阻断层（最硬，5de609a + 8f43841）：`approval.py` DANGEROUS_PATTERNS
     增加"凭据文件读取"模式——`cat|od|xxd|head|tail|less` ×
     `sudoers|/etc/shadow|*.pwd|password|secret` 路径组合，命中触发审批/阻断；
     8f43841 补 kubeconfig 凭据读取的绝对路径形态（/root /home/<user>），修复
     sudo 场景漏网。
  2. 输出打码层（兜底，5de609a + 7b875c3）：redact 增强——工具输出中
     `password|secret|passwd` 上下文行附近的敏感串打码；7b875c3 补三个实证
     泄露路径：`_JSON_KEY_NAMES` 增加自定义凭据字段名（ssh_key/private_key/
     passphrase 等）、`_SECRET_ENV_NAMES` 覆盖 SSHPASS 类 ENV 赋值、
     reasoning/thinking 渲染路径（`_stream_reasoning_delta`）接入 redact——
     "所有渲染出口"（工具输出、工具调用行、reasoning 块）统一过 redact。
  3. 行为约束层（辅助）：ops profile SOP 明确"凭据类文件只回显校验结果
     （wc -c / file / grep -c 计数），不回显内容"（SOP 条款曾写入 default_soul
     后回滚，见 db23e0b，改为运行时约束）。
- **治本方向（未实施）**：识别"agent 主动读取凭据文件"这个行为模式本身，而非
  逐条加路径——正确路径是引导用户自行验证（sudo -S 提示输入），不是 agent 读
  文件猜密码。待排期。

### 6. 拓扑分层重构：三层模型（host/跨 host 总览 → 服务索引 → 服务详情）— ✅ 已实施（2026-08-12，批次二）

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

- **已实施（批次二）**：
  - **schema v0.2 三层模型**：第一层 `topology.yaml`（version: 2，<50 行：
    environments + hosts + cross_host + key_paths）；第二层 `hosts/<hostname>.yaml`
    （每 host 一个服务索引：name/type/env/endpoint/detail）；第三层沿用
    `entities/<name>.yaml`（原位迁移，不改名）。样例重构为 v0.2（RFC5737 占位
    实体不变：node1/node2/test-host + k3s-prod/ingress 跨 host + 6 个服务），
    ops-init 同步铺 hosts/ 目录。
  - **v0.1 向后兼容**：`load_topology` + `_all_core_entities` 双版本解析——
    v0.1 扁平 core_entities 内部展开为同一扁平实体视图（name+env 契约不变，
    权限矩阵/runbook 按服务名绑定不失效）；v0.1 无 host 概念，`host=` 过滤返回
    明确错误而非静默错结果。
  - **查询路径**：topo_query 无参 → 第一层总览（紧凑，只 hosts+cross_host）；
    `host=<name>` → host + 第二层服务索引；`entity=<name>` 跨层名解析（先第二层
    服务名，再第一层 host/cross_host，host 命中附带 services 列表）；detail=True
    → 第三层档案。TOPO 段注入（plugins/memory/topo）改为只渲染第一层（token
    开销恒定）。
  - **ops_target**：扁平视图含第二层服务（服务经 "服务 → 所属 host → env" 链路
    继承 env），ssh/scp 命中服务实体同样能解析目标级 env。
  - **#30 方案 1 一并落地**：列表/总览视图默认只返回紧凑字段
    （name/type/env/endpoint/stale），detail=True 才给全字段。
  - **核销方式**：`tests/tools/test_topo_v2.py`（14 例）+ ops-init/topo_provider/
    ops_target 相关套件全过；`vigil ops-init` 铺 v0.2 样例后
    topo_query 总览 / host=node1 / entity=harbor detail=True 均正确。

### 7. 会话结束自动汇总 token 用量（可发现性增强）— ✅ 已实施（2026-08-12，批次三）

- **为什么**：`/usage` 已内置 session 级 token 汇总（`agent.session_total_tokens`，
  input/output/reasoning 分类 + context 占用 + 压缩次数），但**用户不知道这个
  命令存在**——用了很久 Vigil 从未发现，直到被提示。用户视角：每轮都有 token
  输出显示，自然想知道"这一个 session 总共烧了多少"，但没有入口自动呈现。
  这是典型可发现性问题：功能存在 ≠ 功能可用。
- **改了什么**：`cli.py` `_print_exit_summary()` 在现有退出摘要（Duration /
  Messages 之后）追加一行 token 汇总：`Tokens: 📊 本次会话: 输入 X · 输出 Y ·
  总计 Z tokens`。纯 UI 呈现，零新数据逻辑——字段读取与 `_show_usage` 同一来源
  （`agent.session_input_tokens / session_output_tokens / session_total_tokens`）。
- **为什么这么改**：有 live agent 且 `session_api_calls > 0` 才打印（无 agent /
  0 调用跳过，避免 0-token 噪音行）；字段用 `getattr(..., 0) or 0` 防属性缺失
  （-q 模式 / 早期退出路径无这些属性时不炸）。
- **核销方式**：`tests/cli/test_exit_summary_tokens.py`（5 例，覆盖有 agent 有
  调用 / 无 agent / 0 调用 / `clear_screen=False` 路径）+ 既有
  `test_exit_summary_resume_hint.py`（5 例，退出提示文案不回归）全过。
- **未做**：`/new` 或新会话启动显示上一会话汇总——需要碰会话生命周期核心
  （prompt 缓存 / conversation_loop 边界），按硬约束 1 登记跳过，只做第 1 点
  （退出汇总）。

### 12. 拓扑自动发现（中间市场开箱即用的地基）— ✅ 已实施（2026-08-12，批次二，与 #6 同批）

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
- **建议**：与第 6 条同批实施。

- **2026-08-11 落地细化（AIOps 三层架构 + 固化边界，接入 Prometheus/Loki 的完整蓝图）**：
  - **固化边界（核心设计原则）**：把确定性的部分固化进代码，LLM 只留判断和编排——
    "工具是 LLM 的眼睛和手，LLM 是大脑"。
  - **固化进代码（LLM 不需要做）**：
    1. PromQL/Loki 查询执行：参数校验、超时、vault 凭据注入、basic auth 头、
       结果解析为结构化数据——LLM 每次拼 curl 是纯重复劳动且易错（转义、
       PromQL 语法、凭据泄露，见第 21 条）；
    2. 告警接收：webhook 解析、归一化、去重聚合（协议处理是确定性代码）；
    3. 拓扑关联：instance → 实体 → 依赖链 → runbook 匹配（查表逻辑，LLM 每次做不一致）；
    4. 凭据管理：vault 读取、注入、不回显（最不能交给 LLM）。
  - **留给 LLM（固化不了也不该固化）**：PromQL 结果语义解释、故障该走哪个
    runbook/是否审批的决策、runbook 步骤执行与进展解释。
  - **落地顺序（三层，逐步做）**：
    1. **数据层（先做，一周内）**：`prom_query` + `loki_query` 内置工具（纯增量，
       仿 tools/topo_tools.py 模式，服务门控 check_fn 加载不污染每次 API 调用；
       只读，工具内 vault 注入 basic auth/OrgID 头，LLM 不见明文；PromQL 语法
       校验 + 查询失败返回原始错误防幻觉）；
    2. **事件层（核心增量）**：Alertmanager webhook 接收（复用
       gateway/platforms/webhook.py）→ 告警归一化 → 拓扑关联（告警 instance/label
       → topo 实体 → env/依赖链/runbook/vault 路径，这是 Vigil 独有优势：
       告警不再孤立，一条 up==0 自动变"实体挂了+依赖链+关联 runbook"）→
       按权限矩阵分级（test 可自动处置，prod 高危走审批）；
    3. **行动层（前两层稳定后）**：告警 → runbook 匹配（映射放 config.yaml，
       不硬编码）→ 执行或审批 → 结果回写告警状态。
  - **风险点（设计必考虑）**：告警风暴（同实体 N 分钟只触发一次 runbook，
    必须聚合去重）；凭据走 vault 工具内注入不见明文；查询工具永远只读、
    处置动作统一走 runbook+权限矩阵；PromQL 错误查询返回原始错误不编造；
    事件循环安全（runbook 触发告警→告警又触发 runbook 防环）；cron 会话
    不递归调度。

  - **2026-08-12 事件层形态修正（重要产品判断）：webhook 接收改为"拉模式巡检"**：
    - **为什么**：Vigil 定位是跑在运维人办公电脑上的本地 agent（非服务器常驻），
      webhook 要求接收端有稳定可达地址，办公电脑三条都不满足——IP 漂移（家庭/
      办公网切换）、NAT 后不可达、人下班关机不在线。**push 模式不适配本地
      agent 形态**；"值班人"语义本就该是主动巡检（拉），不是被动等电话。
    - **落地形态（纯拉模式，集群零改动）**：cron 定期（*/5）跑采集脚本拉
      alertmanager `/api/v2/alerts`（无认证）+ prometheus `/api/v1/alerts`
      （vault basic auth）→ 有告警才输出 → cron script 模式注入 agent →
      值班播报（拓扑关联/影响面/runbook 匹配/结论分级）；无告警脚本无输出
      → 系统跳过 AI 调用零成本。alertmanager receiver 空壳不用动。
    - **实测价值**：值班人自动发现真实监控盲区（现网 prometheus 缺
      k3s-metrics 采集 job，真实节点宕机漏报）——拉模式+拓扑关联的
      "语义检查"能力是纯通知系统没有的。
    - **秒级实时**（可选后置）：233 侧放轻量信箱（webhook 写 233 本地，
      Vigil 拉取），office 电脑离线不丢告警（堆在 alertmanager，回来补拉）。
  - **2026-08-12 实测缺陷（数据层/事件层实施验证中发现）**：
    1. **cron agent 会话 LLM 调用异常慢**：cron 会话 API latency 30-183s/次
       （交互会话同 provider 2-6s），5 步分析要 5-10 分钟，曾挂死一次
       （进程死、execution 状态未回写成僵尸）。需排查 cron 会话的并发/
       超时配置（可能与交互会话共用 provider 池被限流或模型推理排队）。
       **排查记录（批次四，2026-08-12；只记录不修复，需公司机器实测）**：
       - **已知现象**：cron 会话每步 API latency 30-183s（交互同 provider
         2-6s）；5 步分析 5-10 分钟；曾整进程挂死（execution 状态未回写，
         僵尸记录）。本机无法复现（无生产负载与真实 provider）。
       - **假设**：① cron 会话与交互会话共用同一 credential pool / API key，
         provider 侧限流或推理排队；② cron 作业解析到与交互不同的模型/
         provider（`cron.model` / `cron.model_provider` 未 pin 时走全局
         model.default，可能落在慢队列）；③ 调度器并发（cron/scheduler.py
         的 `_running_agents` 多 agent 并发）与交互会话争抢 provider 配额。
       - **配置级待验证方向（已读代码确认存在这些键）**：`config.yaml` 的
         `cron.model` / `cron.model_provider`（job 级 pin > cron.model >
         model.default 的解析链）；`cron.provider`（builtin 时 ticker 在线程
         里跑，agent 调用在主线程/线程池——`future.result(timeout=...)` 只在
         执行/回写路径，无每次 LLM 调用的超时覆盖，观测到的 30-183s 更接近
         provider 侧排队而非本地超时）；交互与 cron 会话是否共享 provider
         连接池/限流桶。
       - **待实测清单（公司机器）**：cron 作业真实 `cron.model` 解析值 vs
         交互会话 `/model`；同一时刻交互+cron 并发的 provider 侧 latency
         分布；单跑 cron 作业（无交互并发）时 latency 是否回落。
    2. **cron script 路径提示误导**：创建时报"script 须放 ~/.hermes/scripts/"
       但实际解析到 ~/.vigil/scripts/（Vigil profile 根），放错位置导致
       首轮"Script not found"但 agent 仍被调起（自带纠错能力反而掩盖了
       脚本缺失，把监控盲区当成了告警分析）。脚本应放 ~/.vigil/scripts/。
    3. **alertmanager 无认证**：/api/v2/alerts 裸奔可读（prometheus 侧需
       basic auth），内网可达即任何人可查告警状态，后续应加
       --web.listen-address 内网绑定或反代认证。
    4. **Vigil 无常驻 gateway，cron 靠会话进程调度（可靠性缺陷）**：系统
       里常驻的 hermes-gateway.service(1428007) 是 upstream Hermes 的
       daemon（/usr/local/lib/hermes-agent，挂 personal profile
       /root/.hermes，仅监听 127.0.0.1:8642，无 IM 渠道），不调度 Vigil
       (/root/.vigil/cron) 的 job。Vigil 的 cron 实际由会话进程内的
       调度循环在跑——会话活着才有调度，且 cron status 会把 upstream
       的 gateway 误报为"自己的"（"Gateway is running PID 1428007"），
       造成"会自动触发"的假象。**修复方向**：Vigil 需要挂在自己
       profile 上的常驻调度载体（vigil gateway install 或常驻会话），
       gateway 检测需区分 upstream/own profile。
       **2026-08-12 实测证据（双安装 profile 重叠加剧此缺陷）**：
       - 心跳文件 /root/.vigil/cron/ticker_heartbeat 持续不存在（等 70s
         >60s tick 间隔仍未出现）→ 当前无任何 ticker 线程在跑；
       - executions 表 10:25 后无 builtin 记录、jobs.json next_run 停在
         10:25 不再推进 → 自动调度确实断档（此前 09:43/10:05 两次 builtin
         是 web_server.py 桌面 ticker _start_desktop_cron_ticker 跑的，
         依赖碰巧打开的会话进程，时灵时不灵）；
       - `vigil cron status` 仍报"✓ Gateway is running, will fire
         automatically"——只看 gateway PID 存活，不看 ticker 心跳
         （源码 #32612/#32895 已为此加心跳检测但 status 输出未用），
         假健康误导排障。
       **2026-08-12 晚设计定稿（采集/分析分层，替代 gateway daemon 方案）**：
       修复方向不是跑 gateway daemon，而是拆两层——**采集（确定性代码，
       不需要 LLM）与分析播报（需要 LLM）分离**：
       - 第一层：systemd --user service（**服务名 vigil-watch.service**，
         避开被 upstream Hermes 占用的 hermes-gateway.service；开机自启、
         journald 日志、失败自动重启，不依赖任何终端会话）→ 每 5 分钟拉
         alertmanager `/api/v2/alerts` + prometheus `/api/v1/alerts`（vault
         basic auth）→ 有告警写入本地队列 `~/.vigil/watch/inbox/`，无告警
         静默退出（零成本）；
       - 第二层：agent 会话启动/常驻时消费 inbox → 拓扑关联/分级/播报 →
         处理完标记清空。session 关闭不影响采集（系统层永远在跑），session
         只是"消费队列的分析工"；办公电脑关机也不丢告警——告警堆在
         alertmanager/233 侧信箱，回来补拉（产品文档 0812 拉模式修正的
         "回来补拉"形态，不是缺陷是设计）。
       - 与"固化边界"原则一致（#12 AIOps 蓝图）：采集是确定性代码固化进
         系统层，LLM 只留判断和编排。
       - cron agent 会话 LLM 调用异常慢（缺陷 1）与本方案相关：若分析播报
         走 cron agent 会话，需先解决 30-183s latency 问题（见 #12 缺陷 1）。
       **已实施（批次四，2026-08-12）**：
       - `tools/watch_collect.py` 采集层 + `vigil watch install|uninstall|status`
         （systemd --user，服务名固定 vigil-watch.service，enable --now 开机自启、
         Restart=on-failure，非 systemd 平台 install 明确报错不假装成功）+
         `hermes_cli/watch_collect_loop.py` 常驻循环（300s 间隔，SIGTERM 优雅
         退出）；`ops.watch.enabled: false` 或 alertmanager 未配置 → 零行为。
       - `tools/watch_tools.py` `watch_digest()` 第二层消费（toolset=watch，
         不注册 _HERMES_CORE_TOOLS，与 prom 同策略；mark_processed 显式消费）。
       - `vigil cron status` 假健康修复：gateway PID 按 HERMES_HOME/--profile
         归属过滤（upstream hermes-gateway 一律不算）+ ticker 心跳真实状态
         分行显示（"Gateway: 真实状态 | Ticker: 真实状态"），采集常驻接管时
         提示以 `vigil watch status` 为准。
       - 核销：`test_watch_collect.py`（7）+ `test_watch_tools.py`（7）+
         `test_watch_cmd.py`（8）+ `test_cron_status_health.py`（11）+ cron
         既有套件全过。
    5. **双安装 profile 重叠（架构级，本案例所有混乱的总根源）**：
       本机同时存在**两套同源但独立安装**：
       - upstream Hermes 0.15.1：/usr/local/lib/hermes-agent/venv +
         /usr/local/bin/hermes，HERMES_HOME=/root/.hermes，4 个 profile
         （daily/internal/personal/test-profile），hermes-gateway.service
         挂 personal；
       - Vigil（fork）：/opt/vigil-venv + /usr/local/bin/vigil，
         HERMES_HOME=/root/.vigil，default + ops 两个 profile。
       重叠症状：cron status 把 upstream gateway 当自己的（见缺陷 4）；
       gateway 检测/端口（8642）不区分安装归属；systemd 服务名
       hermes-gateway.service 被 upstream 占用，Vigil 装自己的会撞名；
       scripts 路径提示指向 ~/.hermes/scripts 实际解析 ~/.vigil/scripts
       （缺陷 2）；`vigil cron list` 与 `hermes cron list` 各看各的
       jobs.json 但用户视角只有一个"cron"。**产品影响**：运维人机器上
       同时跑 Hermes 和 Vigil 是真实场景（Vigil 从 Hermes 演进而来），
       profile/daemon/端口/服务名必须显式隔离，否则所有"常驻"类功能
       （cron/gateway/webhook）都会出现"以为在跑其实没跑"的假象。
       **修复方向（排期）**：安装时分配独立 HERMES_HOME 语义（Vigil
       专属 home，不与 ~/.hermes 混居）、gateway 检测按安装归属过滤、
       systemd 服务名用 vigil-gateway.service、cron status 展示 ticker
       心跳真实状态（含 provider 判定）。
    5. **"秒级实时"的正确形态是集群侧信箱 + 出站订阅，不是 gateway 接收**：
       办公电脑形态下 gateway 无法解决 webhook 接收（无稳定可达地址，
       "开接收端口"= "给自己装 postfix 收邮件"，推不进来）。秒级 =
       alertmanager webhook → 233 上轻量 HTTP 信箱（事件推进集群本地）
       → Vigil 短轮询/长连接出站订阅。拉取方向不变（出站），事件到达
       即时。gateway 仅在 Vigil 部署于服务器（固定可达地址）时才有
       接收意义。

- **已实施（批次二，2026-08-12）**：
  - **发现引擎**（新文件 `tools/topo_discovery.py`，零侵入独立模块）：
    `discover_host(host, env, creds, runner=)` 经 SSH 扫 docker ps/compose ls、
    kubectl（可选）、ss -tlnp、nvidia-smi，输出 v0.2 schema 片段（第一层 host
    行 + 第二层 services 索引 + 第三层详情草案），带 `source: discovered` +
    `last_verified: 今天` + `needs_review: true`（人工确认前不落盘为权威）。
    只读：本模块只采集，落盘由 `write_discovery()`（用户确认后）执行。
  - **凭据不落明文**：密码经保险箱（`tools/credential_vault`）+ SSH_ASKPASS
    （0600 脚本读保险箱文件）注入，命令串/环境/日志无明文；默认 runner 走
    key/agent（BatchMode）。
  - **无半截数据**：SSH 级失败抛 DiscoveryError 直接中止；单探针失败（docker/
    kubectl 未装）记为 skipped，不影响其余探针。
  - **引导式入口**：`vigil topo-discover --host <ip> --env <env> [--user] [--key]
    [--dry-run] [--force] [--yes]`（subcommands 解析器 + main.py 接线，仿
    ops-init）。交互密码经 getpass 入保险箱；展示实体清单 → 用户确认 → 落盘
    （v0.2 结构：写 hosts/<hostname>.yaml + 追加 topology.yaml hosts 段 +
    entities/<name>.yaml 草案）。`--dry-run` 只展示不落盘；`--force` 覆盖已存在
    host 条目；现有 v0.1 topology.yaml 拒绝覆盖（明确错误提示先迁移）。
  - **核销方式**：`tests/tools/test_topo_discovery.py`（10 例，mock ssh 输出：
    服务/端口/镜像映射、无明文断言、dry-run 不写、落盘结构可被 topo_tools
    校验、v0.1 拒绝、ssh 失败无半截数据）+ `vigil topo-discover --help` 正常。

### 13. 开箱即用 UI 壳（中间市场的产品形态）— ⬜ 未实施（待排期，远期，依赖 #12）

- **为什么**：CLI 是运维工程师友好形态，但中间市场（秘书/机械背景用户）看到
  `vigil ops-init --env prod` 就放弃。中间市场需要 Web 界面或桌面应用：
  填服务器 IP → 自动发现 → 点按钮说话。CLI 是内核，UI 是壳。
- **待改方向**：
  1. Web 控制台（复用/扩展现有 dashboard）：服务器接入向导（IP + 凭据 →
     自动发现 → 拓扑展示）、对话界面（同 CLI 内核，不同前端）。
  2. 面向"非技术"的引导：无命令操作，全部点选。
  3. 远期，与 token 差价模式（用户订阅不配模型 key）配套。

### 11. 交互层应以 environment 为核心概念，而非 profile（`/env prod` 切换）— ✅ 已实施（2026-08-10，25c8cb5）

- **为什么**：用户实测 `/env prod` 报 Unknown command——Vigil 的 CLI 命令体系
  （commands.py）继承 Hermes 的 profile 概念，没有环境切换命令。但 Vigil 的
  数据层已是 environment 语义：权限矩阵 `_active_env()` 按 env 判定（config
  `role`）、banner 显示 ENV badge（test/uat/prod）、ops-init `--env` 参数。
  **数据层认 env、交互层认 profile，两层割裂**。用户判断：profile（谁在用，
  Hermes 个人场景隔离）不适合 Vigil，Vigil 核心应是 environment（操作哪里，
  运维环境隔离）——"我在 prod 还是 test，决定了能干什么"。
- **已实施**：`/env` 命令（commands.py 注册 + cli.py handler，无参显示
  当前/可用列表、带参校验切换）+ `ops.environments` 可自定义列表（ops-init
  生成默认三档，`--env` 去 choices 支持自定义名如 bare_metal_prod，自动追加
  定义并告警拓扑不同源）+ 权限矩阵按 env 名查表（行为由 role 决定，
  `_matrix_row`）+ banner ENV badge 随切换更新（写 config
  `ops.permissions.env`，profile 保留职责分离）。切换持久化写 config
  （`_active_env()`/banner 都从 config 读），跨会话保留。
- **设计约束（已并入实施）**：env 必须可自定义，不能硬编码 test/uat/prod——
  真实运维环境是二维组合：**物理环境（local/cloud/bare_metal）× 等级
  （uat/prod）**——如 bare_metal_uat / bare_metal_prod / local / cloud。
  `ops.environments: [{name, isolation, role}]` 定义列表；权限矩阵按 env 名
  查表（行为由 isolation/role 决定，不依赖名字是 test/uat/prod）；拓扑
  environments 段与 config 的 env 定义同源（实施时告警不同源，同源待做）。

### 8. 缺少主流 IM 平台 adapter — ✅ 已核实（2026-08-12）：adapter 全部在位，无需同步开发

- **核实结论（2026-08-12 修正本条记录）**：本条原记录"Vigil 0.1.6 打包裁掉了
  10+ 个 adapter"**已被证伪**。`diff -rq` 上游 `hermes-source_1/plugins/platforms/`
  ↔ Vigil `plugins/platforms/`：目录清单完全一致（22 个 adapter 目录 / 63 个
  py 文件，与上游同数），0 个 "Only in"（无缺文件/多文件）；17 个文件有内容
  diff，全部为品牌化文案（hermes→vigil 命令名，如 `hermes setup` → `vigil
  setup`，抽查 telegram/slack/feishu 均为纯文案替换）。→ 从"要同步开发"变成
  "已核实无需开发"。
- **启用路径核实**：`hermes_cli/plugins.py` 扫描 bundled `plugins/platforms/`，
  读 `plugin.yaml`（kind: platform），向 `gateway/platform_registry.py` 注册
  **延迟加载器**（`_register_deferred_platform`，按需 import 避免每次 CLI 启动
  加载 ~20 个平台 SDK）；`gateway/run.py` 启动时查 `platform_registry` 并
  `create_adapter()` 实例化。telegram/slack/feishu 等 adapter 的
  `__init__.py::register(ctx)` 自动完成注册，**无需额外注册步骤**——启用走
  `vigil gateway` 配置（可选依赖 try/except 包裹，装则启用）。
- **无代码改动**（未发现真缺文件，按任务要求不自行复制上游代码）。

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

### 9. 自动化运维触发源：cron 已有，缺事件驱动入口（告警 → runbook）— ✅ 已实施（2026-08-12，批次四：值守层采集/分析分层落地）

- **为什么**：Vigil 已有完整 cron 系统（cron/jobs.py + scheduler.py，
  gateway 本身是常驻 daemon）——定时触发已解决。但运维自动化的触发源不该
  只有定时，还应有**事件驱动**：Prometheus 告警、服务状态变化、webhook 打到
  Vigil → 自动拉起对应 runbook。cron 是"到点就查"，事件是"出事才动"——
  后者才是运维 agent 的核心价值，当前缺失。
- **待改方向**（2026-08-12 修正：接收形态从 webhook 改为拉模式巡检，见 #12）：
  1. 告警入口：cron 定期拉 Alertmanager `/api/v2/alerts` + Prometheus
     `/api/v1/alerts`（vault basic auth）→ 有告警才注入 agent。
  2. 告警 → runbook 匹配（事件源/告警名 → runbook 名）映射放 config.yaml，
     不硬编码。
  3. 分级处置：test 可自动处置，prod 高危走审批。
- **已实施（批次四，2026-08-12）——值守层落地（采集/分析分层，替代 gateway
  daemon 方案，见 #12 缺陷 4 设计定稿）**：
  - **第一层采集（确定性代码，不需要 LLM）**：新文件 `tools/watch_collect.py`
    `collect_once()`——拉 Alertmanager `/api/v2/alerts`（复用 prom_tools 的
    endpoint/凭据/10s 硬超时/vault basic auth 注入），有活跃告警原子写
    `~/.vigil/watch/inbox/<timestamp>.json`（`{collected_at, alerts, processed}`
    契约），无告警不写；按 alertname+instance 对未处理 inbox 去重（恢复后再
    复发允许新条目）；采集失败/配置缺失只写 `~/.vigil/watch/errors.log` 一行
    不抛异常（systemd 重启循环不能炸）；`ops.watch.enabled: false` 或
    alertmanager 未配置 → 零行为（硬约束 3）。
  - **常驻服务**：`vigil watch install|uninstall|status` 子命令 +
    `hermes_cli/watch_collect_loop.py` 常驻循环（每 300s 一次，SIGTERM 优雅
    退出）→ systemd --user unit **服务名固定 vigil-watch.service**（硬约束 7：
    与 upstream hermes-gateway 无任何关联），enable --now 开机自启、Restart=
    on-failure；`watch status` 报真实状态（active/inactive + 上次采集 +
    inbox 未处理数），不报假健康；非 systemd 平台 install 明确报错 +
    "WSL 可改用 cron 或常驻终端"，不假装成功。
  - **第二层分析（需要 LLM）**：新工具 `watch_digest()`（tools/watch_tools.py，
    toolset=watch，不注册 _HERMES_CORE_TOOLS，与 prom 同策略）——读 inbox
    未处理条目 → 紧凑摘要（alertname/severity/instance/采集时间 + 拓扑关联：
    instance 命中拓扑实体给出实体名/env，未命中标注"不在拓扑"）→ agent 分级/
    播报/runbook 匹配；`mark_processed=True` 显式消费（契约写进 docstring）；
    session 关闭队列堆积，下次启动补处理（"回来补拉"形态）。
  - **未做（登记跳过原因）**：任务 2B「会话启动自动调 watch_digest 在欢迎区
    显示一行提示」——需要碰会话生命周期核心（欢迎区渲染 / 会话启动路径 /
    prompt 缓存边界），按硬约束 1 跳过，只做 2A（手动工具）。消费入口 =
    agent 主动调 watch_digest；后续若做自动提示，须在不碰 conversation_loop
    的入口（如 CLI 欢迎横幅静态区）另排期。
  - **告警 → runbook 映射/分级处置**（待改方向 2/3）：未实施——依赖 agent
    语义层（watch_digest 输出已含实体/env，runbook 匹配由 agent 做），
    后续如需固化可加 config 映射表。
- **核销方式**：`tests/tools/test_watch_collect.py`（7 例：有告警写 inbox/
  无告警不写/去重/processed 后允许复发/disabled 零行为/缺 alertmanager 只写
  errors/失败不抛）+ `tests/tools/test_watch_tools.py`（7 例：摘要+拓扑关联/
  无待处理/mark_processed 消费/命中未命中两态/check_fn 门控）+
  `tests/hermes_cli/test_watch_cmd.py`（8 例：unit 内容/平台守卫/uninstall/
  status 真实状态/服务活跃辅助函数）+ cron 相关回归全过。
### 10. Prometheus 探查应为内置工具，而非 LLM 自写 curl/PromQL — ✅ 已实施（2026-08-12，批次三）

- **为什么**：Vigil 定位是运维 harness，但 tools/ 目录**没有任何 prometheus/
  metrics 内置工具**——探查 Prometheus 数据靠 LLM 自己拼 curl + PromQL。
  这不符合定位：就像不该让 LLM 自己封装 kubectl 一样，PromQL 查询、指标
  解释、告警关联应是基础内置能力，LLM 只做语义层。
- **改了什么**：
  1. **新文件 `tools/prom_tools.py`**：`prom_query(query, step, duration)`（PromQL
     即时/range 查询，结果解析为紧凑结构化文本 series → 时间点摘要，截断上限
     20 series / 30 点）+ `alert_query()`（Alertmanager `/api/v2/alerts`，活跃
     告警摘要 alertname/severity/instance/labels；无告警明确返回"无活跃告警"）。
  2. **配置**：`hermes_cli/ops_init.py` `_CONFIG_TPL` 的 `ops:` 块加
     `prometheus: {endpoint, alertmanager, vault_path}` 模板段；`toolsets.py`
     `TOOLSETS` 加 `"prom"` 条目（纯数据 toolset）；`hermes_cli/tools_config.py`
     `CONFIGURABLE_TOOLSETS` 加 `("prom", "📊 Prometheus 监控", ...)`（`vigil
     tools` 列表可见）。
  3. **门控（零 footprint）**：check_fn = `ops.prometheus.endpoint` 非空才可用；
     **不**加入 `_HERMES_CORE_TOOLS`、**不**默认注册（ops-init 模板
     `platform_toolsets.cli` 不加 prom），用户启用走 `vigil tools` / 显式
     `platform_toolsets`。endpoint 为空 → 工具不出现在 schema（硬约束 3）。
  4. **防幻觉 + 防挂死**：PromQL 语法预校验（白名单字符集 + 拒 shell 元字符
     `; | & \` $() ${}` + 拒 shell 命令前缀如 curl/kubectl/docker + 必含表达式
     形态），非法输入明确报错且不发请求；HTTP 失败/超时返回**原始错误**不编造
     值；网络硬超时 10s。
  5. **凭据注入**：`vault_path` 指向本机保险箱（`tools/credential_vault`）JSON
     凭据条目 `{"user": ..., "pass": ...}`，请求时解析为 Basic Auth header
     注入（读取即登记 vault 来源），工具输出/日志无明文。
- **核销方式**：`tests/tools/test_prom_tools.py`（18 例：结构化摘要 / 非法
  PromQL 不发请求 / HTTP 500 / 超时 / 非 JSON / Prometheus error status /
  alert 两态 / check_fn 门控 / vault header 注入 + 输出日志无明文）；ops-init
  模板渲染 + tools_config/banner 套件（60 例）全过。

### 14. topo/runbook 工具集应默认启用——默认 profile 开箱即用 ops 能力 — ✅ 已实施（2026-08-12，批次一）

- **为什么**：Vigil 是运维 agent harness，但 **topo_query/topo_update/runbook_load
  这些核心运维工具只注册在 ops profile**（`profiles/ops/config.yaml` 的
  `platform_toolsets.cli: [hermes-cli, topo, runbook]`），默认 profile 的
  `platform_toolsets.cli` 里根本没有 topo/runbook。后果：即使用户已经通过
  ops-init 生成了完整拓扑数据（topology.yaml + entities/ 都在），直接
  `vigil`（default profile）依然显示"未启用 ops harness"，topo_query 完全
  不可用，必须 `vigil -p ops` 才能用。**数据存在 ≠ 能力可用**——工具集没
  注册，agent 的可用工具列表里就没有 topo_query。
- **与第 1 条的关系**：第 1 条解决"ops-init 是启用能力的前置门槛"（init
  不该是必经之路，已实施：check_fn 数据存在性门控）；本条解决"即使数据就位，
  默认 profile 也未注册运维工具集"（能力缺省不启用，**未实施**）。两条叠加
  才是完整的"装上即用"——数据存在性门控 + 工具集默认注册。
- **2026-08-12 代码实测（合并账本时复核）**：`_HERMES_CORE_TOOLS`（62 个
  核心工具）不含 topo_query/topo_update/runbook_load/runbook_checkpoint；
  `_get_platform_tools({'platform_toolsets': {}}, 'cli')` 返回的工具集里
  **没有 topo/runbook**（实测 topo_query in tools: False）；`_RECENTLY_SHIPPED_TOOLSETS`
  只有 bfl，无 topo/runbook 特例。→ #14 确认未实施。
- **验证（2026-08-11 实测）**：升级 0.1.8 后 `/root/.vigil/profiles/ops/`
  拓扑数据完好（topology.yaml 9421B + entities/ 全在），但直接 `vigil`
  读 `/root/.vigil/config.yaml`（无 ops 段、toolsets 无 topo/runbook）→
  banner 显示 off；`vigil -p ops` 才正常加载。对比 config：
  - 默认 profile：`platform_toolsets.cli` 无 topo/runbook，无 `ops` 段
  - ops profile：`platform_toolsets.cli: [hermes-cli, topo, runbook]` +
    `memory.provider: topo` + `ops` 段全 enabled
- **待改方向**：
  1. 默认 profile 缺省启用 topo/runbook 工具集（topo_query/topo_update/
     runbook_load 是 Vigil 的核心工具，应像 terminal/file 一样随产品默认
     注册，而不是靠用户手写 platform_toolsets 或切 profile）。
  2. 与第 1 条的数据存在性门控对齐：工具集默认注册，能力按 topology.yaml
     是否就位自动切换（有数据 → 正常服务；无数据 → 提示初始化而非静默不可用）。
  3. memory.provider 默认 topo（或至少不因默认 provider 阻断拓扑查询）。
  4. 迁移路径：老用户（ops profile 已有数据）升级后直接 `vigil` 应能读到
     现有拓扑，无需手动软链/改 config。

- **2026-08-11 实证补充（工具缺失 → agent 退化为直接编辑文件，绕过权限矩阵）**：
  - **现象**：用户要求"更新 233 拓扑信息"，本会话（默认 profile，topo 工具
    未启用）无 topo_update 可走，agent 在 reasoning 里明确选择
    "直接编辑实体文件 + topology.yaml"，并自我开脱"ops.permissions.env=test
    审批宽松，用户明确要求，直接做"——实际改的是 prod-* 实体，跨环境操作
    本该更严，判断反了。
  - **危害**：权限矩阵只保护 topo_update 工具调用（execute/approve/deny
    门控），不保护文件编辑。工具缺失时 agent 绕道 patch yaml = 矩阵形同
    虚设，且这一路径无审计戳。
  - **原则**：**fail-closed**——topo 工具不可用时（未启用/加载失败/配置
    缺失）应拒绝拓扑变更并提示用户启用工具或切 ops profile，不得退化为
    直接编辑事实文件。工具层是权限矩阵的载体，载体缺失 = 防御失效，不是
    "换个方式达成同样目的"。
  - **待改方向**（并入第 1/14 条实施时一并处理）：能力门控检查工具集可用性；
    检测到拓扑类操作（更新/删除实体）但 topo_update 不可用时，返回
    "工具未启用，请运行 vigil -p ops 或启用 topo 工具集"并中止，禁止
    fallback 到文件编辑；文件编辑路径也应在 ops profile 内受 runbook/
    SOP 约束。

- **已实施（批次一）**：
  - `hermes_cli/tools_config.py` `_get_platform_tools`：无显式配置分支对
    cli 平台自动加入 topo/runbook 工具集（显式 `platform_toolsets.cli` 列表
    权威，不覆盖用户选择；仅 cli，不污染消息平台）。工具可用性仍由 check_fn
    数据存在性门控（无数据零 footprint）。
  - `hermes_cli/config_defaults.py`：`memory.provider` 默认 `topo`；
    `plugins/memory/topo` 的 `is_available()` 改为数据存在性门控（缺省→有
    topology.yaml 即注入 TOPO 段；显式 false 关闭；非 ops profile 无数据时
    零影响）。显式 `""` 仍是关闭外部 provider 的语义。
  - 迁移路径（老用户 ops profile 数据）：新增 `tools/ops_data_home.py`，
    default profile（HERMES_HOME=<root>）无数据时回退 sibling ops profile
    （<root>/profiles/ops）——topo_query/topo_update/runbook_load/check_fn/
    topo provider 统一走该解析（读写同一数据 home，不分裂）。单测：
    tests/tools/test_ops_tools_default_registration.py（10 例）+
    test_topo_provider.py 新增 1 例。

### 15. read_file binary 误判（中文采样截断伪影）— ✅ 已实施（2026-08-11 正式修复 42cd841，随 v0.1.9 后版本带上）；agent 自主修改自身安装文件的 gate 已一并落地

- **bug 现象**：read_file 报 "runbook 编码损坏"，实际文件完好。根因：
  `_is_likely_binary` 用 `head -c 1000` 采样 + lossy 解码（errors="replace"），
  中文 runbook 第 998 字节恰好切在 3 字节 UTF-8 字符"程"（e7 a8 8b）中间，
  截断尾巴变 U+FFFD，被 `if "\ufffd" in sample` 误判为 binary → 文件损坏假报。
- **已实施（42cd841，2026-08-11，正式修复进源码）**：`tools/file_operations.py`
  `_is_likely_binary` 遇 U+FFFD 时用 Python rb 读原始字节做严格解码——
  真损坏抛 UnicodeDecodeError（保持原判定），截断伪影能干净解码（放行，
  继续走非打印字符比例检查）。补单测（1000 字节边界切断多字节字符用例：
  997/998/999 边界不误判、真损坏仍判 binary）。测试 123 全过。
- **一并落地（site-packages 写入 gate）**：`tools/approval.py` + `tools/file_tools.py`
  对写 site-packages/安装目录（>/tee/sed -i/cp/pip --force-reinstall）加硬 gate，
  阻断 agent 自主修改自身安装代码；普通操作（pip install requests、日志 sed、
  读文件）放行。测试覆盖 38+69 行新增用例。
- **已知残留边界**：`patch -p1 < diff`（路径藏在 stdin diff 内）是终端侧残留
  向量——文件工具硬拒已覆盖主体，此残留可接受（Codex 实施时自声明）。
- **历史（已回滚的热补丁，留档）**：2026-08-11 当天曾由会话热补丁
  site-packages（遇 U+FFFD 用 rb 严格解码），用户当场纠正"文件没问题就不该改
  Vigil 自身"后回滚；备份 /root/.vigil/file_operations.py.bak.20260811_103303
  （= 原始版本，可删）。正式修复 42cd841 已进源码，热补丁不再需要。
- **边界原则（与 #14 同源，已固化）**：agent 对自身安装文件/源码的修改必须经
  用户明确同意——site-packages/源码仓库写入设硬 gate（拒绝或审批），默认 deny。

### 16. 敏感凭据输入的生命周期管理——对话输入不可避免，风险控制放在"输入之后" — ✅ 第 1+2 层已实施（2026-08-12，批次一）；第 3 层降级 SOP 辅助层

- **背景**：prod-rotate-wangwp10 runbook 执行时，用户通过 clarify 输入明文密码
  （旧/新值）。会话记录必然落盘——"凭据不落盘、不进日志"的承诺在对话通道下不
  成立，与早上的 reasoning 泄露（值进推理流）是同一类风险的另一入口。
- **权衡**：让非技术用户"自己 export 环境变量 / 写 600 权限文件"等于要求先会
  运维才能用运维工具——对话输入是唯一对普通用户可行的入口（可用性）。
  **不教育用户，让系统兜底**：输入方式保留，风险控制转移到"输入之后的处理"。
- **方案（产品化，三层）**：
  1. 用户直接输入（保留可用性，无感知）。
  2. Vigil 收到后自动写入本机 600 权限凭据文件/保险箱（安全存储）。
  3. 会话记录落盘前对该输入做 redact / 替换占位符（不留明文）。
  4. 生命周期：任务结束凭据从保险箱清除或标记过期。
- **已实施（批次一，第 1+2 层）**：新增 `tools/credential_vault.py` 凭据保险箱——
  `store(name, value)` 写 `<HERMES_HOME>/secrets/<name>`（权限 600 + owner 校验，
  name 白名单防路径穿越）、`retrieve(name)`（读取即登记 vault 来源）、
  `expire(name)`（删除 + 标记过期）、会话内来源登记（user/vault + 时间戳，
  供 #25 的 sudo guard 查询）。SOP 层（不改核心）：对话输入疑似凭据时由系统
  存入保险箱、会话记录不保留明文，agent 只引用名字、命令串过 redact（#21）。
  第 3 层"会话记录落盘 redact"：触碰 conversation_loop，按硬约束 1 降级为
  SOP 辅助层（不硬改核心）。单测：tests/tools/test_credential_vault.py（8 例）。
- **未实现前的最低兜底（已执行）**：
  - ~/.vigil 数据目录权限锁 600（防其他用户读会话记录）。
  - 会话记录不参与同步（MSC 通道确认日志不离机）。
  - 敏感任务结束后提供"清理该会话记录"指令（一键打码或删除）。
- **runbook note 分层**（不写死"必须环境变量"）：默认对话输入（系统自动脱敏
  存储），高级/高敏场景用环境变量或 600 临时文件。
- **关联**：凭据处理是"让 agent 在企业环境安全干活"卖点的核心模块，应作为
  产品特性排期，而非依赖用户自觉。

- **2026-08-11 实证补充（clarify 二次明文 + agent 自探测绕过）**：
  - 当日在 234 sudo 机制研究中，用户又两次通过 clarify 输入密码明文（含刚轮换
    后的有效新值），均进入会话记录——第 16 条问题当日即复发。
  - **边界问题**：sudo -S 被安全策略拦截后，agent 自行探测多条绕过路径
    （ssh -tt、sudo -n、sudo -l、/run/sudo/ts 枚举），违反"凭据持有者=用户，
    遇到凭据问用户"原则；.env SUDO_PASSWORD 方案未验证远端适用性即推荐
    （该机制只作用于本机 Hermes 进程的 sudo，不穿透 ssh 远端）。
  - **原则固化**：安全策略拦截的路径不得自行探测绕过；被拦即停，问用户或走
    "用户手动执行"路径。

### 17. sudo timestamp 研究 + 安全策略可用性缺口 — 🟡 研究完成，233/234 落地见 #18，其余待排期

- **背景**：prod 密码轮换后用户观察到"已登录会话在窗口期内仍可免密提权"。
  研究 234 sudo/ssh 机制，目标：窗口期控制。
- **机制结论**：sudo timestamp_timeout 默认 5 分钟（RHEL 系），
  timestamp_type=tty（per-tty）；timestamp 只记认证时刻，密码变更不影响已存在
  timestamp；/run/sudo/ts 为 tmpfs，机器重启自动清空；新 ssh 连接（新 tty）
  无共享 timestamp，必须重新认证。
- **方案修正（对 Vigil 原方案的更正）**：
  1. 用户目标"15 分钟窗口"是**放宽**（默认 5 分钟更严）——安全方向应保持或
     缩短；业务确需长窗口再配 `Defaults timestamp_timeout=15`。
  2. 根治"改密码后旧会话仍可提权"：密码轮换后清空 timestamp——root 执行
     `rm -rf /run/sudo/ts/*` 或逐已登录会话 `sudo -k`；已加入
     prod-rotate-wangwp10 的 verify 步（见 #18）。
  3. **保持 timestamp_type=tty（默认），不得改 global**——global 跨会话共享
     timestamp，更宽松，与安全目标相反（Vigil 原方案曾把 global 标为"更严格"，
     逻辑错误）。
- **安全策略可用性缺口（待设计补丁）**：sudo -S 管道一刀切拦截（防暴力）导致
  合法用户提供正确密码也无法走 agent 通道，只能手动执行。拦截符合设计意图，
  但缺"用户明确授权后临时放行"的合法通道（见 #25）。

### 18. sudo timestamp 修复落地 + 轮换 runbook 补 sudo -k — ✅ 已执行（2026-08-11）

- **执行结果**：233 + 234 两台已修复——写入 `/etc/sudoers.d/timestamp` =
  `Defaults timestamp_timeout=5`（用户实选 5 分钟，非最初提的 15），visudo -c
  校验通过，`sudo -k` 后 sudo 重新要求密码（验证正确）。235/236/237/238 按
  用户要求未动（仍为 -1）。
- **根因确认**：`/etc/sudoers` 里 `Defaults env_reset,timestamp_timeout=-1`
  才是"改密码后旧会话仍免密"的真正原因（timestamp 永不过期）；
  `/etc/sudoers.d/wangwp10 = ALL(ALL:ALL) ALL` 是标准规则本身要密码，
  不是 NOPASSWD。
- **sudo -k 身份陷阱（本次踩坑）**：root 下 `sudo -k` 清的是 root 的 timestamp，
  不碰 wangwp10 的——清空目标用户 timestamp 必须在目标用户自己的 shell 执行。
  root shell 里清完再测 wangwp10 的 sudo 仍免密，一度误判"配置没生效"。
- **落地到 runbook**：prod-rotate-wangwp10 的 verify 步已加第 3 条命令——6 台
  轮换后 `ssh ... 'sudo -k'` 清空全部旧 timestamp，verify 条件加"6 台 sudo
  timestamp 已清空"；pitfall 注明"漏掉 sudo -k 会让改密前已建立的 timestamp
  继续免密（-1 时永不过期）"。runbook_load 验证 LOAD_OK。
- **待办**：235-238 如需收紧，流程与 233/234 相同（用户暂不动）；5 vs 15 分钟
  待用户最终确认。

### 19. runbook 的 vault 步骤应默认询问用户，不得默认更新 — ✅ 已应用（2026-08-11），schema 层待排期

- **为什么**：prod-rotate-wangwp10 原 vault 步是"自动同步更新 CSNDC/maas ssh_key
  字段"。实际执行时用户选择不更新 vault（凭据自行管理），并要求改为**每次执行
  现场询问**。原因：vault 字段语义历史上多变（ssh_key / ssh_key_new/sudo_new
  都曾存过已作废的中间值），agent 无法判断当前字段语义是否与流程意图一致，
  擅自更新会覆盖用户手工维护的凭据状态。
- **已应用**：runbook vault 步改为——title"询问用户是否同步更新 vault"，
  commands 第一条是"询问用户是否写入，明确同意后才执行"，gate"用户明确确认后
  才可执行；默认不更新"，pitfall 记录字段语义历史教训。
- **通用原则**：凡 runbook 涉及**外部凭据存储**（vault / secrets manager /
  密钥文件）的写操作，schema 层面应默认"询问"，除非 runbook 显式标注 auto。
- **待排期**：runbook schema v0.2 增加 `vault_write: ask|auto` 字段（与 #31 模板化同批）。

### 20. 凭据轮换流程的方法论教训 — ✅ 原则已固化（2026-08-11，行为约束层）

- **越界修改自身安装代码**：read_file 误判时 agent 自主 patch 了 site-packages
  的 file_operations.py（第 15 条），用户纠正"文件没问题就不该改 Vigil 自身"后
  回滚。原则：**agent 对自身安装文件/源码的修改必须经用户明确同意**，与第 14 条
  "工具缺失不得退化为直接编辑文件"同源——两条都是"agent 不应有绕开权限矩阵的
  路径"。代码层 gate 已落地（#15 一并实施）。
- **凭据获取应先问用户，不自行探测**：sudo -S 被拦截后 agent 尝试了 ssh -tt /
  sudo -n / sudo -l / /run/sudo/ts 枚举等多条绕过路径（第 16 条已记录），用户
  两次纠正"你不应该自己找办法而是应该询问我"。原则固化：**凭据持有者=用户，
  遇到凭据问用户；安全策略拦截的路径不得自行探测绕过，被拦即停**。
- **不再询问"是否存 skill"**：用户明确"我是 Vigil，运维流程沉淀走 runbook，
  不存 skill"（对应第 4 条待改项）。agent 在运维场景完成任务后不应再问
  "要不要存成 skill"。

### 21. grafana datasource 认证修复——命令文本再次携带密码明文 — ✅ 已实施（2026-08-12，批次一，并入 #5/#16 凭据红线）

- **现象**：grafana-datasource-auth 修复过程中，agent 两次在**给用户的命令文本**
  里嵌入了 prometheus 密码明文（一次是 PUT datasource JSON，一次是 patch
  ConfigMap 命令），用户两次纠正"你又把密码带出来了"。
- **与第 16 条/第 20 条的关系**：第 16 条是"对话输入"泄露，第 20 条是"agent
  自探测"问题，本条是**输出通道**——agent 生成"给用户手动执行的命令"时把凭据
  直接拼进命令串。这是继 reasoning 泄露、工具调用行泄露之后的新泄露面：
  **命令文本泄露**。
- **根因**：密码从 vault 读出后存在 python 变量，agent 在构造命令字符串时直接
  f-string 内插，没有意识到该字符串会完整显示给用户。
- **本次实际做法（修复后）**：密码只经 vault→python 变量→写入本地 yaml 文件→scp；
  给用户的 apply 命令不含任何密码（密码在文件里）；验证用哈希比对（sha256[:16]）
  而非明文。
- **待改方向（并入第 5/16 条的凭据红线）**：
  1. runbook 的 commands 字段支持 `<vault:path/field>` 占位符，执行时由 runbook
     工具从 vault 解析注入，agent 构造命令时永远拿不到明文。
  2. agent 输出层：检测到命令文本含"疑似凭据字符串"（长度≥16 的高熵串）时
     打码/警告。
  3. 原则固化：**"给用户执行的命令"与"agent 自己执行的命令"同等对待——凭据
     一律占位符/文件路径，不回显**。

- **已实施（批次一）**：
  1. runbook schema v0.1 兼容扩展：commands 支持 `<vault:path/field>` 占位符，
     `runbook_load` 返回时描述化（"«vault:path — 凭据引用（执行时从保险箱/vault
     读取，明文不进会话记录）»"），**永远不返回明文**；带 vault 引用的 runbook
     附 `vault_refs` 计数 + 提示。
  2. `agent/redact.py` 新增命令文本内联凭据打码（`_redact_command_inline_credentials`）：
     已知凭据 flag（--from-literal=KEY=VAL / --password / --token / --secret /
     -u user:pass / -p）后的值打码；命令上下文里的高熵裸 token（≥16 字符、混合
     字符类、香农熵 ≥3.2）兜底打码；纯 hex/UUID/pod 名/端口/长 URL 排除不误伤。
     复用 redact_sensitive_text 现有机制（#5 三路径之外的第四出口：命令文本）。
  单测：tests/tools/test_runbook_vault_refs.py（2 例）+
  tests/agent/test_redact_command_text.py（17 例）。

### 24. 给用户的命令无编号——LLM 表达层问题，非 harness 缺陷 — ✅ 已分类（非代码问题，操作习惯修正）

- **现象**：agent 给用户的命令序列无编号，用户两次反馈"没有 1 2 步骤"。
- **分类（已查证）**：Vigil 代码不渲染/重排 agent 回复中的命令文本（原样透传），
  无"自动编号"机制，也不应有——命令组织是 LLM 表达层职责，非 harness 层。
- **结论**：非代码问题。缓解靠 agent 行为规范（命令序列带编号、逐步输出），
  已作为操作习惯修正。
- **可选产品增强（低优先）**：若希望降低 LLM 表达不一致的影响，可考虑 runbook
  执行模式（runbook_checkpoint）天然带步骤编号，引导用户对多步任务走 runbook
  而非自由对话。

### 25. sudo -S 硬拦截分级改造——按"密码来源"放行，而非一刀切 — ✅ 已实施（2026-08-12，批次一）

- **现状**：`tools/approval.py` 的 `_check_sudo_stdin_guard` 对命令文本含
  `sudo -S` 且环境无 `SUDO_PASSWORD` 时**无条件 block**（注释：防 LLM 暴力猜
  密码）。设计意图正确（暴力向量真实存在），但实现是一刀切——只看语法、不看
  密码来源。
- **问题（与产品定位冲突）**：Vigil 定位"给不会运维的开发用"，目标用户：
  1. 大概率不会配置 SUDO_PASSWORD（那是运维概念，恰是目标用户不懂的）；
  2. 典型用法是对话里告诉 agent "密码是 xxx" → 期待 agent 自己用——当前守卫
     直接堵死，agent 在 prod 上无法执行任何 sudo，只能当顾问；
  3. 实测（两次任务：密码轮换、grafana 修复）均被同一守卫卡死，最后只能甩命令
     给用户手动跑——对不会运维的用户，这一步他们自己做不了。
- **关键洞察**：拦截的应是"密码来源不明"，而非"sudo -S 语法"。三种情况应区分：
  1. 密码来自 vault（凭据系统权威值）→ 放行
  2. 密码来自用户本轮明确提供（用户即凭据持有者，给了即授权）→ 放行，命令中
     不留明文（走环境变量/文件）
  3. 密码是 agent 猜测/试探（无来源）→ 拦截（保留现防暴力能力）
- **待改方向**：
  1. `_check_sudo_stdin_guard` 现有 `SUDO_PASSWORD in os.environ` 放行分支扩展
     为"会话内已登记凭据（用户提供或 vault 读取）即视为已配置"；
  2. 或走 approval 流程：拦截时用户可当场确认放行（人在场审批）；
  3. 拦截消息给出可操作出路（"请在对话中提供密码，或将 SUDO_PASSWORD 写入
     .env"），而非死路；
  4. 不碰会话前缀/缓存（approval.py 是执行层，改动不影响 prompt caching）。
- **关联**：第 17 条"安全策略可用性缺口"（合法用户无法走 agent 通道）、第 21 条
  凭据红线（放行时命令不得含明文）。

- **已实施（批次一）**：`_check_sudo_stdin_guard` 三态判定——① 密码来自
  vault（会话内 `tools/credential_vault.py` 读过）→ 放行；② 密码来自用户
  本轮明确提供（approval 审批确认 / 保险箱 store 登记 user 来源）→ 放行；
  ③ 无来源（agent 猜测/试探）→ 拦截（保留防暴力）。登记源在新增的
  `tools/credential_vault.py`（store/retrieve/`mark_user_authorized()`，approval
  审批成功路径登记）。拦截消息给可操作出路："请在对话中提供密码（仅本次使用，
  系统将安全存储、不回显），或将 SUDO_PASSWORD 写入 .env"。已知边界：
  登记是模块级（进程/会话生命周期），gateway 多会话共享进程时跨会话放行
  （任务书允许"模块级或会话级 dict"；如需会话隔离可后续按 session key 收窄）。
  单测：tests/tools/test_sudo_stdin_guard_sources.py（7 例）。

### 26. ssh-agent 生命周期与 terminal 环境变量持久化错配 — 🟡 缓解已落地（vssh 包装脚本），核心待改待排期

- **现象**：terminal 会话间环境变量持久化（`SSH_AUTH_SOCK` 被保留），但会话
  结束后 ssh-agent 守护进程已被回收；新会话里 `ssh-add -l` 报 "Could not open
  a connection to your authentication agent"，而 `ssh` 直接报 "Permission
  denied (publickey,...)"——表象像 key 认证失败，实际是 agent 已死，仅靠报错
  无法定位，浪费一轮排查。
- **根因**：环境变量持久化机制不校验所引用进程/socket 是否仍存活；ssh-agent
  这类"跨会话需要的守护进程"被会话边界回收，而它的 socket 路径却被当成普通
  env 保留。
- **待改方向**：
  1. terminal 持久化环境变量时对 socket/pid 类变量做存活校验，失效则清除或
     显式告警；
  2. 或提供会话级守护进程托管（agent 声明需要存活的进程，跨调用保活）；
  3. 缓解（已落地）：包装脚本每次调用内完成 vault→fresh agent→ssh（本机
     /root/bin/vssh），不依赖跨会话 agent。

### 27. runbook env_mismatch 警告与命令级权限矩阵判定不一致 — ✅ 已实施（2026-08-12，批次三）

- **现象**：每次 runbook_load 加载 prod runbook（test 会话）都返回
  `env_warning: 跨环境操作默认拒绝，确认后再执行`，但后续实际执行并未因此被拒
  ——硬 gate 在命令级权限矩阵（L2 走审批+auto-approve）。警告语义（"默认拒绝"）
  与实际行为（放行）不一致。
- **问题**：LLM 要么被"默认拒绝"误导以为有硬 gate 而松懈，要么对每条 runbook
  重复的警告麻木；真正的判定（命令级矩阵、L4 阶段门）与加载时警告不联动。
- **改了什么（方案 1：纯文案，零逻辑）**：`tools/runbook_tools.py` `_full_payload`
  的 `payload["env_warning"]` 措辞改为——
  "runbook 适用环境与当前会话环境不一致；跨环境操作由命令级权限矩阵逐条判定
  （L2 及以上走审批），不是整体拒绝。"`env_mismatch: True` 布尔字段语义不变
  （下游依赖不破坏）。
- **为什么这么改**：env_mismatch 只是加载时提示，硬 gate 在命令级权限矩阵
  （L2 走审批 / L3-L4 拒绝）——措辞与真实判定逻辑对齐，消除"默认拒绝"语义
  冲突；不把 runbook 整体降级 read-only（会破坏跨环境 runbook 的合法用途，
  且与矩阵逐条判定冲突）。
- **核销方式**：`tests/tools/test_runbook_tools.py`（env_mismatch 断言同步新
  措辞）+ `test_runbook_vault_refs.py` 全过（16 例）。

### 28. 凭据字段语义漂移第三次实证——组合字段 + 工具化校验定位 — ⬜ 未实施（并入 #19，待排期）

- **现象**：轮换后 vault 凭据字段 `ssh_key` 仍是失效旧值；当前 pem passphrase
  在组合字段 `ssh_key_0811/sudo_0811`（44 字符，字段名带斜杠与时间点）里，
  另有 `ssh_key_stale/sudo_stale` 更早旧值。字段语义=轮换时点+用途拼合，
  agent 无法从命名推断当前值。
- **定位方法（推荐固化）**：对候选字段逐个 `ssh-keygen -y -f <pem> -P <值>`
  校验（exit 0=匹配），纯工具校验、不做字符串猜测/拼接——校验失败即停止并询问
  用户。
- **待改方向（并入第 19 条）**：runbook 凭据读取步提供"字段校验"模板；vault
  字段命名去掉斜杠/时点组合式，单字段单语义；agent 读取凭据时先校验后使用，
  失效即停。

### 29. 拓扑事实层缺"访问路径"类字段 — 🟡 事实补录已完成（2026-08-11），schema 约定待排期

- **现象**：alertmanager 部署流程中，定位以下信息各花了一轮或多轮排查，而这些
  是每次 prod 操作都要用的"关键路径"：
  1. ssh key 文件位置（/root/wangwp10.pem）——只有 node233 实体偶然记了，
     其他节点没有；
  2. pem passphrase 的 vault 位置与字段语义（CSNDC/maas 多次轮换，当前生效
     字段带斜杠/时点组合名）——完全没进拓扑，靠 memory + 逐字段 ssh-keygen
     校验才定位；
  3. k3s manifest 声明文件位置（本机 /opt/k3s-manifests ↔ 233:/home/wangwp10/，
     且生效源文件名是 prometheus_new.yaml 不是 prometheus.yaml）——完全没进
     拓扑。
- **根因**：topo schema 的 attrs 是自由键，没有为"凭据位置 / 声明源 / 访问方式"
  定义约定字段名，agent 不知道往哪记、也不知道去哪查——信息只能散在
  memory/runbook/skill 里，每次靠会话记忆与探测。
- **已做（事实补录）**：prod-node233 加 ssh_key_passphrase_vault（只记 vault
  路径与校验方法，不记值）、k3s_manifest_src；prod-k3s 加 kubectl_access、
  manifest_src、note 补 monitoring 栈端口；prod-alertmanager 补 manifest 源与
  镜像加载路径。
- **待改方向（schema 层）**：
  1. entity attrs 约定保留键（如 `ssh_key`、`credential_vault`、`manifest_src`、
     `access`），topo 工具提供这些键的补全/校验提示，agent 新增实体时按约定记录；
  2. 或第一层 environments 增加 `access` 段（key 路径 / vault mount / 默认用户），
     host 级实体 detail 再细化；
  3. "凭据位置"与"凭据值"分离原则固化：拓扑只存位置（vault 路径+字段名+校验
     方法），值永远在 vault——避免拓扑成为新泄露面。

### 30. topo_query token 成本——全量重读无缓存，三层优化方案 — 🟡 方案 1 已实施（2026-08-12，批次二，随 #6 落地）；方案 2/3 未做

- **现状（已读代码确认）**：
  1. `tools/topo_tools.py::load_topology` 每次 topo_query 都全量
     `read_text + yaml.safe_load`，无进程内缓存、无 mtime 判断、无 TTL；
  2. topo_query 无参调用返回全部 45 实体全字段（endpoint/owner/source/
     last_verified/detail 路径…）进 LLM 上下文——这是 token 大头；
  3. 拓扑**不注入** system prompt（default_soul 只有一句 "topology table
     (topo_query)" 指引），完全靠 agent 主动查询，每次查询结果都是新增 token；
  4. 代码库已有缓存先例：`hermes_cli/banner.py::_banner_state_cache`（TTL 缓存）
     与 `approval_mode.py` 注释明确 "preserving the prompt-cache prefix"
     （DeepSeek context caching 命中 token 约 1/10 价格）。
- **为什么值得做**：拓扑是"每次操作前都该读"的强制事实层（行为规范已要求），
  但当前实现让"读拓扑"本身成为每次会话的 token 开销大头——事实层查询成本与
  它的使用频率成正比，设计上应反比。
- **三层优化方案（按收益/成本排序）**：
  1. **返回体积裁剪（收益最大，改动最小）**：topo_query 列表视图支持字段选择
     （如 `fields=name,type,env,endpoint,stale`），或默认列表模式只返回紧凑
     字段，detail=True 才给全字段。当前 45 实体×8 字段 ≈ 2-3KB/次，裁剪后 ≈
     0.6KB。工具契约是 JSON 字符串，改 handler 即可，不动 schema。
  2. **进程内 TTL 缓存（省 IO，不省 token）**：load_topology 按文件 mtime/内容
     hash 缓存（参照 banner.py 先例，TTL 如 30s）；topo_update 写文件后主动
     失效。收益是省磁盘+解析，对 token 无直接影响——但配合 1 是基础设施。
  3. **命中 provider context caching（省钱的机制）**：保持 system prompt/tool
     schema 前缀稳定（approval_mode.py 已在做）；若后续把紧凑拓扑注入 system
     prompt，则注入内容必须内容稳定（版本号/updated_at 变化会打断缓存前缀）；
     会话内重复查询相同实体时 agent 优先引用已返回结果而非重查。
- **行为层配套（不可靠，应升级为代码层强制）**：会话启动一次 topo_query 概览
  （compact）、操作前仅 detail 目标实体——本次 alertmanager 会话已按此执行，
  但这是 LLM 自觉行为（系统提示只有一句话指引，框架不发起任何自动查询），换
  模型/换上下文压力即失效。"每次操作前读拓扑"若真为强制事实层要求，必须代码层
  实现而非依赖自觉。
- **行为层→代码层的升级路径**：
  1. 会话启动时框架注入紧凑拓扑概览（compact 版 ~0.6KB）进 system prompt，
     保证每个会话开局即有事实底子，且前缀稳定可吃 context caching（可配置
     ops.topology.inject_summary: true/false）；
  2. 或工具层前置门：高危命令（ssh/scp/kubectl）过权限矩阵前强制校验本会话
     已读目标实体，未读则拒绝并提示先 topo_query；
  3. 两者可叠加：注入保证下限，前置门兜底强制。

- **已实施（批次二，方案 1 返回体积裁剪）**：topo_query 无参总览 / type / env
  列表视图默认只返回紧凑字段（name/type/env/endpoint/stale），detail=True 才给
  全字段；v0.2 无参视图进一步只返回第一层 hosts+cross_host（服务在第二层，不进
  总览）。工具契约是 JSON 字符串，只改 handler，schema 参数不变。方案 2（进程内
  TTL 缓存）与方案 3（注入前置门）未做，保持待排期。

### 31. runbook 产品方向：自然进化机制 + 模板化商业化 — ⬜ 未实施（产品方向，排期见下）

- **背景（用户设计构想）**：runbook 的初衷是用户自己写、或用 Vigil 多了自然
  进化；进一步想到 runbook 本身可以商业化（卖钱）。两者是自洽闭环——进化积累
  含金量，商业化变现含金量。
- **现状**：runbook 进化依赖 agent 在会话里踩坑后手动 patch（如本次
  alertmanager 部署把 sudo ctr 卡死、quay.io 拉镜像、prometheus_new.yaml 命名
  等坑写进 runbook），无机制保证；schema v0.1 只有 version 字段，无修订流、
  无模板变量。
- **进化机制缺口**：
  1. 会话历史（session DB）就是进化原料——每次排障成功/失败都留在会话里，
     可自动提炼"这段摸索→runbook 步骤/pitfall"；
  2. runbook 修订流：执行后产生 diff 建议（执行中改过的命令、新坑），用户确认
     后合入版本——与"拓扑读一遍靠 LLM 自觉"同类问题，需机制而非自觉。
- **商业化卡点与解法**：可执行的 runbook 必须含环境信息（端点/凭据位置/文件名），
  可卖的 runbook 必须剥离环境信息（否则泄露客户拓扑）——解法是模板化：schema
  v0.2 支持 {{var}} 占位符 + <vault:path/field> 凭据引用，执行时绑定环境。
  这恰好合并第 21 条的待改方向（runbook commands 支持 vault 占位符，原为凭据
  安全）——一个改动两个卖点。
- **可卖形态**：剥离环境后的通用模板（如本次 alertmanager 部署去掉 IP/节点号/
  凭据位置后 = "离线 k3s 监控告警部署手册"）；行业场景包（离线环境部署/监控栈
  搭建/凭据轮换）天然适合。
- **排期建议（合并排期）**：runbook schema v0.2（模板变量 + vault 占位符 +
  修订流）→ 进化机制（会话历史提炼）→ 商业化（模板库）。

### 32. prod 变更执行缺少强制确认门 — ✅ 已实施（2026-08-12，批次一代码层强制确认门）

- **现象**：排障定位根因后，agent 提议改 healthcheck 并重建容器；用户回复
  "本地 compose 文件在 /opt/ansible"——agent 据此直接修改模板并执行
  ansible-playbook 完成 prod 变更（双节点容器重建），事后被纠正："这不是 prod
  环境么 你怎么就给我运行 ansible 了"、"下不为例"。
- **根因**：
  1. 权限矩阵与"执行前必须确认"只是行为规范（LLM 自觉），代码层无强制 gate——
     普通命令/playbook 执行在 prod 上不触发任何确认关卡；runbook_checkpoint
     的阶段门只覆盖 checklist=true 的 deploy runbook，本次根本未走 runbook
     流程；
  2. agent 把信息类回复（指出文件位置）误读为变更授权——确认与信息获取混在
     同一个用户回合里，变更动作前没有独立的确认步骤；
  3. 用户话语的意图边界（"告诉我在哪" vs "授权你去改"）没有工具层判据，全凭
     模型推断。
- **已做（行为约束层）**：凭据纪律条目固化"prod 变更（含 ansible-playbook
  执行）必须先 clarify 获显式授权；指出文件位置 ≠ 授权执行"。
- **待改方向（代码层，与第 30 条方案 2 合并排期）**：
  1. 高危/变更类命令（ssh/scp/kubectl/ansible-playbook/服务重启与容器重建类）
     过权限矩阵前，prod 环境默认 require_confirmation=true 强制人工确认门，
     test/uat 默认 false——确认粒度按"环境 × 操作类型"，不依赖自觉；
  2. 确认门语义：agent 提出变更方案 → 用户显式确认 → 才允许执行；"执行"与
     "信息获取"分开判定，信息类回复不构成执行授权；
  3. runbook_checkpoint 扩展：deploy 类 runbook 默认 checklist=true，任何触发
     部署的路径强制过阶段门；
  4. 可选：变更执行前框架注入"目标环境 + 影响范围 + 回滚方式"摘要供确认展示
     （与第 31 条模板化同源）。

- **已实施（批次一）**：`tools/ops_permissions.py` 的
  `check_ops_command_permission` 返回 decision 新增 `require_confirmation` 字段——
  变更类命令（ansible-playbook / kubectl apply|delete|edit|scale|rollout|drain|
  cordon / docker compose up|restart|rm|down / docker restart|rm|stop /
  systemctl restart|stop / helm upgrade|install|uninstall，见 `_CHANGE_COMMAND_RE`）
  在 env=prod 时默认 true；未分级变更类命令（ansible-playbook）在 prod 合成
  审批级判定（否则漏网）。L3/L4 deny 仍硬拒（deny > 确认门）。
  `tools/approval.py`：approve+require_confirmation 不因 yolo / mode=off /
  永久 allowlist / smart-approval 自动放行——强制走人工确认门（与 L2 approve
  同一条审批通道），消息标注"⚠ prod 变更确认门"；不提供永久 allowlist、
  不持久化会话放行（每次都确认）。`tools/runbook_tools.py`：
  `kind: deploy` runbook 默认 `checklist: true`（部署路径强制过阶段门），显式
  `checklist: false` 仍可关闭。单测：tests/tools/test_ops_confirmation_gate.py
  （45 例）+ test_ops_permissions_guard.py 新增 4 例 + test_runbook_tools.py
  新增 2 例，相关套件全过。

### 33. topo_update 参数契约未校验——错误结构静默写入嵌套层 — ✅ 已实施（2026-08-12，批次五：attrs 键展开合并 + 复杂值拒绝 + 契约正反例）

- **现象**：工具文档契约是"env/type/endpoint/owner/status/healthcheck/depends_on/depended_by 为顶层字段，其余键写入 attrs"，即调用方应直接传字段键。但调用方传了 `{"attrs": {...}}`（把 attrs 当普通键再包一层），工具静默接受，把内容写进 attrs.attrs 嵌套层，返回 status=updated 无任何警告。一次批量登记导致 20 个实体档案变成两层嵌套，topo_query detail 结构异常，且第一层概览看不出问题——靠用户追问"记录为什么不对"才发现。
- **根因**：
  1. 工具层未对 updates 参数做结构校验——"attrs" 不是合法顶层字段键，按契约应拒绝或报错，实际被当作普通属性写入；
  2. 契约表述有歧义："其余键写入 attrs" 容易被调用方（LLM）理解为"需要包一层 attrs"——契约应给正例/反例，或工具侧做规范化（非顶层字段自动展开进 attrs，显式拒绝 attrs 键）；
  3. 无事后可观测性：嵌套层写入仍是合法 YAML，校验全绿，只有人工查阅 detail 才能发现。
- **待改方向**：
  1. topo_update 参数校验：updates 中出现 "attrs" 键时自动展开合并（或拒绝并报错），错误信息明确"字段键直接传，不需要包 attrs 层"；
  2. 契约文档补正例/反例（这是工具使用契约类缺陷，与第 27 条 env_mismatch 同类：工具契约歧义 → 行为偏差）；
  3. 可选防御：topo_query detail 检测 attrs 异常嵌套并提示。
- **本次已做（数据修复）**：20 个受影响档案已用保注释脚本把嵌套内容提升回顶层 attrs（冲突键保留旧值），复核无残留；后续更新已按正确参数格式验证。
- **2026-08-12 复核**：批次二（schema v0.2）后 topo_update 的 attrs 处理仍在（tools/topo_tools.py:613-617 直接 `attrs[key] = value`），"attrs 键自动展开/拒绝"未实施。**并入批次二任务 1 的 B 项一并排期**（与 v0.2 参数契约修订同批）。
- **已实施（批次五，2026-08-12）**：
  1. `tools/topo_tools.py` `topo_update` 的 updates 循环新增 `"attrs"` 键显式分支：值为 dict → 展开合并进顶层 attrs（`existing.setdefault("attrs", {})`），**不再写入 attrs.attrs 嵌套层**；值非 dict → 明确报错。audit 追加 note："updates 含 attrs 键：字段键直接传，不需要包 attrs 层；已自动展开合并。"
  2. 非白名单键的 dict/list 复杂值 → 明确报错（防 LLM 传嵌套结构继续污染档案；depends_on/depended_by 是已定义顶层列表字段，走原分支不受影响）。
  3. `_DEFAULT_TOPO_UPDATE_SCHEMA` description 补正例 `{"endpoint": "1.2.3.4", "owner": "x"}` / 反例 `{"attrs": {...}}`（应直接传字段键，attrs 键会被自动展开）+ 标量限制说明。
  单测：tests/tools/test_topo_update_contract.py（8 例：attrs 展开合并不产生 attrs.attrs / 合并既有 attrs / non-dict 拒绝 / 顶层字段 / 普通标量 / dict 和 list 拒绝 / depends_on 列表仍允许）+ 既有 test_topo_tools.py / test_topo_v2.py 全过。
- **核销方式**：`ops.topo.enabled=false` 时工具不注册零影响；合法调用（顶层字段 + 标量 attrs）行为不变，仅新增结构防御与提示；季度体检抽查新档案不再出现 attrs.attrs 两层嵌套。

### 34. 上游推理服务 500 崩溃：无降级 + 崩溃会话不入库 — 🟡 缺陷 2 已核实“边执行边写”已存在（批次五，登记结论未加代码）；缺陷 1 保持未实施（原因见下）

- **背景**：切换模型后实测新模型能力。新模型（经自建推理服务接入）在完成 4 个复杂任务（拓扑总览、监控服务全检、多轮 ssh/vssh 排查）后，于一次 pty 重试调用时上游返回 500。两个独立缺陷同时暴露：
- **缺陷 1：上游 500 无降级**。RemoteProtocolError → 重试耗尽 → InternalServerError，重试全失败后会话直接断连，无降级/兜底路径（当前未配置 fallback 模型；若后续配置，需验证 5xx 时是否真正生效）。
- **缺陷 2：崩溃会话未入库 → 复盘误判（本次实证）**。session_search 检索不到崩溃会话的任何记录，仅凭 DB 空缺曾推断"该模型未执行任何操作"——实际该模型成功执行了大量工具调用。崩溃发生在持久化之前，整段轨迹丢失，数据库空缺 ≠ 模型没干活，复盘会被假象误导。
- **待改方向**：
  1. 重试耗尽后触发 fallback 模型降级并显式告知"已降级"，不静默断连；
  2. 会话持久化边执行边写（或崩溃时 flush 已执行轨迹），保证排障复盘不因崩溃丢失证据。
- **未实施**：待排期。
- **批次五核实（2026-08-12，读代码验证，不实测远端）**：
  - **缺陷 2（崩溃轨迹 flush）——核实结论：边执行边写已存在，flush 是多余的，不强行加代码**。会话持久化是"边执行边写"：
    1. `agent/turn_context.py:1241` 在首次 LLM 调用前即持久化入站 user 消息（Crash-resilience）；`agent/conversation_loop.py` 工具循环每步 flush（assistant(tool_calls) 块在工具执行前落库、每个工具结果立即落库，`tests/run_agent/test_tool_call_incremental_persistence.py` 契约钉住），且每条终止路径——重试耗尽（含 500）、invalid response、中断、截断、拒绝、外层 catch-all——都调用 `agent._persist_session()`；`agent/turn_finalizer.py:410` 每 turn 出口（成功 + 异常）统一持久化；`cli.py:12072/14837` CLI 关闭/退出兜底。
    2. 落库走 `hermes_state.SessionDB.append_messages_batch`（单事务 + FTS5 可检索），`_flush_messages_to_session_db` 用 `_DB_PERSISTED_MARKER` 去重（#860 契约，`tests/run_agent/test_860_dedup.py` 钉住），重复 flush 幂等不产生重复行；崩溃（含 SIGKILL）最多丢失当前 in-flight 半截 turn，已完成轨迹"哪怕缺最后一段"也已在库，session_search 可检索到。
    3. **登记核实结论，未加代码**（批次五明确：不适用场景登记即可，不强行加代码）。
  - **缺陷 1（500 降级探测）——登记"探测提示保持未实施，原因"**：
    1. **fallback 配置已存在**：config.yaml 顶层 `fallback_providers`（list，legacy `fallback_model` 自动迁移，`hermes_cli/config.py:1967-2020` 规范化），`agent/agent_init.py:1404-1411` 构建 `_fallback_chain`，`agent/chat_completion_helpers.py:1730 try_activate_fallback` 逐级切换。
    2. **5xx 时确实生效（读代码验证）**：`agent/error_classifier.py` 将 500/502 分类为 `server_error, retryable=True`（请求校验/上下文溢出形态除外）、503/529 为 overloaded；`agent/conversation_loop.py:5328-5347` 重试耗尽后先 `_try_recover_primary_transport` 再 `_try_activate_fallback()`，fallback 可用则自动降级继续。断连并非静默：每次 attempt 缓冲行含 `[HTTP 500]` 状态码，终端 `❌ API failed after N retries — <摘要>`。
    3. **保持未实施原因**：提示文本要求"已重试 N 次失败"的 N（retry_count/max_retries）是 `agent/conversation_loop.py` 重试循环的局部变量，断连出口也在该文件（SDK `max_retries=0` 已禁用，重试归外层循环所有）——硬约束 1 禁止触碰 conversation_loop，从 `agent/chat_completion_helpers.py` 或持久化入口拿不到 N，硬加需跨层传递计数（侵入）。按批次五完成标准第 3 条登记"缺陷 1 保持未实施，原因"，不硬改。
    4. "已执行轨迹已保存"语义由架构保证（缺陷 2 核实结论）：断连前所有终止路径均已调用 `_persist_session`，用户不会"以为全丢"。
- **核销方式**：季度体检复核 conversation_loop 重试耗尽路径是否仍先落库再返回（当前 5482 行附近先 `_persist_session` 再 return）；fallback 配置存在性随时可从 config.yaml 检查；若后续允许触碰 conversation_loop，在终端断连出口补一行"上游推理服务 500，已重试 N 次失败；已执行轨迹已保存"即可关闭缺陷 1 的提示部分。

### 35. 换模型即失守：行为约束层 vs 代码层 gate 的实证 — ✅ 已实施（2026-08-12，批次五：JSON 值形态检测兜底，组合字段名回显打码）

- **现象**：探查 prod k3s 节点状态（需加载带 passphrase 的 ssh key）时，切换后的模型（397B）单任务内三次凭据违规：
  1. **输出层无脱敏**：vault API 返回 JSON 全量回显到工具输出，含组合字段名（ssh_key_0811/sudo_0811 形态）的凭据字段——redact 名单只认 api_key/token/secret 等固定键名，自定义字段名原样过（第 5 条 2026-08-11 实证同源，**再次复现**）；
  2. **命令构造无检测**：`echo <passphrase> | ssh-add` 明文进命令文本（第 21 条命令文本泄露面，**再次复现**）；
  3. **引用历史消息时第三次展示**含密码的命令（第 21 条泄漏面第三次实证）。
  唯一被拦住的是 sudo -S 管道（代码层 gate 生效），另两处（输出回显、命令构造）无任何 gate。
- **根因（产品级洞察）**：凭据纪律写死在行为约束层（SOUL/memory/runbook），无代码层 gate——**模型一换约束立即失效**（deepseek→397B）。三次违规中只有 sudo -S（代码层）被拦，说明安全不变式不能依赖 LLM 自觉，必须钉在执行层；行为约束只作辅助，不是防线。与第 5/21/25/32 条同源，本条是"换模型即失守"的最直接实证。
- **本次验证的可行正解（ssh key passphrase 非交互加载）**：passphrase 写临时文件（600）→ `SSH_ASKPASS=/bin/cat SSH_ASKPASS_REQUIRE=never ssh-add <key> < tmpfile` → 立即删 tmpfile。应沉淀进 runbook（prod-rotate-wangwp10 或 ssh 凭据加载流程），并封装本地脚本，避免每次摸索。
- **2026-08-12 复核（批次一实施后）**：
  - sudo -S 管道：**已被代码层 gate 拦截**（#25 三态分级已实施，无来源仍拦截）——实证中"唯一被拦住"项现已是显式 gate；
  - 命令构造层：**部分覆盖**——批次一 #21 已在 agent/redact.py 增加 `_CMD_CRED_FLAG_RE`（--from-literal=KEY=VAL / --password 等 flag 值打码）+ `_CMD_HIGH_ENTROPY_TOKEN_RE`（≥16 字符高熵裸 token 兜底，排除纯 hex/UUID/路径/URL 形态），`echo <passphrase> | ssh-add` 这类高熵 passphrase 会命中；但 `echo <短密码> | ssh-add`（<16 字符）仍可能漏；
  - 输出层 JSON 自定义字段名：**缺口仍在（2026-08-12 实测确认）**——`_JSON_KEY_NAMES` 是精确键名匹配（含 ssh_key/private_key/passphrase 等），但 `ssh_key_0811`/`sudo_0811` 这类**斜杠/时点组合字段名不在名单**，vault JSON 原样输出（实测 `{"ssh_key_0811": "..."}` 未被打码）。字段名无法穷举 → 需"值形态检测"（高熵串+上下文关键词）兜底，**未实施**。
- **待改方向（并入第 5/21/25 条，优先级上调）**：
  1. 输出层 redact 覆盖所有渲染出口 + 自定义凭据字段名；字段名无法穷举（本次是斜杠/时点组合）→ 增加"值形态检测"（高熵串+上下文关键词）兜底——具体：`_JSON_FIELD_RE` 之外增加值形态检测（JSON 值 ≥16 字符高熵串 → 打码），复用批次一 #21 的 `_looks_like_inline_secret` 启发式；
  2. 命令构造层凭据检测（第 21 条，批次一已做主体，短值补漏）；
  3. sudo 被拦后的系统化 fallback：被拦即停 + 引导用户手动执行（本次反复尝试多轮才停下，行为约束换模型后不可靠）；
  4. 产品设计原则固化：行为约束层降级为辅助，安全 gate 以代码层为准。
- **已实施（批次五，2026-08-12）——JSON 值形态检测兜底**：
  1. `agent/redact.py` 新增 `_JSON_VALUE_SHAPE_RE`（任意键名 + 值 ≥16 字符形态）作为精确键名 `_JSON_FIELD_RE` pass **之后**的兜底层：值满足 `_looks_like_inline_secret`（≥16 字符、≥2 字符类混合、非纯 hex/UUID/路径/URL、香农熵 ≥3.2）→ `_mask_token` 打码，与键名无关。`{"ssh_key_0811": "<高熵>"}` / `{"sudo_0811": "<高熵>"}` → 值打码（#35 核心用例）。
  2. 防误伤复用主链现有守卫：env 引用形态（`_ENV_LOOKUP_VALUE_RE`）、非秘密常量键（`_is_non_secret_constant_key`，严格模式）、已打码值（`_already_masked_value`，`_prefix_present`）直接放行；URL/路径/base64（含 `/ \ : .`）与长纯文本（字符类单一）被 `_looks_like_inline_secret` 天然排除——`{"description": "<50 字符普通文本>"}`、`{"url": "https://..."}`、`{"path": "/var/lib/..."}` 不打码；精确键名 `{"password": "short"}` 原逻辑不回归。
  3. 新逻辑挂在 `redact_sensitive_text` 主链内，工具输出 / reasoning / 命令文本三个渲染出口自动覆盖（批次一 #5 已接线，无需重复接线，测试各验证一例）。
  单测：tests/agent/test_redact_value_shape.py（12 例：值打码 4 + 防误伤 4 + 精确键名回归 1 + 三渲染出口 3）+ 既有 test_redact.py / test_redact_command_text.py 全过。
- **2026-08-12 验收修复**：独立验收发现首字母大写的正常英文句子（lower+upper 天然混合字符类，如 `{"description": "This is a perfectly normal..."}`）被 `_looks_like_inline_secret` 误判打码。修复：该函数开头排除**含空格 token**（真凭据从不含空格；`_CMD_HIGH_ENTROPY_TOKEN_RE` 匹配的单 token 本就不含空格，命令文本路径零影响）——仅改 1 行。补首字母大写回归用例 `test_caps_sentence_not_masked`（value_shape 12→13 例），实测 `ssh_key_0811`/`sudo_0811` 高熵值仍打码、句子不再误伤、三渲染出口抽查通过。
- **核销方式**：redact 是输出层常驻，与 `ops` 开关无关；组合字段名无法穷举，值形态检测是兜底，精确键名 pass 仍先行。风险点：恰好"≥16 字符 + 混合字符类"的普通长字符串有被误判面，已用空格排除 + 字符类/路径/URL/纯 hex 排除 + 防误伤测试用例钉住。

## 附：合并时的代码实测记录（2026-08-12）

- `_HERMES_CORE_TOOLS`（toolsets.py）62 个核心工具，不含 topo/runbook 四个工具。
- `_get_platform_tools({'platform_toolsets': {}}, 'cli')` 实测返回不含
  topo/runbook → #14 未实施确认。
- `_RECENTLY_SHIPPED_TOOLSETS = frozenset({"bfl"})`，无 topo/runbook 特例。
- topo/runbook check_fn 数据门控已生效（#1）；ops_permissions 默认启用（#1）。
- 42cd841（#15）共 7 文件 305 行：file_operations 修复 + approval/file_tools
  site-packages gate + 单测。
- 5de609a/8f43841/7b875c3（#5）共三提交：DANGEROUS_PATTERNS 凭据读取阻断 +
  kubeconfig 绝对路径补漏 + redact 三路径（ssh_key/SSHPASS/reasoning）。

### 37. 数据根 legacy fallback 无特征判断——空/他人 ~/.hermes 被当 Vigil 数据根 — ✅ 已实施（2026-08-12，b240ef0）
- **发现**：2026-08-12 晚产品讨论时发现 `default_data_root_for` 的旧 hermes 布局兜底
  只看 `~/.hermes` 目录存在，无法区分"Vigil 老数据"和"Hermes 本体/无关残留"——用户
  机器上恰好有 ~/.hermes（装过 Hermes 本体 profile=personal/work，或别的工具残留）时，
  Vigil 会把别人的数据根当自己的，读错配置、写错数据，甚至与正在运行的 Hermes 抢数据。
  当晚实战触发：第一个试用用户（朋友二）机器上**确实装有 Hermes**，已让其暂缓安装等修复。
- **修复（b240ef0）**：
  1. 新增模块级 `_looks_like_vigil_legacy_data(hermes_dir)`：`_safe_exists(hermes_dir / "profiles" / "ops")`
     ——Vigil v0.1.5 及以前数据在 ~/.hermes 且默认 profile 是 ops；Hermes 本体是 personal/work，
     无 ops，这是可靠区分特征。
  2. `default_data_root_for` legacy 分支：`if _safe_exists(legacy) and _looks_like_vigil_legacy_data(legacy)`
  3. `vigil_data_root_candidates` 同步（auth 测试护栏/gateway remap 3+2 处消费）：无特征只返回 (primary,)
  4. 环境变量路径（VIGIL_HOME/HERMES_HOME）未动，显式指定仍优先；未新增 env var。
- **验收**：tests/test_hermes_constants.py 60 passed（改 2 用例 + 新增 3 用例：
  empty_hermes_no_fallback / has_other_profiles_no_fallback / candidates_empty_hermes_excluded
  / candidates_ops_profile_included）；行为探针五场景符合预期（空 .hermes→.vigil、
  ops→.hermes、personal→.vigil、都不存在→.vigil、.vigil+ops→.vigil 且 candidates 含 legacy）。
- **核销方式**：fallback 语义从"目录存在即兜底"改为"含 Vigil 老数据特征才兜底"；
  老安装（profiles/ops 在）仍无感兼容，新用户/Hermes 本体用户不再被串。

### 38. 批次十六 安全三连：redact 三盲区（§V）+ sudo_exec 提权工具（§W）+ SSH 认证熔断（§AD） — ✅ 已实施（2026-08-14，批次十六；commit hash 佐证：§V 1deb38d / §W 104af2e / §AD 0ab8e47）
- **发现**（2026-08-14 凌晨 dogfooding 日志 §V/§W/§AD，prod 环境多次复现）：
  1. §V 凭据泄露三盲区：`curl -H "X-Vault-Token: s.xxx"` 的 OpenBao 认证 header 不在
     `_SECRET_HEADER_NAMES`；`s.` 前缀不在 `_PREFIX_PATTERNS`；`sudo -S <<< '密码'` /
     heredoc / `SUDO_PASS=$(curl vault | jq)` 不在命令文本 redact 名单——token/密码
     明文显示在工具调用展示行（用户原话"sudo -S 暴露了密码"）。
  2. §W 无正规提权工具：agent 每次上 prod 都拼 `sudo -S <<< '密码'` 管道，展示层
     必然带密码；审批匹配 shell 字符串形态，内联 token 可绕过。
  3. §AD 认证自伤：agent 连续尝试多种认证方式耗尽 OpenSSH MaxAuthTries（默认 6）
     → "Too many authentication failures" → 几分钟内 ssh 全部被拒（生产自伤）。
- **修复（批次十六，三提交）**：
  1. redact 三盲区（agent/redact.py，1deb38d）：`_SECRET_HEADER_NAMES` 追加
     x-vault-token/x-vault-request/x-vault-namespace（header 名命中即打码，值形态
     不重要），值类排除引号（curl -H 收尾引号是结构字符，吞掉破坏语法）；
     `_PREFIX_PATTERNS` 追加 `s\.[A-Za-z0-9]{20,}`（≥20 防误伤句点文本），
     `_extract_literal_prefix` 支持转义元字符（预筛保持 `s.` 精确，不退化）；
     `_CMD_CRED_FLAG_RE` 旁追加确定性上下文匹配：sudo -S herestring/heredoc stdin
     密码注入（出现即密码，无值形态启发式）、`*_PASS/_PASSWORD/_TOKEN/_KEY/_SECRET=$(…)`
     赋值形态整段打码（含带引号形态）；ENV pass 对命令替换值跳过（交给命令文本
     pass 整段打码，避免打残结构尾部漏出）；无 sudo 上下文的普通 `<<<` 不碰。
  2. sudo_exec 提权工具（tools/sudo_tool.py 新增，104af2e）：toolset=topo 新工具，
     凭据从拓扑表 credential 引用解析（vssh 三通道复用），注入一律 ASKPASS——
     本地 `sudo -A` + SUDO_ASKPASS env；远端 scp 0700 askpass + 保险箱文件到 /tmp、
     用完即删；**禁止任何 `sudo -S` / `echo 密码 | sudo -S` / `<<< '密码'` 形态**
     （§V 泄露根源，工具自身不得使用）；命令校验 fail-closed（bash -c / `>` / `&` /
     `;` / `$()` / 内嵌 sudo 拒绝，只读管道放行）；权限矩阵联动（`sudo <command>`
     过 ops_permissions：prod 变更类 → require_confirmation 人工确认门、deny 拒绝、
     只读诊断直行）；凭据缺失/认证失败 → 停下来问用户（fail-closed）。
  3. SSH 认证熔断（tools/topo_discovery.py，0ab8e47）：`_build_ssh_runner` 会话级
     host:user 失败计数（模块级 dict + 锁，进程内存）；exit 255 + stderr 含
     Permission denied/MaxAuthTries/Authentication failed 判定认证失败；3 次熔断
     返回可操作错误（MaxAuthTries=6 提示 + 手动 ssh / 拓扑 credential 声明）不再
     自动重试；成功一次清零；CLI vssh 路径不经此 runner，不计数不受影响。
- **验收**：三新套件全绿（test_redact_vault_forms 16 + test_sudo_exec 16 +
  test_ssh_auth_breaker 6）+ 回归不破（test_redact 三件套 114 + topo_tools/
  topo_discovery/vssh 90）；行为探针三条符合预期（§V 7 形态逐条打码 + §W 只读直行/
  prod 变更需确认 + §AD 第 4 次调用直接熔断）。
- **硬约束**：未碰 conversation_loop / prompt 缓存 / 压缩逻辑；无新 env var
  （SUDO_ASKPASS 是 sudo 程序 env，非 HERMES_*/VIGIL_*）；凭据值不进 argv/命令串/
  日志（askpass 脚本 0700 只 cat 保险箱文件）；git diff 只含白名单文件
  （agent/redact.py、tools/sudo_tool.py、tools/topo_discovery.py（任务 3 方案 A 指定
  挂载点）、tests/ 对应新测试）。
- **已知风险/误伤面**：redact `s.` 前缀要求 ≥20 字符，短 `s.xxx` 只在 header 上下文
  打码；赋值形态只匹配 *_PASS/_PASSWORD/_TOKEN/_KEY/_SECRET 后缀 + 命令替换，普通
  赋值（COUNT=$(wc -l) 等）不误伤；sudo_exec 对含 `>`/`&`/`;` 的命令 fail-closed
  （含 2>&1），诊断命令请用纯只读形态；远端 sudo 的 pty 注入时序（RHEL requiretty）
  标注为待实测项，当前实现走 scp 远端 askpass。
- **核销方式**：redact 名单追加式，季度体检 grep 新增形态仍生效；sudo_exec 可用性
  由 check_topo_requirements 数据门控（无 topology.yaml 零 footprint）；熔断计数
  进程内存、重启清零（会话级语义）。

### 39. 批次十七 权限矩阵 B'：未分级命令 prod 档默认审批 + 变更类覆盖测试（§AA 联动） — ✅ 已实施（2026-08-14，批次十七；commit hash 佐证：B' dc59dde / 变更覆盖 98f7b70）
- **发现**（2026-08-13 晚 + 2026-08-14 §AA 源码核验）：
  1. 实测洞：agent 把 k3s-prod 集群改名成 k8s-prod，跨 20+ 文件 `mv` + patch 全部
     放行——`mv 单文件` 不在任何分级模式里（classify_command → grade=None），
     prod 档对未分级命令直接放行（批量危险操作拆成单条无害命令即全绕过）。
  2. §AA 风险点 1：prod 变更确认门依赖 `_CHANGE_COMMAND_RE` 识别变更类，漏识别的
     变更命令会退回普通审批 → yolo 放行；现有测试没有覆盖变更清单完整性。
- **修复（批次十七，B' 方案拍板"毕竟是 Prod"）**：
  1. B'（tools/ops_permissions.py，dc59dde）：`check_ops_command_permission`
     else 分支（grade=None 或未声明环境）——prod 档未分级命令默认
     approve + require_confirmation=True（强制人工确认门，同 #32 变更门通道，
     yolo / smart-approval / 永久 allowlist 都绕不过）；L1 查询命令匹配 L1 模式
     （grade 非 None）不受影响，prod 仍 execute；L3/L4 deny 优先级不变；非 prod
     档（local/test/dev）未分级命令维持现状放行。description 文案：未分级+prod
     明示"未分级命令在 prod 需人工确认（B'）"，保留"prod 变更确认门"字样。
  2. 变更类覆盖测试（tests/tools/test_change_command_coverage.py，98f7b70）：
     20 条变更样本（ansible-playbook/kubectl apply/delete/edit/scale/rollout/drain/
     cordon、docker compose up/restart/rm/down、docker restart/rm/stop、
     systemctl restart/stop、helm upgrade/install/uninstall）逐条断言命中
     `_CHANGE_COMMAND_RE` + prod 档 require_confirmation=True；反向断言
     （ls/cat/kubectl get/docker ps/systemctl status/helm list）不命中。
     全部命中，无需补正则（变更清单无漏项）。
- **验收**：tests/tools/test_ops_permissions.py（30）+ test_ops_permissions_guard.py
  （23）+ test_change_command_coverage.py（4）全绿；审批侧回归
  （test_ops_confirmation_gate / test_approval / test_command_guards /
  test_sudo_stdin_guard_sources / test_yolo_mode / test_denial_circuit_breaker /
  test_sudo_exec，221 例）不破；行为探针三条符合预期（prod 未分级 mv →
  approve+confirm；同命令 test 档 → None 不变；classify ls -la → L1）。
- **硬约束**：_DEFAULT_GRADES / L1 排除名单未动；非 prod 行为不变（探针对比确认）；
  无新 env var；diff 只含白名单文件（tools/ops_permissions.py + 三个测试文件）。
- **已知风险（B' 噪音面）**：prod 上原本"直接跑"的未分级命令开始要审批——典型
  噪音：`helm list`（未分级查询，不在 L1 模式）、`ssh host 'df -h'` 类包装命令
  （整体未分级）、自定义脚本/单文件 mv/cp/tar。这些是 B' 的预期代价（宁可多确认
  不漏放）；若某类查询高频且确属只读，后续应补进 L1 模式（需另立批次，本批不改
  分级模式）。
- **核销方式**：approval.py `_ops_confirmation_required` 通道复检（require_confirmation
  的 approve 决策 yolo/allowlist 不绕过，批次十七 guard 测试钉住）；变更清单
  test_change_command_coverage.py 常驻，新增变更类命令需同步加样本。

### 40. 批次十八 /topo 斜杠命令 + 合并式补齐 + systemd 无端口过滤（A+B 方案落地） — ✅ 已实施（2026-08-14，批次十八；commit hash 佐证：/topo 命令 2963602 / 合并补齐 d7c22aa / systemd 过滤 68f1d8a / 落盘联动 d2a4e27）
- **背景（dogfood 实测）**：
  1. 手打 topo discover 走 LLM 路由（agent 初始化 + API 调用 3-8s/次 + 可能被打断
     "Interrupted during API call" + clarify 12s+）——用户拍板要**不依赖 LLM、本地
     直调发现引擎**的会话内入口（方案 A）。
  2. 现状 write_discovery 对已存在 host 非 force 直接拒绝（--force 整体替换会冲掉
     手动维护实体，dogfood §M 教训）——用户拍板重扫已有 host 时**新服务自动追加、
     旧手动实体保留**（方案 B，不是覆盖）。
  3. 8/14 §H：扫真实 prod 主机 117 条 services，systemd 70+ 全是系统噪音
     （aegis/chronyd/cloud-*/plymouth/networkmanager），业务只有 app/db 2 个；C1
     黑名单只挡 systemd 内部服务，其余靠 LLM 在 topo_update 手动过滤——**LLM 判断
     不是机制保证**。用户拍板：systemd-service 且无监听端口 → 不自动落盘，保留
     发现结果里标 needs_review（不误杀脚本服务，用户确认后才入表）。
- **修复（批次十八，A+B 方案落地）**：
  1. /topo 斜杠命令（2963602）：commands.py 注册（Session 组、cli_only，照 /env
     先例）+ cli.py 路由 + `_handle_topo_command`——`/topo [host...] [--env E]
     [--user U] [--key K] [--cluster C] [--force] [--yes]`，无 host 交互收集
     （host/env/凭据优先从拓扑表 credential 自动读，缺省再问 user/key）；凭据复用
     vssh 同套 askpass/vault 机制，密码明文不进 argv/命令串/日志；展示服务列表
     （needs_review=true）+ 三步 review 引导。agent 工具 topo_discover 与 CLI 子
     命令 vigil topo-discover 均保留（新增入口不替代）。
  2. 合并式补齐（d7c22aa）：write_discovery 新增 merge=True（默认）：已有 host 非
     force 时读 hosts/<host>.yaml 现有索引，发现服务同名跳过（保留现有行含手动
     endpoint/owner），新名字追加进索引 + entities/；host 行保留原内容只刷新
     last_verified；merge=False 显式关闭时维持旧拒绝语义；--force 整体替换不变；
     结果新增 appended/kept/merged 统计。
  3. systemd 无端口过滤（68f1d8a）：新增 _parse_ss_proc_ports（ss -tlnp 进程名→
     端口，含 loopback，进程名按字母数字归一容忍 node-exporter/node_exporter
     差异）；discover_host 重构 ss 探测提前到 systemd 之前——systemd 实体无监听
     端口 → 进 pending_review（不入 services/details/落盘），有端口
     （prometheus/node-exporter 等）正常入表且端口不再补 unidentified；docker/k8s
     与同名跳过逻辑不变；probes['systemctl'] 补"另 N 个无端口系统服务未入表（可
     确认）"。
  4. 落盘联动（d2a4e27）：/topo 落盘路径调 write_discovery(merge=not force)；落盘
     前打印将写入清单（host + 新增实体名）y/N 确认（默认 N，--yes 跳过；
     --dry-run 只展示）；落盘后打印"追加 N / 保留 M / 系统服务 P 未入表"。
- **验收**：tests/hermes_cli/test_topo_slash_command.py（8）+ tests/tools/
  test_topo_discovery.py（43，其中批次十八新增 8：合并 3 + systemd 过滤 3 + 更新
  2）全绿；回归 test_commands.py / test_topo_tools.py 不破（test_commands 与
  test_env_command 各 1-3 例既有失败为 HERMES_HOME/VIGIL_HOME 环境 fixture 问题，
  基线同样失败，与本批无关）；行为探针四条符合预期（/topo 参数解析+引擎被调+
  引导；write_discovery 合并语义；systemd 无端口过滤；落盘联动 merge 传递与计数）。
- **硬约束**：ops_permissions.py 未动；无新 env var（SUDO_ASKPASS 等均为既有
  机制）；不碰 conversation_loop / prompt 缓存 / 压缩；diff 只含白名单文件
  （hermes_cli/commands.py、cli.py、tools/topo_discovery.py、两个测试文件）。
- **合并补齐边界**：追加时按 name 同名跳过（现有行含手动 endpoint/owner 保留，
  不覆盖）；新 host 走正常追加；force=True 语义不变（整体替换供主动重建）。
- **已知风险**：/topo 交互式收集在 TUI（_app 非空且非主线程）下 _prompt_text_input
  会干净取消（None），交互模式主要服务经典 CLI；合并追加按 name 判重，同 host
  不同 cluster 的同名服务会视为已存在（v0.3 索引按 host 分文件，cluster 维度在
  行内）；systemd 无端口过滤依赖 ss -tlnp 有权限读到 Process 列（无权限时全部
  systemd 服务进 pending_review，是安全方向）。
- **核销方式**：/topo 走 process_command 路由 → 本地直调 discover_host/write_discovery
  （与 agent 工具同引擎）；合并/过滤统计在 write_discovery 返回值与 probes 文案
  常驻可测；测试钉住 merge/force 语义与 pending_review 边界。

### 41. 批次十九 行为层安全加固——SSH 多 key IdentitiesOnly + vault 值打码 + 熔断扩展 + sudoers.d 硬拒（§AF/§AG/§AE） — ✅ 已实施（2026-08-14，批次十九；commit hash 佐证：IdentitiesOnly 2ce48a8 / vault 打码 76c9a85 / 熔断扩展 6e947db / sudoers.d 5c86b75 / -K clarify 1fafe60 / write_file 46299b3）
- **背景（dogfood 实测，8/14 下午 NetBox 迁移实验）**：批次十六的熔断只挂
  topo_discovery runner，agent 自由拼的 ssh/scp/ansible 路径全在外 → 当天锁
  15 分钟 ×5 次；vault 密码明文复述（改密码 30 秒内再泄露）；agent 想写
  /etc/sudoers.d/ 传密码；ansible -K 密码提示挂死 terminal；inventory 同步
  报告把凭据写进 644 MD。本批 = 行为层强制约束（不再加 redact 名单——名单
  追不上 LLM 自由发挥）。
- **修复**：
  1. IdentitiesOnly 全线（2ce48a8）：vssh `_build_ssh_argv` 与 topo_discovery
     `_build_ssh_runner` 无条件加 `-o IdentitiesOnly=yes`（多 key 环境 `-i key`
     不等于只用这个 key，会遍历 agent 所有 key 刷爆 MaxAuthTries）；sudo_tool
     `_scp_argv_from_ssh` 改逐段翻译（`-p`→`-P`、其余 `-o`/`-i` 原样透传，
     scp 支持 `-o`），不再按下标取位（防 argv 加参数即碎）。
  2. vault 值打码（76c9a85）：redact 补三个盲区——①引号 YAML 值
     （`monitor_auth_token: "MsVY…"`，`_YAML_ASSIGN_RE` lookahead 与 JSON 引号
     key 要求之间的空档，新增 `_YAML_QUOTED_ASSIGN_RE` 同款 key 门控）；②任意
     键名高熵值（`{"cipher": "bW9u…"}`，严格模式值形态 pass 不再挂
     `_CFG_SECRET_WORD_RE` 门控——文本无秘密词时也运行，只靠
     `_looks_like_inline_secret` 过滤）；③`_JSON_KEY_NAMES` 补 `passwd`（精确
     短值）。值打码、key 名保留；UUID/版本号/hex/散文引号值不误伤。
  3. 熔断扩展（6e947db）：信号不只 exit 255——`Too many authentication
     failures`（sshd 限流信号）与单次 stderr 内 `Permission denied` ×2（多 key
     遍历典型输出）都计数；runner 计数逻辑重构（认证失败判定移出 returncode
     255 分支）；sudo_tool 远端 `_scp`/`_ssh_run` 接入共享计数（guard 预检 +
     note 记录/清零，模块级 dict 与 topo 探测同会话共用）；熔断错误分层归因：
     连接层错误（Too many/Permission denied）→ 停止 + 检查 IdentitiesOnly +
     重试次数；执行层错误（转义/权限）→ 才换传递方式，不换姿势掩盖真凶。
  4. sudoers.d 硬拒（5c86b75）：HARDLINE_PATTERNS 追加两类——重定向写入
     （`> / >>` 目标为 /etc/sudoers(.d)/，含 heredoc 形态）与 tee/install/cp/mv
     写入（`_CMDPOS` 锚定命令位、路径须为末参，防 `cp /etc/sudoers.d/x
     /tmp/backup` 备份误杀）；描述引导"禁止通过修改 sudoers 实现免密/传密码
     ——提权走 sudo_exec 工具（ASKPASS 注入）"；yolo 也绕不过。
  5. -K 转 clarify（1fafe60）：terminal_tool 前景执行输出后处理——输出以交互
     密码提示收尾（BECOME password / Password for / Enter passphrase / sudo
     password / password:，先剥 `_wait_for_process` 超时后缀）→ 不透传挂起，
     返回明确错误引导 clarify / `ANSIBLE_BECOME_PASS` 环境变量（值从
     vault 取，不进命令行）/ vssh/sudo_exec；长输出中间提密码、裸词 password
     不误伤。
  6. write_file 过 redact + 0600（46299b3）：write_file_tool 写入前内容过
     redact_sensitive_text（code_file=True + credential_values=True，与工具
     输出展示同一条通道）；疑似凭据 → 写打码内容 + chmod 600 + 警告；普通
     内容原样、权限不变。
  7. system prompt 行为约束（2ce48a8）：stable tier 新增静态常量
     `OPS_CREDENTIAL_SSH_GUIDANCE`（ops/terminal 工具就位即注入，字节稳定不
     破 prompt 缓存）——SSH 系命令必须带 IdentitiesOnly、优先 vssh/sudo_exec、
     凭据值只许 env/askpass/stdin 注入禁止复述/落盘、熔断后停止重试 + 用户
     纠正即停、sudoers.d 禁写、-bK 优先 ANSIBLE_BECOME_PASS。
- **验收**：新增/扩展测试全绿——test_batch14_e3_vssh.py（argv 各形态 +
  IdentitiesOnly 专项）、test_topo_discovery.py（runner 探测路径）、
  test_redact_vault_forms.py（三形态 + 误伤反向）、test_ssh_auth_breaker.py
  （信号扩展 2 + 归因 1 + sudo_tool 共享计数 1）、test_hardline_blocklist.py
  （sudoers.d 写入 14 + 读取 11 + yolo 场景）、test_terminal_password_prompt.py
  （19 例）、test_file_tools.py（4 例）；回归 test_sudo_exec.py / test_topo_tools.py /
  test_system_prompt.py / 全部 redact 套件不破；行为探针 6 条符合预期。
- **硬约束**：diff 只含白名单文件（vssh.py、sudo_tool.py、topo_discovery.py、
  approval.py、redact.py、file_tools.py、terminal_tool.py【任务 5 正文点名挂载
  点】、prompt_builder.py/system_prompt.py 仅常量文本、对应新测试、OPS-DELTA）；
  无新 env var；凭据值不进 argv/命令串/日志；不碰 conversation_loop/prompt
  缓存/压缩；批次十六 sudo_exec/redact 三盲区未破坏（测试回归佐证）。
- **任务 2 影响评估（值打码对正常 vault 读取）**：三形态（精确键名、引号 YAML、
  任意键名高熵值）值打码、key 名保留——agent 仍能看到"哪些凭据存在"，只是
  值不可复述，可读性损失小；误伤面被 `_looks_like_inline_secret` 收窄
  （≥16 字符、混合字符类、熵 ≥3.2、无空格、无 `/ \ : .`）——UUID/hex/版本号/
  短占位值/散文引号值全部豁免；短精确键值（`"passwd": "shortpw"`）按 key 名
  打码，与既有 `"password"` 语义一致。
- **任务 3 共享状态说明**：计数器保持 topo_discovery 模块级 dict（进程内存，
  host:user 独立，线程锁保护）；sudo_tool 通过函数 import 接入同一 dict——同一
  进程内 vssh（exec 不计数）/sudo_exec/topo 探测路径共用；成功一次清零；
  vssh 是 os.execvpe 无返回值，不计数不受影响（既有测试钉住）。
- **已知风险**：①write_file redact 误伤面——高熵非凭据值（如 32 字符随机
  标识符、`{"nonce": "AbCdEfGhIjKlMnOpQrStUvWxYz012345"}`）可能被打码；短
  占位值（`apiKey: "test"`）按 key 名打码（与展示通道一致）——写真实配置
  时若含占位凭据会被打码，需用户手动修正；②熔断按 host:user 计数，agent
  对同一 host 换 user 试登录会另起计数（凭据缺失 fail-closed 已在 sudo_exec
  挡）；③`_is_ssh_auth_failure` 对 exit 255 + 单次 Permission denied 的远端
  命令退出码与 ssh 认证失败无法 100% 区分（批次十六既有语义，非本批引入）；
  ④terminal -K 识别是输出后置（挂起到超时才报错）——前置识别需改
  environments/base.py（白名单外），本批不落地。
- **核销方式**：测试常驻——vssh argv 含 IdentitiesOnly、redact 三形态值打码、
  熔断 3 次即断 + 错误含 IdentitiesOnly、sudoers.d 写入 hardline 拦截（yolo
  场景）、terminal 密码提示转 clarify、write_file 敏感内容 600+打码；行为
  探针命令在批次十九 prompt 验收段可复跑。

### 42. 批次二十 审批交互体验——审批框选项按场景过滤（session/always 无效选项）+ 审批/clarify 超时策略 wait 化 — ✅ 已实施（2026-08-14，批次二十；commit hash 佐证：任务 1 选项过滤 3f4a4f5 / 任务 2 超时 wait 化 5d9252a / 任务 3 clarify 对齐 206fbad）
- **背景（用户 8/14 上午发现 + dogfood §I）**：① prod 变更确认门（require_confirmation）
  弹审批框仍显示 "session"/"always"——但确认门在 approval.py 的 allowlist 短路
  **之前**判定，选了也不生效（下次照样弹），误导用户以为已授权；② 多 session
  场景（gateway 推 Telegram/Discord + 多个 CLI 会话）下 approvals.timeout 默认
  300s 超时静默 deny，用户根本看不见请求就被拒绝——"用户没机会决定"违反复核
  语义。判定层本身是对的（确认门短路 + fail-closed 不变），本批只修交互层。
- **修复（任务 1，选项过滤）**：
  1. `tools/approval.py`：`prompt_dangerous_approval` 新增 `allow_session` 参数并
     透传给回调（旧签名回调 TypeError 时自动去 allow_session 重试，兼容旧调用）；
     `_run_approval_gate` 新增 `allow_permanent`/`allow_session` 参数（gateway
     approval_data 不再写死 True）；`check_all_command_guards` Phase 3 对
     `_ops_confirmation_required`（prod 确认门）传 `allow_permanent=False` +
     `allow_session=False` 给 gateway 与 CLI 两条 surface——非 prod 普通审批保持
     原作用域不变。
  2. `hermes_cli/callbacks.py` `approval_callback` 签名加
     `allow_permanent=True, allow_session=True`（默认值，兼容旧调用），choices 按
     参数过滤（prod 门 → 只剩 `["once","deny"]`，+view 逻辑保留）。
  3. `cli.py` `HermesCLI._approval_callback`/`_approval_choices`（实际注册的 CLI
     交互框）同步加 `allow_session`，choices 过滤后编号/高亮提示自动只显示有效项。
  4. TUI/API 展示层：`tui_gateway/server.py` `_emit_approval_request` 与
     `gateway/platforms/api_server.py` `_approval_event_choices` 增加
     `allow_session is False` → `["once","deny"]` 分支（choices 渲染处同步隐藏
     session 提示）。
- **修复（任务 2，超时 wait 化）**：`hermes_cli/config_defaults.py` approvals 段新增
  `timeout_policy: "wait"`（默认 wait：超时后不自动 deny 也不自动批准，fail-closed
  保持 pending，超时仅作"提醒间隔"；`"deny"` = 旧行为超时拒绝）。`_await_gateway_decision`
  wait 模式超时后重推一次通知并继续挂起等待（interrupt 仍可中断 → deny）；CLI
  交互框（cli.py + callbacks.py）wait 模式超时打印"审批仍在等待"并继续等；非交互
  input() 循环 wait 模式 join 超时后继续等同一个输入线程。判定层未动：timeout
  永远不等于批准。
- **修复（任务 3，clarify 对齐）**：cli.py `_clarify_callback` + callbacks.py
  `clarify_callback` 对齐同一 `approvals.timeout_policy`——wait 超时后不自动
  "agent will decide"，打印提示继续等；deny 保持旧行为。`<=0` 无限等待语义
  不变（wait 模式下 `timeout<=0` = null deadline，与既有 clarify 语义一致）。
- **验收**：新增 tests/tools/test_approval_choices_filter.py（choices 过滤 5 组 +
  view、HermesCLI._approval_choices 5 例、prod 确认门回调收到 False/False、非 prod
  保持双作用域、旧签名回调兼容 2 例）+ tests/tools/test_approval_timeout_policy.py
  （默认值 wait、gateway wait 重推 2 次 / deny 单次通知 timeout、CLI wait 越过
  deadline 继续等 / deny 返回 timeout、input 循环 wait 继续等、clarify wait 继续等
  / deny 自动跳过）；回归 test_approval*.py / test_ops_confirmation_gate.py /
  test_command_guards.py / test_ops_permissions_guard.py / test_clarify_gateway.py /
  test_cli_approval_ui.py / test_tui_approval_redaction.py / test_api_server_runs
  （choices）/ test_commands.py 全部通过（基线既有 9 例环境噪音失败不变：config
  readonly ×2 + mode_parity ×6 + slack config gate ×1，stash 基线同命令同结果）；
  行为探针 5 条符合预期。
- **注册点清单（重点）**：`set_approval_callback` 实际注册点为 `HermesCLI._approval_callback`
  （cli.py:7512/14358 + cli_commands_mixin.py:1980 后台任务）——本批已同步签名；
  `computer_use` 独立回调经 `_computer_use_approval_callback` 转发不受影响；
  background_review / delegate_tool / acp 回调均带 **kwargs 或 **_，新 kwarg 天然
  兼容。签名改动影响面：CLI（choices 过滤 + wait 策略生效）、TUI/desktop
  （`approval.request` choices 载荷过滤）、gateway（approval_data 标志位传适配器，
  wait 重推通知）；判定层（L3/L4 deny/hardline/prod 确认门优先级）零改动。
- **已知风险（wait 挂起对 agent 循环的影响）**：wait 模式审批/clarify 长期挂起会
  阻塞当前 agent 执行线程（与旧 deny 模式同样阻塞，只是不再自动解除）——后续
  工具调用排队等待，用户可能误以为卡死；缓解：①每间隔打印/重推"仍在等待"提示，
  ②gateway 有 interrupt（/stop、/new、inactivity）逃生口立即 resolve deny，
  ③多 session 用户可在任一会话 /approve|/deny 解除。CLI 单会话场景若用户完全
  离开，会一直挂到用户回来或 Ctrl+C——这是"不静默拒绝"的代价（fail-closed
  保持 pending，绝不自动批准）；若后续要"挂起但不阻塞"（agent 先做别的），
  需要把审批状态提升为全局 pending 队列（排期方案 B，本批未做）。
- **核销方式**：测试常驻——prod 确认门回调 kwargs 双 False、choices 过滤、
  timeout_policy 默认 wait、gateway wait 重推通知、CLI wait 越过 deadline 继续
  等；行为探针命令在批次二十 prompt 验收段可复跑。

### 43. 批次二十一 凭据 fail-closed 硬化——认证失败停+问用户，禁止自探测（§Q/§AD/§AF 收口） — ✅ 已实施（2026-08-14，批次二十一；commit hash 佐证：任务 1 认证失败停+问 857b364 / 任务 2 凭据缺失 fail-closed 969e008 / 任务 3 force_trip dd57bcc / 任务 4 vssh 双路径 3440d75）
- **背景（§Q/§AD/§AF 连续 3 天实测）**：批次十六熔断只约束"失败 3 次停止重试"，
  "停之后"没约束——agent 熔断后换工具/换姿势继续试（§AF 补丁 2 实锤 heredoc→管道→
  base64）；凭据缺失时自行翻 ~/.ssh/ 试密钥/猜 vault 字段（§Q 实锤）；用户纠正方向
  后仍换姿势重试（§AF 补丁 2 需求 2）。根因层 = 行为层缺 fail-closed：凭据缺失/
  认证失败/被纠正 → 停 + 问用户。
- **修复（任务 1，认证失败 → 停 + 问用户）**：
  1. `tools/topo_discovery.py` `_ssh_auth_breaker_error` 扩展：熔断错误信息加
     "请勿换用户名/换 key/翻 ~/.ssh/ 继续尝试（会触发 sshd 限流锁 15 分钟）" +
     "3) 或询问用户提供正确凭据"（既有 1) 手动 ssh 验证凭据 2) 补充拓扑表
     credential 声明保留，全链路可操作）。
  2. `tools/sudo_tool.py`：本地/远端 sudo 认证失败（stderr 命中 incorrect
     password / sorry try again / password required 等信号）→ 返回"停止自动重试 +
     询问用户提供正确密码 + 禁止连续猜 vault 字段"引导；熔断 RuntimeError 经
     handler 原样透传（现含问用户指令）。
  3. `agent/prompt_builder.py` `OPS_CREDENTIAL_SSH_GUIDANCE` 新增：认证失败 →
     停止尝试并询问用户；禁止翻 ~/.ssh/ 找 key、试多个用户名、连续猜 vault 字段、
     换工具换姿势重试同一目标（会触发 MaxAuthTries 限流锁 15 分钟）；正确做法 =
     报告失败原因 + 请用户提供凭据或手动执行。
- **修复（任务 2，凭据缺失 → 问用户，禁止自探测）**：
  1. `_build_ssh_runner` fail-closed：无 SSH 认证凭据（无 key_path/askpass_file/
     key_passphrase_file）→ 直接报"目标主机未配置 SSH 凭据（拓扑表 credential
     缺失）"错误，**不再回退"尝试默认 key/ssh-agent"**（§Q 根因）；sudo_password_file
     是 sudo 密码不是 SSH 认证凭据，单独提供不放行。
  2. `tools/sudo_tool.py` 无凭据消息扩展：3) 或询问用户提供正确凭据；禁止翻
     ~/.ssh/ 试密钥/猜 vault 字段/换用户名试登录。
  3. prompt 常量新增：凭据缺失/未配置时询问用户，禁止自行翻 ~/.ssh/ 找 key、
     试多个用户名、猜 vault 字段名。
- **修复（任务 3，用户纠正 → 停止当前路径）**：`tools/topo_discovery.py` 熔断
  计数器加 `force_trip(host, user)` 方法——外部检测到用户纠正信号后调用，该
  host:user 立即进入熔断态（等价 3 次失败），runner/guard 入口直接停试、零 ssh
  调用（sudo_tool 共享计数同样生效）；prompt 常量强化：用户纠正操作方向时立即
  停止当前尝试路径，不换姿势/换工具重试同一目标，先确认正确做法（检查工具清单/
  问用户）再继续。会话层"检测纠正信号"的接线点不在本批白名单（conversation
  loop），以 prompt 约束 + force_trip 可供接线实现。
- **修复（任务 4，vssh 凭据解析双路径对齐）**：`_resolve_topology_credential`
  加 `allow_fallback: bool = True` 参数——CLI 交互路径（用户手动 `vigil vssh`，
  默认 True）保留 None → ssh-agent/交互回退；agent 工具路径（`sudo_exec` 传
  False）把 None 视为硬停，调用方 fail-closed 报错问用户，不自行翻 ~/.ssh/。
- **验收**：新增 tests/tools/test_credential_fail_closed.py（无凭据零 ssh 调用、
  discover_host 透传、key_path/askpass 正常、sudo_password 单独不放行）+ 追加
  test_ssh_auth_breaker.py（熔断错误含禁自探测指令、force_trip 立即熔断/零调用/
  host 隔离/sudo_tool guard 联动）+ test_sudo_exec.py（本地 sudo 认证失败问用户、
  breaker 错误透传）+ test_batch14_e3_vssh.py（allow_fallback=False 无凭据 None、
  有凭据正常、sudo_exec 传 False 接线）+ test_system_prompt.py（prompt 常量三
  行为约束 + ops 工具加载时进 stable tier）。回归 test_topo_discovery.py /
  test_topo_tools.py / test_topo_update_contract.py / test_topo_v2.py /
  test_sudo_stdin_guard_sources.py / test_topo_slash_command.py 全部通过
  （174 passed, 6 skipped 为 test_topo_v2 既有版本 skip）。
- **硬约束核对**：diff 白名单 = tools/sudo_tool.py、tools/topo_discovery.py、
  hermes_cli/subcommands/vssh.py、agent/prompt_builder.py（行为约束常量）、对应
  新测试 + 既有测试更新、OPS-DELTA.md——共 10 文件；无新 env var；批次十六熔断
  判定逻辑未改（3 次计数语义不变，只扩错误信息 + 加 force_trip）；sudo_exec 工具
  本体未改（只加认证失败引导分支 + 消息）。
- **任务 2 影响面（不再 fallback 默认 key）**：依赖无凭据 → ssh-agent/默认 key
  回退的合法流程 = CLI 交互路径 `topo-discover`（无 --key/无密码时此前走
  BatchMode）+ agent 工具 `topo_discover`（无 key 参数）。本批后两路径在无凭据
  时都返回"未配置凭据"错误——CLI 用户在场，用 --key / 交互密码 / 拓扑表 credential
  声明即可恢复（错误消息含 3 步引导）；agent 工具路径正是 §Q 自探测源头，fail-closed
  是目标。CLI `_prompt_credentials` 的"使用 ssh-agent / 默认 key"提示行保留（非
  白名单文件），紧随其后的 fail-closed 错误会明确覆盖其语义。
- **已知风险（标准位置 key 误伤评估）**：fail-closed 会拒绝"agent 用 ~/.ssh/id_rsa
  直接连"的场景（§Q 教训的正反两面）——评估结论：不放行"标准位置有 key"豁免。
  理由：①§Q 实锤"翻 ~/.ssh/"正是自探测，豁免会重新打开探测口（agent 无法区分
  "标准 key"与"翻找"）；②OpenSSH 默认 key 同样触发 MaxAuthTries 遍历；③逃生口
  已存在：用户把 key 声明进拓扑表（`credential: {type: ssh_key, ref: ~/.ssh/id_rsa}`）
  或 `--key`/vssh 显式指定，一行声明即放行，无需探测。误伤面 = 未声明凭据的既有
  自动化流程，报错信息含完整 3 步引导可恢复。
- **已知风险（force_trip 接线点）**：会话层"检测用户纠正信号"（新对话轮次明确
  否定）→ 调 force_trip 的接线不在本批白名单内（需动 conversation loop），本批
  落地为 prompt 行为约束 + 可接线 API；若后续发现纠正后仍自试，需在 run_agent/
  cli 检测否定反馈时调 force_trip（排期）。
- **核销方式**：测试常驻——test_credential_fail_closed.py（零 ssh 调用断言）、
  test_ssh_auth_breaker.py（force_trip + 熔断消息指令）、test_sudo_exec.py（认证
  失败问用户）、test_batch14_e3_vssh.py（allow_fallback 接线）；行为探针命令在
  批次二十一 prompt 验收段可复跑。

### 44. 批次二十二 /help 清理——~/.hermes→~/.vigil 文案残留 + Examples 运维化 + 折叠命令可查性（§G 收尾） — ✅ 已实施（2026-08-14，批次二十二；commit hash 佐证：任务 1 文案清理 599ec50 / 任务 2 Examples b366672 / 任务 3 可查性 0b5aff6）
- **背景（§G 需求 3/4/5）**：①用户机器数据根是 ~/.vigil，help 文案仍写
  ~/.hermes/... 会误导排查（dogfood 2026-08-13 实测 9 处）；②`vigil -h` 底部
  Examples 是上游模板（hermes-agent-dev,github-auth / gateway install），对运维
  用户无用且误导；③58 个继承命令折叠成一行，用户不知道里面有什么、怎么查。
- **任务 1（~/.hermes → ~/.vigil 用户可见文案清理，13 处 help/description）**：
  1. `hermes_cli/main.py:11663` checkpoints help（`Inspect / prune / clear ~/.hermes/checkpoints/`）。
  2. `hermes_cli/main.py:11403` secrets description（`~/.hermes/.env` → `~/.vigil/.env`）。
  3. `hermes_cli/_parser.py:283` 顶层 `--ignore-user-config` help。
  4. `hermes_cli/_parser.py:476` chat 版 `--ignore-user-config` help。
  5. `hermes_cli/subcommands/acp.py:41` `--setup-browser` help（node/）。
  6. `hermes_cli/subcommands/approvals.py:74` `--db` help（state.db）。
  7. `hermes_cli/subcommands/claw.py:57-58` `--no-backup` help（zip snapshot + backups/）。
  8. `hermes_cli/subcommands/webhook.py:61` `--script` help（scripts/）。
  9. `hermes_cli/subcommands/hooks.py:19-21` description（config.yaml + shell-hooks-allowlist.json）。
  10. `hermes_cli/subcommands/security.py:21` description（plugins/）。
  11. `hermes_cli/subcommands/skills.py:164` reset description（skills/.bundled_manifest）。
  12. `hermes_cli/subcommands/cron.py:51,123` `--script` help（scripts/，两处）。
  13. `hermes_cli/subcommands/dashboard.py:185` register description（.env）。
  14. `hermes_cli/subcommands/gateway.py:264,303` relay description（.env，两处）。
  15. `hermes_cli/curator.py:808,819` backup/rollback help（skills/，两处）——继承命令
      help，`--help-all` 逐条展示，任务 1 全仓 grep 漏网收编（curator.py 在白名单
      边缘：hermes_cli/ 下但非 main/_parser/subcommands，属"继承命令 description 由
      Vigil 展示，改它不违反"的明示类别）。
- **任务 2（Examples 换运维场景）**：`hermes_cli/_parser.py` `_EPILOGUE` 上游模板
  全删，换 6 条运维用例——`vigil chat`（完整 ops harness）/ `topo-discover -e prod
  -H 10.0.1.29`（SSH 扫描）/ `watch status` / `vssh node1`（凭据注入，密码不进命令行）/
  `config set model.default deepseek-v4-flash` / `setup`。命令名/参数形态按当前真实
  parser 核对（topo-discover/vssh/watch 均存在）。
- **任务 3（折叠命令可查性）**：
  1. 折叠行提示补全：`完整列表与说明见：vigil --help-all` + 新增
     `运维常用继承命令：doctor / sessions / cron / skills（用法：vigil <命令> --help）`。
  2. `--help-all` 改用 `_ExpandedGroupedHelpFormatter`（继承 `_GroupedHelpFormatter`，
     仅 `expand_inherited=True`）：Vigil 命令一组 + 继承命令一组逐条完整展开，
     与 `-h` 同分组渲染（原来平铺）。
  3. 展开行覆盖所有可调用名字：argparse 只为带 `help=` 的子命令生成伪 action，
     别名（learning/memory-graph/gui）与无 help 的弃用命令（login）需专门处理——
     别名复用正名 help、login 用子 parser description 兜底（弃用指引在 --help-all
     可见），保证"全量"名副其实、`-h`/`--help-all` 数量一致（56 个）。
  4. 运维高频白名单：`_VIGIL_COMMANDS` 15 个——config/logs/status 原有已覆盖，
     新补 `doctor`/`sessions`；`resume` 未提（sessions 已覆盖会话管理，折叠行提示
     点 doctor/sessions/cron/skills 四个即可，不过度膨胀 Vigil 组）。
- **验收**：新增 tests/hermes_cli/test_help_text_cleanup.py——7 用例：18 条
  help/description 旧串消失+新串就位（读源码断言）、`-h`/`--help-all` 输出无
  ~/.hermes 且含 ~/.vigil（行为探针）、_EPILOGUE 含 6 运维示例不含上游模板、
  `-h` Examples 段含 topo-discover/vssh、_VIGIL_COMMANDS 覆盖 config/logs/status/
  doctor/sessions、折叠行含 --help-all 提示+运维常用提示、`--help-all` 分组标题+
  逐条展开（moa/gateway/secrets/egress/cron 行 + gui/learning/memory-graph/login
  别名与弃用命令行）。E1 测试 test_batch14_e1_help_grouping.py 更新
  test_help_all_lists_inherited_commands（"继承命令（来自 hermes" not in out →
  分组标题 + 逐条行断言，批次二十二新渲染）。回归：test_help_text_cleanup.py 7 +
  test_batch14_e1_help_grouping.py 4 + test_commands.py + test_subparser_routing_
  fallback.py + test_argparse_flag_propagation.py + test_startup_plugin_gating.py
  全绿（62 passed；仅既有基线噪音 test_config_gate_included_in_slack_when_on ×1
  与 test_gateway_service.py systemd ×5、test_relaunch.py ×2 在 base 同样失败，
  非本批引入）。
- **硬约束核对**：diff 白名单 = hermes_cli/main.py、hermes_cli/_parser.py、
  hermes_cli/subcommands/*.py、hermes_cli/curator.py（继承命令 help 收编，见上）、
  对应新测试 + E1 测试更新、OPS-DELTA.md——共 16 文件；无新 env var；无命令删除
  （git diff 无 CommandDef 删除，login 等保留可调用）；不碰 conversation_loop /
  prompt 缓存；数据根语义不变（display_hermes_home() 用户可见路径 ~/.vigil，
  hermes_constants.py:787）。
- **残留终扫（grep -rn \".hermes\" hermes_cli/ cli.py）**：help=/description=/epilog
  上下文残留 = 0。其余 ~340 处全部保留，分三类：①代码注释/模块 docstring（如
  main.py:680,2703,3524,3535、auth.py 各段、config_defaults.py 注释）——兼容层
  说明，硬约束 4 明示保留；②运行时输出/错误消息（如 main.py:10094 dashboard
  register 错误引导 print "writes HERMES_DASHBOARD_OAUTH_CLIENT_ID into
  ~/.hermes/.env for you"）——非 help/description/epilog，本批 grep 范围外，且该
  print 依赖具体写入实现路径，改动属行为层（排期单独处理）；③真实逻辑引用
  （HERMES_HOME 环境名、.env 兼容加载、uninstall 清理路径等）——语义正确不能改。
- **已知风险**：①文案改动对依赖旧路径文案的测试的影响——已扫全仓，仅本批新增
  测试引用旧串（作为消失断言），test_cli_preloaded_skills 等含 "hermes-agent-dev"
  的是技能名断言，与 Examples 无联动；②curator.py 在硬约束 5 白名单边缘，若
  后续批次收紧白名单需连同说明一起评审；③--help-all 逐条展开后 login（无 help）
  靠 description 兜底显示，若未来给 login 加回 help= 行为不变（仍显示）；④继承
  命令数量会随上游增减变化，测试用 \d+ 正则 + ≥50 下限，不锁死具体数字（行为
  契约而非快照）。
- **核销方式**：测试常驻——test_help_text_cleanup.py（7 用例全自动断言）；行为
  探针命令可复跑：`vigil -h | grep -c '~/.hermes'` = 0、`vigil -h` Examples 段含
  topo-discover、`vigil --help-all` 分组逐条可读。

### 45. 批次二十三 Trajectory 运行轨迹——事件级记录 + 审计查询/回放（DSH/TencentDB 借鉴，Vigil 独缺补上） — ✅ 已实施（2026-08-14，批次二十三；commit hash 佐证：任务 1 事件记录 77a992c / 任务 2 查询命令 40c8b60 / 任务 3 复盘回放 237dad3）
- **背景**：运行轨迹/使用记录三家对比中 Vigil 独缺（DSH 有 append-only
  SessionEvent log + 可回放；TencentDB 控制层有使用记录；Vigil 只有审批审计）。
  运维审计合规是硬需求——出事时要知道"agent 当时看到了什么、为什么这么决策、
  执行了什么"。本批落地轻量版：审计合规（谁在什么时间执行了什么命令、结果
  如何）+ 事故复盘（当时的决策链）。不做 DSH 的 projection 折叠架构。
- **任务 1（事件级记录，agent/trajectory.py 扩展 + 挂载点注入）**：
  1. `record_event(**kwargs)`：append-only JSONL 落盘 <数据根>/trajectory/
     <session_id>.jsonl；事件字段 ts/session_id/seq/type/tool/action/result/
     approval/meta；seq 会话内递增（启动时读一次文件行数作基数，跨进程续号）。
  2. **强制 redact**：action/result 过 redact_sensitive_text(credential_values=
     True, force=True)——凭据值绝不落轨迹（`sudo -S <<< 'hunter2secret'` →
     `sudo -S <<< '***'`，`ghp_xxx` → `ghp_ab...3456`，`curl -u admin:pass` →
     `admin:***`）。
  3. **容量控制**：单 session 上限 5000 条（MAX_EVENTS_PER_SESSION），超限记
     一条 trajectory_truncated 后该 session 停止记录（防 runaway 会话撑爆磁盘）。
  4. **best-effort**：任何失败只记 debug 日志，绝不干扰执行路径（挂载点用
     try/except 包裹）。
  5. 现有 save_trajectory（ShareGPT conversation dump）保留，事件通道为新增
     并行通道，互不替代。
- **任务 2（`vigil trajectory` 查询命令，hermes_cli/subcommands/trajectory.py
  新增 + main.py 注册）**：
  - `trajectory list`：列出所有轨迹文件（session + 事件数 + 首末事件时间跨度）。
  - `trajectory show <session_id>`：按 seq 时间正序打印事件流；`--type
    terminal` 只看命令事件（type 或 tool 命中，审计核心）；`--type approval`
    只看审批事件。
  - `trajectory search <query>`：跨 session 子串搜 action/result。
  - `trajectory prune --before <ISO>`：删最后活动早于指定时间的轨迹文件
    （保留期管理，默认不自动删）；naive 输入按本地时区解释。
  - 单行可 grep 输出；show 的 session_id 做路径安全净化（防穿越）。
- **任务 3（复盘回放，--replay）**：紧凑时间线模式——`[HH:MM:SS] +2.5s
  [tool_result] terminal: <action> → <result>`，事件间间隔标注（识别审批 300s
  等待/熔断锁 15 分钟等待等卡顿点）；`--replay --approval` 只看审批时间线
  （复核"哪些命令被批准了"）。
- **挂载点清单（最小侵入，全部 best-effort 单行调用）**：
  1. terminal 工具调用：`tools/terminal_tool.py`——前台分支 `env.execute` 前记
     tool_call、result_dict 构建后 return 前记 tool_result（result=exit_code +
     输出前 200 字符）；后台分支 spawn 前记 tool_call、启动成功 return 前记
     tool_result（"background started (pid N)"）；命令被用户中断（rc=130 +
     [Command interrupted] marker）追加记 interrupt 事件；顶层异常记 error
     事件。共 6 处挂载点，均不改变返回路径。
  2. 审批事件：`tools/approval.py`——`_run_approval_gate`（check_dangerous_
     command / request_tool_approval 的汇聚核心）与 `check_all_command_guards`
     （terminal 命令守卫）的 gateway/CLI 两条请求路径：prompt/notify 前记
     requested，各结果 return 前记 approved/denied/timeout；无 notify callback
     的排队路径记 requested(pending_approval)。共 14 处挂载点（每处 1 行）。
  3. interrupt/error：**未改 run_agent.py**（不在本批白名单）——interrupt 事件
     由 terminal 挂载点检测 rc=130 + 中断 marker 记录（命令级中断即审计关注点）；
     error 事件由 terminal 顶层异常路径记录。会话级 interrupt 需动 conversation
     loop，属后续排期（prompt 任务 1.3 的 run_agent.py 挂点因白名单冲突下沉，
     语义等价：审计可见"命令被中断"）。
- **验收测试**：tests/agent/test_trajectory_events.py（11 用例：落盘/seq/append-
  only、sudo 密码与 URL userinfo 打码、5000 截断+停止、写入失败不抛、save_
  trajectory 回归、真实 terminal 执行产生 call/result 事件、真实执行命令含密钥
  打码、两次执行两对事件、审批 deny/timeout/approved 各记 requested+结果）+
  tests/hermes_cli/test_trajectory_cmd.py（8 用例：list 两 session、show 排序与
  --type 过滤、缺失 session 报错、search 命中/不命中、prune 删旧留新、
  --replay 时间线 +2.5s/+3.0s 间隔、--approval 过滤）。回归：test_terminal_tool* 4
  套件 + test_approval* 3 套件 + test_subparser_routing_fallback.py +
  test_batch14_e1_help_grouping.py + test_help_text_cleanup.py 全绿
  （181 passed；既有基线噪音不在本批套件内）。
- **硬约束核对**：diff 白名单 = agent/trajectory.py、tools/terminal_tool.py、
  tools/approval.py、hermes_cli/subcommands/trajectory.py、hermes_cli/main.py、
  对应新测试、OPS-DELTA.md——共 8 文件；无新 env var；**conversation_loop 未碰**
  （run_agent.py 零改动，验证方式：git diff 无 run_agent.py/model_tools.py/
  cli.py；挂载点全部在工具执行层 best-effort 单行调用，不改变返回路径）；
  现有 trajectory JSONL 兼容（save_trajectory 原样保留）。
- **行为探针（实测）**：python3 -c 模拟 terminal（含密码）+ 审批 → JSONL 4 条
  seq 1-4、`secret leaked: False`、action 打码 `sudo -S <<< '***' ...`；
  `vigil trajectory list` 显示 2 个 session 及事件数；`show --type approval`
  只出审批；`--replay` 输出 `[HH:MM:SS] [tool_call] terminal: ...` 紧凑时间线。
- **事件量性能影响评估**：record_event 单事件约 52µs（2000 事件 104ms 实测，
  含 redact + JSONL append）；每次 terminal 执行 2 条事件约 0.1ms，相对命令
  执行时间（秒级）可忽略；审批事件仅在有人工审批发生时产生（低频）。写入为
  单行 append（O(1)），不读不重写文件（仅跨进程启动时读一次行数）。
- **已知风险**：①磁盘长期占用——每条事件 ~200-400 字节，5000 条上限下单
  session 最大 ~2MB；建议保留期（如 90 天）用 `vigil trajectory prune --before`
  定期清理（命令已提供，默认不自动删）。②redact 截断策略——result 只存输出
  前 200 字符（事故复盘看的是"执行了什么/结果如何"，完整输出在会话内仍可
  复现；命令输出已在 terminal 工具层 redact 过，事件层二次 redact 兜底）。
  ③审批 action 用 command 或 description——tirith 描述可能含长文本，事件层
  只存摘要字段不存完整描述。④seq 跨进程续号依赖文件行数，若事件文件被外部
  截断/损坏则续号回退（append-only 语义下正常使用不会发生）。
- **核销方式**：测试常驻——test_trajectory_events.py（事件记录/redact/截断/
  挂载点）+ test_trajectory_cmd.py（查询/回放）；行为探针命令在批次二十三
  prompt 验收段可复跑。

### 46. 批次二十四 拓扑状态自动同步——topo_status_sync 差异检测/确认落盘 + 状态变更主动询问（§R） — ✅ 已实施（2026-08-14，批次二十四；commit hash 佐证：任务 1 工具 6a744c9 / 任务 2 行为约束 1743d98 / 任务 3 描述引导 3c1c117）

- **为什么**：§R 实测（2026-08-13 停 snipe-it）——容器 stop 后拓扑实体状态不
  自动同步，用户需专门补"同步为 stopped"才改 entities/hosts 文件。期望：状态
  变更后实体状态自动跟进或主动询问。现状 topo_query（查）/topo_update（改）
  都没有"对比拓扑状态 vs 实际容器状态"的差异检测能力。
- **怎么改**（3 文件 + 2 测试文件，均在白名单内）：
  1. **任务 1（工具）** `tools/topo_tools.py` 新增 `topo_status_sync` agent 工具
     （注册进 `topo` toolset，check_fn=check_topo_requirements 同 topo_query）：
     - 参数 `host`（可选，默认全部；也接受实体名）+ `confirm`（bool，默认 False）。
     - 读拓扑表（复用 load_topology / _all_core_entities / _entity_env），对
       docker 容器实体（`attrs.container` / `type=docker-container` /
       `attrs.runtime=docker`）执行 `docker ps -a --format '{{json .}}'` 检查
       实际状态；**复用发现引擎 `topo_discovery._parse_docker_ps`**（模块级函数
       直接 import，未新写探测，topo_discovery.py 零改动）。
     - **L3 合并**：容器实体的 attrs.container/status 存第三层 detail 档案
       （topo_update 写 status 的目标文件），L2 索引行只有引用——`topo_status_sync`
       先 `_enrich_entity_detail` 合并 L3 再判断，避免误判"无容器实体"。
     - 差异判定：实际状态可确定（docker ps 明确 running/exited）且与拓扑 status
       不一致 → `action=update`；不确定（容器缺失/主机不可达/无 docker 权限）→
       `action=ask`；实体未声明 status 时**不自动覆盖手动值**（容器实际 stopped
       且未声明 → ask，避免覆盖手工维护）。
     - `confirm=False`（默认）dry-run 只报告差异不落盘；`confirm=True` 对
       action=update 实体复用 `topo_update` 写 status（PROD 实体仍走审批矩阵，
       action=ask 不动）。docker 原始 State → 既有 status 语义映射（exited/
       created/dead/removing/paused → stopped，restarting → running），**不引入
       新枚举**。返回 `{checked, changed, confirm, differences, needs_user,
       write_errors}`。
     - 探测 runner 复用发现引擎 `_build_ssh_runner`/`_build_local_runner`：
       SSH 凭据走拓扑表 credential 引用（ssh_key → key_path 引用，密码类凭据
       runner 内部 askpass 注入），凭据值不进 argv/日志。
  2. **任务 2（行为约束）** `agent/prompt_builder.py` 新增静态常量
     `OPS_TOPOLOGY_SYNC_GUIDANCE`（不读环境/会话，随 stable tier 进入缓存前缀，
     字节稳定）；`agent/system_prompt.py` 在 topo 工具加载时注入 stable tier——
     "执行改变容器/服务状态的命令（docker stop/start/restart/rm、systemctl、
     kubectl scale/delete/restart、docker compose down/up）后主动跑
     topo_status_sync 检查差异，不一致时向用户报告并询问'是否同步拓扑状态？'，
     用户确认后 confirm=True；不要假设拓扑自动更新"。`topo_status_sync` 加入
     `_OPS_SECURITY_TOOLS`（该工具会 SSH 探测，SSH 纪律块同步生效）。
  3. **任务 3（描述引导）** `topo_tools.py` topo_update schema 描述补一句：
     "状态变更（容器 stop/start 等）请先运行 topo_status_sync 检测差异，再确认
     同步；手动 topo_update 写 status 仅用于 topo_status_sync 无法确定的状态"
     ——引导 agent 走正规链路，不绕过工具直接 patch 文件（§P/§R 问题 2）。
- **验收测试**：tests/tools/test_topo_status_sync.py（新，10 用例：dry-run 检测
  差异不落盘 / confirm 落盘 status=stopped / docker 不可用 → ask 不落盘 / 无差异
  checked>0 changed=0 / host 过滤 / 未声明 status 不覆盖 → ask / handler 透传 /
  registry 注册 + toolset 成员 + schema 引导）；tests/agent/test_system_prompt.py
  增量 3 用例（常量含 topo_status_sync + 命令清单关键词、topo 工具加载时进
  stable tier、无 topo 工具不注入）。回归：tests/tools/test_topo_tools.py +
  test_topo_discovery.py + test_system_prompt.py 全绿（91 passed）。
- **行为探针（实测）**：mock docker ps（exited）+ 实体 running → 差异 detected
  （action=update/write_status=stopped），dry-run 文件不变；confirm=True → 实体
  status 变 stopped（落盘断言）；无差异 → checked=1 changed=0；system prompt
  常量含 topo_status_sync 约束；resolve_toolset("topo") 含 topo_status_sync。
- **docker ps State 解析复用方式**：import 已有模块级函数
  `topo_discovery._parse_docker_ps`（不提取不改写；topo_discovery.py 零改动），
  探测命令 `docker ps -a` 与发现引擎的 `docker ps` 同一 JSON 格式，解析共用。
- **硬约束核对**：diff 白名单 = tools/topo_tools.py、agent/prompt_builder.py、
  agent/system_prompt.py、tests/tools/test_topo_status_sync.py、
  tests/agent/test_system_prompt.py、OPS-DELTA.md——共 6 文件；无新
  HERMES_*/VIGIL_* env var；**conversation_loop / prompt 缓存 / 压缩逻辑未碰**；
  权限矩阵/审批判定未动（topo_update PROD 审批原样复用）。
- **已知风险**：①无 docker 权限/主机不可达 → 全部 action=ask，不自动写（宁可
  问也不误写）；②手动维护实体（status 手工填/未声明 status）不会被自动覆盖——
  未声明 status 且实际 stopped → ask 由用户决定；③SSH 探测仅支持拓扑表
  credential ssh_key 引用，vault/askpass 类凭据的 host 探测会走 ask 分支
  （交给用户/未来 vssh 凭据链路）；④confirm=True 逐实体写（PROD 逐个审批），
  部分成功部分失败时 write_errors 可见。
- **核销方式**：测试常驻——test_topo_status_sync.py（差异检测/落盘/ask 语义）+
  test_system_prompt.py 常量断言；行为探针命令在批次二十四 prompt 验收段可复跑。

### 47. 批次二十五 反馈闭环——runbook_create 工具 + 专有名词绑定 + 跑通任务主动提议沉淀（§AH 补丁 4 + TencentDB 借鉴） — ✅ 已实施（2026-08-14，批次二十五；commit hash 佐证：任务 1 工具 9e0ca98 / 任务 2 名词绑定 774eaa3 / 任务 3 主动提议 6427596）

- **为什么**：§AH 补丁 4 实测——用户说"沉淀为 runbook 下次修改 ansible 之后进行
  syntax-check"，agent 写了 Markdown 文档（/opt/ansible/runbooks/*.md）而非结构化
  YAML，且 runbook 体系只有 load/checkpoint 没有创建工具，无自动触发。TencentDB
  反馈闭环借鉴：跑通任务 → runbook 草稿自动沉淀。本批 = 反馈闭环第一步。
- **怎么改**（3 文件 + 3 测试文件，均在白名单内）：
  1. **任务 1（runbook_create 工具）** `tools/runbook_tools.py` 新增 `runbook_create`
     agent 工具（注册进 `runbook` toolset，check_fn=check_runbook_requirements 同
     runbook_load/checkpoint）：
     - 参数：runbook（kebab-case 必填）/ title / triggers / summary / steps /
       rollback / env / kind（deploy|incident|checklist，默认 incident）/
       overwrite（默认 False）。
     - **fail-closed 校验**：名称严格 `^[a-z0-9]+(-[a-z0-9]+)*$`（防路径穿越
       ".."/隐藏文件）；steps/commands 非空；commands 疑似明文凭据（赋值式
       password=/token:、flag 式 --password/-p、curl `-u user:pass`）→ 拒绝并
       提示改用 `<vault:path/field>` 占位符（复用 _VAULT_REF_RE 机制），错误消息
       **只报键名不报值**（凭据值绝不回显）；`-p 8080:80` 端口发布不误判。
     - 写盘前复用 `_validate_runbook` 校验 → 保证 runbook_load 原样加载回来
       （kind=deploy 的 checklist/rollback 规则一并生效）；同名已存在未
       overwrite → 报错。写 `<home>/runbooks/<name>.yaml`（复用 _runbooks_dir）。
     - 返回 {status: created/updated, name, path, steps}。
  2. **任务 2（专有名词绑定）** `agent/prompt_builder.py` 静态常量
     `OPS_RUNBOOK_GUIDANCE`（任务 3 扩展前的名词绑定段）："用户说'沉淀/记录/保存为
     runbook' = 调 **runbook_create** 创建结构化 YAML（runbooks/<name>.yaml），
     **不是写 Markdown 文档**；runbook 是程序层机制（triggers+steps+commands+
     rollback，runbook_load 加载、runbook_checkpoint 门控）；意图是行为约束时把
     约束写进 triggers+steps，创建后告知'已创建 runbook，触发词为 …'"。
     `agent/system_prompt.py`：`runbook_create` 加入 `_OPS_SECURITY_TOOLS`，新增
     `_RUNBOOK_TOOLS` 门控——runbook 工具加载时注入 stable tier（字节稳定静态文本）。
  3. **任务 3（主动提议 + 尾部引导）** 同常量追加反馈闭环段："完成可复用运维流程
     （故障修复/部署/排查跑通）后主动提议'这次流程可以沉淀为 runbook，要我创建
     吗？'——用户确认才创建，不自动创建（噪音）也不从不提议（浪费经验）；判断
     标准 = 是否可能再次发生（重启服务/同类故障/同一应用部署）。执行既有 runbook
     有新坑/新命令时提议更新（overwrite=true）"。`tools/runbook_tools.py`
     `_full_payload` 的 note 尾部追加"若本次执行有改进（新坑/新命令），可向用户
     提议更新本 runbook（runbook_create overwrite=true）"。
- **验收测试**：tests/tools/test_runbook_create.py（新，14 用例：创建→runbook_load
  回读 / triggers 模糊匹配 / 明文密码拒绝且值不回显 / vault 占位符放行 /
  `-p 8080:80` 不误判 / overwrite 保护与更新 / 非法名称（含 ".."）拒绝 / 空 steps
  拒绝 / deploy checklist 规则复用 / handler 透传 / registry 注册 + schema /
  load 尾部引导）。tests/agent/test_system_prompt.py 增量 4 用例（名词绑定 + 提议
  语义 + 注入/不注入门控）。回归：tests/tools/test_runbook_tools.py +
  test_runbook_vault_refs.py 全绿（52 passed）。
- **行为探针（实测）**：runbook_create 创建 ansible-syntax-check → runbook_load
  回读 triggers/commands；明文 `curl -u admin:secret123` 拒绝且错误消息不含值、
  `<vault:ansible/pass>` 放行；prompt 常量含 runbook_create + "不是写 Markdown
  文档" + "沉淀为 runbook"；runbook_load 返回尾部含"提议更新…overwrite=true"；
  resolve_toolset("runbook") 含 runbook_create。
- **runbook_create schema 与现有样例一致性**：落盘结构对齐 schema v0.1 样例
  （name/title/version:1/env/kind/triggers/summary/steps/rollback 字段序一致）；
  写盘前复用 `_validate_runbook`（load 同一校验器）保证可回读。env 沿用 runbook
  schema 既有枚举 test/uat/prod（批次 prompt 写 local/test/dev/prod 系与 topo
  枚举混写；硬约束"不改 runbook_load/checkpoint 既有语义"优先，若放开 _VALID_ENVS
  会改 load 校验行为——已按 schema v0.1 收敛，工具描述已注明）。
- **硬约束核对**：diff 白名单 = tools/runbook_tools.py、agent/prompt_builder.py、
  agent/system_prompt.py、tests/tools/test_runbook_create.py、
  tests/agent/test_system_prompt.py、OPS-DELTA.md——共 6 文件；无新
  HERMES_*/VIGIL_* env var；**conversation_loop / prompt 缓存 / 压缩逻辑未碰**；
  runbook_load / runbook_checkpoint 语义未动（create 为纯增量，load 仅尾部追加
  一行引导文本）。
- **已知风险**：①主动提议噪音控制——判断标准（是否可能再次发生）在 prompt 常量
  里约束，靠 LLM 遵循；不自动创建，用户确认才落盘。②overwrite 误覆盖风险——
  同名需显式 overwrite=true，默认拒绝；覆盖前无二次确认，靠工具参数显式化兜底。
  ③明文凭据识别是"疑似"启发式（宁误报提示改占位符），非加密检测；复杂形态
  （base64/拼接）不在本批覆盖。④env 枚举与批次 prompt 文案差异（见一致性说明）。
- **核销方式**：测试常驻——test_runbook_create.py（创建/回读/凭据拒绝/覆盖）+
  test_system_prompt.py 常量断言；行为探针命令在批次二十五 prompt 验收段可复跑。

### 45. 批次二十六 /help 会话内清理最后一轮 + 花哨命令去留 + UI 壳收尾 — ✅ 已实施（2026-08-15，批次二十六）
- **背景**：批二十二已做第一轮 /help 清理，用户 2026-08-13 排期的逐项清单还有
  残留：/reload-skills 文案仍写 ~/.hermes；/debug nous 是死入口（上传即外泄到
  Nous 内部存储）；/usage 描述含 Codex 概念；Skill Commands 段逐条列出 67 个
  hermes 继承的消费级 skill；/pet /hatch /moa 三个死命令（宠物消费功能 + 依赖
  Nous 多模型）仍挂在命令面；UI 壳合入后工作树残留 node_modules / web_dist /
  package-lock.json 噪音。
- **任务 1（/reload-skills 文案）**：`hermes_cli/commands.py` reload-skills 描述
  `~/.hermes/skills/` → `VIGIL_HOME (~/.vigil) skills/`；连带
  `agent/skill_commands.py` scan_skill_commands / reload_skills docstring 同改。
- **任务 2（/debug nous 死入口移除）**：`commands.py` args_hint `[nous|local]` →
  `[local]`；`cli_commands_mixin.py` `_handle_debug_command` nous 恒 False（nous
  词容忍但绝不置 True），输 nous 打印一行中文提示（已移除，按默认 paste 处理或
  用 /debug local），args.nous=False 字段保留（run_debug_share 读取）；
  hermes_cli/debug.py 底层 nous 路径未动（子命令层面不在本批范围）。
- **任务 3（/usage 文案中性化）**：`commands.py` usage 描述去 "banked Codex
  limit reset" → "rate-limit reset"；`cli.py` 两处 docstring + `gateway/
  slash_commands.py` 注释同改；reset 功能与 handler 语义原样保留（用户已拍板）。
- **任务 4（Skill Commands 折叠）**：`cli.py` show_help ⚡ Skill Commands 段
  改为一行 "⚡ Skill Commands (N installed) — 使用 --help-all 或 /skills 查看全部"；
  skill 注册/执行机制完全不动（自定义 skill 仍可 /skill-name 调用）。
- **任务 5（花哨命令去留，用户已拍板）**：从 COMMAND_REGISTRY 删除 /pet
  （petdex）、/hatch + 别名 /generate-pet、/moa；删除对应 CLI dispatch 分支与
  handler（cli.py process_command + cli_commands_mixin.py _handle_pet_command /
  _handle_hatch_command）；`_SLACK_VIA_HERMES_ONLY` 移除 moa 并清理注释；
  /curator、/kanban 保留不动；宠物机制 hermes_cli/pets.py 保留（TUI 在用）；
  ui-tui/ 前端不动；`-m moa:<preset>` 模型路由与 MoA provider 基建保留（非命令面）。
- **任务 6（UI 壳收尾）**：`web_dist` 是构建产物（wheel 不打包——pyproject
  package-data 无 web_dist、setup.py 注明运行时解析，`cd web && npm run build`
  生成）→ .gitignore 新增 `node_modules/` + `hermes_cli/web_dist/`；本地
  npm install 产生的 package-lock.json `peer: true` 噪音回退（web_dist 未提交，
  锁文件不随批提交）。
- **验收测试**：tests/hermes_cli/test_debug.py（nous 词容忍但不置 True + 已移除
  提示）、tests/cli/test_moa_command.py（/moa 从 registry 删除 + unknown-command
  不排程，保留 TestNormalizeMoaModel）、tests/hermes_cli/test_help_text_cleanup.py
  / test_batch14_e1_help_grouping.py（--help-all 继承命令名单去掉 moa）；回归
  test_commands.py / test_busy_policy_invariants.py / test_commands_execute.py /
  test_pet_toggle.py / test_cli_pet_pane.py / gateway/test_moa_one_shot_restore.py。
- **硬约束核对**：只改显示层/命令注册/handler + 测试 + OPS-DELTA.md + .gitignore
  （白名单内）；无新 HERMES_*/VIGIL_* env var；conversation_loop / prompt 缓存 /
  压缩逻辑未碰；工具（tool）语义、skills/ 目录未动；gateway/run.py 的 /moa
  dispatch 分支未动（本批只清 CLI 命令面，gateway 属消息面，单独排期）。
- **核销方式**：测试常驻——debug/moa/help 三组用例 + /help 实测（--help-all
  无 moa/pet/hatch，Skill Commands 折叠行）；季度体检检查命令面是否回添
  消费级命令、nous 上传路径是否重新暴露。

### 46. 批次二十七 runbook env 枚举对齐——test/uat/prod → local/test/dev/prod + 老值档位映射（批二十五遗留关闭）
- **背景**：批十已把 ops env 统一为四值 local/test/dev/prod（ops_permissions
  `_ENV_TIERS` / `_LEGACY_ENV_TIER_MAP` / `_map_env_tier`，老自定义名读取按档位
  映射不报错、只打一次警告）。但 runbook schema 的 `_VALID_ENVS` 仍是 v0.1 三值
  {"test","uat","prod"}——用户按四值写 `env: dev/local` 直接报错；
  `_full_payload` 的 env_mismatch 拿老值 uat 与四值 session_env 比会误判不匹配。
  批二十五 runbook_create 落地时为避免改 load 语义收敛到 v0.1，本批专项对齐。
- **任务 1（load 路径）**：`tools/runbook_tools.py` `_VALID_ENVS` →
  {"local","test","dev","prod"}；新增 `_normalize_runbook_env(env)`——四值直通，
  老值 uat→prod、staging→dev（直接复用 ops_permissions._map_env_tier，档位映射 +
  每名只警告一次），ops 配置显式声明的老自定义名按 isolation/role 档位接受；
  **未声明名（如 sandbox）拒绝**——不沿用 _map_env_tier 的"名字推导默认 dev"
  兜底，避免把拼写错误静默接受成 dev（与验收探针/测试一致）。`_validate_runbook`
  用映射后值校验，**不改写 data["env"]**（load 保留文件原值，约束 1）；错误消息
  改为"可选 local/test/dev/prod；老值 uat/staging 按档位映射"。`_full_payload`
  env_mismatch 比较前 rb env 与 session env 各自映射（uat→prod 与 prod 会话不误报）。
- **任务 2（create 路径）**：`runbook_create` env 校验用同款
  `_normalize_runbook_env`；uat/staging 接受并**落盘映射后的新值**（create 是新
  写入，直接规范到新枚举：uat→prod、staging→dev）；未声明名拒绝，错误消息去掉
  "与 runbook schema v0.1 一致"；docstring 补 env 四值 + 映射说明。
- **验收测试**：test_runbook_tools.py +4（load 老值 uat fixture 不报错且
  data["env"] 保留 "uat"、session=prod 不误报 / session=test 报不匹配；load
  sandbox 拒绝且消息含新枚举；_validate_runbook 直调四值+uat/staging 合法、
  sandbox 抛错）。test_runbook_create.py +6（四值 parametrize 落盘+回读、uat→prod
  / staging→dev 落盘断言、sandbox 拒绝 + 错误消息含新枚举、无 "test/uat/prod"
  字样）。回归 test_runbook_vault_refs.py 全绿；套件 30→40 passed，失败集合与
  基线一致（无新增）。
- **硬约束核对**：diff 白名单 = tools/runbook_tools.py、tests/tools/
  test_runbook_tools.py、tests/tools/test_runbook_create.py、OPS-DELTA.md 共 4
  文件；无新 HERMES_*/VIGIL_* env var；conversation_loop / prompt 缓存未碰；
  runbook_load/checkpoint/create 返回契约未变（load 不落盘改写、create 返回值
  结构不变）；映射语义复用 ops_permissions（import 无循环，tools 同包）。
- **遗留关闭**：批二十五"env 枚举与批次 prompt 文案差异"遗留关闭（OPS-DELTA
  条目 45 记录项 ④）；README/cli.py/topo_discover 的 "test/uat/prod" 历史文案
  不在本批白名单，单独排期。
- **核销方式**：测试常驻——test_runbook_tools.py / test_runbook_create.py 的
  四值+映射用例；季度体检检查 _normalize_runbook_env 与 ops_permissions
  _map_env_tier 是否仍一致（映射表若有更新需同步）。

### 47. 批次二十八 UI 壳第二批后端——执行/审批/审计 API（契约 vigil-exec-api-draft.md 逐项实现）
- **背景**：第一批只读 API（/api/topology /api/runbooks /api/status /api/health）已上线；
  本批按 DSH 前端契约实现第二批：统一 pending 审批注册表 + /api/approvals 三端点、
  /api/exec 三态 + SSE 流、Audit/Sessions JSON 化、Incidents 结构占位。全批端点不进
  PUBLIC_API_PATHS（public_paths.py 未改），错误格式统一 {"error": {code, message,
  details}}，分页统一 limit/offset/total/has_more（默认 50/0）。
- **任务 1（approval pending 注册表，tools/approval.py 加段）**：新增
  register/list/get/wait/approve/deny/reclaim/clear 八原语，线程安全（复用 _lock），
  条目字段照契约（id/command/description/env/grade/session_key/source/status/scope/
  created_at/timeout_at/allow_session/allow_permanent）。**关键语义**：
  - 已超时 approve/deny → {"code": "timeout"}（wait 策略：不自动批准/拒绝，命令保持
    pending）；回收只改状态不裁决。
  - scope 校验：allow_session=False 拒 session、allow_permanent=False 拒 permanent
    （prod 变更确认门条目两者都 False → 只接受 once）；smart_denied 强制 once。
  - 审批侧效应（allowlist 持久化 + trajectory 记录）**由源头审批流原生完成**——注册表
    只存 callback 等待（threading.Event 唤醒 guard 线程），不复制判断逻辑，CLI/gateway
    审批路径零改动零影响。
  - **边界**：进程内注册表，跨进程/跨 gateway 全局化不在本批（进程重启即丢，同 exec
    内存记录）。
- **任务 2（/api/approvals）**：GET 列表（env/status 过滤 + pending 排前 + prod 优先 +
  分页）；POST approve/deny——错误映射 not_found→404 / timeout→409 / invalid_request
  →400，成功 200 {"status": "approved", "scope"} / {"status": "denied"}。
- **任务 3（/api/exec + SSE，[新建]）**：POST /api/exec 校验链 = guard 线程
  check_all_command_guards(command, "local", approval_callback=web 回调)（hardline /
  sudo-stdin / user-deny / ops 矩阵 deny / B' 门全量继承，yolo 也绕不过 prod 未分级
  确认门）→ 三态：executed（本机直跑，输出截断 100KB）/ needs_approval（回
  approval_id + pending:true）/ denied（回 code+reason，终态无 exec_id，照契约）。
  host 缺省本机；env 缺省 ops.permissions.env；远端 host → 400 明确"不在本批范围"。
  执行链复用 terminal_tool.local_exec_stream（sudo 变换继承 _transform_sudo_command、
  IdentitiesOnly/ASKPASS 语义由批十六/十九修复全量继承，不重新发明）。GET
  /api/exec/{id} 返回执行记录全量（command 用已脱敏展示值，command_raw 绝不出 API）。
  GET /api/exec/{id}/stream SSE 事件 exec:start/output/exit/error：approved → 实时
  执行推送；executed → 重放已捕获（已脱敏）输出；denied/error → exec:error；
  needs_approval → 轮询等审批（超时/回收 → exec:error，命令保持 pending）。
  **执行记录为进程内内存态 + trajectory terminal 事件**（record_event 强制 redact），
  进程重启丢内存记录可接受；软上限 500 条（淘汰最早已终态记录，pending 不淘汰）。
- **任务 4（Audit / Sessions JSON 化，复用 trajectory.py）**：GET /api/audit/sessions
  （session_id/event_count/started_at/ended_at）、GET /api/audit/events（type/session
  过滤 + 分页，action/result 只给前 500 字符 preview）、GET
  /api/audit/events/{session_id}/{seq}（单事件全量，过 redact）、DELETE
  /api/audit/events（session_id/older_than prune）；GET /api/sessions/{id}/events
  （会话事件流）。复用 _iter_event_files/_load_events/_event_file_path/_parse_iso，
  不重写轨迹逻辑。
- **任务 5（Incidents 占位）**：GET /api/incidents 返回空列表 + schema_version:1
  （不编数据；未来数据源候选见契约，独立排期）。
- **契约歧义解释（OPS-DELTA 登记，未改契约）**：
  1. **§5 /api/sessions**：既有 GET /api/sessions（web_routers/sessions.py）返回
     id/last_active 行结构，桌面 SessionInfo / dashboard selftest / 负数 limit 422
     语义都依赖它，不能动；DSH 前端按契约消费 session_id/last_activity_at/running。
     解释：web_server.py 先注册同路径包装路由——委托原 handler（旧字段与 422 校验
     零变化），行级追加契约字段（session_id=id、last_activity_at=last_active 别名，
     running=活跃注册表或 ended_at is None），两边消费方同时满足。
  2. **Audit 事件结构**：契约示例 {command, result:{exit_code, output_preview}} 是
     示意；实际 trajectory 事件为 {ts/session_id/seq/type/tool/action/result/...}
     原样返回（契约明言"trajectory 事件原样"），preview = action/result 前 500 字符。
  3. **denied 响应是终态**：{"status":"denied","code":"denied","reason"} 不带
     exec_id（契约 §3 原文如此）；执行记录仍在内存中（GET /api/exec/{id} 与 SSE
     exec:error 可查）。
  4. **smart 审批模式**：DEFAULT_CONFIG 的 approvals.mode 缺省 smart，guard 会先跑
     aux LLM 评估再触发审批回调；POST /api/exec 等 guard 决策/审批登记最长 15s
     （_EXEC_GUARD_DECISION_WAIT），等待期间 guard 完成（smart 直接放行/拒绝）则按
     真实决策返回，不误报 internal。
- **安全（最高优先）**：新增端点一律不进 PUBLIC_API_PATHS；执行面命令/输出双层脱敏
  ——redact_sensitive_text(credential_values=True, force=True)（token/ENV 赋值/
  password 值）+ topo_export._redact_value（URL userinfo / .pem/id_rsa 私钥路径 /
  home 前缀），单层各有盲区（前者不遮 URL userinfo，后者不遮 token 值），必须两层
  都过；审批条目 command 在 prompt 面已先打码再展示。已知**既有**redact 盲区（非本
  批引入，未修）：`-p <值>` 这类行内 flag 值两层都不遮，CLI/gateway/轨迹同受限于
  此，单独排期。
- **验收测试**：tests/tools/test_web_approval_registry.py（10 用例：注册表原语/
  超时 fail-closed/scope/smart_denied 强制 once/列表排序分页）+ tests/hermes_cli/
  test_batch28_exec_api.py（16 用例：approvals 三态含 409 timeout 与 scope 校验、
  exec 三态含 B' yolo 绕过验证与凭据打码、SSE 事件序列、audit 过滤分页 prune、
  sessions 契约字段、incidents 占位、凭据 grep 0 命中）。回归 test_web_server.py /
  test_web_server_git.py / test_web_server_session_search.py / test_web_server_
  gateway_topology.py / test_web_server_host_header.py / test_approval*.py /
  test_ops_*.py / test_terminal_tool.py——失败集合与基线一致（环境噪音：test_debug
  15 + test_commands 1 + web_server 主题/插件 manifest 7 + approval_mode_parity/
  ops_permissions_guard 8，均 stash 对比在干净 main 上同样失败，无新增）。
- **硬约束核对**：diff 白名单 = hermes_cli/web_server.py、tools/approval.py（仅新增
  pending 注册表段）、tools/terminal_tool.py（仅 local_exec_stream 封装）、新增测试
  2 个、OPS-DELTA.md 共 5 类文件；public_paths.py 未改；无新 HERMES_*/VIGIL_* env
  var；conversation_loop / prompt 缓存 / 压缩逻辑未碰；terminal_tool/approval.py/
  ops_permissions 既有判断逻辑零改动（approval.py 只加段）。
- **遗留**：审批注册表与 exec 记录跨进程/跨 gateway 全局化、远端 host 执行、
  Incidents 真实数据源、`-p` 行内凭据 redact 盲区，均单独排期。
- **核销方式**：测试常驻——两个新套件全绿 + 凭据 grep 0 命中；季度体检检查注册表
  语义是否与既有审批流漂移（allowlist/trajectory 必须仍由源头完成）、SSE 线程安全
  （call_soon_threadsafe）是否被"优化"回裸 put_nowait。

### 48. 批次二十九 setup 工具面净化 + Nous 残留移除 + cua-driver 禁用（批十二 D1 落地） — ✅ 已实施（2026-08-15，批次二十九）

- **为什么**：批二十六清了 /help 命令面，工具面/配置面仍是 hermes 全量——工具选择
  清单混排消费级工具且 **BFL FLUX 3 Video / TTS 默认勾选**（白占工具加载 + token）；
  Search provider **默认选中 Nous Subscription（Firecrawl）**（Vigil 用户无 Nous
  订阅 = 死入口，中间用户不换配置搜索就废）；TTS/Browser provider 列表也有 Nous
  Subscription 死入口；dashboard 启动被指自动下载 cua-driver（实测现状已无该触发
  路径，见任务 3 核实）。本批 = 工具面净化（批十二 D1）+ Nous 残留清理 + cua 确认。
- **任务 1（工具选择清单净化）**：
  1. 预选运维工具集：`_OPS_CORE_TOOLSETS`（web/terminal/file/code_execution/vision/
     skills/todo/memory/session_search/clarify/delegation/cronjob/prom）默认勾选——
     `tools_command` first_install 的 checklist 预选改为
     `(current_enabled - _DEFAULT_OFF_TOOLSETS) | set(_OPS_CORE_TOOLSETS)`，prom
     由此进入首次安装默认勾选（此前不在 hermes-cli composite，从未默认开）。
  2. 取消默认勾选：`bfl`、`tts` 加入 `_DEFAULT_OFF_TOOLSETS`（运行时 schema 也不
     加载，省 token；`vigil tools` 手动启用路径不变）。
  3. 消费级工具折叠：`_prompt_toolset_checklist` 改为两段式——运维区在前，`── 高级 /
     娱乐工具（默认不启用，需要的在此勾选）──` 分隔行后是 `_ADVANCED_TOOLSETS`
     （video/image_gen/video_gen/bfl/x_search/tts/context_engine/homeassistant/
     spotify/discord/discord_admin/yuanbao/computer_use/browser）。分隔行作为不可选
     行处理（结果映射跳过 None 索引，勾选分隔行 = 空操作）；插件 toolset 仍在尾部。
  4. 清单标题改为 "选择 Vigil 可用的工具 — {platform}"（运维语境）。
  - **边界**：`_CONFIG_ONLY_TOOLSETS`（stt）仍在 "Reconfigure" 流程配 provider，不进
    清单；`context_engine` 折叠但**不加入** `_DEFAULT_OFF_TOOLSETS`（其启停由
    `context.engine` 驱动，非清单勾选）。
- **任务 2（Nous 残留清理，功能代码保留）**：
  - 删除 `TOOL_CATEGORIES` 全部 managed "Nous Subscription" 行：tts（342 段）、stt
    （435 段）、web（508 段）、image_gen（543 段）、video_gen（563 段）、browser
    （640 段）——只删 UI 行，`nous_subscription.py` / `nous_account.py` 模块与
    gateway 运行时代码零改动。
  - **Search provider 默认值**：web 类别剩 "Firecrawl Self-Hosted" 硬编码行；运行时
    plugin 行注入后把免费无 key 的 `ddgs` 移到 index 0（`_visible_providers`），
    未配置用户的默认高亮 = ddgs（免费可用），绝不默认订阅项。`tools/web_tools.py`
    `_get_backend()` 最终兜底 `"firecrawl"` → `"ddgs"`（无任何凭据的新装用户不再落到
    无 key 的 firecrawl 死后端；`hermes tools` 选 ddgs 时 post_setup 自动装 ddgs 包）。
  - TTS 列表默认高亮仍为 Edge（免费），Browser 默认高亮仍为 Local Browser。
  - **边界（插件侧 Nous 行保留）**：image_gen/video_gen 的 plugin 行（如 "Nous
    Portal (image)"、Krea tag 里的 managed Nous Subscription 字样）**不在本批白名单**
    （plugins/ 不可改）且仅经折叠的 advanced 区 + 显式 opt-in 可达；对 Nous 用户是
    真实能力，保留。默认 setup 全链路（quick setup 无工具清单 + first_install 只配
    web 免费项）已无 "Nous Subscription / Nous Portal / billed to your
    subscription" 字样。
  - `_RECENTLY_SHIPPED_TOOLSETS` 清空：bfl 在 v0.1.0 基线前已发布，早过 one-release
    back-fill 窗口，且本批改默认关——保留会把默认关又拉回开（账本注释已更新）。
  - `yuanbao` 加入 `_DEFAULT_OFF_TOOLSETS`（与 spotify 同类的消费级 IM 集成，默认关）。
- **任务 3（cua-driver 禁用核实，无需改码）**：
  - 核实结论：**默认路径已不含自动下载**——`install_cua_driver` 调用点仅 3 处：
    `tools_config.py:1795`（post_setup="cua_driver"，仅用户显式启用 computer_use
    工具集时）、`update_cmd.py`（`vigil update`，且前置 `shutil.which("cua-driver")`
    ——未装不触发，默认 no-op）、`main.py`（`vigil computer-use install` 显式命令）。
  - dashboard 启动无下载触发：web_server 只有只读 `computer_use_status()` 端点
    （读 `cua-driver doctor` 不下载）；`computer_use` 在 `_DEFAULT_OFF_TOOLSETS` 且
    check_fn 门控，默认会话零 schema。`_maybe_nudge_update`（cua_backend.py）只在
    computer_use 后端 `start()` 时做 ~20h 缓存的 GitHub 版本检查（仅日志提示，非下载）。
  - 未注册任何 autostart/systemd 行为（本批零改动）。
- **测试**：tests/hermes_cli/test_batch29_tools_surface.py（23 用例：默认工具集断言
  含 prom 预选/无 bfl/tts/消费级、清单两段式与分隔行不可选、provider 面无 Nous 死
  入口 + web 默认 ddgs + tts/browser 免费默认、setup 可见文案无订阅措辞、web 运行时
  兜底 ddgs、first_install 不触发 install_cua_driver）+ test_tools_config.py 既有
  快照复用用例改挂 tts 类别（原 image_gen managed 行已删）。回归 test_tools_config.py
  / test_setup_branding.py / test_setup_tools_direct.py / test_post_setup_gating.py /
  test_install_cua_driver.py 全绿。
- **硬约束核对**：diff 白名单 = hermes_cli/tools_config.py、tools/web_tools.py、
  tests/hermes_cli/test_batch29_tools_surface.py + test_tools_config.py、OPS-DELTA.md
  共 5 类文件（setup.py 无改动需求——工具可用性摘要早已运维化）；功能代码零删除
  （nous_subscription/nous_account 保留，消费级工具代码保留）；无新 HERMES_*/VIGIL_*
  env var；conversation_loop / prompt 缓存 / 压缩逻辑未碰；凭据值不进 argv/日志
  （ddgs 免费无 key，无新增凭据面）。
- **遗留**：插件侧 Nous gateway 行（image_gen/video_gen/browser 的 managed 行）保留
  为 Nous 用户真实能力——若 Vigil 明确"永不支持 Nous"，另排期在 `_visible_providers`
  按名过滤；`updates.refresh_cua_driver` 默认 True 属 config_defaults.py（不在白名单）
  未动——实际已被 `shutil.which("cua-driver")` 门挡住，无自动下载。
- **核销方式**：测试常驻——批次 29 套件全绿；季度体检检查：清单两段式是否被改回
  单层混排、web 默认是否被改回订阅项、`_DEFAULT_OFF_TOOLSETS` 是否又漏掉消费级
  工具、首次安装是否重新出现 cua-driver 下载。

### 49. 批次三十二 行为层安全——凭据值全局登记打码 + 提权受控通道强制（Codex 产出，2026-08-16）

- **背景（dogfood 实测，用户跑"停 GitLab"任务）**：①agent 引导用户把 sudo 密码
  写裸明文文件（`~/credential/sudo_credential`）、手写 echo 密码 askpass 脚本
  （`~/.vigil/tmp-askpass-sudo`，绕过 credential_vault 受控通道），且 review diff
  展示明文；②clarify 密码输入明文落 state.db（role=tool 消息，如 id 503）。低熵
  裸密码（`wwplove815`：无键名形态、不足 16 字符不挂高熵门）漏过 redact 两套既有
  检测。本批治本：**凭据值全局登记打码（不依赖熵检测）+ 提权受控通道绑死**。
- **任务 1（凭据值全局登记打码）**：
  1. **登记表（agent/redact.py）**：`register_credential_value()` 进程内 set +
     持久化 `<VIGIL_HOME>/credential_values.json`（600 权限，原子写）；redact 时
     惰性加载合并。`redact_sensitive_text` 尾部新增"值精确匹配登记表 → 打码"
     路径（词边界保护，防腐蚀更长 token）——任何输出通道统一生效（终端/review
     diff/SSE/trajectory/state.db 落库副本/JSON 会话快照）。登记资格过滤：长度
     ≥6、非纯数字、非单字符重复（防把常见词全局打码）；值本身不打日志（只记长度）。
  2. **登记来源 ①**：`credential_vault.store()` 写入的值自动登记。
  3. **登记来源 ②**：clarify 问题文本含敏感关键词（密码/password/密钥/secret/
     凭据/sudo…）时答复值登记（≥8 字符且含数字/符号/大写，防 "bearer"/"yes"
     等常见答复词误登记）；返回给 agent 的 JSON 保持明文（agent 仍需该值去
     store vault），脱敏发生在落库层。
  4. **落库脱敏**：`run_agent._flush_messages_to_session_db` 对 clarify 工具结果
     内容（含 question+user_response JSON 形态）写 SQLite 前过 redact（force）——
     登记值精确打码；live 内存消息保持明文；非敏感答复（redact 恒等）不受影响。
     JSON 会话快照路径（_save_session_log）本就过 redact，登记后自动打码。
  5. **review diff 展示层兜底（agent/display.py）**：`extract_edit_diff` 返回 +
     `_emit_inline_diff` 输出前再过一次 redact（双保险，防登记表外的新形态）。
  6. **已落库明文清理命令**：`vigil security scrub`（新子命令，hermes_cli/
     scrub_secrets.py）——扫描 state.db messages 各文本列（content/api_content/
     reasoning/reasoning_content/tool_calls/reasoning_details/codex_*）打码登记值，
     重建 FTS；支持 `--dry-run`、`--values <值>`（追加）、`--session <id>`（过滤）。
  7. **登记表文件自护**：`credential_values.json` 自身内容过 file_read redact 时
     值被精确打码（read_file 读不出明文）。
- **任务 2（提权受控通道强制，vssh 同款，零新机制）**：
  1. **prompt 层**：新常量 `OPS_CREDENTIAL_GUIDANCE`（agent/prompt_builder.py，
     与 OPS_CREDENTIAL_SSH_GUIDANCE 并列）——提权/sudo 走 sudo_exec/vssh（拓扑表
     credential 引用 + vault，ASKPASS 内部注入）；禁引导用户写裸密码文件、禁手写
     echo 密码 askpass、禁让用户把密码贴普通对话（要收密码走 clarify，答复自动
     登记+打码+落库脱敏）。`agent/system_prompt.py` 在 `_OPS_SECURITY_TOOLS` 同门控
     下并列注入（静态文本，字节稳定，缓存前缀安全）。
  2. **工具层校验**：
     - write_file（tools/file_tools.py）：内容含 `echo '<8-32 字符>'`（或
       printf 形态）且目标路径在 `~/.vigil` 或 `~/credential/` 下 → 拦截并引导
       "用 credential_vault / sudo_exec 受控通道"（redact 之前检测原始内容）。
     - terminal（tools/approval.py HARDLINE_PATTERNS，批十九 sudoers.d 同款
       硬拒风格）：`echo/printf '<8-32 值>' (>|>>) ~/.vigil 或 ~/credential/`
       → hardline 硬拒（yolo 也绕不过）；`$HOME`/绝对路径形态同覆盖。
     - sudo_exec（tools/sudo_tool.py）：拓扑表无该 host credential 且用户要
       sudo 时，工具内部走 clarify 收密码 → `credential_vault.store()`（0600，
       name=`sudo-<host>`）→ 执行 → 返回拓扑登记提示（credential 声明
       type: vault, ref）。回调经 ContextVar 注册（CLI `_install_tool_callbacks`
       一行），`propagate_context_to_thread` 的 `copy_context` 自动传播到工具
       worker 线程；gateway/web/oneshot 无交互通道时保持原 fail-closed 引导。
       认证失败时提示已存凭据名（可更新或 expire）。
- **测试**：tests/agent/test_redact_registry.py（13：登记资格/全形态打码/file_read
  sentinel/持久化 600+重载/边界保护/登记表自护）+ test_display.py 补 2 +
  test_system_prompt.py 补 3；tests/tools/test_askpass_interception.py（17：write_file
  4 形态拦截 + terminal hardline 5 形态 + 良性 5）；test_sudo_clarify_flow.py（4：
  clarify→store→执行/认证失败提示/fail-closed/名清洗）；test_clarify_registration.py
  （7：敏感登记/多选/非敏感不误伤）；test_credential_vault.py 补 1；tests/run_agent/
  test_flush_clarify_redaction.py（3：落库脱敏/非敏感不受影响/非 clarify 不动）；
  tests/hermes_cli/test_scrub_secrets.py（6：dry-run/清理/非敏感保留/--values/
  --session/空表）。回归：tests/agent/test_redact*.py、tests/tools/test_sudo_exec.py、
  tests/tools/test_hardline_blocklist.py、tests/agent/test_turn_context.py、
  tests/hermes_cli/test_batch31_chat_api.py、tests/hermes_cli/test_batch28_exec_api.py
  全绿。
- **边界**：登记表持久化在 `<VIGIL_HOME>/credential_values.json`（profile-aware，
  进程内热集合 + 磁盘持久化，重启不丢）；已落库明文清理 = `vigil security scrub`
  （对历史 state.db 一次性清理，新写入自动脱敏）；gateway/web 对话页的 sudo_exec
  无 clarify 交互通道（保持原引导文案），CLI 全量支持；terminal 管道形态
  （`echo x | tee ~/.vigil/...`）不在本批硬拒范围（write_file 内容检测已覆盖同型）。
- **核销方式**：测试常驻——批次 32 套件全绿；季度体检检查：登记表是否仍被
  store/clarify 写入并持久化 600、askpass 硬拒是否被削弱、sudo_exec 无凭据引导
  是否仍指向 vault 受控通道。

### 50. 批次三十三 小修合集——对话页会话状态管理 + /topo 三件套 + E4 命中率价格 + 拓扑状态组装（Codex 产出，2026-08-16）

- **背景（2026-08-16 dogfood 实测攒的小修，全部已有排期）**：①对话页切页再切回消息不显示（ChatPage 卸载丢本地 state，chat_api 无历史拉取端点）；②agent 工作时 session 下拉被禁（busy 禁输入连带禁下拉，后端 busy 实为 per-session 注册表，切换本应安全）；③CLI 新式 TUI 会话敲 /topo 三次"已取消"（`_prompt_text_input` 的 slash-worker 守卫——TUI app 在跑且非主线程时直接 return None）；④用户习惯 `ip1,ip2` 逗号分隔被当 hostname 报 "hostname contains invalid characters"；⑤/topo 无参无用法提示（可发现性）；⑥退出汇总缺 E4 承诺的缓存命中率 + 价格（只有命中数）；⑦拓扑页集群/主机/服务全无状态显示（status 在第三层 entities/*.yaml，build_view 只读第一/二层 → card.status 恒空 → StatusPill 不渲染）。
- **任务 1（对话页会话状态管理，后端 1 端点 + 前端分槽）**：
  1. **后端历史端点（hermes_cli/chat_api.py）**：新增 `GET /api/chat/sessions/{id}/messages`——复用 `_load_conversation_history`（新增 `repair_alternation`/`include_row_ids` 参数：展示端点传 verbatim 转录 + 稳定行 id 作 React key），返回 `{chat_session_id, messages[{id, role, content, tools[], timestamp}], total, busy}`；内容过 `_redact_text`/`_preview`；assistant tool_calls 与紧随 tool 行折叠进同一条气泡的 `tools` 数组（对齐 SSE 流式渲染的 ChatToolEvent）；孤立 tool 行兜底、pending tool 无输出补空串；会话不在注册表 → 404；`busy` 随注册表返回。
  2. **前端状态分槽（web/src/lib/chat.ts + api.ts + ChatPage.tsx）**：`stateFromHistory(history, busy)` 纯函数（历史 → ChatTurnState，本地 id 重编号防 nextId 冲突）；`ChatPage` 改 `states: Record<sessionId, ChatTurnState>` + 每会话 AbortController + `loadedRef` 防重复拉取；挂载/切会话拉历史；busy 时 **select 不禁用**（仅禁当前会话输入框）；切走 abort SSE（后台 turn 继续跑），切回轮询 `listChatSessions` 发现 busy 翻转 → 重拉历史恢复；`loadSessionHistory` 槽位保护（已有消息不覆盖流式现场）。
- **任务 2（/topo 三件套，cli.py）**：
  1. **2a TUI 线程安全输入**：`_prompt_text_input` 重写——无 app → 直接 `input()`；主线程+app → 原 `run_in_terminal` 路径；**非主线程+app → 照 `_prompt_text_input_modal` 的 `call_soon_threadsafe` + response_queue modal 机制**（`_text_input_state`/`_text_input_deadline`），用户在正常输入区输入、Enter 提交（空→None）、ESC/Ctrl+C 取消、超时（默认 120s）取消；无可调度 app loop → 干净取消 None（原 #23185 纪律）。接线：`handle_enter` text_input 分支、`_modal_prompt_active`、ESC/Ctrl+C 取消、pet awaiting_input、外部编辑器 guard/filter、stash filter、prompt 图标（⌨）、`_get_text_input_display` 面板 + `text_input_widget` 加入 `_build_tui_layout_children`。通用修复，其他 slash 交互命令受益。
  2. **2b 逗号分隔**：host 参数 token 按逗号拆分（逐段 strip），交互输入同样拆分。
  3. **2c 无参用法**：无参/交互模式先打印 `用法: /topo <host...> [--env E] [--user U] [--key K] [--cluster C] [--force] [--yes]`。
- **任务 3（E4 命中率 + 价格，cli.py + hermes_cli/main.py）**：退出汇总 token 行补 `缓存命中率 X%`（cache_read/(cache_read+inp)）+ 估算成本；新 `_estimate_exit_cost_label`——优先 `agent.session_estimated_cost_usd` 会话内累计值（conversation_loop 路径最准，含 MoA/codex 特殊路径），缺失/未知时降级 `estimate_usage_cost` 现估，deepseek 等路由官方快照缺 cache-write 单价时降级 cache_write=0 近似估算（真实会话也能出价）；订阅含价显示"成本已含（订阅）"；成本估算异常不影响汇总。状态栏 token 行 `_format_session_token_line` 补 `cache read A/B (P%)`（B=0 省略百分比，batch14 既有断言兼容）。
- **任务 4（拓扑状态组装，hermes_cli/subcommands/topo_export.py）**：新 `_derive_status(statuses)`——全健康（running/up/healthy/ok/active/online）→ `running`；任一非健康（stopped/exited/degraded/…）→ `degraded`；全空 → 空串（不显示，避免误显示）；服务卡从 `_load_detail`（第三层 entities，已 sanitize）merge `detail["status"]`；主机卡由服务状态推导；集群卡由主机推导（`view["clusters"]` 补 status）；HTML 导出集群头加状态 pill。前端仅 TopologyPage 集群 header 加 `<StatusPill status={group.meta?.status} />` + api.ts `TopologyCluster.status?` 类型（StatusPill 组件本就正确，缺的是数据；数据组装在 topo_export.py，未碰渲染组件）。
- **测试**：tests/hermes_cli/test_batch33_chat_history.py（7：404/结构化+tool 折叠/redact/busy/空会话/pending 无输出/孤立 tool 行）+ test_batch33_topo_status.py（7：服务 merge/主机推导/集群推导/全健康/全空/derive 规则/HTML pill）+ test_batch33_e4_hitrate_cost.py（10：命中率/零分母省略/未知模型省略/异常不崩/token 行百分比/deepseek 缺价降级/累计优先/订阅含价）+ test_batch33_topo_slash.py（4：逗号双主机/带空格+选项/无参用法/交互逗号）+ tests/cli/test_prompt_text_input_thread_safety.py（7，旧"后台线程直接取消"契约改为"走 modal 队列"）+ test_exit_summary_tokens.py（5，同步旧断言）。回归：批三十一四文件组合 35 全绿（test_tool_callback + test_batch31_chat_api + test_batch28_exec_api + test_turn_context——历史端点改动 chat_api 后必跑）+ 批三十三新增与相关套件 113 全绿 + 批三十二安全回归（redact/scrub/askpass/clarify/credential_vault/sudo_clarify）74 全绿 + web `npm run build` ✓ + vitest 28 全绿。
- **实测（真实 ~/.vigil，deepseek-v4-flash）**：playwright——拓扑页 pills running/stopped/exited/degraded/restarting 全显示、集群头 4 组全带 pill、页面零 error；对话页 1a 发消息→切拓扑页→切回 /chat 用户气泡+assistant 回复完整恢复；1b A busy（思考中 true）时 select 可切到新建会话 B 且能发消息、切回 A 显示"处理中"+部分流内容、跑完轮询自动恢复最终历史。PTY 驱动真实 TUI（from cli import main）——/topo 弹出 `⌨ 输入` 面板（"在上方输入区输入后回车提交…"）+ 用法行；`/topo host_a,host_b --env dev --yes` 两行独立发现结果、无 "invalid characters"；无参敲 /topo 先打用法行；退出汇总实测 `缓存命中率 4.6% · 估算成本 ≈$0.01`。
- **边界**：历史端点走注册表进程内会话——重启后会话不在注册表 → 404（会话列表本就只列活会话，与现状一致）；deepseek 官方快照缺 cache-write 单价 → 降级忽略 cache-write 的近似估算（与 E4 硬编码价格表口径一致）；历史工具摘要为 `_preview` 单行截断预览（完整内容在会话日志/state.db）；TUI modal 超时 120s（沿用 `_prompt_text_input` 原默认）；web_dist 已随 `npm run build` 更新（含新 ChatPage chunk）。
- **核销方式**：测试常驻——批次 33 套件全绿；季度体检检查：会话状态分槽是否被改回单 state（切页丢消息复发）、`_prompt_text_input` 非主线程路径是否又退化回 return None（/topo 挂死复发）、退出汇总是否又丢命中率/价格、topo 状态组装是否被后续拓扑图批次正确接管（第三层 status 继续透出）。

### 53. 批次三十六 对话页"停止"按钮（后端真中断）+ dashboard install/uninstall/status（Codex 产出，2026-08-16）

- **背景（用户实测痛点 2026-08-16）**：chat 对话页 agent 工作中（busy）无退出/停止按钮——任务跑偏或太久只能干等，切会话/刷新只 abort 前端 SSE 显示，后端 worker 继续跑完浪费 token。本批：前端显式"停止"按钮 + 后端真中断端点 + 常驻 dashboard 产品化（`vigil dashboard install/uninstall/status`，systemd user 会话，中间市场用户不再手写 unit）。基线 c22fd98（0.1.18 发布后）。
- **任务 1（后端 interrupt 端点，核心）**：`POST /api/chat/sessions/{id}/interrupt`（hermes_cli/chat_api.py）——会话不存在 404；不 busy → 409 not_busy（幂等：重复中断/空闲都是明确状态）；busy → ①取消本会话所有 pending web 审批（`deny_web_approval(..., reason="interrupted")` 唤醒等待中的审批回调，工具按拒绝收尾，审批中心/铃铛不再 pending——只清挂起审批，不动审批核心逻辑）②`agent.interrupt(hard_cancel=True)`（复用 gateway /stop / CLI Ctrl+C 同套 AIAgent 中断机制：`_interrupt_requested` + 活跃请求 abort + 工具线程 fan-out，显式停止非 redirect）③等 worker 收尾（`_turn_done`，上限 5s）返回 200。SSE 事件类型零新增：worker 侧 run_conversation 返回 interrupted → 现有 chat:done 带 `interrupted: true` 收尾。busy 由 worker finally 单一写入方复位（端点等待 `_turn_done` 保证返回时已可继续发新消息）。
- **任务 2（前端"停止"按钮）**：ChatPage busy 时输入框旁显示红色警示"停止"按钮（抽成 web/src/components/StopButton.tsx，与"发送"并列）；点击 → ①POST /interrupt ②abort 当前 SSE（现有 AbortController）③`markTurnInterrupted`（web/src/lib/chat.ts 纯函数：busy 解除 + 消息区"已停止"状态行，本地瞬态标记不持久化）→ 可立即发新消息；中断请求失败（网络/409）提示但不卡死（仍 abort SSE + 本地标记，后台由注册表轮询兜底复位）。api.ts 新增 `interruptChatSession`。
- **任务 3（`vigil dashboard install/uninstall/status`）**：新 hermes_cli/dashboard_service.py（照 dashboard_register 的模块+delegate 模式：main.py 加 cmd_dashboard_install/uninstall/status 薄委托，dashboard.py 加 install/uninstall/status 子命令）——unit 名 = 用户手写版同名 `vigil-dashboard.service`（install 覆盖行为明确：先停旧服务再写，绝不悄悄并存两个 dashboard 抢端口）。install [--port N]：探测 systemd user 会话（`systemctl --user show-environment`；不可用 → 明确报错"当前环境无 systemd user 会话，无法常驻"，不装）；`sys.executable` 优先回退 `shutil.which("vigil")` 解析解释器写 ExecStart（`-m hermes_cli.main dashboard --no-open --skip-build --port N`）；`Environment=HOME=<当前用户 home>` 必写（防数据根错位）；daemon-reload → enable → start → 打印 http://127.0.0.1:<port>。uninstall：stop + disable + 删 unit 文件 + daemon-reload（幂等：不存在也成功）。status：active/exited + enabled + URL/端口摘要（未安装 → 明确提示）。重复 install 覆盖旧 unit、`--port` 变更即生效。
- **测试**：后端 tests/hermes_cli/test_batch36_chat_interrupt.py（6：404/不 busy 409/中断成功 busy 复位可继续发+再次 interrupt 幂等 409/chat:done 带 interrupted 标记/挂起审批被取消 denied 且不再 pending/只取消本会话审批不动他会话）+ test_batch36_dashboard_service.py（11：unit 内容参数化/python 路径解析（sys.executable 优先、which 回退）/install 流程调用序列+URL/覆盖安装先停旧/无 systemd 明确报错不装/systemctl 缺失报错/uninstall 幂等（含 unit 不存在）/status 未安装+摘要含端口）。回归：批三十一四文件组合 35 全绿（test_tool_callback + test_batch31_chat_api + test_batch28_exec_api + test_turn_context）+ test_batch33_chat_history 7 + 前端 vitest 61 全绿（新增 chat markTurnInterrupted 3 + StopButton SSR 3 + api interrupt 3）+ `npm run build` ✓ + `tsc -b` ✓。注：`test_batch33_chat_history` 与 `test_web_server.py` 同跑时 5 例污染失败为基线 c22fd98 预存问题（stash 验证与本批无关，未动 web_server）。
- **实测（真实 ~/.vigil，真实 deepseek 回合；approvals.mode smart→manual 临时切，测完还原 diff 校验字节一致；dashboard 8805 + playwright chromium）**：c 非 busy 无停止按钮；a 发"分析下拓扑并给出建议"→ 停止按钮出现 → 点击 → 消息区"已停止" → 输入立即可用；b 停止后等 10s 消息区零新增（真中断验证，agent.log 显示 `interrupted_by_user` / `interrupted_during_api_call` + `stream_interrupt_abort` tcp_force_closed——不再浪费 token）；a2 新消息立即可发并再次停止（2 条"已停止"行）；d manual 模式发 `rm -rf /tmp/vigil-b36-demo` → 审批卡出现 → 点停止 → 审批被取消（审批中心 pending 过滤下该命令消失，全部过滤下显示 denied，agent.log `BLOCKED: User denied this command`）；console 零 error。截图 /tmp/b36-shots/。dashboard install 实测：`install --port 9121` → unit 生成（对照模板：真实 python 路径 `/home/wpwang/projects/vigil-agent-release/.venv/bin/python` + `Environment=HOME=/home/wpwang` + `--port 9121`）→ daemon-reload/enable/start → `systemctl --user is-active`=active + curl health 200；重复 install 幂等；`--port 9122` 变更生效（9122 health 200）；uninstall → 服务停 + unit 删 + 二次 uninstall 幂等 + status 显示未安装；测完还原用户手写版 unit（字节一致 diff 校验）+ 9119 服务恢复 active。
- **边界**：interrupt 端点等 worker 收尾上限 5s（极端卡死场景超时返回 200，busy 由 worker 最终复位；前端本地已标记停止，若后端仍忙新消息会 409 提示）；审批取消只在 chat interrupt 路径（不动审批核心，审批记录保留 denied 供审计，不删除）；"已停止"状态行是本地瞬态标记（不持久化，切会话/重拉历史消失，agent 实际产出以历史为准）；systemd user 会话缺失时 install 明确失败不安装（uninstall 仍做文件级清理）；unit 与手写版同名——install 覆盖行为明确（先停再写），用户自定义 unit 内容会被接管（本批已还原现场）；ESLint 因仓库缺 `@eslint/js` 依赖预存故障无法运行（与本次改动无关）。
- **核销方式**：测试常驻——批次 36 套件全绿（6 后端 + 11 dashboard + 61 前端 + 35 回归）；季度体检检查：interrupt 是否仍走 AIAgent.interrupt 硬中断（未退化为纯前端 abort）、审批取消是否仍在 chat interrupt 路径清理 pending（未扩散到审批核心）、停止按钮是否仍接 /interrupt 端点（未退回纯 SSE abort）、dashboard unit 是否仍写 Environment=HOME 且 --port 生效、install 是否仍先停旧 unit 再写。

### 52. 批次三十五 拓扑图 react-flow + 活性 last_seen + 详情抽屉（Codex 产出，2026-08-16）

- **背景（用户拍板 2026-08-16）**：拓扑页三处升级——①静态 SVG 缩略图（纯装饰、信息量趋零）重做为可交互拓扑图（react-flow：缩放/平移/节点 label/状态着色/点击联动）；②在线/离线 = 活性（liveness），第一层实现 lazy last_seen（agent 交互即活性，零主动探测成本）；③详情展示从"卡片内展开+横向滚动条"升级为右侧详情抽屉。基线 b1a5e5f（批三十四后）。
- **任务 1（活性打点 lazy last_seen，后端，零探测成本）**：
  1. **运行时状态存储（新 hermes_cli/runtime_state.py）**：`<VIGIL_HOME>/runtime_state.json`（0600 原子写 tmp+os.replace，`{host: {last_seen: epoch}}`），`mark_host_activity`（空 host 不打）/`get_host_activity`/`load_activity`；进程内锁线程安全。last_seen 只进运行时文件，拓扑 yaml/hosts 结构零改动（静态事实层与运行时瞬态分离）。
  2. **三处打点（host 明确的成功交互路径，失败不打）**：sudo_exec（tools/sudo_tool.py `_sudo_exec_handler` returncode==0 后；本地别名 → 本机主机名经 `_topology_host_names()` 匹配，匹配不到不打）；topo_status_sync（tools/topo_tools.py per-host `probe_err is None` 后，dry-run confirm=False 同样算交互——probe 已真实执行）；vssh（hermes_cli/subcommands/vssh.py `run()` 在 `os.execvpe` 前——到达即凭据解析+argv 构造成功）。
  3. **本机 host 按拓扑名匹配走本地 runner**：`_runner_for_host` 增加"host_name == socket.gethostname() → 本地 runner"——拓扑把本机注册为 docker-host（LAPTOP-T2JA2ERE，env: local）时无 SSH credential 也能本地 docker 探测并打点（而非 SSH 回环报凭据缺失），与 sudo_tool 本地打点口径一致。
  4. **API 合并**：topo_export.build_view 每 host card 合并 `load_activity()` 的 last_seen（epoch）；无记录 → 不返回该字段（前端显示"未探测"）。
- **任务 2（react-flow 拓扑图，前端主）**：
  1. **依赖**：`@xyflow/react@^12.11.3`（web 为 npm workspace 成员，锁文件随根 package-lock.json 同步）。
  2. **图模型（web/src/lib/topologyGraph.ts 纯函数 buildGraphModel）**：集群→主机→服务三层手摆布局（按集群分组列排，宿主相同服务纵向排布，结构清晰不重叠、label 全可见）；`MAX_GRAPH_NODES=300` 溢出保护（超限停止加节点防卡死）；host→services 实线、cross_host 连线、key_paths 链路琥珀粗线高亮；`nodeToneClass`/`entityFromNode`/`kindLabel`（节点→实体引用→抽屉）。数据来自现有 GET /api/topology，零新端点。
  3. **图组件（web/src/components/TopologyGraph.tsx）**：ReactFlow fitView/Controls/MiniMap/Background，节点不可拖拽；四种自定义节点组件（cluster 容器视觉/host 活性点+状态/service 状态着色/cross）；running 绿/stopped 灰/离线红复用 --vigil-ok/--vigil-error 状态色系；点节点 → 详情抽屉。TopologyPage 替换缩略图位置，保留下方卡片/列表视图（图=总览导航，卡片/列表=明细）；TopologyMiniGraph 保留（OverviewPage 仍引用，未删）。
- **任务 3（详情抽屉 web/src/components/DetailDrawer.tsx）**：右侧滑出抽屉（fixed 定位 + 遮罩点击关闭 + aria-label；复用 CSS 变量 + dropdown/审批 modal 阴影层次）；内容 = 顶部标题（实体名 + EnvBadge + StatusPill）+ DetailTree 全宽渲染（长命令不折行，overlay 滚动）；触发统一——图中节点点击 + 卡片"详情"按钮都开抽屉；卡片内展开组件 DetailBox 退役删除（DetailTree 保留）。HostCard 加活性行（在线/离线/未探测），ListRow 加活性列。
- **测试**：后端 tests/hermes_cli/test_batch35_runtime_state.py（13：runtime_state roundtrip/多 host/0600 权限/空 host 不打点/build_view 有·无 last_seen 两态合并/status_sync 成功打·失败不打/本机名=拓扑名走本地 runner/sudo_exec 成功打·认证失败不打/vssh 打点·无参列主机不打）。前端 52 vitest 全绿（topologyGraph 5：三层映射/连线+keypath/布局不重叠/300 上限/entityFromNode+着色；DetailDrawer SSR 3；ops lastSeenInfo 3 等）+ `npm run build` ✓ + `tsc -b` ✓。回归：批三十三拓扑状态组装 + 批十四 vssh + sudo_exec + topo_tools + topo_export 组合 **119 通过**。
- **实测（真实 ~/.vigil，dashboard 8803 + playwright chromium，真实模型回合）**：a 拓扑图渲染——48 节点、集群/主机/服务/cross 三层 label 全含、controls/minimap 在、12 个 emerald 运行态着色节点；b 缩放/平移均改变 viewport（生效）；c 点服务节点 → 抽屉（含"服务"kind + host/env/cluster/type detail 键）→ 遮罩关闭；点卡片"查看详情"→ 抽屉 → "关闭详情"关闭；d **last_seen 关键场景**——对话页发"请用 topo_status_sync 检查 host LAPTOP-T2JA2ERE（confirm=false，dry-run）"，agent 真实跑工具成功（本机名→本地 runner），回拓扑页该 host 节点显示"在线 · 刚刚活跃"，未交互 host 显示"未探测"；e 卡片/列表视图切换正常、"拓扑总览"标签在；页面 console 零 error。截图 /tmp/b35-shots/（a-graph/b-zoomed/c-drawer-from-node/c-drawer-from-card/d-activity）。
- **边界**：last_seen 阈值 10 分钟（web/src/lib/ops.ts `LAST_SEEN_ACTIVE_MINUTES`）——≤10 分钟"在线 · X 分钟前活跃"（绿点），超"离线 · 已 X 分钟无活动"（灰点），无记录"未探测"；图节点上限 300；活性零主动探测成本（不探活，agent 交互即活性；定时探活/prom 对接留第二/三层）；本机打点按拓扑 host 名匹配（runtime_state 只收拓扑已知 host）；last_seen 只进 runtime_state.json（0600 原子写），拓扑 yaml/hosts 零改动；sudo_exec 本地别名（localhost/127.0.0.1/空）不直接打点，先解析本机名匹配拓扑。
- **核销方式**：测试常驻——批次 35 套件全绿（13 后端 + 52 前端 + 119 回归）；季度体检检查：打点是否仍只三处成功路径（sudo_exec/status_sync/vssh）、runtime_state 是否仍只进运行时文件（未漏进拓扑 yaml）、本机名→本地 runner 是否被改回 SSH 回环、图上限 300/阈值 10 分钟是否被削弱、DetailBox 是否被复活且抽屉触发被改回卡片内展开。

### 51. 批次三十四 审批全局弹窗——全局轮询 + modal + 铃铛角标实时化（Codex 产出，2026-08-16）

- **背景（用户拍板 2026-08-16）**：审批是 Vigil 安全模型的核心交互，应是"全局一等交互"而非对话页内嵌——现状审批卡只在对话页 SSE（chat:approval_pending）出现，在拓扑页/审批页时 agent 请求审批完全不可见（多 session 并行审批的可见性痛点，批二十方案 B 同源）。决策：**前端轮询**（审批低频分钟级，3-5s 延迟可接受；SSE 留作以后可选）+ **页面内居中 modal**（非 OS toast，App.tsx 层全局挂载）+ **队列语义**（一次弹一个，处理完弹下一个，同 id 不重复弹）+ **忽略 = 标记 seen 收起**（≠拒绝，命令继续等待，超时照现有 timeout_policy）。后端零改动——审批注册表 + GET /api/approvals + POST approve/deny 现成（web_server.py），本批只动 web/src/**。
- **任务 1（全局审批轮询 web/src/lib/approvalPoller.ts）**：`ApprovalPoller` 单例——3-5s（默认 4s）轮询 `GET /api/approvals?status=pending`（limit 200，复用 api.ts getApprovals）；维护"已见 pending id"集合，新 id → snapshot.added 回调通知（弹窗入队），同 id 不重复上报；id 从 pending 消失（批准/拒绝/超时）→ 从已见集合释放（防无限膨胀，另加上限保护 200，超出不再接收）；轮询失败保留上次快照、下个 tick 重试、并发 poll 去重；`start()/stop()` 幂等（App 根组件持有）+ `refresh()`（批准/拒绝后手动刷新）+ `subscribe`/`getSnapshot`；hooks：`useApprovalPolling`（start/stop + 订阅，App 唯一持有者）、`useApprovalSnapshot`（只读订阅，弹窗/铃铛用）。
- **任务 2（审批弹窗 web/src/components/ApprovalModal.tsx + 队列纯逻辑 approvalQueue.ts）**：全局挂载（App.tsx 与 ApprovalBell 平级），任何路由可见；内容 = 标题"该命令需要审批" + grade 徽章（L{grade}，amber）+ 命令（monospace 完整显示，`whitespace-pre` 纵向滚动不折行）+ EnvBadge（prod 红/test 琥珀/dev 橙）+ 审批说明（approval 记录 description，如"delete in root path"）+ 按钮：批准（emerald）/拒绝（red）/忽略（次要样式）；深色遮罩居中，复用 vigil-card/vigil-btn/CSS 变量，无新依赖。队列语义在纯函数 `approvalQueue.ts`（enqueue 去重保序/dequeue/当前项不在 pending 自动移除/busy/error 状态位）：多个 pending 排队一次弹一个；批准/拒绝走 POST approve（scope=once）/deny → 成功移除弹下一个；失败内联报错可重试；忽略 = 移除队列（poller 已 seen，本次会话不再弹）。**无 ESC/遮罩点击关闭——忽略必须是显式动作，避免误触把安全交互藏掉**。
- **任务 3（与对话页卡片关系）**：对话页内嵌审批卡保留（消息上下文记录），全局弹窗与卡片独立工作、不共用状态；弹窗批准/拒绝后卡片状态由 agent 后续事件更新（现状行为，未动 ChatPage/SSE）；同一 approval 可能同时在卡片与弹窗可见，决策在弹窗完成（已注明边界）。
- **顺带**：ApprovalBell（App.tsx）角标 count + 下拉列表 + 顶栏 "Approvals N" 徽标全部改接轮询快照——实时角标，不再只挂载查一次；删除两处旧的挂载时一次性 getApprovals。
- **测试**：web/src/lib/approvalPoller.test.ts（6：新 id 只上报一次去重/id 消失释放可重报/上限 200 不重报/start 幂等+周期+stop/失败保留快照恢复/订阅退订+total）+ approvalQueue.test.ts（6：入队保序去重/空新增不产生新引用/忽略=dequeue 清错/批准拒绝后 queue[0] 轮转/别处裁决自动移除/busy+error 状态位）。回归：vitest 全量 40 通过（含批三十一 chat 状态机、批三十三 stateFromHistory、mock 凭据红线）+ `npm run build` ✓ + `tsc -b` ✓。ESLint 因仓库缺 `@eslint/js` 依赖预存故障无法运行（与本次改动无关）。
- **实测（真实 ~/.vigil，approvals.mode smart→manual 临时切，测完还原 diff 校验一致；dashboard 8802 + playwright chromium）**：a1 发 `rm -rf /tmp/vigil-b34-demo` → 弹窗出现（命令 + env=test 徽章 + 说明"delete in root path" + 三按钮）→ 批准 → modal 关闭、agent 回合完成、聊天审批卡同现（chat:approval_pending 回归 ✓）；a2 `chmod 777` → 拒绝 → 命令被拦、回合完成；**b 关键场景**：审批触发后立即切 /topology → 弹窗仍出现（全局可见性 ✓，URL 确认在拓扑页）；c 两个会话各触发一个审批 → 队列 [demo3, demo4]，modal 显示第一个，批准后自动切换弹第二个（队列语义 ✓）；d 忽略 → 弹窗收起、铃铛角标保留 1、铃铛下拉可见、审批中心列表可见（审批中心批准后回合收尾）；e 铃铛角标 0→1 实时变化；页面 console 零 error。另用路由拦截注入 env=prod/grade=5/长命令的伪审批验证渲染：命令完整显示、L5 徽章、prod 徽章、说明、三按钮全命中。
- **边界**：轮询 4s 延迟（审批低频，可接受；批准/拒绝后 refresh() 立即刷新角标）；忽略 ≠ 拒绝——命令继续等待，超时（300s/现有 timeout_policy）后由端点标 timeout、下个 poll 自动移出队列；弹窗与卡片独立——弹窗决策后卡片状态由后续 SSE 事件更新，可能短暂双显；审批记录不含明文凭据（端点过 _redact_tree，命令为已脱敏展示值；凭据终扫通过）；后端零改动（未动注册表/权限矩阵/SSE 事件形态，未新增 env var）。
- **核销方式**：测试常驻——批次 34 套件全绿（poller/queue 12 例 + 回归 40）；季度体检检查：全局弹窗是否仍接轮询（铃铛角标是否又退化为挂载查一次）、忽略语义是否被改回"拒绝"（命令等待语义被破坏）、弹窗是否仍全局挂载（App 层）、新审批通道（SSE 若后续加入）是否与 poller 已见集合去重冲突。

### 54. 批次三十七 行为层补完——诊断命令固化 + ports 字段 + memory 拦截 + 重绘修复 + discover 引导（Codex 产出，2026-08-16）

- **背景（2026-08-14 NetBox dogfood 实验 §AF-AD 的 5 个未落地小项）**：①§X agent 排障时现拼诊断命令（19.5 分钟里 12.9 分钟审批等待 + 3 版脚本试错）；②§Y 拓扑实体只有 endpoint 无 ports（prometheus 9090 没记录 → LLM 只能 docker exec 进容器查端口）；③§Z 拓扑数据写进 memory 而非 topology（污染记忆且不进权威拓扑表）；④§AB 中断/deny 审批后历史消息段重复渲染（显示层假重复）；⑤C3 topo_discover 返回无下一步引导（LLM 会话内绕圈）。基线 28f7692（批三十六后）。
- **任务 1（§X 诊断命令固化，agent/prompt_builder.py + agent/system_prompt.py）**：新 `OPS_DIAGNOSTIC_COMMANDS_GUIDANCE` 常量（预设只读命令集：`free -h && swapon --show` / `df -hT` / `uptime && cat /proc/loadavg` / `top -bn1 | head -20`，标注 L1 只读不触发审批），与 SSH 纪律同门控（`_OPS_SECURITY_TOOLS`）注入 stable tier。静态文本 → 缓存前缀字节稳定。
- **任务 2（§Y 拓扑 ports 字段，tools/topo_tools.py + topo_export.py + 前端）**：新 `_normalize_ports`（实体 schema v0.3 可选 `ports: [9090, 443]` 列表，load 校验：非空 1..65535 整数列表；缺失/空 → None，老文件完全兼容；非法逐项丢弃告警，绝不打断加载），接入 `load_topology`（hosts/cross_host/clusters 行）、`_load_host_index`（服务行）、`_load_entity_file`（第三层档案）；`_card_fields` 白名单返回值补 `ports`；`TopologyCard.ports` + TopologyPage Facts/ServiceRow 展示（DetailTree 本就渲染列表，详情抽屉自动带出）。**未改拓扑 yaml 现有字段语义，未动 redact/审批/权限矩阵，未新增 env var**。
- **任务 3（§Z memory 拦截，tools/memory_tool.py + prompt 常量）**：`memory_tool` 写入路径（单条 add/replace + 批量 operations）新增拓扑事实拦截——`_topo_fact_names` 取活拓扑表实体/主机/集群名集合 + IP 正则 + endpoint 形态（host:port / ip:port，纯时间 09:30、纯词 timeout:30 不误伤）→ 命中返回 tool_error 引导"平台事实写拓扑表（topo_update），记忆只放人的偏好"；普通偏好（无拓扑特征）放行。prompt 层同区注入 `OPS_TOPO_MEMORY_GUIDANCE`（topo 工具 + memory 工具同时在场才注入），双管齐下。拦截在审批门之前（拓扑事实连 stage 都不进）。
- **任务 4（§AB CLI 重绘假重复，cli.py）**：`_recover_terminal_after_interrupt` 不再走 `_force_full_redraw`（清屏 + `_replay_output_history`）——中断/deny 是内部事件，终端内容未被外部清空，清屏重放把同一段历史再渲染一遍就是"历史段重复"的来源。保留 `flush_stdin`（#33271 stdin 侧修复）+ 原地 `app.invalidate()` 重绘活体 chrome；Ctrl+L / `/redraw` / resize 的外部清屏重绘路径不变。
- **任务 5（C3 topo_discover 引导）**：确认已落地（commit 7295ce7，`_discover_handler` 返回尾部 `_guide`：发现摘要 + topo_query/topo_update 下一步建议），本批补验收断言即可。
- **测试**：新增 34 例后端（§X 常量含三类命令 + 门控注入/缺省不注入 3；§Z 常量 3 + memory 拦截 11（实体名/集群名/IP/endpoint 拦、偏好/纯端口/时间放行、replace 拦、批量原子拒、无拓扑表 IP 仍拦）；§Y `_normalize_ports` 4 + ports 传播 2 + export build_view/card 2；§AB interrupt 恢复不重绘 4 + 更新原 #33271 恢复测试 3）+ 前端 2（TopologyPage ports 渲染/缺省）。回归：批三十三拓扑状态 22 + 批三十五 runtime state + topo slash + topo export 31 + memory 套件 44 + system_prompt 31 + cli force_redraw/中断恢复 16 + ops_dashboard_api 20 全绿；`tsc -p . --noEmit` ✓ + vitest 新例 ✓。预存环境失败与本次无关（test_approval launchctl / daytona / credential_files / web_tools_config 等，stash 基线验证同失）。
- **边界**：memory 拦截是行为护栏非安全边界——实体名匹配按词边界，偏好文案含实体名仍会拦（引导拆分）；拓扑加载失败时名字信号为空，仅 IP/endpoint 形态信号生效；ports 校验丢弃非法项不阻断加载；§AB 修复针对 interrupt/deny 恢复路径，外部清屏（Ctrl+L/resize）的重放语义保持不变。
- **核销方式**：测试常驻——批次 37 套件全绿；季度体检检查：诊断命令常量是否仍随 `_OPS_SECURITY_TOOLS` 注入（缓存前缀字节稳定）、`_normalize_ports` 是否仍容忍非法项、memory 拦截是否仍先于审批门、`_recover_terminal_after_interrupt` 是否被重新接回清屏重放（§AB 回归）。

### 55. 批次三十八 会话生命周期显式状态机 + 停止/审批超时兜底 + 自举观测（Codex 产出，2026-08-17）

- **背景（2026-08-17 dogfood 第 6 天 §AS/§AI/§AW）**：网页 UI chat 会话反复"卡死"——turn 正常结束但 session 永不落终态（ended_at=None，UI 永远"进行中"，§AS A1）；主 API 重试耗尽无错误消息/无 finalize/无 fallback 线程凭空消失（§AS A2）；UI 停止只停轮询不停后端 turn（§AS A3）；审批超时后 session 卡死（§AW）；会话状态无可观测命令、last_activity 失真（§AS A5/A4 + §AQ）。基线 633052a（v0.1.19，批三十六/三十七后）。
- **任务 1（sessions 显式 status 字段 + finalize 统一入口，hermes_state_common.py + hermes_state_schema.py + hermes_state.py）**：sessions 表新增 `status TEXT NOT NULL DEFAULT 'running'`（running/ended/failed/interrupted/finalize_error），SCHEMA_VERSION 25→26；v26 一次性存量迁移（ended_at 非 NULL → ended，版本门控不覆盖后续显式写入）；`end_session`/`reopen_session`/`promote_to_session_reset`/压缩父行/孤儿压缩全部同步写 status；新 `SessionDB.set_session_status`（非法值拒绝）+ `finalize_session_row` 统一入口（try/except 兜底：主写失败 → 二次写 `finalize_error` 落库再 re-raise，不允许静默跳过；ended_at/end_reason 用 COALESCE 保留已有终态边界如 compression）。UI 状态改读 status（`session_status_is_running` helper，兼容 ended_at 兜底）——web_server /api/sessions running、状态页 active 计数、cron runs、web_routers/sessions is_active 四处。
- **任务 2（turn 级异常兜底，hermes_cli/chat_api.py）**：`_run_chat_turn` finally 统一收尾——异常/error 结果 → 用户可见 `chat:error` + finalize(ended, error)，重试耗尽返回 failed=True 的路径同样落 ended(error)，绝无静默退出；行状态无残留 running。
- **任务 3（UI stop 语义与后端一致）**：批三十六已实现 `POST /api/chat/sessions/{id}/interrupt`（hard_cancel），本批补齐中断后 `finalize(interrupted)` + busy 复位（worker finally 单点写入）+ 下一轮 turn 起点 `set_session_status(running)` 复活；UI 停止按钮已走 interrupt 端点，不再只停轮询。
- **任务 4（审批超时/拒绝路径 finalize，hermes_cli/chat_api.py + web/src/pages/ChatPage.tsx）**：会话层自限审批等待窗口（由 timeout_at 推导剩余秒数传给 `wait_web_approval`，超时返回 "timeout"，审批核心逻辑零改动）；回调记录终态（approval_timeout/denied）→ turn finalize 落 ended(approval_timeout/denied)，interrupt 优先；前端审批卡 `approvalIsTimedOut` 客户端兜底——timeout_at 过期显示"审批超时，已终止"不再转圈。
- **任务 5（观测命令 + last_activity，hermes_cli/sessions_cmd.py + hermes_cli/db_cmd.py + main.py）**：`vigil sessions status <id>`（status/ended_at/end_reason/busy（活动注册表）/有效 last_activity（last_activity_at 与 messages 最大时间戳取新，复用 `_sql_session_last_active`）/最近 5 条工具调用，新 SessionDB `get_recent_tool_calls`）；`vigil db query "<SQL>"`（封装 SessionDB 只读连接，不依赖系统 sqlite3；仅放行 SELECT/EXPLAIN/PRAGMA，单语句约束，写语句/分号拼接在打开连接前拒绝并提示风险；字符串单元格 force redact 不泄露凭据）；web chat 工具完成即刷新会话 last_activity（§AS A4）。
- **测试**：新增 40 例后端（status 列/migration/迁移只跑一次 13；chat turn finalize 7（正常→ended/turn_complete、error→ended/error、异常→ended/error、interrupt→interrupted+可续、审批超时→ended/approval_timeout、审批拒绝→ended/denied、finalize 失败→finalize_error 落库）；sessions status 命令 6；db query 命令 9）+ 前端 5（`approvalIsTimedOut` 四态）。回归：hermes_state 全套 + batch31/33/36 chat + 单查 finalize + 会话边界 hooks + cli new/resume/busy_input + gateway api_server + turn_finalizer/中断/fallback 套件 + dashboard 全套全绿；`tsc -p . --noEmit` ✓ + vitest chat 套件 ✓。预存环境失败：`test_web_server.py::TestBuildSchemaFromConfig::test_no_single_field_categories`（stash 基线验证同失，与本批无关）。
- **边界**：status 语义 = 会话生命周期状态，多轮 web chat 每轮收尾落终态、下一轮起点复活 running（ended_at 同时作为终态时间戳）；压缩轮换后 finalize 打 agent 活行 session_id 而非创建时 chat_session_id；审批超时自限等待只动会话层，`reclaim_timed_out_web_approvals`/审批核心逻辑零改动；`vigil db query` 是只读诊断面，不替代 `vigil sessions` 管理操作。
- **核销方式**：测试常驻——批次 38 套件全绿；季度体检检查：`finalize_session_row` 是否仍保证 finalize_error 兜底、web chat turn 收尾是否仍走统一入口、`session_status_is_running` 是否被重新接回 ended_at 直读（status 回归）、db query 白名单是否仍拦截写语句。

### 56. 批次三十九 拓扑三层写同步 + 查询一致性 + 实体文件名双后缀（Codex 产出，2026-08-17）

- **背景（2026-08-17 dogfood 第 6 天 §AV 实测，chat_2d9565c1）**：topo_update(needs_review:false) 返回 updated，但 L3 实体文件顶层 needs_review 仍 true（`_TOPLEVEL_UPDATE_FIELDS` 白名单不含 needs_review，值被写进 attrs）；topo_query 读 L2 hosts 索引行（该行仍 true）→ 更新"看起来没生效"；手动 patch L3 → topo_query 仍读 L2 旧值。根因：topo_update 只写 L3 detail（唯一 write_text），不同步 L2 hosts 索引——写读不同层。另发现实体文件名双后缀 `entities/local_dsl-review-dsl-review.yaml.yaml`（cluster 前缀 + 名字重复 + .yaml 双拼）。基线 7a52048（批三十八后）。
- **任务 1（topo_update 三层写同步，tools/topo_tools.py + tools/topo_discovery.py）**：`_TOPLEVEL_UPDATE_FIELDS` 白名单补 `needs_review`（走顶层，不再落 attrs）；写 L3 后对服务行（`_host` 标记，第一层 host/cross_host 行自身在 topology.yaml 不属于 L2 索引层）同步 L2 hosts 索引对应行——`_L2_INDEX_SYNC_FIELDS` = needs_review/source/last_verified/status（与 L3 写的一致；L2 是服务索引，endpoint/owner 等全量档案留在 L3）；索引缺行按 L2 行形态补建（缺行本身就是漂移，补建优先）；写失败不吞掉（记日志 + 返回带 `l2_index_warning`，L3 已写入不受影响），成功审计带 `l2_index_synced: <host>`。
- **任务 2（查询一致性兜底，L3 为审核态权威）**：`_enrich_entity_detail` 把 `needs_review` 纳入 detail 补齐集——但按 L3 权威语义覆盖（L3 显式携带即覆盖 L2，含 false 假值；L3 缺字段保留 L2 setdefault 兜底）；`_query_entity`（entity= 跨层解析）与 `_query_host`（host= 索引展开）两条路径都走 `_enrich_entity_detail`，status/type/env/attrs 同款补齐。任务 1 写同步 + 本任务读回退双管，单边失效也不漂移。
- **任务 3（实体文件名双后缀 + 重名去重，tools/topo_discovery.py）**：`_entity_filename` 对 name 段先剥 `.yaml`/`.yml` 后缀再拼文件名（`_strip_entity_ext`，杜绝 `*.yaml.yaml`）；`{base}-{base}` 重复形态去重（`_dedupe_repeated_name`，§AV 的 dsl-review-dsl-review 只影响文件名不影响实体名）；`_service_from_container` 补前缀叠加守卫（name 已带 `{project}-` 前缀不再拼第二遍）；`write_discovery` 的 detail fallback 同款剥后缀；`_load_entity_file`/topo_update 读入走 `_entity_detail_candidates`——存量 `*.yaml.yaml` 文件剥一层再匹配，查询兼容不阻塞，不自动重命名存量。
- **任务 4（并入：其它 L3 写入路径同步检查）**：全仓 L3 写入口审计——topo_update（已修）、write_discovery（同函数内 L2+L3 一次写齐，结构上一致）、topo_status_sync（confirm 复用 topo_update，自动收敛）；统一抽共享 `sync_l2_index_row(home, *, host_name, entity, fields, services_index, cluster)` 帮助函数（缺行补建/越界拒绝/失败返回 warning 不吞掉），新老写入路径共用，杜绝第三处各写各的。只收敛字段语义，不改拓扑数据模型、无 schema 变更。
- **测试**：新增 15 例后端（tests/tools/test_batch39_topo_sync.py——白名单 needs_review 顶层不落 attrs 1；三层一致 false/true 2；status 同步 1；L2 写失败 warning 不伤 L3 1；v0.1 无 L2 不触发 1；entity=/host= 查询 L3 权威（含 L3 缺字段保留 L2）3；_entity_filename 剥后缀/重名去重/write_discovery fallback 3；存量 *.yaml.yaml 查询兼容 1；共享帮助函数缺行补建/越界拒绝/status_sync confirm 收敛 3）。回归：全 topo 套件 173 全绿（tools topo 五件 + batch33 topo slash/status + topo_slash_command + topo_export + web_server topology 缓存/网关 + memory topo_provider）。预存环境失败与本批无关（launchctl/daytona/credential_files/web_tools_config/视频生成/voice/watch/vercel 等，stash 基线同失，均不涉 topo 模块）。
- **边界**：L2 索引行只镜像审核/状态生命周期字段（needs_review/source/last_verified/status），不复制全量档案；`{base}-{base}` 去重只作用于文件名路径，实体名 `name:` 不变；存量 `.yaml.yaml` 不自动重命名（后续可手动）；topo_update 返回值契约不变（仅 audit 新增 l2_index_synced/l2_index_warning 字段）。
- **核销方式**：测试常驻——批次 39 套件全绿；季度体检检查：`_TOPLEVEL_UPDATE_FIELDS` 是否仍含 needs_review、topo_update 写 L3 后是否仍调 `sync_l2_index_row`、查询路径是否仍走 `_enrich_entity_detail`（needs_review 以 L3 为权威）、`_entity_filename` 是否仍剥扩展名/去重（新写入出现 `.yaml.yaml` 即回归）。

### 57. 批次四十 CLI 命令集统一 + 写防护/配置管理缺陷（Codex 产出，2026-08-17）

- **背景（2026-08-17 dogfood 第 6 天 §AG/§AU/§AS B·C）**：dashboard 命令集形态不统一
  （stop 是 `--stop` flag 不是子命令）；runbook 格式门控静默失败（runbooks/ 下非
  .yaml 被忽略无警告）；write_file 防护误判 + redact 破坏性打码（`finish_reason=
  tool_calls`、`Qwen3_5_397B_A17B_FP8` 被打成 «redacted-value»，原文丢失）；config
  管理三缺陷（config set 把 list 当 str 存、配置写入防护只在工具层可绕过、存量显式
  platform_toolsets 覆盖新默认且 migrate 不合并）。基线 fcd8b08（批三十九后）。
- **任务 1（dashboard 子命令统一，hermes_cli/subcommands/dashboard.py + main.py）**：
  新增 `vigil dashboard start|stop|restart` 子命令（start=裸启动、stop=优雅停、
  restart=stop+start，对齐 systemctl/docker 直觉）；`--stop`/`--status` flag 保留兼容
  仅 help 标 deprecated 不报错；`status` 子命令统一为"进程表（PID/端口/进程启动时间/
  host 最近活跃 heartbeat）+ systemd unit 摘要"（批三十六的 systemd status 语义并入，
  dashboard_service 本体不动）；`_report_dashboard_status` 输出补 port/started/
  last host activity；`cmd_dashboard_stop`/`cmd_dashboard_restart` 独立 handler，
  `--stop` 分支委托同一 kill 路径（安全不变量：stop 永不落入 server-start）。
- **任务 2（runbook 格式门控可见，tools/runbook_tools.py + file_tools.py）**：
  `check_runbook_requirements` 扫描 runbooks/ 下非 `.yaml` 文件（.md/.yml/其他）→
  `_warn_ignored_runbook_files` 去重警告（点名文件 + "仅支持 schema v0.1 的 .yaml"）；
  写入端 `_check_runbook_write_path`：write_file 落盘 runbooks/ 目录非 .yaml 扩展名
  → 拒绝并提示正确格式（.yaml 放行）；runbook_load/create 描述补"只支持 .yaml"。
- **任务 3（askpass 正则收窄，tools/file_tools.py §AS B1）**：`_check_askpass_script_write`
  加 `_looks_like_bare_askpass_value` 值形态过滤——含 `$()`/反引号/空白/管道/`&&`/`;`/`>`
  的 echo 值不算裸凭据（runbook 诊断 `echo "$(cat $t/comm) $(grep ...)"` 放行）；
  真 askpass 形态（shebang+echo 裸值 / printf 裸值 / 无 shebang 裸值写 ~/.vigil、
  ~/credential、~/.vigil/secrets）仍拦截（安全底线不回退，terminal 硬线不动）。
- **任务 4（redact 可逆化，agent/redact.py + file_tools.py §AS B2）**：`redact_sensitive_text`
  新增 `persist_write=True`（write_file 落盘）——跳过展示面登记值 pass（模型名/字段名
  等公共形态不再被登记表破坏文档），真凭据由前缀/密钥键名/私钥等凭据形态 pass 覆盖；
  `reversible_write_redaction` 上下文管理器 + `_mask_token` 捕获：被掩码值落为
  `«redacted:N»` 可逆占位并记录原文；`_redact_write_content` 返回 (masked, 敏感, 占位表)；
  写打码内容前先写 `<target>.redact-backup.json`（0600，含原文 + 占位映射），
  `restore_redacted_write` 还原 byte-identical 原文；`_lockdown_credential_file` 警告
  注明恢复点。展示面（终端输出）登记值打码行为不变。
- **任务 5（config set list 值修复，hermes_cli/config.py §AS C1）**：`set_config_value`
  对 default 为 list/dict 的 key 先 `yaml.safe_load`——解析为 list/dict 按结构落盘；
  解析为标量 → 拒绝并提示 `vigil config edit`（不再 str 落库后被下游当字符列表遍历）；
  bool/int/float 与 str-typed key 既有行为回归不变。
- **任务 6（config 写防护收敛，hermes_cli/config.py §AS C2）**：方向=接受可写、保护下移。
  新增 `_audit_config_write`（写入后 `validate_config_structure` + 追加
  `<VIGIL_HOME>/logs/config-audit.log`，带时间戳 + source + error 级问题）；
  `config set` 落盘后校验并打印警告（已保存但可能不生效）；`save_config` 统一入口
  后置审计（脚本绕过路径 python+yaml.safe_dump 留痕）；工具层 `_check_sensitive_path`
  拒写 config.yaml 保留（不再是唯一防线）。
- **任务 7（platform_toolsets 并集迁移，config_migrations.py §AS C3）**：版本 33→34，
  新增 `_migrate_to_34`：存量显式 `platform_toolsets.cli` 列表 ∪ 默认 topo/runbook
  （缺哪个补哪个），经 `_persist_migration` 落盘 + results.config_added diff 提示 +
  非 quiet 打印"已合并默认工具集"；无显式列表不动（不产生 defaults dump）。kanban
  隐式展开核查：`kanban_*` 工具在 `_VIGIL_CORE_TOOLS` → 复合 toolset 展开使
  `_get_platform_tools('cli')` 含 kanban——意图内（schema 由 kanban_tools check_fn
  门控：无 VIGIL_KANBAN_TASK / 无 profile 显式启用即零工具），不收敛，加回归测试。
- **测试**：新增 58 例后端（tests/hermes_cli/test_batch40_dashboard_lifecycle.py 15——
  子命令分发/--stop 兼容/stop 不落 server-start/restart 三态/status PID+端口+心跳；
  test_batch40_config_set_list.py 5；test_batch40_config_audit.py 5；test_batch40_
  migrate_platform_toolsets.py 7——并集迁移/diff/无 dump/kanban 门控；tests/tools/
  test_batch40_runbook_gate.py 9——.md 警告/纯 yaml 无警告/写端扩展名拒绝；
  test_batch40_askpass_narrow.py 10——诊断放行/真 askpass 拦截；test_batch40_redact_
  reversible.py 7——登记模型名原文保留/真凭据打码+备份+还原/展示面不回退）。
  回归：401 例（dashboard 生命周期+批三十六 service、runbook 三件+ops 注册、askpass
  拦截、redact registry+8 个 redaction 套件、file_tools 五件、config 五件 + toolsets/
  toolset_validation 等）全绿。预存失败与本批无关：test_toolset_validation 断言
  'vigil' 而输入 'hermes'（基线同失）、test_update_stale_dashboard ps fallback
  （基线同失）、launchctl/daytona/credential_files/web_tools_config/视频生成/voice/
  watch/vercel 等环境类。
- **边界**：`--stop`/`--status` flag 永不移除（兼容）；`restart` 无进程时直接启动；
  落盘可逆备份只覆盖 write_file 单次写入（后续手动编辑不再同步 sidecar）；迁移是
  一次性版本步进（用户可在迁移后手动删 topo/runbook）；`«redacted:N»` 占位与
  `«redacted-value»` 等既有 mask 同风格（`_already_masked_value` 已识别，防二次打码）。
- **核销方式**：测试常驻——批次 40 六件套全绿；季度体检检查：dashboard 子命令
  start/stop/restart 存在且 stop 分支在 server-start 之前；runbook 门控警告与写端
  扩展名校验在；askpass 值形态过滤在（真 askpass 仍拦截）；redact persist_write 跳过
  登记值 pass + 备份还原在；config set list 解析 + config-audit.log 审计在；migrate v34
  并集在（`_migrate_to_34` 注册于 MIGRATIONS）。

### 58. 批次四十一 Chat 页 UI 八项缺陷收口（Codex 产出，2026-08-17，UI 反馈单 23-30）

- **范围**：web/src/（ChatPage/ApprovalsPage/ApprovalModal/lib·api·chat）+ 后端最小支撑
  （chat_api.py / web_server.py / tools/approval.py / agent/conversation_loop.py 一行持 id）。
  前后端契约改动全部写在本条；审批核心裁决逻辑零改动（只做幂等化），不碰
  conversation_loop/system_prompt/prompt_cache/compression/权限矩阵/拓扑/批四十 redact 防护。
- **§5 工具输出零错位（硬性要求，最高优先）**：根因=并行同名单工具（如两个 terminal
  命令）结果按"最后一个同名未出结果"挂接 → 完成顺序与工具顺序不一致时输出串位（查
  nvidia 显 mysql 结果）；历史折叠的 reversed 匹配在结果行乱序入库时同源错位。修法：
  ① `agent/conversation_loop.py` 的 tool_callback 事件增量 `tool_id`（模型 tool_call id，
  纯增量字段，其它消费方零影响）；② `chat_api._tool_cb` 转发 `chat:tool`/`chat:tool_result`
  带 tool_id（提供商无 id 时按 turn 内顺序补序号兜底）；③ 前端 `applyChatEvent` 按
  tool_id 精确挂接（无 id 时回退旧按名行为）；④ `_history_to_view_messages` 改正序
  挂接（结果行恒按 tool_calls 顺序落库）+ 保留 tool_id 字段。回归测试：前端交错
  同名单事件零错位（chat.test.ts）+ 后端 SSE 事件 tool_id 序列断言（test_batch41_
  chat_ui_support.py）+ 历史正序折叠断言。
- **§23 停止后幽灵 busy 残留（反馈单 23）**：根因=本地 markTurnInterrupted 置 busy=false
  后，后台轮询效应被短路（依赖本地 busy），注册表真 busy 残留时用户只见 409 且
  UI 无察觉。修法：stopTurn 后独立跑 `verifyBusyCleared`（≤10s 每 2s 轮询注册表；
  翻转→强制全量重拉历史+重挂"已停止"本地行；未翻转→可见警告"仍显示忙碌，可在会话
  列表刷新重试"+解除本地快照 busy 防永久锁死）；轮询恢复路径保留 interrupted 标记
  （markTurnInterrupted 幂等，不重复追加）。后端核查只读：`chat_session_messages` 的
  busy 源=注册表 `session.busy`，worker finally 复位——批三十八已做，未改。
- **§2 审批批准后不刷新/重复提交报错（反馈单 24）**：后端 `approve_web_approval`/
  `deny_web_approval` 幂等化——已裁决重复请求返回当前终态（`{status, already_resolved}`
  + 原 scope），不再 invalid_request；web 端点照实返回状态（200）。前端弹窗提交中
  ref 同步防重入（连点只发一次）+ 成功即 dequeue 弹下一个（原逻辑保留）。
- **§3 推理过程查看（反馈单 25）**：后端数据源核查=SessionDB 持久化 assistant 消息
  自带 reasoning/reasoning_content，历史视图已可读——新增 `_history_to_view_messages`
  输出 `reasoning`（纯文本）；流式新增 `chat:reasoning` 增量事件（turn 内设置
  `agent.reasoning_callback`，finally 复位）。前端 assistant 消息渲染"查看推理过程"
  折叠块（默认一行摘要，展开完整链，样式照代码块）。
- **§4 工具/审批有序步骤序列（反馈单 26）**：前端消息模型新增 `steps` 有序序列
  （工具/审批按事件到达顺序串①②③…，状态标签 完成/进行中/等待审批/待执行/已批准/
  已拒绝），审批卡内联在对应步骤下方；多步时顶部显示"共 N 步 · 当前状态"。
- **§6 审批详情可展开 + 长命令滚动（反馈单 28）**：后端关闭 description 单向截断
  （chat_api 注册审批/SSE 事件存全量 redact 文本）；新增 GET /api/approvals/{id} 详情
  端点（全量 command/description/grade/env/session_key/source）。前端审批列表行可展开
  完整命令（等宽 + max-height 滚动 + 展开全文，保留缩进）+ 完整描述；弹窗长命令同样
  处理。
- **§7 多 session 切回 thinking/busy 指示恢复（反馈单 29）**：注册表 busy 快照
  busyMap（每次 listChatSessions 更新）+ activeId 变化立即重拉列表；本地槽位丢失 busy
  时用快照恢复"处理中"指示（独立于消息流事件）；有效 busy=本地||快照，轮询与输入禁
  用都吃有效 busy；历史/轮询恢复路径不依赖增量事件。
- **§8 对话页可选模型（反馈单 30）**：新增 GET /api/models（读 config + 静态目录，零网
  络零敏感信息；默认模型标 default，tag 派生 快/省 vs 强/慢）；POST /api/chat/sessions
  支持可选 model（目录校验，缺省用配置默认，会话级生效）；新增 POST
  /api/chat/sessions/{id}/model 切换（目录校验、忙时 409、失败回滚字段）。前端输入区
  上方模型下拉（选项严格来自接口，按会话记忆，新建会话继承当前选择）。
- **端点变更清单**：新增 GET /api/models、GET /api/approvals/{id}、POST
  /api/chat/sessions/{id}/model；POST /api/chat/sessions 接受 body {model}；approve/deny
  幂等化（200 + 当前状态）；chat SSE 新增 chat:reasoning 事件；chat:tool/chat:tool_result
  新增 tool_id 字段；GET /api/chat/sessions/{id}/messages 历史消息新增 reasoning 与
  tools[].tool_id 字段。
- **测试**：前端 vitest 87 全绿（批四十一新增 20：chat 状态机 tool_id/推理/步骤/幂等
  markTurnInterrupted；ChatPage 组件 7——stop 校验恢复/10s 警告/切回 busy 恢复/模型下拉/
  新建带模型/推理折叠展开/步骤标签；ApprovalModal 3——批准即关/连点单发/长命令展开；
  ApprovalsPage 2——详情展开不截断/短命令直显）；`npm run build` ✓、`tsc -p . --noEmit` ✓
  （ESLint 因仓库缺 @eslint/js 预存故障无法运行，与本次无关）。后端新增 20 例
  （test_batch41_approval_idempotent.py 9 + test_batch41_chat_ui_support.py 11）；
  回归：批三十一/三十三/三十六/三十八 chat 套件 + 批二十八 exec + 审批注册表/超时/
  拒绝规则/智能策略 + 批四十六件套 58 + web_server 相关 36 全绿。预存失败与本批无关：
  test_approval.py::TestLaunchctlGatewayLifecycle（launchctl 环境类，基线同失）。
- **核销方式**：测试常驻——tool_id 交错序列回归（前端 chat.test.ts + 后端 SSE 断言）；
  approvals 重复批准/拒绝返回 200+当前状态；chat:reasoning 事件 + 历史 reasoning 字段
  在；步骤序列状态标签单测在；审批详情展开 + 长命令 jsdom 不截断断言在；ChatPage 组件
  测试切回 busy 恢复 + stop 校验在；/api/models + 会话 model 创建/切换端点单测在。

### 59. 批次四十二 Chat 页状态同步族——审批联动/消息顺序/reasoning 按步骤挂载（Codex 产出，2026-08-18，dogfood §BH/§BI/§BJ/§BK + §AY 设计否决）

- **范围**：web/src/（ChatPage/ApprovalModal/ApprovalsPage/lib·chat·api·approvalEvents）+ 后端最小支撑（chat_api.py 两处：SSE reasoning 归属 + 历史 reasoning 结构化）。审批裁决逻辑/权限矩阵/拓扑/批四十 redact·askpass 防护/conversation_loop 核心零改动（仅转发层缓冲归属，不深入循环内部）。
- **§BH 全局弹窗批准后 chat 内审批卡同步（dogfood §BH）**：根因=两条裁决路径互不广播——ApprovalModal/ApprovalsPage 走各自队列/列表，ChatPage 的 chat 内审批卡只认本地 resolveApproval。修法：新增前端 pub/sub 模块 `web/src/lib/approvalEvents.ts`（notifyApprovalResolved + subscribeApprovalResolved）；弹窗/审批中心批准·拒绝成功即广播（id + 终态）；ChatPage 挂订阅，按 approvalId 对所有会话槽位 `markApprovalResolvedInSessions` 幂等回写（只重建含该卡的槽位，无匹配卡返回原引用不触发重渲染）。不选全量重拉 getChatHistory——审批卡是 SSE 在途态、不在历史端点里，重拉反而丢卡。
- **§BI 结论渲染顺序（dogfood §BI）**：MessageBubble 重排为 步骤序列（StepList）→ 消息级推理（ReasoningBlock）→ 结论 content 最尾（过程在前、结论收尾，对齐 CLI 惯例）；内容为空（流式中/无正文）不渲染空占位（data-testid 包裹条件渲染）。纯前端，历史消息同样生效。
- **§BJ reasoning 按工具调用粒度挂载（dogfood §BJ，43 步长会话"查看推理过程"单框被覆盖）**：后端最小透传——`chat:reasoning` 增量先入 turn 内缓冲（推理先于工具执行到达，此时还不知道将导向哪个 tool_call），`tool_start` 到来时按该 tool_id 归属转发（chat:tool 先推再推 chat:reasoning.tool_id，前端先建行再挂）；未认领的推理（最终答复前思考/异常收尾）在 done/error 前按消息级转发。历史消息 reasoning 兼容双结构：单值字符串（0/多工具调用、缺 id、空推理）或 `{steps:[{tool_id,text}]}`（恰一个带 id 工具调用）。前端：ChatToolEvent 新增 reasoning，applyChatEvent 按 tool_id 归属（未匹配退回消息级）；StepList 每工具条目下"该步推理"折叠行（默认折叠，展开全文）；stateFromHistory 按 steps.tool_id 挂回工具行、旧单值仍读消息级。
- **§BK 审批卡显示触发步骤推理摘要（dogfood §BK，盲批风险防护）**：StepList 计算每个审批步骤前最近一个工具步骤的该步推理（SSE 顺序天然满足：推理随 chat:tool 先到、审批在工具执行内到），ApprovalCard 新增 triggerReasoning——一行摘要 + 可展开（默认折叠），批准/拒绝前用户可先看 agent 为什么执行该命令。
- **§AY 模型选择器去用途标签（设计否决，产品不教育用户）**：ChatPage 下拉去掉 `m.tag` 的"快/省 vs 强/慢"渲染，只显示模型名 + `· 默认` 标识（config model.default 是事实信息保留）；后端 /api/models 的 tag 字段保留（兼容，前端不渲染）。
- **端点/事件/结构变更清单**：① chat SSE `chat:reasoning` 事件新增可选 `tool_id`（归属该步工具调用；旧无 tool_id 语义不变=消息级）；② GET /api/chat/sessions/{id}/messages 历史 `reasoning` 字段允许 `string | {steps:[{tool_id,text}]}`（旧单值读取兼容）；③ 其余端点/审批裁决/审批注册表零改动（task 1 纯前端广播）。
- **测试**：前端 vitest 104 全绿（批四十二新增 17：chat.test.ts 9——tool_id 归属/未归属回退/并行不串/历史结构化挂回/旧单值兼容/markApprovalResolvedInSessions 槽位幂等；ChatPage 组件 6——§BI DOM 顺序/空内容无占位/该步推理折叠展开/审批触发推理展开/全局批准·拒绝广播回写；ApprovalModal 2——批准/拒绝广播调用；approvalEvents 2——订阅退订/异常隔离）；`npm run build` ✓、`tsc -p . --noEmit` ✓。后端新增 7 例（test_batch42_chat_ui_sync.py：SSE 归属顺序/无工具消息级/收尾未认领 + 历史单工具结构化/多工具旧值/无工具旧值/缺 id 旧值）；批四十一 test_reasoning_sse_events 断言随聚合语义更新（无工具时缓冲聚合为单事件，文本拼接等价）。回归：批 31/33/36/38 chat 套件 + 批 28 exec + 审批超时/拒绝规则/批四十一幂等 + 批四十六件套 58 全绿。预存失败与本批无关：`test_web_server.py::TestBuildSchemaFromConfig::test_no_single_field_categories`（基线 9bba900 同失，CONFIG_SCHEMA 类别计数断言，与本批改动无交集）、`tests/tools/test_approval.py::TestLaunchctlGatewayLifecycle`（launchctl 环境类，基线同失）。
- **核销方式**：测试常驻——前端 chat.test.ts tool_id 归属/回退/历史结构化断言在；ChatPage 组件测试 DOM 顺序（compareDocumentPosition）+ 全局弹窗批准→对话卡"已批准"（notifyApprovalResolved 广播）在；后端 test_batch42 的 SSE 归属顺序 + 历史双结构断言在；模型下拉无 tag 文案断言在。季度体检检查：chat:reasoning.tool_id 归属、历史 reasoning.steps 结构、审批卡触发推理摘要行、全局裁决广播订阅。

### 60. 批次四十三 dashboard 重启缺陷收口——自伤/端口单一来源/无头降级（Codex 产出，2026-08-18，dogfood §AX/§BF）

- **范围**：hermes_cli/（dashboard_procs.py / dashboard_service.py / main.py / web_server.py / config_defaults.py）+ 测试。不碰 server 本体（start_server/认证/SSE 路由不变）、批四十四个子命令形态保留（start/stop/restart/status + --stop/--status 兼容）、权限矩阵/拓扑/批四十 redact·askpass 防护零改动。新增配置键 `dashboard.port`（写 config_defaults.py DEFAULT_CONFIG，行为配置走 config.yaml，不新增 env var）。
- **§AX restart 停完没起——SIGTERM 自伤（dogfood §AX）**：根因=`_scan_dashboard_processes` 按 cmdline 前缀匹配 "vigil dashboard"，而 `vigil dashboard restart` 自身进程的 cmdline 也含该前缀 → 把自己 SIGTERM，start 阶段永远执行不到（stop 恰是要退出所以无感，restart 致命）。修法：扫描器内新增 `_is_server_command`——匹配 server patterns 的候选还要按 token 边界排除 lifecycle 子命令形态（dashboard 后紧跟 restart/stop/status/install/uninstall/register/start/help 视为命令进程非 server，永不返回），restart/stop 自身绝不 kill。加防御：`cmd_dashboard_restart` 的 start 阶段包 `try/except SystemExit`——start 失败（端口占用/构建失败/auth 拒绝）时输出明确错误"✗ Dashboard stopped but failed to start during restart. Run `vigil dashboard start --port N` to inspect and retry."并透传非零退出，禁止静默退出（呼应 §AU 静默失败原则）。
- **§BF 端口单一来源——restart 端口不继承（dogfood §BF）**：根因=代码默认 9119 / systemd ExecStart 9120 / config.yaml 无定义三处不一致 → restart 读代码默认 9119 撞 hermes dashboard。修法：`config_defaults.py` 新增 `dashboard.port`（默认 9119）；`dashboard_service._resolve_dashboard_port` 单点解析顺序=显式 `--port` > config `dashboard.port` > 已装 unit 的 ExecStart 端口（`_unit_port` 复用）> 内置默认；`cmd_dashboard` start 路径入口把解析值写回 `args.port`（probe/reexec argv/start_server 全部一致）；`install` 用解析值并把端口持久化到 config `dashboard.port` + unit ExecStart 同时写入（两处一致，restart 继承"上次用的端口"）。config 读写经 dashboard_service 模块级 `load_config/save_config/load_config_readonly` 转发（便于测试 monkeypatch）。
- **§BF start 无头环境 gio 报错（dogfood §BF）**：`_maybe_open_browser` 开浏览器动作收口——无 DISPLAY/WAYLAND 提前返回（原有）；有显示但 `webbrowser.open` 返回 False 或抛异常（gio "Operation not supported"）→ 静默降级打一行"（无图形环境，跳过自动打开浏览器；访问 http://...）"，异常吞掉不外抛，不破坏 server 启动。
- **端点/配置变更清单**：① 新增配置键 `dashboard.port`（config.yaml 行为配置，默认 9119，写 DEFAULT_CONFIG）；② 无端点点位变更、无新 env var、无 server 本体改动。
- **测试**：后端 pytest 全绿。批四十三新增 15 例：`test_batch40_dashboard_lifecycle.py` 增 10——self-exclusion（restart/stop/status cmdline 排除、真 server 保留）、restart start 失败报错文案、restart start 成功无多余报错、`_resolve_dashboard_port` 四场景（显式/config/unit/默认）、install 端口 config+unit 一致；`test_batch43_dashboard_restart_fix.py` 新增 3——无 DISPLAY 不尝试开浏览器、有显示 gio 失败函数不抛、成功路径不破坏。批四十三单文件 3 + 批四十 lifecycle 扩 12 全绿；回归：批三十六 dashboard_service 11 + 批四十 config 五件套 41。预存失败与本批无关：`tests/test_web_server.py` + `tests/hermes_cli/test_web_server.py`（基线 435a3fb 同失 86 例，环境/模拟类，与本批改动无交集——已验证 stash 后基线同数失败）、`test_web_server.py::TestBuildSchemaFromConfig::test_no_single_field_categories`（CONFIG_SCHEMA 类别计数，基线同失）。
- **核销方式**：测试常驻——`_scan_dashboard_processes` 排除 lifecycle 子命令断言在（restart/stop/status 不被当 server）；restart start 失败必有明确报错断言在；`_resolve_dashboard_port` 解析优先级四场景 + install 端口 config/unit 一致断言在；无头降级不抛断言在。季度体检检查：扫描器 `_is_server_command` 排除集在、`dashboard.port` 默认 9119 在、install 持久化端口、restart 失败报错文案。

### 61. 批次四十四 runbook 凭据校验误判收口——语义识别替代词面（Codex 产出，2026-08-18，dogfood §BN/§BL）

- **范围**：tools/runbook_tools.py + tests/tools/test_batch44_runbook_semantic_secret.py。不碰 conversation_loop/审批核心/权限矩阵/拓扑/批四十 redact·askpass 防护；配置走 config.yaml，本批白名单是代码常量不涉及配置键；前端零改动；`<vault:path/field>` 占位符放行行为保持。
- **§BN -p 值形态误判（dogfood §BN）**：原 FLAG_RE 对 `-p\s+(\S+)` 只豁免数字开头值，`mkdir -p /root/x.pem`、`scp -p`、`-i /root/x.pem`、`docker -p 8080:80` 路径/端口/无值 flag 全被当密码，逼 agent 绕词。修法：新增 `_first_command_name(cmd)` 跳过 sudo/env/nohup/time 前缀与前置环境变量赋值取真实命令名；新增 `_PASSWORD_FLAG_COMMANDS` 白名单（sshpass/mysql/mysqldump/mysqlimport/mariadb/mariadb-dump/pg_dump/pg_restore/redis-cli/mongosh/mongodump/sqlplus/sqlcmd/psql）；新增 `_looks_like_attribute(value)`（路径/URL/数字端口/变量/`<vault:`/无值 → 属性放行）；新增 `_is_password_flag(flag, value, cmd)`——`-p` 只在密码命令白名单 AND 值形态是密码候选（非数字/路径/变量/引用的短串）才拦；`-P`（DB 工具大写端口）一律放行；`--password/--token/--api-key` 词面明确，保持严格。FLAG_RE 重写为正则交替组（long-flag 空格/= + `-p`/`-P` 空格/紧贴/= 三形态）。
- **§BL 变量名含 PASS 误判（dogfood §BL）**：`VAULT_PASS=secret/data/...` 变量名 ASCII 含 PASS 被 ASSIGN_RE 大小写不敏感命中。修法：新增 `_assign_value_is_plaintext(value)`——赋值式只在值是**真明文**（纯短串，无 `/`、`$`、`<vault:`、`:` 等引用/路径特征）才拦；指向 secret 的路径/引用（VAULT_PASS=secret/data/CSNDC/maas、$VAR、<vault:>）放行。`PASSWORD=hunter2` 真明文仍拦。
- **§BL 错误信息给正确写法**：新增 `_secret_error_hint(key)`——命中拦截时报"疑似含明文凭据（{key}）。正确写法：优先用受控凭据通道——目标主机经 topo_query 取凭据后经 vssh/sudo_exec 注入，或命令里用 `<vault:path/field>` 占位符…若这只是本地文件路径/端口/连接参数而非凭据，可直接写"。`_scan_commands_for_secrets` 的 step/rollback 两分支统一复用它。错误只报键名不报值——凭据值绝不进错误/日志。
- **安全底线不回退**：`sshpass -p hunter2`、`mysql -psecret`/`mysql -p'secret'`（含紧贴形态）、`PASSWORD=hunter2`、`--password=abc`、`curl -u admin:secret123` 全部仍拦；测试断言 24 值边界（11 放行 + 8 拦截 + hint 2 + 完整路径 5）。
- **测试**：批四十四新增 26 例全绿（`test_batch44_runbook_semantic_secret.py`——`_find_plaintext_secret` 直测 + `runbook_create` 完整路径）；回归：批二十五 create/checkpoint/vault-refs/批四十 gate 49 例全绿；`tests/tools/ -k runbook` 77 例（含 3 skip）全绿。全量 `tests/tools/` 95 失败均为预存失败与本批无关（video/voice/watch/web/zombie 等环境/模拟类；已 stash 基线上手工复现 3 类同失）。
- **核销方式**：测试常驻——`mkdir -p /path`、`scp -p`、`docker -p 端口`、`psql -p 端口`、`-i 私钥路径`、DB 工具 `-P` 大写端口、`VAULT_PASS=secret/data/...` 引用全放行断言在；`sshpass -p`、`mysql -p'pwd'`/`-psecret`、`PASSWORD=hunter2`、`--password=abc` 真明文拦截断言在；错误消息含 topo_query/vssh/`<vault:path/field>` 正确写法 + 值不回显断言在。季度体检检查：`_PASSWORD_FLAG_COMMANDS` 白名单在、`_is_password_flag`/`_assign_value_is_plaintext` 语义判定在、`_secret_error_hint` 正确写法引导在。

### 62. 批次四十五 Overview 连线拓扑总览——整网层级拓扑替换弱展示（Codex 产出，2026-08-18，dogfood §BC/§BD）

- **范围**：纯前端 web/（OverviewPage.tsx / TopologyPage.tsx / TopologyPage.test.tsx + 新增 OverviewPage.test.tsx + 删除 TopologyMiniGraph.tsx）。后端/API 零改动（GET /api/topology 契约不变）；复用 buildGraphModel/TopologyGraph，未复制图模型逻辑；不动 react-flow 依赖（@xyflow/react）；其他 Overview 卡片（Runbook Queue/指标卡）不动；前端零凭据。
- **§BC Overview 弱展示替换（dogfood §BC）**：Overview Topology 卡片弃用只读 SVG 版 TopologyMiniGraph（无连线语义/不可缩放/不可点跳），改用完整交互版 TopologyGraph + buildGraphModel——整网层级连线（集群→主机→服务 smoothstep + cross_host + 关键链路琥珀高亮边）可缩放/平移/点跳。新增 `drawer` state + 复用现有 DetailDrawer 组件（点节点 → 实体详细抽屉），不造端到端抽屉；卡片给足高度（`h-full min-h-[420px]`）。
- **§BD TopologyPage 冗余总览卡片删除（dogfood §BD）**：删掉 TopologyPage 顶部"拓扑总览（琥珀点 = 关键链路服务）"卡片区块（该卡此前渲染与主图分区同一个 TopologyGraph，内容重复）。主图/列表/卡片分区照旧，"关键链路（N 条）"琥珀链段照旧，详情抽屉照旧。移除 TopologyPage 中不再使用的 TopologyGraph import。琥珀/关键链路信息已由主图 buildGraphModel 关键链路琥珀高亮承担，删卡不丢功能。
- **§AZ 图例 + 空态增强（任务 3）**：Overview 卡片头部加一行图例色标（集群/主机/服务/关键链路琥珀）；key_paths 为空时仍显示完整层级拓扑 + 一行提示"未配置关键链路（key_paths），将仅按集群/主机/服务展示"，不因无关键链路而空卡。
- **组件删除**：`web/src/components/TopologyMiniGraph.tsx` 整个删除（grep 确认仅 OverviewPage 引用，TopologyPage 已先在批五十二把 MiniGraph 替换为 TopologyGraph，无其他引用）。
- **测试**：新增 OverviewPage.test.tsx 4 例（整网图渲染+关键链路图例、key_paths 空态提示+仍显示层级、加载失败显示错误、图表例呈现）；TopologyPage.test.tsx 增 2 例（不再渲染冗余卡片、graph/card 模式主图仍完整渲染三层+关键链路）。web/ `npx vitest run` 110 全绿（15 文件）；`npx tsc -b --noEmit` 通过。
- **核销方式**：测试常驻——Overview 渲染整网层级拓扑（集群/主机/服务名在）+ TopologyPage 不再含"拓扑总览（琥珀点 = 关键链路服务）"文案断言在；key_paths 空态提示文案断言在。季度体检检查：Overview 用 TopologyGraph 而非 MiniGraph、TopologyPage 无冗余总览卡、TopologyMiniGraph.tsx 不存在、图例/空态提示在；TopologyPage 主图/详情抽屉/搜索/状态筛选照旧。

### 63. 批次四十六 dashboard restart 端口继承真修——未传/显式三态区分 + restart 真拉起（Codex 产出，2026-08-19，dogfood §BS 第 3 次复现）

- **范围**：hermes_cli/（subcommands/dashboard.py / dashboard_service.py / main.py）+ 测试（test_batch40_dashboard_lifecycle.py 改造 + 新增 test_batch46_dashboard_restart_fix.py）。不碰 server 本体（start_server/认证/SSE 路由不变）、批四十三 `_is_server_command` 自排除照旧（restart/stop/status 仍是 lifecycle 命令形态，永不当 server）、批四十四 runbook/批四十 gate/拓扑/redact·askpass 防护零改动；配置走 config.yaml `dashboard.port`，不新增 env var。批四十三已验收的 install 写 config+unit 两处一致、`--stop`/`--status` 兼容全部保持。
- **§BS 根因确认——"未传 --port" 被 parser 默认值伪装成 "显式 9119"（dogfood §BS）**：restart parser `--port` 默认 `9119`（subcommands/dashboard.py），`cmd_dashboard_restart → cmd_dashboard` 里 `_resolve_dashboard_port(args)` 拿到 9119（非 None）→ `port = int(args_port)` 直接返回 → config/unit 回退永不执行。修法：`--port` 默认值改 `None`（dashboard/serve/start/restart/install 全走同一 `_add_server_runtime_args`），只有用户显式传了才是非 None；`_resolve_dashboard_port` 判定从 `if args_port` 改 `if args_port is not None`（显式 `--port 0` OS 自动分配也原样透传，顺带修掉批四十三引入的 desktop `serve --port 0` 被解析成 9119/config 的回退错）。
- **§BS 第二层坑——schema 默认掩盖 unit 回退**：`config_defaults.py` 的 `dashboard.port: 9119` 经 `load_config` deep-merge 后恒存在，`_resolve_dashboard_port` 读合并 config 时"没写键"也返回 9119 → unit ExecStart（9120）回退仍不执行（§BF 调查记录"config.yaml 无 port 定义"场景）。修法：新增 `dashboard_service._config_dashboard_port()` presence-sensitive 读——raw 文件（`read_raw_config`）只认用户显式写的 `dashboard.port`，无键（或文件缺失）→ None → 继续回退 unit ExecStart → 默认 9119；managed-scope overlay 在叶子层同 `load_config` 顺序生效。解析语义定稿：显式 `--port` > config `dashboard.port`（显式写）> 已装 unit ExecStart 端口 > 默认 9119。
- **§BS 只停不启——restart 改为 spawn 分离 server 子进程**：原实现 restart 的 start 阶段 in-process 跑 `cmd_dashboard`（前台常驻），且该进程 cmdline 是 `dashboard restart`（lifecycle 形态）→ 下次 restart/`vigil dashboard stop` 的扫描永远看不到它 → 第二次 restart 必撞端口失败（连续 restart 验收过不了）。修法：`cmd_dashboard_restart` 停完旧进程后，用解析出的继承端口 spawn 一个**分离**（`start_new_session=True`，日志落 `$VIGIL_HOME/logs/dashboard-restart.log`，同 `_respawn_dashboard_processes` 约定）的 `python -m hermes_cli.main dashboard --port <继承端口> --host <host> --no-open --skip-build` 子进程——子进程是 server 形态 cmdline，后续 restart/stop 能扫到停掉；restart CLI 本体保持 lifecycle 形态（批四十三自排除不破坏）。`--no-open`+`--skip-build` 对齐 install unit 与 update respawn 约定（重启不弹浏览器、不触发意外重建）。
- **§BS 失败提示误导端口——提示端口来自解析结果**：start 阶段失败（端口被占/auth 拒绝等）错误文案 `Run \`vigil dashboard start --port {port}\`` 的 port 由 `_resolve_dashboard_port(args)` 现场解析（继承的那个，如 9120），删掉硬编码 9119 兜底（原 `getattr(args, "port", 9119)` 在 parser 默认改 None 后还会打出 "None"）。成功判定用 server 打印的 `VIGIL_DASHBOARD_READY port=<N>`（`flush=True`，uvicorn 绑上 socket 后才有，desktop 同款信号）而非 TCP 探测——TCP 探测会被占坑第三方应答而误报成功（失败路径实测暴露），READY 行按 spawn 时 log size 起读，上次运行遗留 READY 不计数；子进程自行退出（bind 失败）立即判失败并贴 log 尾部错误详情。
- **端点/配置变更清单**：① parser `--port` 默认 9119 → None（help 文案同步）；② `_resolve_dashboard_port` 判定/回退语义如上；③ 无新增配置键、无新 env var、无 server 本体改动、无端点点位变更。
- **测试**：批四十六新增/改造 40 例全绿——新增 `test_batch46_dashboard_restart_fix.py` 20 例（parser 未传→None/显式传值、`_config_dashboard_port` presence 三态、真实 config+unit 文件解析链三态+显式 9119/0、restart spawn argv 继承 9120 + server 形态、失败提示端口 9120 非 9119、`_wait_for_dashboard_ready` READY 行语义 4 例含第三方占坑不误报/遗留 READY 不计数/子进程退出即失败）；`test_batch40_dashboard_lifecycle.py` 改造 20 例适配 spawn 契约（stop→spawn、abort 不 spawn、失败提示、端口解析四场景+显式 9119/0）。回归：批三十六 dashboard_service 11 + 批四十三 3 + 批四十 lifecycle 全套 + dashboard flags/unified_launch/web_dist_validation/serve/tui_backcompat 全绿（71 例 + 宽 -k 回归 708 过）。预存失败与本批无关（基线 stash 同失，已 diff 核对）：`test_update_stale_dashboard.py::TestCmdlineCapture::test_falls_back_to_ps_without_proc`（hermes→vigil 改名遗留断言）、web_server/param_clamps/auth_prefix 等环境/模拟类。
- **真实环境端到端验证（本批硬门槛，已在本机实测贴输出）**：`vigil dashboard install --port 9120`（本机 9119 已有 hermes dashboard，按任务用 9120 避开）→ `ss -tlnp` 见 systemd 实例绑 9120 + `curl` 200；`vigil dashboard restart`（无 --port）→ 停旧起新，继承 9120 非 9119，`ss -tlnp` 新 PID 绑 9120 + HTTP 200；连续 3 次 restart 均 EXIT=0 + 9120 + 200；失败路径（python http.server 占 9120）→ restart EXIT=1，报错"Run \`vigil dashboard start --port 9120\`"（端口正确），log 见 `ERROR: [Errno 98] address already in use`。验证完清理：E2E dashboard 全部停止、9120 无残留监听、用户原 `vigil-dashboard.service`（v0.1.18/9119）unit 文件原样恢复并 start、用户 `hermes-dashboard.service` 恢复 enabled/active 原状、`~/.vigil/config.yaml` 与备份 diff 一致（零改动）、临时 VIGIL_HOME/脚本全删。
- **核销方式**：测试常驻——parser 未传 `--port` → None 断言在；解析链三态（unit 9120/config 值/默认 9119）+ 显式 9119 就用 9119 断言在；restart spawn argv 带继承端口且不含 restart token（server 形态可被后续 stop 扫到）断言在；失败提示含继承端口断言在；READY 行（非 TCP 探测）判定语义断言在。季度体检检查：`_add_server_runtime_args`/install parser `--port` 默认 None 在、`_config_dashboard_port` presence 读在、restart spawn 分离子进程 + READY 行等待在、失败文案端口来自解析结果在。

### 64. 批次四十七 sudo_exec 三连修复——嵌套 shell 执行形态判定 / && 放行 / 审批门弹窗链路（Codex 产出，2026-08-19，dogfood §BQ/§BR/§BT，用户决策 §BW：sudo_exec 是开源版地基必须修对）

- **范围**：`tools/sudo_tool.py`（命令校验 `_validate_command` + `_sudo_exec_handler` 审批链路）、`tools/approval.py`（新增薄封装 `request_ops_approval`，加段不加逻辑）、`web/src/lib/approvalPoller.ts`（轮询间隔 + 测试）、`tests/tools/test_sudo_exec.py`（边界用例 + 审批门用例改造）、新增 `tests/tools/test_batch47_sudo_approval.py`。审批裁决核心（`_run_approval_gate`/`check_all_command_guards`/权限矩阵/拓扑/conversation_loop）零改动——只新增一个复用既有核心的入口。
- **§BQ 嵌套 shell 检测改判执行形态**：删掉裸路径正则 `(^|[\s/])/?bin/(bash|sh|zsh|dash)(\s|$)`（`useradd -s /bin/bash` 参数值被误判为嵌套 shell）；嵌套 shell 现在只看**执行形态**——① `bash -c`（保留）② 首 token 是 shell（`sh -c …`/`bash script.sh`/交互 shell）③ 管道到 shell（`curl … | sh`）④ heredoc（`<<`/`<<<`）。`useradd/chsh/usermod` 白名单：这些命令的 `-c` 是注释/选项不是 `bash -c`，跳过词面 `bash -c` 规则（执行形态规则不跳过）。验收：`useradd -s /bin/bash netbox` 过、`bash -c`/`sh -c`/`| sh`/heredoc 仍拦。
- **§BR `&&` 放行、`&` 精确拦截**：L98 从裸 `&` 改为 `(?<![&>])&(?![&])`——真正的后台 `&` 仍拦（含 `cmd1&cmd2`），`&&` 顺序连接放行（独立 shell 下拆单条无意义），`>&`/`2>&1` 由 `>` 重定向规则继续拦。报错提示区分操作符并教正确写法（"多步操作可用 && 顺序连接，或写成脚本文件后 sudo_exec 执行脚本"）。验收：`cd X && gunzip Y && docker load` 过、`cmd &` 拦、`>&` 拦。
- **§BT 审批门弹窗链路（根因：审批记录从未落库）**：sudo_exec 的 approve 决策此前直接返回 `require_confirmation` JSON 交回 LLM 转述——web 端永远没有审批记录落 `/api/approvals`，全局审批弹窗轮询不到 pending、用户在聊天里"批准"也不生效（连等两次不放行）。修法：approve 决策改走既有审批门——新增 `tools/approval.request_ops_approval(command, ops_decision)` 薄封装复用 `_run_approval_gate`（与 terminal 的 ops_matrix 审批同一门）：CLI 交互提示 / web 经每线程回调落 `register_web_approval`（弹窗出现）→ 批准 → `wait_web_approval` 返回 → sudo_exec 继续执行；gateway 通知回环或 pending 注册表；无人在场 fail-closed BLOCK。普通 approve 用 `ops_matrix:{grade}:{env}` key（session/永久 allowlist 生效）；prod 变更确认门（require_confirmation）用独立 `ops_confirmation:{grade}:{env}` key 且不提供 session/永久选项——每次都强制人工确认（同 `check_all_command_guards` 的 `_ops_confirmation_required` 语义，allowlist 不能跳过）。
- **§BT 前端**：`web/src/lib/approvalPoller.ts` 轮询间隔 4000ms → 3000ms（验收：审批产生后弹窗 ≤3s 出现；聊天 SSE 另有 `chat:approval_pending` 即时推）。轮询 start/停止条件复核：`useApprovalPolling` 在 App 根挂载时 start、卸载时 stop，路由/会话切换不停止（§BP 同族确认无丢失）。
- **测试**：后端新增/改造 33 例全绿——`test_batch47_sudo_approval.py` 8 例（产生→落库→批准→放行 全链路 mock、拒绝 fail-closed、确认门隐藏 session/永久、非确认门保留、无人在场 BLOCK、`_sudo_exec_handler` 端到端批准后确实执行/拒绝后不执行）；`test_sudo_exec.py` 边界 5 例（useradd/chsh/usermod -s/--shell 放行、执行形态四类仍拦、`&&` 放行、后台 `&`/`>&` 仍拦、值位置 shell 词不误判）+ 审批门用例改造 2 例。回归：sudo 系套件（clarify/熔断/askpass 窄化/ops 确认门/审批注册表/幂等/选项过滤/hardline）330 过（hardline 1 例跨文件预存泄漏与本次无关，见下）、vssh/runtime_state/system_prompt/chat_interrupt/exec_api 96 过；前端 112 全绿（新增 approvalPoller 2 例：默认间隔 ≤3s、stop 后重 start 恢复）+ `tsc --noEmit` ✓。预存失败与本批无关：`test_hardline_blocklist.py::test_sudo_stdin_guard_detects_without_password` 在同进程多文件直跑时被 `test_sudo_clarify_flow.py` 的 vault store 来源标记泄漏影响（基线 stash 同失；规范 per-file 隔离跑全绿）。
- **核销方式**：测试常驻——`_validate_command` 边界用例在（`useradd -s /bin/bash` 过 / 执行形态四类拦 / `&&` 过 / `&`、`>&` 拦）；`request_ops_approval` 落库→批准→放行断言在；确认门 allow_session/allow_permanent=False 断言在；`_sudo_exec_handler` 批准后执行/拒绝后不执行断言在；前端默认轮询间隔 ≤3s + start/stop 断言在。季度体检检查：`tools/sudo_tool.py` 无裸 `&`/`/bin/` 词面拦截（改为执行形态 + `(?<![&>])&(?![&])`）、approve 决策必经 `request_ops_approval`、`web/src/lib/approvalPoller.ts` `POLL_INTERVAL_MS <= 3000` 在。

---

## OPS-DELTA #66：dogfood 三改——Overview 真拓扑图 + chat clarify 交互 + 右下角审批弹窗（2026-08-20）

- **背景（用户 dogfood 原话）**：①"现在overview的拓扑不可缩放 拓扑页的总览鸡肋
  需要再overview页做成真正的拓扑图 可以缩放 有主机和服务间的关系连线 然后可以改变
  集群显示"（澄清："现在的react-flow 拓扑信息都是列表一样排下来 更像是表格不像拓扑"）
  ②"chat ui里面llm想要使用clarify问我问题 但是没有对应的API处理 他没办法用 这个是
  个已知的bug 没修吧" ③"右下角审批弹窗就没做 之前批次做了吗"（补充痛点："比如我在ui
  里面让他干活 然后我切到其他程序 没有弹窗 没有通知 审批又有时限 本质上和我经常没
  给你allow一个意思"）。
- **范围**：`web/src/lib/topologyGraph.ts`（力导向布局引擎）、`web/src/components/
  TopologyGraph.tsx`（集群切换标签栏 + 节点可拖 + 缩放 + 联动高亮）、`web/src/pages/
  TopologyPage.tsx`（图↔表联动）、`hermes_cli/chat_api.py`（web clarify 回调工厂 +
  应答端点 + 中断解挂）、`web/src/lib/chat.ts` + `web/src/pages/ChatPage.tsx`（clarify
  卡片）、`web/src/lib/api.ts`（answerChatClarify）、`web/src/components/ApprovalModal.tsx`
  （右下角形态 + 倒计时）、`web/src/lib/approvalNotification.ts`（新增，桌面通知）、
  `web/package.json`（新增依赖 d3-force ~20KB）、对应前后端测试。**未碰**：审批/权限
  矩阵后端逻辑（注册表、超时策略、命令门控）、clarify 工具定义（tools/clarify_tool.py）、
  /api/topology 数据结构、新增 env var（clarify_timeout 复用既有配置项）。
- **§BW 拓扑力导向布局（任务 1）**：根因=原 topologyGraph 用 GRAPH_LAYOUT 手摆分层
  列排（集群按列、主机/服务分行）——视觉是表格/列表，不是网络拓扑；大拓扑 fitView
  后缩到极小、控件不明显，用户实测"不可缩放"。修法：删除 GRAPH_LAYOUT 常量，新增
  d3-force 力导向引擎 `layoutForceGraph`（forceLink 树形连接 120 + forceManyBody 斥力
  + 按节点类别半径 forceCollide + forceCenter；种子固定的 mulberry32 随机源 → 布局
  跨运行确定可单测；同步跑 240 tick，300 节点上限内可行）；`buildGraphModel` 只产
  节点/连线数据（初始坐标 = 确定性散布，无表格语义）。组件：节点可拖（拖后固定，
  刷新/切集群回到力导向）、fitView 适配 + minZoom 0.1 / maxZoom 2.5、Controls 常显。
  集群切换（前端过滤，不加后端参数）：`clusterOptions` + `filterGraphModel` 纯函数，
  TopologyGraph 图上方标签栏（"全部" + 各集群），切换 → 节点重排 + `key={cluster}`
  重挂载触发 fitView。TopologyPage 总览同引擎 + 图↔表双视图联动：图中点选节点 →
  列表滚动到该行（`topo-row-<name>` + scrollIntoView）；列表点行 → 图中节点高亮
  （focusedName → data.selected → ring 高亮）。DetailDrawer 与琥珀关键链路边保留。
- **§BX web chat clarify 交互（任务 2，修复已知 bug）**：根因=chat_api.py 只有
  `_approval_callback_factory`（328 行），无 clarify 回调接线 → LLM 调 clarify 拿
  "Clarify tool is not available"。修法（仿 approval 的 SSE + 注册表模式，工具定义
  零改动）：新增 `_clarify_callback_factory`——线程内回调登记会话级挂起条目
  `ChatSession.pending_clarify`（_WebClarifyEntry：clarify_id/question/choices/
  multi_select/timeout_at + threading.Event）→ SSE `chat:clarify_pending` → 阻塞等
  应答 → 返回选择串；超时（复用 `clarify_timeout`，config_defaults 已有，不新增配置）
  → 返回 `[user did not respond within Xm]`（agent 自行决定）。新增应答端点 POST
  /api/chat/sessions/{id}/clarify（body {answer: str | str[]}；404 会话不存在 / 409
  no_pending_clarify / 409 clarify_timed_out / 400 缺 answer；200 resolved）。多
  session 并行各自独立（状态挂会话上，无全局表互踩）；中断时 `_cancel_pending_
  clarify_for_session` 解挂（置空应答 + set 事件，阻塞回调立刻返回随 interrupt 收尾，
  不僵尸挂到超时）。安全：answer 不回显、不落日志，敏感答复（sudo 密码等）走批
  三十二既有 redact 机制（clarify_tool 登记 + _redact_text 展示）。前端：clarify 卡
  片只在对话流内呈现（clarify 是"问题"，审批是"命令确认"，不做全局弹窗——对齐批
  三十四边界决策）：问题文本 + choices 单选/多选按钮 + 自由文本 + 提交/取消 + 超时
  倒计时；提交 → POST /clarify → 卡片收起、agent 回合继续；超时 → "已超时，agent
  自行决定"；`chat:done` 收口仍 pending 的卡。
- **§BY 右下角审批弹窗 + 桌面通知 + 倒计时（任务 3）**：根因=批三十四弹窗是居中深色
  遮罩 modal，用户切走看不到、审批有时限 → 命令卡住/超时。修法（只动前端呈现位置，
  审批/权限矩阵后端零改动）：① ApprovalModal 改右下角卡片（Grafana 通知风格：
  fixed bottom-4 right-4、卡片式、队列可堆叠——"还有 N 个审批待处理"）；② 新增
  `web/src/lib/approvalNotification.ts`——审批到达时发系统级通知（标题"Vigil 需要
  审批" + 命令摘要前 80 字符 + 剩余时限），权限首次触发时请求（幂等），拒绝 → 降级
  仅页内弹窗不阻塞审批；点击通知 → 聚焦 dashboard 窗口（弹窗全局常驻，聚焦即见；
  页面打开期间生效，无需 Service Worker）；③ 弹窗 + 通知都带超时倒计时（timeout_at
  推导，≤60s 红色强调，过期显示"审批超时，已终止"）。批三十四安全语义全保留：无
  自动消失（等显式动作）、批准/拒绝/忽略三按钮、队列一次弹一个（approvalQueue 复用）、
  全局可见（App 根挂载）、无 ESC/遮罩点击关闭（根本不再渲染遮罩）。
- **端点/事件变更清单**：新增 POST /api/chat/sessions/{id}/clarify（web chat clarify
  应答）；chat SSE 新增 `chat:clarify_pending` 事件（session_id / question / choices /
  multi_select / timeout_at）；/api/topology 零改动（集群切换纯前端过滤）；approval
  相关端点零改动。
- **测试**：前端 vitest 133 全绿 + `tsc -p . --noEmit` ✓——新增：topologyGraph 力导向
  5 例（自由散布非列排/两两不重叠/种子确定/空图 overflow/集群过滤/集群选项）、
  TopologyGraph 组件 3 例（标签栏 + 缩放控件、集群切换只显示选中集群、点选回调）、
  chat.ts clarify 3 例（clarify_pending 卡片 + markClarifyResolved、clarifyIsTimedOut、
  done 收口）、ChatPage 组件 2 例（卡片渲染 + 选择提交 answerChatClarify、超时显示）、
  approvalNotification 6 例（权限门/正文摘要/触发/拒绝降级/请求幂等/点击聚焦）、
  ApprovalModal 3 例（右下角形态无遮罩、队列堆叠"还有 N 个"、倒计时/超时文案）。
  后端新增 7 例（test_batch49_chat_clarify.py：SSE 事件字段 + 应答等价全链路、多选
  列表、超时 sentinel、多会话独立、端点契约 404/409/400/200、超时 409、answer 不落
  日志 caplog）。回归：chat 系 6 套件 43 过、approval/sudo 系 5 套件 58 过、clarify
  系 4 套件 54 过。
- **核销方式**：测试常驻——力导向布局确定性断言（`layoutForceGraph` 种子固定）、
  集群过滤断言、clarify 全链路（回调 → SSE → 应答 → 回合继续）与端点契约断言、
  answer 不落日志断言、ApprovalModal 右下角形态/队列堆叠/倒计时断言、桌面通知 mock
  断言（触发/权限拒绝降级/点击聚焦）。季度体检检查：`web/src/lib/topologyGraph.ts`
  无 GRAPH_LAYOUT 列排常量（改为 d3-force）、`hermes_cli/chat_api.py` 有 clarify 回调
  工厂 + /clarify 端点、`web/src/components/ApprovalModal.tsx` 右下角形态 + 通知接线。
- **状态**：未 commit（待用户核验后由惯例提交；开发目录 master，基线 9e445cbb）。

---

## OPS-DELTA #67：sudo 远端 scp 双重前缀修复 + dashboard Incidents 接入 watch inbox（2026-08-21）

- **背景（用户 dogfood 原话/实测）**：① sudo_exec 对远端主机（prod）执行、凭据为
  vault 类型时整条通道不可用：`scp: failed to upload file /tmp/vigil-sudo-askpass-XXX.sh
  to wangwp10@10.123.66.233:/tmp/vigil-sudo-YYYY.sh`，真实 stderr `scp: dest open
  "wangwp10@10.123.66.233:/tmp/vigil-sudo-...sh": No such file or directory`——表面像
  认证失败，实际是 scp 把整串带冒号路径当字面量。② alertmanager 接入 vigil watch
  （OPS-DELTA #9，纯配置启用）后 `vigil watch install` 常驻每 300s 拉告警写
  `~/.vigil/watch/inbox/`，但 dashboard `/api/incidents` 是结构占位返回空数组
  （web_server.py:4298-4310 原注释"Vigil 无监控/告警体系"），前端 IncidentsPage 是
  页面壳——告警消费层缺 UI 一环。
- **范围**：`tools/sudo_tool.py`（任务 1）、`hermes_cli/web_server.py` + `web/src/lib/api.ts`
  + `web/src/pages/IncidentsPage.tsx`（任务 2）+ 新增测试（`tests/tools/test_sudo_exec.py`
  回归 2 例、`tests/hermes_cli/test_batch50_incidents.py` 6 例、`web/src/pages/
  IncidentsPage.test.tsx` 2 例）+ 既有占位契约测试改造（`test_batch28_exec_api.py`）。
- **§BY 任务 1 根因（行号实锤）**：`tools/sudo_tool.py` `_run_remote_sudo` 调用 `_scp`
  时 dest 已带 `f"{user}@{host}:{remote_script}"` 前缀（330-331 行），而
  `_scp_argv_from_ssh`（223 行）构造 scp 目标又拼一次 `f"{ssh_argv[-1]}:{dest}"`
  （ssh_argv[-1] 恒为 `user@host`）→ 双重前缀 → scp 把整串当字面量路径 → 远端
  `dest open ... No such file`。`_scp_argv_from_ssh` docstring 写明 dest 应为纯远端
  路径，调用方违反约定。修法：`_run_remote_sudo` 两处 `_scp` 只传纯路径
  `remote_script` / `remote_vault`，`_scp_argv_from_ssh` 不动。全仓 grep 确认
  `_scp(` 调用点仅这两处 + 测试（`tools/environments/ssh.py:177` 是自拼单前缀的独立
  实现，无同类问题）。
- **§BZ 任务 2 后端**：`/api/incidents` 从占位改为读 watch inbox——复用
  `tools/watch_collect.inbox_dir()` / `_iter_inbox()`（不硬编码路径），按
  `alertname|instance` 去重保留最新采集、collected_at 新的在前；条目字段
  alertname/severity/instance/startsAt/state/collected_at/processed/source
  （="alertmanager"）；保持响应 schema `{incidents, total, schema_version, limit,
  offset, has_more}`，schema_version 1→2；inbox 缺失/为空/坏 JSON → 空列表 200 不
  500。processed 只读展示（标记处理是 watch_digest agent 通道的事，本任务不做）。
- **§BZ 任务 2 前端**：`web/src/lib/api.ts` 新增 `IncidentItem` 类型 +
  `IncidentsResponse` 补 limit/offset/has_more；IncidentsPage 渲染告警列表
  （severity 分级样式：critical=红 `--vigil-error` / warning=黄 `--vigil-warn` /
  info=蓝 `--vigil-primary`；字段全展示；processed 显示"已处理"），空态文案"暂无告警"。
- **测试**：后端新增 6 例（test_batch50_incidents.py：读 inbox 字段+去重保最新、
  同键去重、processed 透传、分页 limit/offset/has_more、无/空 inbox 空列表 200、
  坏 JSON 跳过）+ sudo 回归 2 例（mock `_scp` 断言 dest 纯路径无 `@`/`:`、
  `_scp_argv_from_ssh` 纯路径 dest 单前缀）+ 占位契约测试改造（schema_version=2）。
  回归：sudo 系 5 套件 + incidents/exec_api 2 套件共 82 过；前端 vitest 全量 136 过
  （新增 IncidentsPage 2 例）+ `tsc -p . --noEmit` ✓。
- **核销方式**：测试常驻——`test_sudo_exec.py` 纯路径 dest 断言（`@`/`:` 不在 dest）、
  `_scp_argv_from_ssh` 单前缀断言、`test_batch50_incidents.py` inbox 契约断言（字段/
  去重/分页/空态）。季度体检检查：`tools/sudo_tool.py` `_run_remote_sudo` 无
  `f"{user}@{host}:"` 拼接、`hermes_cli/web_server.py` `/api/incidents` 调用
  `watch_collect` inbox 读取且 schema_version>=2、`web/src/pages/IncidentsPage.tsx`
  非空态壳。
- **状态**：未 commit（待用户核验后由惯例提交；开发目录 master，基线 33f0d67c）。

### 65. 批次五十一 模型目录聚合所有已配置 LLM——custom/DS 一键切换（2026-08-22，用户需求：公司 custom + DS 切换不便）

- **为什么**：`/api/models` 目录只列当前 provider（deepseek）静态 2 模型，且创建/切换会话的
  runtime 解析不传 requested provider（恒用 config model.provider）——就算目录塞进 custom
  模型也会拿 deepseek 的 base_url/key 去跑。用户公司场景 custom + DS 并存，切模型要先改
  config 重跑 setup，无法在 UI 切换。
- **怎么改**：
  - `hermes_cli/chat_api.py` `_model_catalog()` 聚合 5 源：① 当前 provider 静态目录 +
    model.default（保留）② custom_providers（config.yaml list，provider 标识
    `custom:<slug>`）③ providers（keyed dict，enabled 才进）④ fallback_providers /
    fallback_model（get_fallback_chain）⑤ auth store 已登录 provider。条目
    `{id, name, provider, description, tag, default}`，id = 模型名（兼容既有调用），
    (provider, model) 去重，default 置顶。
  - 新增 `_catalog_lookup(model, provider)`：provider 空时优先当前 provider 再任意。
  - 创建会话（POST /api/chat/sessions）与切换（POST .../model）body 支持可选
    `provider` 字段，与 model 成对校验；路由改
    `resolve_runtime_provider(requested=provider, target_model=model)`——不传 provider
    保持旧行为（兼容）。
  - 前端：`web/src/lib/api.ts` ChatModelOption 加 provider 字段、createChatSession /
    setChatSessionModel 带 provider；`web/src/pages/ChatPage.tsx` 下拉按 provider 分组
    （optgroup），切换/新建时反查条目 provider 随提交。
- **测试**：后端 batch41 新增 4 例（聚合 5 源断言、显式 provider 路由转发、provider
  不匹配 400、切换带 provider runtime 按 requested 路由）+ 既有 7 个 chat 套件
  stub 签名补 provider 参数；回归 chat 相关 10 套件 65 过。前端 vitest 全量 136 过
  （ChatPage 用例加 provider 断言 + optgroup 分组断言）+ `tsc --noEmit` ✓。
- **核销方式**：测试常驻——`test_batch41_chat_ui_support.py` 聚合断言（custom/
  providers/fallback/auth 各源进目录）、provider 成对校验 400 断言、切换 requested
  路由断言。季度体检检查：`chat_api.py` `_model_catalog` 含 custom_providers 来源、
  `_switch_session_agent_model` 传 requested。
- **状态**：未 commit（与批 50 一并待用户核验提交；开发目录 master）。

### 66. 批次五十二 YAPL P1 数据层——拓扑 schema v0.4 落地（2026-08-23，设计单一事实来源 yapl-design.md §9）

- **为什么**：拓扑 schema v0.3 → v0.4（YAPL v1.0 P1 数据层）：第一层删
  sources/services_index/cross_host/key_paths、clusters/hosts 行补治理字段
  （type/provenance/credentials 数组等）；第二层 hosts/ 改名 services/（服务行删
  env/cluster 冗余、type 枚举化 + managed_by/extra_ports/log_paths/depends_on）；
  第三层 attrs/ops/status → snapshot 二维分支 + checks（不放 type/env/cluster）；
  新增硬件层 hardware/<host>.yaml（静态规格 + controller 枚举）。用户拍板：不做
  v3→v4 迁移（~/.vigil 旧数据直接删除重建）、全部改动在 branch v1.0 一次做完、
  硬件层数据直接 topology discover 产出。
- **怎么改**：
  - `tools/topo_discovery.py`：发现引擎改产 v0.4（host 行 role/runtime 数组 +
    type=host + os + credentials 数组，无 services_index；服务行 type 按 9.7 判定表
    归类 + managed_by + extra_ports/log_paths/depends_on；unidentified 端口/无端口
    systemd 进 pending_review，不入 services）；L3 档案 snapshot（common/by_type/
    by_runtime）+ checks + detail 路径键（OPS-DELTA #42 命名
    entities/{cluster}__{host}__{name}.yaml，修复 v0.4 重写时丢 detail 键导致实体
    文件写偏路径的 bug）；新增硬件层探针（lscpu/meminfo/df/raid/gpu/ip/防火墙，
    controller 枚举）；写盘 services/<host>.yaml + hardware/<host>.yaml + version 4，
    剥离 v0.4 删除的顶层字段，合并语义保留治理字段不覆盖。
  - `tools/topo_schemas.py`（新增）：词表层 schemas.yaml——受控枚举 + unknown 兜底
    （validate_enum/validate_enum_list/managed_by_command/export_schemas_yaml）；
    修复 default_schemas 对 schema_version 标量 list() 崩溃；防火墙探测
    "inactive" 子串误判 active。
  - `tools/topo_tools.py`：读取端 v0.4（services/ 优先、hosts/ 兼容回退；总览不再
    含 cross_host/key_paths；detail 合并 snapshot/checks）；topo_update 字段分流
    （L2 服务行字段 / L3 档案顶层字段 / snapshot.common），无 L2 层实体
    （v0.1 扁平/cross_host）needs_review 落 L3 顶层防更新丢失；topo_status_sync
    status 写 L3。
  - `hermes_cli/subcommands/topo_export.py`：卡片补 managed_by/extra_ports/log_paths/
    depends_on（前端图连线用）；`hermes_cli/ops_init.py` 样例复制 services/ +
    hardware/；`plugins/memory/topo/__init__.py` 第一层渲染 v0.4（clusters type 即
    controller + hosts role/runtime 数组）；`hermes_cli/runtime_state.py` 注释同步。
  - `hermes_cli/ops_samples/`：样例升级 v0.4（topology.yaml version 4 + services/ +
    entities/ snapshot 档案 + hardware/；删除 hosts/ 与 cross_host/key_paths 样例）。
  - `web/`：`topologyGraph.ts` key_paths 琥珀边 → services 层 depends_on 高亮边；
    `TopologyPage`/`OverviewPage` 移除关键链路展示，量规改"服务依赖连线"；
    `api.ts` TopologyCard 补 v0.4 服务行字段。
- **测试**：后端 topo 相关 13 套件 264 过 6 跳（test_topo_discovery 45、
  test_batch39_topo_sync 14、新增 test_topo_v4 16、test_topo_v2 16、ops_init/
  memory/first_install 53、topo_export/status/dashboard 58、runbook 系等）；
  前端 vitest 全量 18 文件 136 过 + `tsc -b --noEmit` ✓。存量无关失败 5 例
  （approval.py `_record_approval_trajectory` 签名冲突 4 例 + watch_tools 1 例，
  未触碰的既有问题，不在本批范围）。
- **核销方式**：测试常驻——test_topo_v4（v0.4 落盘结构/硬件层枚举/snapshot/
  services/ 路径/unknown 兜底）、test_topo_discovery（services/ 落盘 + 实体命名 +
  硬件层）、test_batch39（needs_review L2 / status L3 分流）。季度体检检查：
  `write_discovery` 产 version 4 + services/ + hardware/、`topo_query` 无参总览无
  cross_host/key_paths、ops_samples 无 hosts/ 目录。
- **状态**：已 commit 12327df2（branch v1.0），工作区干净；发布基线 vigil-agent-release 未动。

### 67. 批次五十二验收 + runbook 清理（2026-08-23，主 session 实测验收）

- **验收结论**：P1 数据层通过，可进 P2。实测证据：/api/topology → version 4、2 集群、4 主机、62 服务、detail 带 snapshot+checks、无 cross_host/key_paths；治理字段（owner/department/os/credentials）原样保留、TODO 未被编造；hardware/ 探针产出真实硬件（LAPTOP：AMD Ryzen 7 8840HS/16 核/23GB）；13 套件 pytest 213 passed / 6 skipped / 4 failed（4 failed = 存量 approval 签名 bug，非 P1 引入）；前端 tsc exit 0。
- **runbook 清理**：删除 deploy-gateway-svc.yaml、gateway-svc-restart.yaml（引用样例实体 gateway-svc/order-db，重建后失效；备份 ~/notes/backups/vigil-topo-v3-20260822/runbook-removed/）。剩余 3 个有效：aliyun-kubeadm-topo-register / dsh-web-start / harbor-restart。
- **遗留待办**（非阻塞，排期外）：
  1. compose 粒度：discover 按 compose_project 聚合为项目一条（设计 9.3 要求，现为容器粒度，本机 harbor 拆 8 条）——P4 执行器前补
  2. 服务行 cluster 继承：读取端应从所属 host 继承 cluster（现兜底 default，API 显示服务挂 default 集群）——P2 顺手修
  3. 存量 approval bug：tools/approval.py:4631 `_record_approval_trajectory() got multiple values for 'status'`（ops_permissions_guard 4 例 + watch_tools 1 例失败）——独立批次修

### 68. 批次五十三 YAPL P2 runbook 层——schema v0.2（action 枚举 + 分层校验 + 变量引用）+ ansible 契约化（2026-08-23，设计单一事实来源 yapl-design.md §10/§六）

- **背景**：YAPL v1.0 P2 runbook 层：schema v0.1 → v0.2（命令彻底消失——steps 用
  action 枚举 + params；目标四维声明 env ⊇ cluster ⊇ host_group ⊇ host；LLM 声明
  契约，执行器 P4 生成命令）；ansible-playbook 契约化（堵"用 inventory 绕开拓扑
  表"，§六处理三选一取校验模式）。老 runbook 不迁移（用户拍板：存量 3 个 v0.1
  只读可跑）。
- **怎么改**：
  - `tools/topo_schemas.py` + `~/.vigil/schemas.yaml`：actions 词表 8 → 23 个
    （§10.2 文本逐数为 23 个，标题"24"对不上——以文本为准，差异登记：文本数下来
    生命周期 6 + 主机 2 + 发布 4 + 数据 2 + 配置 1 + 查询 3 + 文件 1 + 执行 1 +
    包 3 = 23；旧枚举 restart_service 更名 restart，v0.1 不消费该词表无兼容影响）。
  - `tools/runbook_tools.py`：`_validate_runbook` 双 schema 分流（_is_v2_runbook：
    steps 含 action → v0.2；含 commands → v0.1；混用报错）；v0.2 三层校验——
    结构（name kebab-case 与文件名一致/version 正整数/kind 含 maintenance/env
    四档/拒绝 permission 字段/triggers 双形态/schedule cron 5/6 段 + IANA 时区/
    triggers 与 schedule 互斥/on_failure 三值语义 + continue 只读动作限定/steps
    非空 + id 唯一/动作 ∈ 词表不兜底/params 按 §10.2 契约校验必填与类型/expect
    通道枚举 + 谓词）、引用（target 存在性 + 类型兼容表：reboot/shutdown 不接
    service、install/upgrade/remove 只接 host/host_group；四维严格嵌套：cluster/
    host_group/host 存在性 + 归属集群在声明 clusters 内 + env 档位一致；无拓扑
    数据时引用层跳过不阻断）、关系（变量引用解析 steps.<id>.params/outputs +
    trigger_context 字段白名单 + 自引用拒绝 + restore/rollback.from 必须引用
    backup 步骤 dest）。runbook_create 扩展 v0.2 字段（clusters/host_groups/hosts/
    schedule/on_failure），描述注入 v0.2 语法说明；v0.2 写 version: 2；checkpoint
    遇 v0.2 返回"执行器 P4 实现，当前仅可创建/校验/预览"；load 对 v0.2 全层校验。
  - `tools/ansible_inventory_guard.py`（新增）：ansible/ansible-playbook 命令含
    `-i <inventory>` → 解析 inventory 主机集合（ini/yaml）→ 与拓扑表主机比对
    （name 或 endpoint）→ 有缺失 = 拒绝列主机 + 引导补拓扑/改用拓扑表主机；
    fail-closed（文件读不到/格式不识别/拓扑表读不到一律拒绝）。接线：approval.
    check_all_command_guards（terminal 全路径，无条件硬拦先于 yolo/allowlist）+
    sudo_exec。P2 过渡期边界：只拦显式 -i，默认 inventory（/etc/ansible/hosts）
    留 P5 操作分类层。
  - `hermes_cli/subcommands/topo_export.py`：P1 遗留待办 #2——build_view 服务行
    cluster 从所属 host 继承（v0.4 服务行不冗余 cluster，显式 cluster 仍优先）。
  - `web/`：RunbooksPage v0.1/v0.2 双形态渲染（动作徽标 + params + expect 通道/
    谓词 + on_failure 标签 + schedule + 目标四维范围 + 双形态 triggers + 场景化
    rollback）；ops.ts yamlPreview 修数组对象缩进（i>0 项缺 `- `、嵌套深一级），
    v0.2 结构序列化正确。
- **测试**：新增 test_runbook_v2 41 例（结构/引用/关系/双 schema 兼容）、
  test_ansible_inventory 16 例（正常/绕行/混合/解析失败 fail-closed/守卫接线）、
  test_topo_v4 补 build_view cluster 继承；相关 24 套件 390 过 6 跳、approval 系
  另 233 过；前端 vitest 19 文件 140 过 + `tsc -b --noEmit` ✓。失败 9 例全为
  存量基线问题（git stash 到 P1 基线复现：approval.py `_record_approval_trajectory`
  签名冲突 4 + launchctl 检测 1 + approval_plugin_hooks 4），非本批引入。
- **核销方式**：测试常驻——test_runbook_v2（v0.2 分层校验/双 schema/变量引用）、
  test_ansible_inventory（inventory ⊆ 拓扑表 + fail-closed）。季度体检检查：
  `_validate_runbook` 对 v0.2 报 permission 拒绝、`check_all_command_guards` 对
  `ansible -i <含拓扑外主机>` 返回 ansible_inventory 硬拦、build_view 服务行
  cluster ≠ default（继承所属 host）。
- **实测**（9131，pid 2659062，新代码重启）：`runbook_create` 落盘
  nginx-config-update.yaml（version 2, kind maintenance，3 步骤 action 枚举 +
  rollback 场景 + 变量引用）→ `runbook_load` 全层校验通过（note 标明 v0.2 仅
  可创建/校验/预览）→ `runbook_checkpoint` 返回"v0.2 执行器在 P4 实现"；未知动作
  frobnicate 报错列合法动作；permission 字段拒绝；存量 3 个 v0.1 runbook load
  全过；ansible 拦截：good inventory（LAPTOP-T2JA2ERE/8.140.60.44）放行、含
  rogue-company-host 拒绝列缺失主机、check_all_command_guards 硬拦
  （approved=False, ansible_inventory=True）、垃圾文件 fail-closed；/api/runbooks
  返回 4 条含 v0.2；/api/topology 服务行 cluster 已继承（nginx→local、
  kubelet→beijing_aliyun，不再 default）。
- **状态**：待 commit（branch v1.0，工作区仅本批 + #67 验收记录未提交）。

### 69. 批次五十四 YAPL P3 操作矩阵层——matrix.yaml + 四模板 + CLI/UI 管理 + matrix_query 只读 + 资产审批 + 审计 + 热生效（2026-08-23，设计单一事实来源 yapl-design.md §11）

- **背景**：YAPL v1.0 P3 操作矩阵层（§11.7 定案全量）：权限 = 操作矩阵唯一裁决
  （runbook 每步执行时查；terminal 直跑 P5 操作分类层后同表）。矩阵独立
  `~/.vigil/matrix.yaml`（安全资产——独立防误改/备份/diff 清晰），LLM 只有
  `matrix_query` 只读工具，修改一律人工通道（CLI 四命令 + UI 表格页），修改即
  审计，热生效（读取路径实时读文件不缓存）。P4 执行器接线矩阵 + 执行豁免 =
  排期外（本批只做数据模型）。
- **怎么改**：
  - `tools/matrix_data.py`（新增，核心数据层）：加载/校验（结构 / 动作枚举 ∈
    schemas.yaml 23 个 / 档位枚举 execute/approve/{approve: required} / **无
    deny 语义**——出现 deny/denied/block 等值一律报错 / 漏配默认 approve+warning /
    其余 dict 形态拒绝）；热生效（load_matrix 每次实时读文件，无缓存；文件缺失 →
    空矩阵全部默认 approve + warning）；四模板生成（模板 1 单人 local / 模板 2
    小团队 local·dev·prod / 模板 3 中型 local·uat·dev·prod，逐格核对 §11.3；模板 4
    三步级联 selections）；每格来源追踪（sources 平行结构：模板名 or manual；
    CLI/UI 改单格 → 该格 manual + 顶层 source manual；edit 批量按 diff 标记；
    reset 按 base_template 回退模板）；原子落盘（临时文件 + os.replace，落盘前对
    **序列化后文档**全量校验）；`matrix edit` 编辑器支持（$EDITOR + 保存时校验，
    content 注入供测试）。
  - `tools/matrix_tools.py`（新增）：`matrix_query` 只读 tool（action 必填枚举
    校验、env 必填、查不到返回默认 approve + 提示漏配；描述明确"修改矩阵 = 人工
    操作（vigil matrix CLI / UI），LLM 无 set 路径"；实现零写接口）；注册
    toolset=matrix，check_fn 恒可用（矩阵缺失也查——缺失=默认 approve）。
  - `toolsets.py` + `hermes_cli/config_defaults.py` + `config_migrations.py` +
    `tools_config.py`：matrix toolset 定义 + cli 平台默认启用（默认列表
    hermes-cli/topo/runbook/matrix）。
  - `hermes_cli/subcommands/matrix.py`（新增）+ `hermes_cli/main.py`：`vigil
    matrix init [--template 1-4] [--force] / show [--json] / set <action> <env>
    <level> / edit / reset`；show 之外四命令均过审计（record_event type=matrix_
    change，session=matrix-cli，meta 含 env/action/old/new/source=cli/operator，
    meta 存原始档位值不含凭据）。
  - `tools/approval.py`：新增 `request_asset_approval`——资产审批门（§11.4 双审批
    层次），复用 `_run_approval_gate`（CLI 交互 / web 注册表 / gateway 回环 /
    无人在场 fail-closed BLOCK 永不无人落盘）；approvals.mode 决定方式
    （off 且非强制人工跳过 / smart 且矩阵判全部动作 execute 级自动批准——
    确定性智能，矩阵即风险裁决不引入 aux LLM / 其余人工门）；**{approve:
    required} 强制人工覆盖 approvals.mode**（off/smart 也必走人工，且不提供
    session/永久 allowlist——只 once/deny，同 prod 变更确认门）。
  - `tools/runbook_tools.py`：v0.2 runbook_create 创建/变更过资产审批门（复用 P2
    三层校验后、落盘前；矩阵对应 env 任一动作 required → force_manual）；审批
    通过落盘带预审标记 `approved_at / approved_by / approved_version`（内容哈希，
    P4 执行豁免数据模型）；审批失败不落盘 + 报错引导；**v0.1 保留原路径**
    （无资产审批，过渡期注明）。
  - `hermes_cli/web_server.py`：`GET /api/matrix`（全量 + 来源 + actions + 漏配
    警告）、`PUT /api/matrix`（单格改 + 审计 source=ui）、`POST /api/matrix/init`
    （四模板生成 + 模板 4 selections，已存在防误覆盖）；全部 `_require_token`
    保护，**不进 PUBLIC_API_PATHS**。
  - `web/`：`MatrixPage.tsx`（行=动作 23、列=env、格=三态下拉 + 每格来源展示 +
    漏配警告横幅 + 顶部「矩阵是人工安全资产，修改即审计」说明 + 空态四模板引导
    + 模板 4 三步级联 modal（execute 集 → 剩余选 approve 集 → 其余 required））；
    `App.tsx` 路由 /matrix + 导航；`api.ts` getMatrix/setMatrixCell/initMatrix。
- **测试**：新增 tests/tools/test_matrix.py 34 例（加载/校验/无 deny/枚举/漏配/
    形态/热生效/模板 1·2·3 逐格断言/模板 4 级联/CLI init-show-set-reset-edit/
    审计事件/matrix_query 只读+缺省/matrix toolset 注册/资产审批通过-拒绝-强制
    人工-smart-off-fail-closed-v0.1 不受影响/web 端点 401+PUT+init+不进 public
    paths）；test_runbook_v2 fixture 补 approvals.mode=off（schema 测试绕过资产
    审批门，审批语义由 test_matrix 专测）；相关 18 套件 350 过（存量基线失败
    4 例：launchctl 1 + batch28 跨文件污染 3——git stash 到 P2 基线复现，非本批
    引入）；前端 vitest 20 文件 144 过 + `tsc -b --noEmit` ✓ + `npm run build`
    ✓（web_dist 已重建，9131 实测用）。
- **核销方式**：测试常驻——test_matrix（无 deny 拒绝/模板逐格/reset 回退/
    matrix_query 只读/资产审批两路径 + v0.1 不受影响）。季度体检检查：matrix.yaml
    sources 平行结构来源追踪、`vigil matrix set` 后该格 manual、runbook v0.2 落盘
    带 approved_* 三标记、`matrix_query` 对未配置动作返回默认 approve + 提示。
- **实测**（9131，pid 2661951，新代码重启）：
  - `vigil matrix init --template 2` → 落盘 source=template2、prod.restart=
    {approve: required}；`show` 表格 + 每格来源；`set restart prod execute` →
    该格 sources 变 manual、顶层 source=manual、`show` 标 ·m；
    `matrix_query(restart, prod)` 返回 execute + 来源 manual；
    改文件（restart 改回 required）不重启直接 matrix_query 返回 required（热生效）；
    `vigil trajectory show matrix-cli` 可见 3 条 matrix_change 审计
    （init/set/reset，meta 含 old/new/source=cli/operator）；
    dashboard /api/matrix GET 200 + PUT 单格 200（来源 manual + 审计 matrix-ui）；
  - 资产审批：runbook_create v0.2（prod.restart 高危）→ 审批弹窗只 [o]nce/[d]eny
    （强制人工）→ 批准落盘带 approved_at/approved_by/approved_version；
    拒绝 → 不落盘 + 报错引导；approvals.mode=smart + 全 execute 动作 → 自动批准
    落盘（approved_by=smart(matrix=all-execute)）；v0.1 runbook 创建无审批无标记。
- **状态**：待 commit（branch v1.0，工作区仅本批改动）；发布基线 vigil-agent-release 未动。

### 70. 批次五十五 YAPL P4 执行器·阶段 1——执行引擎骨架（runbook_execute + 步骤流程 + 审批门接矩阵 + 执行记录）（2026-08-23，设计单一事实来源 yapl-design.md §10/§11）

- **背景**：P4 执行器层阶段 1：v0.2 runbook 从"仅可创建/校验/预览"到可执行。
  命令由执行器生成（LLM 永不接触命令语法）；权限 = 操作矩阵唯一裁决（每步执行
  时查 matrix，runbook 无 permission 字段）；`{approve: required}` 强制人工
  （覆盖 approvals.mode，无 allowlist 绕过）。
- **怎么改**：
  - `tools/runbook_exec.py`（新增，执行引擎）：`runbook_execute` 工具 +
    `execute_runbook` 引擎核心。每步流程 = 变量替换 → target 解析 → 审批门 →
    handler 生成命令 → 现有执行通道 → expect 检查 → 执行记录；
    - **变量替换**（§10.5）：`{{ steps.<id>.params.<key> }}`（从已执行步骤的
      实际 params 取值）/ `{{ steps.<id>.outputs.<key> }}`（exit_code/stdout/
      stderr）/ `{{ trigger_context.<field> }}`；步骤未执行/不存在/字段缺失 =
      报错停；
    - **target 解析**（§10.2）：service / host / host_group / cluster 多态 →
      topo_tools 实体 → managed_by / endpoint / os / container / compose
      project/namespace / 本地远端判定（endpoint ∈ 本机网卡 = local）；不在
      拓扑表 = 拒绝（前置校验层已先拦，双保险）；
    - **审批门**（§11.1/11.5）：矩阵 execute → 直接执行；approve → 交互审批
      （复用 request_ops_approval，CLI 提示 / web 注册表 / gateway 回环，
      无人在场 fail-closed）；required → 强制人工（require_confirmation 语义，
      不提供 allowlist）；定时执行豁免（见阶段 4 前置逻辑：预审标记 + 内容哈希
      漂移检查，本批已实现 `_check_scheduled_exemption`）；
    - **on_failure**（§10.4）：stop（默认）/ continue（只读）/ rollback /
      {rollback: 场景名}；rollback 后终止；rollback 失败 → 强制 stop 人工介入；
    - **expect**（§10.3）：`generate_expect_check` 确定性生成检查命令
      （http/docker/systemctl/kubectl/pm2/process/port 通道）+ 谓词断言
      （http_status / body_contains / contains / exit_code）；
    - **执行记录**（事后审计数据模型）：`~/.vigil/runtime/runbook_executions.jsonl`
      JSONL 追加（上限 500 行），stdout/stderr/error 截断 + 强制脱敏
      （redact + 补充赋值式凭据掩码——实测 redact 对句内引号形态漏网）；
    - 工具注册 toolset=runbook，check_fn 同 runbook 系（数据存在性门控）。
  - `tools/runbook_tools.py`：`runbook_checkpoint` 的 v0.2 分支提示改为指向
    `runbook_execute`（checkpoint 是 v0.1 checklist 阶段门，不混用）。
- **测试**：tests/tools/test_runbook_exec.py 引擎部分（变量替换跨步骤/trigger_
  context/缺失报错、target 多态/不存在拒绝、审批三态、on_failure 四形态 +
  rollback 终止 + rollback 失败强制 stop、expect 通过/失败、定时豁免未预审
  拒绝/预审放行/哈希漂移拒绝、执行记录脱敏断言）——17 例。
- **核销方式**：测试常驻——test_runbook_exec.py 引擎用例；季度体检：
  执行一次 v0.2 runbook 后 `runtime/runbook_executions.jsonl` 出现记录且无
  凭据明文；矩阵 required 动作执行被强制人工拦截。
- **状态**：与阶段 2 同批 commit（工作区一并）。

### 71. 批次五十六 YAPL P4 执行器·阶段 2——23 动作 handler 命令生成表（2026-08-23，设计单一事实来源 yapl-design.md §10.2）

- **背景**：P4 执行器层阶段 2：动作 → 确定性命令生成（handler 唯一命令来源），
  未覆盖组合报错引导不猜命令。
- **怎么改**：
  - `tools/runbook_handlers.py`（新增，命令生成器）：`generate_commands(action,
    params, target)` → CommandSpec 列表（argv 零 shell / cmd+shell / sudo /
    transfer / script_asset 五形态）；分派 = action × target 类型 × managed_by；
    - 生命周期 start/stop/restart/reload/enable/disable：systemd（sudo）、
      docker、docker_compose（-p project）、kubectl（rollout restart /
      scale replicas）、pm2 五通道；
    - 主机族 reboot/shutdown：systemctl reboot/poweroff（sudo）；
    - 发布族 deploy/rollback/scale/decommission：kubectl（set image + rollout
      status / rollout undo / scale / delete）与 docker_compose（up -d
      --force-recreate / rm -sf）通道；单容器 docker 发布 = 未覆盖报错引导
      （先纳入 compose）；
    - 数据族 backup/restore：docker cp 容器路径（src 推导：params.src →
      snapshot config/data 目录 → /etc/<name>(gateway/web) / /var/lib/<name>）；
      主机 tar；
    - 配置族 apply_config：nginx 容器 sed -E 行翻转（key 取末段，value 标量
      校验）与 kubectl configmap patch（argv 零 shell）通道；
    - 查询族 query/fetch_log/verify：多通道（docker/systemd/kubectl/pm2），
      pattern/grep 用 shlex.quote + `|| true`（grep 无命中不误判失败）；
    - 文件 transfer_file：三/四形态（本→本 cp、本→远/远→本 scp、远→远直传）
      返回 transfer spec 由执行器走 ssh argv 通道；
    - 执行族 run_script：只引用资产（script_asset spec，执行器解析
      ~/.vigil/scripts/ + 预审标记校验）；
    - 包族 install/upgrade/remove：apt（Ubuntu/Debian）与 dnf（Rocky/CentOS/
      RHEL）按 target.os 判定，version/repo/deps 语义；
    - `generate_expect_check` / `evaluate_expect`：expect 通道确定性生成 +
      谓词断言（§10.3）。
  - 安全：参数一律 shlex.quote 进 shell 形态；argv 形态零 shell；命令不含
    凭据（远端经 ssh argv 注入）；生成命令不引入注入面（value 标量校验等）。
- **测试**：tests/tools/test_runbook_exec.py 命令生成表部分（restart×5 通道、
  enable/disable k8s、reboot/shutdown、query/fetch_log/verify 多通道、
  apply_config nginx sed + k8s configmap、backup/restore、包族 os 分派、
  transfer/script spec、未覆盖组合报错、expect 通道枚举 + 断言）——22 例。
- **核销方式**：测试常驻——命令生成表用例；季度体检：新增 managed_by 时
  handler 同步（schemas.yaml 注释已注明"managed_by 加值贵"）。
- **状态**：与阶段 1 同批 commit（工作区一并）。

### 72. 批次五十七 YAPL P4 执行器·阶段 3——脚本资产库 + run_script 受控执行（2026-08-23，设计单一事实来源 yapl-design.md §10.9/§11.3/§11.4）

- **背景**：逃生舱受控——run_script 只引用资产库脚本（不内联）。脚本资产创建
  /变更走内容审批（tirith 扫描 + 资产审批），交互执行 execute（11.3 特例：
  资产已预审）；引用不存在脚本 = 报错引导创建。install/upgrade/remove 包族
  handler 与 rollback 场景引用已在阶段 1/2 完成（本批验收）。
- **怎么改**：
  - `tools/script_assets.py`（新增）：`script_asset_create`（名称 kebab-case
    防穿越 / 内容非空+上限 / tirith 扫描 block→拒绝、warn→强制人工 / 资产审批
    门复用 P3 request_asset_approval，**smart_low_risk=False**——脚本内容本身
    是风险裁决，不走矩阵式 smart 自动批准；approvals.mode=off 且非 warn 跳过；
    fail-closed 永不无人落盘 / 落盘 0700 脚本 + `.meta/<name>.json` 预审标记
    approved_at/approved_by/approved_version=内容哈希 / 覆盖需 overwrite=true
    重新审批）+ `script_asset_list`（只读列出 + 审批状态）；
  - `tools/runbook_exec.py`：`_exec_script_asset` 复用 script_assets 的
    resolve/read_meta（消除重复）：引用不存在 → 报错引导 script_asset_create；
    无审批标记 / 缺 approved_* / 内容哈希漂移 → 拒绝执行（豁免失效）；通过 →
    `bash <path> <args>` 执行。
  - 包族 handler（阶段 2 已建）：apt/dnf 按 target.os 判定、remove deps 默认
    false 保守——本批测试覆盖。
- **测试**：tests/tools/test_script_assets.py 12 例（创建审批通过落盘带三标记 /
  拒绝不落盘 / mode=off 跳过 / 名称+内容校验 / tirith block 拒绝 / overwrite
  语义 / list / run_script 缺资产引导 / 无标记拒绝 / 哈希漂移拒绝 / 正常执行
  argv 捕获 / resolve 路径 helper）；包族与 rollback 场景由阶段 1/2 用例覆盖。
- **核销方式**：测试常驻——test_script_assets.py；季度体检：scripts/.meta 标记
  与内容哈希一致、run_script 引用不存在资产时报错引导而非内联。
- **状态**：与阶段 4/5 后续批次同链（本批独立可验收，随阶段 4 提交或独立 commit）。

### 73. 批次五十八 YAPL P4 执行器·阶段 4——调度器接线（schedule 注册 + 定时豁免 + 事后审计 + cron_gen 工具）（2026-08-23，设计单一事实来源 yapl-design.md §10.7/§11.4）

- **背景**：P4 执行器层阶段 4：runbook schedule（cron + timezone）注册到现有
  cron 调度器（不新造轮子）；定时触发 = 资产审批豁免（预审 runbook 跳过逐次
  审批，不弹窗）+ 执行后记录 + 通知（事后审计，无人值守矛盾解法）；未预审
  runbook 定时执行 → 拒绝并提示先过资产审批。cron_gen 工具承接 §10.7
  "LLM 禁止手算 cron"——自然语言 → cron 确定性转换。
- **怎么改**：
  - `cron/jobs.py`：`create_job` 新增 `runbook` 参数（kebab-case 校验、
    与 no_agent 互斥、job 字典落 `runbook` 标记 + 标签源）；runbook job 是
    确定性 tick（无 LLM），prompt 可为空。
  - `cron/scheduler.py`：`run_job` 在 no_agent 分支后、LLM 路径前插 runbook
    分支——`_load_runbook` 缺失 → 失败通知；存在 → `execute_runbook(
    scheduled=True)` 带 trigger_context（source=schedule、schedule.cron/
    timezone 取实际触发的 job 调度，runbook 内声明式 schedule 兜底）；结果
    摘要 + 每步状态 → 通知（事后审计）；ok/rolled_back 为成功。
  - `tools/runbook_schedule.py`（新增）：`register_runbook_schedule`（幂等
    注册/更新/注销，`use_cron_store` 隔离 VIGIL_HOME，job 名 `runbook:<名>`）
    + `runbook_schedule_status`（只读查询）。runbook_create 落盘后同步调用
    （失败仅日志，不阻断落盘）。
  - `tools/cron_gen.py`（新增，toolset=runbook）：自然语言 → cron 确定性
    转换（每 N 分钟/小时/天、每小时、每天 [时段词] H 点 [M 分] 与 HH:MM、
    每周[星期X]、每月 D 日、每季度；中文/阿拉伯数字）+ 已给 cron 校验回显 +
    tz IANA 验证与当前墙钟；未匹配/非法 → 报错引导，绝不猜测手算。时段词
    语义：凌晨/早上/上午原值、中午→12、下午/晚上 +12（12 点不变）。
  - 修复：工具发现机制只认模块顶层 `registry.register`——`runbook_exec.py` /
    `script_assets.py` / `cron_gen.py` 原用 `_register()` 包装会被 AST 扫描
    跳过（CLI 运行时 `runbook_execute`/`script_asset_*`/`cron_gen` 全部缺失，
    阶段 1-3 埋的接线 bug），统一改为顶层注册。
- **接线点（OPS-DELTA 注明）**：cron job schedule 由 `cron.jobs.parse_schedule`
  解析（croniter），ticker 按 hermes 配置时区推进；runbook.schedule.timezone
  是权威墙钟语义（校验 + 展示 + 执行记录），调度推进时区差异登记在案——hermes
  配置时区 ≠ runbook timezone 时以 hermes 配置时区为准（ticker 机制不双跑）。
- **测试**：tests/tools/test_runbook_schedule.py 23 例（cron_gen 13 组短语→
  cron 全对 + 非法引导 + tz helper；注册/更新同步/注销/无 schedule noop；
  定时触发预审豁免执行 + trigger_context 注入 + 执行记录 source=schedule +
  未预审拒绝 + runbook 缺失报错 + 矩阵 required 下定时执行零审批回调）。
- **核销方式**：测试常驻——test_runbook_schedule.py；季度体检：
  `runbooks/<名>.yaml` 带 schedule 时 `vigil cron` 出现 `runbook:<名>` job，
  触发后 `runtime/runbook_executions.jsonl` 记录 source=schedule；cron_gen
  未匹配输入返回引导而非猜测。
- **状态**：阶段 4 独立 commit（阶段 5 前端/实测随后）。

### 74. 批次五十九 YAPL P4 执行器·阶段 5——前端执行入口 + 执行历史 + 9131 实测收尾（2026-08-23，设计单一事实来源 yapl-design.md §10.2/§11.1）

- **背景**：P4 执行器层阶段 5（收尾）：RunbooksPage v0.2 runbook 加"执行"
  入口（确认 → 跑 → 每步状态展示）+ 执行历史列表（最近 N 次：runbook/时间/
  来源/结果）；后端执行/历史端点；9131 实测全链路（交互执行/失败回滚/定时
  豁免/资产审批）并修掉实测暴露的执行器 bug。v0.1 runbook 不显示执行按钮
  （老路径照旧）。
- **怎么改**：
  - `hermes_cli/web_server.py`：`POST /api/runbook/executions`（body
    name/env/trigger_context → `execute_runbook(scheduled=False)`；交互上下
    文 + 每线程 web 审批回调——矩阵 approve/required 步骤落 web 审批注册表 +
    右下角弹窗，execute 直跑；v0.1 拒绝 400；name 白名单防穿越；不进
    PUBLIC_API_PATHS）+ `GET /api/runbook/executions`（`recent_executions`
    只读，事后审计视图）。
  - `web/src/lib/api.ts`：`runRunbook` / `getRunbookExecutions` + 执行结果/
    历史类型。
  - `web/src/pages/RunbooksPage.tsx`：v0.2 详情头"执行"按钮 → 确认弹窗 →
    POST → 执行结果卡（终态徽标 + 每步状态/命令/回滚块）+ 执行历史表
    （runbook/时间/来源/结果，可刷新）。
  - **实测发现并修复（执行器阶段 1/2 埋的 4 个 bug，本批一并收口）**：
    ① `runbook_exec._local_host_names` 只比对 hostname/DNS 名——本机服务
    endpoint 是网卡 IP（172.18.120.67，eth0）时误判 remote 走 SSH 报
    Host key verification failed；改为 ioctl SIOCGIFADDR 枚举全部本地网卡
    IPv4 + 默认路由兜底（Linux/WSL；其他平台回退 DNS 名）。
    ② backup handler：dest 父目录不存在时 docker cp 报
    "invalid output path"——docker 通道先 `mkdir -p <parent>`（shell 形态，
    无层级 dest 保持纯 argv）。
    ③ restore handler：docker cp 目录到已存在路径会把 src 塞成 dst 子目录
    （复原语义错误）——改 `docker cp <src>/. <container>:<dst>/` 内容复原。
    ④ `resolve_target` 的 compose_service 取实体 `docker_compose.services
    [].name`（容器名 docker-nginx-1）——compose restart/pull 报 no such
    service；改为执行时取容器 label `com.docker.compose.service`（nginx），
    docker 不可用回退存量名。
- **测试**：后端 `tests/hermes_cli/test_batch59_runbook_exec_api.py` 8 例
  （历史空/有记录、POST 缺名/坏名/404、v0.1 拒绝且 execute_runbook 不调、
  接线参数断言、矩阵 required → web 审批注册 → 批准 → 执行记录落盘）；
  执行器回归补 `test_local_endpoint_matches_interface_ip`（网卡 IP 判 local、
  非本机判 remote）+ backup/restore 命令形态更新；前端 RunbooksPage 3 例新增
  （v0.2 执行按钮+确认+结果分步、v0.1 无按钮、历史列表渲染）。vitest 20 文件
  147 过、tsc -b --noEmit 过、npm run build 过。
- **9131 实测记录**（VIGIL_HOME=/home/wpwang/.vigil + load_hermes_dotenv 重启）：
  - `nginx-config-update` 交互执行：local 档全 execute 直跑（无审批卡）→
    backup `docker cp docker-nginx-1:/etc/nginx …`（mkdir 修复后父目录自动
    建）→ 本机 docker-nginx-1 处于 restarting（8/22 起既存崩溃，非本次引入）
    → apply `docker exec sed` exit 1 → on_failure rollback 触发 → rb-restore
    ok + rb-reload `docker compose -p docker restart nginx` ok → 终态
    `rolled_back`（compose_service label 修复前此处报 no such service）。
  - schedule：预审 runbook（*/2 缩短 cron）`register_runbook_schedule` →
    cron job `runbook:sched-demo` → `run_job` 触发 → 豁免执行（无审批回调）、
    ledger source=schedule、trigger_context.schedule.cron/timezone 注入、
    通知 doc 每步状态；未预审 runbook → `blocked` + 提示先过资产审批。
  - run_script：`script_asset_create`（tirith allow + 内容审批 + 预审标记
    落盘）→ 9131 POST 执行 runbook → `bash <资产>` exit 0 stdout
    demo-asset-ok。
  - 执行历史 `GET /api/runbook/executions` 返回 6 条（source=user/schedule）。
  - 实测后清理：临时 runbook ×4、脚本资产 ×1、备份目录、sched-demo cron
    job 全部移除，存量 runbooks/（4 个）未动。
- **核销方式**：测试常驻——test_batch59_runbook_exec_api.py + 前端
  RunbooksPage.test.tsx；季度体检：RunbooksPage v0.2 有执行按钮/历史表、
  `/api/runbook/executions` 两个端点存在、执行记录 source 区分 user/schedule。
- **状态**：阶段 5 独立 commit（P4 全 5 阶段收官；OPS-DELTA #70-#74）。

### 75. 批次六十 YAPL P5 操作分类层——action classifier（terminal 收口）+ 双矩阵统一 + L1-L4 分级退役（2026-08-23，设计单一事实来源 yapl-design.md §11.1/§11.2/§11.5/§11.6 + §八待办）

- **背景**：YAPL v1.0 最后一块（P5）。P3 操作矩阵（action × env）与 P4 执行器
  已就位，但 terminal 直跑仍走 L1-L4 命令分级（`_DEFAULT_GRADES` 正则表 →
  grade × env → execute/approve/deny），与 runbook 路径（同一矩阵）两套语义。
  P5 收口：terminal 命令先过**操作分类层**（规则表优先的命令/意图 → 动作枚举
  23 个），再按动作 × env 查**同一 matrix.yaml**（双矩阵统一）；L1-L4 分级
  退役，判定对象从"命令正则分级"换成"动作枚举"。§八待办（"terminal 检测
  ansible/ssh/scp 类运维命令 → 提示走受控通道"）一并收口为 classifier 规则。
- **怎么改**：
  - `tools/action_classifier.py`（新）：`classify_command(command)` →
    `{action, rule, note, chain}`。内置规则表（docker/kubectl/systemctl/service/
    apt/dnf/yum/pip/pm2/helm/ansible/curl/wget/备份恢复族/reboot/shutdown，
    长模式先：docker compose 先于 docker、kubectl rollout 先于 kubectl）；
    受控通道规则（ssh/scp/rsync/sftp）在规则表**最前**——ssh 串内嵌
    systemctl/docker 时受控通道语义优先（action=run_script/transfer_file +
    note"走 vssh/拓扑凭据受控通道"）。管道不干扰（`docker ps | grep harbor` →
    主命令 docker ps → query，词面绕过消失）；链式（`&&`/`||`/`;`/换行）拆
    子命令各自分类、`chain` 携带供矩阵层取保守，`action` 字段静态档位最保守
    （展示用）；sudo/doas 前缀剥离（`-i`/`-u root`/取值 flag 多剥一个值）；
    识别不出 → `unknown`（保守，默认 approve 不是 deny）。**不是 LLM 工具**
    （不注册 model tool；LLM 只该知道 terminal 走同一矩阵）。
  - `tools/ops_permissions.py` 重写：删 `_DEFAULT_GRADES`/`_GRADE_ORDER`/
    `_DEFAULT_MATRIX`/`classify_command`(L1-L4)/`_L1_EXCLUSIONS`/
    `_CHANGE_COMMAND_RE`/`_matrix_row` 与 deny 分支。保留 env 辅助
    （`defined_environments`/`_map_env_tier`/`_raw_env_definition`/
    `_LEGACY_ENV_TIER_MAP`/`_active_env`/`_active_role`，cli `/env`、runbook
    env 校验继续引用）。`check_ops_command_permission(command, target_env)`：
    classifier → `matrix_data.load_matrix_or_empty()` → `get_level()`；
    execute → None（直过）、approve → dict（走现有审批门）、required →
    `require_confirmation=True`（强制人工）；unknown/动作漏配 → 默认 approve
    （保守）+ warning；链式取矩阵档位最高。decision 字段：
    `action/action_name/rule/note/env/env_tier/role/level/require_confirmation/
    description/classification`。
  - `tools/approval.py` 接线（`check_all_command_guards`）：ops 矩阵段改为
    classifier → 矩阵语义，**删除 deny 硬拒分支**（矩阵无 deny；无条件层
    hardline/sudo stdin/user deny/ansible guard 仍在矩阵之前，顺序不动）；
    无人在场 keep fail-closed，消息改 `action_name`。审批键迁移：
    `ops_matrix:{grade}:{env}` → `ops_matrix:{action_name}:{env}`（
    `ops_confirmation:` 同）；`request_ops_approval`（sudo 路径）同迁，仍兼容
    读 `grade` 字段 fallback（老调用方无 `action_name` 时不断）。`_ops_confirmation_required`
    复用机制不变：required 强制人工，覆盖 yolo/mode/allowlist。
  - 探测点 grade 退役：`hermes_cli/chat_api.py` `_probe_ops_grade` →
    `_probe_ops_action`（`action_name`）；`hermes_cli/web_server.py`
    `grade_box["grade"]` → `grade_box["action"]`（/api/exec + approval_callback
    注册 `action=`）。`tools/sudo_tool.py` docstring/注释更新（逻辑不变；
    `decision.get("action") == "deny"` 分支保留为死代码兼容，矩阵不再产 deny）。
  - 审批注册表：`register_web_approval` 新增 `action=None` 参数（entry 落
    `"action"`，`grade` 字段保留兼容）；`_web_approval_view`/`list_web_approvals`
    透传 `action`（/api/approvals 可见，前端弹窗可展示动作枚举）。
  - 规则表可配置化：`schemas.yaml` `ops.schemas.command_rules`（
    `[{pattern, action, note}]`，按序命中、长模式先）覆盖内置表；
    `tools/topo_schemas.py` `_DEFAULT_SCHEMAS` 加 `"command_rules": []` 白名单
    键；按 schemas.yaml mtime 缓存、配置改动热生效。
- **退役语义（行为变化点）**：
  - L1-L4 命令分级（`_DEFAULT_GRADES` 正则表/graded 判定/deny 档）不再驱动
    terminal 审批；terminal 直跑只有"动作 × 矩阵"概念。
  - B'（批次十七：未分级命令 prod 档默认审批 + `_CHANGE_COMMAND_RE` 变更类
    确认门）退役——`_CHANGE_COMMAND_RE` 不再驱动审批；prod 变更确认由矩阵
    required 档驱动（template2：prod restart/start/stop/… =
    `{approve: required}`）。
  - unknown → 默认 approve 走审批门（矩阵无 deny，保守 = approve 不是拒绝）。
    矩阵未配置动作/env（含 test env）→ get_level 默认 approve。**行为变化**：
    原来 test env 直接执行的未分级命令（如 `echo`/`ls`）现在也走审批门——
    batch28 的 echo/ls 用例按新语义改为 needs_approval 后批准/或改用矩阵
    execute 档命令（`docker ps` + test query=execute）。
  - 审批键迁移：`ops_matrix:{grade}:{env}` → `ops_matrix:{action_name}:{env}`。
    永久 allowlist/审计历史里按 grade 键存的记录不迁移（旧键失效即重新审批，
    保守方向，可接受）；`request_ops_approval` 读 `grade` fallback 保证存量
    调用不崩。
  - classifier 不接触凭据；审批键不含敏感信息（动作枚举 + env 名）。
- **测试**：test_action_classifier.py 87（规则表大全/管道不干扰/链式保守/
  unknown 兜底/受控通道优先/配置规则覆盖）+ test_terminal_matrix.py 14
  （execute 直过/管道/approve smart+人工门/required 强制人工覆盖 smart+yolo/
  unknown 默认 approve/无条件层 4 个/链式保守/terminal vs runbook 一致性）+
  test_ops_permissions.py 23 + test_ops_permissions_guard.py 18（target 跨
  环境，断言动作键 `ops_matrix:run_script:prod`）+ test_ops_confirmation_gate.py
  45（prod required 确认门 + 规则表外 unknown 默认 approve）+ 
  test_change_command_coverage.py 3（classifier 覆盖变更命令族）+
  test_ops_init.py 25（模板矩阵写盘；unknown→approve 语义）+
  test_batch28_exec_api.py 16（三态/凭据零泄露/审批 action 字段）。回归：
  runbook_exec/matrix/runbook_v2/runbook_tools/sudo_exec/ops_target/cron 586
  passed（1 个无关 coroutine warning）；test_env_command.py 剩 3 个既有失败
  （`bare_metal_prod` 映射 prod 的 env 名单展示，git worktree 验证 base
  batch59 同样失败，非本批引入）。
- **9131 实测记录**（VIGIL_HOME=/home/wpwang/.vigil + load_hermes_dotenv
  重启，pid 3024768）：
  - `docker ps` env=local → 分类 query → executed exit 0（无审批卡）。注：
    /api/exec 的 env 参数为展示字段（P4 既有语义），矩阵按会话 env（config
    `ops.permissions.env`=test，test 未配矩阵 → 漏配 approve）→ smart 模式
    自动放行；manual 模式行为由 batch28 测试覆盖。
  - `docker ps | grep harbor` env=local → 主命令分类 query（词面绕过消失）
    → executed exit 0，stdout 命中 harbor-log 行。
  - `docker restart harbor-core` env=local → restart → executed exit 0。
  - `ssh root@39.106.217.32 uptime`（target 命中 prod 主机）→ 分类 run_script
    （规则 ssh.controlled，note"ssh 类走 vssh/拓扑凭据受控通道"）→
    run_script × prod = `{approve: required}` → `needs_approval` 弹窗
    （审批条目 action=run_script、grade=null、allow_session/permanent=false、
    description 含"强制人工确认（run_script × prod = {approve: required}）
    …受控通道…目标: 39.106.217.32 (prod)"）→ 批准 once → SSE 执行 → 记录
    executed（本机无 ssh key，连接被拒 exit 255——是执行结果，不是审批拦截）。
  - unknown：`mv /tmp/p5-live-test-a /tmp/p5-live-test-b` → unknown → 默认
    approve → smart 自动放行 executed exit 1（源文件不存在；manual 模式弹窗
    由 batch28 `test_exec_prod_unknown_defaults_approve_then_deny` 等覆盖）。
  - 一致性（同一矩阵两路径）：runbook 直查 `matrix_data.get_level` vs
    terminal `classifier → check_ops_command_permission`——ssh→prod
    run_script required=required、docker ps→local query execute=execute、
    docker restart→local restart execute=execute、docker restart→prod
    restart required=required、mv→local unknown approve=approve，档位全一致。
  - 实测后清理：无落盘产物（mv 未创建文件；审批/执行记录在进程内存，随服务
    重启消失）；`docker restart harbor-core` 为任务书指定的本机容器操作。
- **核销方式**：测试常驻——test_action_classifier.py + test_terminal_matrix.py
  + test_ops_permissions*.py + test_batch28_exec_api.py；季度体检：
  terminal 直跑与 runbook 执行共用同一 matrix.yaml（classifier → 矩阵）、
  `ops_matrix:{action}:{env}` 审批键、未知命令默认 approve 弹窗、
  `ssh root@prod-host …` 命中 prod 档 required 弹窗。
- **状态**：P5 独立 commit（YAPL v1.0 收官；OPS-DELTA #75）。

### 76. P5 回归修复——test_command_guards 7 例 Tirith 内容安全断言恢复（矩阵未初始化惰性 + tirith 警告整体 session-max）（2026-08-23，P5 后续批次）

- **背景**：YAPL P5（de466bd2）后全量回归（scripts/run_tests.sh）抓到
  tests/tools/test_command_guards.py 7 例失败（Tirith 内容安全行为断言：
  both_allow / noninteractive_skips_external_scan / warn_cli_prompts_user /
  warn_session_approved / warn_non_interactive_auto_allow /
  import_error_allows / tirith_warning_disallows_permanent）。P4 基线
  （083bbd32）该文件 29 passed 全绿。
- **根因（diff 验证）**：check_all_command_guards 的 P4→P5 结构一致（仅 deny
  分支移除、消息/审批键换 action），回归 100% 来自
  check_ops_command_permission 返回值变化：
  1. P5 的 unknown → 默认 approve 在**矩阵未初始化**（matrix.yaml 不存在）时
     把 echo/ls/curl 等 P4 直放的普通命令也弹进审批门（P4：矩阵缺失 → 行
     None → 非 prod 未分级命令交回原检查直接放行）；
  2. curl（query）等命中规则表的命令在 test env 矩阵漏配 → approve → ops
     警告（is_tirith=False）混入 tirith 警告提示 → has_permanent_capable
     被顶成 True（弹窗错误出现"Always allow"）。
- **怎么改**：
  - `tools/ops_permissions.py::check_ops_command_permission`：矩阵未初始化
    （`matrix_path().is_file()` 为 False）→ **非 prod 档惰性**——返回 None
    交回原有检查（tirith / 危险命令层兜底），恢复 P4 基线；prod 档（含
    uat→prod 映射、role=prod 自定义名）未初始化仍门控（与 P4 B'/变更确认门
    一致）。矩阵一旦初始化（文件存在）→ 全量 P5 语义（unknown → 默认
    approve，任何 env）。runbook 路径不受影响（其矩阵缺失仍按空矩阵 approve
    门控，P4 既有，非本批回归面）。
  - `tools/approval.py::check_all_command_guards`（Phase 3 has_permanent_capable）：
    "Always" 提供规则改为——dangerous-pattern 键恒可永久；ops_matrix 键在
    纯 ops 提示可永久（P5 allowlist 语义保留，batch28 unknown 审批断言
    allow_permanent=True 不变）；**提示一旦混入 tirith 内容安全警告 → 整体
    session-max（allow_permanent=False）**——一次"永久放行"不得吞掉内容级
    安全发现（PR #67312 语义）；纯 tirith 提示恒 False（现状）。
  - `tests/hermes_cli/test_batch28_exec_api.py`：test_credential_zero_leak_
    all_endpoints 恢复 P4 语义（test env 未初始化矩阵 → echo 链直跑，凭据
    零泄露仍全端点断言）——此前按"test env unknown 也弹窗"改的审批前置
    流程随惰性语义撤销。
- **测试**：test_command_guards.py 29 passed（P4 基线基准，全绿）；验收套件
  test_ops_permissions_guard 18 + test_ops_confirmation_gate 45 +
  test_terminal_matrix 14 + test_action_classifier 87 + test_ops_permissions
  23 + test_sudo_exec 26 + test_ops_target 15 + test_ops_init 25 +
  test_batch28_exec_api 16 + test_batch47_sudo_approval + vssh 30 = 全绿；
  runbook/matrix 存量回归 133 passed；test_approval.py +
  test_approval_plugin_hooks.py 仍 5 例存量失败（_record_approval_trajectory
  多值参数，git worktree 在 de466bd2 基线复现同样 5 例——非本批引入、未变多，
  独立批次修）。
- **9131 实测记录**（VIGIL_HOME=/home/wpwang/.vigil + load_hermes_dotenv
  重启，pid 3233336；live config approvals.mode=smart + 真实 aux LLM 会阻塞
  事件循环导致 /api/exec 挂起——既有问题，本批用 guard 级捕获验证 web 审批
  条目同源字段）：
  - `curl https://bit.ly/abc`（矩阵存在、test 漏配 approve + tirith warn
    shortened_url）→ 弹窗出现，pattern_keys=["tirith:shortened_url",
    "ops_matrix:query:test"]，**allow_permanent=False、allow_session=True**
    （无"Always allow"，符合验收）→ 批准后执行。
  - 对照 `curl https://example.com`（无 tirith 警告）→ 纯 ops 提示
    allow_permanent=True（矩阵 approve 的永久 allowlist 语义保留）；env=local
    档（query=execute）→ ops=None 无提示。
  - 正交验证：`curl` × local = execute（ops 直接过）时 tirith warn 仍独立
    弹窗（execute 档直接过 ≠ 跳过 tirith warning）。
- **核销方式**：测试常驻——test_command_guards.py 29 常绿 + 验收套件；
  季度体检：未初始化矩阵（无 matrix.yaml）时非 prod 命令交回原检查、
  tirith 警告弹窗无"Always allow"（allow_permanent=False）。
- **状态**：独立 fix commit（P5 回归修复）。

### 77. 存量 bug 修复批次——4 例（approval 轨迹签名冲突 / launchctl 高危漏判 / env 可用名单 / topo 落盘断言）（2026-08-23，P5 后续批次）

- **背景**：P5 收官后全量回归 + 分套件验收暴露 4 个存量 bug（均与 YAPL 无关的
  既有问题），本批次全部修复。实际失败面与任务书略有出入：任务书列的
  test_ops_permissions_guard 4 例当前已过（batch61 后旧状态），实跑失败为
  test_approval_plugin_hooks 4 例 + test_approval 1 例 + test_env_command 3 例
  + test_topo_slash_command 1 例（后两文件 P4 基线同失败，纯存量）。
- **Bug 1——_record_approval_trajectory 签名冲突（4 例，最高优先）**：
  `tools/approval.py::_record_approval_trajectory(status, *, command,
  description, ...)` 定义 status 为位置参数，但 check_execute_code_guard 无
  回调 fallback（约 :3610）与 check_all_command_guards 无回调 fallback（约
  :4744）两处同时传位置 `"requested"` 与 `status="pending_approval"` 关键字
  → `TypeError: got multiple values for argument 'status'`。其余 14 处调用均
  只传位置参数。**修复**：删除两处多余的 `status="pending_approval"` kwarg
  （与 requested 记录行为一致，record_event 的 approval 字段即状态；最小改动，
  不引入 meta 字段）。git blame：两处与批次二十三（fc723eb86）同批引入。
- **Bug 2——launchctl 高危检测漏判（1 例）**：DANGEROUS_PATTERNS 的 launchctl
  模式目标匹配只写 `(vigil|ai\.vigil)`，注释里明说的服务标签
  `ai.hermes.gateway` 反而漏判；且 `start` 动词缺失（start/stop 同属服务
  生命周期）。`launchctl stop ai.hermes.gateway` → dangerous=False。
  **修复**：动词列补 `start`，标签匹配扩为 `(vigil|hermes|gateway)`；查询类
  动词（list/print）不在列、非自身标签（com.example.unrelated）不命中，
  对照测试 test_unrelated_labels_not_flagged 保持不误伤。
- **Bug 3——/env 可用环境列表缺 config 定义环境（3 例）**：cli.py /env 的
  可用名单来自 ops_permissions.defined_environments()（档位折叠：自定义名
  bare_metal_prod → prod 档被折叠掉），于是列表只有 local/test/prod、
  `/env bare_metal_prod` 报"未定义"、报错提示也不列自定义名。**修复**：新增
  `all_defined_environments()`（config ops.environments 按名字原样返回，去重
  保序；未定义回退内置四值），cli.py /env 展示与切换改用该名单；档位映射
  语义由权限判定侧 `_map_env_tier` 保留（bare_metal_prod 仍走 prod 档，更严
  不更松）——`defined_environments()` 折叠语义与其单测不动。测试侧：env_home
  fixture 显式初始化 matrix.yaml（P5 矩阵语义在矩阵存在时对任意 env 全量生效；
  矩阵未初始化惰性（OPS-DELTA #76）只对 prod 门控、test 档交回原检查，与本
  测试"矩阵跟随 /env 切换"的意图不符；真实运维环境配置自定义 env 即已跑过
  vigil matrix init）。
- **Bug 4——topo slash 交互收集落盘断言陈旧（1 例）**：交互链路
  （收集 host/env/凭据 → 确认 → write_discovery）实测完整可用，落盘产物为
  v0.4 布局 `services/<host>.yaml` + `entities/...` + `topology.yaml`；测试
  断言 `hosts/8.140.60.44.yaml` 是 batch52（12327df2，拓扑 schema v0.4 分层）
  前的旧路径（P4 基线即失败，存量）。**修复**：断言改 `services/8.140.60.44.yaml`；
  不改 topo 数据层（v0.4 写路径是刻意设计）。
- **测试**：4 个文件全绿——test_approval 100 + test_approval_plugin_hooks 21
  + test_ops_permissions_guard 18 + test_env_command 6 + test_topo_slash_command
  8 = 153 passed；相关回归 test_command_guards 29 + test_ops_permissions 23 +
  邻域 299（sudo_exec / ops_confirmation_gate / terminal_matrix /
  action_classifier / ops_target / batch28_exec_api / topo_discovery /
  topo_v4 / topo_tools / topo_status_sync）全绿，无新增失败。已知存量（本批
  不动，HEAD 复现同失败）：test_gateway_restart_loop 14 例；test_ops_
  permissions_guard 先跑时 test_command_guards 2 例跨文件状态污染（单跑 29
  全绿，batch61 验收口径不变）。
- **9131 实测**：/env 展示与 launchctl 检测不涉及运行时可跳过（任务书明确）。
- **核销方式**：测试常驻——上述 4 文件 + 回归套件；季度体检：launchctl
  stop/start ai.hermes.gateway 判 dangerous 且 list 不误伤、/env 无参列出全部
  config 定义环境（含自定义名）、trajectory 无多值参数 TypeError。
- **状态**：独立 fix commit（存量 bug 修复批次）。

### 78. UI 监控 API 批次——健康探测 / PromQL 查询 / 活跃告警 + 监控页（2026-08-23，YAPL 之后第一个新功能面）

- **背景**：YAPL P1-P5 收官后 dashboard 没有任何监控页面/API——agent 侧有
  prom 工具集（prom_query/alert_query，批次三），但人看不到监控数据。本批补
  齐 UI 监控展示层。定位：通用监控展示层，Prometheus 协议为底座但不绑死——
  查询/告警走 PromQL/Alertmanager 兼容层；**服务健康不依赖 Prometheus**
  （开箱即用，拓扑数据即可探测）。数据源复用 ops.prometheus 配置段（零新配置）；
  未配置 → 503 + 明确提示，健康探测照常。
- **新增**（纯新增面，不改既有功能语义，未碰 YAPL 核心）：
  - `hermes_cli/monitoring.py`：健康探测（拓扑服务枚举 + 并发探测 + 30s 缓存）
    + PromQL 查询 + Alertmanager 活跃告警三个只读函数。探测复用 P4 执行器
    检查通道语义（http_status / port）但独立轻量实现（不依赖 runbook 执行器）。
  - `hermes_cli/web_server.py` 三个端点（全 `_require_token`，不进
    PUBLIC_API_PATHS，与 /api/matrix 同机制）：
    - `GET /api/monitoring/health`——遍历拓扑服务（P1 数据层 services/），
      HTTP 类 endpoint → GET 200-399 = up；端口类 → TCP 通 = up；无 endpoint/
      需认证 → unknown（不误报）；extra_ports 补充探测。并发 5、单次 3s、
      批次总 30s（超时未完成 → unknown，不误报 down）；30s 短缓存（内存
      dict + TTL，refresh=1 强制重探）；只读动态状态——不落盘、不写拓扑、
      不产生审计、凭据明文不落日志。
    - `GET /api/monitoring/query?promql=&duration=&step=`——封装 prom_tools
      只读通道，结构化 series（时间点 + min/max/last）；promql 必填、
      duration/step 默认 30m/60s、非法 400；未配置 503；上游超时/错误 502
      （上游原始信息不吞）。
    - `GET /api/monitoring/alerts`——Alertmanager /api/v2/alerts 实时快照
      （alertname/severity/instance/labels/startsAt/state，resolved 过滤）；
      未配置 503；空列表 200 不 500。
  - 前端：`web/src/lib/api.ts` 三个调用 + `MonitoringPage.tsx`（三块布局：
    服务健康表格/汇总/状态筛选/30s 自动刷新；PromQL 查询 + 手写 SVG
    sparkline（不引图表库）；活跃告警 severity 色标复用 Incidents 分级样式；
    未配置/空态引导，全 unknown 不崩）+ App.tsx 路由 /monitoring（label「监控」，
    Activity 图标）。
- **语义边界（与 Incidents 并存）**：`/api/incidents`（批五十）是 watch 采集
  的告警流（inbox 归档）；`/api/monitoring/alerts` 直接查 Alertmanager 实时
  状态——两者并存：Incidents = 采集归档，monitoring/alerts = 实时快照。
- **测试**：后端 `tests/hermes_cli/test_monitoring_api.py` 16 例（health
  三态/HTTP 非 2xx=down/30s 缓存 + 强制重探/批次超时 unknown 不误报/token
  401；query 未配置 503/参数校验 400/上游错误透传 502/结构化 series/自定义
  duration-step；alerts 未配置 503/空列表 200/字段映射 + resolved 过滤/上游
  错误 502）；前端 `MonitoringPage.test.tsx` 6 例（渲染三态徽标/筛选/告警徽标
  + 空态/Prometheus 未配置引导（健康块照常）/查询 series + sparkline/加载失败
  空态）。vitest 21 文件 153 过、`tsc -b --noEmit` 过、`npm run build` 过。
  存量回归：prom_tools/topo/runbook/web 端点套件 212 passed 全绿。
- **9131 实测记录**（VIGIL_HOME=/home/wpwang/.vigil + load_hermes_dotenv
  重启，pid 3495518；VIGIL_DASHBOARD_SESSION_TOKEN 固定便于 curl）：
  - `GET /api/monitoring/health`：真实拓扑 4 主机 62 服务全量探测——summary
    `{up: 10, down: 45, unknown: 7}`；本机真实在听的 web/frontend/backend/
    app/nocobase/healthtracker/kind-registry/harbor-registry 等 up（0.8-2.9ms）；
    未监听端口（istio/argocd 等 k8s 服务本 WSL 未跑）down（1-4ms，connection
    refused 快失败）；无 endpoint 服务（阿里云 3 台 kubelet/hbrclient + 本机
    nginx）unknown（不误报）；extra_ports 补充探测（eventbus 4222/6222/8222
    均 down，如实）。第二次请求 cached=true、checked_at 不变（30s 缓存生效）。
  - 无 token → 三端点均 401；`/api/monitoring/query?promql=up` →
    503 `prometheus_unavailable`（config ops.prometheus.endpoint 为空）；
    `/api/monitoring/alerts` → 503 `alertmanager_unavailable`（未配置分支验证，
    任务书允许）；参数校验/上游错误路径由单测覆盖（本机无 Prometheus 实例）。
  - 前端：SPA 根页 200 且注入 session token；`/monitoring` 路由 200；
    web_dist 含 MonitoringPage 分块（MonitoringPage-*.js）；三块渲染/徽标/
    刷新由 vitest 6 例覆盖（本环境无浏览器，服务端 serve 验证 + 组件测试）。
  - 实测后清理：服务保留运行（9131 = 开发目录服务，沿用旧批习惯）。
- **核销方式**：测试常驻——test_monitoring_api.py 16 + MonitoringPage 6 +
  存量回归；季度体检：health 三态（无 endpoint 不误报 down）、30s 缓存、
  query/alerts 未配置 503 提示、无新配置项（零新配置）。
- **状态**：独立 feat commit（UI 监控 API 批次）。

### 79. chat 用量面板——token 显示 + LLM 价格三级来源（2026-08-23，YAPL 之后第二个新功能面）

- **背景**：sessions 表已记录每会话 input_tokens/output_tokens（/api/analytics/usage
  按天/按模型聚合），config 有 display.show_cost 开关——但 chat UI 没有任何
  token/费用展示。本批补齐：ChatPage token 按钮 + 用量面板（当前会话实时
  token + 今天/近 30 天历史累计 + 费用），费用走独立 ~/.vigil/pricing.yaml
  三级来源。不碰 YAPL 核心 / 监控 API / 拓扑 / runbooks。
- **新增**（零侵入面，未改既有功能语义）：
  - `tools/pricing.py`（新文件，非工具模块——无 registry.register，不被工具
    发现）：`~/.vigil/pricing.yaml` 加载/校验（schema_version/updated_at/
    prices，currency 限 usd|cny，非法条目跳过）、三级查找
    `get_model_price(model)`（manual > online > builtin）、费用计算
    `estimate_cost`、OpenRouter `/models` 公开拉取 `fetch_openrouter_prices`
    （无需 key；只覆盖 online 条目、manual 永不被覆盖；临时文件 + os.replace
    原子写）、入口 `try_refresh_pricing_online`（未配置不拉取，失败不抛不阻塞）。
  - **builtin 兜底语义**：优先复用 `agent/usage_pricing` 官方快照（provider 从
    入参或 config model.provider 解析；OpenRouter/Nous 等 official_models_api
    路由跳过——get_model_price 永不做网络），再按模型名精确扫快照，最后落
    内置估算表（任务书给定 deepseek-v4-flash 0.28/0.42 USD，标注"估算值，
    以实际账单为准"；实际 deepseek-v4-flash 走官方快照 0.14/0.28，与
    sessions 行内 estimated_cost_usd 同源一致）。
  - `GET /api/chat/usage?session_id=`（chat_api.py，照现有 chat 端点模式——
    不进 PUBLIC_API_PATHS、无显式 _require_token，dashboard 鉴权兜底；与
    /api/chat/sessions 一致）：token 直接读 sessions 行（update_token_counts
    每 API 调用增量写、get_session 读到的即实时 totals，无需聚合 messages）；
    活会话优先注册表会话级模型（切换模型后行内 model 仍是首个计费模型，
    COALESCE 语义）；费用按三级价格，价格不可用 cost: null（不瞎算）。
  - config_defaults.py ops 段加 `pricing.openrouter.base_url`（空 = 零网络
    依赖）；web_server lifespan 启动后台线程触发一次在线拉取（不配置/失败均
    不阻塞主流程）。
  - 前端：api.ts `getChatUsage` / `getUsageAnalytics`（历史累计复用现有
    /api/analytics/usage，无新聚合端点）+ 类型；ChatPage header 加「用量」
    按钮 + 面板（当前会话 input/output/总计 + 费用约 $/¥ + 来源标签 手动价/
    在线拉取/内置估算；今天/近 30 天历史累计；无会话空态；会话切换重拉、
    打开拉一次）。
- **语义注明（config display.show_cost）**：show_cost 只控制 CLI 状态栏费用
  显示（默认 off），**不控制 token 显示**——chat 用量面板 token 恒显，费用
  跟随价格可用性（三级价格命中才显，未命中只显 token 不瞎算）。
- **测试**：后端 `tests/tools/test_pricing.py` 9 例（manual>online>builtin
  优先级、currency 校验跳过、deepseek 官方快照 0.14/0.28、快照清空后估算表
  0.28/0.42 兜底、费用计算、在线拉取成功写缓存+manual 保留、拉取失败不阻塞、
  未配置不拉取）+ `tests/hermes_cli/test_chat_usage.py` 5 例（真实 temp
  VIGIL_HOME + SessionDB 建行：实时 token+费用 0.002156、未知模型 cost null、
  404、注册表会话级模型优先、manual 覆盖生效）。前端 ChatPage.test.tsx +4 例
  （面板渲染 token+费用+来源标签/价格不可用只显 token/无会话空态/切会话重拉）。
  vitest 21 文件 157 过、`tsc -b --noEmit` 过、`npm run build` 过。存量回归：
  chat 套件 53 过（1 例存量失败 `test_approval_callback_registers_web_approval_and_waits`
  ——基线即失败，OPS-DELTA #75 grade 退役后断言未同步，非本批引入）、
  usage/analytics/session 套件 25+28 过。
- **9131 实测记录**（VIGIL_HOME=/home/wpwang/.vigil + load_hermes_dotenv 重启，
  pid 4097708；VIGIL_DASHBOARD_SESSION_TOKEN=vigil-monitor-test-9131）：
  - `GET /api/chat/usage?session_id=chat_dc02911b`：真实 token
    input=15248 / output=76 / total=15324；deepseek-v4-flash 内置价
    （官方快照 0.14/0.28）→ cost=0.002156（≈ $0.00 两位显示，源值同
    sessions 行 estimated_cost_usd=0.00216496 一致）；price_source=builtin、
    pricing_version=deepseek-pricing-2026-07。
  - 手动覆盖：写 ~/.vigil/pricing.yaml（deepseek-v4-flash manual 0.10/0.20）
    → cost=0.00154、price_source=manual；删除后还原 cost=0.002156/builtin
    （manual > builtin 优先级实测生效）。
  - 无 token → 401；未知会话 → 404 error envelope。
  - 前端：SPA 根页 200 且注入 session token；`/assets/ChatPage-BQnKHr4Y.js`
    200 且含「创建会话后显示用量」「价格不可用，仅显示 token」分块；面板
    交互（按钮/三块/费用/空态/切换重拉）由 vitest 4 例覆盖（本环境无浏览器，
    服务端 serve 验证 + 组件测试）。
  - 实测后清理：pricing.yaml 已还原删除；服务保留运行（9131 = 开发目录服务，
    沿用旧批习惯）。
- **核销方式**：测试常驻——test_pricing.py 9 + test_chat_usage.py 5 +
  ChatPage 4 + 存量回归；季度体检：三级优先级（manual>online>builtin）、
  get_model_price 零网络、在线拉取失败不阻塞、pricing.yaml 不落密钥、
  show_cost 语义（token 恒显、费用跟随价格可用性）、无汇率换算。
- **状态**：独立 feat commit（chat 用量面板批次）。

### 80. runbook 长任务进度事件流——执行进度 SSE + 前端实时面板 + 主动播报引导（2026-08-23，YAPL 之后第三个新功能面）

- **背景**：dogfood 复盘（2026-08-23 OS patch session）暴露产品缺陷——2 小时+
  runbook 执行（window-check → preflight → 单台确认 → 14 步 patch）用户催了约
  5 次"你没有主动播报"；Vigil 默认"完成时通知"对长任务不够，关键节点必须主动
  可见。runbook_exec 此前只有 approval_callback（审批回调），无进度事件机制；
  前端 RunbooksPage 只有执行按钮 + 事后历史列表，无进行中视图。
- **新增**（机制层 + 行为层，执行记录机制不动）：
  - `tools/runbook_exec.py`：`execute_runbook(..., exec_id=None,
    progress_callback=None)`——与 approval_callback 同模式的事件回调
    （None = 不播报，LLM 工具路径零影响）。事件对象
    `{type: step_start|step_done|step_failed|rollback_start|rollback_done|runbook_done,
    exec_id, runbook, version, step_id, title, action, target, status, phase, ts, detail?}`。
    触发点全覆盖：v0.1 拒绝/校验失败/定时豁免 → runbook_done(blocked/error)；
    每步 step_start → step_done|step_failed；失败触发回滚 → rollback_start →
    回滚步骤（phase=rollback）→ rollback_done → runbook_done（status/error/
    duration_s/step_count）。输出摘要 `_clip(…, 2000)` 截断防爆；回调异常仅记
    日志不阻断执行。执行工具描述加「长任务主动播报」引导（行为层 LLM 修正）。
  - `hermes_cli/web_server.py`：`_RUNBOOK_STREAMS` 进程内事件总线（exec_id →
    buffer/wake/result/done，缓冲上限 500、TTL 600s 淘汰）；
    `POST /api/runbook/executions` 契约变更——立即返回
    `{exec_id, runbook, env, started_at, status: "running"}`，后台线程执行
    （保留 set_hermes_interactive_context/审批卡注册逻辑，审批照常弹卡）；
    `GET /api/runbook/executions` 加 `data.running`（进行中流列表，UI 显示
    "运行中" + 可展开实时步骤）；新增
    `GET /api/runbook/executions/{exec_id}/progress` SSE——先重放缓冲再实时
    等 wake（晚连客户端从起点看起），runbook_done 后关闭；未知 exec_id →
    runbook:error 后关闭。消费端断开不阻塞执行（call_soon_threadsafe 唤醒，
    执行线程从不阻塞）。
  - 前端：api.ts 新契约类型 + `runbookProgressStream(execId, onEvent, signal)`
    （fetch+reader SSE 解析，支持 AbortSignal）；RunbooksPage——执行按钮 →
    立即返回 exec_id → 开 SSE 累积实时步骤（进度面板：状态徽标 成功/失败/
    运行中/被拦截/已回滚 + 回滚 banner + 终态行）；执行历史"运行中"行可点击
    展开实时步骤（复用同一 SSE）；卸载时 abort 流（服务端执行不受影响）。
  - 行为层 skill：`skills/autonomous-ai-agents/hermes-agent/SKILL.md` Hard
    Invariants 补一条「Long tasks report proactively」——关键节点主动播报，
    不等用户催（照 skill 现有格式）。
- **语义边界（实时可见与事后审计分离）**：事件流不落库、不产生审计——实时
  可见是进行中的可观测性；执行记录照旧 ledger
  （runtime/runbook_executions.jsonl）/trajectory/audit 事后审计。ledger 记录
  新增 exec_id 字段（关联执行与进度流）；执行记录机制本身不动。
- **测试**：后端 `tests/hermes_cli/test_runbook_progress.py` 7 例（成功序列、
  失败→回滚序列、回调异常不阻断、ledger exec_id、POST 立即返回 + SSE 真实
  事件、SSE 未知 id、history running）+ `tests/hermes_cli/test_batch59_runbook_exec_api.py`
  3 例同步新契约（共 8 例）。前端 RunbooksPage.test.tsx 6 例（进度面板步骤流/
  状态徽标/终态/运行中行展开 + act 环境修复）。vitest 21 文件 158 过、
  `tsc -b --noEmit` 过、`npm run build` 过。存量回归：runbook_exec/v2/schedule/
  tools/batch40/batch44/create/vault_refs 179 passed。
- **9131 实测记录**（VIGIL_HOME=/home/wpwang/.vigil + load_hermes_dotenv 重启，
  pid 4138044；VIGIL_DASHBOARD_SESSION_TOKEN=vigil-monitor-test-9131；
  临时只读 runbook t-progress-test/t-progress-fail 实测后已删）：
  - 成功路径 SSE：step_start(q1) → step_done(q1, 输出摘要截断 2000)
    → step_start(q2) → step_done(q2) → runbook_done(ok, duration_s=0.2,
    step_count=2)；ledger 记录含 exec_id。
  - 失败回滚路径 SSE：step_start(q1) → step_done(q1) → step_start(q2) →
    step_failed(q2, "expect 未通过: http_status 期望 200，实际 0") →
    rollback_start(场景 rb-main) → step_start(rb-q, phase=rollback) →
    step_done(rb-q, phase=rollback) → rollback_done(ok) → runbook_done(
    status=rolled_back, rolled_back=true, step_count=3)。
  - POST 立即返回 {status: "running"}；GET executions 的 data.running 执行中
    有、完成后清空；未知 exec_id → SSE runbook:error 后关闭；无 token → 401。
  - 前端：SPA 根页 200 且注入 token；RunbooksPage 分块（RunbooksPage-EtL9OhtR.js）
    含「执行进度/运行中/实时步骤/已回滚」文案；面板交互由 vitest 6 例覆盖
    （本环境无浏览器，服务端 serve 验证 + 组件测试）。
  - 实测发现存量 bug（非本批引入，未修，记录待办）：`runbook_handlers._query`
    的 has_target 无 pattern 分支返回 `shell: False` 但命令含管道
    （`ps aux | grep <name> || true`）→ shlex.split 把管道当参数 →
    ps "garbage option" 失败。batch55/56 引入，本批未碰 handlers。
  - 实测后清理：临时 runbook 已删；服务保留运行（9131 = 开发目录服务，沿用
    旧批习惯）。
- **核销方式**：测试常驻——test_runbook_progress.py 7 + batch59 8 + 前端 6 +
  存量回归；季度体检：事件触发点全覆盖（校验失败/每步/回滚/终态）、SSE 断连
  不阻塞执行、输出截断、事件不落库（无审计膨胀）、POST 契约（立即返回 +
  后台执行 + 审批照常弹卡）。
- **状态**：独立 feat commit（runbook 长任务进度事件流批次）。

### 81. Runbook 执行锁 + 覆盖率仪表盘——并发下发防护 + 高危动作覆盖缺口（2026-08-23，YAPL 之后第四个新功能面）

- **背景**：batch80 进度流落地后暴露两个缺口——1) runbook 无并发防护，同名或
  targets 重叠的 runbook 可被 web/定时/LLM 多路同时下发，长任务（2h+）场景可能
  双写同一目标；2) 执行覆盖无仪表盘，矩阵里 `approve: required` 的高危动作是否
  被 runbook 覆盖、近 30 天实际使用与缺口无人可见。本批补齐执行锁 + 覆盖率只读
  仪表盘，执行记录/审计机制不动。
- **新增**（机制层 + 展示层）：
  - `tools/runbook_lock.py`（新文件）：进程级注册表（exec_id → runbook/version/
    targets/started_at/env），`try_acquire`/`release`/`peek_conflict`（只读探测，
    不注册）/`list_locks`/`_prune`（TTL 6h 惰性过期）。冲突判定：**同名 runbook
    或 target 集合重叠** → 拒绝 + 明确错误「runbook X 正在执行中（exec_id，自
    started_at）——锁定中，禁止并发下发（锁 TTL 6h）」；同一 exec_id 重入不判
    冲突。
  - `tools/runbook_exec.py`：引擎入口 `execute_runbook` 包 `_execute_locked()`
    （原执行体缩进 4 空格），锁在入口 `try_acquire`（权威检查，覆盖 web/定时/
    LLM 全部路径），冲突 → `runbook_done(blocked)` + 返回 blocked，`finally:
    release`；新增 `_runbook_targets(data)` 收集 runbook+回滚步骤的
    params.target。
  - `hermes_cli/web_server.py`：`GET /api/runbook/executions` 返回
    `data.locks`（list_locks 快照，UI 据此显示「锁定中」）；`POST
    /api/runbook/executions` 预检 `peek_conflict` → 409 `{code:"locked"}`（引擎
    入口兜底，竞态下 SSE 收 blocked 终态，不悬挂）。
  - `tools/runbook_coverage.py`（新文件，纯只读）：`high_risk_snapshot`——
    matrix.yaml 任一 env `{approve: required}` 的高危动作集 − runbooks 步骤动作
    集 = 未覆盖风险；`usage_snapshot`——近 30 天 trajectory/audit 事件
    （tool_call/approval/terminal）action 字段 → P5 classifier 归类 → 频率表 +
    covered + 缺口 top 5。`GET /api/runbook/coverage` 返回
    `{generated_at, high_risk, usage}`（不进 PUBLIC_API_PATHS，与 runbook 执行
    记录同门控，不加 token）。
  - 前端：api.ts `RunbookLock`/`RunbookCoverageResponse` + `getRunbookCoverage()`；
    OverviewPage 5 卡（Nodes/Services/Runbooks/Incidents/未覆盖风险——Incidents
    接 `getIncidents({limit:1}).total` 真实计数，修复硬编码 0；未覆盖风险卡
    violet 色调，sub「高危 N 已覆盖 M（覆盖率 P%）」，点击跳 /runbooks）；
    RunbooksPage 运行中行「锁定中」可见徽标 + lockOnly 行（无实时流的定时/后台
    锁显示「锁定中（无实时流）」不展开）+「Runbook 覆盖率」区块（动作×使用
    次数×覆盖状态×runbooks + 缺口「建议沉淀 runbook」+ 空态「暂无审计数据」
    引导）。
- **语义边界**：锁是**进程内存级**（单进程，web/定时/LLM 同进程互斥），跨进程
  锁不在本批范围；覆盖率纯只读——不落库、不产生审计、不写矩阵/classifier；
  unknown 动作不计入（classifier 归类失败不污染）；窗口 30 天；
  coverage_pct 四舍五入；空矩阵/空 runbook/无审计 → 空态（覆盖率 0%，不崩）。
- **顺手修**（2 项，均小改动）：
  - `tests/hermes_cli/test_batch31_chat_api.py`：`ev["grade"]` 断言 → `action`
    字段断言（grade 已退役，OPS-DELTA #75 存量断言失同步，本批顺手对齐）。
  - `tools/runbook_handlers.py`：`_query` has_target 无 pattern 分支 base 加
    `base_shell=True`（`ps aux | grep ... || true` 含管道必须 shell 执行，否则
    shlex.split 拆碎报 garbage option——batch55/56 引入、batch80 实测发现的存量
    bug）；expect process（`pgrep -af ... || true`）与 port（`2>/dev/null ||
    exit 1`）分支同步 `shell:True`，`base_shell=False` 初始化防 UnboundLocalError；
    全文件 AST 扫描确认无残留 `shell:False` + 操作符命令。
- **测试**：新增 `tests/tools/test_runbook_lock.py` 6 例（acquire/release、同名
  冲突、target 重叠、同 exec_id 重入、peek 只读、TTL 过期释放）、
  `tests/tools/test_runbook_coverage.py` 5 例（v1+v2 动作收集、高危快照、空矩阵
  不崩、usage 统计+缺口、无审计数据）、`tests/tools/test_runbook_exec.py` +6
  （TestPipelineShellRegression 4 + TestExecutionLock 2 引擎入口锁集成）、
  `tests/hermes_cli/test_runbook_progress.py` +4（POST 409、locks 快照、coverage
  端点 2 例）。关键：三处测试加锁注册表清理 fixture（进程级锁残留会污染跨文件
  断言）。后端 runbook 全套 + batch31 + batch59 + lock + coverage 220 passed；
  matrix/classifier/terminal_matrix 135 passed；chat_usage/pricing 14 passed。
  前端 vitest 21 文件 163 passed、`tsc -b --noEmit` 过、`npm run build` 过
  （web_dist 是 gitignore 产物，不入库，部署时生成）。
- **9131 实测记录**（VIGIL_HOME=/home/wpwang/.vigil + load_hermes_dotenv 重启，
  pid 已更新为最新代码；VIGIL_DASHBOARD_SESSION_TOKEN=vigil-monitor-test-9131；
  临时只读 runbook t-lock-test 实测后已删）：
  - 锁冲突：POST1 → `exec_20260823_0001 running`；POST2 → 409
    `{code:"locked", message:"runbook t-lock-test 正在执行中（exec_id=
    exec_20260823_0001...）锁定中"}`；GET executions 的 locks 快照显示该锁；
    执行完成后 `locks: []`，ledger 记录 `t-lock-test ok exec_20260823_0001`。
  - 覆盖率：high_risk total=15（prod `approve: required` 真实数据）、covered=4、
    coverage_pct=27、uncovered=[decommission, deploy, install, reboot, remove,
    rollback, scale, shutdown, start, stop, upgrade]；usage 30 天扫描 315 事件：
    query 35（covered）、run_script 8、install 6（未覆盖，缺口）、restart 6。
  - Incidents：`GET /api/incidents?limit=1` total=0（本机 .vigil watch inbox 无
    告警数据，正常；前端已接真实计数而非硬编码 0）。
  - 前端：SPA 分块 OverviewPage-BUSa-NWg.js 含「未覆盖风险/覆盖率/高危」、
    RunbooksPage-BXdN-Cd5.js 含「锁定中/建议沉淀 runbook/暂无审计数据/覆盖率」。
- **核销方式**：测试常驻——test_runbook_lock.py 6 + test_runbook_coverage.py 5
  + runbook_exec/progress 回归；季度体检：锁 TTL 6h 惰性过期、冲突判定（同名/
  targets 重叠/同 exec_id 重入）、引擎入口权威检查覆盖全部路径、覆盖率只读
  （无审计写入）、unknown 不计入、30 天窗口、coverage_pct 舍入、_query 管道
  shell 修复不回归。
- **状态**：独立 feat commit（runbook 执行锁 + 覆盖率仪表盘批次）。

### 82. 拓扑发现粒度规范化——compose 项目聚合 + helm release 探测（2026-08-23，设计 9.3/9.5 最后缺口）

- **背景**：batch52 遗留待办①——compose 粒度（现为容器粒度，本机 harbor 拆 8
  条）+ helm release 探测缺口（by_runtime.helm.releases 从未填充，设计 9.5）。
  9.3 粒度定案：docker compose = 项目一条（容器细节进第三层）、k8s = service
  一条（已实现，本批只验证不改）、systemd = 单元一条。
- **新增**（`tools/topo_discovery.py`，读取端/前端零改动，拓扑展示自动适应）：
  - **compose 项目聚合**：docker ps 容器按 `com.docker.compose.project` 分组 →
    每个项目产一条服务行。命名规则（9.3）：默认取与项目同名的 compose service
    名（项目名剥环境后缀 -local/-dev/-prod/-test/-staging/-uat 后与 service
    同名也算应用主 service——harbor-local 内有 service harbor → name: harbor）；
    直接同名优先（nocobase 项目内 service nocobase）；无 → 项目名。**与 k8s
    服务同名时 compose 退让项目名**（k8s 行已实现不改，同机 compose + k8s 双跑
    保持 k8s 服务名）。type 按 9.7 判定表对项目主 service 归类；endpoint 取项目
    内有 published 端口的容器主端口（第一个，无 → null）；extra_ports 其余
    published 端口去重；depends_on/log_paths 照旧。第三层档案
    `by_runtime.docker_compose = {project, workdir（working_dir label，探测不到
    空）, services: [{name: 容器名, state}]}`（状态列表，快照记状态不记配置）。
    无 compose_project 的 docker run 单容器 → 保持容器粒度一条（name = 容器名，
    managed_by: docker，by_runtime.docker）。
  - **helm release 探测**：kubectl 可用时 `helm list -A -o json`（失败 skipped
    不阻塞）。k8s service 条目**不变**（粒度不拆，managed_by 保持 kubectl），
    release 细节写该实体档案 `by_runtime.helm.releases = [{name, chart, revision,
    status, namespace}]`。关联规则：release 的 (namespace, name) 与 k8s svc 实体
    完全匹配才挂载；关联不上（集群级组件如 cilium/istio-base）→ 单独记录
    `{cluster|env|default}-helm-extra` 实体档案（与实体文件名同源兜底），待集群
    实体档案（设计 9.5 预留）落地后迁入。
  - k8s 探针前置：kubectl 探测移到 docker 之前（compose 聚合命名需知道 k8s 服务
    名），probe 顺序变化仅影响 probes 字典展示顺序，runtime 判定结果不变。
- **语义边界**：第一层治理字段（owner/os/credentials）不覆盖（本机 host 行保留）；
  快照记状态不记配置内容；helm 信息只是档案补充不参与 managed_by 判定；阿里云
  3 台未补跑（本批只重建本机，补跑命令 `vigil topo-discover -e prod -H
  <host>`，凭据就绪后执行）。
- **顺手修**：`_parse_published_ports` 只认 host 已发布端口（含 `->` 映射）——
  容器内部端口（`5001/tcp` 无 host 映射）不再视为 published。存量解析偏差：此前
  dify 项目 api 容器行 endpoint 记 LAPTOP:5001（实际不可达）；修复后项目 endpoint
  取真实 published 端口（plugin_daemon 5003）。
- **P2/P4 兼容检查（结果：差异报告，未改 runbook）**：`nginx-config-update`
  （v0.2，target: nginx）——dify compose 项目聚合实体名为 `docker`（项目名，无
  同名/剥后缀 service），target `nginx` 不再命中拓扑 → `resolve_target` 拒绝执行
  （"target 'nginx' 不在拓扑表"）。按任务约束不擅自改 runbook/实体；建议后续：
  runbook target 改 `docker`（handler 按 compose_service label 执行，语义不变）
  或对实体建别名。`harbor-restart`（v0.1 老执行路径）不受影响。
- **测试**：`tests/tools/test_topo_discovery.py` +8 例（多容器项目聚合一条 +
  extra_ports 去重 + L3 services 状态列表 + workdir；同名 service 优先；无同名
  退让项目名；k8s 同名冲突 compose 退让；docker run 单容器保持容器粒度；helm
  匹配挂实体 + 未关联单独记录；helm 失败 skipped；kubectl 不可用 helm 不探测）；
  存量断言 postgres→db 对齐（db 项目聚合）。回归：topo_discovery/v4/v2/tools/
  status_sync/batch39/update_contract/credential_fail_closed/ssh_auth_breaker/
  sudo_exec/batch33/topo_slash/runbook_exec/lock 14 套件 **249 passed / 6
  skipped**。
- **9131 实测记录**（VIGIL_HOME=/home/wpwang/.vigil + load_hermes_dotenv 重启，
  pid 76871；VIGIL_DASHBOARD_SESSION_TOKEN=vigil-monitor-test-9131）：
  - 备份后删除本机 services/LAPTOP-T2JA2ERE.yaml + entities/local__laptop-t2ja2ere__*.yaml
    （备份 ~/notes/backups/vigil-topo-v4-20260823-before-compose-aggregate/）；
    重跑 `vigil topo-discover -e local --yes` 重建。
  - **服务条数收敛：59 → 46**。compose 聚合实体命名清单（供核对）：docker（dify，
    gateway，endpoint LAPTOP:5003 = plugin_daemon 真实 published，extra [8777]）、
    harbor-local（registry，1514，k8s harbor 同名冲突退让项目名）、code（app，
    3000，extra [8765]）、auto_ssh_serve_platform（app，8000）、nocobase（app，
    13000，extra [5432]）、kind-registry（docker run 单容器，容器粒度）。
  - 档案结构：docker 实体 `by_runtime.docker_compose = {project: docker,
    workdir: /home/wpwang/dify/docker, services: [13 容器状态]}`；harbor 实体
    kubectl（ns harbor）+ helm（release harbor/harbor-1.19.1/revision 1）；匹配
    实体 istio-ingress/istiod 均挂 helm releases；`local-helm-extra` 实体记
    cilium + istio-base（未关联集群级组件）。
  - `GET /api/topology`（无 token loopback 可读）：LAPTOP 46 服务，compose 行
    在列，nginx 不在（runbook 差异如上）。
  - 治理字段保留：topology.yaml 本机 host 行 owner/credentials 原样未覆盖。
  - 本机 helm 实际存在（5 releases）→ 走真实探测路径而非 skipped（任务书预期
    本机无 helm，实测有，更完整）。
- **核销方式**：测试常驻——test_topo_discovery 8 新增例 + 存量 topo 回归；季度
  体检：compose 聚合命名规则（同名/剥后缀/k8s 退让）、endpoint 取真实 published
  端口、L3 services 状态列表、helm 关联规则（匹配/单独记录）、managed_by 保持
  kubectl、治理字段不覆盖、阿里云补跑。
- **状态**：独立 feat commit（拓扑发现粒度规范化批次）。

### 83. dify 实体改名 + 命名规则冲突条款 + runbook target 修正（2026-08-23，batch67 收尾小批次）

- **背景**：batch67 聚合命名把本机 dify 项目（/home/wpwang/dify/docker，compose
  项目名 docker）命名为 **docker**——与 docker 平台名混淆，LLM 视角歧义（"操作
  docker 实体还是 docker 运行时？"）。用户拍板粒度维持「项目一条」并补命名规则
  冲突条款；`nginx-config-update`（v0.2 样例）target: nginx 因聚合后实体名
  docker 而 resolve 失败（batch67 已列报告）。
- **数据操作（任务 1）**：`services/LAPTOP-T2JA2ERE.yaml` gateway 行 name
  docker → dify（detail 引用同步）；实体档案
  `entities/local__laptop-t2ja2ere__docker.yaml` → 改名
  `local__laptop-t2ja2ere__dify.yaml`（内容 name/detail 同步，compose_project
  docker 事实值不动）。冲突检查：entities/ 无 dify 残留、services 行名唯一、
  matrix/权限无实体名绑定、全目录无旧文件名引用。备份
  ~/notes/backups/vigil-dify-rename-20260823/。
- **runbook（任务 2）**：`runbooks/nginx-config-update.yaml` steps 与 rollback
  的 target: nginx → dify（5 处，只改 target 值；expect 的 target: http 与 url
  不动；handler 按容器 label com.docker.compose.service 执行 nginx，语义不变）。
- **命名规则冲突条款（任务 3，代码 + 测试）**：`tools/topo_discovery.py` 聚合
  命名补条款——compose 项目名 ∈ 平台/运行时保留词（docker/k8s/kubernetes/helm/
  compose/containerd/podman/systemd/systemctl/kubectl/kubelet/runtime）→ 强制用
  业务主 service 名（不依赖同名 service；主 service = 第一个有 published 端口的
  service，无则按剥环境后缀逻辑的业务名）；主 service 名也无业务语义（api/web/
  worker/plugin_daemon 等泛化组件名单）→ 保留项目名并注明人工核对。dify 实测
  归入兜底（主 service plugin_daemon 泛化 → 项目名 docker）——本批已人工改名
  dify，重扫时 merge 语义保留手动行（新增 docker 行需人工清理，OPS-DELTA 注明）。
  新增测试 3 例：保留词项目 → 用业务主 service（docker 项目内 registry → 实体
  registry，project 事实值不动）；保留词项目主 service 泛化（api/web）→ 项目名；
  非保留词项目 → 原规则不变（回归）。
- **顺手修（9131 实测暴露）**：`tools/runbook_exec.py` `_is_local_endpoint`
  只比较完整 endpoint 字符串——本机服务行 endpoint 带端口（LAPTOP-T2JA2ERE:5003）
  永不匹配本机身份 → 误判远端，SSH 回环报 Host key verification failed。修复：
  endpoint 剥 `:port`（含 IPv6 方括号）后再比对本机 hostname/网卡 IP。测试 +3
  断言（带端口本机 endpoint 判 local / 远端带端口判 remote）。
- **阿里云补跑（任务 4，可选）**：凭据 ~/.ssh/aliyun_nopass.pem 可用，3 台全部
  补跑（备份后删旧文件重建，备份
  ~/notes/backups/vigil-aliyun-rebuild-20260823/）。结果：39.106.217.32 /
  39.107.92.54 → kubelet（systemd，无端口），8.140.60.44 → hbrclient（阿里云
  备份客户端）；无运行容器（compose 聚合 N/A）；kubectl 探测权限不足（无 sudo
  密码，非阻塞）；hardware/ 3 台补齐；治理字段（owner）保留。
- **测试**：test_topo_discovery +3 例（命名冲突条款）；test_runbook_exec +3 断言
  （endpoint 带端口本地判定）。回归：topo 全套 + runbook_exec/lock/coverage +
  batch33/topo_slash/credential/ssh/sudo 16 套件 **257 passed / 6 skipped**。
- **9131 实测记录**（VIGIL_HOME=/home/wpwang/.vigil + load_hermes_dotenv 重启，
  pid 338126 → 修复后重启；VIGIL_DASHBOARD_SESSION_TOKEN=vigil-monitor-test-9131）：
  - `/api/topology`：LAPTOP 46 服务，**dify 在列**（type gateway、endpoint
    LAPTOP:5003、managed_by docker_compose、detail name=dify），**无 docker 实体
    残留**；dify detail 正常加载（by_runtime.docker_compose，无 404）。
  - **nginx-config-update 实际执行**（exec_20260823_0001/0002，走 9131 POST +
    SSE）：`resolve_target("dify")` 通过（type service、remote=False、container
    docker-nginx-1、compose_project docker、compose_service nginx——本地通道，
    SSH 误判修复验证）；步骤真实执行：backup 步 `mkdir -p /backup/nginx/config
    && docker cp docker-nginx-1:/var/lib/dify /backup/nginx/config/latest`——
    第一次因 SSH 误判远端失败（exit 255 Host key verification failed，修复后消
    失）；主机侧准备 /backup 目录（sudo，root 目录样例 runbook 假设）后重跑，
    backup 步 docker cp 报 `/var/lib/dify` 在 nginx 容器不存在（样例 runbook
    未给 src、实体 config_dir/data_dir 未登记，兜底路径随实体名变为
    /var/lib/dify；nginx 容器处于 Restarting 崩溃循环）→ runbook_done failed
    （on_failure: stop，回滚未触发）。**如实记录**：resolve 通过 + 本地执行链路
    验证完成；崩溃循环预期下的 verify→回滚链路未走到（首步 backup 即失败，样例
    runbook 源路径问题，不在本批改 runbook 范围内）。ledger 三条执行记录齐全。
- **核销方式**：测试常驻——test_topo_discovery 命名冲突条款 3 例 + runbook_exec
  endpoint 判定回归；季度体检：保留词项目命名（业务主 service/泛化兜底）、dify
  重扫 merge 语义（手动行保留）、endpoint 带端口本地判定、阿里云 kubectl 权限
  补跑（sudo 就绪后）。
- **状态**：独立 feat commit（dify 改名 + 命名规则冲突条款批次）。

### 84. 环境类测试失败排查批次（2026-08-23，batch69 全量回归收敛）

- **背景**：batch68 后全量回归（scripts/run_tests.sh 逐文件隔离）失败面过大，
  任务书要求重跑拿当前基线 → 逐类归因（A 平台 / B 测试设计 / C 依赖 / D 真
  bug）→ 修完收敛 → 每例剩余失败有可解释理由。开发在 branch v1.0
  （vigil-agent），发布基线 vigil-agent-release 不动。
- **基线**（重跑后）：98 文件 / 212 失败 + 1 超时
  （`test_model_switch_custom_providers.py` 600s，真实网络轮询
  curated/ollama 模型列表）+ 1 收集错误（`test_mcp_sse_transport.py`
  patch 目标缺失 = anthropic SDK 缺装）。完整失败清单存
  /tmp/failing_files.txt（本批报告附件）。
- **A 类（平台特定 skip，1 文件）**：`test_dashboard_profiles_nav_label.py`
  本 fork web 前端无 `web/src/i18n/`（上游测试面残留）——skipif 精确检测
  该目录存在性，理由注释写明"本 fork 前端无 i18n 面"，CI 有该目录时照跑。
- **B 类（测试设计修复 / 品牌对齐 / mock 补齐，~56 文件）**：
  - 品牌改名对齐（.hermes → .vigil 路径、hermes → vigil 命令/昵称/文案、
    `-p <profile> gateway start` 命令串）：test_context_references /
    test_file_safety_container_mirror / test_credential_files / test_file_sync /
    test_ssh_bulk_upload / test_image_generation_artifacts /
    test_vercel_sandbox_environment / test_google_workspace_credential_files /
    test_grounded_citations_skill / test_feishu_bot_admission / test_irc_adapter /
    test_matrix_mention / test_slack / test_telegram_mention_boundaries /
    test_config / test_projects_db / test_termux_all_extra_compat /
    test_packaging_build_guard / test_toolset_validation / test_codex_runtime_switch /
    test_update_stale_dashboard / test_web_server_messaging_profiles /
    test_hermes_home_profile_warning / test_skin_vigil_root / test_setup_reconfigure /
    test_setup_openclaw_migration / test_batch40_migrate_platform_toolsets（matrix
    并入 cli toolset 契约）/ test_ops_first_install / test_verify_console_scripts /
    test_stage2_hook_symlink_chown（镜像内用户仍为 hermes，chown 断言改回
    hermes:hermes）等。
  - 环境/mock 补齐：test_vision_routing_31179（`_module_isolation` fixture 只恢复
    sys.modules 不恢复父包属性导致模块分裂 + 能力未知时保守放行）、
    test_relay_shared_metrics_runtime（fake_run_conversation 签名 7→8）、
    test_model_switch_custom_providers（mock 网络模型列表，428s→9.4s 全绿）、
    test_watch_collect / test_watch_tools（缺省随 DEFAULT_CONFIG 合并为 false）、
    test_web_providers / test_web_tools_config（无凭据兜底 ddgs，OPS-DELTA #48）、
    test_web_approval_registry（契约键补 action，batch60 迁移）、
    test_approval_choices_filter（fixture 补 matrix.yaml，断言改强制人工确认）、
    test_terminal_password_prompt（mock 独立 ansible inventory 安全层）、
    test_file_read_guards（`_make_safe_tempdir` 不再落仓库根触发 install-code
    写保护）、test_voice_mode / test_voice_wsl_pipewire（mock powershell/ffmpeg
    可用性，WSL2 实机有）、test_mcp_serve（utime 显式 +1s，WSL 粗粒度 mtime）、
    test_hermes_state_readonly_preflight（WAL sidecar 显式创建）、
    test_tui_gateway/test_protocol（skin 目录经 VIGIL_HOME 解析）。
  - **本批新增关键修复**：`test_save_conversation_location.py` 原
    `startswith("cli")` 的 sys.modules 清扫把整个 `hermes_cli` 包 + 全部子模块
    逐出且不恢复——`"hermes_cli".startswith("cli")` 为真——造成模块身份分裂
    （旧模块持有旧 hermes_constants 的 contextvar，新模块持有新 contextvar，
    `set_hermes_home_override` 对旧 get_hermes_home 不可见；profiles 根
    monkeypatch 落在错误副本上）。单进程批跑（非逐文件隔离）时级联炸 6 例：
    `test_relay_shared_metrics_consent[True-False]`、web_server
    builder-mcp 档案路径、messaging profiles scoped read/write x3 + port
    binding guard。修复：只逐出 `cli`/`hermes_constants` 两个真实重载目标并在
    teardown 精确恢复 sys.modules，根因消除（本文件 2 测试自身也继续全绿）。
  - `test_run_agent.py` `_BarrierDB` 补 `flush_token_counts` 空实现——线程内
    `_persist_session` 收尾调用缺方法导致 PytestUnhandledThreadExceptionWarning。
  - canonical 全量回归首跑暴露的 3 例补充修复：
    `test_otlp_exporter.py`（clean-hermes 改名 commit 把 span 名/attr 前缀改
    vigil 但测试未同步——补齐断言；该文件此前因 opentelemetry 未装被
    importorskip，随 C 类装依赖后首次真正执行）、
    `test_termux_api_detection.py`（环境探测未钉死——WSL 开发 shell 有
    PULSE_SERVER 时可用、hermetic env -i 下不可用，结果随宿主环境翻转；
    按 voice 批次惯例 mock is_container/PULSE_SERVER/powershell 回退钉住
    Termux:API 分支语义）、
    `test_relay_shared_metrics.py` 并发写 flake（见 D 类）、
    `test_transcription_tools.py`（0.1s idle timeout 撞并行跑 interpreter
    启动耗时——放宽为 1.0s idle / 8×0.2s 心跳，语义"idle 短于总时长且心跳
    不断"不变）。
- **C 类（依赖安装，环境层面无代码改动）**：装 anthropic 0.87.0 / mcp 1.28.1 /
  daytona 0.155.0 / modal 1.3.4 / hindsight-client 0.6.1 / parallel-web 0.4.2 /
  defusedxml 0.7.1 / setuptools 83.0.0，对应外部依赖缺失用例（nous_portal /
  mcp sse / modal / daytona / hindsight / parallel provider 等）转绿；`ddgs`
  不装（测试全部走 mock）。`test_mcp_sse_transport.py` 收集错误随 anthropic
  SDK 安装消失。
- **D 类（真 bug，修 11 文件）**：
  - `cron/lifecycle_guard.py`：gateway 生命周期正则各分支只认 `vigil` 不认
    `hermes` 命令名（launchctl label / systemctl unit / pkill），launchctl
    submit 分支已双名但 kickstart/unload 等漏 → 双名匹配。
  - `gateway/run.py`：honcho config memo 键只 (path, mtime_ns)——WSL/ext4
    粗粒度 mtime 下同 tick 两次写入同尺寸文件缓存不失效 → 键加内容 sha256。
  - `hermes_cli/main.py`：桌面打包可执行文件解析只认 Vigil.app/Vigil.exe，
    apps/desktop package.json productName 仍为 Hermes → mac/win 双名候选。
  - `hermes_cli/model_switch.py`：Nous 家族识别修正（外部 Hermes 模型不误标
    Vigil）。
  - `hermes_cli/providers.py`：xAI provider label。
  - `hermes_cli/web_server.py`：`_CATEGORY_MERGE` 补 `platform_toolsets` →
    agent（该分类仅 cli 一个 schema 字段，消除单字段孤儿 tab）。
  - `plugins/platforms/photon/sidecar/index.mjs`：鉴权 header
    `x-hermes-sidecar-token` → `x-vigil-sidecar-token`（adapter/README 早已
    Vigil 名，服务端漏改）；liveness probe 前缀同步 vigil。
  - `tools/credential_files.py`：容器挂载路径硬编码 `/root/.hermes` →
    `_default_container_base()`。
  - `locales/en.yaml`：update 播报 "Starting Hermes update" → Vigil。
  - `tools/watch_collect.py` / `tools/watch_tools.py`：docstring 订正"缺省 =
    DEFAULT_CONFIG 合并后 false"（行为未改，文档与实现对齐）。
  - `hermes_cli/observability/shared_metrics.py`：写路径 busy timeout
    250ms → 5s（与 schema 路径一致）——并发计数器写入在忙机/并行回归跑下
    可超 250ms 锁等待，报 `database is locked` 丢计数（canonical 全量跑 1/2
    概率暴露）；新增回归测试 `test_counter_write_waits_out_busy_lock_instead_of_failing`
    （持锁 0.3s，写方 5s 内必须等出而非报错）。
  - D 类大项清单（未在本批硬改，单独报告）：YAPL 核心 2 文件——
    `tests/scripts/test_footgun_subprocess_encoding.py`（footgun 规则在
    tools/runbook_exec.py / sudo_tool.py / topo_discovery.py 命中新匹配）、
    `tests/tools/test_subprocess_stdin_guard.py`（subprocess stdin= 检查同样
    命中 YAPL 子进程扫描面）——按硬约束不碰 YAPL 核心，单独报告留人工决定。
- **收敛验证**：
  - 96 文件失败集（基线 98 剔除 YAPL 2）单进程批跑：2089 passed / 24 skipped /
    0 failed（316.66s，--tb=short，见 pytest 输出附件）；24 skipped 均为精确
    带理由 skip。
  - canonical `scripts/run_tests.sh` 逐文件隔离全量回归（env -i hermetic）
    收敛结果见本批交付报告：除 YAPL 2 文件（单独报告）外 0 失败。
- **状态**：独立 commit 分组（fix(tests) 主体 + fix(core) 生产代码 + README
  Windows 安装指引）。

### 85. subprocess 安全规范批次（2026-08-24，batch70 —— YAPL 2 检查修复）

- **背景**：batch69 全量回归（scripts/run_tests.sh 逐文件隔离）暴露 YAPL 检查
  2 例失败：`tests/scripts/test_footgun_subprocess_encoding.py`（footgun 规则：
  本仓库 subprocess 调用一律显式 `text=True, encoding='utf-8', errors='replace'`
  同行，按源行扫描）与 `tests/tools/test_subprocess_stdin_guard.py`（subprocess
  调用无 stdin 注入用途时一律显式 `stdin=subprocess.DEVNULL`）命中
  `tools/runbook_exec.py` / `tools/sudo_tool.py` / `tools/topo_discovery.py`
  的 subprocess 调用点。batch69 按硬约束未碰 YAPL 核心、单独报告留人工决定；
  本批（batch70）用户拍板确认为真实安全规范问题，修复 3 个工具文件，仍不碰
  YAPL 核心与测试本体（footgun 规则不动）。开发在 branch v1.0
  （vigil-agent），发布基线 vigil-agent-release 不动。
- **怎么改**（只动 3 个文件内的 subprocess 调用点，同文件同类缺陷一并修；
  无 input 注入的调用统一补 `stdin=subprocess.DEVNULL`，编码三件套与
  `text=True` 保持同一源行——footgun 规则按行扫描，跨行会被判 miss）：
  - `tools/runbook_exec.py`：`_exec_local`（474）本地命令执行、
    `_exec_transfer` 的 scp 执行（604）、`_exec_script_asset` 的 bash 脚本
    执行（672）——均补 DEVNULL + 编码三件套。
  - `tools/sudo_tool.py`：`_run_local_sudo`（247，sudo -A + SUDO_ASKPASS
    认证流）、`_ssh_run`（305，ssh 远端命令）、`_scp`（357，scp 上传）——
    均补 DEVNULL + 编码三件套。
  - `tools/topo_discovery.py`：ssh 远端执行（306）与本地 sudo -S（356）——
    两处是 **sudo 密码 stdin 注入点，`input=sudo_stdin` 保留不动**（注入语义
    不得削弱），仅补编码三件套；`_askpass_output`（380，askpass 脚本读取
    密码明文）无注入用途——补 DEVNULL + 编码三件套。
- **为什么保留 `input=`**：`tools/sudo_tool.py` 无 input（密码走
  SUDO_ASKPASS 环境）；`tools/topo_discovery.py` 两处 sudo 以
  `input=sudo_stdin` 注入密码，DEVNULL 会截断认证导致 sudo 失败——按任务书
  约束"sudo 密码注入语义不得削弱"，只补显式编码不碰注入路径。
- **回归面**：2 个 YAPL gate 测试全绿（12 passed：footgun 规则 0 hits +
  stdin guard 全绿）；回归套件 1（17 文件 388 passed：test_sudo_exec /
  test_sudo_stdin_guard_sources / test_askpass_interception /
  test_batch40_askpass_narrow / test_ssh_auth_breaker / test_runbook_exec /
  test_batch59_runbook_exec_api / test_command_guards / test_approval /
  test_batch47_sudo_approval / test_sudo_clarify_flow / test_topo_discovery /
  test_topo_v4 / test_topo_status_sync / test_batch39_topo_sync /
  test_topo_tools）；回归套件 2（15 文件 220 passed：runbook 系列 +
  topo_slash 系列 + approval/cron 契约）。canonical 全量（约 30min）未跑，
  以回归套件 + 实机通道实测为准。
- **实测验证**（9131 服务已用新代码重启，VIGIL_DASHBOARD_SESSION_TOKEN 起
  服务，令牌实测生效）：
  - ssh 通道：`sudo_tool._ssh_run`（305，真实路径非 mock）连
    39.106.217.32（ssh_key ~/.ssh/aliyun_nopass.pem）→
    `hostname && uname -r` 返回 `node1 / 5.14.0-687.12.1.el9_8.0.1.x86_64`，
    returncode 0。
  - scp 通道：`_scp`（357）上传探针文件到 `/tmp/vigil-b70-scp-probe.txt`
    → 远端 cat + 清理 returncode 0，内容回读正确。
  - runbook 命令通道：临时只读 v2 runbook（query action, target dify）POST
    `/api/runbook/executions` → exec_id `exec_20260824_0001`，SSE progress
    step_done ok（`docker-nginx-1 Restarting (1) 4 seconds ago`）、
    runbook_done ok（`duration_s 0.23, step_count 1`）；ledger 已落盘；
    临时 runbook 文件已清理（不留在用户 runbooks 里）。
- **状态**：独立 fix commit（本批 batch70），只含 3 个工具文件 +
  OPS-DELTA.md 本条登记；YAPL 核心/测试本体未动。

### 86. YAPL 主框架阶段 A——契约基础设施 + resolve_topo_ref（2026-08-24，batch71）

- **背景**：YAPL P1-P5 + 监控/用量/播报/小项/粒度/排查全部完成（HEAD
  88694774，batch70）。本批 = YAPL 主框架（LLM 编译工具）第一阶段——契约
  基础设施：契约文件加载 + 分层校验器 + resolve_topo_ref 共享库。设计单一
  事实来源 = yapl-design.md 第十三章（2026-08-24 逐字段盘完）。只做阶段 A，
  不做 LLM 生成管线/沙箱/注册（阶段 B）、不做 runbook 第 24 动作（阶段 C）。
  开发在 branch v1.0（vigil-agent），发布基线 vigil-agent-release 不动。
- **新增模块（零侵入，独立文件）**：
  - `tools/contract_tools.py` —— 契约加载器 + 分层校验器（schema v0.1）：
    - 加载器：`~/.vigil/contracts/` 目录加载（缺失 = 空集）；非 .yaml 忽略 +
      警告（照 runbooks/ 先例）；文件名 `<name>.yaml` ↔ 内部 name 一致 =
      硬校验拒绝；`load_contracts` / `load_contract`。
    - 分层校验（失败 = ValueError 带字段/枚举/示例引导，不落盘）：
      - 结构层：name kebab-case（同 runbook 创建名规则）+ 与文件名一致 +
        统一命名空间重名拒绝（内置工具表 `registry.get_all_tool_names()` +
        runbooks/*.yaml stem）；description 非空；action 必填 ∈ schemas.yaml
        actions 词表（当前 23 个，词表读取失败跳过——同 runbook v0.2 惯例）；
        **拒绝 permission/steps 字段**（设计铁律：权限 = 矩阵唯一裁决，契约
        是原子层）。
      - params 层：类型系统 string/integer/boolean/float/topo_ref(+kind)/
        enum(非空标量 values)/list(必带 items，items 不支持 list 嵌套)；
        default 类型与声明一致（integer 拒绝 bool）；required=true + default
        = 矛盾拒绝；enum default ∈ values。
      - returns 层：键 + 类型（支持设计 13.1 简写 `restarted: integer`
        归一为完整声明）；**拒绝 topo_ref（含 list items 内）**——返回是
        数据不是引用。
      - tests 命门：最少 2 用例（1 正常 + ≥1 错误路径）；input 与 params
        匹配（未知键拒绝 / required 缺失拒绝 / 值类型一致）；expect 精确
        匹配结构 或 `{error: <非空错误码>}` 二选一（错误码枚举后续随实现
        扩展）；handler mock 可选 `{status: failed|success}`（本批只校验
        存在性/形态，语义阶段 B 用）。
  - `tools/topo_ref.py` —— `resolve_topo_ref(topo, name, kind=None,
    context=None, home=None)` 共享库，解析顺序（**永不静默取第一个**）：
    1. 精确匹配（kind 限定下唯一）→ 返回；2. 上下文收敛（context
    `{env?, cluster?, host?}` 过滤，实体侧未知字段不参与排除避免误杀）→
    范围内唯一 → 返回；3. 歧义报错列候选（实体全名 cluster__host__name +
    env/cluster 归属）→ 引导 clarify/改全名；4. 无匹配拒绝 + 提示先
    topo_query。实体获取与 P4 resolve_target 同源（topo 行 +
    `tools.topo_tools._all_services`，service 层需 home 读 services 索引）。
- **核心接入（修现状坑）**：`tools/runbook_exec.py` `resolve_target` 的
  service 层旧实现 `next(...)` **静默取第一个**（重名服务时命中错误实体）——
  改为调 `resolve_topo_ref(kind="service")`（重名报歧义不再静默）；
  host/cross_host/cluster/host_group 匹配逻辑保留（host 显式查表 →
  cross_host 旧层 → service 层，列表序行为与旧 next() 一致）。执行引擎新增
  scope 线程：`execute_runbook` 从 runbook 声明范围（env + 单值
  clusters/hosts）构建 `{env?, cluster?, host?}` 上下文，经
  `_run_one_step` / `_run_rollback_scenario` 传入 target 解析——唯一名行为
  不变（精确匹配优先，上下文只在重名时参与），范围收敛由 runbook 声明驱动。
- **回归面**：新增 `tests/tools/test_contracts.py`（加载器 5 + 结构层 10 +
  params 类型系统 12 + returns 3 + tests 命门 9）+ `tests/tools/test_topo_ref.py`
  （解析四步 + kind 限定/跨层歧义 + resolve_target 接入回归：唯一名行为不变、
  重名不再静默、runbook 范围 scope 收敛）。存量回归：runbook_exec / runbook_v2 /
  runbook_tools / runbook_create / runbook_lock / runbook_schedule /
  runbook_coverage / runbook_vault_refs / batch40_runbook_gate /
  batch44_runbook_semantic_secret / matrix / topo_tools / topo_v2 / topo_v4 /
  topo_status_sync / batch39_topo_sync / topo_update_contract / topo_discovery
  （373 passed / 6 skipped）+ topo_slash / batch33_topo_slash / runbook_progress
  （23 passed）。canonical 全量（约 30min）未跑，以回归套件 + 9131 实机为准。
- **实测验证**（9131 用新代码重启，VIGIL_HOME=/home/wpwang/.vigil +
  load_hermes_dotenv()，pid 2574978 常驻，令牌实测生效）：
  - 契约加载：contracts/ 缺失 → load_contracts = {}（空集）；
    手写合法契约（设计 13.1 rolling-restart，action=restart ∈ 23 词表）→
    load + validate 全过；非法契约（缺 tests）→ BLOCKED「tests 最少 2 个
    用例（1 正常 + ≥1 错误路径）——无用例 = 碰运气」；追加 permission 字段 →
    BLOCKED「禁止 permission 字段——权限 = 操作矩阵唯一裁决」；临时契约文件
    已清理（不留在用户 contracts/）。
  - topo_ref 三路径（真实拓扑 49 服务 4 主机，重名风险 = kubelet × 2 于
    beijing_aliyun/prod 两主机）：唯一路径 dify → service@LAPTOP-T2JA2ERE；
    上下文收敛 kubelet + host=39.106.217.32 / 39.107.92.54 → 各自命中；
    歧义路径 kubelet 无上下文 → BLOCKED 列候选
    `beijing_aliyun__39.106.217.32__kubelet` / `beijing_aliyun__39.107.92.54__kubelet`
    + 「不静默取第一个」；无匹配 ghost-service → BLOCKED + 「先 topo_query
    确认实体名」。resolve_target 接入同路径验证（kubelet 不再静默、ctx 收敛
    命中 39.107.92.54、dify 唯一行为不变）。
- **状态**：独立 feat commit（batch71），只含 2 新模块 + runbook_exec 接入
  + 2 测试文件 + OPS-DELTA.md 本条登记。

### 87. YAPL 主框架阶段 B——编译生成管线（2026-08-24，batch72）

- **背景**：YAPL 主框架阶段 A（契约基础设施，batch71，HEAD 1223804c）验收后，
  本批 = 阶段 B 编译生成管线：契约 → LLM 生成 Python 薄包装 → 沙箱跑 tests
  自证 → 资产审批 → 注册工具表。设计单一事实来源 = yapl-design.md 第十三章
  §13.4-13.6（2026-08-24 定案）。只做阶段 B，不做 runbook 第 24 动作 /
  type=tool 引用（阶段 C）、不做前端。开发在 branch v1.0（vigil-agent），
  发布基线 vigil-agent-release 不动。
- **新增模块（独立文件，核心面窄）**：
  - `tools/contract_runtime.py` —— 信任层：`run_specs(specs, target, *,
    home, runner)`（runner 注入 = 沙箱 mock；None = 真实
    `runbook_exec._run_spec` 通道：local/ssh/sudo/transfer/script_asset）+
    `any_spec_failed`（任一条非零退出 → execution_failed 映射）。
    生成代码只允许 import 白名单（runbook_handlers/runbook_exec/topo_tools/
    topo_ref/本模块）——执行细节全部落在信任层，LLM 不写执行逻辑。
  - `tools/contract_compile.py` —— 编译管线核心：
    - 生成规范 `_GENERATION_SPEC`（固定模板注入系统提示：参数校验逐项 /
      topo_ref 解析 / 分派调 handler / 结果包装 / 三错误码
      invalid_params|entity_not_found|execution_failed）+ 默认生成器
      `_default_llm_generate`（复用 `chat_api._create_chat_agent`，
      config model.default 路径，`enabled_toolsets=[]` 零工具，用量记
      `_last_llm_usage` 供 token 预算评估）。
    - 越界扫描 `_overreach_scan`（12 项禁止模式：subprocess/os.system/
      socket./urllib/requests./eval(/exec( 等）+ ast.parse 语法检查先行
      （语法错直接重试不进沙箱）。
    - 沙箱自证 `sandbox_selftest`：独立子进程 + TemporaryDirectory（不落盘
      主目录），合成沙箱拓扑（hosts+services，**entity_not_found 用例专属
      引用不建实体**——验证拒绝路径；同一实体既正常又不存在 = 契约自相矛盾
      沙箱按存在建、该用例自然失败）；handler 通道 monkeypatch
      `runbook_handlers.generate_commands` + 注入 runner（
      `handler: {status: failed}` 用例 → exit_code 1 验证错误传播）；60s 超时
      防死循环；失败带用例名/期望/实际 → LLM 重试（上限 3 次）。
    - 注册：编译代码落 `contracts/compiled/<name>.py` chmod 0600（只读语义，
      人工不改）+ `registry.yaml` 原子记录（name/action/status/compiled_at/
      approved_by/path/toolset/schema）+ 动态 `registry.register(name,
      toolset="contract", schema, handler, emoji="📜")`；失败回滚
      （unlink+deregister）。重名拒绝（registry.yaml 条目 或 统一工具命名空间
      冲突）→ register_conflict。
    - 工具可见性：schema 由契约 params 自动生成（enum→enum、list→array、
      topo_ref→string）；启动钩子 `ensure_compiled_tools_registered`
      （model_tools 模块级调用，幂等热加载 registry.yaml 已注册契约，注册后
      按 registry generation 增量对 `get_tool_definitions` 可见）；执行入口
      `_tool_handler_for` = 每次现读代码文件（只读语义）+ 自动附加
      time_elapsed + JSON 串返回（registry 工具管线只接受字符串结果）。
  - `hermes_cli/subcommands/contract.py` —— `vigil contract compile
    <name> [--yes] [--max-retries N]`（--yes = 非交互显式同意注册审批，
    置 VIGIL_INTERACTIVE + approval_callback 返回 once）+ `vigil contract
    list`（registry.yaml 记录查询）；挂载进 `hermes_cli/main.py`。
  - `toolsets.py` 新增 `"contract"` toolset（tools: []，数据存在性门控——
    无注册契约零工具零 footprint）；`hermes_cli/tools_config.py` cli 平台
    默认启用列表加 `"contract"`（显式列表权威，不覆盖用户选择）。
  - **凭据纪律（设计 13.2 补）**：生成代码零凭据接触（薄包装只引用实体，
    SSH/远程执行走 handler 凭据注入链）；params 校验器阶段 A 已拒绝
    password/secret/token/key 类参数（contract_tools 校验器），本批生成规范
    同步禁止生成代码含凭据逻辑。
- **动态注册机制选择（OPS-DELTA 注明）**：工具发现机制原只认模块顶层
  `registry.register`（import 时静态）；编译工具是运行时产物，动态注册走
  `registry.register(toolset="contract")` + 启动钩子热加载——注册后按
  generation 增量对 `get_tool_definitions`（model_tools.py:305 语义）可见，
  容器/热重载无影响（每次现读代码文件，只读语义）。重名与内置工具表统一
  命名空间互斥。
- **回归面**：新增 `tests/tools/test_contract_compile.py` 24 用例（编译状态机
  各失败路径：contract_not_found/契约校验失败/generation_failed 重试超限/
  语法错重试/越界拒绝重试/自证失败重试带用例名引导/审批拒绝不注册/tirith
  block 拒/register_conflict 重名拒绝；沙箱自证：全过/失败带用例名/超时杀
  死；注册：registry.yaml 记录/0600 落盘/schema 生成/动态注册后
  get_tool_definitions 可见/ensure 钩子幂等热加载；mock 生成器注入不真调
  LLM）。存量回归：test_contracts/test_topo_ref/test_runbook_exec/
  test_script_assets/test_runbook_v2/tools/create/lock/schedule/coverage/
  vault_refs/test_batch40_runbook_gate/test_batch44_runbook_semantic_secret
  （294 passed）+ test_approval/test_command_guards/test_batch47_sudo_approval/
  test_topo_tools/test_topo_v4/test_batch39_topo_sync（193 passed）。
  实机修复 2 个单元测不到的问题：沙箱 ENF 专属引用被误建实体（
  `present | (absent - present)` → 只建正常用例引用）；registry dispatch 给
  handler 传 task_id 等 kwargs + 工具管线只收字符串结果（handler 改
  `**kwargs` + JSON 串返回）。
- **实测验证**（9131 用新代码重启，VIGIL_HOME=/home/wpwang/.vigil +
  load_hermes_dotenv()，VIGIL_DASHBOARD_SESSION_TOKEN=vigil-b70-test-9131）：
  - 真编译全链路：`~/.vigil/contracts/verify-service.yaml`（action=verify，
    returns {ok: boolean}，3 用例含 entity_not_found + handler failed）→
    `vigil contract compile verify-service --yes` 一次通过：LLM 生成合规薄
    包装（白名单 import + call(params, *, home, context, runner) + 三错误码 +
    跑通沙箱 3/3）→ 资产审批（approved_by=wpwang）→ 注册。token 消耗：
    deepseek-v4-flash 1 次 API 调用，in=24336 out=2375 total=26711，
    estimated_cost_usd=0.003633，latency 17.4s，prompt cache 命中
    3200/24336（13%）——单契约编译一次约 2.7 万 token / <$0.004（预算评估
    参考：普通契约 ≈ 25k prompt 模板 + 2-3k 生成）。
  - 注册后可见 + 直调：新进程 import model_tools（启动钩子日志
    `compiled contracts re-registered: 1`），`get_tool_definitions(
    enabled_toolsets=["contract"])` 含 verify-service；`handle_function_call`
    直调 target=dify（真实拓扑唯一名，docker_compose 服务）→
    `{"ok": true, "time_elapsed": 0.085}`（真实 docker inspect 通道）；
    错误路径 ghost-svc → entity_not_found、缺参 → invalid_params。
  - 测试契约已全部清理（contracts/ 恢复到实测前状态），9131 已用新代码
    重启常驻。
- **状态**：独立 feat commit（batch72），只含 4 新文件 + 4 文件接入 +
  1 测试文件 + OPS-DELTA.md 本条登记。

### 88. YAPL 主框架阶段 C——runbook 第 24 动作 + 嵌套编排 + 3 补丁（2026-08-24，batch73）

- **背景**：YAPL 主框架阶段 A（契约基础设施，batch71）/ 阶段 B（编译生成管线，
  batch72）验收后，本批 = 阶段 C 编排层闭环：runbook 第 24 动作（嵌套引用）+
  子 runbook 范围继承 + 环状防护 + 回滚联动 + 变量不跨层 + type=tool 工具引用；
  外加 3 补丁（凭据纪律校验器 / topo_update credentials 数组 / CLI 命令索引）。
  设计单一事实来源 = yapl-design.md §13.5/§13.6 + §13.2（2026-08-24 定案）。
  开发在 branch v1.0（vigil-agent），发布基线 vigil-agent-release 不动。只做
  阶段 C 主体 + 3 补丁：不做引导注入完整清单（任务 5 只覆盖 4 命令）、不做期望
  状态、不做前端。
- **runbook 第 24 动作（词表 + 校验器）**：
  - `schemas.yaml actions` +1 = `runbook`（词表配置化，照 9.6 加值流程）；矩阵
    setup 四模板不动——`matrix_data._build_cells` 显式跳过 runbook 动作，未配
    档位 = 漏配默认 approve（保守，`get_level` 未配 action → approve 语义已
    有）；手动 `vigil matrix set runbook <env> <level>` 仍可显式配档位。
  - 校验器（runbook_tools `_validate_runbook_v2` 分层扩展）：
    - 结构层：`_ACTION_CONTRACTS["runbook"]` = `{required: [ref, type],
      typed: {ref: str, type: runbook_ref_type}}`——ref 非空字符串、type 枚举
      runbook|tool 必填不猜；
    - 关系层 `_validate_runbook_refs`：type=runbook → ref ∈ runbooks/ 文件
      （不存在 = 报错引导先 runbook_create）；type=tool → ref ∈ 契约编译注册表
      （contracts/registry.yaml，未注册 = 报错引导先 vigil contract compile）；
      引用自己 / 互相引用（跨文件全图 DFS）→ 拒绝并报环路路径（如
      `a → b → a`）；
    - 变量不跨层：子 runbook 文件内 `{{ steps... }}` 只引用自身步骤（校验器
      不跨文件解析）；父向子传值走 params 显式传入——父 steps 的 params 里的
      `{{ }}` 由父执行时解析后传入子（`_run_one_step_impl` 先 substitute_params
      再分派 runbook 动作）。
    - 引用层修正：runbook 动作的 params 不是拓扑 target（type=tool 时 target
      是普通契约参数）→ 跳过 runbook 步骤的 target 引用校验（否则
      `target: nginx` 这类工具参数会被误拒"不在拓扑表"）。
- **执行器（runbook_exec 嵌套编排）**：
  - `_run_one_step_impl` 对 `action: runbook` 内联分派 `_run_runbook_step`：
    - type=runbook：`approve(env, "runbook", …)` 查 runbook 动作档位（未配 =
      漏配默认 approve，保守）；`_run_sub_runbook` 加载子 runbook（_load_runbook
      路径）→ 再校验（引用校验通过前提下）→ 范围继承 `_merge_sub_scope`：子
      声明缺省字段从父执行上下文补（env/cluster/host，同时是 resolve_topo_ref
      的 context）；子声明了 = 子声明优先但受父约束（父已有该维度且子声明不同
      → 拒绝）。子步骤顺序执行，每步照常走现有执行引擎（审批门查矩阵 /
      expect / on_failure——无豁免）；子执行记入 ledger（runbook/version/范围
      来源 inherited|declared/结果，nested 标记）。
    - type=tool：调阶段 B 注册工具（contract_compile.load_compiled_call，
      context=scope 透传）——run_script 资产预审语义：交互执行 execute（不查
      矩阵，工具已资产审批）；定时触发走父 runbook 资产审批豁免 + 事后审计；
      参数传参照工具 params（父 steps 的 `{{ }}` 解析后传入）。
  - 回滚联动：子失败 → 子的 on_failure 先生效（stop/rollback 子自己的场景，
    子执行失败本身已触发子的回滚）→ 子最终失败 → 父引用步骤视为失败 → 父
    步骤的 on_failure 生效（父 on_failure: rollback 时，父回滚只处理父已完成
    的其他步骤）。步骤循环抽成 `_run_steps_loop` 父/子共用。
  - 单独运行无范围声明（collect_scope=True）：执行器返回待收集状态
    （result=needs_scope / status=scope_collection），由调用方 clarify 用户
    （目标 env/cluster/host，单台确认模式复用）后带范围重跑；用户无响应/拒绝
    = 不执行（fail-closed）。LLM 工具 `runbook_execute` 与 web 执行入口
    （web_server POST）均 collect_scope=True；web 侧对 needs_scope 补一个
    runbook_done(scope_collection) 终态事件，SSE 不按"异常结束"收尾。
  - 环状防护运行时兜底：执行栈深度上限 `_MAX_NESTING_DEPTH=10`（depth 贯穿
    步骤循环/回滚/嵌套分派），超限 fail-closed 拒绝执行——防校验遗漏 / 文件
    被外部手改后成环的死循环。
  - 顺修存量真 bug：runbook 级 `on_failure: {rollback: 场景}`（dict 形态）此前
    被 `str()` 串成 `"{'rollback': …}"` 导致 `_resolve_on_failure` 拒收——父
    回滚联动依赖 dict 形态，改为原值传递（stop/continue/rollback 字符串不受
    影响）。
- **补丁 1——凭据纪律校验器（contract_tools.validate_contract）**：params
  参数名匹配 password/passwd/secret/token/key/api_key/private_key/access_key/
  credential/auth_key/pwd 等词（大小写不敏感、`_` 视作词边界，复合名
  ssh_key/api_token/db_password 也拒；monkey/keyboard 这类含 key 词的正常名不
  误杀）+ default 值形态疑似凭据（`password=…`/`token: …`/PEM 私钥块）→ 报错
  "契约 params 不支持凭据参数（凭据走拓扑 credentials / secret 引用，设计
  13.2）"——引导改用 topo_ref/vault 引用。
- **补丁 2——topo_update credentials 数组（topo_tools，堵 LLM 绕行）**：
  背景 2026-08-24 dogfood：topo_update 字段白名单不支持 v0.4 credentials 数组
  → LLM 会话里无法表单更新凭据 → 绕行直接编辑 topology.yaml（敏感字段绕行更
  危险）。本补丁：host 行 credentials 数组更新（v0.4 结构
  [{type: ssh_key|secret, ref, user?, port?}]），白名单字段补 credentials；
  校验 type 枚举 / ref 必填非空 / 数组形态 / port 1..65535；非 host 实体拒绝
  （服务凭据走所属 host）；写盘走既有 topology.yaml 落盘路径 + 审计
  （credentials_updated + credentials_refs 进返回 audit，照现有 topo_update
  审计）；**凭据纪律**：ref 只收引用（路径 / vault:… 引用 / 标识，字符集
  `[A-Za-z0-9._~/:\-]+`，含空白/= 的疑似明文被拒），不接受明文凭据值写入；
  校验失败不落盘。
- **补丁 3——运维 CLI 命令索引（LLM 引导注入）**：dogfood §F 教训（不工具化/
  不索引 → LLM 人肉推理空耗 40K token）。注入点 = 现有 L1 注入槽位：topo
  memory provider 的 system prompt block（plugins/memory/topo，
  `render_topo_block` 末尾追加 `_CLI_COMMAND_INDEX` 一行）——随 TOPO 段注入，
  数据存在性门控不变（非 ops profile 零 prompt 变化）。本批至少覆盖
  contract compile/list + topo-discover + topo-update + matrix show 四个
  （完整运维 CLI 清单后续 dogfood 补）：索引如实标注——`vigil topo-discover`、
  `vigil contract compile <name>`、`vigil contract list`、`vigil matrix show`
  是 CLI 命令；topo-update 无 CLI 子命令（现成路径 = topo_update 工具，表单
  更新不编辑 topology.yaml），如实标工具路径避免误导 LLM 找不存在的命令。
- **回归面**：新增 `tests/tools/test_batch73_yapl_stage_c.py` 43 用例（校验器：
  ref 必填/type 枚举/子 runbook 不存在引导/自引用拒绝/环状拒绝含环路路径/工具
  未注册引导/注册通过/范围四字段全可选/变量引用文件内自洽；执行器：嵌套顺序
  + 范围继承 inherited|declared/子声明超出父范围拒绝/子步骤查矩阵无豁免/runbook
  动作档位门/子失败子的 on_failure 先生效/父回滚联动/type=tool execute 豁免 +
  上下文透传/未注册运行时拒绝/变量不跨层/跨层显式传参/无范围待收集/深度兜底
  断链；补丁：凭据参数名 10 组 + 明文默认值拒绝 + 正常参数通过 + 词边界不误杀/
  topo_update credentials 增改删 + 非法形态拒绝 + 非 host 拒绝 + 校验失败不
  落盘/CLI 索引注入 + 无拓扑静默）。存量回归：runbook_exec/runbook_v2/
  runbook_tools/create/coverage/lock/schedule/vault_refs/batch40/batch44
  （196 passed）+ contract_compile/contracts/topo_tools/topo_v4/topo_ref/
  topo_update_contract/matrix/terminal_matrix/batch39/topo_discovery（254
  passed，1 例 test_sudo_stdin_guard_still_blocks_before_matrix 为存量隔离性
  flake，HEAD batch72 同组合复现，与本批无关）+ test_batch59_runbook_exec_api
  （fake_execute 补 collect_scope kwarg）/test_runbook_progress/
  test_topo_provider/test_batch33_topo_slash（33 passed）。
- **状态**：独立 feat commit（batch73），只含 1 新测试文件 + 8 文件接入 +
  OPS-DELTA.md 本条登记。

### 89. runbook_create 强制 v0.2 + 远端 sudo root 免密 + K3S runtime 误判（2026-08-24，batch74）

- **背景**：用户 dogfood 实证 3 个 bug，一批修复。开发在 branch v1.0（vigil-agent），
  发布基线 vigil-agent-release 不动。前端无改动。
- **任务 1——runbook_create 强制 v0.2（最高优先级）**：用户让 Vigil 写"探查 argocd
  状态，未运行则恢复"的 runbook，Vigil 生成 v0.1 格式（steps[].commands 裸 SSH
  命令），执行时 LLM 直接 terminal 跑——resolve_topo_ref / 动作词表 / 矩阵动作裁决
  全部被绕过（topo_ref 被击穿，只剩 terminal 审批门兜底）。这是 §六"LLM 绕行入口"
  教训的第三个实例（schema 只写"v0.2 推荐"不强制，v0.1 保留本意是存量兼容，却成了
  LLM 偷懒路径）。修复：
  - `runbook_create` 内、`_validate_runbook` 前加 v0.2 强制门：`is_new`（同名文件
    不存在）且提交 v0.1（steps 含 commands）→ 直接 tool_error："新 runbook 必须
    使用 v0.2 声明式格式…v0.1 commands 格式仅供 overwrite 存量文件"；动作词表从
    schemas.yaml actions 读（含第 24 动作 runbook），不硬编码；
  - 存量判定 = `exists and not _is_v2_runbook(磁盘文件)`：存量 v0.1 + overwrite=true
    + 提交 v0.1 → 放行（v0.1 仅此一途）；磁盘已是 v0.2 + 提交 v0.1 → 即使 overwrite
    也拒绝（不能把 v0.2 降级成 v0.1）；存量文件不可读 → 保守拒绝；
  - `_DEFAULT_CREATE_SCHEMA` description："v0.2（推荐）"→"v0.2（唯一允许的新建格式）"
    + "新建 runbook 必须用 v0.2；v0.1 commands 格式会被拒绝，仅供 overwrite 存量文件"；
  - `_validate_runbook` 不动（存量 v0.1 加载/校验/checklist 语义保留）。
  - 新测试 `test_batch74_runbook_v2_required.py`（6 例）：新建 v0.1 拒绝（含
    "必须使用 v0.2"、动作词表含 runbook）、新建 v0.2 通过（过资产审批 + 预审标记）、
    存量 v0.1 overwrite 放行、存量 v0.2 提交 v0.1 拒绝（磁盘保持 v0.2）、argocd 同款
    v0.2（query/scale/restart, target: argocd-server）通过。
  - 存量测试适配（v0.1 语义测试全部改为"先放 v0.1 存量 + overwrite=true"）：
    test_runbook_create（secret 扫描 4 例 + deploy checklist 1 例走 overwrite 存量，
    其余 create 机制用例改 v0.2 + fixture 补 approvals.mode=off）、
    test_batch44_runbook_semantic_secret（fixture 预置 v0.1 存量 + overwrite）、
    test_runbook_v2 双 schema（v1 用例预置存量 + overwrite）、test_matrix
    test_v1_unaffected 同理。
- **任务 2——远端 sudo root 免密 + 非 root 兜底（sudo_tool._run_remote_sudo）**：
  原实现无脑要求 vault 类型凭据，把最常见场景（root + 无密码 key，如 aliyun_nopass.pem）
  拦了。修复：
  - `cred_type == "ssh_key"`：user==root → `_ssh_run(…, "sudo -n <command>")` 直通
    （root 的 sudo 默认免密，-n 保证不卡交互）；exit≠0 且 stderr 命中
    `_SUDO_AUTH_FAILURE_HINTS`（"a password is required" 等）→ 可操作错误"远端 sudo
    需要密码…请配置 vault 凭据或手动执行"；命令自身失败（无密码提示）→ 直通返回
    结果，不误报；
  - `cred_type == "ssh_key"`：user!=root → 先试 `sudo -n`，成功直通（可能配了
    NOPASSWD），认证失败 → 报错引导配 vault（保持现状语义）；
  - vault → 现有 `sudo -A` + scp askpass 流程不变；askpass 类型仍 fail-closed；
    任何情况不猜密码、不翻 ~/.ssh/、不换用户名重试（§Q/§AD 教训）。
  - 新测试 `test_batch74_sudo_root_nopass.py`（7 例）：root+ssh_key 命令形态
    `sudo -n <command>` 且无 askpass/vault 注入、密码提示 → "配置 vault" 引导、
    vault 走 `sudo -A` 不变、非 root 成功直通 / 失败引导、命令自身失败直通、
    askpass fail-closed。test_sudo_exec 的"ssh_key 不支持远端注入"用例改为 askpass
    类型（ssh_key 已支持）。
- **任务 3——K3S runtime 误判（topo_discovery）**：原实现
  `runtime = "k3s" if runtime == "unknown" else runtime`——kubectl 可用且 runtime
  还是 unknown → 一律标 k3s，不探测任何 k3s 特征，标准 kubeadm 集群（containerd +
  cilium）被误标 k3s（用户阿里云 beijing 集群实证，手动改数据后 re-discover 复发）。
  修复（两条独立 probe，任一命中即 k3s，兼容远端 shell）：
  - `kubectl version -o json 2>/dev/null | grep -o 'k3s[0-9]*' | head -1`（k3s 的
    server gitVersion 形如 v1.x.x+k3s1）；
  - `ls -d /etc/rancher 2>/dev/null`（k3s 安装路径）；
  - 两条都空/探测失败/超时 → 保守标 `kubernetes`（宁可把 k3s 漏标成 kubernetes，
    不可把 kubeadm 误标 k3s）；probes["k3s"] 记 detected / not-detected 供审计；
  - `_host_roles_for` 把 kubernetes 并入 worker 分支（标准 k8s 节点 role 不落空）；
    存量 topology.yaml 已有 runtime 不受影响（discover 只改新发现，合并时治理字段
    不覆盖）。
  - 新测试 `test_batch74_k3s_detect.py`（6 例）：gitVersion +k3s → k3s、标准 v1.30.x
    → kubernetes（role 仍 worker）、kubectl 不可用 → unknown（落盘 ["bare"]）且不跑
    k3s 探测、/etc/rancher 存在 → k3s、两特征都失败 → 保守 kubernetes、docker 可用
    时 docker 优先。test_topo_discovery 的 `test_discover_docker_unavailable_…`
    断言 ["k3s"] → ["kubernetes"]（默认 runner 无 k3s 特征）；FakeRunner 改最长前缀
    优先（"kubectl version" override 覆盖泛化 "kubectl" key）。
- **顺修存量测试 bug（test_approval_interrupt）**：两个用例直接给
  `approval._get_approval_config` 赋 lambda（改 timeout/deny 策略）且从不恢复 →
  泄漏成"mode 恒 manual"，全套件后续所有走资产审批的用例（v0.2 runbook 创建、
  ops_permissions_guard / cron_approval_mode / approval_mode_parity 等）被错误
  BLOCK（基线 HEAD 同组合 51 例失败，与本批功能无关，但 v0.2 runbook 用例因此
  全量跑必挂）。setup 保存原函数、teardown 恢复——顺修后 51 → 32 例，本批相关
  用例（runbook_v2 / matrix / approval_mode_parity / script_assets）全转绿；
  剩余 32 例（terminal cwd/spill、command_guards、cron_approval_mode、
  ops_permissions_guard、hardline_blocklist 等）与基线 HEAD 完全一致 = 存量
  全量顺序干扰，单跑全绿，与本批无关。
- **验收**：3 个新测试文件全绿（6+7+6）；实测 runbook_create 提交 v0.1 新格式
  → "新 runbook 必须使用 v0.2"拒绝信息出现，argocd 同款 v0.2（action:
  query/scale/restart，target: argocd-server）→ created 落盘 version:2 +
  approved_by 预审标记；回归 tests/tools 全套 6418 passed / 32 failed（32 与
  基线 HEAD 同文件同用例，存量干扰）；runbook / sudo / topo / approval 相关
  套件零回归。
- **状态**：独立 feat commit（batch74，1 个 commit），只含 3 个工具文件 + 6 个
  存量测试文件适配 + 1 个存量测试顺修（test_approval_interrupt）+ 3 个新测试
  文件 + OPS-DELTA.md 本条登记。

### 90. v0.2 runbook 编写引导三件套（skill + 样例 + 工具描述）（2026-08-25，batch75）

- **背景**：batch74 强制 v0.2 后 LLM 不会写 v0.2 runbook——它加载的
  runbook-authoring skill 内容还是 v0.1 时代（明写 "schema v0.1"、教 steps 用
  commands）、ops_samples 的 3 个样例全是 v0.1 commands 格式、tools list 里也
  没有 v0.2 完整示例，最终产出 v0.1 → 被 batch74 拒绝。根因：强制校验是"守门"，
  但没有"指路"材料。本批 = 三件套同步更新，给 LLM 和用户"看着示例写出正确 v0.2"。
  开发在 branch v1.0（vigil-agent），发布基线不动；禁止读 ~/.hermes。
- **任务 1——runbook-authoring skill 重写为 v0.2**：文件
  `~/.vigil/skills/vigil/runbook-authoring/SKILL.md`（运行时副本，开发仓库无对应
  源——已确认 skills/ 与 optional-skills/ 均无 runbook-authoring，差异记本条目）。
  改动：
  - 首段 "schema v0.1" → "schema v0.2 声明式动作"，注明 v0.1 commands 格式会被
    拒绝、仅供 overwrite 存量文件；
  - "runbook_create 参数要点"整段替换为 v0.2 语法：顶层结构 YAML（version/kind/
    env/triggers/clusters/steps/rollback）+ 24 动作词表 + 必填 params 表（照
    `_ACTION_CONTRACTS`）+ 校验器规则（expect 是对象不是字符串、target 必须拓扑
    实体、triggers/schedule 互斥、无 permission 字段）；
  - 删掉 "SSH 命令形态" 与 commands_if_needed 等 v0.1 条目，换为"执行用
    runbook_execute，命令由执行器生成，LLM 永不接触命令语法"；
  - 保留触发场景、重写流程（load → topo_query → session_search → create
    overwrite）、坑（重写保留已确认触发词）、checklist 门控；
  - 新增"完整示例"块（指向 ops_samples 的 v0.2 样例）。
- **任务 2——ops_samples 新增 v0.2 样例**：`hermes_cli/ops_samples/runbooks/
  argocd-server-check-restart.yaml`（argocd-server 状态探查与恢复：query 前置 →
  fetch_log 探查 → scale 恢复，Gatekeeper require-pod-limits 坑写进 note +
  on_failure: stop，rollback 仅人工预案）。文件命名与 name 字段一致（校验器
  name-filename 一致性要求；任务书草案的 `-v02` 后缀会破坏 load-by-name，故用
  干净名）。target/cluster 对齐 ops_samples 种子拓扑（k3s-prod 集群、argocd
  服务，样例环境实体；任务书草案的 beijing_aliyun/argocd-server 是用户真实集群，
  样例统一用占位拓扑并在注释注明"target 需 topo_query 确认"）。任务书草案里的
  expect 字符串形态不符合校验器（expect 必须是 {target: 检查通道, 谓词} 对象），
  全部改为对象形态。直调 `_validate_runbook_v2` + runbook_load 通过（含引用层）。
- **任务 3——runbook_create description 补 v0.2 完整示例**：`_DEFAULT_CREATE_SCHEMA`
  description 末尾追加精简完整示例（name/title/version/kind/env/triggers/steps
  restart + expect 对象 + rollback，一行 JSON 形态控制 token），并注明"target
  必须是拓扑表实体名，先 topo_query 确认"。
- **存量测试适配**：test_runbook_tools `test_load_list` 3 → 4 个样例（新样例进
  rb_home 列表）；新增 `test_v02_sample_validates_against_ops_topo`（铺 ops 拓扑
  + 服务 + v0.2 样例 → `_validate_runbook` + load 回读断言 v0.2）；OPS-VERIFY.md
  手测脚本样例数 3 → 4。
- **验收**：skill 无 "schema v0.1" 残留、v0.2 语法完整（顶层结构 + 动作表 +
  示例）；样例 `_validate_runbook_v2` 直调通过；description 含 v0.2 示例；
  pytest runbook 全家桶 + ops_init/ops_first_install 178 例全绿，宽回归 542
  passed / 9 skipped；实测（模拟 LLM 视角：读 skill + 样例 + description → 写
  argocd v0.2 runbook）→ runbook_create created 落盘 + runbook_load 回读
  version:2。
- **状态**：独立 feat commit（batch75，1 个 commit），只含 1 个样例文件 + 1 个
  工具文件（description）+ 1 个测试文件 + OPS-VERIFY.md + OPS-DELTA.md 本条
  登记；skill 运行时副本（~/.vigil）随本批同步，仓库无对应源故不在 commit 内。

### 91. 执行器链路三件：managed_by 推断 + runbook_create 解除存在性门控 + skill 引导硬规则（2026-08-25，batch76）

- **背景**（用户 dogfood 实证 2026-08-25 上午）：会话写"探查 argocd 状态并恢复"
  v0.2 runbook（action: query/fetch_log/scale, target: argocd-server），暴露三个
  递进问题：
  - A 架构断层：argocd-server 实体档案与 L2 服务行都无 managed_by（v0.3 存量 k8s
    实体只写 type=k8s-service，无 managed_by 字段；v0.4 发现写入的 managed_by 是
    L2 服务行字段，但同名合并/旧数据可能缺）→ `runbook_exec._resolve_target` 返回
    空 → `runbook_handlers` 退化为 "bare" → kubectl 通道丢失，query 退化成 ps aux、
    scale/start 报 UnsupportedCommand。
  - B 引导死锁：runbook_create/load/checkpoint 三工具全挂
    `check_fn=check_runbook_requirements`（runbooks/ 有 yaml 才可用）→ 目录空时
    runbook_create 根本不出现在 LLM 工具列表 → LLM 想创建 runbook 没工具可用 →
    绕行直接写 YAML 文件（落盘成功但 0 个审批标记，绕过资产审批门 + 三层校验器）。
  - C 引导不足：batch75 后 LLM 写 runbook 仍读 8 段 runbook_tools.py 源码——样例
    用占位拓扑（k3s-prod/argocd）套不到真实拓扑，从源码反推校验规则陷入兔子洞。
- **任务 1——执行器 managed_by 推断（`tools/runbook_exec.py`）**：`_resolve_target`
  return dict 前加推断，优先级 **显式 managed_by > snapshot.by_runtime 键 >
  type 映射 > 空**，只补缺失不覆盖显式值：
  - by_runtime 键（kubectl/docker_compose/docker/systemd）——topo_discovery 写
    `by_runtime = {managed_by: 块}`，键即发现期 managed_by，v0.4 数据 L2 缺字段
    但 L3 快照在时最准；
  - type 映射与 topo_discovery 写入对齐：`k8s-*`/kubectl/k8s → kubectl（v0.3 存量
    k8s 实体 type=k8s-service/k8s-deploy）、docker/container → docker、
    docker_compose/compose → docker_compose、systemd/service → systemd；
  - 未知 type（app 等）→ 保持空串不瞎猜。集群/host_group 目标维持 ""（无 managed_by
    概念）。核实结论：topo_discovery.py:1443 的 kubectl 写入路径本身执行且 L2 写盘
    保留（只 pop `_host`），用户数据缺失是 v0.3 存量写入格式没有该字段——执行器侧
    推断是系统级修复，覆盖新旧两代数据。
- **任务 2——runbook_create 解除存在性门控（`tools/runbook_tools.py`）**：
  `runbook_create` 注册去掉 `check_fn=check_runbook_requirements`（工具始终在 LLM
  工具列表——目录空正是它该工作的时候）；`runbook_load`/`runbook_checkpoint`/
  `runbook_execute` 保留门控不动（读/执行依赖存量数据，空目录工具不出现合理）。
  安全不削弱：create 内部 `exists and not overwrite` 同名保护、`_validate_runbook`
  三层校验、v0.2 资产审批门（approve/矩阵/凭据纪律）全部保留，`_create_handler`
  不直接调 check_fn，去掉注册门控不影响内部检查。
- **任务 3——skill 两条硬规则（`~/.vigil/skills/vigil/runbook-authoring/SKILL.md`
  运行时副本，仓库无对应源）**："创建/重写流程"一节顶部加两条：① target 必须
  topo_query 拿到真实实体名（样例/ops_samples 是占位拓扑，用真实名替换，别照抄
  别编）；② 校验规则本 skill 已写全、禁止读源码（不确定语法直接 runbook_create 试，
  报错信息自带修复指引，不读 tools/runbook_tools.py 反推）。
- **新测试**（tests/tools/）：
  - `test_batch76_managed_by_infer.py`（5 例）：type=k8s-service → kubectl、
    type=docker → docker、显式 managed_by=systemd 不被覆盖、未知 type=app → 空、
    L3 快照 by_runtime.kubectl → kubectl；
  - `test_batch76_create_always_available.py`（4 例）：空 runbooks/ → runbook_create
    在 LLM 工具列表 + runbook_load 门控隐藏；非空目录 → 三工具全在；空目录
    runbook_load 调用报无数据 + create 注册无 check_fn。
- **验收**：两个新测试文件 9 例全绿；回归 runbook 全家桶（test_runbook_*.py +
  test_batch76_*.py）171 passed、test_runbook_exec/tools/topo_discovery/create/
  batch74 147 passed 零回归；E2E 实测（模拟 LLM 视角：空 runbooks/ → create 工具
  可见 → 创建 v0.2 argocd-check-restart → approvals.mode=smart + 矩阵 execute →
  资产审批通过落盘带 approved_at/approved_by/approved_version → resolve_target
  (argocd-server) = managed_by kubectl）；skill 两条硬规则 grep 在场。
- **状态**：独立 feat commit（batch76，1 个 commit），只含 2 个工具文件 + 2 个新
  测试文件 + OPS-DELTA.md 本条登记；skill 运行时副本（~/.vigil）随本批同步，仓库
  无对应源故不在 commit 内。

### 93. 执行器 expect 检查四修——通道语义 / 默认轮询 / 显式覆盖 / rollback 审批强化（2026-08-25，batch78）

- **背景**（2026-08-25 实测，runbook_execute 真实执行 argocd-full-stack-recovery）：
  执行器跑通（7 个审批门、远端 kubectl 触达），但 expect 检查连环误判：
  - ① 通道语义错：expect `{target: kubectl, body_contains: "1/1 Running"}` 生成的
    检查命令是 `kubectl get deployment/<obj> -o wide`——deployment 的 READY 列
    格式是 "1/1 1 1"（ready/up-to-date/available），不是 "1/1 Running"（那是 pod
    格式）。任何 runbook 写 "1/1 Running" 配默认 kind 都必失败。
  - ② 零重试：expect 检查只跑一次，失败立即 failed——scale 后 pod 还在
    ContainerCreating/拉镜像/过 readiness 就判失败（kubectl rollout status 有
    --timeout=120s 就是为这个）。
  - ③ 误判触发回滚：restore-server 的 expect 误判失败 → on_failure
    {rollback: rollback-all} → rollout undo 真执行（prod！）。审批弹窗虽标了
    rollback[...] 前缀，但用户惯性批准"恢复性"操作——回滚步骤的审批强度与正常
    步骤相同，未区分。
- **任务 1——kubectl 检查通道默认查 pod（`tools/runbook_handlers.py`）**：
  `generate_expect_check` 的 kubectl 分支默认 kind 改 **pod**（"1/1 Running" 是
  pod 语义，pod 状态是最终真相）；命令按 kind 区分：pod（默认）→
  `kubectl {ns}get pods -l app={obj} -o wide`（pod 名带随机后缀，按 label 查）、
  deployment → `get deployment/{obj} -o wide`（保持现状）、statefulset/sts →
  `get sts/{obj} -o wide`、其他 kind → UnsupportedCommand 报错引导
  （可用值: pod/deployment/statefulset）。
- **任务 2——expect 默认轮询 + 显式覆盖（`tools/runbook_exec.py`）**：
  `_run_one_step_impl` 的 expect 段加轮询：变更类动作
  （start/stop/restart/reload/enable/disable/reboot/shutdown/deploy/rollback/
  scale/decommission/backup/restore/install/upgrade/remove）默认
  attempts=12×interval=10（2 分钟窗口，幂等等待就绪）；只读类动作
  （query/fetch_log/verify/transfer_file/run_script/apply_config/runbook）默认
  单次快查。`expect.retry: {attempts, interval}` 显式覆盖（优先级高于默认）；
  `expect.retry: false` 关闭轮询；缺省按动作类别。每次结果记录
  （expect.attempts + detail 注明"第 N/M 次尝试通过"）；全败 →
  status=failed + error 含"N 次尝试均失败，最后一次: ..."。轮询只加等待不改
  命令内容；失败仍 fail-closed。sleep 用 time.sleep（同步路径，交互可中断）。
- **任务 3——rollback 步骤审批强化（`tools/runbook_exec.py`）**：
  防用户对"恢复性"操作惯性批准（rollout undo 是 L3 高风险操作，实证 expect
  误判触发 undo 且被批准）。`_run_one_step_impl` 加 phase 参数：phase=="rollback"
  时 desc 前缀加 `"⚠ 回滚（rollback 场景 {name}）: "` 并传
  force_confirmation=True；`_step_approval` 增加 `force_confirmation` 参数
  （True 时 decision["require_confirmation"]=True，无视矩阵档位，覆盖
  approvals.mode=smart 的自动批准；require_confirmation 语义 = 不提供
  session/永久 allowlist，每次都弹人工确认）。rollback 场景执行路径
  （_run_rollback_scenario 传 phase="rollback"）自动带 force，不用 runbook 声明。
  scheduled 豁免语义不变：核实 _run_steps_loop 在 scheduled 下同样会回滚，
  但 _approve 的 scheduled 短路先于 force 生效——仅交互路径加强，定时路径仍
  走资产审批豁免。
- **任务 4——argocd-full-stack-recovery 用例同步（`~/.vigil/runbooks/` 运行时
  数据，仓库无对应源）**：preflight/check-server/check-repo/check-controller/
  restore-server 的 expect 全部显式加 `kind: pod`（不依赖默认值，语义清晰）；
  restore-server 加 `retry: {attempts: 12, interval: 10}`（scale 后 pod 拉起
  要时间，显式声明）。
- **新测试**（tests/tools/test_batch78_expect_fixes.py，15 例，四组）：
  ① 通道语义——无 kind → `get pods -l app=`、deployment/sts 命令、bogus kind
  拒绝、evaluate 的 pod/deployment 语义差异（"1/1 Running" 命中 pod 输出、
  不命中 deployment "1/1 1 1"）；② 轮询——变更类默认轮询第 4 次通过
  （attempts=4，retry 12×10）、只读类单次、retry 显式封顶 2 次、retry:false
  关轮询、全败报"3 次尝试均失败，最后一次"；③ 回滚审批——rollback 步骤
  request_ops_approval 收到 require_confirmation=True + desc 含 ⚠ 回滚、
  正常步骤 approve 档位不强制、force 覆盖 execute 档位、scheduled 仍豁免；
  ④ 用例——restore-server 语义（kind=pod + retry 12×10，
  ContainerCreating→Running 序列）不再误判回滚。
- **存量测试适配**：回滚步骤强制审批是有意行为变更——test_runbook_exec 的
  `test_variable_substitution_cross_step` / `test_on_failure_rollback_terminates`
  与 test_batch73_yapl_stage_c 的 `test_sub_failure_child_on_failure_first` /
  `test_parent_rollback_linkage` 原假设回滚步骤免审批执行，改为走审批回调放行
  （VIGIL_INTERACTIVE=1 + approval_callback "once"），断言语义不变。
- **验收**：test_batch78_expect_fixes.py 15 例全绿；回归 runbook 全家桶
  （test_runbook_*.py + batch73/76/78）186 passed 零回归；实测
  generate_expect_check(kubectl, "1/1 Running") → `get pods -l app=` 查 pod、
  mock 轮询第 4 次通过 expect ok、rollback 场景审批 require_confirmation=True；
  E2E 加载真实 argocd-full-stack-recovery.yaml（temp VIGIL_HOME + prod 矩阵
  execute + mock runner 模拟 ContainerCreating→Running）→ 5 个 expect 全走
  pod 通道、restore-server 第 4 次尝试通过、result ok 不触发回滚。
- **状态**：独立 feat commit（batch78，1 个 commit），只含 2 个工具文件 + 2 个
  存量测试文件适配 + 1 个新测试文件 + OPS-DELTA.md 本条登记；runbook 用例
  （~/.vigil/runbooks/argocd-full-stack-recovery.yaml）随本批同步，仓库无对应
  源故不在 commit 内。

### 94. expect 检查三修——真实 selector / rollback 健康检查 / retry 24×10（2026-08-25/26，batch79）

- **背景**（2026-08-26 实测，用户 node1 手动验证 + argocd-full-stack-recovery
  第三次执行）：expect 检查与 rollback 的通用 bug 连环致死：
  - 根因 1（expect label 硬编码）：`generate_expect_check` 的 pod 通道硬编码
    `kubectl get pods -l app=<name> -o wide`。但 k8s 推荐 label
    （app.kubernetes.io/name）应用（argocd 等）**没有 `app=` label** → 查询返回
    空 → 响应体空 → expect 必败（用户实测：`get pods -l app=argocd-redis` 返回
    "No resources found"，而真实 selector
    `{"app.kubernetes.io/name":"argocd-redis"}` 查得到 Running pod）。
  - 根因 2（rollback 破坏性）：`kubectl rollout undo` 回滚到**上一个 revision**。
    修复步骤（prepare/scale）刚产生新 revision（resources 补好），undo 把修复
    冲掉 → 之后全部 FailedCreate（Gatekeeper 拒）→ 死循环。
  - 根因 3（retry 时长）：batch78 默认变更类 12×10s=120s，pod 实际需要
    2-4 分钟才 Ready（镜像拉取 + 启动 + readiness），窗口偏短。
- **任务 1——expect kubectl pod 通道读真实 selector（`tools/runbook_handlers.py`）**：
  pod 通道改为一条 bash 命令两步走：先 `kubectl {ns}get deployment/{obj}
  -o jsonpath='{.spec.selector.matchLabels}'` 读 deployment/sts 的真实
  selector（sts 用 `get sts/{obj}`，kind 判断同现有逻辑），再用
  `kubectl {ns}get pods -l "$S" -o wide` 按真实 label 查 pod
  （shell=True，selector 中间产物通过子命令替换注入，不进 expect body——
  expect 检查支持命令序列，runner 逐个执行但 evaluate_expect 只取 results[0]，
  合并成单条 shell 命令保证最终 stdout 是 pod 列表）。selector 为空 → 回退
  `app={name}` 并注明；**selector 读取失败 → exit 非 0 → fail-closed**
  （stderr 报"无法读取 deployment {obj} selector"）。显式 `expect.selector`
  直接用它（shell=False，跳过读取）；`expect.label` 保留兼容（app={label}
  旧行为）。
- **任务 2——rollback 条件 undo（`tools/runbook_handlers.py`，方案 A+C）**：
  `_rollback` 的 kubectl 通道默认改为
  `rollout status --timeout=5s && echo '当前健康无需回滚' || rollout undo`
  （shell=True）——当前 revision 健康则不 undo（修复状态不被冲掉），不健康才
  undo（回到已知良好状态）；显式 `force_undo: true` 跳过健康检查直接 undo
  （shell=False）；`to` revision 保留在 undo 后缀。docker_compose 通道不变。
  健康检查不削弱审批：undo 仍走矩阵门 + force_confirmation（batch78 强化）。
- **任务 3——expect 默认 retry 加长（`tools/runbook_exec.py`）**：
  变更类动作默认 attempts 12×10 → **24×10（240s = 4 分钟窗口）**；
  工具描述 + skill expect retry 说明同步；`expect.retry` 显式声明仍覆盖
  （含改短）；只读类保持单次。
- **运行时数据（`~/.vigil`，仓库无源，不在 commit）**：
  argocd-full-stack-recovery.yaml 全部 5 个 retry 块 attempts 12→24；
  skills/vigil/runbook-authoring/SKILL.md 补：kubectl 查 pod 默认读真实
  deployment selector（推荐 label 应用无需 `app=`）、`expect.selector` 可覆盖、
  变更类默认轮询 24×10s、`retry: false` 关闭。
- **新测试**（2 文件 12 例）：
  ① tests/tools/test_batch79_expect_selector.py（7 例）——临时 bin/kubectl
  fake（SCENARIO 环境变量）+ 真实 bash -c 执行生成的 shell 命令：两步读真实
  selector（stdout 是 pod 列表、selector JSON 不进 body）、旧 `-l app=` 对
  推荐 label 返回 No resources（对照）、`expect.selector` 覆盖、`expect.label`
  兼容、selector 读失败 fail-closed（stderr 含"无法读取…"）、selector 空回退
  app=、E2E（execute_runbook scale + expect 通过）；
  ② tests/tools/test_batch79_rollback_safe.py（5 例）——fake kubectl 验证：
  默认命令含健康检查、健康跳过 undo（无 rollout undo 调用）、不健康执行 undo、
  force_undo 跳过检查、to=revision 保留。fake kubectl 输出为真实多空格列对齐，
  断言用 re.sub(r"\s+", " ") 归一化。
- **存量测试适配**：test_batch78_expect_fixes.py——retry 默认断言 12→24；
  `test_kubectl_default_kind_is_pod` 改为断言真实 selector 命令（shell=True +
  jsonpath + `get pods -l "$S" -o wide`）；`_probe_runner` 改按
  `"jsonpath=" in cmd` 匹配。
- **验收**：test_batch79_expect_selector.py 7 例 + test_batch79_rollback_safe.py
  5 例全绿；E2E（temp VIGIL_HOME + prod 矩阵 execute + 真实
  argocd-full-stack-recovery.yaml + mock runner）场景 A 全绿不触发回滚（9 次
  expect 全部真实 selector 查到 Running，修复前 app= 必败）、场景 B
  restore-redis 持续 ContainerCreating → rollback-all 触发且 rb 步骤命令全部
  走健康检查（rollout status 先行，健康跳过 undo）→ 修复不被冲掉；回归
  runbook 全家桶（test_runbook_*.py + batch73/76/78/79 + approval + matrix）
  444 passed 零回归。
- **状态**：独立 fix commit（batch79，1 个 commit），只含 tools/runbook_handlers.py、
  tools/runbook_exec.py、tests/tools/test_batch78_expect_fixes.py、
  tests/tools/test_batch79_expect_selector.py、
  tests/tools/test_batch79_rollback_safe.py + OPS-DELTA.md 本条登记；runbook
  用例与 skill（~/.vigil/runbooks、~/.vigil/skills）随本批同步，仓库无对应源
  故不在 commit 内。

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

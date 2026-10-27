# Vigil 周报（周期变更汇总）

按时间窗汇总 Vigil 的「作业面 + 变更面」，产出一份 Markdown 周报，并可选发布到
BookStack。链路是确定性的两段式：

```
采集脚本（只读取数 + 渲染 MD）
  → 模型（只补写第 1、4 段叙述）
  → publish_bookstack（确定性发布）
```

- 作业面：这周有哪些会话、做了什么写操作、哪些失败或未闭环。
- 变更面：拓扑实体相对上次快照的变化（可选：git 仓库提交）。
- Confluence 本轮只留接口，不实现。

## 文件

| 文件 | 作用 |
|---|---|
| `cron/scripts/weekly_change_digest.py` | 采集器：state.db + topology.yaml + git → facts JSON 或 Markdown |
| `tools/bookstack_tool.py` | `publish_bookstack` 工具：建页 / 更新 / 幂等 |
| `tools/publish_targets.py` | `publish_target` 抽象：file / bookstack / confluence |
| `tests/cron/test_weekly_change_digest.py` | 采集器测试 |
| `tests/tools/test_bookstack_tool.py` | 发布工具测试 |
| `tests/fixtures/weekly_digest/state.db` | 固定 fixture（schema 子集 + 3 个窗口内会话 + 1 个窗口外会话） |

## 采集器用法

```bash
python cron/scripts/weekly_change_digest.py \
  --db /home/wpwang/.vigil/state.db \
  [--window 7d | --week 2026-W44 | --since 2026-10-20 --until 2026-10-27] \
  [--topology /home/wpwang/.vigil/topology.yaml] \
  [--state-dir /home/wpwang/.vigil] \
  [--repos /path/a,/path/b] \
  [--exclude-job NAME] [--exclude-job-id ID] \
  [--format facts|md] [--out PATH]
```

### 窗口

- 时区固定 `Asia/Shanghai`；数据库里是 unixepoch。
- 统一左闭右开 `[since, until)`：`since` 当刻算，`until` 当刻不算。
- `--window 7d`（默认）：`until = 当前时刻`，`since = until - 7d`。支持 `24h`、`2w`。
- `--week 2026-W44`：ISO 周，周一到下周一 00:00。
- `--since/--until`：`YYYY-MM-DD` 或 ISO 8601；只写日期时按当天 00:00 处理。
- `label` 取窗口起点所在 ISO 周（`--week` 用其本身）。周报文件名用它；周五 18:00 的
  7d 窗口起点是上周五，所以默认任务通常以上一周的周号命名。
- 会话归属按 `sessions.started_at` 落在窗口内判定；消息级统计（写操作、错误、
  只读工具 Top）只取窗口内的消息。

### 排除周报自身会话

周报 job 自己也会产生 `source=cron` 会话（主题可能是「向 BookStack 发布周报」）。
这些会话不能计入作业面，否则会出现「周报统计周报流水线」的自指数据。

1. cron 运行预跑脚本时会注入 `VIGIL_CRON_JOB_ID` 和 `VIGIL_CRON_JOB_NAME`；
   采集器默认读取它们，排除 `source=cron` 且满足任一条件的会话：
   (1) `session.id` 以 `cron_<当前 job id>_` 开头；(2) `session.title` 以当前 job 名开头。
2. job id 匹配是主路径，能覆盖改名前后的历史会话；标题匹配只作兜底。
3. 这些 cron 会话派生的 `subagent` 子会话也一并排除（例如「把这份 MD 发布到
   BookStack」的委派任务），避免流水线自述残留在作业面里。
4. 手动运行时可用 `--exclude-job NAME`（可重复；会按
   `<state-dir>/cron/jobs.json` 把名字解析成 id，并做标题前缀兜底）或
   `--exclude-job-id ID`（可重复）排除历史 job。
5. 排除只影响 `sessions` / `totals`，不新增 facts 字段，也不改窗口判定。

### 写操作判定

不使用 `messages.effect_disposition`（实测为 `None` / `none` / `unknown`）。

1. `terminal` 调用取 `command`，走 `tools/action_classifier.classify_command`。
2. 写类 = `start`、`stop`、`reload`、`transfer_file`、`restart`、`install`、
   `upgrade`、`apply_config`、`run_script`、`scale`、`deploy`、`rollback`、
   `restore`、`remove`、`decommission`、`reboot`、`shutdown`。
3. 不算写：`query`、`fetch_log`、`verify`、`backup`。
4. 链式命令（`&&` / `;` / 换行）按分类器的 `chain` 拆开：任一子命令命中写类
   就记一次写操作；所有子命令都识别不出才进 `unclassified`。
5. 非 terminal 写类白名单：`write_file`、`patch`、`topo_update`、
   `skill_manage`、`memory`、`publish_bookstack`。

### facts JSON

字段固定，不要改名。所有字符串都去换行并截断（`first_user_msg` ≤200 字、
`summary` ≤120 字、`errors` ≤160 字）；不写整段命令或文件内容。

```json
{
  "window": {"since": "2026-10-20T00:00:00+08:00", "until": "2026-10-27T00:00:00+08:00", "label": "2026-W43"},
  "generated_at": "2026-10-27T19:30:00+08:00",
  "db": "/home/wpwang/.vigil/state.db",
  "totals": {"sessions": 12, "messages": 840, "write_actions": 23, "failed_sessions": 1,
             "tokens": {"in": 0, "out": 0}},
  "sessions": [
    {"id": "20261020_101500_ab12cd", "source": "web", "started_at": "...", "ended_at": "...",
     "duration_min": 42, "end_reason": "cli_close", "message_count": 88, "tool_call_count": 31,
     "tokens": {"in": 0, "out": 0},
     "first_user_msg": "≤200 字",
     "write_actions": [{"tool": "terminal", "action": "apply_config", "summary": "≤120 字", "count": 3}],
     "read_only_tool_top": [["read_file", 12], ["search_files", 5]],
     "errors": ["≤160 字"]}
  ],
  "unclassified": [{"session_id": "...", "tool": "terminal", "summary": "≤120 字"}],
  "topology": {"snapshot_path": "...", "snapshot_missing": false,
               "changed": [{"entity": "harbor", "env": "prod", "field": "last_verified",
                            "old": "2026-09-01", "new": "2026-09-27", "source": "agent"}],
               "added": [], "removed": []},
  "repos": [{"path": "/x/y", "dirty": false, "commits": [{"sha": "abc1234", "date": "2026-10-25", "author": "wpwang", "subject": "..."}]}],
  "gaps": ["数据缺：xxx"]
}
```

`failed_sessions` = 有工具错误、或 `end_reason` 属于中断/超时/错误类的会话。
`gaps` 记录数据缺口（topology 缺失、首跑无快照、未指定 repos、unclassified 截断等）。

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 正常 |
| 2 | 参数错误（含写出失败） |
| 3 | state.db 不存在 / 打不开 / 不是 Vigil 库 |

采集器只用标准库 + `pyyaml`，不访问网络；只有发布到 BookStack 需要网络。

### 拓扑快照

`topo_update` 没有落盘审计，所以变更面只能靠快照 diff：

1. 本次读到的实体按 `kind/name` 摊平（`environments`、`clusters`、`hosts` 以及
   未来新增的顶层实体列表都按同一规则处理）。
2. 快照写在 `<state-dir>/digest/topology-snapshot.json`。
3. 首跑没有历史快照：`snapshot_missing: true`，`changed` / `added` / `removed`
   全为空（不假装有变化），同时建立基线。
4. 之后每次对比上一份快照，输出字段级 `changed` 和实体级 `added` / `removed`。
5. `credentials` / `credential` 等敏感字段不写入快照。

### git（可选）

`--repos` 用 `git log --since --until --numstat` 和 `git status --porcelain` 只读
取数，不 fetch、不 clean、不改任何仓库。不是 git 仓库时记入 `gaps`，不中断采集。

## Markdown 契约

路径：`<state-dir>/reports/Vigil-周报-{ISO年}-W{两位周}.md`
页名 = 文件名去 `.md`，例如 `Vigil-周报-2026-W44`。

固定 5 段，中文，禁 emoji，表格列 ≤6：

```markdown
# Vigil 周报 2026-W44（10-26 ~ 11-02）

## 1. 摘要

## 2. 变更明细
| 环境 | 实体 | 变化 | 来源 |

## 3. 作业明细
| 时间 | 会话 | 主要动作 | 写操作 | 结果 |

## 4. 风险与未闭环

## 5. 下周期
```

- 表格数据只能来自 facts；`--format md` 的渲染在脚本里。
- 模型只允许补写第 1、4 段的叙述文字，不许改数字、时间、实体名。
- facts 缺字段时写「数据缺」，禁止编造。
- 空数据：零会话写「本周期无作业记录」；无拓扑变更写「本周期无拓扑变更」；
  第 4 段为空写「无」；第 5 段 1 到 3 条，且对应第 4 段。

## BookStack 凭证

优先级（逐字段）：显式参数 > 环境变量 > 凭证文件。

1. 环境变量：`BOOKSTACK_URL`、`BOOKSTACK_TOKEN_ID`、`BOOKSTACK_TOKEN_SECRET`。
   建页用的书 ID 可放 `BOOKSTACK_BOOK_ID`（`book_id` 参数缺省时读取）。
2. 凭证文件：`<VIGIL_HOME>/credentials/bookstack.json`，权限必须是 `0600`
   （组/其他位有任何权限都会拒绝读取）：

```json
{
  "base_url": "https://bookstack.example.com",
  "token_id": "3",
  "token_secret": "xxxxxxxxxxxxxxxx",
  "book_id": 5
}
```

```bash
chmod 600 ~/.vigil/credentials/bookstack.json
```

凭证不要提交进仓库，也不要写进 jobs.json、prompt、日志或报告。

## 发布工具 `publish_bookstack`

| 参数 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 页面名（= 文件名去 `.md`） |
| `markdown` | 是 | 页面正文 |
| `book_id` | 建页时必填 | 目标书 ID；也可用 `BOOKSTACK_BOOK_ID` |
| `chapter_id` | 否 | 给了则挂章 |
| `page_id` | 否 | 给了直接 `PUT /api/pages/{id}` |
| `base_url` / `token_id` / `token_secret` | 否 | 覆盖配置 |
| `timeout` | 否 | 默认 30 秒 |

行为：

1. `page_id` 给了 → `PUT /api/pages/{page_id}`。
2. 否则 `GET /api/pages?filter[name]=<name>&count=50&book_id=<book_id>` 精确匹配
   `name`：命中 → `PUT` 更新；未命中 → `POST /api/pages` 新建。
3. 建页必须带 `book_id` 或 `chapter_id`，两者都缺时返回
   「建页需要 book_id（或 chapter_id）」。
4. 同一 `name` 重跑走更新，不产生第二个页面。
5. 工具不设 `requires_env`：没配凭证时仍出现在工具列表里，调用时才返回
   `tool_error` 并点名缺哪个变量。
6. 令牌不会出现在日志、异常或返回值里；异常文本先脱敏再抛出。

返回值：

```json
{"success": true, "action": "created", "page_id": 12,
 "url": "https://bookstack.example.com/books/reports/page/vigil-w44", "book_id": 3}
```

## 定时任务注册（由用户执行）

1. 先把 wrapper 放进 `<VIGIL_HOME>/scripts/`（创建期只接受这个目录下的相对文件名）：

```bash
cat > /home/wpwang/.vigil/scripts/weekly_change_digest.sh <<'EOF'
#!/bin/bash
exec /home/wpwang/projects/vigil-agent/.venv/bin/python \
  /home/wpwang/projects/vigil-agent/cron/scripts/weekly_change_digest.py "$@"
EOF
chmod 700 /home/wpwang/.vigil/scripts/weekly_change_digest.sh
```

2. 注册时 `--script` 只写文件名，不能写绝对路径：

```bash
VIGIL_HOME=/home/wpwang/.vigil vigil cron create "0 18 * * 5" \
  "见下方 job prompt" \
  --name "Vigil 周报" \
  --script weekly_change_digest.sh \
  --deliver local
```

job prompt：

```
你是 Vigil 周报生成器。上面注入的 JSON 是采集器由 state.db / topology.yaml / git 确定性生成的事实，禁止修改其中任何数字、时间、实体名。

步骤：
1. 用 write_file 写出 MD 报告，路径由 facts 的 label 决定：<state-dir>/reports/Vigil-周报-<label>.md，结构为：1. 摘要 / 2. 变更明细 / 3. 作业明细 / 4. 风险与未闭环 / 5. 下周期。表格数据必须与 JSON 完全一致；只允许你撰写第 1、4 段的叙述。
2. 调用 publish_bookstack：name = 文件名去 .md，markdown = 刚写文件的内容，book_id 取环境变量 BOOKSTACK_BOOK_ID。
3. 回报两行：MD 绝对路径；BookStack 返回的 action + url。发布失败最多重试 1 次，仍然失败就只报错误，禁止编造 url。
```

注意：

- `vigil cron create` / `cronjob` 在**创建期**校验脚本路径：只接受
  `<VIGIL_HOME>/scripts/` 下的相对路径（如 `weekly_change_digest.sh`、
  `monitors/check.py`）。绝对路径或 `~` 开头返回
  `Script path must be relative to ~/.vigil/scripts/.`，目录穿越返回
  `Script path escapes the scripts directory via traversal`；两种情况 job
  都不会创建，不是运行期才拦。
- 也可以直接 `cp` 仓库脚本到 `<VIGIL_HOME>/scripts/`，然后
  `--script weekly_change_digest.py`；仓库更新后需要重新复制。wrapper 方式
  不需要复制，推荐。
- cron 会把 `VIGIL_CRON_JOB_ID` / `VIGIL_CRON_JOB_NAME` 传给预跑脚本，
  采集器默认排除当前 job 自己的历史会话，所以周报不会统计自己。
- job prompt 里的 `<state-dir>` 由 facts 的 `db` 字段（`<state-dir>/state.db`）推导；
  本机是 `/home/wpwang/.vigil`。
- `--deliver local` 表示投递到本地 cron 输出目录，不往聊天窗口刷屏。
- `publish_bookstack` 是 opt-in：只挂在 `bookstack` toolset、不在核心工具里。
  需要发布时用 `vigil tools` 勾选；`weekly-oplog` 蓝图会自动把该 toolset 加进
  生成的 job。

## Confluence 预留

`tools/publish_targets.py` 定义 `PublishTarget` 抽象：

- `FileTarget`：写本地 Markdown，离线可用。
- `BookstackTarget`：本轮实现。
- `ConfluenceTarget`：本轮只抛 `NotImplementedError`。

转换点在 `ConfluenceTarget._markdown_to_storage`：Confluence 的存储格式是
XHTML（`<p>` / `<table>` 等），Markdown 不能直接提交，需要先转 storage format
再调用 `/rest/api/content/{id}`。上层调用方式不变。

## 验证

```bash
# fixture facts（窗口外会话不应出现）
python cron/scripts/weekly_change_digest.py \
  --db tests/fixtures/weekly_digest/state.db --window 7d --format facts

# fixture Markdown
python cron/scripts/weekly_change_digest.py \
  --db tests/fixtures/weekly_digest/state.db --window 7d --format md --out /tmp/w.md

# 真实库（只读）
python cron/scripts/weekly_change_digest.py \
  --db /home/wpwang/.vigil/state.db --window 30d --format facts | head -40

# 单测
python -m pytest tests/tools/test_bookstack_tool.py tests/cron/test_weekly_change_digest.py -q
```

### BookStack 本地真机验证

直接用 `-e DB_CONNECTION=sqlite` 启动 `lscr.io/linuxserver/bookstack`
（v26.09.2-ls293）会报 `could not find driver`：该镜像只带 `pdo_mysql`，
没有 `pdo_sqlite`。实测改用 MariaDB sidecar（`--network` 连通两个容器）后，
建页、更新、同名幂等全部正常。这只影响本地验证方式，不影响
`publish_bookstack` 的实现。

## 已知限制

1. 首次运行没有拓扑历史快照，只能建立基线，不能报告变更。
2. `unclassified` 最多保留 50 条，超出部分记入 `gaps`。
3. 每个会话最多保留 8 条工具错误、每个仓库最多 200 条提交。
4. 会话级 `message_count` / `tokens` 取 `sessions` 表（会话生命周期计数）。
5. Confluence 未实现。
6. cron job prompt 按任务书原样使用，由模型从 facts 重写 MD；脚本的
   `--format md` 是确定性模板，可用于校验或回填。

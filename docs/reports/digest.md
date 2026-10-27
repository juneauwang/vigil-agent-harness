# 作业周报（digest）

按「我提的任务 → Vigil 实际做了什么 → 改了哪些服务/参数」汇总一个时间窗，产出一份
Markdown，可直接发 BookStack。

这是**内置能力**：`pip install vigil-agent-harness` 后 `vigil digest` 就在，不依赖
`~/.vigil/scripts/` 里的任何 wrapper。

与 `weekly-digest.md`（会话/计数为中心）互补：那份回答「这周跑了多少」，这份回答
「这周我动了什么」。

## 1. 用法与参数

```bash
# 事实 JSON（喂给写稿的 agent，不给模型看渲染稿）
vigil digest --window 7d --format facts

# 确定性 Markdown（不经过模型，模板稿）
vigil digest --week 2026-W43 --format md --out /tmp/r.md

# 渲染 + 写报告 + 发布 BookStack（同页名幂等更新）
vigil digest --window 7d --format md --publish bookstack
```

| 参数 | 说明 |
|---|---|
| `--window` | 滚动窗口 `7d / 24h / 2w`；不传则读 `ops.digest.window`，再缺省 `7d` |
| `--week` | ISO 周（`2026-W43`），周一 00:00 到下周一 00:00；与其余窗口参数互斥 |
| `--since` / `--until` | 显式区间，必须同时给且 `since < until`；与 `--window` 互斥 |
| `--db` | `state.db` 路径，缺省 `<VIGIL_HOME>/state.db` |
| `--state-dir` | 报告输出根目录，缺省 db 所在目录 |
| `--format` | `facts`（默认）/ `md` |
| `--out` | 写文件；缺省打 stdout。**发布时它决定 BookStack 页名**（文件名去 `.md`） |
| `--prose` | `auto`（默认，读 `<state-dir>/reports/<label>.prose.json`）/ `none`（强制模板）/ 具体路径 |
| `--publish` | `none`（默认）/ `file` / `bookstack`；要求 `--format md` |
| `--exclude-job-id` | 额外排除某个 cron job 的会话，可重复 |

## 2. 时间窗与自排除

1. 时区固定 `Asia/Shanghai`，窗口左闭右开 `[since, until)`。
2. `label` 取窗口结束前最后一刻所在 ISO 周（`--week` 用其本身）；报告文件名与
   BookStack 页名都用它，例如 `Vigil-作业周报-2026-W44`。
3. 会话归属按 `sessions.started_at` 落在窗口内判定。
4. 自排除：有 job 上下文时只排**本 job 及其派生 subagent**；手动跑（无 job 上下文）
   排全部 `cron` / `subagent` 会话。已删除的历史 job 用 `--exclude-job-id` 手工清。

## 3. facts 契约

字段名固定，模型与发布端都依赖：

```
window{since,until,label}  generated_at  db
totals{tasks,changes,by_kind,excluded_pipeline_sessions,active_days}
days[]{date,task_indexes[],changes,kinds{}}
tasks[]{when,session,source,asks[],changes[],change_total,end_reason}
  changes[]{kind,target,detail,why}   # kind: 拓扑/文件/runbook/技能/记忆/发布/命令
gaps[]
```

抽取规则（**不使用** `tools/action_classifier`：它把 `helm show chart` 判成
restore、把探测型 `curl` 判成 `transfer_file`）：

1. 终端写类判定用写动词白名单：kubectl apply/delete/create/scale/rollout/patch、
   helm install/upgrade/uninstall/rollback、systemctl start/stop/restart/reload/
   enable/disable、docker run/rm/stop/build/push、docker compose up/down、scp/rsync、
   rm/mv/cp/chmod/chown、git push/reset/commit/merge/rebase、DDL/DML、
   reboot/shutdown、带数据体的 curl。只读/探测命令不入表。
2. 复合命令按 `&&` / `||` / `;` / 换行**拆段逐段判定**（管道不拆，只看第一节）；
   数据类 flag 区分大小写（`-d` 是 data、`-D` 是 dump-header）；引号内的远程命令
   （`ssh host 'kubectl apply …'`）二次段首匹配，但 `grep/egrep/rg/awk/sed/find/ls/cat`
   的模式串跳过。
3. 文件类 `detail` = 用途句（首个注释行 → 首个有效行 → YAML `kind:`/`name:`）。

## 4. 报告结构与 prose 契约

五段固定：`1. 本周期做了什么` / `2. 变更明细(表)` / `3. 执行过的写类命令(摘)` /
`4. 未闭环与待跟进` / `5. 下周期`。

**表格与数字永远来自 facts**，prose 只能换第 1/4/5 段的叙述——模型不进不可校验主路径。

prose 文件路径：`<state-dir>/reports/Vigil-作业周报-<label>.prose.json`

```json
{
  "summary": "按天分组的人话：### 10-20 做了什么 → 结果",
  "risks":   "只写 end_reason 不是 cli_close/turn_complete 的任务、以及 gaps",
  "next":    "1-3 条，必须对应 risks 里的未闭环项"
}
```

规则：

1. 三个字段值都是 Markdown 片段，**不含** `##` 标题（标题由脚本渲染）。
2. 文件不存在 / JSON 坏 / 顶层不是对象 → 打一行 stderr 并**回落确定性模板**，
   报告照出，不因缺叙述而失败。
3. `--prose none` 强制模板；`--prose <path>` 指定其它位置。

## 5. 三种生产形态

### (1) A/B 双 job（本机在用）

周五 18:00 写稿，18:10 渲染发布。模型只写 prose，脚本负责其余：

| 时间 | job | 模式 | script / args | 工具集 |
|---|---|---|---|---|
| `0 18 * * 5` | Weekly work log (digest) | agent | `builtin:digest` / `--window 7d --format facts` | `file, session_search, skills` |
| `10 18 * * 5` | Weekly work log (render) | `no_agent` | `builtin:digest` / `--format md --publish bookstack` | — |

A 的 prompt 只要求：读注入的 facts → 按 `facts.days` 分组写三段 → 写成上面的
prose JSON → 回读确认是合法 JSON。禁止发布、禁止编造、禁止用 curl。

这样做的原因：模型若直接写整篇 15KB 报告，会撞上「疑似凭据落盘」护栏（正文含
ssh/chmod/密码 等词），被逼成分段 patch；改成只交 JSON 后工具链干净，且结构上
保证「模型只写叙述」。

### (2) 单 job agent 模式（`weekly-oplog` 蓝图默认）

一条 job 端到端：agent 自己写 MD 并调 `publish_bookstack`。工具集由蓝图自动加上
`bookstack`。适合「想让模型连排版一起写」的场景，但会回到上面那个护栏问题。

### (3) `no_agent` 模板模式

`--format md` 直接出模板稿，stdout 即产物（`--deliver` 决定投递到哪）。不需要模型，
适合「先看数据、不相信叙述」的阶段。

蓝图槽位 `time / day / window / publish / deliver`；`window` 的白名单是
`7d / 14d / 30d`。要 `45d` 这种：建完后改 `~/.vigil/cron/jobs.json` 里该 job 的
`script_args`（调度器每轮 `get_due_jobs()` 重读文件，不用重启），或走 agent 的
cronjob 工具传 `script_args`。注意 `vigil cron edit` 只改 schedule/prompt/script/…，
**没有** `--script-args`。`publish=bookstack` 但 `ops.digest.publish` 未配置时，
蓝图回退为 `no_agent + file` 并在 prompt 里明说「未配置发布目标」，不静默失败。

## 6. 发布：BookStack

`config.yaml`：

```yaml
ops:
  digest:
    window: 7d
    publish:
      target: bookstack              # none | file | bookstack
      bookstack:
        base_url: http://127.0.0.1:8080
        book_id: 1
        credential_ref: bookstack-api
```

凭据只进 `credential_vault`（`<VIGIL_HOME>/secrets/`，0600 + owner 校验），
**禁止**写进 config.yaml / jobs.json / 报告 / 日志：

```bash
vigil credential store bookstack-api   # 隐藏输入；也可 --stdin 从管道读（值不打印）
vigil credential list                  # 只看名字
vigil credential expire bookstack-api
```

行为：

1. 先写 `<state-dir>/reports/Vigil-作业周报-<label>.md`，再调
   `BookstackTarget`（`tools/publish_targets.py`）。
2. 幂等：在 `book_id` 范围内按**页名精确匹配**，命中则更新、否则新建；
   `--out` 改了文件名就等于改了页名。
3. stdout 只打印 `action=… page_id=… url=… md=…`，不打印凭证；失败走退出码 4。
4. 页名里的中文会被 BookStack 的 slug 规则丢弃——`name="Vigil 发布工具自检"` 会
   落成 `/page/vigil`。**页名必须带 ASCII 部分**（周报的 `2026-W44` 就是）。

## 7. Confluence：现状与缺口

`tools/publish_targets.py` 里 `ConfluenceTarget` 只是占位：`publish()` 直接
`raise NotImplementedError`，`build_target("confluence")` 能构造但不能发。要真正打通，
三件事，接口不用改：

1. `_markdown_to_storage()`：Markdown → Confluence storage format（XHTML）。
   标题/段落/表格/代码块/行内代码是必须覆盖的集合（本报告只用到这几类）。
2. `publish()`：按页名在 `space_key` 下查 `GET /rest/api/content?spaceKey=&title=`，
   命中 `PUT /rest/api/content/{id}`（version+1），否则 `POST /rest/api/content`
   （带 `ancestors` 落父页）。按 `page_id` 幂等，与 BookStack 同语义。
3. 配置：`ops.digest.publish.target: confluence` +
   `ops.digest.publish.confluence{base_url, space_key, parent_id, credential_ref}`，
   凭据仍走 credential_vault（`token_id:token_secret` 或 PAT，看部署形态）。
   同时要给 CLI 的 `--publish` 与蓝图 `publish` 槽位加 `confluence` 选项。

当前判定：**没做，也不假装能用**。别在 `target` 里填 `confluence`，会得到
`NotImplementedError`。

## 8. 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 参数错误（窗口格式、`--publish` 未配 `--format md`、互斥参数） |
| 3 | `state.db` 不存在或打不开 |
| 4 | 报告写出或发布失败 |

发布失败必须非 0；stdout/stderr 都不打印 token。

## 9. 排障

1. `digest: 发布失败：无法读取凭据 'bookstack-api'` → 先 `vigil credential store
   bookstack-api`（值是 `token_id:token_secret`，BookStack → Access & Security 生成）。
2. 页面没更新、只新建了一页 → `--out` 或报告文件名变了，页名跟着变。检查
   `<state-dir>/reports/` 下的文件名是否还带同一周 `label`。
3. 报告第 1 段是模板味 → prose 文件没生成或路径不对。看 stderr 有没有
   `prose 文件不存在，回落模板`；确认 A job 的 `state-dir` 与 B job 一致。
4. 数字对不上 → 别改 prose，改 facts：先 `--format facts` 看 `tasks[].changes[]`，
   再决定是抽取规则问题还是窗口问题。
5. 自己跑的命令出现在了周报里 → 手动跑的窗口会把 `cron`/`subagent` 全排掉，但
   你本人在 CLI 里的会话算用户作业，属于预期；只想看某几段就用 `--since/--until` 收窄。

## 10. 验证

```bash
python -m pytest tests/ops/test_digest.py -q
vigil digest --window 7d --format facts | head -30
vigil digest --window 7d --format md --out /tmp/d.md && head -20 /tmp/d.md
vigil digest --window 7d --format md --publish bookstack    # 看 action/page_id/url
```

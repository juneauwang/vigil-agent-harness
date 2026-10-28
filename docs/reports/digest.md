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
| `--window` | 滚动窗口 `<N>[dhmw]`——`1d` / `12h` / `7d` / `2w` / `90d` 都行；不传则读 `ops.digest.window`，再缺省 `7d` |
| `--week` | ISO 周（`2026-W43`），周一 00:00 到下周一 00:00；与其余窗口参数互斥 |
| `--since` / `--until` | 显式区间，必须同时给且 `since < until`；与 `--window` 互斥 |
| `--db` | `state.db` 路径，缺省 `<VIGIL_HOME>/state.db` |
| `--state-dir` | 报告输出根目录，缺省 db 所在目录 |
| `--format` | `facts`（默认）/ `md` |
| `--out` | 写文件；缺省打 stdout。**发布时它决定 BookStack 页名**（文件名去 `.md`） |
| `--prose` | `auto`（默认，读 `<state-dir>/reports/<文件名前缀><label>.prose.json`）/ `none`（强制模板）/ 具体路径 |
| `--publish` | `none`（默认）/ `file` / `bookstack`；要求 `--format md` |
| `--exclude-job-id` | 额外排除某个 cron job 的会话，可重复 |

## 2. 时间窗与自排除

1. 时区固定 `Asia/Shanghai`，窗口左闭右开 `[since, until)`。
2. **怎么命名看窗口跨度，不再一律叫「周报」**（时区固定 `Asia/Shanghai`，
   `until` 用 `until - 1s` 取日期）：

   | 跨度 | label | 标题首行 | 文件名前缀 |
   |---|---|---|---|
   | `--week` 给定 | `2026-W43` | `# Vigil 作业周报 2026-W43（…）` | `Vigil-作业周报-` |
   | ≤ 2 天 | `2026-10-28` | `# Vigil 作业日报 2026-10-28（10-27 ~ 10-28）` | `Vigil-作业日报-` |
   | 2 天 < 跨度 ≤ 10 天 | `2026-W44` | `# Vigil 作业周报 2026-W44（10-21 ~ 10-28）` | `Vigil-作业周报-` |
   | > 10 天 | `2026-07-30_2026-10-28` | `# Vigil 作业报告 2026-07-30_2026-10-28（…）` | `Vigil-作业报告-` |

   `label` 只含 `[0-9A-Za-z_-]`（BookStack 拿 ASCII 做 slug）；报告文件名与
   BookStack 页名都是「前缀 + label」（页名 = 文件名去扩展名，规则不变），例如
   `Vigil-作业日报-2026-10-28`、`Vigil-作业报告-2026-07-30_2026-10-28`。
3. 会话归属按 `sessions.started_at` 落在窗口内判定。
4. 自排除：有 job 上下文时只排**本 job 及其派生 subagent**；手动跑（无 job 上下文）
   排全部 `cron` / `subagent` 会话。已删除的历史 job 用 `--exclude-job-id` 手工清。

## 3. facts 契约

字段名固定，模型与发布端都依赖：

```
window{since,until,label,granularity,span_days}  generated_at  db
  # granularity: daily | weekly | range（报告粒度，前端与 prose 提示词可读）
totals{tasks,objects,objects_with_identity,objects_missing_identity,by_type,
       changes,by_kind,excluded_pipeline_sessions,active_days}
tasks[]{when,session,source,asks[],end_reason,
        objects[],object_total,with_identity,intents[],changes[],change_total}
  objects[]{type,identity,params,result,source,hits}
    # type: 账号与权限 / 监控采集 / 监控面板 / 数据源 / 配置 / 系统服务
    #       可复用资产 / 技能 / 文档 / 发布
    # identity: 能复核的标识（role@host/db、scrape job 名、datasource uid、文件名…）
    #           抽不到 = 空串，渲染成「标识未记录」
    # source:  会话#消息 id（溯源锚点）
  intents[]{family,label,objects[],with_identity,results[]}
    # family: 账号 / 监控 / 配置 / 交付（= 子目标）
  changes[]{kind,target,detail,why}   # 仅保留供溯源；报告不再渲染
gaps[]
```

### 3.1 对象抽取规则（确定性，认不出就留空）

**报告的主语是集群里的对象，不是 Vigil 调了什么。** 所有标识都从工具参数与
工具结果里用正则抽，抽不到就留空并计入覆盖率缺口，**禁止让模型补全**。

| 来源 | 抽出来的对象 | 标识/参数 |
|---|---|---|
| `psql/mysql/mongosh/redis-cli … CREATE ROLE|USER` | 账号与权限 | `role @ host/db` + SUPERUSER |
| `curl -X POST … /api/datasources` | 数据源 | name + uid + datasource url（取返回体） |
| `curl … /api/dashboards`、`/d/<uid>` | 监控面板 | dashboard uid / URL |
| `curl … /-/reload` | 不单独成对象 | 结果并到同任务的 scrape job（`target state=up`） |
| `ansible-playbook x.yml` / `kubectl apply -f x.yml` | 配置 | playbook / manifest 名 |
| `systemctl start|enable <unit>` | 系统服务 | unit 名 |
| `sudo_exec` / `vssh`（host 来自工具参数） | 同上各类 | 命令没有 `-h` 时用 `host` 补 |
| `apt/yum/dnf install <pkg>` | 系统服务 | `<pkg> @ <host>` |
| `helm install|upgrade <release>` | 配置 | `helm release <name>` |
| `docker run <image>` | 系统服务 | `容器 <image>` |
| `topo_update(entity, updates)` | 拓扑 | 实体名 + 标量字段（dict/list 只留键） |
| `topo_delete(entity)` | 拓扑 | 实体名 + env + 归档路径（结果「删除（已归档）」） |
| 写 `…/runbooks/*.yml` | 可复用资产 | `runbook <stem>` |
| 写文件内容含 `job_name:` | 监控采集 | job 名 + `targets=[...]` + `scrape_interval` |
| 写文件内容含 `CREATE ROLE` / `postgresql_user` | 账号与权限 | role 名（同一 role 的多条记录合成一个对象） |
| 写文件内容含 `kv/…`、`secret/…` | 账号与权限 | role + 口令路径 |
| `runbook_execute` | 可复用资产 | runbook 名 + env |
| `skill_manage` | 技能 | 技能名 |
| 写 `.md`（非 /tmp） | 文档 | 文件名 |

过程产物**不进报告**：`/tmp`、`/var/tmp` 下的文件，以及 `diag-*` / `probe-*` /
`smoke-*` / `*-verify.*` / `*-check.*`。它们只留在 `changes[]` 与 `source` 里做证据。

### 3.2 子目标聚类

`intents[]` 由 `group_into_intents()` 生成：先按对象类型落到 账号 / 监控 / 配置，
交付物（runbook/技能/文档/发布）不单独成节 —— 先按标识词对齐
（`xinggh-superadmin-account` → 账号族，`grafana-dashboard-sync` → 监控族），
对不上就挂到它前面最近的主族。

### 3.3 变更判定（`changes[]`，只供溯源）

`changes[]` 是「写类动作」的流水，报告不再渲染，只作为 `source` 的旁证。判定规则
（**不使用** `tools/action_classifier`：它把 `helm show chart` 判成
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

四段固定：`1. 子目标与结果` / `2. 变更明细（按对象）` / `3. 未闭环与待跟进` /
`4. 下周期`。**没有「执行过的命令」「调了什么工具」这类章节** —— 那些是证据，
不是周报内容。

报告头部固定给覆盖率：

```
- 作业会话 3 个 / 变更对象 17 个，其中 14 个带可复核标识
- 对象构成：账号与权限 3、监控采集 5、…
- 标识缺口：3 个（没有名字/URL/参数，见第 3 节）
```

第 2 段每行四列：`对象 | 标识 / 参数 | 结果 | 溯源`。标识抽不到写
「标识未记录」，并在第 3 段落成「数据缺口：N 个对象没有可复核标识」。

> 这条口径的理由：另一个人拿着报告，要能判断集群到底改了什么；能复核的标清楚，
> 不能复核的也标清楚 —— 编一个 URL 出来比缺更糟。

prose 文件路径：`<state-dir>/reports/<前缀><label>.prose.json`（前缀按粒度 `Vigil-作业日报-` / `Vigil-作业周报-` / `Vigil-作业报告-`；新名字读不到时回落旧的 `Vigil-作业周报-<label>.prose.json`）

```json
{
  "summary": "第 1 段正文：每个子目标一行「做了什么 → 结果」",
  "risks":   "第 3 段正文：卡在哪 + 下一步",
  "next":    "第 4 段正文：1-3 条"
}
```

规则：

1. 三个字段值都是 Markdown 片段，**不含** `##` 标题（标题由脚本渲染）。
2. 模型禁止写 facts 里没有的标识（账号名/job 名/URL/uid）；identity 为空就是
   「标识未记录」，不许猜。
3. 模型禁止写工具名、命令、临时脚本、会话 id。
4. 文件不存在 / JSON 坏 / 顶层不是对象 → 打一行 stderr 并**回落确定性模板**，
   报告照出，不因缺叙述而失败。
5. `--prose none` 强制模板；`--prose <path>` 指定其它位置。

## 5. 三种生产形态

### (1) A/B 双 job（本机在用）

周五 18:00 写稿，18:10 渲染发布。模型只写 prose，脚本负责其余：

| 时间 | job | 模式 | script / args | 工具集 |
|---|---|---|---|---|
| `0 18 * * 5` | Weekly work log (digest) | agent | `builtin:digest` / `--window 7d --format facts` | `file, session_search, skills` |
| `10 18 * * 5` | Weekly work log (render) | `no_agent` | `builtin:digest` / `--format md --publish bookstack` | — |

A 的 prompt 只要求：读注入的 facts → 按 `facts.tasks[].intents[]` 每个子目标写一行
「做了什么 → 结果」→ 写成上面的 prose JSON → 回读确认是合法 JSON。禁止发布、
禁止编造标识、禁止写工具名/命令/临时脚本、禁止用 curl。

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

1. 先写 `<state-dir>/reports/<前缀><label>.md`（前缀按窗口粒度，见 §2），再调
   `BookstackTarget`（`tools/publish_targets.py`）。
2. 幂等：在 `book_id` 范围内按**页名精确匹配**，命中则更新、否则新建；
   `--out` 改了文件名就等于改了页名。
3. stdout 只打印 `action=… page_id=… url=… md=…`，不打印凭证；失败走退出码 4。
4. 页名里的中文会被 BookStack 的 slug 规则丢弃——`name="Vigil 发布工具自检"` 会
   落成 `/page/vigil`。**页名必须带 ASCII 部分**（label 的 `2026-10-28` /
   `2026-W44` / `2026-07-30_2026-10-28` 就是）。

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

# 作业周报（digest）

按「我提的任务 → Vigil 实际做了什么 → 改了哪些服务/参数」汇总一个时间窗。
这是内置能力：`pip install` 后 `vigil digest` 就在，不依赖 `~/.vigil/scripts/`
里的任何 wrapper。

与 `weekly-digest.md`（会话/计数为中心）互补：那份回答「这周跑了多少」，这份
回答「这周我动了什么」。

## 三种用法

```bash
# 1. facts JSON：喂给写稿 agent
vigil digest --window 7d --format facts

# 2. 确定性 Markdown：不经过 LLM
vigil digest --window 2026-W43 --format md --out /tmp/r.md

# 3. 渲染 + 写报告 + 发布 BookStack（同页名幂等更新）
vigil digest --window 7d --format md --publish bookstack
```

`--publish` 缺省为 `none`（只出 facts/Markdown）；`file` 写报告，`bookstack` 走
发布配置。发布时先写 `<state-dir>/reports/Vigil-作业周报-<label>.md`，再调
`BookstackTarget`；stdout 只打印 `action / page_id / url / md`，不打印凭证。

## 窗口

1. 时区固定 Asia/Shanghai，窗口左闭右开 `[since, until)`。
2. `--window 7d|24h|2w`（默认 `7d`）：`until = 现在`，`since = until - 窗口`。
3. `--week 2026-W43`：ISO 周，周一 00:00 到下周一 00:00；与 `--window` 互斥。
4. `--since/--until`：显式区间；只写日期按当天 00:00，必须同时给且 since < until。
5. `label` 取窗口结束前最后一刻所在 ISO 周（`--week` 用其本身），报告文件名和
   BookStack 页名都用它。
6. 会话归属按 `sessions.started_at` 落在窗口内判定。

## facts 契约

字段名固定，模型与发布端都依赖：

```
window{since,until,label}  generated_at  db
totals{tasks,changes,by_kind,excluded_pipeline_sessions,active_days}
days[]{date,task_indexes[],changes,kinds{}}
tasks[]{when,session,source,asks[],changes[],change_total,end_reason}
  changes[]{kind,target,detail,why}   # kind: 拓扑/文件/runbook/技能/记忆/发布/命令
gaps[]
```

抽取规则（**不使用** `tools/action_classifier`，它会把 `helm show chart` 判成
restore）：

1. 终端写类判定用写动词白名单：kubectl apply/delete/create/scale/rollout/patch、
   helm install/upgrade/uninstall/rollback、systemctl start/stop/restart/reload/
   enable/disable、docker run/rm/stop/build/push、docker compose up/down、
   scp/rsync、rm/mv/cp/chmod/chown、git push/reset/commit/merge/rebase、
   DDL/DML、reboot/shutdown、curl 带 `-X POST`/`--data`。只读/探测命令不入表。
2. 文件类 detail = 用途句（首个注释行 → 首个有效行 → YAML `kind:`/`name:`）。
3. 自身会话排除：有当前 job id 时只排本 job 及其派生 subagent；无 job id（手动
   跑）排全部 `cron`/`subagent`。已删除的历史 job 用 `--exclude-job-id` 手工清。

## 发布配置

`config.yaml` 的 `ops.digest.publish`（缺省不写 = 不发布）：

```yaml
ops:
  digest:
    publish:
      target: bookstack          # none | file | bookstack
      bookstack:
        base_url: http://127.0.0.1:8080
        book_id: 1
        credential_ref: bookstack-api
```

凭证只放 `credential_vault`，**禁止**写进 config.yaml / jobs.json / 报告 / 日志：

```bash
# 隐藏输入；也可 --stdin 从管道读（值仍不打印）
vigil credential store bookstack-api
# 值为 token_id:token_secret
vigil credential list
vigil credential expire bookstack-api
```

`credential_vault` 落在 `<VIGIL_HOME>/secrets/`（0600 + owner 校验）。`retrieve()`
失败时 `vigil digest --publish bookstack` 返回 4，并提示先用
`vigil credential store <ref>` 写入。

## 定时任务：weekly-oplog 蓝图

蓝图 key `weekly-oplog`（`cron/blueprint_catalog.py`），槽位：
`time / day / window / publish / deliver`。填参生成的 job：

1. 默认 agent 模式：`script=builtin:digest`，`script_args` 只产 facts；prompt 按
   `facts.days` 分组写五段，表格逐值照抄，写完调用 `publish_bookstack` 发布。
   `publish_bookstack` 只挂在 `bookstack` toolset，蓝图会把它加进该 job 的
   `enabled_toolsets`，不需要手动勾选。
2. `publish=file|none`：生成 no_agent job，`script_args` 为
   `--format md --publish <target>`，stdout 直接投递（确定性模板稿）。
3. `publish=bookstack` 但 `ops.digest.publish` 未配置：回退为 no_agent + file，
   job prompt 里明确写「未配置发布目标」，不静默失败。

job 里没有绝对脚本路径，也不需要 `~/.vigil/scripts/` 下的 wrapper。

## 内置脚本 / 兼容

- `builtin:digest` 是 cron 白名单里的内置脚本；创建期只允许 `builtin:digest`，
  未知名字返回明确错误。
- 运行期直接 import `ops.digest` 执行，不起子进程、不做路径 resolve；job 的
  `script_args` 作为 argv，job id/name 直接传给 `collect()`。
- 旧 job `--script oplog_digest.py` 仍可用：`cron/scripts/oplog_digest.py` 保留为
  `from ops.digest import main` 的薄 shim。
- 旧 `cron/scripts/oplog_digest.py` 的采集逻辑已搬到 `ops/digest.py`，facts 字段
  与抽取规则不变。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 参数错误（窗口/发布目标/格式组合） |
| 3 | state.db 不存在或打不开 |
| 4 | 报告写出或发布失败 |

发布失败必须非 0；stderr/stdout 不打印 token。

## 验证

```bash
python -m pytest tests/ops/test_digest.py -q
python -m pytest tests/ -q -k "digest or cron"
vigil digest --window 30d --format facts | head -30
vigil digest --window 45d --format md --out /tmp/d.md
```

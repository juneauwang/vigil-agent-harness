# 作业周报（oplog digest）

按「我提的任务 → Vigil 实际做了什么 → 改了哪些服务/参数」汇总一个时间窗，用于写周报和追踪变更。
与 `weekly-digest.md`（会话/计数为中心）互补：那份回答"这周跑了多少"，这份回答"这周我动了什么"。

## 命令

```bash
# 事实 JSON（喂给写稿 agent）
python cron/scripts/oplog_digest.py --db ~/.vigil/state.db --window 7d --format facts

# 直接出稿（无 LLM，确定性渲染）
python cron/scripts/oplog_digest.py --window 2026-W43 --format md --out /tmp/r.md
```

参数：`--window 7d|24h|2w`、`--week 2026-W43`、`--since/--until`、`--format facts|md`、`--out`、
`--exclude-job-id <id>`（可重复）。

## facts 结构

```
window{since,until,label}  totals{tasks,changes,by_kind,excluded_pipeline_sessions,active_days}
days[]{date, task_indexes, changes, kinds}        # 按天聚合，第 1 段照它分组
tasks[]{when,session,source,asks[],changes[],change_total,end_reason}
   changes[]{kind,target,detail,why}              # kind: 拓扑/文件/runbook/技能/记忆/发布/命令
gaps[]                                            # 数据缺项
```

## 变更抽取规则

| 来源 | kind | target | detail |
|---|---|---|---|
| `topo_update` | 拓扑 | 实体名 | 改动的字段=值（原样，不概括） |
| `write_file` / `patch` | 文件 | 路径 | 写/改 + 用途句（首个注释行，或 YAML kind/name） |
| `runbook_execute` | runbook | runbook 名 | env |
| `skill_manage` | 技能 | skill 名 | action / file_path |
| `memory` | 记忆 | target | 操作与内容摘要 |
| `publish_bookstack` | 发布 | 页名 | - |
| `terminal` | 命令 | 写动词标签（如 `kubectl:apply`、`helm:upgrade`、`scp`） | 命令摘要 ≤110 字 |

终端的写类判定**不用** `tools/action_classifier`（它把 `helm show chart` 判成 restore、
镜像存在性检查判成 install），改用写动词白名单：kubectl apply/delete/create/scale/rollout/patch、
helm install/upgrade/uninstall/rollback、systemctl start/stop/restart/reload/enable/disable、
docker run/rm/stop/build/push、docker compose up/down、scp/rsync、rm/mv/cp/chmod/chown、
git push/reset/commit/merge/rebase、DDL/DML、reboot/shutdown、curl 带 `-X POST`/`--data`。
只读与探测命令（echo/printf/ls/cat/grep/curl GET/helm show/kubectl get…）不入表。

## 自身会话排除

- cron 预跑脚本会收到 `VIGIL_CRON_JOB_ID` / `VIGIL_CRON_JOB_NAME`（`cron/scheduler._run_job_script` 注入）。
- **有 job id（定时跑）**：只排除当前 job 的 cron 会话 + 其派生 subagent，其他定时作业（如告警自动处置）照常计入。
- **无 job id（手动跑）**：排除全部 `cron` / `subagent` 会话——手动跑是为了记"我干了什么"。
- 已删除的历史测试 job 用 `--exclude-job-id` 手工清。

## 定时接法（写稿与发布分离）

```bash
# 18:00 写稿：agent 拿 facts 写五段（摘要/风险/下周由它撰写，表格逐值照抄）
vigil cron create "0 18 * * 5" "<见 oplog prompt>" --name "Vigil 作业周报（LLM 写稿）" \
  --script oplog_digest.sh --deliver local

# 18:10 发布：no-agent，把刚写出的 MD 按页名 create-or-update 发 BookStack（幂等）
vigil cron create "10 18 * * 5" --name "Vigil 周报发布（确定性）" \
  --script publish_latest_report.sh --deliver local --no-agent
```

脚本必须放在 `<VIGIL_HOME>/scripts/`，`--script` 只写文件名（绝对路径在创建期即被拒）。

## 验证

```bash
python cron/scripts/oplog_digest.py --window 45d --format md | head -40
```

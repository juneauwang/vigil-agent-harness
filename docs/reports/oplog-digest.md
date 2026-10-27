# 作业周报（oplog digest）— 已内置为 `vigil digest`

该功能已收进内置能力，完整用法、窗口语义、facts 契约、发布配置和蓝图说明见
[digest.md](digest.md)。本文件只保留旧入口的迁移说明，不再维护第二套说法。

## 命令迁移

```bash
# 旧
python cron/scripts/oplog_digest.py --window 7d --format facts

# 新
vigil digest --window 7d --format facts
```

旧脚本 `cron/scripts/oplog_digest.py` 保留为薄 shim（`from ops.digest import main`），
已注册的 `--script oplog_digest.py` job 仍能跑；新 job 不要再走物理脚本。

## 定时任务迁移

旧接法是两条 job：

1. 18:00 写稿：`--script oplog_digest.sh` + agent。
2. 18:10 发布：`--script publish_latest_report.sh --no-agent`。

新接法用 `weekly-oplog` 蓝图生成 **一条** job，`script` 为 `builtin:digest`，
agent 写五段后直接调 `publish_bookstack`；不需要 wrapper，也不需要第二条发布
job。未配置发布目标时蓝图回退为 no_agent + file。

详细步骤见 [digest.md](digest.md)。

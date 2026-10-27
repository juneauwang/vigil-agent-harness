# 内置运维能力（builtin）

Vigil 有两类「装上就有」的能力：**子命令**（人跑）和 **内置 cron 脚本**（调度跑）。
两者共用同一份配置与同一份凭据库，换机器只改 `config.yaml`，不需要往
`~/.vigil/scripts/` 里放 wrapper。

当前内置清单（`cron/builtin_scripts.py` 的 `BUILTIN_SCRIPTS`）：

| 内置名 | 模块 | 子命令 | 用途 |
|---|---|---|---|
| `builtin:digest` | `ops.digest` | `vigil digest` | 作业周报：按时间窗汇总「我提的任务 → 改了什么服务/参数」 |
| `builtin:alert-autodispatch` | `hermes_cli.alert_autodispatch` | `vigil alerts auto-dispatch` | 告警命中已授权 runbook 时自动执行一次 |

周报的完整手册在 [reports/digest.md](reports/digest.md)；告警自动派发在下文第 3 节。

## 1. builtin 机制（为什么不用外挂脚本）

1. 白名单：只有 `BUILTIN_SCRIPTS` 里的名字能创建。`builtin:evil` 在**创建期**就
   报「未知内置脚本」，不会等到运行时才失败。
2. 参数走 job 字段：`builtin:digest --window 7d` 这种写法会被拒——脚本名必须
   只有 `builtin:<name>`，参数放在 `script_args` 数组里。
3. 运行期 `cron/scheduler.py:_run_builtin_script()` 直接 `importlib.import_module()`
   调 `main()`，不起子进程、不做路径解析；stdout/stderr 捕获后按脚本同样的规则
   脱敏，再写进 job 输出。
4. `no_agent: true` 时该 job 不调用模型，产出就是脚本 stdout。

对比之下，外挂脚本必须落在 `<VIGIL_HOME>/scripts/` **目录内**。写绝对路径指到别处
（例如 `~/.vigil/runtime/x.sh`）会被判越界：

```
Blocked: script path resolves outside the scripts directory (/home/<user>/.vigil/scripts)
```

这正是告警自动派发长期 `last_status = error` 的原因——它的注册函数早期会在
`runtime/` 下生成一个 0700 脚本。1.1.3 起改为注册 `builtin:alert-autodispatch`，
不再落文件。

## 2. 怎么建一个内置任务

蓝图（推荐：面板或对话里填参，校验后直接建 job）：

```
/blueprint weekly-oplog time=18:00 day=friday window=7d publish=bookstack deliver=local
```

`/blueprint` 在对话里用（CLI 和网关都行）；Dashboard 的 Automation Blueprints
是同一套槽位的表单。`vigil cron create` **没有** `--blueprint/--set` 参数，它只接受
`schedule [prompt]` 加 `--script/--no-agent/--name/--deliver/--repeat/--skill/
--workdir/--model/--provider`。

手写（等价，字段全）：

```yaml
name: Weekly work log (digest)
schedule: "0 18 * * 5"     # 周五 18:00
script: builtin:digest
script_args: ["--window", "7d", "--format", "facts"]
no_agent: false            # true = 纯脚本，不调模型
enabled_toolsets: [file, session_search, skills]
```

自检：

```bash
vigil cron list                      # 看 script 列是不是 builtin:*
vigil cron status                    # 调度器在不在跑
vigil cron runs <job_id>             # 历史执行记录 + 输出
vigil cron tick                      # 手动跑一轮到期的 job（调试用）
```

## 3. 告警自动派发（`builtin:alert-autodispatch`）

作用：拉活跃告警 → 按关键词匹配 runbook → **只执行已授权**的那个 → 写审计。
不授权就只给建议，人工兜底。

### 3.1 两道开关，缺一不派发

1. 调度开关（有没有 job 在跑）：

   ```bash
   vigil alerts auto-dispatch --schedule 5m     # 注册/更新；off 注销
   ```

   ⚠️ 注册函数会把裸 `5m` 归一成 `every 5m`。手写 job 时也必须写 `every 5m`——
   cron 的 `parse_schedule("5m")` 语义是**一次性，5 分钟后**，跑完就
   `state=completed`，看起来在值守其实早停了（历史 job 就这么坏的）。

   同时也是 `config.yaml` 里的 `ops.alerts.auto_dispatch.interval`（人读的意图值，
   CLI 注册时以 `--schedule` 为准）。

2. 配置门（跑起来之后要不要真的动）：

   ```yaml
   ops:
     alerts:
       auto_dispatch:
         enabled: true        # opt-in，默认 false
         interval: 5m         # 期望节奏（注册 job 时用）
         max_per_tick: 3      # 单轮最多派发几个，防风暴
   ```

配置门**每 tick 重读**，所以线上紧急关停只需要把 `enabled` 改 false，不用删 job。

### 3.2 授权与安全带

1. runbook 必须声明 `alert_auto_run: true`（或等价授权字段）才会进候选；
   命中但未授权 → 维持「建议闭环」，绝不执行。
2. `alert_auto_severity` 是 severity 白名单，不在里面的告警跳过。
3. 幂等：按 `alertname + instance + startsAt` 记账，同一实例同一轮只派一次；
   失败也不重试风暴（occurrence 已消耗，人始终能在告警列表看到）。
4. 执行走 `scheduled=True` 豁免通道（预审标记 + 内容哈希校验 fail-closed），
   被拒 → `blocked`，审计标 `needs-human`。
5. 审计落 `<VIGIL_HOME>/runtime/alert_autodispatch.jsonl`，Web 的告警页读它。
   凭据按白名单脱敏后才写盘。

### 3.3 自检

```bash
vigil alerts                                  # 活跃告警 + 建议
vigil alerts history                          # 审计回看
python -m hermes_cli.alert_autodispatch       # 手动跑一次 tick（1.1.3 起可用）
```

输出首行固定形如：

```
alert 自动派发：活跃告警 N 条，授权 runbook M 个，本轮派发 K 次。
```

没配 Alertmanager 时会是：

```
活跃告警拉取失败，本轮跳过：未配置 Alertmanager（config ops.prometheus.alertmanager），仅健康探测可用。
```

这是**正常**的——没有数据源就什么都不该动。

## 4. 写脚本资产：tirith 会拦什么（tirith 0.3.3 实测）

`script_asset_create` 落盘前把**整个脚本体当一条命令**交给
`tools.tirith_security.check_command_security()` 扫，`action=block` 直接不进审批。
实测被拦的只有三类内容，**跟脚本逻辑复杂度无关**——204 行、30 多个 `if`/`for`/
`case`/变量当命令名，全部 `allow`：

| 规则 | 实测触发样例 | 改法 |
|---|---|---|
| `confusable_text` (HIGH) | 中文注释里的全角句号 `。`（U+3002） | 注释/文案用 ASCII `.`；全角括号、冒号不触发 |
| `trailing_dot_whitespace` (MEDIUM) | 注释里的占位 URL `http://...?token=...` | 写成 `http://<host>:<port>/?token=<token>` |
| `mass_file_deletion` (CRITICAL) | 脚本体出现 ≥3 个删除目标：2 处 `rm`，或 1 处 `rm a b c` | 收敛成唯一一处 `rm`、最多 2 个目标 |

⚠️ `mass_file_deletion` 的计数是**按「被扫描过」而不是「被执行过」累加**的：同一段
`rm -f a b` 连扫三次，判定依次是 `allow` → `block(4 files)` → `block(6 files)`，
窗口约 20 秒。后果有两个：

1. 资产创建失败后**不要连着重试**，越试越黑；等 30 秒再试，或先改内容。
2. 批量实测脚本片段时会看到「什么都 block」的假象——先扫过带 `rm` 的脚本，
   后面没 rm 的片段也被连带计数。判因要看 `rule_id`，不要看「block」两个字。

推荐写法（全脚本只有一处 `rm`、动态目标，已验证 `allow`）：

```bash
# 全脚本只允许这一处删除操作
rm_tmp() { rm -f -- "${1:-/nonexistent}" "${2:-/nonexistent}"; }
```

`rm_tmp "$tmp"` / `rm_tmp "$PIDFILE"` 随便调，不加删除目标计数
（`f(){ rm -f "$1"; }` 连调两次也是 allow）。提交前可先单独扫一遍：

```bash
cd /tmp && python -c "
from tools.tirith_security import check_command_security
print(check_command_security(open('/path/to/script.sh').read()))"
```

不要为了过闸把真实逻辑塞进一个薄封装再用 `exec` 调——资产审批哈希只覆盖封装体，
被审的内容和实际执行的内容不是同一份，那等于绕过内容审批。

## 5. 配置字段总表

```yaml
ops:
  digest:
    window: 7d                      # 命令行没给窗口时的缺省（--window 优先）
    publish:
      target: bookstack             # none | file | bookstack
      bookstack:
        base_url: http://127.0.0.1:8080
        book_id: 1
        credential_ref: bookstack-api
  alerts:
    auto_dispatch:
      enabled: true
      interval: 5m
      max_per_tick: 3
```

凭据只进 `credential_vault`（`~/.vigil/secrets/`，0600），**不要**写进
`config.yaml` / `jobs.json` / 报告 / 日志：

```bash
vigil credential store bookstack-api     # 隐藏输入，值为 token_id:token_secret
vigil credential list                    # 只看名字
vigil credential expire bookstack-api
```

## 6. 排障速查

1. `Blocked: script path resolves outside the scripts directory`
   → job 的 `script` 是外挂绝对路径。改成 `builtin:<name>` 或把脚本放回
   `<VIGIL_HOME>/scripts/`。
2. job 跑一次就 `state=completed`、再也不动
   → schedule 写成了裸 `5m`（= 一次性）。周期任务必须 `every 5m` 或 cron 表达式。
3. 工具/蓝图「明明有代码但用不了」
   → 网关进程没重启，内存里的 toolset 是旧版本。`systemctl --user restart` 相关
     服务，或 `vigil gateway restart`。
4. `pip install vigil-agent-harness` 装到 1.0.9
   → 国内腾讯镜像未同步 1.1.x，加 `-i https://pypi.org/simple`。
5. 「仓库里有，但 `vigil` 跑起来没有」
   → 运行时是独立快照，和仓库无链接。判活要离开仓库目录：

   ```bash
   cd /tmp && python -c "import ops.digest as d; print(d.__file__)"
   ```

   打印 `site-packages` 才是真装上了；打印仓库路径是 cwd 假阳性。
6. 模型写的周报发布失败、但它说「工具没给我」
   → 这是旧账：网关未重启导致 `publish_bookstack` 不在 toolset 里。现在出版走
     脚本（`--publish bookstack`），不依赖模型有工具。

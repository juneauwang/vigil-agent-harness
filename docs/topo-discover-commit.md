# topo_discover 落盘：发现草案闭环

`topo_discover` 以前只返回发现片段，会话内无法把新实体写进拓扑；本变更补上
`commit` 分支，让 agent 能新建 `needs_review=true` 的草案。确认权仍在人手里：
落盘后用 `topo_query` 查看，再用 `topo_update` 逐条置 `needs_review=false`。

实现只复用既有 `discover_host` + `write_discovery(merge=True)`，没有重写探测层，
也没有新增权限裁决源。

## 默认行为（commit=false）

不落盘，行为与旧版完全一致：

- 返回 v0.4 发现片段：`host` / `services` / `details` / `hardware` /
  `pending_review` / `probes`；
- 追加 `_guide`，提示 `topo_query` 查看、`topo_update` 确认；
- 不创建 `topology.yaml`、`services/`、`entities/`、`hardware/`。

## commit=true 的语义

1. 仍先执行发现（只读探测），然后进入落盘分支。
2. `tier = _env_tier(env)`；`prod` 档位先过人工审批，审批不通过直接返回
   `tool_error(..., approved=False)`，**不写任何拓扑文件**。
3. 通过审批后调用 `write_discovery(home=<VIGIL_HOME>, discovery=..., merge=True)`。
   工具侧**永远不传 `force=True`**，不会整体替换用户拓扑。
4. 落盘内容全部是 `needs_review=true` 草案；工具不会替人把 `needs_review`
   置 false。
5. 返回：

```json
{
  "status": "committed",
  "written": ["services/<host>.yaml", "topology.yaml", "entities/...yaml", "hardware/<host>.yaml"],
  "topology": "/home/<user>/.vigil/topology.yaml",
  "index": "/home/<user>/.vigil/services/<host>.yaml",
  "appended": 2,
  "kept": 0,
  "merged": true,
  "drafts": ["<host>", "bookstack"],
  "all_needs_review": true,
  "next": "topo_query 查看待审实体 → topo_update 置 needs_review=false"
}
```

`drafts` 只列本次真正新写出的实体：新 host（若主机行是新建的）+
`appended` 新增服务。第二次同 host 重跑 `drafts` 为空、`appended=0`。

## prod 审批

- 档位来自 `_env_tier(env)`：`prod`、`uat`、名字含 `prod` 的环境都按 prod 处理。
- 审批调用：

```python
request_tool_approval(
    "topo_discover",
    "落盘 PROD 拓扑发现草案：host=..., env=prod，服务实体 N 个，原因：...",
    rule_key=f"topo_discover:commit:{host}:prod",
)
```

- `rule_key` 固定为 `topo_discover:commit:<host>:prod`，用于 `[a]lways`
  会话/永久放行与审计。
- 非交互场景（cron 等）由既有 approval 策略决定；未获批时 fail-closed，
  不落盘。
- 非 prod 档位不弹审批。

## 幂等与合并边界

合并由 `write_discovery` 的既有语义负责：

1. 同 host 第二次 commit：同名服务跳过，`appended=0`、`kept` 等于现有服务行数，
   服务行数和实体文件数不增加。
2. 已存在同名服务的人工字段（`endpoint` / `type` / `managed_by` /
   `needs_review` 等）**逐字保留**，不会被发现结果覆盖。
3. host 行的治理字段（`owner` / `department` / `os` / `role` / `runtime` /
   `credentials` 等）保留，只把 `last_verified` 刷新为当天。
4. 新服务追加到 `services/<host>.yaml`，并写对应 `entities/*.yaml` 档案；
   已存在的实体档案不重写。
5. `pending_review`（无监听端口的 systemd 服务、unidentified 端口）不写入
   正式服务索引，仍只在返回片段里待人工确认。
6. 写入始终是 v0.4：`version: 4`，不出现 `sources` / `cross_host` /
   `key_paths` 等旧顶层字段，host 行不带 `services_index`。

`--force` / `force=true` 只保留 CLI 对齐，工具侧不消费；整体替换用户拓扑不在
本工具的权限范围内。

## 回滚

工具不自动回滚。落盘前按下面的目录约定做全量备份（只备份 `topology.yaml`
不够，服务索引和实体档案也会新增）：

```bash
BK=/home/wpwang/.vigil/topology-backup-$(date +%Y%m%d-%H%M%S)
mkdir -p "$BK"
cp -a /home/wpwang/.vigil/topology.yaml "$BK"/
[ -d /home/wpwang/.vigil/services ] && cp -a /home/wpwang/.vigil/services "$BK"/
[ -d /home/wpwang/.vigil/entities ] && cp -a /home/wpwang/.vigil/entities "$BK"/
[ -d /home/wpwang/.vigil/hardware ] && cp -a /home/wpwang/.vigil/hardware "$BK"/
```

回滚时反向覆盖：

```bash
cp -a "$BK/topology.yaml" /home/wpwang/.vigil/topology.yaml
cp -a "$BK/services/." /home/wpwang/.vigil/services/
cp -a "$BK/entities/." /home/wpwang/.vigil/entities/
cp -a "$BK/hardware/." /home/wpwang/.vigil/hardware/
```

建议在回滚前停掉正在跑的 agent/cron，避免写入竞争；回滚后跑
`vigil topo status` 或 `topo_query` 复核。

## 使用示例

只预览：

```json
{"host": "127.0.0.1", "env": "local"}
```

落盘草案：

```json
{
  "host": "127.0.0.1",
  "env": "local",
  "commit": true,
  "reason": "登记本机 BookStack 等服务"
}
```

落盘后确认（由人决定）：

```json
{"entity": "bookstack", "updates": {"needs_review": false}}
```

## 测试

```bash
python -m pytest tests/tools/test_topo_discover_commit.py -q
python -m pytest tests/tools -q -k topo
```

第一条覆盖 7 个场景：commit=false 不落盘、local 落盘、人工字段保留、幂等、
prod 审批拒绝、prod 审批通过、v0.4 结构不降级。全部用注入假 runner，不连真机。

## 已知限制

1. `pending_review` 不落盘，仍需人工确认后另行登记。
2. `drafts` 只表示本次新写出的实体；已存在的待审实体不会重复列出。
3. 落盘来源是 `source=discovered`；`topo_update` 确认时会写成 `source=agent`。
4. 审批只覆盖 prod 档位；非 prod 的新建不做二次确认（按任务定调：prod 才
   是矩阵裁决边界）。

## 生命周期：发现 → 变更 → 删除

三个写入口现在齐了，语义按同一个档位口径：

| 阶段 | 工具 | 落盘 | 审批 |
|---|---|---|---|
| 发现（新增） | `topo_discover(commit=true)` | `needs_review=true` 草案 | prod 档 `rule_key=topo_discover:commit:<host>:prod` |
| 变更 / 审核 | `topo_update(entity, updates)` | 改字段 / 置 `needs_review=false` | prod 档 `rule_key=topo_update:prod:<entity>` |
| 删除 | `topo_delete(entity, reason, force)` | 归档后摘行 | prod 档 `rule_key=topo_delete:<env>:<entity>` |

### `topo_delete` 的六步与边界

1. 定位：`clusters` → `hosts` → `cross_host` → `services`（服务行带 `_host`，
   摘行时改的是**读到的那个索引文件**，`_host_index_path` 读写共用）。
2. 引用扫描：别的实体的 `depends_on` / `depended_by`、cluster 成员、服务行的
   `host`。有引用且未 `force` → 报错并列出引用者，什么都不动。
3. 审批：prod 档走 `request_tool_approval`；非 prod 按既有矩阵语义放行
   （与 discover/update 一致，不新造规则）。拒绝 → 什么都不动。
4. 归档：`<VIGIL_HOME>/topology-archive/<YYYY-MM-DD>/<entity>.yaml`，含被删行、
   L3 档案 `detail`、`removed` 清单、`reason`、`deleted_at`。**写归档失败即中止**，
   不允许「删了没归档」。
5. 摘行：拔 L3 档案文件 → 从 `topology.yaml`（或 `services/<host>.yaml`）摘掉那一行，
   L1 行删除时 `updated_at=today`。
6. 审计：`record_event(type="topo_delete", action="delete <entity>",
   meta={env, archived, forced, referrers, reason, operator})`，best-effort。

不做：批量/pattern 删除、级联删 runbook/技能/凭据（引用只用于拦）、v0.1 扁平
`core_entities` 结构（报错带迁移提示）。

**CLI 入口暂缺**：工具层对 prod 走人工审批门，而 `request_tool_approval` 在
非交互上下文是 fail-closed —— 直接包一层 `vigil topo delete` 会得到「prod 实体
永远删不掉」的假功能。要做必须先定一条「终端已人工确认」的通道（例如 `--yes`
显式视为人工确认并跳过工具门），属独立决策。

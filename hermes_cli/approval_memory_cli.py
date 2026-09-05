"""``vigil approvals list / forget / export`` —— 命令级自进化白名单管理（batch86）。

红线 3（可查看/撤销/审计）的 CLI 层：白名单全量查看、一键撤销、一键导出，全部
动作记 trajectory 审计。数据文件 ``<hermes home>/approval_memory.yaml``（默认
不存在 = 空白名单 + 内置只读种子表兜底），核心读写都在
:mod:`tools.approval_memory`——本模块只做 CLI 呈现与参数落地。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

from pathlib import Path

from hermes_cli.i18n import t


def _status_label(status: str) -> str:
    """状态列标签（i18n；未知状态原样返回）。"""
    if status == "active":
        return t("approvals_mem.status_active", "active（沉淀完成，下次跳过审批）")
    if status == "pending":
        return t("approvals_mem.status_pending", "pending（计数中）")
    if status == "banned":
        return t("approvals_mem.status_banned", "banned（被拒过，永不自动沉淀）")
    if status == "inactive":
        return t("approvals_mem.status_inactive", "inactive（已撤销，回到弹审批）")
    return status


def _memory_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()).expanduser()


def _row_kind(row: Dict[str, Any]) -> str:
    """来源标注：seed = 内置只读种子（冷启动起点）；否则自进化/管理条目。"""
    if row.get("source_task") == "seed":
        return t("approvals_mem.kind_seed", "seed（内置只读种子）")
    return t("approvals_mem.kind_evolved", "自进化/管理")


def run_list(args) -> int:
    """``vigil approvals list``：白名单全量（种子 + 自进化分开标注）。"""
    try:
        from tools.approval_memory import list_rows
        rows = list_rows(home=_memory_home())
    except Exception as exc:
        print(t("approvals_mem.read_failed", "✗ 读取 approval_memory 失败：{exc}", exc=exc), file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return 0
    if not rows:
        print(t("approvals_mem.empty",
                "（空：approval_memory.yaml 不存在或无数目——仅内置种子在运行时生效）"))
        return 0
    header = (
        f"{t('approvals_mem.col_template', '模板'):<16}"
        f"{t('approvals_mem.col_status', '状态'):<46}"
        f"{t('approvals_mem.col_count', '次数'):>4}  "
        f"{t('approvals_mem.col_skip', '免问'):<5}"
        f"{t('approvals_mem.col_approved_at', '沉淀时间'):<28}"
        f"{t('approvals_mem.col_source', '来源')}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        count = row.get("success_count", 0)
        count_s = "—" if row.get("source_task") == "seed" else str(count)
        opt = "yes" if row.get("user_opt_in") else "no"
        approved_at = row.get("approved_at") or "—"
        print(
            f"{row['template']:<16}{_status_label(row['status']):<46}"
            f"{count_s:>4}  {opt:<5}{approved_at:<28}{_row_kind(row)}"
        )
    print()
    print(t(
        "approvals_mem.list_note",
        "说明：active = 沉淀完成下次跳过审批；banned = 被用户拒过永不自动沉淀"
        "（仅 forget 可解除）；种子默认 active（source=seed），文件条目可覆盖。",
    ))
    return 0


def run_forget(args) -> int:
    """``vigil approvals forget <template>``：撤销单条（置 inactive，计数清零）。"""
    template = str(getattr(args, "template", "") or "").strip().lower()
    try:
        from tools.approval_memory import forget
        if not template:
            print(t("approvals_mem.forget_requires_template",
                    "✗ 需要指定要撤销的命令模板（如：vigil approvals forget lscpu）"),
                  file=sys.stderr)
            return 2
        ok = forget(template, home=_memory_home())
    except Exception as exc:
        print(t("approvals_mem.forget_failed", "✗ 撤销失败：{exc}", exc=exc), file=sys.stderr)
        return 2
    if not ok:
        print(t("approvals_mem.forget_not_found",
                "✗ 模板 {template!r} 不在白名单（非种子也非已沉淀条目），无需撤销。",
                template=template),
              file=sys.stderr)
        return 1
    print(t(
        "approvals_mem.forgot",
        "✓ 已撤销 {template!r}（置 inactive、计数清零）：该命令回到弹审批状态。"
        "重新批准 3 次（或选『以后不用问』）可再沉淀。已审计。",
        template=template,
    ))
    return 0


def run_export(args) -> int:
    """``vigil approvals export``：一键导出（stdout YAML/JSON，备份/review）。"""
    try:
        from tools.approval_memory import list_rows, seed_templates
        rows = list_rows(home=_memory_home())
        seeds = sorted(seed_templates())
    except Exception as exc:
        print(f"✗ 读取 approval_memory 失败：{exc}", file=sys.stderr)
        return 2
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "description": t(
            "approvals_mem.export_description",
            "命令级自进化白名单 v0.1（approval_memory.yaml）——用户批准的只读命令"
            "模板沉淀为 active，下次跳过审批；seeds 为内置只读种子（冷启动起点，"
            "默认 active 不在文件里）。",
        ),
        "seeds": seeds,
        "entries": rows,
    }
    try:
        from agent.trajectory import record_event
        record_event(
            type="approval_memory", session_id="approvals-cli",
            action="export", meta={"template": "", "action": "export",
                                   "entries": len(rows)},
        )
    except Exception:
        pass  # 导出审计失败不阻断导出
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        import yaml
        print(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False,
                             default_flow_style=False), end="")
    return 0


def run_approvals_memory_command(args) -> int:
    """main.py cmd_approvals 分发：list / forget / export。"""
    sub = str(getattr(args, "approvals_command", "") or "").strip()
    if sub == "list":
        return run_list(args)
    if sub == "forget":
        return run_forget(args)
    if sub == "export":
        return run_export(args)
    print(f"unknown approvals subcommand: {sub}", file=sys.stderr)
    return 2

"""YAPL P3 操作矩阵只读工具（yapl-design.md §11.7 安全边界）。

LLM 侧只有 ``matrix_query`` 读接口——查某 action × env 的档位（execute /
approve / {approve: required}）+ 每格来源，供 runbook 执行前自检/汇报。
**实现上无任何写接口**：修改矩阵 = 人工操作（``vigil matrix`` CLI / UI
表格页），本模块不 import 也不调用 matrix_data 的任何写函数。工具描述
明确告知 LLM 这一点，防幻觉 set 路径。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from tools.matrix_data import (
    actions,
    get_level,
    load_matrix_or_empty,
)
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


def matrix_query(action: str, env: str, home: Optional[Any] = None) -> str:
    """查操作矩阵档位（只读）：action × env → execute / approve / {approve: required}。

    热生效：每次调用实时读 matrix.yaml（不缓存）；文件缺失 → 空矩阵（全部动作
    默认 approve 保守）。查不到（环境/动作未配置）→ 默认 approve + 提示漏配。
    """
    act = str(action or "").strip()
    env_name = str(env or "").strip()
    if not act:
        return tool_error("action 必填——动作词表（schemas.yaml actions）：" + ", ".join(actions()))
    if act not in set(actions()):
        return tool_error(
            f"action 非法: {act!r}——合法动作：{', '.join(actions())}"
            "（schemas.yaml actions，§10.2）。"
        )
    if not env_name:
        return tool_error("env 必填——矩阵环境名（如 local/dev/prod，见 matrix.yaml 键）。")

    data = load_matrix_or_empty(home)
    result = get_level(data, env_name, act)
    warnings = list(data.get("warnings") or [])

    note = ""
    if not result["configured"]:
        note = (
            f"matrix.{env_name}.{act} 未配置——默认 approve（保守）。"
            "矩阵是人工安全资产：修改请走 vigil matrix CLI 或 dashboard UI，"
            "LLM 无 set 路径。"
        )
    payload = {
        "action": act,
        "env": env_name,
        "level": result["level"],
        "source": result["source"],
        "configured": result["configured"],
        "note": note,
    }
    if warnings:
        payload["warnings"] = warnings
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _query_handler(args: Dict[str, Any], **kwargs) -> str:
    return matrix_query(
        action=args.get("action", ""),
        env=args.get("env", ""),
    )


def check_matrix_requirements() -> bool:
    """矩阵工具可用性：ops.matrix.enabled: false 显式关闭；否则恒可用。

    与 runbook 不同，matrix_query 不按数据存在性门控——设计（§11.2/11.7）：
    文件缺失 → 空矩阵（全部动作默认 approve 保守）+ warning。矩阵是安全资产，
    查询必须永远可用（哪怕尚未 init）。
    """
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        return (cfg.get("ops", {}) or {}).get("matrix", {}).get("enabled") is not False
    except Exception:
        return True


_DEFAULT_QUERY_SCHEMA = {
    "name": "matrix_query",
    "description": (
        "查询操作矩阵档位（只读，YAPL §11）：某动作 × 环境 → execute / approve / "
        "{approve: required}，返回每格来源（模板名 or manual）。"
        "操作矩阵是权限唯一裁决（runbook 每步执行时查；terminal 直跑后续同表）。"
        "参数：action（必填，动作词表枚举）、env（必填，环境名如 local/dev/prod）。"
        "查不到 action×env → 默认 approve（保守）。"
        "**矩阵是人工安全资产：修改 = 人工操作（vigil matrix CLI / dashboard UI），"
        "LLM 无 set 路径**——不要尝试创建/修改矩阵，也不要在 runbook/命令里内联"
        "矩阵档位。执行前自检/汇报用本工具即可。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "动作枚举（schemas.yaml actions）：start/stop/restart/reload/"
                    "enable/disable/reboot/shutdown/deploy/rollback/scale/decommission/"
                    "backup/restore/apply_config/query/fetch_log/verify/transfer_file/"
                    "run_script/install/upgrade/remove。",
            },
            "env": {
                "type": "string",
                "description": "环境名（matrix.yaml 键，如 local/dev/prod/test/uat）。",
            },
        },
        "required": ["action", "env"],
    },
}


registry.register(
    name="matrix_query",
    toolset="matrix",
    schema=_DEFAULT_QUERY_SCHEMA,
    handler=_query_handler,
    check_fn=check_matrix_requirements,
    emoji="🧭",
    max_result_size_chars=8_000,
)

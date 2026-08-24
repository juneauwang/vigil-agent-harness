"""YAPL 主框架阶段 B：编译契约的信任层运行时。

生成代码（LLM 产出，不可信）只允许 import 白名单模块（tools.runbook_handlers /
tools.runbook_exec / tools.topo_tools / tools.topo_ref / 本模块）——执行细节
全部落在信任层：目标富化（resolve_target）、命令生成（generate_commands）、
真实通道（runbook_exec._run_spec）或注入 runner（沙箱 mock）。生成代码不
直接 subprocess/ssh（越界检查 + tirith + 沙箱三重兜底）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


def run_specs(specs: List[Dict[str, Any]], target: Dict[str, Any], *,
              home, runner: Optional[Callable] = None) -> List[Dict[str, Any]]:
    """逐条执行命令规格（P4 handler 通道）。

    ``runner`` 注入时（沙箱 mock）直接使用，不碰真实通道；None = 真实执行
    （``runbook_exec._run_spec``：local / ssh / sudo / transfer / script_asset）。
    """
    results: List[Dict[str, Any]] = []
    for spec in specs or []:
        if runner is not None:
            results.append(runner(spec, target))
        else:
            from tools.runbook_exec import _run_spec
            results.append(_run_spec(home, target, spec))
    return results


def any_spec_failed(results: List[Dict[str, Any]]) -> bool:
    """任一条命令非零退出 → 执行失败（薄包装据此映射 execution_failed）。"""
    return any((r or {}).get("exit_code") != 0 for r in (results or []))


__all__ = ["run_specs", "any_spec_failed"]

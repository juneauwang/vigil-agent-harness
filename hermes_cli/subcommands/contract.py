"""``vigil contract`` —— YAPL 契约管理（阶段 B：编译生成管线）。

设计依据：yapl-design.md 第十三章 §13.4（2026-08-24 盘完）。管线：
契约 → LLM 生成薄包装 → 沙箱跑 tests 自证 → 资产审批（tirith + 内容审批）
→ 注册进工具表（toolset=contract，注册后 LLM 会话可见可直调）。

命令：
    vigil contract compile <name> [--yes] [--max-retries N]
        编译管线（生成 → 自证 → 审批 → 注册）。--yes = 非交互模式下 CLI
        显式同意注册审批（等价交互会话里点一次）；不带 --yes 走标准交互
        审批门（无人在场 fail-closed，不落注册）。
    vigil contract list
        列出已注册契约（registry.yaml 记录：name/action/status/compiled_at/
        approved_by/path）。
"""

from __future__ import annotations

import json
import os
import sys


def build_contract_parser(subparsers, *, cmd_contract) -> None:
    """Attach the ``contract`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "contract",
        help="YAPL 契约管理：compile/list（阶段 B 编译生成管线）",
        description=(
            "YAPL 契约（~/.vigil/contracts/）编译管理：compile 走生成 → 沙箱 "
            "自证 → 资产审批 → 注册工具表；list 查已注册契约。"
        ),
    )
    sub = parser.add_subparsers(dest="contract_command", metavar="{compile,list}")

    compile_parser = sub.add_parser(
        "compile",
        help="编译契约（生成薄包装 → 沙箱自证 → 审批 → 注册）",
        description=(
            "读 contracts/<name>.yaml → 校验（阶段 A）→ LLM 生成薄包装 → 沙箱 "
            "跑 tests 自证 → tirith + 内容审批 → 注册进工具表。注册后 LLM 会话"
            "可见可直调；--yes 在非交互模式下显式同意注册审批。"
        ),
    )
    compile_parser.add_argument(
        "name", help="契约名（contracts/<name>.yaml）"
    )
    compile_parser.add_argument(
        "--yes", action="store_true",
        help="非交互：CLI 显式同意注册审批（等价交互会话点一次）",
    )
    compile_parser.add_argument(
        "--max-retries", type=int, default=3,
        help="生成-自证循环上限（语法错/越界/自证失败都算一次；默认 3）",
    )
    compile_parser.set_defaults(func=cmd_contract)

    list_parser = sub.add_parser(
        "list",
        help="列出已注册契约（registry.yaml）",
        description=(
            "列出已注册编译契约：name/action/status/compiled_at/approved_by/"
            "path——加载时已注册工具由启动钩子热加载进运行时工具表。"
        ),
    )
    list_parser.set_defaults(func=cmd_contract)


def _approving_callback(command: str, description: str, **kw) -> str:
    """CLI --yes 审批回调：用户已在命令行显式同意注册审批。"""
    return "once"


def _run_compile(args) -> int:
    from tools.contract_compile import compile_contract

    if getattr(args, "yes", False):
        # 用户显式 --yes = 同意注册审批；置交互标记让资产审批门走回调路径。
        os.environ.setdefault("VIGIL_INTERACTIVE", "1")
        approval_callback = _approving_callback
    else:
        approval_callback = None
    result = compile_contract(
        name=getattr(args, "name", ""),
        approval_callback=approval_callback,
        max_retries=max(1, int(getattr(args, "max_retries", 3) or 3)),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "registered" else 1


def _run_list(args) -> int:
    from tools.contract_compile import registered_contracts

    registry = registered_contracts()
    if not registry:
        print(json.dumps({
            "contracts": [],
            "note": "无已注册编译契约——用 `vigil contract compile <name>` 编译注册",
        }, ensure_ascii=False, indent=2))
        return 0
    rows = []
    for name, rec in sorted(registry.items()):
        rows.append({
            "name": name,
            "action": rec.get("action"),
            "status": rec.get("status"),
            "compiled_at": rec.get("compiled_at"),
            "approved_by": rec.get("approved_by"),
            "path": rec.get("path"),
        })
    print(json.dumps({"contracts": rows}, ensure_ascii=False, indent=2))
    return 0


def run(args) -> int:
    """dispatch（main.py cmd_contract 入口）。"""
    sub = getattr(args, "contract_command", None)
    if sub == "compile":
        return _run_compile(args)
    if sub == "list":
        return _run_list(args)
    print(
        "usage: vigil contract <compile <name> [--yes] | list>\n"
        "\n"
        "YAPL 契约（阶段 B）：compile = 生成 → 沙箱自证 → 审批 → 注册；"
        "list = 查已注册契约。",
        file=sys.stderr,
    )
    return 1

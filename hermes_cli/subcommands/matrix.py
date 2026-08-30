"""``vigil matrix`` —— 操作矩阵管理（YAPL P3，yapl-design.md §11.7 定案）。

矩阵是**人工安全资产**：LLM 永不改矩阵（只有 matrix_query 只读工具），修改
一律走人工通道——本 CLI 四命令（+ init 生成）+ dashboard UI 表格页。修改即
审计（record_event 落 trajectory，`vigil trajectory` / /api/audit/events 可查）。

命令：
    vigil matrix init [--template 1|2|3|4] [--force]
        生成 matrix.yaml（setup 四模板，§11.3；模板 4 = 三步级联多选）。
    vigil matrix show [--json]
        全量矩阵（含每格来源：模板名 or 手动改 + 漏配默认 approve 警告）。
    vigil matrix set <action> <env> <level>
        单格改档位（execute / approve / required）+ 审计。
    vigil matrix edit
        $EDITOR 打开编辑（批量），保存时校验（结构/枚举/无 deny 语义）。
    vigil matrix reset [--template N]
        回退到基底模板（放弃手动改动）+ 审计。
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any, Callable, Dict, List, Optional

from tools.matrix_data import (
    TEMPLATE_LABELS,
    TEMPLATE_NAMES,
    MatrixValidationError,
    actions,
    edit_matrix,
    init_matrix,
    load_matrix_or_empty,
    matrix_path,
    reset_to_template,
    set_level,
)

_LEVEL_DISPLAY = {
    "execute": "execute",
    "approve": "approve",
    "required": "{approve: required}",
}


def build_matrix_parser(subparsers, *, cmd_matrix: Callable) -> None:
    """Attach the ``matrix`` subcommand to ``subparsers``."""
    matrix_parser = subparsers.add_parser(
        "matrix",
        help="操作矩阵管理（YAPL §11）：init/show/set/edit/reset",
        description=(
            "操作矩阵（matrix.yaml，权限唯一裁决）：init 用四模板生成；show 全量"
            "查看（含每格来源）；set 单格改档位；edit 编辑器批量改；reset 回退"
            "模板。矩阵是人工安全资产，修改即审计——LLM 只有 matrix_query 只读"
            "工具，无 set 路径。"
        ),
    )
    matrix_sub = matrix_parser.add_subparsers(dest="matrix_command", metavar="{init,show,set,edit,reset}")

    init_parser = matrix_sub.add_parser(
        "init",
        help="用 setup 模板生成 matrix.yaml（模板 1/2/3/4）",
        description=(
            "生成操作矩阵（~/.vigil/matrix.yaml）。模板 1 单人本地 / 模板 2 小团队"
            "（local/test/dev/prod）/ 模板 3 中型团队（local/test/uat/dev/prod）/ 模板 4 自定义"
            "（三步级联多选）。已存在时报错（安全资产防误覆盖），--force 可重建。"
        ),
    )
    init_parser.add_argument(
        "--template", "-t", type=int, default=2, choices=(1, 2, 3, 4),
        help="模板编号（默认 2：小团队）",
    )
    init_parser.add_argument(
        "--force", action="store_true",
        help="已存在时覆盖重建（默认拒绝，防误覆盖）",
    )
    init_parser.set_defaults(func=cmd_matrix)

    show_parser = matrix_sub.add_parser(
        "show",
        help="全量矩阵（含每格来源 + 漏配默认 approve 警告）",
        description="展示 matrix.yaml 全量档位，每格来源（模板名 or 手动改）与漏配警告。",
    )
    show_parser.add_argument("--json", action="store_true", help="JSON 输出（脚本/测试用）")
    show_parser.set_defaults(func=cmd_matrix)

    set_parser = matrix_sub.add_parser(
        "set",
        help="单格改档位：vigil matrix set <action> <env> <level>",
        description="把某动作×环境的档位改为 execute / approve / required（required = 强制人工）。修改即审计。",
    )
    set_parser.add_argument("action", help="动作（schemas.yaml actions，如 restart）")
    set_parser.add_argument("env", help="环境名（如 local/dev/prod）")
    set_parser.add_argument("level", choices=("execute", "approve", "required"),
                            help="execute / approve / required（{approve: required} 强制人工）")
    set_parser.set_defaults(func=cmd_matrix)

    edit_parser = matrix_sub.add_parser(
        "edit",
        help="打开编辑器批量修改（保存时校验，变动的格子来源变 manual）",
        description="$EDITOR 打开 matrix.yaml 副本，保存时校验（结构/枚举/无 deny 语义），"
                    "通过后原子落盘，变动的格子来源 → manual，修改即审计。",
    )
    edit_parser.add_argument("--editor", default=None, help="编辑器命令（默认 $EDITOR → nano/vim/vi）")
    edit_parser.set_defaults(func=cmd_matrix)

    reset_parser = matrix_sub.add_parser(
        "reset",
        help="回退到基底模板（放弃全部手动改动）",
        description="把矩阵回退到生成时的 setup 模板（base_template），放弃全部手动改动，"
                    "每格来源恢复模板名。修改即审计。",
    )
    reset_parser.add_argument(
        "--template", "-t", type=int, default=None, choices=(1, 2, 3),
        help="显式指定回退模板（默认用 base_template）",
    )
    reset_parser.set_defaults(func=cmd_matrix)

    def _matrix_help(_args) -> int:
        matrix_parser.print_help()
        return 0

    matrix_parser.set_defaults(func=_matrix_help)


def _audit(*, env: str, act: str, old: Any, new: Any, source: str, extra: str = "") -> None:
    """修改即审计（best-effort：失败只记日志，绝不阻断修改）。"""
    try:
        from agent.trajectory import record_event
        record_event(
            type="matrix_change",
            session_id="matrix-cli",
            tool="matrix",
            action=f"{extra}{env}.{act}".strip(".") if extra else f"{env}.{act}",
            result=f"{_fmt(old)} -> {_fmt(new)}",
            approval="",
            meta={
                "env": env,
                "action": act,
                "old": old if old is None else str(old),
                "new": new,
                "source": source,
                "operator": getpass.getuser(),
            },
        )
    except Exception:
        import logging
        logging.getLogger(__name__).debug("matrix audit event failed", exc_info=True)


def _fmt(level: Any) -> str:
    if level is None:
        return "(未配置)"
    return _LEVEL_DISPLAY.get(str(level), str(level))


def _print_matrix(data: Dict[str, Any], *, json_out: bool = False) -> None:
    if json_out:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    matrix = data.get("matrix") or {}
    sources = data.get("sources") or {}
    if not matrix:
        print("（矩阵为空——全部动作默认 approve 保守。先运行 vigil matrix init --template N）")
        return
    actions_ = actions()
    envs = list(matrix.keys())
    # 表头：环境 × 动作；格子显示档位缩写 + 来源标记（m=手动改）
    header = f"{'动作':<16}" + "".join(f"{env:>22}" for env in envs)
    print(header)
    print("-" * len(header))
    for act in actions_:
        row = f"{act:<16}"
        for env in envs:
            level = (matrix.get(env) or {}).get(act)
            src = ((sources.get(env) or {}).get(act) or "")
            if level is None:
                cell = "approve*"
            else:
                cell = _LEVEL_DISPLAY.get(level, level)
            marker = "·m" if src == "manual" else ""
            row += f"{cell+marker:>22}"
        print(row)
    print(f"\n来源：模板名 or ·m=手动改；approve* = 漏配默认 approve（保守）。"
          f"顶层来源：{data.get('source') or '未生成'}；base_template: {data.get('base_template') or '-'}")
    for warn in data.get("warnings") or []:
        print(f"⚠ {warn}")


def _cmd_init(args) -> int:
    from tools.matrix_data import init_matrix
    template = f"template{args.template}"
    print(f"生成操作矩阵（{TEMPLATE_LABELS.get(template, template)}）...")
    try:
        data = init_matrix(template, force=bool(args.force))
    except FileExistsError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    except (ValueError, MatrixValidationError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    _audit(env="*", act="init", old=None, new=f"template{args.template}", source="cli",
           extra=f"init template{args.template} ")
    print(f"✓ 已写入 {data['path']}（热生效，无需重启服务）")
    _print_matrix(data)
    return 0


def _cmd_show(args) -> int:
    data = load_matrix_or_empty()
    _print_matrix(data, json_out=bool(args.json))
    return 0


def _cmd_set(args) -> int:
    level = args.level
    if level == "required":
        level = "required"
    try:
        data = load_matrix_or_empty()
        summary = set_level(data, args.env, args.action, level)
    except (ValueError, MatrixValidationError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    from tools.matrix_data import write_matrix
    write_matrix(data)
    _audit(env=args.env, act=args.action, old=summary["old"], new=level, source="cli")
    if not summary["changed"]:
        print(f"（{args.env}.{args.action} 已是 {_fmt(level)}，无变化）")
        return 0
    print(f"✓ {args.env}.{args.action}: {_fmt(summary['old'])} -> {_fmt(level)}（已审计）")
    return 0


def _cmd_edit(args) -> int:
    result = edit_matrix(editor=args.editor)
    if result["errors"]:
        for err in result["errors"]:
            print(f"✗ {err}", file=sys.stderr)
        return 1
    for warn in result["warnings"]:
        print(f"⚠ {warn}")
    if result["changed_cells"]:
        _audit(env="*", act="edit", old=None, new=f"{result['changed_cells']} 格", source="cli",
               extra=f"edit {result['changed_cells']} 格 ")
        print(f"✓ 已保存：{result['changed_cells']} 格变动（来源 → manual，已审计）")
    else:
        print("（无变动，矩阵保持不变）")
    return 0


def _cmd_reset(args) -> int:
    try:
        data = load_matrix_or_empty()
        template = f"template{args.template}" if args.template else None
        reset_to_template(data, template=template)
    except (ValueError, MatrixValidationError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    from tools.matrix_data import write_matrix
    write_matrix(data)
    base = data.get("base_template") or template or "?"
    _audit(env="*", act="reset", old="manual", new=base, source="cli",
           extra=f"reset {base} ")
    print(f"✓ 已回退到 {base}（放弃全部手动改动，已审计）")
    _print_matrix(data)
    return 0


def run(args) -> int:
    """argv Namespace → 子命令分发（测试/独立入口）。"""
    sub = getattr(args, "matrix_command", None)
    if sub == "init":
        return _cmd_init(args)
    if sub == "show":
        return _cmd_show(args)
    if sub == "set":
        return _cmd_set(args)
    if sub == "edit":
        return _cmd_edit(args)
    if sub == "reset":
        return _cmd_reset(args)
    print("usage: vigil matrix <init|show|set <action> <env> <level>|edit|reset>",
          file=sys.stderr)
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    """独立入口（测试/直接调用）：argv → Namespace → run。"""
    parser = argparse.ArgumentParser(
        prog="vigil matrix",
        description="操作矩阵管理（YAPL §11.7）：init/show/set/edit/reset",
    )
    sub = parser.add_subparsers(dest="matrix_command")
    init_parser = sub.add_parser("init", help="生成矩阵（四模板）")
    init_parser.add_argument("--template", "-t", type=int, default=2, choices=(1, 2, 3, 4))
    init_parser.add_argument("--force", action="store_true")
    show_parser = sub.add_parser("show", help="全量矩阵")
    show_parser.add_argument("--json", action="store_true")
    set_parser = sub.add_parser("set", help="单格改档位")
    set_parser.add_argument("action")
    set_parser.add_argument("env")
    set_parser.add_argument("level", choices=("execute", "approve", "required"))
    edit_parser = sub.add_parser("edit", help="编辑器批量改")
    edit_parser.add_argument("--editor", default=None)
    reset_parser = sub.add_parser("reset", help="回退模板")
    reset_parser.add_argument("--template", "-t", type=int, default=None, choices=(1, 2, 3))
    args = parser.parse_args(argv)
    if not getattr(args, "matrix_command", None):
        parser.print_help()
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

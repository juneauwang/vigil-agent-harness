"""task40 守卫：schema ↔ handler 接线全量扫描（防止整类"静默漏转发"）。

背景（2026-09-28 手工发现）：``runbook_create`` 的 schema 声明了
``alert_auto_run`` / ``alert_auto_severity``，但注册 handler ``_create_handler``
没往下传 → 经工具路径根本开不了告警自动派发，且是**静默失败**（工具返回成功、
落盘 YAML 里没这行）。``cronjob`` 的 ``attach_to_session`` 同型。本测试把这一整类
钉死：**对注册表里每个工具的 schema ``properties`` 键名，断言它必须能在该工具的
handler 里以字符串字面量 / 关键字实参名的形式出现**。缺席 = handler 漏接线。

判据（鲁棒版，防假通过）：用 AST 分析 handler 源码（``inspect.getsource`` →
``ast.parse``），收集**所有字符串常量 + 关键字实参名**，并**排除注释与 docstring**
（注释不在 AST 里，天然排除；docstring 会被显式跳过）。只在注释里出现 = 不算。

白名单：确属「整包转发」（handler 把 ``args`` 原样下传，逐键不出现）或「schema
暴露无效参数、另有归属」的工具，逐条附理由。白名单**不许**出现"暂时不知道"这类
理由；条目一旦不再缺席（说明已接线）会被 ``test_whitelist_entries_are_justified``
判为陈旧，需移除，避免白名单变成垃圾桶。
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Dict, FrozenSet, Iterable, Set

import pytest

# ---------------------------------------------------------------------------
# 白名单：工具名 → 理由（每条必须写清"为什么它不是漏接线"）
# ---------------------------------------------------------------------------
_WHITELIST_REASONS: Dict[str, str] = {
    "computer_use": (
        "整包转发：tools/computer_use_tool.py:24 "
        "`handler=lambda args, **kw: handle_computer_use(args, **kw)` "
        "—— 全部参数经 args 原样下传。"
    ),
    "discord": (
        "整包转发：tools/discord_tool.py:1086-1090 `_make_handler` 返回 "
        "`lambda args, **kw: handler_fn(**{k: args.get(k, v) for k, v in "
        "_HANDLER_DEFAULTS.items()})`；`_HANDLER_DEFAULTS`(1079-1084) 的键集 == "
        "schema properties（逐键转发，只是以 dict 展开形式而非显式 kwarg）。"
    ),
    "discord_admin": (
        "整包转发：tools/discord_tool.py:1113 "
        "`handler=_make_handler(discord_admin_handler)`，与 discord 同一转发 lambda。"
    ),
    "bfl_flux3_text_to_video": (
        "整包转发：tools/flux3_video_tool.py:654 "
        "`return await _submit(\"text_to_video\", _without_media(args))`；"
        "`_submit_args`(640-643) 用 `dict(args).items()` 构造请求体。"
    ),
    "bfl_flux3_image_to_video": (
        "整包转发：tools/flux3_video_tool.py:659 "
        "`prepared = await _prepare_media(args, kwargs.get(\"task_id\"))` → 662 "
        "`_submit(\"image_to_video\", prepared)`；`_prepare_media`(337) `prepared = "
        "dict(args or {})` 逐键原样传。"
    ),
    "bfl_flux3_keyframes_to_video": (
        "整包转发：tools/flux3_video_tool.py:670 `_prepare_media(args, ...)` → 673 "
        "`_submit(\"keyframes_to_video\", prepared)`。"
    ),
    "bfl_flux3_video_continuation": (
        "整包转发：tools/flux3_video_tool.py:678 `_prepare_media(args, ...)` → 681 "
        "`_submit(\"video_continuation\", prepared)`。"
    ),
    "topo_discover": (
        "非 handler 漏接线，而是 schema 暴露了工具场景无效的参数：tools/topo_tools.py:190-196 "
        "声明 `dry_run`/`force`，但 `_discover_handler` 只返回片段、不落盘（工具路径没有"
        "落盘分支），两参数恒无效。修法是改 schema（属 API 面变更），按任务书「不在本任务"
        "范围」由主 session 定夺，此处不修。"
    ),
}

WHITELIST: FrozenSet[str] = frozenset(_WHITELIST_REASONS)

# 覆盖率下限：本守卫是"注册表驱动"的，扫描面缩水（模块导入失败/依赖缺失）会让
# 缺席集变空 → 守卫静默空过变绿。下限取当前实测值（venv+model_tools 下为 100）
# 的保守值；**发现要调低它 = 先查为什么缩水**，别动这个数字。
_SCAN_FLOOR = 90


# ---------------------------------------------------------------------------
# AST 采集
# ---------------------------------------------------------------------------
class _HandlerTokens(ast.NodeVisitor):
    """收集 handler 源码里的字符串常量 + 关键字实参名，跳过 docstring。"""

    def __init__(self) -> None:
        self.strings: Set[str] = set()
        self.keywords: Set[str] = set()

    @staticmethod
    def _docstring_node(body: list) -> ast.AST | None:
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            return body[0]
        return None

    def _visit_body(self, body: list) -> None:
        doc = self._docstring_node(body)
        if doc is not None and isinstance(doc.value, ast.Constant):
            self.strings.discard(doc.value.value)
        for stmt in body:
            if stmt is doc:
                continue
            self.visit(stmt)

    def visit_Module(self, node: ast.Module) -> None:  # noqa: N802
        self._visit_body(node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_body(node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_body(node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._visit_body(node.body)

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        if isinstance(node.value, str):
            self.strings.add(node.value)

    def visit_keyword(self, node: ast.keyword) -> None:  # noqa: N802
        if node.arg:
            self.keywords.add(node.arg)
        self.generic_visit(node)

    @property
    def tokens(self) -> Set[str]:
        return self.strings | self.keywords


def _parse_handler(handler) -> ast.AST:
    """handler 源码 → AST；standalone 片段解析失败时回退到解析整个定义文件。"""
    try:
        src = inspect.getsource(handler)
    except (OSError, TypeError) as exc:  # pragma: no cover - 目前无此类 handler
        pytest.fail(f"无法读取 handler 源码，无法校验 schema↔handler 接线: {handler!r}: {exc}")
    try:
        return ast.parse(textwrap.dedent(src))
    except SyntaxError:
        path = inspect.getsourcefile(handler)
        if not path:
            pytest.fail(f"handler 源码片段无法解析，且找不到定义文件: {handler!r}")
        with open(path, "r", encoding="utf-8") as fh:
            return ast.parse(fh.read())


def _handler_tokens(handler) -> Set[str]:
    tokens = _HandlerTokens()
    tokens.visit(_parse_handler(handler))
    return tokens.tokens


def _scan() -> "tuple[Dict[str, list], int]":
    """({工具名: 缺席键列表}, 实际被检查的工具数)。

    第二个返回值是**覆盖率**：注册表若因导入失败缩水，缺席集会变空而本守卫
    静默空过变绿（2026-09-28 实测过这种假绿：扫到 0 个工具、命中 0、全绿）。
    所以调用方必须同时断言扫描面够宽（见 test_scan_floor_is_met）。
    """
    import model_tools  # noqa: F401  (触发内建工具发现，填充注册表)
    from tools.registry import registry

    missing: Dict[str, list] = {}
    scanned = 0
    for name in registry.get_all_tool_names():
        schema = registry.get_schema(name)
        if not isinstance(schema, dict):
            continue
        props = (schema.get("parameters") or {}).get("properties")
        if not isinstance(props, dict) or not props:
            continue
        entry = registry.get_entry(name)
        if entry is None or entry.handler is None:
            continue
        scanned += 1
        present = _handler_tokens(entry.handler)
        absent = [key for key in props if key not in present]
        if absent:
            missing[name] = absent
    return missing, scanned


def _scan_missing_properties() -> Dict[str, list]:
    """{工具名: 缺席键列表} —— schema 声明了但 handler 未出现的键。"""
    return _scan()[0]


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------
def test_every_schema_property_is_wired_into_its_handler():
    """注册表全量：schema properties 每个键名都要在 handler 里出现。"""
    missing = _scan_missing_properties()
    unexpected = {n: k for n, k in missing.items() if n not in WHITELIST}
    assert not unexpected, (
        "以下工具的 schema 声明了参数，但 handler 源码里找不到对应键名"
        "（= 工具路径静默失效，模型传了也不生效）。\n"
        "修法：handler 补 `args.get(\"<key>\")` 转发；确属整包转发/门的加进 "
        "_WHITELIST_REASONS 并写清理由。\n"
        + "\n".join(
            f"  - {n}: 缺席 {k}"
            for n, k in sorted(unexpected.items())
        )
    )


def test_whitelist_entries_are_justified():
    """白名单条数 == 理由数，且每条理由非空（不许"暂时不知道"）。"""
    assert len(WHITELIST) == len(_WHITELIST_REASONS), (
        f"白名单条数({len(WHITELIST)}) 与理由数({len(_WHITELIST_REASONS)}) 必须一致"
    )
    for name in WHITELIST:
        reason = _WHITELIST_REASONS[name]
        assert isinstance(reason, str) and reason.strip(), f"白名单 {name} 缺理由"
        assert "暂时不知道" not in reason and "TODO" not in reason.upper(), (
            f"白名单 {name} 的理由必须具体（不许占位/未知）"
        )


def test_whitelist_entries_are_not_stale():
    """白名单里的工具若已不再缺席（说明已接线/已改），必须移除条目。"""
    from tools.registry import registry

    missing = _scan_missing_properties()
    stale = [
        name
        for name in WHITELIST
        if registry.get_entry(name) is not None and name not in missing
    ]
    assert not stale, (
        "以下白名单条目已不再缺席——请从 _WHITELIST_REASONS 移除，保持守卫紧致："
        f"{sorted(stale)}"
    )


def test_scan_floor_is_met():
    """守卫的守卫：扫描面必须够宽，否则上面那条全量断言会**空过变绿**。

    实测过的假绿（2026-09-28）：扫到 0 个工具、命中 0、测试全绿 —— 因为注册表
    在缺依赖的环境下缩水。所以覆盖率本身也是要被断言保护的量。
    """
    _, scanned = _scan()
    assert scanned >= _SCAN_FLOOR, (
        f"只扫到 {scanned} 个工具（下限 {_SCAN_FLOOR}）——注册表可能因模块导入失败"
        "缩水，schema↔handler 全量守卫会空过变绿。先查导入失败（缺依赖 / 导入报错），"
        "不要调低 _SCAN_FLOOR。"
    )

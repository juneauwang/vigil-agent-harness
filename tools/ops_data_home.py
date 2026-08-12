"""Ops 数据目录解析 —— default profile 对 ops profile 数据的只读回退（OPS-DELTA #14）。

迁移路径：老用户把拓扑/runbook 数据铺在 ops profile（<root>/profiles/ops）下，
升级后直接 ``vigil``（default profile，HERMES_HOME=<root>）也应能读到现有
拓扑/runbook，无需手动软链或改 config。本模块提供统一的"数据 home"解析：
active home 有数据 → 用 active home（写路径也落这里，数据不分裂）；active
home 无数据且存在 sibling ops profile → 回退到 ops profile（老数据迁移）。

零侵入：独立文件，只做路径解析，不碰 conversation_loop / 配置注入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def ops_sibling_profile_home(home: Path) -> Optional[Path]:
    """Return ``<root>/profiles/ops`` when it exists and differs from ``home``.

    - ``home`` 是 named profile（<root>/profiles/<name>）→ root = 上级的上级；
    - ``home`` 是 default profile（<root> 本身，兼容旧布局）→ root = home。
    """
    root: Optional[Path] = None
    try:
        if home.parent.name == "profiles":
            root = home.parent.parent
        else:
            root = home
    except Exception:
        return None
    if root is None:
        return None
    candidate = (root / "profiles" / "ops").resolve()
    if candidate == home.resolve():
        return None
    return candidate if candidate.is_dir() else None


def resolve_ops_data_home(home: Path, probe: str) -> Path:
    """Active home 优先；无 ``probe`` 相对路径数据时回退 sibling ops profile。

    ``probe`` 是数据探针的相对路径（如 ``topology.yaml``、``runbooks``）：
    active home 下存在该路径 → 返回 active home（写路径一致，数据不分裂）；
    否则若 sibling ops profile 下存在 → 返回 ops profile（老数据迁移）。
    两者都无 → 返回 active home（调用方照常报"数据缺失"）。
    """
    home = Path(home)
    if (home / probe).exists():
        return home
    ops_home = ops_sibling_profile_home(home)
    if ops_home is not None and (ops_home / probe).exists():
        return ops_home
    return home

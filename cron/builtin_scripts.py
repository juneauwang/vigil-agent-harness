"""内置 cron 脚本白名单（``builtin:<name>``）。

内置脚本随 wheel 分发，不占 ``~/.vigil/scripts/``；创建期只允许白名单名字，
运行期由 ``cron.scheduler._run_builtin_script`` 直接 import 执行，不起子进程、
不做路径 resolve。
"""

from __future__ import annotations

from typing import Optional

# name -> 实现模块（运行时 import）
BUILTIN_SCRIPTS = {
    "digest": "ops.digest",
}


def builtin_name(script: str) -> Optional[str]:
    """``builtin:digest`` → ``"digest"``；非 builtin 前缀/空名返回 None。"""
    raw = str(script or "").strip()
    if not raw.startswith("builtin:"):
        return None
    return raw.split(":", 1)[1].strip() or None

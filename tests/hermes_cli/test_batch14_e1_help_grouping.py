"""批次十四 E1 — 顶层 help 分组验收测试。

覆盖：``vigil -h`` 输出 "Vigil 命令：" 组且 Vigil 运维命令（setup/topo-discover/
vssh/watch/ops-init…）在前醒目；继承命令折叠为一行（带数量与 ``--help-all`` 提示）
不逐条展开说明；``--help-all`` 全量列出继承命令；``vigil gateway -h`` 等继承命令
仍可正常使用（分组只是 help 展示层，不改变命令解析）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(*argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("CI", None)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *argv],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=180,
    )


def test_help_groups_vigil_commands_first():
    """``-h`` 分组：Vigil 命令在前醒目，继承命令折叠提示。"""
    proc = _run_cli("-h")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout

    assert "Vigil 命令：" in out
    assert re.search(r"继承命令（来自 hermes，\d+ 个）", out)
    assert "完整列表与说明见：vigil --help-all" in out
    # 分组顺序：Vigil 组在继承折叠行之前。
    assert out.index("Vigil 命令：") < out.index("继承命令（来自 hermes")
    # Vigil 运维命令在组内醒目列出。
    for name in ("setup", "ops-init", "topo-discover", "vssh", "watch"):
        assert re.search(rf"^\s+{re.escape(name)}\s", out, re.M), name
    # 顶层 usage 用 {command} metavar（分组不影响解析层）。
    assert "{command}" in out


def test_help_folds_inherited_descriptions():
    """``-h`` 不逐条罗列继承命令的说明（gateway 等只折叠为名字一行）。"""
    proc = _run_cli("-h")
    assert proc.returncode == 0
    out = proc.stdout
    # 继承命令的完整说明不展开（gateway 的 description 只在 --help-all 出现）。
    assert "Messaging gateway management" not in out
    # 但命令名仍在折叠行内（功能保留，可调用）。
    assert re.search(r"继承命令（来自 hermes，\d+ 个）：.*\bgateway\b", out)


def test_help_all_lists_inherited_commands():
    """``--help-all`` 同分组全量列出（Vigil 一组 + 继承命令一组逐条说明）。"""
    proc = _run_cli("--help-all")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "Messaging gateway management" in out
    # 批次二十二：--help-all 与 -h 同用分组渲染，继承命令有分组标题且逐条展开。
    assert re.search(r"继承命令（来自 hermes，\d+ 个）：", out)
    for name in ("moa", "gateway", "secrets", "egress", "cron"):
        assert re.search(rf"^\s+{re.escape(name)}\s", out, re.M), name


def test_inherited_subcommand_still_usable():
    """继承命令 ``vigil gateway -h`` 仍可用（分组不改变命令解析）。"""
    proc = _run_cli("gateway", "-h")
    assert proc.returncode == 0, proc.stderr
    assert "Manage the messaging gateway" in proc.stdout

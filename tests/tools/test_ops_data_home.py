"""resolve_ops_data_home —— 数据只挂 home 根，sibling ops profile 回退已删除。

OPS-DELTA #14 修订：Vigil 无老用户，default profile 不再回退
<root>/profiles/ops 的拓扑/runbook 数据；数据只在解析出的 home
（VIGIL_HOME 或 ~/.vigil）下。无数据 → 返回 home，调用方报"数据缺失"。
"""

from __future__ import annotations

from pathlib import Path

from tools.ops_data_home import resolve_ops_data_home


def test_returns_home_when_probe_data_present(tmp_path):
    home = tmp_path / "vigil_root"
    (home / "runbooks").mkdir(parents=True)
    assert resolve_ops_data_home(home, "runbooks") == home


def test_returns_home_when_probe_data_missing(tmp_path):
    home = tmp_path / "vigil_root"
    home.mkdir()
    assert resolve_ops_data_home(home, "runbooks") == home


def test_sibling_ops_profile_no_longer_falls_back(tmp_path):
    """sibling ops profile 有数据也不再回退——直接返回 home，由调用方报缺失。"""
    home = tmp_path / "vigil_root"
    home.mkdir()
    (tmp_path / "profiles" / "ops" / "runbooks").mkdir(parents=True)
    assert resolve_ops_data_home(home, "runbooks") == home


def test_path_coercion(tmp_path):
    assert resolve_ops_data_home(str(tmp_path / "home"), "topology.yaml") == Path(tmp_path / "home")

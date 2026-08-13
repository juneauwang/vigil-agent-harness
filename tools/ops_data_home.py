"""Ops 数据目录解析 —— 数据只挂在解析出的 home 下（VIGIL_HOME 或 ~/.vigil）。

OPS-DELTA #14 的 sibling ops profile 只读回退已删除：老用户把拓扑/runbook
数据铺在 ops profile（<root>/profiles/ops）下也不再自动回退——Vigil 无老安装
用户，数据只在 home/ 下（home/runbooks、home/topology.yaml 等）。老用户
（已有 profiles/ops 数据）不自动迁移，靠手动或后续迁移工具。

本模块提供统一的数据 home 解析：始终返回 active home（写路径也落这里，
数据不分裂）；无数据 → 调用方照常报"数据缺失"。

零侵入：独立文件，只做路径解析，不碰 conversation_loop / 配置注入。
"""

from __future__ import annotations

from pathlib import Path


def resolve_ops_data_home(home: Path, probe: str = "") -> Path:
    """Return the ops data home — always the active ``home`` itself.

    ``probe`` 保留为兼容参数（历史签名），不再参与回退判断：数据只允许挂在
    home 下（home/runbooks、home/topology.yaml）。sibling ops profile
    （<root>/profiles/ops）回退已删除；无数据 → 返回 home，调用方报
    "数据缺失"。
    """
    return Path(home)

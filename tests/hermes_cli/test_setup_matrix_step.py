"""batch83-fix（OPS-DELTA #99）：vigil setup 的权限矩阵生成步骤。

覆盖：
  - setup_matrix 默认模板 2 → matrix.yaml 落盘（数据根）+ base_template 正确；
  - 模板选择（prompt_choice）可换模板 1；
  - 幂等：matrix.yaml 已存在 → 跳过（安全资产不覆盖，不弹选择）；
  - `vigil setup matrix` 段落可被 CLI 解析器接受。
"""

from __future__ import annotations

import argparse

import pytest

import hermes_cli.config as hc
from hermes_cli.setup import setup_matrix
from hermes_cli.subcommands.setup import build_setup_parser


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()


def _matrix_data(home) -> dict:
    import yaml
    return yaml.safe_load((home / "matrix.yaml").read_text(encoding="utf-8"))


def test_setup_matrix_writes_template2_by_default(_home, monkeypatch):
    """默认模板 2（小团队 local/test/dev/prod）→ matrix.yaml 落盘数据根。"""
    monkeypatch.setattr(
        "hermes_cli.setup.prompt_choice",
        lambda _question, choices, default=0, description=None: default,
    )

    setup_matrix({})

    path = _home / "matrix.yaml"
    assert path.is_file()
    data = _matrix_data(_home)
    assert data["base_template"] == "template2"
    assert set(data["matrix"]) == {"local", "test", "dev", "prod"}


def test_setup_matrix_template_choice_is_respected(_home, monkeypatch):
    """prompt_choice 选模板 1 → matrix.yaml 为 template1（单人本地）。"""
    monkeypatch.setattr(
        "hermes_cli.setup.prompt_choice",
        lambda _question, choices, default=0, description=None: 0,
    )

    setup_matrix({})

    data = _matrix_data(_home)
    assert data["base_template"] == "template1"
    assert set(data["matrix"]) == {"local"}


def test_setup_matrix_keeps_existing_matrix(_home, monkeypatch):
    """幂等：matrix.yaml 已存在 → 跳过（安全资产不覆盖，不弹模板选择）。"""
    from tools.matrix_data import init_matrix

    init_matrix("template3", home=_home)
    before = _matrix_data(_home)
    calls = []

    def _prompt(question, choices, default=0, description=None):
        calls.append(question)
        return 0

    monkeypatch.setattr("hermes_cli.setup.prompt_choice", _prompt)

    setup_matrix({})

    after = _matrix_data(_home)
    assert after["base_template"] == "template3"  # 未被覆盖成模板 2
    assert calls == []  # 已存在 → 不弹选择


def test_setup_parser_accepts_matrix_section():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    handler = object()
    build_setup_parser(subparsers, cmd_setup=handler)

    args = parser.parse_args(["setup", "matrix"])

    assert args.section == "matrix"
    assert args.func is handler

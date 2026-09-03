"""batch86 任务 4 —— approvals list/forget/export CLI（OPS-DELTA #102）。

覆盖验收：n `vigil approvals list` 显示种子 + 自进化条目，字段齐全（模板/
状态/计数/沉淀时间/来源任务）；o forget 种子后回到弹审批，重新批准 3 次可再
沉淀；p banned 条目仅能通过显式 forget 解除（不自动复活）；导出含种子清单 +
条目（备份/review）。handler 通过 argparse 解析的 Namespace 直接调用
（hermes_cli.approval_memory_cli.run_*）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import hermes_cli.config as hc
from hermes_cli.approval_memory_cli import (
    run_export,
    run_forget,
    run_list,
)
from tools import approval_memory as am


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    # conftest _hermetic_environment（autouse）已把 VIGIL_HOME 指到
    # tmp_path/hermes_test——不再覆盖，读写都落在同一 home，避免状态错位。
    from hermes_constants import get_hermes_home
    home = Path(get_hermes_home())
    hc._LOAD_CONFIG_CACHE.clear()
    am._read_cache.clear()
    # 自进化条目：lsblk active（3 次）+ mytool banned（被拒）
    am.record_success("lsblk", source_task="sess-1")
    am.record_success("lsblk", source_task="sess-1")
    am.record_success("lsblk", source_task="sess-1")
    am.record_denial("mytool", source_task="sess-2")
    yield home
    hc._LOAD_CONFIG_CACHE.clear()
    am._read_cache.clear()


# --- n：list 字段齐全、种子与自进化分开标注 ---

def test_list_text_shows_all_fields(cli_home, capsys):
    assert run_list(SimpleNamespace(json=False)) == 0
    out = capsys.readouterr().out
    # 文本视图的"来源"列是类别标注（seed/自进化），原始 source_task 在 JSON 视图
    assert "lsblk" in out and "active" in out
    assert "自进化/管理" in out          # lsblk/mytool 条目
    assert "mytool" in out and "banned" in out
    assert "seed（内置只读种子）" in out
    assert "lscpu" in out  # 虚拟种子也在列表
    # 来源任务 id 在数据层/JSON 层齐全（红线 3：来源任务可审计）
    assert am.get_entry("lsblk")["source_task"] == "sess-1"
    assert am.get_entry("mytool")["source_task"] == "sess-2"


def test_list_json_machine_readable(cli_home, capsys):
    assert run_list(SimpleNamespace(json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    by = {r["template"]: r for r in rows}
    assert by["lsblk"]["status"] == "active"
    assert by["lsblk"]["success_count"] == 3
    assert by["lsblk"]["source_task"] == "sess-1"
    assert by["mytool"]["status"] == "banned"
    assert by["mytool"]["never_denied"] is False
    assert by["lscpu"]["source_task"] == "seed"
    assert by["lscpu"]["status"] == "active"


# --- o：forget 种子 → 回到弹审批；重新批准 3 次可再沉淀 ---

def test_forget_seed_then_resediment(cli_home):
    assert am.is_active("lscpu")
    assert run_forget(SimpleNamespace(template="lscpu")) == 0
    assert not am.is_active("lscpu")
    assert am.get_entry("lscpu")["status"] == "inactive"
    # 重新批准 3 次可再沉淀
    for _ in range(3):
        am.record_success("lscpu")
    assert am.is_active("lscpu")
    assert am.get_entry("lscpu")["source_task"] != "seed"  # 现在走自进化


def test_forget_unknown_template_error(cli_home, capsys):
    assert run_forget(SimpleNamespace(template="nope")) != 0
    assert "不在白名单" in capsys.readouterr().err


# --- p：banned 仅显式 forget 可解除 ---

def test_banned_only_explicit_forget(cli_home):
    # 批准 N 次不自动复活（banned 语义在 approval_memory 内）
    for _ in range(4):
        am.record_success("mytool")
    assert am.get_entry("mytool")["status"] == "banned"
    assert not am.is_active("mytool")
    # 显式 forget → 解除（inactive → 重新积累）
    assert run_forget(SimpleNamespace(template="mytool")) == 0
    assert am.get_entry("mytool")["status"] == "inactive"
    assert not am.is_active("mytool")
    for _ in range(3):
        am.record_success("mytool")
    assert am.is_active("mytool")


# --- export：种子清单 + 条目（备份/review） ---

def test_export_yaml_contains_seeds_and_entries(cli_home, capsys):
    assert run_export(SimpleNamespace(json=False)) == 0
    data = yaml.safe_load(capsys.readouterr().out)
    assert data["schema_version"] == 1
    assert "lscpu" in data["seeds"] and "cat" in data["seeds"]
    by = {e["template"]: e for e in data["entries"]}
    assert by["lsblk"]["status"] == "active"
    assert by["mytool"]["status"] == "banned"
    assert "lsblk" in yaml.safe_dump(data)  # stdout YAML 完整


def test_export_json(cli_home, capsys):
    assert run_export(SimpleNamespace(json=True)) == 0
    data = json.loads(capsys.readouterr().out)
    assert "seeds" in data and any(e["template"] == "lsblk" for e in data["entries"])

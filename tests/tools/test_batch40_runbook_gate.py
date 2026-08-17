"""批次四十（§AU）：runbook 格式门控可见——静默失败必须消失。

覆盖：
  - runbooks/ 下含非 .yaml 文件（.md）→ check_runbook_requirements 日志警告
    （点名文件、说明只支持 .yaml）；
  - 纯 .yaml → 无警告；
  - 工具注册行为不变（有 .yaml 数据即可用）；
  - 写入端校验：write_file 落盘 runbooks/ 目录非 .yaml 扩展名 → 拒绝并提示
    正确格式；.yaml 正常放行。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

import hermes_cli.config as hc
from tools.file_tools import write_file_tool
from tools.runbook_tools import (
    _ignored_runbook_files,
    _warned_ignored_runbooks,
    check_runbook_requirements,
    runbook_load,
)

VALID_YAML = (
    "name: demo\ntitle: demo\ntriggers: [demo]\n"
    "steps:\n  - {id: s1, commands: [ls]}\n"
)


@pytest.fixture
def rb_home(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    (home / "runbooks").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    _warned_ignored_runbooks.clear()
    try:
        yield home
    finally:
        hc._LOAD_CONFIG_CACHE.clear()
        _warned_ignored_runbooks.clear()


class TestGateVisibility:
    def test_md_file_triggers_warning(self, rb_home, caplog):
        (rb_home / "runbooks" / "ops-note.md").write_text("# 说明文档", encoding="utf-8")
        (rb_home / "runbooks" / "demo.yaml").write_text(VALID_YAML, encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="tools.runbook_tools"):
            assert check_runbook_requirements() is True
        assert any("非 .yaml" in r.message for r in caplog.records)
        assert "ops-note.md" in " ".join(r.message for r in caplog.records)

    def test_ignored_files_listed(self, rb_home):
        (rb_home / "runbooks" / "a.md").write_text("x", encoding="utf-8")
        (rb_home / "runbooks" / "b.yml").write_text("x", encoding="utf-8")
        (rb_home / "runbooks" / "demo.yaml").write_text(VALID_YAML, encoding="utf-8")
        assert _ignored_runbook_files() == ["a.md", "b.yml"]

    def test_pure_yaml_no_warning(self, rb_home, caplog):
        (rb_home / "runbooks" / "demo.yaml").write_text(VALID_YAML, encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="tools.runbook_tools"):
            assert check_runbook_requirements() is True
        assert not any("非 .yaml" in r.message for r in caplog.records)

    def test_md_only_dir_requirement_stays_false(self, rb_home, caplog):
        (rb_home / "runbooks" / "only.md").write_text("x", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="tools.runbook_tools"):
            assert check_runbook_requirements() is False
        assert any("非 .yaml" in r.message for r in caplog.records)

    def test_tool_registration_unchanged(self, rb_home):
        """有 .yaml 数据 → 门控可用、runbook_load 正常（注册行为不变）。"""
        (rb_home / "runbooks" / "demo.yaml").write_text(VALID_YAML, encoding="utf-8")
        assert check_runbook_requirements() is True
        assert runbook_load("demo") is not None


class TestWriteSideGate:
    def test_non_yaml_write_into_runbooks_rejected(self, rb_home):
        target = os.path.join(os.environ["VIGIL_HOME"], "runbooks", "note.md")
        res = write_file_tool(target, "# 标题\n", task_id="t1")
        assert "拒绝" in res and ".yaml" in res

    def test_yml_write_into_runbooks_rejected(self, rb_home):
        target = os.path.join(os.environ["VIGIL_HOME"], "runbooks", "note.yml")
        res = write_file_tool(target, "name: x\n", task_id="t1")
        assert "拒绝" in res

    def test_yaml_write_into_runbooks_allowed(self, rb_home):
        target = os.path.join(os.environ["VIGIL_HOME"], "runbooks", "new.yaml")
        res = write_file_tool(target, VALID_YAML, task_id="t1")
        assert "拒绝" not in res
        assert (rb_home / "runbooks" / "new.yaml").is_file()

    def test_non_runbooks_directory_untouched(self, tmp_path, monkeypatch):
        home = tmp_path / "other_home"
        (home / "docs").mkdir(parents=True)
        monkeypatch.setenv("VIGIL_HOME", str(home))
        hc._LOAD_CONFIG_CACHE.clear()
        target = os.path.join(str(home), "docs", "note.md")
        res = write_file_tool(target, "# ok\n", task_id="t1")
        assert "拒绝" not in res
        assert (home / "docs" / "note.md").is_file()

"""YAPL P4（批次五十七）：脚本资产库 + run_script 执行校验验收测试。

覆盖（任务书 §P4 阶段 3）：
  - script_asset_create：名称/内容校验、审批通过落盘带预审标记、拒绝不落盘、
    approvals.mode=off 跳过、tirith block 拒绝 / warn 强制人工；
  - script_asset_list：列出资产 + 审批状态；
  - run_script 执行：引用不存在报错引导、无审批标记拒绝、内容哈希漂移拒绝、
    正常资产执行（mock runner 校验 argv）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.script_assets import (
    meta_dir,
    read_meta,
    resolve_script_path,
    script_asset_create,
    script_asset_list,
    scripts_dir,
)
from tools.runbook_exec import execute_runbook, _exec_script_asset


@pytest.fixture
def shome(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    home.mkdir(parents=True)
    (home / "runbooks").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    from tools.matrix_data import template_matrix, write_matrix
    write_matrix(template_matrix("template1"), home)
    yield home


def _approve_cb(**kw):
    from tools.approval import set_hermes_interactive_context, reset_current_session_key
    reset_current_session_key()
    set_hermes_interactive_context("1")
    from tools import terminal_tool
    terminal_tool.set_approval_callback(lambda *a, **k: "once")
    return None


class TestScriptAssetCreate:
    def test_create_approved_written_with_markers(self, shome, monkeypatch):
        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        from tools import terminal_tool
        terminal_tool.set_approval_callback(lambda *a, **k: "once")
        try:
            out = json.loads(script_asset_create(
                name="db-backup", content="#!/bin/bash\necho backup db\n",
                description="db 备份脚本", home=shome))
        finally:
            terminal_tool.set_approval_callback(None)
        assert out["status"] == "created"
        path = scripts_dir(shome) / "db-backup.sh"
        assert path.is_file()
        meta = read_meta(shome, "db-backup")
        assert meta and meta["approved_at"] and meta["approved_by"]
        assert meta["approved_version"]
        # 内容哈希 = 审批版本
        import hashlib
        assert meta["approved_version"] == hashlib.sha256(
            path.read_bytes()).hexdigest()[:16]

    def test_create_denied_not_written(self, shome, monkeypatch):
        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        from tools import terminal_tool
        terminal_tool.set_approval_callback(lambda *a, **k: "deny")
        try:
            out = script_asset_create(name="bad", content="echo hi\n", home=shome)
        finally:
            terminal_tool.set_approval_callback(None)
        assert "审批未通过" in out or "BLOCKED" in out
        assert not (scripts_dir(shome) / "bad.sh").exists()

    def test_mode_off_skips(self, shome, tmp_path, monkeypatch):
        # approvals.mode=off：跳过人工门（资产审批语义同 P3）
        import yaml
        (shome / "config.yaml").write_text(
            yaml.safe_dump({"approvals": {"mode": "off"}}), encoding="utf-8")
        from hermes_cli import config as hc
        hc._LOAD_CONFIG_CACHE.clear()
        try:
            out = json.loads(script_asset_create(name="auto", content="echo ok\n",
                                                 home=shome))
        finally:
            hc._LOAD_CONFIG_CACHE.clear()
        assert out["status"] == "created"
        assert read_meta(shome, "auto")["approved_by"] == "approvals.mode=off"

    def test_name_validation(self, shome):
        out = script_asset_create(name="../evil", content="echo x\n", home=shome)
        assert "非法" in out
        out = script_asset_create(name="ok", content="", home=shome)
        assert "必填" in out

    def test_tirith_block_rejected(self, shome, monkeypatch):
        def fake_scan(content):
            return {"action": "block", "findings": [{"id": "x"}],
                    "summary": "malicious pattern"}
        monkeypatch.setattr("tools.tirith_security.check_command_security", fake_scan)
        out = script_asset_create(name="evil", content="rm -rf /\n", home=shome)
        assert "tirith" in out and "block" in out
        assert not (scripts_dir(shome) / "evil.sh").exists()

    def test_overwrite_requires_flag(self, shome, monkeypatch):
        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        from tools import terminal_tool
        terminal_tool.set_approval_callback(lambda *a, **k: "once")
        try:
            script_asset_create(name="x", content="echo 1\n", home=shome)
            out = script_asset_create(name="x", content="echo 2\n", home=shome)
            assert "已存在" in out
            out2 = json.loads(script_asset_create(
                name="x", content="echo 2\n", overwrite=True, home=shome))
            assert out2["status"] == "updated"
            assert "echo 2" in (scripts_dir(shome) / "x.sh").read_text()
        finally:
            terminal_tool.set_approval_callback(None)

    def test_list(self, shome, monkeypatch):
        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        from tools import terminal_tool
        terminal_tool.set_approval_callback(lambda *a, **k: "once")
        try:
            script_asset_create(name="a", content="echo a\n", home=shome)
        finally:
            terminal_tool.set_approval_callback(None)
        out = json.loads(script_asset_list(home=shome))
        assert out["count"] == 1 and out["scripts"][0]["name"] == "a"
        assert out["scripts"][0]["approved"] is True


class TestRunScriptExecution:
    def _rb_with_script(self, script, args=None):
        return {
            "name": "s", "title": "S", "version": 2, "kind": "incident",
            "env": "local",
            "steps": [{"id": "run", "title": "run", "action": "run_script",
                       "params": {"script": script, "args": args or []}}],
        }

    def test_missing_asset_guides_creation(self, shome):
        res = execute_runbook(self._rb_with_script("ghost"), home=shome)
        assert res["result"] == "failed"
        assert "script_asset_create" in res["steps"][0]["error"]

    def test_unapproved_asset_refused(self, shome):
        base = scripts_dir(shome)
        base.mkdir(parents=True)
        (base / "raw.sh").write_text("echo hi\n")
        res = execute_runbook(self._rb_with_script("raw"), home=shome)
        assert "无审批标记" in res["steps"][0]["error"]

    def test_hash_drift_refused(self, shome, monkeypatch):
        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        from tools import terminal_tool
        terminal_tool.set_approval_callback(lambda *a, **k: "once")
        try:
            script_asset_create(name="drift", content="echo v1\n", home=shome)
            (scripts_dir(shome) / "drift.sh").write_text("echo v2 -- tampered\n")
        finally:
            terminal_tool.set_approval_callback(None)
        res = execute_runbook(self._rb_with_script("drift"), home=shome)
        assert "哈希漂移" in res["steps"][0]["error"]

    def test_approved_asset_executes(self, shome, monkeypatch):
        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        from tools import terminal_tool
        terminal_tool.set_approval_callback(lambda *a, **k: "once")
        try:
            script_asset_create(name="go", content="echo go\n", home=shome)
        finally:
            terminal_tool.set_approval_callback(None)
        captured = {}
        from tools import runbook_exec as rex
        def fake_exec(home, spec, timeout=120):
            captured["spec"] = spec
            return {"exit_code": 0, "stdout": "done", "stderr": ""}
        monkeypatch.setattr(rex, "_exec_script_asset", fake_exec)
        res = execute_runbook(self._rb_with_script("go", ["--x"]), home=shome)
        assert res["result"] == "ok"
        assert captured["spec"]["script_asset"] == "go"
        assert captured["spec"]["args"] == ["--x"]

    def test_resolve_path_helpers(self, shome):
        assert resolve_script_path(shome, "../evil") is None
        base = scripts_dir(shome)
        base.mkdir(parents=True)
        (base / "tool.sh").write_text("x")
        assert resolve_script_path(shome, "tool") == base / "tool.sh"
        assert resolve_script_path(shome, "tool.sh") == base / "tool.sh"

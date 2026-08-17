"""批次四十（§AS C2）：config 写入统一入口后置 schema 校验 + 审计。

覆盖：
  - ``config set`` 写入合法值 → 无警告、无审计记录；
  - ``config set`` 写入非法结构（fallback_model 标量）→ 已保存但警告可见
    + config-audit.log 留痕（source/时间/错误）；
  - 脚本绕过路径（save_config 直接写）→ 同样触发审计（工具层不再是唯一防线）；
  - 工具层拦截保留（write_file 写 config.yaml 仍拒）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as hc
from tools.file_tools import write_file_tool


@pytest.fixture(autouse=True)
def _home_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    cfg_path = os.path.join(str(tmp_path), "config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump({"_config_version": hc.DEFAULT_CONFIG["_config_version"]}, f)
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()


def _audit_path(tmp_path) -> Path:
    return Path(str(tmp_path)) / "logs" / "config-audit.log"


class TestLegalWrite:
    def test_legal_write_no_audit(self, tmp_path, capsys):
        hc.set_config_value("model.default", "gpt-4o")
        assert not _audit_path(tmp_path).exists()
        assert "写入后校验发现错误" not in capsys.readouterr().err


class TestIllegalStructureAudited:
    def test_config_set_illegal_structure_warns_and_audits(self, tmp_path, capsys):
        hc.set_config_value("fallback_model", "gpt-4o")
        err = capsys.readouterr().err
        assert "写入后校验发现错误" in err
        assert "fallback_model" in err
        audit = _audit_path(tmp_path)
        assert audit.is_file()
        body = audit.read_text(encoding="utf-8")
        assert "config set fallback_model" in body
        assert "fallback_model should be a dict" in body

    def test_save_config_bypass_path_audited(self, tmp_path):
        """脚本绕过路径：save_config 直接写非法结构 → 审计留痕。"""
        hc.save_config({
            "_config_version": hc.DEFAULT_CONFIG["_config_version"],
            "custom_providers": {"name": "bad"},
            "model": {"default": "gpt-4o"},
        })
        audit = _audit_path(tmp_path)
        assert audit.is_file()
        body = audit.read_text(encoding="utf-8")
        assert "source=save_config" in body
        assert "custom_providers is a dict" in body

    def test_legal_save_config_no_audit(self, tmp_path):
        hc.save_config({
            "_config_version": hc.DEFAULT_CONFIG["_config_version"],
            "model": {"default": "gpt-4o"},
        })
        assert not _audit_path(tmp_path).exists()


class TestToolLayerBlockRetained:
    def test_write_file_config_still_blocked(self, tmp_path):
        cfg = Path(str(tmp_path)) / "config.yaml"
        res = write_file_tool(str(cfg), "model:\n  default: hacked\n", task_id="t1")
        assert "Refusing to write to Vigil config file" in res

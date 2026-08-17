"""批次四十（§AS B2）：write_file 落盘 redact 可逆化——不再篡改用户文档。

覆盖：
  - 已登记"凭据值"的模型名/字段名写入文档 → 落盘原文保留（byte-identical）；
  - 普通文档内容（snake_case 标识符、中文）放行；
  - 真凭据（API key / password 键值）在 write_file 落盘仍被打码 + 权限 600
    + 原文备份（可逆占位 «redacted:N» 落盘）；
  - restore_redacted_write 还原原文（可逆占位可还原）；
  - 展示面（终端输出）登记值打码行为不回退（persist_write 只影响落盘）。
"""

from __future__ import annotations

import json
import os
import stat

import pytest

import hermes_cli.config as hc
from agent import redact
from tools import file_tools


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)
    redact._reset_registered_credential_values_for_tests()
    yield
    redact._reset_registered_credential_values_for_tests()


class TestDocumentFidelity:
    def test_registered_model_name_round_trips(self, tmp_path):
        """登记过的模型名写入文档 → 落盘原文保留（§AS B2 主修复点）。"""
        redact.register_credential_value("Qwen3_5_397B_A17B_FP8")
        redact.register_credential_value("tool_calls")
        doc = (
            "model=Qwen3_5_397B_A17B_FP8\n"
            "finish_reason=tool_calls\n"
            "snake_case_field_ok = 42\n"
            "这是普通文档内容。\n"
        )
        target = tmp_path / "notes" / "report.md"
        target.parent.mkdir(parents=True)
        res = file_tools.write_file_tool(str(target), doc, task_id="t1")
        assert "拒绝" not in res
        assert target.read_text(encoding="utf-8") == doc

    def test_plain_doc_passes_unchanged(self, tmp_path):
        doc = "# 运维记录\n- 步骤一：检查 harbor 状态\n- model: gpt-4o\n"
        target = tmp_path / "notes.md"
        res = file_tools.write_file_tool(str(target), doc, task_id="t1")
        assert "拒绝" not in res
        assert target.read_text(encoding="utf-8") == doc


class TestCredentialStillMasked:
    def test_api_key_masked_with_backup_and_restore(self, tmp_path):
        doc = "api_key=sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF\n"
        target = tmp_path / "creds.md"
        res = json.loads(file_tools.write_file_tool(str(target), doc, task_id="t1"))
        assert res.get("error") is None
        disk = target.read_text(encoding="utf-8")
        assert "sk-proj" not in disk
        assert "«redacted:" in disk  # 可逆占位
        # 权限收紧 600
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        # 原文备份 sidecar 存在且含占位映射 + 原文
        backup = file_tools._redact_backup_path(str(target))
        assert backup.is_file()
        payload = json.loads(backup.read_text(encoding="utf-8"))
        assert payload["original"] == doc
        assert any(k.startswith("«redacted:") for k in payload["placeholders"])
        # 可逆占位可还原：restore 后与原文 byte-identical
        restored = json.loads(file_tools.restore_redacted_write(str(target)))
        assert restored["status"] == "restored"
        assert target.read_text(encoding="utf-8") == doc

    def test_password_key_value_masked(self, tmp_path):
        doc = "password: hunter2hunter2\n"
        target = tmp_path / "pw.md"
        res = json.loads(file_tools.write_file_tool(str(target), doc, task_id="t1"))
        assert res.get("error") is None
        disk = target.read_text(encoding="utf-8")
        assert "hunter2hunter2" not in disk
        assert "原文已备份" in (res.get("_warning") or "")

    def test_restore_without_backup_errors(self, tmp_path):
        target = tmp_path / "plain.md"
        target.write_text("no secret here", encoding="utf-8")
        res = file_tools.restore_redacted_write(str(target))
        assert "未找到可逆备份" in res


class TestDisplaySurfaceUnchanged:
    def test_registered_value_still_masked_on_display(self):
        """展示面（终端输出）登记值打码不回退——persist_write 只影响落盘。"""
        redact.register_credential_value("wwplove815")
        out = redact.redact_sensitive_text(
            "echo 'wwplove815' > ~/.vigil/askpass", force=True
        )
        assert "wwplove815" not in out
        assert "«redacted-value»" in out

    def test_persist_write_skips_registered_values_only(self):
        """persist_write 跳过登记值 pass，但凭据形态 pass 仍生效。"""
        redact.register_credential_value("wwplove815")
        doc = "note: wwplove815\napi_key: sk-proj-abcdefghijklmnopqrstuvwxyzABCDEFGH\n"
        masked = redact.redact_sensitive_text(
            doc, code_file=True, credential_values=True, persist_write=True
        )
        assert "wwplove815" in masked          # 裸低熵词放行（文档保真）
        assert "sk-proj" not in masked          # 真凭据仍打码

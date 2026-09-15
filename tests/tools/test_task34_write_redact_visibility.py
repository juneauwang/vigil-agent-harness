"""task34 PART D — write_file 落盘后回读校验 + 恢复指引（只做可见性）。

背景：写侧脱敏真改文件内容。主 session 实测：25 处源码引用被替换成
«redacted:N»（产品名/哈希被吞）、heredoc 后半段被吞，打码版被当正常产出
交付，恢复机制不可发现。本套件断言：
  - 疑似误报（占位符落代码围栏/表格、长度对账不平）→ 告警出现在
    工具结果 _warning + 日志，不静默交付；
  - 打码发生时文件末尾有恢复指引行（备份文件名 + 恢复命令）；
  - 正常内容零打扰：无告警、无指引行、逐字节不变、无备份 sidecar。
"""

from __future__ import annotations

import json
import logging
import stat

import pytest

import hermes_cli.config as hc
from agent import redact
from tools import file_tools

# 合成值：拼接构造，不在本文件留下真实凭据形态的完整字面量
_V = "Aa1" + "-x9" * 6 + "Zz"
_V2 = "Tk9" + "7qz" * 5 + "Xw"


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)
    redact._reset_registered_credential_values_for_tests()
    yield
    redact._reset_registered_credential_values_for_tests()


class TestSuspicionDetector:
    """_redact_false_positive_suspicion 纯函数判定。"""

    def test_placeholder_in_code_fence_flagged(self):
        original = "```bash\nexport MINIO_ROOT_PASSWORD=" + _V + "\n```\n"
        persisted = "```bash\nexport MINIO_ROOT_PASSWORD=«redacted:1»\n```\n"
        reasons = file_tools._redact_false_positive_suspicion(
            original, persisted, {1: _V}
        )
        assert any("代码围栏" in r for r in reasons)

    def test_placeholder_in_table_row_flagged(self):
        original = "| k | v |\n| --- | --- |\n| cfg | {\"api_key\": \"" + _V + "\"} |\n"
        persisted = "| k | v |\n| --- | --- |\n| cfg | {\"api_key\": \"«redacted:1»\"} |\n"
        reasons = file_tools._redact_false_positive_suspicion(
            original, persisted, {1: _V}
        )
        assert any("表格" in r for r in reasons)

    def test_length_mismatch_flagged(self):
        """吞掉一段（非占位替换）：长度对账不平——只 grep 占位符会漏的形态。"""
        original = "head\n" + _V + "\ntail\n"
        # 占位映射为空（该替换不是可逆捕获通道做的），长度却变短
        persisted = "head\n\ntail\n"
        reasons = file_tools._redact_false_positive_suspicion(original, persisted, {})
        assert any("长度" in r for r in reasons)

    def test_reconciled_masking_not_flagged(self):
        """正常凭据打码（映射能解释长度、占位符在正文不在围栏/表格）→ 无疑点。"""
        original = "api_key=" + _V + "\nnote: 部署记录\n"
        persisted = "api_key=«redacted:1»\nnote: 部署记录\n"
        assert file_tools._redact_false_positive_suspicion(
            original, persisted, {1: _V}
        ) == []


class TestFalsePositiveVisibility:
    def test_fenced_credential_warns_and_hints(self, tmp_path, caplog):
        """误伤现场：代码围栏里的 env 赋值被打码 → 告警 + 末尾恢复指引 + 可恢复。"""
        doc = (
            "# 部署核对\n"
            "```bash\n"
            "export MINIO_ROOT_" + "PASSWORD=" + _V + "\n"
            "```\n"
            "以上来自 compose 渲染结果。\n"
        )
        target = tmp_path / "deploy.md"
        with caplog.at_level(logging.WARNING, logger="tools.file_tools"):
            res = json.loads(
                file_tools.write_file_tool(str(target), doc, task_id="t1")
            )
        assert res.get("error") is None
        # 工具结果里可见的疑似误报告警
        assert "疑似误报" in (res.get("_warning") or "")
        # 日志里可见
        assert "疑似误报" in caplog.text
        # 末尾有恢复指引行（备份文件名 + 恢复命令）
        disk = target.read_text(encoding="utf-8")
        backup = file_tools._redact_backup_path(str(target))
        assert disk.endswith(
            f"或运行：python3 -c \"from tools.file_tools import "
            f"restore_redacted_write; print(restore_redacted_write("
            f"r'{target.resolve()}'))\" -->\n"
        )
        assert backup.name in disk
        assert "«redacted:»" not in disk  # 指引行是说明文字，不是空占位
        # 权限收紧 + 备份在
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert backup.is_file()
        # 还原后与原文逐字节一致（指引行不属于原文）
        restored = json.loads(file_tools.restore_redacted_write(str(target)))
        assert restored["status"] == "restored"
        assert target.read_text(encoding="utf-8") == doc

    def test_nonreversible_mask_length_warning(self, tmp_path):
        """非可逆通道（私钥块整段掩码）→ 长度对账告警 + 指引行照常落盘。"""
        key_body = "MIIEv" + "QIBADANBg" * 8
        doc = (
            "服务器密钥：\n"
            "-----BEGIN PRIVATE KEY-----\n"
            + key_body + "\n"
            "-----END PRIVATE KEY-----\n"
        )
        target = tmp_path / "key.md"
        res = json.loads(
            file_tools.write_file_tool(str(target), doc, task_id="t1")
        )
        assert res.get("error") is None
        assert "疑似误报" in (res.get("_warning") or "")
        assert "长度" in (res.get("_warning") or "")
        disk = target.read_text(encoding="utf-8")
        assert key_body not in disk
        assert "[REDACTED PRIVATE KEY]" in disk
        assert "恢复指引" in disk or "原文完整备份" in disk

    def test_normal_content_zero_touch(self, tmp_path):
        """正常内容：无告警、无指引行、逐字节不变、无备份 sidecar。"""
        doc = (
            "# 运维记录\n"
            "- 步骤一：检查 harbor 状态\n"
            "- model: gpt-4o\n"
            "- ensure_ascii=False 与 16 位哈希 0123456789abcdef0123456789abcdef\n"
        )
        target = tmp_path / "notes.md"
        res = json.loads(file_tools.write_file_tool(str(target), doc, task_id="t1"))
        assert res.get("error") is None
        assert res.get("_warning") is None
        raw = target.read_bytes()
        assert raw == doc.encode("utf-8")  # 逐字节不变
        assert not file_tools._redact_backup_path(str(target)).is_file()

"""OPS-DELTA 批次三十二 — 凭据值全局登记表（agent/redact.py）。

覆盖：登记来源（register_credential_value）资格过滤、登记后任意输出通道精确
打码（低熵裸密码形态：``echo 'wwplove815'``）、file_read 非复用 sentinel、
登记表持久化 600 权限 + 重载、词边界保护、登记表文件自护（read_file 读不出
明文）。
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from agent import redact


@pytest.fixture(autouse=True)
def _registry_isolation(tmp_path, monkeypatch):
    """登记表隔离：临时 VIGIL_HOME + 清空进程内登记表 + 打码强制开启。"""
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    import hermes_cli.config as hc
    hc._LOAD_CONFIG_CACHE.clear()
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)
    redact._reset_registered_credential_values_for_tests()
    yield
    redact._reset_registered_credential_values_for_tests()


class TestRegisterEligibility:
    def test_low_entropy_value_registers(self):
        """wwplove815 类低熵密码（无键名形态、不足 16 字符）可登记。"""
        assert redact.register_credential_value("wwplove815") is True

    def test_short_values_skipped(self):
        assert redact.register_credential_value("root") is False
        assert redact.register_credential_value("") is False
        assert redact.register_credential_value(None) is False

    def test_pure_digit_and_repeated_char_skipped(self):
        assert redact.register_credential_value("12345678") is False
        assert redact.register_credential_value("aaaaaa") is False

    def test_value_never_logged(self, caplog):
        redact.register_credential_value("s3cret-xyz-123")
        assert "s3cret-xyz-123" not in caplog.text


class TestRegisteredValueMasking:
    def test_bare_echo_value_masked(self):
        redact.register_credential_value("wwplove815")
        out = redact.redact_sensitive_text(
            "echo 'wwplove815' > ~/.vigil/askpass", force=True
        )
        assert "wwplove815" not in out
        assert "«redacted-value»" in out

    def test_all_quote_variants_masked(self):
        redact.register_credential_value("wwplove815")
        for text in (
            "echo 'wwplove815'",
            'echo "wwplove815"',
            "echo wwplove815",
            "pw=wwplove815",
            "the password is wwplove815, keep it safe",
        ):
            out = redact.redact_sensitive_text(text, force=True)
            assert "wwplove815" not in out, text

    def test_file_read_uses_nonreusable_sentinel(self):
        redact.register_credential_value("wwplove815")
        out = redact.redact_sensitive_text(
            '{"values": ["wwplove815"]}', file_read=True
        )
        assert "wwplove815" not in out
        assert "«redacted-secret»" in out

    def test_plain_text_identity(self):
        redact.register_credential_value("wwplove815")
        text = "hello world, nothing secret here"
        assert redact.redact_sensitive_text(text) == text

    def test_boundary_protection_does_not_corrupt_longer_token(self):
        redact.register_credential_value("wwplove815")
        out = redact.redact_sensitive_text(
            "prefix wwplove815X suffix wwplove815 end", force=True
        )
        assert "wwplove815X" in out  # 长 token 中的前缀不被腐蚀
        assert "wwplove815 end" not in out

    def test_credential_values_json_self_protection(self):
        """登记表文件自身：read_file 读它时值被精确打码，读不出明文。"""
        redact.register_credential_value("wwplove815")
        path = os.path.join(os.environ["VIGIL_HOME"], "credential_values.json")
        raw = open(path, encoding="utf-8").read()
        assert "wwplove815" in raw  # 磁盘明文（600 权限）
        out = redact.redact_sensitive_text(raw, file_read=True)
        assert "wwplove815" not in out


class TestRegistryPersistence:
    def test_persisted_with_0600(self):
        redact.register_credential_value("wwplove815")
        path = os.path.join(os.environ["VIGIL_HOME"], "credential_values.json")
        assert os.path.isfile(path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, oct(stat.S_IMODE(os.stat(path).st_mode))
        data = json.loads(open(path, encoding="utf-8").read())
        assert "wwplove815" in data["values"]

    def test_reload_from_disk(self):
        redact.register_credential_value("wwplove815")
        redact._reset_registered_credential_values_for_tests()
        assert "wwplove815" in redact.registered_credential_values()

    def test_registered_credential_values_snapshot(self):
        redact.register_credential_value("zzz-secret-9")
        redact.register_credential_value("aaa-secret-1")
        vals = redact.registered_credential_values()
        assert vals == ("aaa-secret-1", "zzz-secret-9")

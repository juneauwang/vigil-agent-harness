"""OPS-DELTA 批次三十二 — clarify 敏感答复凭据值登记。

问题文本含敏感关键词（密码/password/密钥/secret/凭据/sudo…）时，答复值登记
进全局 redact 登记表（任何输出通道精确打码）；非敏感答复（是/否/常见答复词）
不登记、不误伤。
"""

from __future__ import annotations

import json

import pytest

from agent import redact
from tools.clarify_tool import clarify_tool


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    import hermes_cli.config as hc
    hc._LOAD_CONFIG_CACHE.clear()
    redact._reset_registered_credential_values_for_tests()
    yield
    redact._reset_registered_credential_values_for_tests()


def _cb(answer):
    return lambda question, choices=None, multi_select=False: answer


class TestSensitiveAnswerRegistration:
    @pytest.mark.parametrize(
        "question,answer",
        [
            ("请输入 sudo 密码", "wwplove815"),
            ("Please enter the password", "Wwplove815"),
            ("请提供 root 密钥", "root-secret-9"),
            ("What is the credential for the vault?", "vault-pass-77"),
        ],
    )
    def test_sensitive_answer_registered(self, question, answer):
        result = json.loads(clarify_tool(question, callback=_cb(answer)))
        # 返回给 agent 的 JSON 保持明文（agent 仍需该值去 store vault）
        assert result["user_response"] == answer
        assert answer in redact.registered_credential_values()
        # 登记后任何输出通道打码
        out = redact.redact_sensitive_text(f"echo '{answer}'", force=True)
        assert answer not in out

    def test_multi_select_sensitive_answers_registered(self):
        answers = ["abc12345", "xyz99999"]
        result = json.loads(clarify_tool(
            "请提供两个密钥",
            choices=["a", "b"],
            multi_select=True,
            callback=lambda q, c=None, multi_select=False: answers,
        ))
        for answer in answers:
            assert answer in redact.registered_credential_values()

    def test_non_sensitive_answer_not_registered(self):
        for question, answer in [
            ("What color?", "blue"),
            ("How are you?", "fine"),
            ("Proceed?", "yes"),
            ("Which token type?", "bearer"),      # 常见答复词，长度 6
            ("Pick a number", "12345"),           # 纯数字/过短
        ]:
            result = json.loads(clarify_tool(question, callback=_cb(answer)))
            assert result["user_response"] == answer
            assert answer not in redact.registered_credential_values(), (question, answer)

    def test_plaintext_still_masked_in_output_channel(self):
        """登记值即使只出现在普通输出文本里也被打码（值层面，非键名形态）。"""
        result = json.loads(clarify_tool("请输入 sudo 密码", callback=_cb("wwplove815")))
        out = redact.redact_sensitive_text(
            json.dumps(result, ensure_ascii=False), force=True
        )
        assert "wwplove815" not in out

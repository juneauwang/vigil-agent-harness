"""OPS-DELTA #35 — redact 值形态检测兜底（JSON 组合字段名回显防护）。

精确键名（_JSON_KEY_NAMES）无法穷举组合字段名（ssh_key_0811/sudo_0811
斜杠/时点形态）——只要 JSON 值本身像凭据（长度 ≥16 + 混合字符类 + 熵 ≥3.2，
复用 _looks_like_inline_secret）就打码，不管字段名叫什么。防误伤：普通长文本
（字符类单一）/ URL / 路径（含 / \\ : .）不打码；精确键名 pass 原逻辑不回归。
三个渲染出口（工具输出 / reasoning / 命令文本）都走 redact_sensitive_text
主链，新兜底自动覆盖。
"""

from __future__ import annotations

import pytest

from agent.redact import redact_sensitive_text, redact_terminal_output

_HIGH_ENTROPY = "s3cr3tK3yV4lueX9!mPqR2026zz"  # 24 chars, lower+upper+digit+symbol
_LONG_PROSE = (
    "this is a very long plain english description that should not be "
    "redacted at all because it is just ordinary prose text"
)
# 首字母大写的正常英文句子——lower+upper 天然混合字符类，曾被误判为凭据
# （OPS-DELTA #35 验收修复：含空格 token 直接排除）。
_SENTENCE_CAPS = (
    "This is a perfectly normal long english description that should not be "
    "redacted at all because it is just ordinary prose text"
)


class TestValueShapeFallback:
    @pytest.mark.parametrize(
        "text",
        [
            '{"ssh_key_0811": "' + _HIGH_ENTROPY + '"}',   # #35 核心用例
            '{"sudo_0811": "' + _HIGH_ENTROPY + '"}',
            '{"ssh_key_2026-08-11": "' + _HIGH_ENTROPY + '"}',
            '{"custom_field_zz9": "' + _HIGH_ENTROPY + '"}',
        ],
    )
    def test_high_entropy_value_masked_regardless_of_key(self, text):
        result = redact_sensitive_text(text)
        assert _HIGH_ENTROPY not in result
        # 值被打码（保留头尾，非整串删除）
        assert "..." in result

    def test_plain_long_text_not_masked(self):
        text = '{"description": "' + _LONG_PROSE + '"}'
        assert redact_sensitive_text(text) == text

    def test_caps_sentence_not_masked(self):
        # 首字母大写的句子不打码（OPS-DELTA #35 验收回归用例）
        text = '{"description": "' + _SENTENCE_CAPS + '"}'
        result = redact_sensitive_text(text)
        assert "..." not in result
        assert _SENTENCE_CAPS in result

    def test_url_not_masked(self):
        text = '{"url": "https://example.com/some/long/path/with/many/segments"}'
        assert redact_sensitive_text(text) == text

    def test_path_not_masked(self):
        text = '{"path": "/var/lib/something/very/long/with/many/segments/here"}'
        assert redact_sensitive_text(text) == text

    def test_base64_image_data_uri_not_masked(self):
        b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        text = '{"image": "data:image/png;base64,' + b64 + '"}'
        assert redact_sensitive_text(text) == text

    def test_exact_key_short_value_still_masked(self):
        # 精确键名 pass 原逻辑不回归（<16 字符也打码）
        result = redact_sensitive_text('{"password": "short"}')
        assert '"password": "***"' in result


class TestRenderOutlets:
    def test_tool_output_outlet(self):
        # 工具输出（terminal stdout）出口：code_file=True + credential_values=True
        out = '{"ssh_key_0811": "' + _HIGH_ENTROPY + '"}'
        result = redact_terminal_output(out, command="cat config.yaml")
        assert _HIGH_ENTROPY not in result

    def test_reasoning_outlet(self):
        # reasoning 块出口：chat_completion_helpers 对推理内容调 redact_sensitive_text
        reasoning = '推理过程引用 vault 字段 {"sudo_0811": "' + _HIGH_ENTROPY + '"}'
        result = redact_sensitive_text(reasoning)
        assert _HIGH_ENTROPY not in result

    def test_command_text_outlet(self):
        # 命令文本出口：agent 拼的命令串含 JSON 字面量
        cmd = "curl -d '{\"ssh_key_0811\": \"" + _HIGH_ENTROPY + "\"}' https://api.example.com"
        result = redact_sensitive_text(cmd)
        assert _HIGH_ENTROPY not in result

"""批次十六 §V — redact 三盲区验收测试（OPS-DELTA 批次十六 任务 1）。

覆盖 2026-08-14 凌晨 dogfooding 日志 §V 的真实泄漏形态 + 误伤反向断言：
  1. 盲区 1：``-H "X-Vault-Token: s.xxx"`` —— OpenBao 认证 header 名单缺失
  2. 盲区 2：``s.pL7…`` 形态 token —— OpenBao ``s.`` 前缀缺失
  3. 盲区 3：``sudo -S <<< '密码'`` / heredoc / ``SUDO_PASS=$(…)`` stdin 注入形态

断言原则（redact 是兜底防线，只做确定性匹配）：
  - 值部分被 **** 替换、命令骨架（header 名 / sudo 结构 / URL）保留；
  - 无 sudo -S 上下文的普通 herestring 不误伤（``grep <<< "hello"`` 等）。
"""

from __future__ import annotations

import pytest

from agent.redact import redact_sensitive_text


class TestVaultHeaderBlindSpot:
    """盲区 1：_SECRET_HEADER_NAMES 补 X-Vault-Token / X-Vault-Request / X-Vault-Namespace。"""

    def test_vault_token_header_long_value_masked(self):
        cmd = (
            'curl -s -H "X-Vault-Token: s.pL7AbCdEfGhIjKlMnOpQrStU" '
            "http://127.0.0.1:8200/v1/secret/data/CSNDC/maas"
        )
        out = redact_sensitive_text(cmd)
        assert "s.pL7AbCdEfGhIjKlMnOpQrStU" not in out
        # 骨架保留：header 名 + URL + curl 结构都在，收尾引号不丢（语法不破）
        assert "X-Vault-Token:" in out
        assert "curl" in out and "127.0.0.1:8200" in out
        assert out.count('"') == 2

    def test_vault_token_header_short_value_masked(self):
        """header 名命中即打码——值形态不重要（s.xxxx 短值也不放过）。"""
        cmd = (
            'curl -s -H "X-Vault-Token: s.xxxx" '
            "http://127.0.0.1:8200/v1/secret/data/CSNDC/maas"
        )
        out = redact_sensitive_text(cmd)
        assert "s.xxxx" not in out
        assert "X-Vault-Token:" in out

    def test_vault_request_and_namespace_headers_masked(self):
        cmd = (
            'curl -s -H "X-Vault-Request: true" '
            '-H "X-Vault-Token: s.abcDEF1234567890123456789" '
            '-H "X-Vault-Namespace: admin/root" '
            "http://127.0.0.1:8200/v1/secret/data/x"
        )
        out = redact_sensitive_text(cmd)
        assert "s.abcDEF1234567890123456789" not in out
        assert "X-Vault-Request" in out and "X-Vault-Namespace" in out


class TestOpenBaoPrefixBlindSpot:
    """盲区 2：_PREFIX_PATTERNS 补 ``s.`` + ≥20 字符（Vault token 标准形态）。"""

    def test_vault_s_prefix_token_masked_standalone(self):
        """无 header 上下文，裸 ``s.`` token 也命中前缀名单。"""
        token = "s.pL7AbCdEfGhIjKlMnOpQrStU"
        out = redact_sensitive_text(f"vault token {token} in output")
        assert token not in out

    def test_s_prefix_requires_20_chars_no_false_positive(self):
        """长度 ≥20 防误伤普通句点开头文本（改点 2 明确要求）。"""
        benign = "see the s. short dot in prose."
        assert redact_sensitive_text(benign) == benign

    def test_s_prefix_short_token_not_masked(self):
        """短 ``s.abc`` 不命中前缀（留给 header 名等上下文通道）。"""
        assert redact_sensitive_text("token s.abc") == "token s.abc"


class TestSudoStdinInjectionBlindSpot:
    """盲区 3：sudo -S 上下文 stdin 密码注入（herestring / heredoc / 赋值形态）。"""

    def test_sudo_S_herestring_password_masked(self):
        cmd = (
            'ssh -i /root/wangwp10.pem wangwp10@10.123.66.237 '
            '"sudo -S -p \'\' smem -s swap -r <<< \'Sup3rSecr3t!\'"'
        )
        out = redact_sensitive_text(cmd)
        assert "Sup3rSecr3t!" not in out
        # 骨架保留：ssh 目标、sudo -S -p ''、herestring 结构都在
        assert "sudo -S -p '' smem -s swap -r <<<" in out
        assert "ssh -i /root/wangwp10.pem wangwp10@10.123.66.237" in out

    def test_sudo_S_herestring_short_password_masked(self):
        """短密码（<18 字符）也整段打码——sudo -S 出现即密码，不做值形态判断。"""
        cmd = "sudo -S <<< 'pw'"
        out = redact_sensitive_text(cmd)
        assert "'pw'" not in out
        assert "sudo -S <<<" in out

    def test_sudo_S_heredoc_quoted_terminator_masked(self):
        cmd = "sudo -S <<'EOF'\nSup3rSecr3t!\nEOF\n"
        out = redact_sensitive_text(cmd)
        assert "Sup3rSecr3t!" not in out
        assert "sudo -S <<'EOF'" in out and "EOF" in out

    def test_sudo_S_heredoc_unquoted_terminator_masked(self):
        cmd = "sudo -S <<EOF\nSup3rSecr3t!\nEOF"
        out = redact_sensitive_text(cmd)
        assert "Sup3rSecr3t!" not in out
        assert "EOF" in out

    def test_sudo_pass_command_substitution_masked(self):
        """``SUDO_PASS=$(curl vault | jq …)`` 赋值形态——整段 ``$(…)`` 打码。"""
        cmd = (
            "SUDO_PASS=$(curl -s -H \"X-Vault-Token: s.pL7AbCdEfGhIjKlMnOpQrStU\" "
            "http://127.0.0.1:8200/v1/secret/data/CSNDC/maas | "
            "jq -r '.data.data[\"ssh_key_0811/sudo_0811\"]')"
        )
        out = redact_sensitive_text(cmd)
        assert out == "SUDO_PASS=****"
        assert "curl" not in out and "jq" not in out and "ssh_key_0811" not in out

    def test_other_credential_assignments_masked(self):
        """同族赋值名（_PASSWORD/_TOKEN/_KEY/_SECRET 后缀）都整段打码。"""
        for cmd in (
            "MY_PASSWORD=$(cat /vault/db-pass)",
            "API_TOKEN=$(curl -s http://vault/v1/token | jq -r .token)",
            "SSH_KEY=$(curl -s http://vault/v1/key | jq -r .key)",
            "client_secret=$(vault read secret/db -format=json | jq -r .data.password)",
        ):
            out = redact_sensitive_text(cmd)
            assert "$(" not in out, f"{cmd!r} -> {out!r}"
            assert out.endswith("=****"), f"{cmd!r} -> {out!r}"

    def test_quoted_command_substitution_assignment_masked(self):
        """带引号赋值（``SUDO_PASS="$(curl …)"``）也整段打码，引号保留。"""
        cmd = (
            "SUDO_PASS=\"$(curl -s http://vault/v1/secret | "
            "jq -r '.data.data[\"sudo_0811\"]')\""
        )
        out = redact_sensitive_text(cmd)
        assert out == 'SUDO_PASS="****"'
        assert "curl" not in out and "sudo_0811" not in out

    def test_bare_herestring_no_sudo_context_not_masked(self):
        """误伤反向：无 sudo -S 上下文的普通 herestring 不动。"""
        for cmd in (
            'echo "hello" <<< "world"',
            'grep -i swap <<< "swap usage"',
            "ssh host \"cat <<< 'config'\"",
            "awk '{print $1}' <<< 'a b c'",
        ):
            assert redact_sensitive_text(cmd) == cmd, cmd

    def test_bare_assignment_no_credential_suffix_not_masked(self):
        """普通赋值（不匹配 *_PASS/_PASSWORD/_TOKEN/_KEY/_SECRET 后缀）不动。"""
        for cmd in (
            "COUNT=$(wc -l < /tmp/x)",
            "OUT_DIR=$(pwd)",
            "PROMPT=$(echo hi)",
            "GIT_SHA=$(git rev-parse HEAD)",
        ):
            assert redact_sensitive_text(cmd) == cmd, cmd

    def test_sudo_S_double_quote_prompt_masked(self):
        """``sudo -S -p ""`` 双引号形态同样命中。"""
        cmd = 'sudo -S -p "" smem <<< "Sup3rSecr3t!"'
        out = redact_sensitive_text(cmd)
        assert "Sup3rSecr3t!" not in out
        assert 'sudo -S -p "" smem <<<' in out

    def test_nested_command_substitution_assignment_masked(self):
        """嵌套命令替换赋值（§AD 真实形态）整段打码。

        ``SUDO_PASS=$(curl … "X-Vault-Token: $(cat /root/.bao_token)" …)``——
        header 值以 ``$(`` 开头时 _SECRET_HEADER_RE 必须跳过（否则破坏括号结构，
        命令文本 pass 在内层 ) 截断），由 _CMD_CRED_ASSIGN_RE 整段打码。
        回归：2026-08-14 验收抓到的 Codex 交付漏洞（简化形态过、嵌套形态漏）。
        """
        cmd = (
            'SUDO_PASS=$(curl -s -H "X-Vault-Token: $(cat /root/.bao_token)" '
            'http://127.0.0.1:8200/v1/secret/data/CSNDC/maas | '
            "jq -r '.data.data[\"ssh_key_0811/sudo_0811\"]')"
        )
        out = redact_sensitive_text(cmd)
        assert out == "SUDO_PASS=****"
        assert "bao_token" not in out and "sudo_0811" not in out and "curl" not in out

    def test_nested_substitution_plain_header_still_masked(self):
        """普通 header 值（非命令替换）仍打码——跳过逻辑不扩大为漏报。"""
        cmd = 'curl -s -H "X-Vault-Token: s.pL7AbCdEfGhIjKlMnOpQrStU" http://127.0.0.1:8200/v1'
        out = redact_sensitive_text(cmd)
        assert "s.pL7" not in out and "***" in out

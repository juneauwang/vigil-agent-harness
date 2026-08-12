"""OPS-DELTA #21 输出侧 — 命令文本内联凭据打码。

验收点：高熵串命令文本过 redact 后打码（--from-literal / -u user:pass /
--password / -p / 高熵裸 token 兜底）；非凭据形态（git sha、pod 名、
-p 端口、长 URL、纯 hex）不被误伤。
"""

from __future__ import annotations

import pytest

from agent.redact import redact_sensitive_text


@pytest.mark.parametrize(
    "cmd",
    [
        "kubectl patch cm grafana --from-literal=GF_SECURITY_ADMIN_PASSWORD=N3wP@ssw0rd2026X!Secret",
        "kubectl create secret generic db --from-literal=db-password=Sup3rS3cretP@ssw0rd123",
        "curl -u admin:Sup3rS3cretP@ssw0rd12345 https://example.com/api",
        "curl --password MyP@ssw0rd12345678 https://x",
        "sshpass -p MyP@ssw0rd12345678 ssh user@host",
        "curl -d 'xK9!mP4$qR7@zT2vB8Nm' https://api.example.com",
        "ansible-playbook site.yml -e vault_password=Sup3rS3cret@2026X",
        "docker login registry.example.com -u admin -p G1tH3re!S3cr3tP@ss99",
    ],
)
def test_command_inline_credentials_masked(cmd):
    redacted = redact_sensitive_text(cmd)
    assert redacted != cmd, cmd
    # 明文不可见（凭据值 token：含符号且不是 user@host 形态）
    for tok in cmd.split():
        if "@" in tok and ":" not in tok and not any(ch in tok for ch in "!$^"):
            continue
        if any(ch in tok for ch in "!@#$%^&*"):
            assert tok not in redacted, (cmd, redacted)


@pytest.mark.parametrize(
    "cmd",
    [
        "kubectl get pod abcdef1234567890abcdef -n prod",
        "git checkout 0123456789abcdef0123456789abcdef01234567",
        "kubectl logs pod-abc123def456-xyz789 -c main --tail=100",
        "curl -p 8080 -u admin https://example.com",
        "docker compose up -d",
        "ls -la /var/log",
        "kubectl get pods -A",
        "systemctl status nginx",
    ],
)
def test_non_secret_command_shapes_survive(cmd):
    assert redact_sensitive_text(cmd) == cmd, cmd


def test_high_entropy_token_in_prose_not_masked():
    """高熵串在普通文本（无命令上下文）里不打码——避免误伤日志/UUID 散文。"""
    text = "the token is abcDEF123!@#xyz and nothing else matters here"
    assert redact_sensitive_text(text) == text

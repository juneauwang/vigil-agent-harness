"""批次十五 — bedrock 探测链绝不触发 lazy-deps 自动安装。

Regression for the #v0.1.12 startup hang: a fresh install without boto3 ran
``from agent.bedrock_adapter import has_aws_credentials`` during provider
auto-detection, and the adapter's module-level ``ensure("provider.bedrock")``
silently pip-installed boto3 (blocking on slow networks before first-run
setup was offered).

Covers:
- resolve_provider() bedrock branch: boto3 missing → skip silently, NO
  lazy_deps.ensure, NO bedrock_adapter import / has_aws_credentials call
- boto3 present → real detection path unchanged (bedrock returned / falls
  through when no AWS creds)
- has_aws_credentials(): boto3 missing → False without botocore session
- importing agent.bedrock_adapter has no import-time side effects
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.auth import AuthError, resolve_provider

REPO_ROOT = Path(__file__).resolve().parents[2]


def _find_spec_gated(monkeypatch, boto3_result):
    """find_spec: return ``boto3_result`` for boto3, real result otherwise."""
    real = importlib.util.find_spec
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: boto3_result if name == "boto3" else real(name),
    )


def _neutralize_auto_resolve(monkeypatch):
    """Pin resolve_provider("auto") to the bedrock tier (no config/env/pool/oauth)."""
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr("hermes_cli.auth.has_usable_secret", lambda _v: False)
    monkeypatch.setattr("hermes_cli.auth._load_auth_store", lambda *a, **k: {})
    monkeypatch.setattr(
        "agent.credential_pool.load_pool",
        lambda _provider: SimpleNamespace(has_credentials=lambda: False),
    )


# ---------------------------------------------------------------------------
# resolve_provider bedrock branch
# ---------------------------------------------------------------------------

def test_resolve_provider_skips_bedrock_without_boto3(monkeypatch):
    """boto3 未装：不 import adapter、不调 ensure、探测正常收尾（no_provider）。"""
    _find_spec_gated(monkeypatch, None)
    _neutralize_auto_resolve(monkeypatch)

    def _ensure_boom(*_a, **_k):
        raise AssertionError("lazy_deps.ensure must never run from a probe path")

    def _creds_boom(*_a, **_k):
        raise AssertionError("has_aws_credentials must not be called when boto3 is missing")

    monkeypatch.setattr("tools.lazy_deps.ensure", _ensure_boom)
    monkeypatch.setattr("agent.bedrock_adapter.has_aws_credentials", _creds_boom)

    with pytest.raises(AuthError) as exc:
        resolve_provider("auto")
    assert exc.value.code == "no_provider_configured"


def test_resolve_provider_detects_bedrock_when_boto3_present(monkeypatch):
    """boto3 已装 + AWS 凭据存在 → 真实检测路径不变（返回 bedrock）。"""
    _find_spec_gated(monkeypatch, object())
    _neutralize_auto_resolve(monkeypatch)
    monkeypatch.setattr("agent.bedrock_adapter.has_aws_credentials", lambda: True)
    assert resolve_provider("auto") == "bedrock"


def test_resolve_provider_bedrock_falls_through_without_aws_creds(monkeypatch):
    """boto3 已装但无 AWS 凭据 → 跳过 bedrock 继续探测（no_provider）。"""
    _find_spec_gated(monkeypatch, object())
    _neutralize_auto_resolve(monkeypatch)
    monkeypatch.setattr("agent.bedrock_adapter.has_aws_credentials", lambda: False)
    with pytest.raises(AuthError) as exc:
        resolve_provider("auto")
    assert exc.value.code == "no_provider_configured"


# ---------------------------------------------------------------------------
# has_aws_credentials
# ---------------------------------------------------------------------------

def test_has_aws_credentials_false_without_boto3(monkeypatch):
    """boto3 未装：即使 AWS env vars 在，也直接 False（不建 botocore session）。"""
    _find_spec_gated(monkeypatch, None)
    from agent.bedrock_adapter import has_aws_credentials
    assert has_aws_credentials(
        {"AWS_ACCESS_KEY_ID": "AKIA...", "AWS_SECRET_ACCESS_KEY": "sec"}
    ) is False


def test_has_aws_credentials_env_fast_path_when_boto3_present(monkeypatch):
    """boto3 已装：env-var 快路径不变（不需要 botocore）。"""
    _find_spec_gated(monkeypatch, object())
    from agent.bedrock_adapter import has_aws_credentials
    assert has_aws_credentials(
        {"AWS_ACCESS_KEY_ID": "AKIA...", "AWS_SECRET_ACCESS_KEY": "sec"}
    ) is True


def test_has_aws_credentials_false_without_aws_creds(monkeypatch):
    """boto3 已装但无凭据 → False（botocore session 返回 None）。"""
    _find_spec_gated(monkeypatch, object())
    mock_session = MagicMock()
    mock_session.get_credentials.return_value = None
    session_mod = types.ModuleType("botocore.session")
    session_mod.get_session = MagicMock(return_value=mock_session)
    botocore_mod = types.ModuleType("botocore")
    botocore_mod.session = session_mod
    with patch.dict("sys.modules", {"botocore": botocore_mod, "botocore.session": session_mod}):
        from agent.bedrock_adapter import has_aws_credentials
        assert has_aws_credentials({}) is False


# ---------------------------------------------------------------------------
# bedrock_adapter import side effects
# ---------------------------------------------------------------------------

def test_bedrock_adapter_import_has_no_lazy_deps_side_effect():
    """import agent.bedrock_adapter 不得触发 lazy_deps.ensure（无副作用）。"""
    code = (
        "import sys, types\n"
        "sys.modules['tools.lazy_deps'] = types.SimpleNamespace("
        "ensure=lambda *a, **k: (_ for _ in ()).throw("
        "AssertionError('ensure called at import time')))\n"
        "import agent.bedrock_adapter\n"
        "print('import-ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "import-ok" in proc.stdout

"""batch86 任务 3 —— 裁决接入 + 内置只读种子表冷启动（OPS-DELTA #102）。

覆盖验收：i sudo lscpu / free -h / cat /etc/hosts（读形态）在 prod 不再弹
审批（种子表命中）；j systemctl restart（prod required）仍弹——白名单不跳过
强制人工门；k unknown 只读命令第 1-3 次弹审批、第 4 次直接执行（沉淀生效）；
l kubectl delete / rm -rf 形态永不进白名单、永远走原判定；m 矩阵缺失 deny
与种子/白名单无冲突（矩阵缺失仍 deny 一切）。
"""

from __future__ import annotations

import yaml

import pytest

import hermes_cli.config as hc
from tools import approval_memory as am
from tools.ops_permissions import check_ops_command_permission


def _write_matrix(home, matrix) -> None:
    (home / "matrix.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "updated_at": "2026-09-01T00:00:00+08:00",
        "source": "test",
        "base_template": "template2",
        "matrix": matrix,
        "sources": {
            env: {act: "test" for act in cells} for env, cells in matrix.items()
        },
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")


T2 = {
    "prod": {
        "query": "execute", "fetch_log": "execute", "verify": "execute",
        "restart": {"approve": "required"}, "reboot": {"approve": "required"},
        "remove": {"approve": "required"}, "run_script": {"approve": "required"},
        "deploy": {"approve": "required"}, "install": {"approve": "required"},
    },
    "dev": {"query": "execute", "restart": "approve", "remove": "approve"},
    "test": {"query": "execute", "restart": "execute"},
    "local": {"query": "execute", "restart": "execute"},
}


@pytest.fixture
def ops_env(tmp_path, monkeypatch):
    def _activate(env="prod", matrix=None):
        (tmp_path / "config.yaml").write_text(
            "ops:\n  permissions:\n    enabled: true\n"
            f"    env: {env}\n    role: operator\n",
            encoding="utf-8",
        )
        if matrix is not None:
            _write_matrix(tmp_path, matrix)
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        hc._LOAD_CONFIG_CACHE.clear()
        am._read_cache.clear()
        return tmp_path
    yield _activate
    hc._LOAD_CONFIG_CACHE.clear()
    am._read_cache.clear()


def _sediment(template, n=3, *, opt_in=False):
    for _ in range(n):
        am.record_success(template, user_opt_in=opt_in, source_task="test")


# ---------------------------------------------------------------------------
# i — 种子命中：prod 只读命令不再弹审批
# ---------------------------------------------------------------------------

def test_seed_hit_skips_prod_approval(ops_env):
    ops_env("prod", T2)
    for cmd in ("sudo lscpu", "sudo free -h", "sudo cat /etc/hosts",
                "ls -la", "df -h", "ps aux"):
        assert check_ops_command_permission(cmd) is None, cmd


# ---------------------------------------------------------------------------
# j — required 强制人工永不被白名单跳过
# ---------------------------------------------------------------------------

def test_required_gate_never_skipped(ops_env):
    ops_env("prod", T2)
    # 即使模板已 active（人为把 restart 塞进白名单），required 门仍弹
    _sediment("systemctl", opt_in=True)
    am.forget("systemctl")
    result = check_ops_command_permission("sudo systemctl restart nginx")
    assert result is not None
    assert result["action"] == "approve"
    assert result["require_confirmation"] is True


# ---------------------------------------------------------------------------
# k — unknown 只读命令：1-3 弹审批 → 第 4 次直接执行
# ---------------------------------------------------------------------------

def test_unknown_readonly_sediments_to_skip(ops_env):
    ops_env("prod", T2)
    assert check_ops_command_permission("sudo lsblk") is not None  # 1st 弹
    _sediment("lsblk", n=2)
    assert check_ops_command_permission("sudo lsblk") is not None  # count=2 仍弹
    am.record_success("lsblk", source_task="test")                # 3rd → active
    assert am.is_active("lsblk")
    assert check_ops_command_permission("sudo lsblk") is None     # 4th 直接执行


# ---------------------------------------------------------------------------
# l — kubectl delete / rm -rf 形态永不进白名单、永远走原判定
# ---------------------------------------------------------------------------

def test_destructive_shapes_never_whitelisted(ops_env):
    ops_env("prod", T2)
    assert am.normalize_template("kubectl delete ns foo") is None
    assert am.normalize_template("rm -rf /tmp/x") is None
    # 原判定不受影响：kubectl delete（remove + 目标解析失败）→ deny
    result = check_ops_command_permission("kubectl delete ns foo")
    assert result is not None and result["action"] == "deny"
    # rm -rf（remove 高危目标解析失败）→ deny，非白名单放行
    result = check_ops_command_permission("rm -rf /tmp/x")
    assert result is not None and result["action"] == "deny"


# ---------------------------------------------------------------------------
# m — 矩阵缺失 deny 与种子/白名单无冲突
# ---------------------------------------------------------------------------

def test_matrix_missing_still_denies_everything(ops_env):
    ops_env("prod", None)  # 无 matrix.yaml
    for cmd in ("sudo lscpu", "sudo cat /etc/hosts", "sudo lsblk", "ls"):
        result = check_ops_command_permission(cmd)
        assert result is not None
        assert result["action"] == "deny"
        assert result.get("matrix_missing") is True

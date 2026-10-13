"""task103 任务 5(b)：拆 ``_active_env()`` 载体（验收 b 的铁证测试）。

config ``ops.permissions.env`` 退场（§11.8）：env 维度一律取自**操作目标实体**
（拓扑表）。本测试把 config env 依次改成 local/test/dev/prod，跑同一批命令，断言
裁决结果**逐字段完全不变**（同一命令同一结果）。

矩阵刻意让各 env 档位不同（restart：local=approve / test=execute / dev=approve /
prod=required；query、run_script 亦不同），因此"读 config env"的实现必然随取值
变化——反证（基线 8ba4b5ce）：结果随 config env 改变，断言相等失败。

命令批（#103 后目标实体固定）：
- ``systemctl restart myapp`` 无 token → 本机 workstation(env=local) → approve；
- ``docker ps``               无 SSH 族 token → 本机 → query execute（None）；
- ``ls -la``                  无 token + unknown → 本机 → 默认 approve；
- ``ssh root@203.0.113.11 'df -h'`` → node-prod(env=prod) → run_script required。
"""

from __future__ import annotations

import yaml

import pytest

import hermes_cli.config as hc
from tools.ops_permissions import check_ops_command_permission

COMMANDS = (
    "systemctl restart myapp",
    "docker ps",
    "ls -la",
    "ssh root@203.0.113.11 'df -h'",
)

_REQ = {"approve": "required"}
MATRIX = {
    "local": {"query": "execute", "restart": "approve", "run_script": "approve"},
    "test": {"query": "execute", "restart": "execute", "run_script": _REQ},
    "dev": {"query": "approve", "restart": "approve", "run_script": _REQ},
    "prod": {"query": "execute", "restart": _REQ, "run_script": _REQ},
}

TOPO = """\
version: 4
environments:
  - {name: local, isolation: relaxed, role: local}
  - {name: test, isolation: relaxed, role: test}
  - {name: dev, isolation: relaxed, role: dev}
  - {name: prod, isolation: strict, role: prod}
clusters:
  - {name: k3s-local, env: local, type: k3s, endpoint: "https://203.0.113.5:6443"}
  - {name: k3s-prod, env: prod, type: k3s, endpoint: "https://203.0.113.15:6443"}
hosts:
  - {name: workstation, type: host, env: local, endpoint: "192.168.1.50"}
  - {name: node-prod, type: host, env: prod, endpoint: "203.0.113.11"}
"""


def _write_matrix(home) -> None:
    (home / "matrix.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "updated_at": "2026-10-07T00:00:00+08:00",
        "source": "test",
        "base_template": "template2",
        "matrix": MATRIX,
        "sources": {e: {a: "test" for a in c} for e, c in MATRIX.items()},
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _set_config_env(home, env: str) -> None:
    (home / "config.yaml").write_text(
        "ops:\n"
        "  permissions:\n"
        "    enabled: true\n"
        f"    env: {env}\n"
        "    role: operator\n",
        encoding="utf-8",
    )
    hc._LOAD_CONFIG_CACHE.clear()


@pytest.fixture
def env_home(tmp_path, monkeypatch):
    (tmp_path / "topology.yaml").write_text(TOPO, encoding="utf-8")
    _write_matrix(tmp_path)
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    monkeypatch.setattr(
        "tools.target_resolve._local_host_identities", lambda: ["workstation"])
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()


def _snapshot(command: str):
    """裁决结果规范化为可比较元组（None = execute 直接放行）。"""
    d = check_ops_command_permission(command)
    if d is None:
        return ("execute",)
    return (
        d.get("action"), d.get("action_name"), d.get("env"),
        d.get("env_tier"), d.get("level"), d.get("require_confirmation"),
    )


def test_config_env_does_not_change_decisions(env_home):
    """★ 验收 b：config env 取任意值 → 同批命令裁决结果完全不变。"""
    baseline = None
    for env in ("local", "test", "dev", "prod"):
        _set_config_env(env_home, env)
        snap = {cmd: _snapshot(cmd) for cmd in COMMANDS}
        if baseline is None:
            baseline = snap
        else:
            assert snap == baseline, f"config env={env} 改变了裁决：{snap} != {baseline}"


def test_target_entity_env_is_the_source(env_home):
    """env 来自目标实体（本机 local / node-prod prod），与 config env 无关。"""
    _set_config_env(env_home, "prod")  # config 撒谎：说是 prod，实际目标是本机
    assert _snapshot("systemctl restart myapp") == (
        "approve", "restart", "local", "local", "approve", False)
    assert _snapshot("docker ps") == ("execute",)
    assert _snapshot("ssh root@203.0.113.11 'df -h'")[3] == "prod"


def test_deprecate_warning_fires_once(env_home):
    """启动 deprecate 警告：config env 非空 → 恰好一次；不阻断（读取兼容）。"""
    import logging
    from tools import ops_permissions as op

    _set_config_env(env_home, "test")
    op._WARNED_ENV_CARRIER = False
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    logger = logging.getLogger("tools.ops_permissions")
    logger.addHandler(handler)
    try:
        op.warn_deprecated_active_env()
        op.warn_deprecated_active_env()
        op.warn_deprecated_active_env()
    finally:
        logger.removeHandler(handler)
        op._WARNED_ENV_CARRIER = False
    warned = [r for r in records if "已退场" in r]
    assert len(warned) == 1, warned
    assert "ops.permissions.env" in warned[0]

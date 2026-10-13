"""task103 任务 5(a)：prod 变更硬门退役（batch80 / OPS-DELTA #95 → §11.8 #5）。

依据（可复现）：硬门专属文案 `prod 变更强制人工确认` 日志 0 次 vs 矩阵 required
文案 `操作矩阵强制人工确认` 92 次 ⇒ 硬门从未产生额外拦截；其覆盖范围矩阵 prod 行
已表达（含 prod.enable/disable/backup/apply_config 那 5 格）。删除后严格度全部交回
矩阵：
- prod × 矩阵 required（kubectl delete/apply、restart 等）→ 与硬门同结果（required）；
- prod × 矩阵 approve（enable/disable/backup/apply_config）→ 恢复 smart 可批
  （require_confirmation=False，用户"审批过多"诉求的正解，= 消除死格子）；
- prod × 矩阵 execute → 直接放行（矩阵是唯一裁决源，不再被硬门升到人工）。

反证（基线 8ba4b5ce）：prod × approve → 硬门强制 require_confirmation=True
（断言 False 失败）；prod × execute → 硬门升到人工（断言 None 失败）。
"""

from __future__ import annotations

import yaml

import pytest

import hermes_cli.config as hc
from tools import matrix_data as md
from tools.ops_permissions import check_ops_command_permission


def _write_matrix(home, matrix: dict) -> None:
    (home / "matrix.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "updated_at": "2026-10-07T00:00:00+08:00",
        "source": "test",
        "base_template": "template2",
        "matrix": matrix,
        "sources": {env: {act: "test" for act in cells}
                    for env, cells in matrix.items()},
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _cfg(tmp_path, local_env="prod", *, matrix=None, extra="") -> str:
    (tmp_path / "config.yaml").write_text(
        "ops:\n"
        "  permissions:\n"
        "    enabled: true\n"
        "    env: test\n"          # #103：config env 退场，不再参与裁决
        "    role: operator\n"
        f"{extra}",
        encoding="utf-8",
    )
    if matrix is None:
        # 真实 template2（含 unresolved 行）；写盘 + 校验走 init_matrix。
        md.init_matrix("template2", home=tmp_path, force=True)
    else:
        _write_matrix(tmp_path, matrix)
    (tmp_path / "topology.yaml").write_text(
        "version: 4\n"
        "environments:\n"
        "  - {name: local, isolation: relaxed, role: local}\n"
        "  - {name: prod, isolation: strict, role: prod}\n"
        "clusters:\n"
        "  - {name: k3s-prod, env: prod, type: k3s, endpoint: \"https://203.0.113.15:6443\"}\n"
        "hosts:\n"
        f"  - {{name: workstation, type: host, env: {local_env}, endpoint: \"192.168.1.50\"}}\n",
        encoding="utf-8",
    )
    return str(tmp_path)


@pytest.fixture
def penv(tmp_path, monkeypatch):
    def _activate(local_env="prod", *, matrix=None, extra=""):
        _cfg(tmp_path, local_env, matrix=matrix, extra=extra)
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        monkeypatch.setattr(
            "tools.target_resolve._local_host_identities", lambda: ["workstation"])
        hc._LOAD_CONFIG_CACHE.clear()
        return tmp_path

    yield _activate
    hc._LOAD_CONFIG_CACHE.clear()
    from tools import ops_permissions as _op
    _op._WARNED_ENVS.clear()


def test_template2_prod_approve_cells_are_smart_approvable(penv):
    """(g) prod.enable/disable/backup 恢复 smart 可批。

    本机 host 声明 prod（Dogfood 场景：prod 服务器上就地操作）；template2 prod
    行把这几个动作定档 approve —— 硬门退役后 require_confirmation=False。
    """
    penv("prod")
    for cmd, action in (
        ("systemctl enable nginx", "enable"),
        ("systemctl disable nginx", "disable"),
        ("pg_dump mydb > /tmp/x.sql", "backup"),
    ):
        decision = check_ops_command_permission(cmd)
        assert decision is not None, cmd
        assert decision["action_name"] == action, cmd
        assert decision["env"] == "prod", cmd
        assert decision["level"] == "approve", cmd
        assert decision["require_confirmation"] is False, cmd
        assert "强制人工确认" not in decision["description"], cmd


def test_template2_prod_apply_config_cell_is_approve():
    """(g) prod.apply_config 在 template2 定为 approve（不再是死格子）。"""
    assert md.template_cells("template2", "prod")["apply_config"] == "approve"


def test_prod_hardgate_message_gone(penv):
    """硬门专属文案不再出现（退役实证）。"""
    penv("prod")
    decision = check_ops_command_permission("systemctl enable nginx")
    assert decision is not None
    assert "prod 变更强制人工确认" not in decision["description"]
    assert "OPS-DELTA #95" not in decision["description"]


def test_prod_decommission_required_unchanged(penv):
    """(f) kubectl delete 打 prod → 矩阵 required（与硬门同结果）。"""
    penv("prod")
    decision = check_ops_command_permission(
        "kubectl delete ns foo --context k3s-prod")
    assert decision is not None
    assert decision["action_name"] == "decommission"
    assert decision["level"] == "required"
    assert decision["require_confirmation"] is True
    assert "操作矩阵强制人工确认" in decision["description"]


def test_prod_deploy_required_unchanged(penv):
    """(f) kubectl apply 打 prod → 矩阵 required（与硬门同结果）。"""
    penv("prod")
    decision = check_ops_command_permission(
        "kubectl apply -f x.yaml --context k3s-prod")
    assert decision is not None
    assert decision["action_name"] == "deploy"
    assert decision["level"] == "required"
    assert decision["require_confirmation"] is True


def test_prod_execute_level_passes_directly(penv):
    """矩阵是唯一裁决源：prod × execute → 直接放行（硬门不再升级到人工）。"""
    penv("prod", matrix={"prod": {"query": "execute", "restart": "execute"}})
    assert check_ops_command_permission("systemctl restart myapp") is None


def test_prod_readonly_still_passes(penv):
    """prod 只读矩阵 execute → 直接放行（回归不变）。"""
    penv("prod")
    assert check_ops_command_permission("kubectl get pods --context k3s-prod") is None


def test_prod_scale_approve_not_forced(penv):
    """prod × scale=approve → 不强制（矩阵单源；回归）。"""
    penv("prod", matrix={"prod": {"scale": "approve"}})
    decision = check_ops_command_permission(
        "kubectl --context k3s-prod scale deployment/x --replicas=1")
    assert decision is not None
    assert decision["action_name"] == "scale"
    assert decision["level"] == "approve"
    assert decision["require_confirmation"] is False


def test_e2e_prod_enable_smart_auto_approved(penv, monkeypatch):
    """(g) E2E：prod enable（矩阵 approve）在 smart 模式自动放行。

    硬门在时会被降级为人工确认；退役后恢复矩阵 approve 语义（smart 可批）。
    """
    import tools.approval as ap

    penv("prod", extra="approvals:\n  mode: smart\n")
    monkeypatch.setenv("VIGIL_SESSION_KEY", "t103-hardgate-e2e")
    monkeypatch.setenv("VIGIL_EXEC_ASK", "1")
    monkeypatch.setenv("VIGIL_GATEWAY_SESSION", "1")
    monkeypatch.setattr(ap, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(ap, "_smart_approve", lambda *a, **k: "approve")
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
    )

    result = ap.check_all_command_guards("systemctl enable nginx", "local")
    assert result["approved"] is True
    assert result.get("smart_approved") is True, result
    assert result.get("user_approved") is None, result

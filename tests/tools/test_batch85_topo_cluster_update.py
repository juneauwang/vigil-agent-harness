"""batch85（OPS-DELTA #101）—— topo_update 白名单扩展：cluster 行 6 字段 +
host 行 cluster 字段。

验收映射：
  e. cluster 的 env/type/endpoint/host_groups/notes/provenance 各字段更新成功（读回一致）；
  f. 更新 cluster env → 强制人工审批（require_confirmation）；拒绝不写；批准后
     矩阵按新 env 裁决（目标实体 env 从 cluster 读）；
  g. host 行 cluster 字段更新成功（归集群/移出集群）；
  h. cluster 的 name / source → 拒绝（报错说明原因：name 走 rename，source 系统维护）；
  i. 原有 L2 服务行白名单行为不变（回归）。
"""

from __future__ import annotations

import json

import yaml

import pytest

import hermes_cli.config as hc
import tools.approval as approval_module
from tools.ops_permissions import check_ops_command_permission as ck
from tools.topo_tools import topo_update

TOPOLOGY = """\
version: 4
updated_at: '2026-08-31'
environments:
  - {name: local, isolation: relaxed, role: local}
  - {name: test, isolation: relaxed, role: test}
  - {name: dev, isolation: relaxed, role: dev}
  - {name: prod, isolation: strict, role: prod}
clusters:
  - {name: k3s-prod, env: prod, type: k3s, endpoint: "https://203.0.113.15:6443", host_groups: [k8s], notes: "生产 k3s", provenance: terraform}
  - {name: dev-cluster, env: dev, type: k3s, endpoint: "https://203.0.113.55:6443"}
hosts:
  - {name: node2, type: host, env: prod, cluster: k3s-prod, endpoint: "203.0.113.11"}
  - {name: workstation, type: host, env: dev, endpoint: "192.168.1.50"}
"""

MATRIX = {
    "prod": {
        "query": "execute", "fetch_log": "execute", "verify": "execute",
        "run_script": {"approve": "required"},
        "install": {"approve": "required"}, "remove": {"approve": "required"},
        "restart": "approve",
    },
    "dev": {
        "query": "execute",
        "install": {"approve": "required"}, "remove": "approve",
        "run_script": "approve",
    },
}


def _load(result: str) -> dict:
    return json.loads(result)


@pytest.fixture
def topo_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "entities").mkdir(parents=True)
    (home / "topology.yaml").write_text(TOPOLOGY, encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def _cluster_row(home, name="k3s-prod") -> dict:
    topo = yaml.safe_load((home / "topology.yaml").read_text(encoding="utf-8"))
    return next(c for c in topo["clusters"] if c["name"] == name)


def _host_row(home, name) -> dict:
    topo = yaml.safe_load((home / "topology.yaml").read_text(encoding="utf-8"))
    return next(h for h in topo["hosts"] if h["name"] == name)


class TestClusterFieldWrites:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("type", "k3s-ha"),
            ("endpoint", "https://203.0.113.66:6443"),
            ("host_groups", ["k8s", "edge"]),
            ("notes", "新备注"),
            ("provenance", "terraform"),
        ],
    )
    def test_cluster_field_written_and_readback(self, topo_home, field, value):
        # dev-cluster（env=dev）不触发 PROD 审批门，验证纯白名单写入
        result = _load(topo_update("dev-cluster", {field: value}))
        assert result["status"] == "updated"
        assert result["type"] == "cluster"
        assert _cluster_row(topo_home, "dev-cluster")[field] == value

    def test_cluster_env_update_requires_approval_deny_keeps_old(self, topo_home, monkeypatch):
        calls = []

        def fake_request_tool_approval(tool_name, reason, *, rule_key="", approval_callback=None):
            calls.append((tool_name, reason, rule_key))
            return {"approved": False, "message": "denied by test"}

        monkeypatch.setattr(approval_module, "request_tool_approval", fake_request_tool_approval)
        result = _load(topo_update("k3s-prod", {"env": "dev"}))
        assert result["approved"] is False
        assert calls[0][2] == "topo_update:cluster-env:k3s-prod"
        assert "矩阵裁决依据" in calls[0][1]
        # 拒绝后不写
        assert _cluster_row(topo_home)["env"] == "prod"

    def test_cluster_env_update_approved_writes_and_matrix_uses_new_env(self, topo_home, monkeypatch):
        """验收 f：批准后 cluster env 落盘，且矩阵按新 env 裁决（目标实体 env 从
        cluster 读——ssh node2 高危变更从 prod 档变 dev 档）。"""
        (topo_home / "config.yaml").write_text(
            "ops:\n  permissions:\n    enabled: true\n    env: test\n    role: test\n",
            encoding="utf-8",
        )
        (topo_home / "matrix.yaml").write_text(yaml.safe_dump({
            "schema_version": 1,
            "updated_at": "2026-08-31T00:00:00+08:00",
            "source": "test",
            "base_template": "template2",
            "matrix": MATRIX,
            "sources": {env: {act: "test" for act in cells} for env, cells in MATRIX.items()},
        }, allow_unicode=True, sort_keys=False), encoding="utf-8")

        monkeypatch.setattr(
            approval_module, "request_tool_approval",
            lambda *a, **kw: {"approved": True, "message": None},
        )
        cmd = "ssh root@node2 'kubectl delete ns foo'"
        before = ck(cmd)
        assert before["env"] == "prod" and before["level"] == "required"

        result = _load(topo_update("k3s-prod", {"env": "dev"}))
        assert result["status"] == "updated"
        assert _cluster_row(topo_home)["env"] == "dev"

        after = ck(cmd)
        assert after["env"] == "dev", f"矩阵应按 cluster 新 env 裁决: {after}"
        assert after["level"] == "approve"

    def test_cluster_env_approval_rule_key_distinct_from_prod(self, topo_home, monkeypatch):
        calls = []
        monkeypatch.setattr(
            approval_module, "request_tool_approval",
            lambda tool, reason, *, rule_key="", approval_callback=None:
                calls.append(rule_key) or {"approved": False, "message": "no"},
        )
        topo_update("k3s-prod", {"env": "dev"})
        assert calls == ["topo_update:cluster-env:k3s-prod"]


class TestClusterRejects:
    def test_cluster_name_rejected_with_rename_hint(self, topo_home):
        result = _load(topo_update("k3s-prod", {"name": "renamed"}))
        assert "error" in result
        assert "rename" in result["error"]
        assert "name 是实体标识" in result["error"]
        assert _cluster_row(topo_home)["name"] == "k3s-prod"

    def test_cluster_source_rejected_system_maintained(self, topo_home):
        result = _load(topo_update("k3s-prod", {"source": "manual"}))
        assert "error" in result
        assert "系统" in result["error"]
        assert "source" in result["error"]

    def test_cluster_unknown_field_rejected(self, topo_home):
        result = _load(topo_update("k3s-prod", {"owner": "x"}))
        assert "error" in result
        assert "owner" in result["error"]

    def test_cluster_host_groups_non_list_rejected(self, topo_home):
        result = _load(topo_update("dev-cluster", {"host_groups": "k8s"}))
        assert "error" in result
        assert "字符串数组" in result["error"]


class TestHostClusterField:
    def test_host_joins_cluster(self, topo_home):
        result = _load(topo_update("workstation", {"cluster": "dev-cluster"}))
        assert result["status"] == "updated"
        assert result["host_cluster_updated"] is True
        assert _host_row(topo_home, "workstation")["cluster"] == "dev-cluster"
        # host cluster 不进 L3 档案（事实来源 = topology.yaml host 行）
        assert not (topo_home / "entities" / "workstation.yaml").exists()

    def test_host_moves_out_cluster(self, topo_home, monkeypatch):
        """node2 是 PROD host——移出集群仍需过 PROD 审批门。"""
        monkeypatch.setattr(
            approval_module, "request_tool_approval",
            lambda *a, **kw: {"approved": True, "message": None},
        )
        result = _load(topo_update("node2", {"cluster": "default"}))
        assert result["status"] == "updated"
        assert _host_row(topo_home, "node2")["cluster"] == "default"

    def test_host_cluster_bad_value_rejected(self, topo_home):
        result = _load(topo_update("workstation", {"cluster": ""}))
        assert "error" in result
        assert "cluster" in result["error"]
        assert "cluster" not in _host_row(topo_home, "workstation")


class TestRegressionL2Service:
    def test_l2_service_whitelist_unchanged(self, topo_home, monkeypatch):
        """验收 i：服务行（L2）白名单行为不变——endpoint 更新仍成功且不进
        cluster 路径。"""
        monkeypatch.setattr(
            approval_module, "request_tool_approval",
            lambda *a, **kw: {"approved": True, "message": None},
        )
        (topo_home / "services").mkdir()
        (topo_home / "services" / "node2.yaml").write_text(
            "host: node2\nservices:\n  - {name: harbor, type: registry, env: prod}\n",
            encoding="utf-8",
        )
        result = _load(topo_update("harbor", {"endpoint": "203.0.113.11:30443"}))
        assert result["status"] == "updated"
        assert result.get("type") != "cluster"  # 不误入 cluster 路径
        saved = (topo_home / "services" / "node2.yaml").read_text(encoding="utf-8")
        assert "203.0.113.11:30443" in saved

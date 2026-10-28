"""topo_delete —— 拓扑实体生命周期第三段（归档后删除）契约测试。

覆盖（任务书 dsh-task-topo-delete「必测」7 条）：
  1. 删 L1 host 行：topology.yaml 只剩另一行、updated_at 变、归档含被删行；
  2. 有 L3 档案的实体：档案文件被删、归档里留 detail 内容；
  3. 服务行：services/<host>.yaml 摘行、同索引其他服务不受影响；
  4. 引用拦截：depends_on 命中 → 报错列引用者；force=true 才删（referrers_ignored）；
  5. PROD 档走人工审批：拒绝 → 错误 + topology.yaml 未变 + 无归档；
  6. 归档失败即中止：返回错误 + topology.yaml 未变（禁止「删了没归档」）；
  7. 审计事件：type/tool/action/meta 字段齐全。

另补：cluster / cross_host 行删除、非 PROD 不过人工门（沿用既有档位语义）、
不存在实体、实体名大小写/点号（文件名净化）与归档路径不回写越界。
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest
import yaml

import tools.approval as approval_module
from tools.topo_tools import topo_delete

BASE_HOSTS = [
    {"name": "node1", "env": "dev", "cluster": "dev-cluster",
     "services_index": "services/node1.yaml"},
    {"name": "node2", "env": "dev", "cluster": "dev-cluster"},
]

NODE1_INDEX = """\
host: node1
services:
  - {name: harbor, type: registry, env: dev}
  - {name: grafana, type: dashboard, env: dev}
"""

HARBOR_DETAIL = """\
name: harbor
snapshot:
  common:
    version: v1.2.3
  by_type:
    registry:
      registry: harbor.local
"""


def _write_topology(home: Path, *, hosts=None, clusters=None, cross_host=None,
                    updated_at: str = "2026-01-01") -> None:
    """按需重写 topology.yaml（默认 base：2 hosts + 1 cluster）。"""
    doc = {
        "version": 4,
        "updated_at": updated_at,
        "environments": [
            {"name": "dev", "isolation": "relaxed", "role": "dev"},
            {"name": "prod", "isolation": "strict", "role": "prod"},
        ],
        "clusters": [
            {"name": "dev-cluster", "env": "dev", "type": "k3s"},
        ] if clusters is None else clusters,
        "hosts": [dict(h) for h in (BASE_HOSTS if hosts is None else hosts)],
    }
    if cross_host is not None:
        doc["cross_host"] = cross_host
    (home / "topology.yaml").write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


@pytest.fixture
def topo_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "services").mkdir(parents=True)
    (home / "entities").mkdir()
    _write_topology(home)
    (home / "services" / "node1.yaml").write_text(NODE1_INDEX, encoding="utf-8")
    (home / "entities" / "harbor.yaml").write_text(HARBOR_DETAIL, encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(home))
    import hermes_cli.config as _hc
    _hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        _hc._LOAD_CONFIG_CACHE.clear()


def _load(result: str) -> dict:
    return json.loads(result)


def _topology(home: Path) -> dict:
    return yaml.safe_load((home / "topology.yaml").read_text(encoding="utf-8"))


def _archives(home: Path) -> list:
    return sorted((home / "topology-archive").rglob("*.yaml"))


def _archive_of(home: Path, entity: str) -> dict:
    path = home / "topology-archive" / _dt.date.today().isoformat() / f"{entity}.yaml"
    assert path.is_file(), f"归档文件不存在: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _deny_approval(monkeypatch, calls: list | None = None):
    calls = calls if calls is not None else []

    def fake_request_tool_approval(tool_name, reason, *, rule_key="", approval_callback=None):
        calls.append((tool_name, reason, rule_key))
        return {"approved": False, "message": "denied by test"}

    monkeypatch.setattr(approval_module, "request_tool_approval", fake_request_tool_approval)
    return calls


# ---------------------------------------------------------------------------
# 必测 1：删 L1 host 行
# ---------------------------------------------------------------------------

class TestDeleteHostRow:
    def test_delete_host_row(self, topo_home):
        before = _topology(topo_home)
        assert [h["name"] for h in before["hosts"]] == ["node1", "node2"]

        result = _load(topo_delete("node2", reason="主机下线"))

        assert result["status"] == "deleted"
        assert result["entity"] == "node2"
        assert result["env"] == "dev"
        assert result["forced"] is False
        assert result["referrers_ignored"] == []
        assert result["archived"] == (
            f"topology-archive/{_dt.date.today().isoformat()}/node2.yaml"
        )

        after = _topology(topo_home)
        assert [h["name"] for h in after["hosts"]] == ["node1"]
        assert after["updated_at"] == _dt.date.today().isoformat()
        assert after["updated_at"] != before["updated_at"]

        archived = _archive_of(topo_home, "node2")
        assert archived["entity"] == "node2"
        assert archived["env"] == "dev"
        assert archived["source"] == "agent"
        assert archived["reason"] == "主机下线"
        assert archived["deleted_at"].startswith(_dt.date.today().isoformat())
        assert archived["row"]["name"] == "node2"
        assert archived["row"]["cluster"] == "dev-cluster"
        assert archived["detail"] is None
        assert "topology.yaml:hosts[node2]" in archived["removed"]

    def test_delete_cluster_row(self, topo_home):
        result = _load(topo_delete("dev-cluster", reason="集群解散"))
        assert result["status"] == "deleted"
        after = _topology(topo_home)
        assert after["clusters"] == []
        assert _archive_of(topo_home, "dev-cluster")["row"]["type"] == "k3s"

    def test_delete_cross_host_row(self, topo_home):
        _write_topology(topo_home, cross_host=[{"name": "billing", "type": "app"}])
        result = _load(topo_delete("billing"))
        assert result["status"] == "deleted"
        assert _topology(topo_home).get("cross_host") == []
        assert "topology.yaml:cross_host[billing]" in result["removed"]

    def test_missing_entity_returns_error(self, topo_home):
        result = _load(topo_delete("nope"))
        assert "error" in result
        assert "nope" in result["error"]

    def test_entity_name_with_dots_and_caps_is_archived_safely(self, topo_home):
        _write_topology(topo_home, hosts=BASE_HOSTS + [
            {"name": "39.106.217.32", "env": "dev"},
            {"name": "LAPTOP-T2JA2ERE", "env": "dev"},
        ])
        for name in ("39.106.217.32", "LAPTOP-T2JA2ERE"):
            assert _load(topo_delete(name))["status"] == "deleted"
        names = sorted(p.name for p in _archives(topo_home))
        assert names == ["39.106.217.32.yaml", "LAPTOP-T2JA2ERE.yaml"]
        assert _archive_of(topo_home, "LAPTOP-T2JA2ERE")["row"]["name"] == "LAPTOP-T2JA2ERE"


# ---------------------------------------------------------------------------
# 必测 2：L3 档案一起归档并删除
# ---------------------------------------------------------------------------

class TestDeleteL3Detail:
    def test_delete_l3_entity_removes_detail_file(self, topo_home):
        detail_file = topo_home / "entities" / "harbor.yaml"
        assert detail_file.is_file()

        result = _load(topo_delete("harbor", reason="服务下线"))

        assert result["status"] == "deleted"
        assert not detail_file.exists()
        assert "entities/harbor.yaml" in result["removed"]
        archived = _archive_of(topo_home, "harbor")
        assert archived["detail"]["snapshot"]["common"]["version"] == "v1.2.3"
        assert archived["detail"]["snapshot"]["by_type"]["registry"]["registry"] == "harbor.local"
        assert archived["row"]["name"] == "harbor"
        assert archived["row"]["host"] == "node1"

    def test_detail_pointer_from_discovery_is_archived_and_removed(self, topo_home):
        """发现链路写的 L2 行带 detail 指针（entities/<cluster>__<host>__<name>.yaml）。"""
        detail_rel = "entities/dev-cluster__node1__harbor.yaml"
        (topo_home / detail_rel).write_text(
            "name: harbor\nsnapshot:\n  common:\n    version: v9\n", encoding="utf-8"
        )
        index = yaml.safe_load((topo_home / "services" / "node1.yaml").read_text(encoding="utf-8"))
        index["services"][0]["detail"] = detail_rel
        (topo_home / "services" / "node1.yaml").write_text(
            yaml.safe_dump(index, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

        result = _load(topo_delete("harbor"))

        assert result["status"] == "deleted"
        assert detail_rel in result["removed"]
        assert not (topo_home / detail_rel).exists()
        assert _archive_of(topo_home, "harbor")["detail"]["snapshot"]["common"]["version"] == "v9"

    def test_entity_without_detail_file_still_deletes(self, topo_home):
        result = _load(topo_delete("grafana"))
        assert result["status"] == "deleted"
        assert result["removed"] == ["services/node1.yaml:services[grafana]"]
        assert _archive_of(topo_home, "grafana")["detail"] is None


# ---------------------------------------------------------------------------
# 必测 3：服务行 + 所属 host 索引
# ---------------------------------------------------------------------------

class TestDeleteServiceRow:
    def test_delete_service_row_and_host_index(self, topo_home):
        index_path = topo_home / "services" / "node1.yaml"

        result = _load(topo_delete("harbor"))

        assert result["status"] == "deleted"
        assert result["removed"][0] == "services/node1.yaml:services[harbor]"
        index = yaml.safe_load(index_path.read_text(encoding="utf-8"))
        assert [s["name"] for s in index["services"]] == ["grafana"]
        assert index["services"][0]["type"] == "dashboard"
        # topology.yaml 的 host 行不受影响（服务行住在索引里）
        assert [h["name"] for h in _topology(topo_home)["hosts"]] == ["node1", "node2"]

    def test_delete_service_keeps_l1_updated_at(self, topo_home):
        """服务行删除不重写 topology.yaml（只有 L1 行变动才 bump updated_at）。"""
        topo_delete("harbor")
        assert _topology(topo_home)["updated_at"] == "2026-01-01"


# ---------------------------------------------------------------------------
# 必测 4：引用拦截 / force
# ---------------------------------------------------------------------------

class TestReferenceGuard:
    def _with_dependency(self, topo_home):
        _write_topology(
            topo_home,
            cross_host=[{"name": "billing", "type": "app", "depends_on": ["node2"]}],
        )

    def test_reference_blocks_without_force(self, topo_home):
        self._with_dependency(topo_home)

        result = _load(topo_delete("node2"))

        assert "error" in result
        assert "billing" in result["error"]
        assert result["referrers"] == ["billing(depends_on)"]
        # 什么都没动
        assert [h["name"] for h in _topology(topo_home)["hosts"]] == ["node1", "node2"]
        assert not (topo_home / "topology-archive").exists()

    def test_force_deletes_and_reports_ignored_referrers(self, topo_home):
        self._with_dependency(topo_home)

        result = _load(topo_delete("node2", force=True))

        assert result["status"] == "deleted"
        assert result["forced"] is True
        assert result["referrers_ignored"] == ["billing(depends_on)"]
        assert [h["name"] for h in _topology(topo_home)["hosts"]] == ["node1"]
        assert _archive_of(topo_home, "node2")["entity"] == "node2"

    def test_own_references_do_not_block(self, topo_home):
        """被删实体自己的 depends_on 不算引用（删 node2 时它自己的依赖不拦）。"""
        _write_topology(
            topo_home,
            hosts=[dict(BASE_HOSTS[0]), {"name": "node2", "env": "dev",
                                         "depends_on": ["node1"]}],
        )
        result = _load(topo_delete("node2"))
        assert result["status"] == "deleted"

    def test_service_host_reference_blocks_host_delete(self, topo_home):
        """删 host 时它名下的服务行就是引用（默认不制造孤立服务行）。"""
        result = _load(topo_delete("node1"))
        assert "error" in result
        assert "harbor(host)" in result["error"]
        assert "grafana(host)" in result["error"]
        assert _topology(topo_home)["hosts"][0]["name"] == "node1"

    def test_cluster_member_reference_blocks_host_delete(self, topo_home):
        """cluster 成员表（hosts / members）里还挂着该 host → 删 host 先拦。"""
        _write_topology(
            topo_home,
            clusters=[{"name": "dev-cluster", "env": "dev", "type": "k3s",
                       "hosts": ["node2"]}],
        )
        result = _load(topo_delete("node2"))
        assert "error" in result
        assert "dev-cluster(hosts)" in result["error"]
        assert result["referrers"] == ["dev-cluster(hosts)"]

    def test_cluster_delete_without_member_backref(self, topo_home):
        """成员表是 cluster 指向 host，不是 host 指向 cluster——无反向引用可删。"""
        _write_topology(
            topo_home,
            clusters=[{"name": "dev-cluster", "env": "dev", "type": "k3s",
                       "hosts": ["node2"]}],
        )
        result = _load(topo_delete("dev-cluster"))
        assert result["status"] == "deleted"
        assert _archive_of(topo_home, "dev-cluster")["row"]["hosts"] == ["node2"]


# ---------------------------------------------------------------------------
# 必测 5：PROD 档人工审批
# ---------------------------------------------------------------------------

class TestProdApproval:
    def _prod_host(self, topo_home):
        _write_topology(topo_home, hosts=BASE_HOSTS + [
            {"name": "prod-node", "env": "prod"},
        ])

    def test_prod_requires_approval(self, topo_home, monkeypatch):
        self._prod_host(topo_home)
        before = (topo_home / "topology.yaml").read_text(encoding="utf-8")
        calls = _deny_approval(monkeypatch)

        result = _load(topo_delete("prod-node", reason="下线"))

        assert result["approved"] is False
        assert "denied by test" in result["error"]
        assert calls and calls[0][0] == "topo_delete"
        assert calls[0][2] == "topo_delete:prod:prod-node"
        assert "下线" in calls[0][1]
        # 审批拒绝 → 什么都不动、无归档
        assert (topo_home / "topology.yaml").read_text(encoding="utf-8") == before
        assert not (topo_home / "topology-archive").exists()

    def test_prod_approved_deletes_and_archives(self, topo_home, monkeypatch):
        self._prod_host(topo_home)
        monkeypatch.setattr(
            approval_module, "request_tool_approval",
            lambda *a, **kw: {"approved": True, "message": None},
        )
        result = _load(topo_delete("prod-node"))
        assert result["status"] == "deleted"
        assert _archive_of(topo_home, "prod-node")["env"] == "prod"

    def test_non_prod_skips_human_gate(self, topo_home, monkeypatch):
        """非 PROD 档位沿用既有矩阵语义（同 topo_update）：不弹人工门。"""
        calls = _deny_approval(monkeypatch)
        result = _load(topo_delete("node2"))
        assert result["status"] == "deleted"
        assert calls == []


# ---------------------------------------------------------------------------
# 必测 6：归档失败即中止
# ---------------------------------------------------------------------------

class TestArchiveFailureAborts:
    def test_archive_failure_aborts(self, topo_home, monkeypatch):
        before = (topo_home / "topology.yaml").read_text(encoding="utf-8")
        detail_file = topo_home / "entities" / "harbor.yaml"
        real_write_text = Path.write_text

        def broken_write_text(self, *args, **kwargs):
            if "topology-archive" in str(self):
                raise OSError("disk full (simulated)")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", broken_write_text)

        result = _load(topo_delete("harbor"))

        assert "error" in result
        assert "归档失败" in result["error"]
        assert result.get("archived") is False
        assert (topo_home / "topology.yaml").read_text(encoding="utf-8") == before
        assert detail_file.is_file()  # L3 档案也未被拔
        assert _archives(topo_home) == []  # 只能有空的日期目录，不能有归档文件
        index = yaml.safe_load((topo_home / "services" / "node1.yaml").read_text(encoding="utf-8"))
        assert [s["name"] for s in index["services"]] == ["harbor", "grafana"]


# ---------------------------------------------------------------------------
# 必测 7：审计事件
# ---------------------------------------------------------------------------

class TestAuditEvent:
    def test_audit_event_written(self, topo_home, monkeypatch):
        captured = []

        def fake_record_event(**kwargs):
            captured.append(kwargs)

        import agent.trajectory as trajectory
        monkeypatch.setattr(trajectory, "record_event", fake_record_event)

        result = _load(topo_delete("node2", reason="退役"))
        assert result["status"] == "deleted"

        assert len(captured) == 1
        event = captured[0]
        assert event["type"] == "topo_delete"
        assert event["tool"] == "topology"
        assert event["action"] == "delete node2"
        assert "node2" in event["result"]
        meta = event["meta"]
        assert meta["env"] == "dev"
        assert meta["archived"] == result["archived"]
        assert meta["forced"] is False
        assert meta["referrers"] == []
        assert meta["reason"] == "退役"
        assert meta["operator"]  # getpass.getuser() 非空
        assert meta["removed"] == ["topology.yaml:hosts[node2]"]
        assert meta["approval_required"] is False

    def test_audit_failure_does_not_undo_delete(self, topo_home, monkeypatch):
        """审计 best-effort：record_event 抛错不回滚已完成的删除。"""
        import agent.trajectory as trajectory

        def boom(**kwargs):
            raise RuntimeError("trajectory down")

        monkeypatch.setattr(trajectory, "record_event", boom)

        result = _load(topo_delete("node2"))
        assert result["status"] == "deleted"
        assert [h["name"] for h in _topology(topo_home)["hosts"]] == ["node1"]


# ---------------------------------------------------------------------------
# 边界：不当级联、不当重名覆盖
# ---------------------------------------------------------------------------

class TestBoundaries:
    def test_does_not_touch_runbooks_or_credentials(self, topo_home):
        runbook = topo_home / "runbooks" / "harbor-restart.yaml"
        runbook.parent.mkdir()
        runbook.write_text("name: harbor-restart\n", encoding="utf-8")
        cred = topo_home / "credential" / "harbor.env"
        cred.parent.mkdir()
        cred.write_text("TOKEN=x\n", encoding="utf-8")

        assert _load(topo_delete("harbor"))["status"] == "deleted"

        assert runbook.is_file()
        assert cred.is_file()

    def test_same_name_service_on_other_host_is_noted(self, topo_home):
        (topo_home / "services" / "node2.yaml").write_text(
            "host: node2\nservices:\n  - {name: harbor, type: registry, env: dev}\n",
            encoding="utf-8",
        )
        result = _load(topo_delete("harbor"))
        assert result["status"] == "deleted"
        assert "note" in result
        assert "node2" in result["note"]
        node2_index = yaml.safe_load(
            (topo_home / "services" / "node2.yaml").read_text(encoding="utf-8")
        )
        assert [s["name"] for s in node2_index["services"]] == ["harbor"]

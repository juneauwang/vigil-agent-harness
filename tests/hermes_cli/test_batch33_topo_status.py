"""批三十三 T4 — 拓扑状态组装（status 在第三层 entities/*.yaml）。

覆盖：服务卡从 detail merge status；主机卡由服务推导（全健康 → running /
任一 stopped/exited/degraded → degraded / 全空 → 空）；集群卡由主机推导；
无 detail 实体不误显示；HTML 导出集群头带状态 pill。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli.subcommands.topo_export import (
    _derive_status,
    build_view,
    render_html,
)

TOPOLOGY = """\
version: 3
clusters:
  - {name: prod, env: prod}
hosts:
  - {name: node1, env: prod, cluster: prod, services_index: hosts/node1.yaml}
  - {name: node2, env: prod, cluster: prod, services_index: hosts/node2.yaml}
  - {name: node3, env: prod, cluster: prod, services_index: hosts/node3.yaml}
  - {name: noindex, env: prod, cluster: prod}
"""

NODE1 = """\
host: node1
env: prod
cluster: prod
services:
  - {name: svc-a, detail: entities/svc-a.yaml}
  - {name: svc-b, detail: entities/svc-b.yaml}
"""

NODE2 = """\
host: node2
env: prod
cluster: prod
services:
  - {name: svc-c, detail: entities/svc-c.yaml}
"""

NODE3 = """\
host: node3
env: prod
cluster: prod
services:
  - {name: svc-d, detail: entities/svc-d.yaml}
"""

# svc-a 健康 / svc-b stopped → node1 degraded
ENTITY_A = "name: svc-a\nstatus: running\n"
ENTITY_B = "name: svc-b\nstatus: stopped\n"
# svc-c running → node2 running
ENTITY_C = "name: svc-c\nstatus: healthy\n"
# svc-d 无 status → node3 全空 → 空
ENTITY_D = "name: svc-d\nendpoint: 10.0.0.1\n"


@pytest.fixture
def topo_home(tmp_path, monkeypatch):
    (tmp_path / "topology.yaml").write_text(TOPOLOGY, encoding="utf-8")
    (tmp_path / "hosts").mkdir()
    (tmp_path / "hosts" / "node1.yaml").write_text(NODE1, encoding="utf-8")
    (tmp_path / "hosts" / "node2.yaml").write_text(NODE2, encoding="utf-8")
    (tmp_path / "hosts" / "node3.yaml").write_text(NODE3, encoding="utf-8")
    (tmp_path / "entities").mkdir()
    for name, content in {
        "svc-a.yaml": ENTITY_A,
        "svc-b.yaml": ENTITY_B,
        "svc-c.yaml": ENTITY_C,
        "svc-d.yaml": ENTITY_D,
    }.items():
        (tmp_path / "entities" / name).write_text(content, encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    return tmp_path


def _by_name(view, key):
    return {item["card"]["name"]: item for item in view[key]}


def test_service_status_merged_from_detail(topo_home):
    view = build_view(topo_home)
    assert view is not None
    node1 = _by_name(view, "hosts")["node1"]
    svcs = {s["card"]["name"]: s for s in node1["services"]}
    assert svcs["svc-a"]["card"]["status"] == "running"
    assert svcs["svc-b"]["card"]["status"] == "stopped"
    # 无 status 的 detail → 保持空（不误显示）。
    node3 = _by_name(view, "hosts")["node3"]
    assert node3["services"][0]["card"]["status"] == ""


def test_host_status_derived_from_services(topo_home):
    view = build_view(topo_home)
    by_name = _by_name(view, "hosts")
    # node1：任一 stopped → degraded
    assert by_name["node1"]["card"]["status"] == "degraded"
    # node2：全健康 → running
    assert by_name["node2"]["card"]["status"] == "running"
    # node3：全部空 → 空（不显示 pill）
    assert by_name["node3"]["card"]["status"] == ""
    # 无服务数据主机 → 空
    assert by_name["noindex"]["card"]["status"] == ""


def test_cluster_status_derived_from_hosts(topo_home):
    view = build_view(topo_home)
    cluster = view["clusters"][0]
    # 主机有 degraded（node1）→ 集群 degraded
    assert cluster["status"] == "degraded"


def test_cluster_all_healthy_running(topo_home):
    # 全健康：把 svc-b 改成 running → node1 running → 集群 running
    Path(topo_home, "entities", "svc-b.yaml").write_text(
        "name: svc-b\nstatus: running\n", encoding="utf-8")
    view = build_view(topo_home)
    assert _by_name(view, "hosts")["node1"]["card"]["status"] == "running"
    assert view["clusters"][0]["status"] == "running"


def test_cluster_no_status_when_all_empty(topo_home):
    for name, content in {
        "svc-a.yaml": "name: svc-a\n",
        "svc-b.yaml": "name: svc-b\n",
        "svc-c.yaml": "name: svc-c\n",
    }.items():
        Path(topo_home, "entities", name).write_text(content, encoding="utf-8")
    view = build_view(topo_home)
    assert "status" not in view["clusters"][0]
    assert all(h["card"]["status"] == "" for h in view["hosts"])


def test_derive_status_rules():
    assert _derive_status([]) == ""
    assert _derive_status(["", "  "]) == ""
    assert _derive_status(["running", "healthy", "ok"]) == "running"
    assert _derive_status(["running", "stopped"]) == "degraded"
    assert _derive_status(["exited"]) == "degraded"
    assert _derive_status(["degraded", "running"]) == "degraded"
    assert _derive_status(["unknown"]) == "degraded"


def test_html_export_cluster_header_has_status_pill(topo_home):
    html = render_html(build_view(topo_home))
    # 集群头带状态 pill（degraded）
    assert 'class="status-pill status-default">● degraded' in html
    # 服务行 pill（running/stopped）
    assert '● running' in html
    assert '● stopped' in html

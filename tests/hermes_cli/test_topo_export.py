"""``vigil topo export --html`` 验收测试。

覆盖：三层拓扑读取（topology.yaml + hosts/*.yaml + entities/*.yaml）；HTML
渲染（集群分组/主机卡片/服务列表/cross_host/关键链路/状态着色/搜索/详情）；
**凭据零泄露**（credential/ref/user/密码/私钥路径绝不进 HTML）；数据缺失与
损坏容错（无 topology / services_index 缺失 / 坏 YAML）；CLI 注册与输出路径。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

from hermes_cli.subcommands.topo_export import (
    _display_path,
    _redact_value,
    _sanitize,
    build_view,
    render_html,
    run,
)

TOPOLOGY_YAML = """\
version: 3
updated_at: 2026-08-13
sources: [terraform.tfstate, agent, manual]
environments:
  - {name: local, isolation: relaxed, role: local}
  - {name: test, isolation: relaxed, role: test}
  - {name: dev, isolation: relaxed, role: dev}
  - {name: prod, isolation: strict, role: prod}
clusters:
  - {name: k8s-prod, env: prod, description: 生产 k3s 集群, owner: your-name}
hosts:
  - {name: node1, env: prod, cluster: k8s-prod, endpoint: "203.0.113.10", role: control-plane, runtime: k3s, status: running, services_index: hosts/node1.yaml, credential: {type: ssh_key, ref: "~/.ssh/aliyun_nopass.pem", user: root}}
  - {name: node2, env: prod, cluster: k8s-prod, endpoint: "203.0.113.11", role: worker, runtime: k3s, status: stopped}
  - {name: lonely, env: test, endpoint: "10.0.0.9", status: unknown}
cross_host:
  - {name: ingress, type: ingress, env: prod, cluster: k8s-prod, status: running, detail: entities/k8s-prod__ingress.yaml, credential: {type: askpass, ref: "/tmp/ask.sh", user: root}}
key_paths:
  - [ingress, gateway-svc, order-db]
"""

HOSTS_NODE1 = """\
host: node1
env: prod
cluster: k8s-prod
services:
  - {name: harbor, type: registry, env: prod, cluster: k8s-prod, endpoint: "203.0.113.10:30443", detail: entities/k8s-prod__node1__harbor.yaml}
  - {name: gateway-svc, type: service, env: prod, cluster: k8s-prod, endpoint: "203.0.113.10:30080", status: running}
  - {name: order-db, type: db, env: prod, cluster: k8s-prod, endpoint: "203.0.113.10:5432", status: running}
"""

HOSTS_NODE2 = """\
host: node2
env: prod
cluster: k8s-prod
services: []
"""

ENTITY_HARBOR = """\
name: harbor
type: registry
env: prod
cluster: k8s-prod
endpoint: "203.0.113.10:30443"
attrs:
  version: "v2.11"
  storage: /data/harbor
  user: dbadmin
  credential: {type: ssh_key, ref: "/home/ops/.ssh/db.pem", user: ops}
depends_on: [postgres]
ops:
  healthcheck: "curl -s http://localhost/api/v2.0/health"
"""

ENTITY_INGRESS = """\
name: ingress
type: ingress
env: prod
cluster: k8s-prod
depends_on: [gateway-svc]
ops:
  healthcheck: "curl -sI http://127.0.0.1"
"""


@pytest.fixture
def topo_home(tmp_path, monkeypatch):
    (tmp_path / "topology.yaml").write_text(TOPOLOGY_YAML, encoding="utf-8")
    (tmp_path / "hosts").mkdir()
    (tmp_path / "hosts" / "node1.yaml").write_text(HOSTS_NODE1, encoding="utf-8")
    (tmp_path / "hosts" / "node2.yaml").write_text(HOSTS_NODE2, encoding="utf-8")
    (tmp_path / "entities").mkdir()
    (tmp_path / "entities" / "k8s-prod__node1__harbor.yaml").write_text(
        ENTITY_HARBOR, encoding="utf-8")
    (tmp_path / "entities" / "k8s-prod__ingress.yaml").write_text(
        ENTITY_INGRESS, encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    return tmp_path


def _args(**overrides):
    base = dict(output=None, html=True)
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# build_view —— 数据读取
# ---------------------------------------------------------------------------

def test_build_view_structure(topo_home):
    view = build_view(topo_home)
    assert view is not None
    assert [c["name"] for c in view["clusters"]] == ["k8s-prod"]
    assert view["key_paths"] == [["ingress", "gateway-svc", "order-db"]]
    assert set(view["key_path_entity_names"]) == {"ingress", "gateway-svc", "order-db"}

    by_name = {h["card"]["name"]: h for h in view["hosts"]}
    assert set(by_name) == {"node1", "node2", "lonely"}
    node1 = by_name["node1"]
    assert node1["card"]["endpoint"] == "203.0.113.10"
    assert node1["card"]["role"] == "control-plane"
    assert node1["card"]["runtime"] == "k3s"
    assert node1["card"]["status"] == "running"
    assert not node1["services_missing"]
    svc_names = [s["card"]["name"] for s in node1["services"]]
    assert svc_names == ["harbor", "gateway-svc", "order-db"]
    gateway = next(s for s in node1["services"] if s["card"]["name"] == "gateway-svc")
    assert gateway["card"]["on_key_path"] is True
    assert node1["card"]["on_key_path"] is False
    # 关键链路上的服务带详情（harbor 有第三层档案）。
    harbor = next(s for s in node1["services"] if s["card"]["name"] == "harbor")
    assert harbor["detail"] is not None

    # lonely：无 services_index → services_missing，cluster 归 default。
    assert by_name["lonely"]["services_missing"] is True
    assert by_name["lonely"]["card"]["cluster"] == "default"
    assert not by_name["lonely"]["services"]

    cross_names = [c["card"]["name"] for c in view["cross_host"]]
    assert cross_names == ["ingress"]
    assert view["cross_host"][0]["card"]["on_key_path"] is True


def test_build_view_no_topology_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    assert build_view(tmp_path) is None
    # 拓扑文件存在但 hosts/cross_host/clusters 全空 → 视为无数据。
    (tmp_path / "topology.yaml").write_text("version: 3\nhosts: []\n", encoding="utf-8")
    assert build_view(tmp_path) is None


def test_build_view_broken_host_index_no_crash(topo_home):
    (topo_home / "hosts" / "node1.yaml").write_text("{{{{ bad", encoding="utf-8")
    view = build_view(topo_home)
    by_name = {h["card"]["name"]: h for h in view["hosts"]}
    assert by_name["node1"]["services_missing"] is True
    assert by_name["node1"]["services"] == []


def test_build_view_v01_core_entities_compat(tmp_path, monkeypatch):
    (tmp_path / "topology.yaml").write_text(
        "version: 1\n"
        "core_entities:\n"
        "  - {name: harbor, type: registry, env: prod}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    view = build_view(tmp_path)
    assert view is not None
    assert view["hosts"][0]["card"]["name"] == "harbor"
    assert view["hosts"][0]["card"]["kind"] == "entity"


# ---------------------------------------------------------------------------
# _sanitize / _redact_value —— 凭据过滤
# ---------------------------------------------------------------------------

def test_sanitize_strips_nested_credentials():
    data = {
        "name": "node1",
        "endpoint": "10.0.0.5",
        "credential": {"type": "ssh_key", "ref": "~/.ssh/aliyun_nopass.pem", "user": "root"},
        "attrs": {"version": "16", "user": "dbadmin",
                  "credential": {"ref": "/home/ops/.ssh/db.pem", "user": "ops"}},
        "ssh": {"user": "dbadmin", "credential": {"type": "ssh_key", "ref": "/k/x.pem"}},
        "password": "hunter2",
        "key_paths": [["ingress", "gateway-svc"]],
    }
    out = _sanitize(data)
    assert "credential" not in out
    assert "user" not in out["attrs"]
    assert "password" not in out
    assert "ssh" in out and out["ssh"] == {}  # ssh 段子键被清空
    assert out["key_paths"] == [["ingress", "gateway-svc"]]  # 关键链路保留
    assert out["name"] == "node1"


def test_redact_value_masks_key_material():
    assert _redact_value("~/.ssh/aliyun_nopass.pem") == "[已过滤]"
    assert _redact_value("ref /home/ops/.ssh/db.pem end") == "ref [已过滤] end"
    assert _redact_value("key id_rsa") == "key [已过滤]"
    assert _redact_value("url ssh://root:secret123@10.0.0.5:22") == "url ssh[已过滤]10.0.0.5:22"
    assert _redact_value("203.0.113.10:30443") == "203.0.113.10:30443"  # 正常 endpoint 不动


def test_redact_value_masks_home_prefix(tmp_path, monkeypatch):
    """值级兜底：绝对 home 前缀 → ~（分享脱敏）；非 home 前缀的相似路径不动。"""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert _redact_value(str(fake_home / ".vigil" / "data")) == "~/.vigil/data"
    # 边界：仅 / 或串尾视为 home 前缀边界；空格/相似前缀不替换（防 /home/u2 误伤）。
    assert _redact_value(f"{fake_home} 下") == f"{fake_home} 下"
    assert _redact_value(str(tmp_path / "home2" / "x")) == str(tmp_path / "home2" / "x")


def test_display_path_masks_home(tmp_path, monkeypatch):
    """数据根显示脱敏：home 下 → ~ 相对；不在 home 下原样显示。"""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert _display_path(fake_home / ".vigil") == "~/.vigil"
    assert _display_path(fake_home) == "~"
    assert _display_path(fake_home / "topology.yaml") == "~/topology.yaml"
    assert _display_path(tmp_path / "elsewhere") == str(tmp_path / "elsewhere")
    # 边界：/…/home2 不是 /…/home 的子路径，不替换。
    assert _display_path(tmp_path / "home2" / "x") == str(tmp_path / "home2" / "x")


# ---------------------------------------------------------------------------
# render_html —— 渲染
# ---------------------------------------------------------------------------

def test_render_html_sections(topo_home):
    view = build_view(topo_home)
    page = render_html(view)
    assert page.startswith("<!DOCTYPE html>")
    assert "Vigil 拓扑视图" in page
    assert "集群 k8s-prod" in page
    assert "node1" in page and "node2" in page and "lonely" in page
    assert "harbor" in page and "registry" in page
    assert "203.0.113.10:30443" in page
    assert "跨主机实体" in page and "ingress" in page
    assert "关键链路" in page
    assert "gateway-svc" in page and "order-db" in page
    assert "topo-search" in page  # 搜索框
    assert "status-running" in page  # 状态着色
    assert "status-stopped" in page
    assert "topo-details" in page  # 内联详情 JSON
    assert "生成时间" in page
    assert "数据根" in page


def test_render_html_missing_services_fallback(topo_home):
    view = build_view(topo_home)
    page = render_html(view)
    assert "无服务数据" in page  # lonely（无 services_index）与 node2（空索引）


def test_render_html_escapes_entity_names(topo_home):
    # 恶意实体名/详情值必须被转义，不能形成原始 HTML 标签。
    (topo_home / "topology.yaml").write_text(
        TOPOLOGY_YAML.replace(
            'name: node1,', 'name: "<img src=x onerror=alert(1)>",'
        ).replace('services_index: hosts/node1.yaml', 'services_index: hosts/node1.yaml'),
        encoding="utf-8",
    )
    view = build_view(topo_home)
    page = render_html(view)
    assert "<img src=x onerror=alert(1)>" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page


def test_render_html_details_json_safe(topo_home):
    (topo_home / "entities" / "k8s-prod__node1__harbor.yaml").write_text(
        ENTITY_HARBOR + 'xss: "<script>alert(1)</script>"\n', encoding="utf-8")
    view = build_view(topo_home)
    page = render_html(view)
    assert "<script>alert(1)</script>" not in page


def test_render_html_masks_data_root_home(tmp_path, monkeypatch):
    """分享脱敏：数据根在 home 下 → 页面只显示 ~/.vigil，绝对路径（含用户名）不进 HTML。"""
    fake_home = tmp_path / "home"
    data_root = fake_home / ".vigil"
    data_root.mkdir(parents=True)
    (data_root / "topology.yaml").write_text(TOPOLOGY_YAML, encoding="utf-8")
    (data_root / "hosts").mkdir()
    (data_root / "hosts" / "node1.yaml").write_text(HOSTS_NODE1, encoding="utf-8")
    (data_root / "hosts" / "node2.yaml").write_text(HOSTS_NODE2, encoding="utf-8")
    (data_root / "entities").mkdir()
    (data_root / "entities" / "k8s-prod__node1__harbor.yaml").write_text(
        ENTITY_HARBOR, encoding="utf-8")
    (data_root / "entities" / "k8s-prod__ingress.yaml").write_text(
        ENTITY_INGRESS, encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    view = build_view(data_root)
    assert view is not None
    page = render_html(view)
    assert "~/.vigil" in page
    assert str(fake_home) not in page      # 绝对 home 路径（含用户名）不进 HTML
    assert str(data_root) not in page
    # 自定义 VIGIL_HOME（不在 home 下）原样显示：tmp_path 里的数据根不受影响。
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert _display_path(other) == str(other)


# ---------------------------------------------------------------------------
# 凭据零泄露（验收核心）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("leak", [
    "aliyun_nopass",          # credential.ref 私钥文件名
    ".pem",                   # 私钥扩展名
    "~/.ssh",                 # 私钥路径
    "db.pem",                 # 第三层 attrs 内嵌 credential.ref
    "/home/ops",              # attrs credential 的用户主目录
    "dbadmin",                # attrs/ssh 段用户名
    "ask.sh",                 # cross_host credential.askpass ref
    "/tmp/ask.sh",
    "hunter2",                # password 字段
    "credential",             # credential 键本身
])
def test_sensitive_values_never_in_html(topo_home, leak):
    (topo_home / "entities" / "k8s-prod__node1__harbor.yaml").write_text(
        ENTITY_HARBOR, encoding="utf-8")
    view = build_view(topo_home)
    assert view is not None
    page = render_html(view)
    assert leak not in page, f"凭据泄露：{leak!r} 出现在 HTML 中"


def test_run_export_output_leak_free(topo_home, capsys):
    """完整 run() 链路：写出的文件全文无凭据样值。"""
    rc = run(_args())
    out_path = topo_home / "topology-view.html"
    assert rc == 0
    assert out_path.is_file()
    content = out_path.read_text(encoding="utf-8")
    for leak in ("aliyun_nopass", ".pem", "~/.ssh", "dbadmin", "credential", "/tmp/ask.sh"):
        assert leak not in content


# ---------------------------------------------------------------------------
# run() —— CLI 行为
# ---------------------------------------------------------------------------

def test_run_no_topology_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    rc = run(_args())
    assert rc == 2
    err = capsys.readouterr().err
    assert "先运行 vigil topo-discover 发现主机" in err
    assert not (tmp_path / "topology-view.html").exists()


def test_run_exports_default_path(topo_home, capsys):
    rc = run(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert (topo_home / "topology-view.html").is_file()
    assert "topology-view.html" in out


def test_run_exports_custom_output(topo_home, tmp_path, capsys):
    target = tmp_path / "views" / "my-topo.html"
    rc = run(_args(output=str(target)))
    assert rc == 0
    assert target.is_file()
    assert "node1" in target.read_text(encoding="utf-8")
    assert not (topo_home / "topology-view.html").exists()


def test_topo_export_via_cli_e2e(topo_home):
    """E2E：`vigil topo export --html` 经真实 CLI 解析并写出文件。"""
    target = topo_home / "e2e.html"
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "topo", "export", "--html",
         "-o", str(target)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "VIGIL_HOME": str(topo_home)},
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert target.is_file()
    page = target.read_text(encoding="utf-8")
    assert "集群 k8s-prod" in page and "node1" in page and "跨主机实体" in page


def test_topo_help_shows_export():
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "topo", "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert "export" in proc.stdout
    assert "topo export" in proc.stdout

"""Raw YAML 编辑器 API（task27 PART A）验收测试。

覆盖：``GET/PUT /api/runbooks/{name}/raw`` 与
``GET/PUT /api/topology/entities/{entityId}/raw``——RAW round-trip
（注释/顺序字节级保真）、校验门（语法 422 + 校验器 422 带行号，不落盘）、
路径白名单（穿越/符号链接逃逸/白名单外/非 .yaml 拒绝）、UTF-8 门、
鉴权（无 token → 401）、审计（yaml_raw_save）、schedule/alert_auto_run
保存后的自动执行豁免失效告警、原子写保留权限位。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

TOPOLOGY_YAML = """\
version: 3
sources: [terraform.tfstate, agent, manual]
environments:
  - {name: prod, isolation: strict, role: prod}
clusters:
  - {name: k8s-prod, env: prod, description: 生产 k3s 集群, owner: your-name}
hosts:
  - {name: node1, env: prod, cluster: k8s-prod, endpoint: "203.0.113.10", role: control-plane, runtime: k3s, status: running, services_index: hosts/node1.yaml}
cross_host: []
"""

HOSTS_NODE1 = """\
host: node1
env: prod
cluster: k8s-prod
services:
  - {name: harbor, type: registry, env: prod, endpoint: "203.0.113.10:30443", detail: entities/k8s-prod__node1__harbor.yaml}
"""

ENTITY_HARBOR = """\
# 实体档案：harbor（registry）
name: harbor
type: registry
env: prod
attrs:
  version: "v2.11"
ops:
  healthcheck: "curl -s http://localhost/api/v2.0/health"
"""

RUNBOOK = """\
# 手工维护的 runbook——注释必须原样保留
name: harbor-restart
title: "Harbor 服务异常恢复"
version: 1
env: prod
kind: incident
triggers:
  - "harbor healthcheck failed"
summary: "harbor 健康检查失败时的标准恢复流程"
steps:
  - id: diagnose
    title: 诊断
    commands:
      - "docker ps --filter name=harbor"
  - id: verify
    title: 真实验证
    verify: "curl -s http://localhost/api/v2.0/health"
    expect: '"status":"healthy"'
rollback:
  - title: 兜底
    commands:
      - "docker logs --tail 100 harbor"
"""

RUNBOOK_V2_AUTO = """\
name: db-failover
title: "数据库主备切换"
version: 2
kind: incident
env: prod
triggers:
  - "db master down"
alert_auto_run: true
alert_auto_severity: [critical]
steps:
  - id: check
    title: 检查复制状态
    action: query
    params:
      target: harbor
"""

RUNBOOK_V2_PLAIN = """\
name: cache-flush
title: "缓存清理"
version: 2
kind: maintenance
env: test
triggers:
  - "cache stale"
steps:
  - id: probe
    title: 探测
    action: query
    params:
      target: harbor
"""


@pytest.fixture
def ops_home(tmp_path, monkeypatch):
    (tmp_path / "topology.yaml").write_text(TOPOLOGY_YAML, encoding="utf-8")
    (tmp_path / "hosts").mkdir()
    (tmp_path / "hosts" / "node1.yaml").write_text(HOSTS_NODE1, encoding="utf-8")
    (tmp_path / "entities").mkdir()
    (tmp_path / "entities" / "k8s-prod__node1__harbor.yaml").write_text(
        ENTITY_HARBOR, encoding="utf-8")
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks" / "harbor-restart.yaml").write_text(RUNBOOK, encoding="utf-8")
    (tmp_path / "runbooks" / "db-failover.yaml").write_text(
        RUNBOOK_V2_AUTO, encoding="utf-8")
    (tmp_path / "runbooks" / "cache-flush.yaml").write_text(
        RUNBOOK_V2_PLAIN, encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(ops_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return test_client


@pytest.fixture
def bare_client(ops_home):
    """不带 session token 的客户端（401 场景）。"""
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import app

    return TestClient(app)


@pytest.fixture
def events(monkeypatch):
    seen = []
    monkeypatch.setattr("agent.trajectory.record_event",
                        lambda **kw: seen.append(kw))
    return seen


# ---------------------------------------------------------------------------
# runbook raw — GET / RAW round-trip
# ---------------------------------------------------------------------------

def test_runbook_raw_get_returns_original_text(ops_home, client):
    """GET 返回磁盘原文：注释/顺序字节级一致，content-type text/yaml。"""
    resp = client.get("/api/runbooks/harbor-restart/raw")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/yaml")
    assert resp.text == RUNBOOK  # byte-for-byte，注释原样


def test_runbook_raw_put_roundtrip_comment_survives(ops_home, client):
    """RAW round-trip 证明：PUT 的原文（含注释行）→ GET 读回字节一致。"""
    edited = RUNBOOK.replace(
        'title: "Harbor 服务异常恢复"',
        'title: "Harbor 服务异常恢复（手工调参版）"  # 行尾注释也保留',
    )
    resp = client.put(
        "/api/runbooks/harbor-restart/raw", content=edited.encode("utf-8"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    disk = (ops_home / "runbooks" / "harbor-restart.yaml").read_text(
        encoding="utf-8")
    assert disk == edited  # 落盘 = 用户原文（绝不 parse→dump 回写）

    back = client.get("/api/runbooks/harbor-restart/raw")
    assert back.status_code == 200
    assert back.text == edited  # GET→PUT→GET 字节一致；注释存活


def test_runbook_raw_put_valid_updates_file_and_audits(ops_home, client, events):
    edited = RUNBOOK.replace("harbor 健康检查失败", "harbor 探活失败")
    resp = client.put(
        "/api/runbooks/harbor-restart/raw", content=edited.encode("utf-8"))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "warnings": []}
    assert "harbor 探活失败" in (ops_home / "runbooks" / "harbor-restart.yaml").read_text(
        encoding="utf-8")

    assert events, "成功保存必须写审计"
    ev = events[-1]
    assert ev["type"] == "yaml_raw_save"
    assert ev["meta"]["source"] == "ui"
    assert ev["meta"]["file"] == "runbooks/harbor-restart.yaml"
    assert ev["meta"]["before_lines"] > 0 and ev["meta"]["after_lines"] > 0


def test_runbook_raw_put_invalid_yaml_422_with_line(ops_home, client):
    broken = RUNBOOK + "steps:\n  - [unclosed\n"
    resp = client.put(
        "/api/runbooks/harbor-restart/raw", content=broken.encode("utf-8"))
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    err = body["errors"][0]
    assert isinstance(err["line"], int) and err["line"] >= 1
    assert err["message"]
    # 不落盘
    assert (ops_home / "runbooks" / "harbor-restart.yaml").read_text(
        encoding="utf-8") == RUNBOOK


def test_runbook_raw_put_validator_failure_maps_line(ops_home, client):
    """name 与文件名不一致 → 校验器 422；行号 best-effort 映射到 name 行。"""
    edited = RUNBOOK.replace("name: harbor-restart", "name: other-name", 1)
    resp = client.put(
        "/api/runbooks/harbor-restart/raw", content=edited.encode("utf-8"))
    assert resp.status_code == 422
    body = resp.json()
    err = body["errors"][0]
    assert "不一致" in err["message"]
    assert err["line"] == 2  # name: 在原文第 2 行（第 1 行是注释）
    assert (ops_home / "runbooks" / "harbor-restart.yaml").read_text(
        encoding="utf-8") == RUNBOOK


def test_runbook_raw_put_top_level_scalar_422(ops_home, client):
    resp = client.put("/api/runbooks/harbor-restart/raw", content=b"- just\n- a list\n")
    assert resp.status_code == 422
    assert "映射" in resp.json()["errors"][0]["message"]


# ---------------------------------------------------------------------------
# runbook raw — 路径白名单 / 编码 / 权限位 / 404 / 401
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["..", "..%2Ftopology", "a%2Fb", ".%2E/topology"])
def test_runbook_raw_traversal_rejected(ops_home, client, bad):
    """穿越/白名单外名称：不可读不可写。%2E%2E（解码为 ..）到达 handler →
    干净 404；含 %2F 的形式被 httpx/SPA 静态挂载拦在路由外（404/405 视
    框架而定），但两种形态都绝不落盘。"""
    for method, url in (("get", f"/api/runbooks/{bad}/raw"),
                        ("put", f"/api/runbooks/{bad}/raw")):
        resp = (client.get(url) if method == "get"
                else client.put(url, content=b"name: x\n"))
        assert resp.status_code in (404, 405), f"{url} -> {resp.status_code}"
    # 白名单外文件分毫未动
    assert (ops_home / "topology.yaml").read_text(encoding="utf-8") == TOPOLOGY_YAML


def test_runbook_raw_encoded_dot_dot_rejected_by_handler(ops_home, client):
    """%2E%2E 形式穿过客户端/框架到达 handler：路由内 clean 404（不创建、
    不读取、不写入白名单外任何文件）。"""
    assert client.get("/api/runbooks/%2E%2E/raw").status_code == 404
    assert client.put("/api/runbooks/%2E%2E/raw",
                      content=b"name: x\n").status_code == 404
    # 白名单内不存在的名字同样 404（不从编辑器创建文件）
    assert client.get("/api/runbooks/topology/raw").status_code == 404


def test_runbook_raw_symlink_escape_rejected(ops_home, client):
    """白名单目录内的符号链接指向目录外 → 拒绝（resolve 越界）。"""
    link = ops_home / "runbooks" / "evil.yaml"
    link.symlink_to(ops_home / "topology.yaml")
    assert client.get("/api/runbooks/evil/raw").status_code == 404
    assert client.put("/api/runbooks/evil/raw",
                      content=b"name: x\n").status_code == 404
    assert (ops_home / "topology.yaml").read_text(encoding="utf-8") == TOPOLOGY_YAML


def test_runbook_raw_missing_404(ops_home, client):
    assert client.get("/api/runbooks/nope/raw").status_code == 404
    assert client.put("/api/runbooks/nope/raw",
                      content=b"name: nope\n").status_code == 404


def test_runbook_raw_requires_token(ops_home, bare_client):
    """/raw 不在公开只读豁免内：无 token 一律 401（GET 与 PUT 都是）。"""
    assert bare_client.get("/api/runbooks/harbor-restart/raw").status_code == 401
    assert bare_client.put("/api/runbooks/harbor-restart/raw",
                           content=RUNBOOK.encode("utf-8")).status_code == 401
    assert bare_client.get(
        "/api/topology/entities/service:node1:harbor/raw").status_code == 401
    assert bare_client.put(
        "/api/topology/entities/service:node1:harbor/raw",
        content=ENTITY_HARBOR.encode("utf-8")).status_code == 401


def test_runbook_raw_put_rejects_binary(ops_home, client):
    resp = client.put("/api/runbooks/harbor-restart/raw",
                      content=b"\x00\x01\x02\xff\xfe")
    assert resp.status_code == 400
    assert (ops_home / "runbooks" / "harbor-restart.yaml").read_text(
        encoding="utf-8") == RUNBOOK


def test_runbook_raw_put_size_limit(ops_home, client, monkeypatch):
    import hermes_cli.web_server as ws
    monkeypatch.setattr(ws, "_YAML_RAW_MAX_BYTES", 16)
    resp = client.put("/api/runbooks/harbor-restart/raw",
                      content=("x" * 64).encode("utf-8"))
    assert resp.status_code == 413


def test_runbook_raw_put_preserves_file_mode(ops_home, client):
    path = ops_home / "runbooks" / "harbor-restart.yaml"
    os.chmod(path, 0o600)
    try:
        resp = client.put("/api/runbooks/harbor-restart/raw",
                          content=RUNBOOK.encode("utf-8"))
        assert resp.status_code == 200
        assert (stat_mode(path) & 0o777) == 0o600
    finally:
        os.chmod(path, 0o644)


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode


# ---------------------------------------------------------------------------
# runbook raw — 自动执行豁免失效告警 + schedule cron 同步
# ---------------------------------------------------------------------------

def test_runbook_raw_put_alert_auto_run_save_flags_reapproval(ops_home, client):
    """alert_auto_run runbook 保存 → 200 成功但 warnings 携带豁免失效告警
    （引擎 fail-closed：内容哈希漂移 / 缺预审标记 → 需重新过资产审批）。"""
    edited = RUNBOOK_V2_AUTO.replace("数据库主备切换", "数据库主备切换 v2")
    resp = client.put("/api/runbooks/db-failover/raw",
                      content=edited.encode("utf-8"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["warnings"], "自动执行授权 runbook 必须携带告警"
    warning = body["warnings"][0]
    assert warning["code"] == "auto_exec_approval_invalidated"
    assert warning["message"]
    # 无预审标记 → 引擎拒绝豁免；文件已落盘（人工作者语义，不阻断）
    assert "数据库主备切换 v2" in (ops_home / "runbooks" / "db-failover.yaml").read_text(
        encoding="utf-8")


def test_runbook_raw_put_plain_v2_no_warning(ops_home, client):
    """无 schedule / alert_auto_run 的 runbook → 保存无告警。"""
    edited = RUNBOOK_V2_PLAIN.replace("缓存清理", "缓存清理 v2")
    resp = client.put("/api/runbooks/cache-flush/raw",
                      content=edited.encode("utf-8"))
    assert resp.status_code == 200
    assert resp.json()["warnings"] == []


def test_runbook_raw_put_schedule_resyncs_cron(ops_home, client, monkeypatch):
    """带 schedule 的 runbook 保存后重新同步 cron 注册（与 runbook_create
    落盘后同一路径）——热生效约束的调度侧。"""
    from tools.runbook_tools import _validate_runbook

    base = RUNBOOK_V2_PLAIN.replace("kind: maintenance", "kind: checklist")
    base = base.replace("triggers:\n  - \"cache stale\"\n", "")
    base = base.replace(
        "alert_auto_run: true\n", "")  # plain 无此行；保险起见幂等替换
    base = base.replace(
        "title: \"缓存清理\"",
        "title: \"缓存清理\"\nschedule:\n  cron: \"0 9 * * *\"\n  timezone: \"Asia/Shanghai\"",
    )
    _validate_runbook(__import__("yaml").safe_load(base), "cache-flush", ops_home)
    (ops_home / "runbooks" / "cache-flush.yaml").write_text(base, encoding="utf-8")

    calls = []
    monkeypatch.setattr("tools.runbook_schedule.register_runbook_schedule",
                        lambda name, schedule, home=None: calls.append((name, schedule)))
    resp = client.put("/api/runbooks/cache-flush/raw",
                      content=base.replace("缓存清理", "缓存清理 v3").encode("utf-8"))
    assert resp.status_code == 200, resp.text
    assert calls and calls[0][0] == "cache-flush"
    assert calls[0][1] == {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"}


# ---------------------------------------------------------------------------
# topology entity raw
# ---------------------------------------------------------------------------

def test_entity_raw_get_service_archive(ops_home, client):
    """service:<host>:<name> → 实体档案原文（entities/*.yaml）。"""
    resp = client.get("/api/topology/entities/service:node1:harbor/raw")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/yaml")
    assert resp.text == ENTITY_HARBOR  # 注释原样


def test_entity_raw_get_host_without_archive_404(ops_home, client):
    """host 节点事实源 = entities/<name>.yaml；档案不存在 → 404（不从编辑器创建）。"""
    assert client.get("/api/topology/entities/host:node1/raw").status_code == 404


def test_entity_raw_get_cluster_maps_to_topology_yaml(ops_home, client):
    """cluster:<name> → topology.yaml 整文件（集群行是其一行的切片，RAW
    round-trip 语义下整文件才是完整事实源单元）。"""
    resp = client.get("/api/topology/entities/cluster:k8s-prod/raw")
    assert resp.status_code == 200
    assert resp.text == TOPOLOGY_YAML
    # 未知集群 → 404
    assert client.get("/api/topology/entities/cluster:nope/raw").status_code == 404


def test_entity_raw_unknown_entity_404(ops_home, client):
    for eid in ("service:node1:nope", "host:nope", "cross_host:ghost", "bogus"):
        assert client.get(f"/api/topology/entities/{eid}/raw").status_code == 404
        assert client.put(f"/api/topology/entities/{eid}/raw",
                          content=b"name: x\n").status_code == 404


def test_entity_raw_put_updates_archive_and_audits(ops_home, client, events):
    edited = ENTITY_HARBOR.replace('version: "v2.11"', 'version: "v2.12"')
    resp = client.put("/api/topology/entities/service:node1:harbor/raw",
                      content=edited.encode("utf-8"))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    path = ops_home / "entities" / "k8s-prod__node1__harbor.yaml"
    assert path.read_text(encoding="utf-8") == edited
    # 读回端（topo 加载路径）能看到新值
    from tools.topo_tools import load_topology, _load_host_index
    topo = load_topology(ops_home)
    index = _load_host_index(ops_home, topo["hosts"][0])
    assert index["services"][0]["name"] == "harbor"

    ev = events[-1]
    assert ev["type"] == "yaml_raw_save"
    assert ev["meta"]["file"] == "entities/k8s-prod__node1__harbor.yaml"
    assert ev["action"].startswith("service:")


def test_entity_raw_put_topology_bad_section_422(ops_home, client):
    """topology.yaml 段类型破坏（cross_host 成映射）→ 422 带行号；不落盘。"""
    broken = TOPOLOGY_YAML.replace("cross_host: []", "cross_host: {a: b}")
    resp = client.put("/api/topology/entities/cluster:k8s-prod/raw",
                      content=broken.encode("utf-8"))
    assert resp.status_code == 422
    body = resp.json()
    err = body["errors"][0]
    assert "cross_host" in err["message"]
    assert isinstance(err["line"], int) and err["line"] >= 1
    assert (ops_home / "topology.yaml").read_text(encoding="utf-8") == TOPOLOGY_YAML


def test_entity_raw_put_syntax_error_422(ops_home, client):
    resp = client.put("/api/topology/entities/cluster:k8s-prod/raw",
                      content=b"clusters: [broken\n")
    assert resp.status_code == 422
    err = resp.json()["errors"][0]
    assert isinstance(err["line"], int)


def test_entity_raw_put_binary_rejected(ops_home, client):
    resp = client.put("/api/topology/entities/service:node1:harbor/raw",
                      content=b"\xff\xfe\x00binary")
    assert resp.status_code == 400
    assert (ops_home / "entities" / "k8s-prod__node1__harbor.yaml").read_text(
        encoding="utf-8") == ENTITY_HARBOR


def test_entity_raw_put_roundtrip_comment_survives(ops_home, client):
    edited = ENTITY_HARBOR.replace(
        "env: prod",
        "env: prod  # 修改行内注释仍在",
    )
    resp = client.put("/api/topology/entities/service:node1:harbor/raw",
                      content=edited.encode("utf-8"))
    assert resp.status_code == 200
    back = client.get("/api/topology/entities/service:node1:harbor/raw")
    assert back.text == edited

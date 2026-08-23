"""批八十：runbook 长任务进度主动播报——进度事件流 + SSE 端点。

覆盖：引擎事件序列（成功 step_start→step_done→runbook_done；失败
step_failed→rollback_start→rollback_done→runbook_done）、回调异常不阻断、
ledger 带 exec_id、POST 立即返回 exec_id、SSE 端点（事件推送/终态收尾/
未知 exec_id）、执行历史 data.running（进行中可见）。
"""

from __future__ import annotations

import json
import threading
import time

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

from hermes_cli import web_server


@pytest.fixture
def ehome(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME + 拓扑 fixture（nginx 服务）+ 矩阵 t1（query=execute）。"""
    home = tmp_path / "vigil_home"
    home.mkdir(parents=True)
    (home / "services").mkdir(parents=True)
    (home / "entities").mkdir(parents=True)
    (home / "runbooks").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    (home / "topology.yaml").write_text("""
version: 4
environments:
- name: local
clusters:
- name: local
  type: docker
  env: local
  host_groups: []
hosts:
- name: LAPTOP-T2JA2ERE
  type: host
  env: local
  cluster: local
  endpoint: 172.18.120.67
  os: Ubuntu 24.04.3 LTS
  credentials: []
""", encoding="utf-8")
    (home / "services" / "LAPTOP-T2JA2ERE.yaml").write_text("""
host: LAPTOP-T2JA2ERE
services:
- name: nginx
  type: gateway
  managed_by: docker_compose
  source: manual
  detail: entities/nginx.yaml
""", encoding="utf-8")
    (home / "entities" / "nginx.yaml").write_text("""
name: nginx
snapshot:
  by_runtime:
    docker_compose:
      project: docker
      services:
      - name: docker-nginx-1
  common:
    config_dir: /etc/nginx
""", encoding="utf-8")
    from tools.matrix_data import template_matrix, write_matrix
    write_matrix(template_matrix("template1"), home)
    (home / "runbooks" / "t-ok.yaml").write_text("""
name: t-ok
title: T OK
version: 2
kind: maintenance
env: local
on_failure: stop
steps:
- id: q1
  title: 查询一
  action: query
  params:
    target: nginx
    pattern: x
- id: q2
  title: 查询二
  action: query
  params:
    target: nginx
    pattern: y
""", encoding="utf-8")
    (home / "runbooks" / "t-rb.yaml").write_text("""
name: t-rb
title: T RB
version: 2
kind: maintenance
env: local
on_failure: stop
steps:
- id: boom
  title: 会失败
  action: query
  params:
    target: nginx
    pattern: z
  on_failure:
    rollback: rb-main
rollback:
- name: rb-main
  steps:
  - id: rb-restore
    title: 恢复
    action: query
    params:
      target: nginx
      pattern: restore
""", encoding="utf-8")
    return home


@pytest.fixture
def client():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    with TestClient(web_server.app) as test_client:
        test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        web_server._RUNBOOK_STREAMS.clear()
        yield test_client
    web_server._RUNBOOK_STREAMS.clear()


def _ok_runner():
    def _run(spec, target):
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}
    return _run


def _bad_runner():
    def _run(spec, target):
        return {"exit_code": 1, "stdout": "", "stderr": "boom"}
    return _run


def _boom_then_ok_runner():
    """仅 pattern=z 的步骤失败（回滚步骤成功 → rolled_back）。"""
    def _run(spec, target):
        argv = " ".join(spec.get("argv") or [str(spec.get("cmd") or "")])
        if "z" in argv:
            return {"exit_code": 1, "stdout": "", "stderr": "boom"}
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}
    return _run


# ---------------------------------------------------------------------------
# 引擎事件序列
# ---------------------------------------------------------------------------

def test_engine_success_event_sequence(ehome):
    from tools.runbook_exec import execute_runbook
    from tools.runbook_tools import _load_runbook

    events = []
    res = execute_runbook(
        _load_runbook(ehome, "t-ok"), env="local", home=ehome,
        runner=_ok_runner(), exec_id="exec_test_1",
        progress_callback=events.append,
    )
    assert res["result"] == "ok"
    types = [e["type"] for e in events]
    assert types == ["step_start", "step_done", "step_start", "step_done",
                     "runbook_done"]
    done = events[-1]
    assert done["status"] == "ok"
    assert done["exec_id"] == "exec_test_1"
    assert done["runbook"] == "t-ok"
    assert done["version"] == "2"
    assert done["step_count"] == 2
    # 事件字段契约：step_id/title/action/target/status/ts
    start = events[0]
    assert start["type"] == "step_start"
    assert start["step_id"] == "q1"
    assert start["title"] == "查询一"
    assert start["action"] == "query"
    assert start["target"] == "nginx"
    assert start["status"] == "running"
    assert start["ts"]
    assert events[1]["status"] == "ok"


def test_engine_rollback_event_sequence(ehome):
    from tools.runbook_exec import execute_runbook
    from tools.runbook_tools import _load_runbook

    events = []
    res = execute_runbook(
        _load_runbook(ehome, "t-rb"), env="local", home=ehome,
        runner=_boom_then_ok_runner(), exec_id="exec_test_2",
        progress_callback=events.append,
    )
    assert res["result"] == "rolled_back"
    types = [e["type"] for e in events]
    assert types == [
        "step_start", "step_failed",
        "rollback_start",
        "step_start", "step_done",   # 回滚步骤（phase=rollback）
        "rollback_done",
        "runbook_done",
    ]
    failed = events[1]
    assert failed["status"] == "failed"
    assert "boom" in failed["detail"]
    rb_start = events[2]
    assert rb_start["status"] == "running"
    assert rb_start["step_id"] == "boom"
    rb_step = events[3]
    assert rb_step["phase"] == "rollback"
    assert rb_step["step_id"] == "rb-restore"
    assert events[-1]["status"] == "rolled_back"
    assert events[-1]["rolled_back"] is True


def test_engine_callback_exception_non_blocking(ehome):
    from tools.runbook_exec import execute_runbook
    from tools.runbook_tools import _load_runbook

    def _boom_cb(event):
        raise RuntimeError("cb down")

    res = execute_runbook(
        _load_runbook(ehome, "t-ok"), env="local", home=ehome,
        runner=_ok_runner(), exec_id="exec_test_3",
        progress_callback=_boom_cb,
    )
    assert res["result"] == "ok"


def test_engine_ledger_carries_exec_id(ehome):
    from tools.runbook_exec import execute_runbook, recent_executions
    from tools.runbook_tools import _load_runbook

    execute_runbook(
        _load_runbook(ehome, "t-ok"), env="local", home=ehome,
        runner=_ok_runner(), exec_id="exec_test_ledger",
    )
    rows = recent_executions(ehome)
    assert rows and rows[0]["exec_id"] == "exec_test_ledger"


# ---------------------------------------------------------------------------
# SSE 端点 + POST 立即返回 + 历史 running
# ---------------------------------------------------------------------------

def _post_run(client, name):
    resp = client.post("/api/runbook/executions", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _sse_events(client, exec_id):
    events = []
    with client.stream("GET", f"/api/runbook/executions/{exec_id}/progress") as resp:
        assert resp.status_code == 200
        ev = None
        data_lines = []
        done = False
        for line in resp.iter_lines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "" and ev is not None:
                events.append((ev, json.loads("\n".join(data_lines))))
                ev = None
                data_lines = []
                if events[-1][1].get("type") == "runbook_done":
                    done = True
                    break
        assert done
    return events


def test_post_returns_exec_id_and_sse_streams_real_events(ehome, client, monkeypatch):
    monkeypatch.setattr("tools.runbook_exec._run_spec",
                        lambda *a, **k: {"exit_code": 0, "stdout": "ok", "stderr": ""})
    data = _post_run(client, "t-ok")
    assert data["status"] == "running"
    assert data["exec_id"].startswith("exec_")

    events = _sse_events(client, data["exec_id"])
    names = [name for name, _ in events]
    assert names == ["runbook:event"] * len(events)
    payloads = [pl for _, pl in events]
    types = [p["type"] for p in payloads]
    assert types == ["step_start", "step_done", "step_start", "step_done",
                     "runbook_done"]
    assert payloads[-1]["status"] == "ok"
    assert payloads[-1]["exec_id"] == data["exec_id"]
    # 终端事件后流关闭（SSE 解析已 break）。


def test_sse_unknown_exec_id(ehome, client):
    events = []
    with client.stream("GET", "/api/runbook/executions/exec_ghost/progress") as resp:
        assert resp.status_code == 200
        ev = None
        data_lines = []
        for line in resp.iter_lines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "" and ev is not None:
                events.append((ev, json.loads("\n".join(data_lines))))
                ev = None
                data_lines = []
    assert events == [("runbook:error", {"message": "执行不存在: exec_ghost"})]


def test_history_running_list_while_in_flight(ehome, client, monkeypatch):
    block = threading.Event()

    def _blocking_run(home, target, spec):
        block.wait(timeout=15)
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr("tools.runbook_exec._run_spec", _blocking_run)
    data = _post_run(client, "t-ok")
    exec_id = data["exec_id"]

    # 执行中（runner 阻塞）→ data.running 可见 + ledger 尚无记录
    body = client.get("/api/runbook/executions").json()
    running_ids = [r["exec_id"] for r in body["data"]["running"]]
    assert exec_id in running_ids
    assert body["data"]["executions"] == []

    block.set()
    from tools.runbook_exec import recent_executions
    deadline = time.time() + 10
    rows = []
    while time.time() < deadline:
        rows = recent_executions(ehome)
        if rows:
            break
        time.sleep(0.05)
    assert rows and rows[0]["exec_id"] == exec_id
    assert rows[0]["result"] == "ok"

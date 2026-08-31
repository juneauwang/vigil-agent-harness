"""批三十五 — lazy last_seen 活性打点（runtime_state + 打点路径 + build_view 合并）。

覆盖：runtime_state 读写/0600 权限/多 host/空 host 不打点；build_view 合并
（有/无 last_seen 两态）；打点路径——topo_status_sync 探测成功才打、失败不打；
sudo_exec 执行成功才打、认证失败不打；vssh 发起会话打点。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.runtime_state import (
    get_host_activity,
    load_activity,
    mark_host_activity,
)
from hermes_cli.subcommands.topo_export import build_view

TOPOLOGY = """\
version: 3
clusters:
  - {name: prod, env: prod}
hosts:
  - {name: node1, env: prod, cluster: prod, services_index: hosts/node1.yaml}
  - {name: node2, env: prod, cluster: prod, services_index: hosts/node2.yaml}
"""

NODE1 = """\
host: node1
env: prod
cluster: prod
services:
  - {name: my-app, type: docker-container, attrs: {container: my-app}}
"""

NODE2 = """\
host: node2
env: prod
cluster: prod
services:
  - {name: svc-x}
"""


@pytest.fixture
def topo_home(tmp_path, monkeypatch):
    # batch83-fix（OPS-DELTA #99）：矩阵缺失 → deny（fail-closed）。本套件测
    # runtime_state 打点，与权限矩阵无关——显式关闭 ops 权限，避免 sudo 路径
    # 被"矩阵未初始化"提前拦截。
    (tmp_path / "config.yaml").write_text(
        "ops:\n  permissions:\n    enabled: false\n", encoding="utf-8")
    (tmp_path / "topology.yaml").write_text(TOPOLOGY, encoding="utf-8")
    (tmp_path / "hosts").mkdir()
    (tmp_path / "hosts" / "node1.yaml").write_text(NODE1, encoding="utf-8")
    (tmp_path / "hosts" / "node2.yaml").write_text(NODE2, encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    return tmp_path


# ── runtime_state 存储 ────────────────────────────────────────────────────

def test_runtime_state_mark_get_roundtrip(tmp_path):
    assert mark_host_activity("node1", home=tmp_path, now=1000.0) is True
    assert get_host_activity("node1", home=tmp_path) == 1000
    assert load_activity(tmp_path) == {"node1": 1000}


def test_runtime_state_multi_host(tmp_path):
    mark_host_activity("node1", home=tmp_path, now=100.0)
    mark_host_activity("node2", home=tmp_path, now=200.0)
    mark_host_activity("node1", home=tmp_path, now=300.0)  # 覆盖旧值
    assert load_activity(tmp_path) == {"node1": 300, "node2": 200}


def test_runtime_state_file_perms_0600(tmp_path):
    mark_host_activity("node1", home=tmp_path, now=100.0)
    path = tmp_path / "runtime_state.json"
    assert path.is_file()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode & 0o077 == 0  # 无 group/other 位
    # 内容为 {host: {last_seen: epoch}} 结构
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["node1"]["last_seen"] == 100.0


def test_runtime_state_empty_host_not_marked(tmp_path):
    assert mark_host_activity("", home=tmp_path) is False
    assert mark_host_activity("   ", home=tmp_path) is False
    assert load_activity(tmp_path) == {}
    # 文件不存在时 get/load 不炸
    assert get_host_activity("nope", home=tmp_path) is None
    assert load_activity(tmp_path) == {}


# ── build_view 合并 last_seen ─────────────────────────────────────────────

def test_build_view_merges_last_seen_when_recorded(topo_home):
    mark_host_activity("node1", home=topo_home, now=float(int(time.time()) - 60))
    view = build_view(topo_home)
    assert view is not None
    cards = {h["card"]["name"]: h["card"] for h in view["hosts"]}
    assert cards["node1"]["last_seen"] == int(time.time()) - 60
    # 无记录的 host 不返回该字段
    assert "last_seen" not in cards["node2"]


def test_build_view_no_last_seen_field_without_runtime_state(topo_home):
    view = build_view(topo_home)
    assert view is not None
    for h in view["hosts"]:
        assert "last_seen" not in h["card"]


# ── 打点路径：topo_status_sync 探测成功才打 ──────────────────────────────

def _fake_runner(ok: bool, stdout: str = "") -> object:
    res = SimpleNamespace(ok=ok, stdout=stdout, stderr="docker 不可用" if not ok else "")

    def runner(_cmd: str):
        return res

    return runner


def test_topo_status_sync_probe_success_marks_host(topo_home):
    from tools.topo_tools import topo_status_sync
    runner = _fake_runner(True, '{"name":"my-app","state":"running"}\n')
    topo_status_sync(host="node1", confirm=False, home=topo_home, runner=runner)
    assert get_host_activity("node1", home=topo_home) is not None


def test_topo_status_sync_local_machine_name_uses_local_runner(topo_home, monkeypatch):
    """本机名 = 拓扑 host 名（如 LAPTOP-T2JA2ERE，env: local）→ 本地 runner 探测。

    无 SSH credential 也须本地 docker 探测成功并打点（不是 SSH 回环报
    凭据缺失）。"""
    import socket
    from tools import topo_tools

    local_name = socket.gethostname()
    topo_yaml = (
        "version: 3\n"
        "clusters:\n"
        "  - {name: local, env: local}\n"
        "hosts:\n"
        f"  - {{name: {local_name}, env: local, cluster: local, services_index: hosts/local.yaml}}\n"
    )
    (topo_home / "topology.yaml").write_text(topo_yaml, encoding="utf-8")
    host_yaml = (
        f"host: {local_name}\n"
        "env: local\n"
        "services:\n"
        "  - {name: my-app, type: docker-container, attrs: {container: my-app}}\n"
    )
    (topo_home / "hosts" / "local.yaml").write_text(host_yaml, encoding="utf-8")
    monkeypatch.setattr(
        topo_tools,
        "_build_local_runner",
        lambda: _fake_runner(True, '{"name":"my-app","state":"running"}\n'),
    )
    topo_tools.topo_status_sync(host=socket.gethostname(), confirm=False, home=topo_home)
    assert get_host_activity(socket.gethostname(), home=topo_home) is not None


def test_topo_status_sync_probe_failure_does_not_mark(topo_home):
    from tools.topo_tools import topo_status_sync
    runner = _fake_runner(False)
    topo_status_sync(host="node1", confirm=False, home=topo_home, runner=runner)
    assert get_host_activity("node1", home=topo_home) is None


# ── 打点路径：sudo_exec 执行成功才打 ─────────────────────────────────────

def _run_sudo_exec(monkeypatch, host, returncode, stderr=""):
    from tools import sudo_tool
    monkeypatch.setattr(
        sudo_tool, "_resolve_topology_credential",
        lambda h, allow_fallback=False: {"type": "ssh_key", "ref": "/tmp/k", "user": "root", "port": 22},
    )
    monkeypatch.setattr(
        sudo_tool, "_run_remote_sudo",
        lambda h, u, p, c, cred: subprocess.CompletedProcess([], returncode, stdout="ok", stderr=stderr),
    )
    return sudo_tool._sudo_exec_handler({"host": host, "command": "ss -tlnp", "env": "test"})


def test_sudo_exec_success_marks_host(monkeypatch, tmp_path, topo_home):
    out = _run_sudo_exec(monkeypatch, "node1", returncode=0)
    assert '"status": "ok"' in out
    assert get_host_activity("node1", home=tmp_path) is not None


def test_sudo_exec_auth_failure_does_not_mark(monkeypatch, tmp_path):
    out = _run_sudo_exec(monkeypatch, "node1", returncode=1, stderr="authentication failed")
    assert "认证失败" in out
    assert get_host_activity("node1", home=tmp_path) is None


# ── 打点路径：vssh 发起会话打点 ──────────────────────────────────────────

def test_vssh_run_marks_host_before_exec(monkeypatch, tmp_path):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    from hermes_cli.subcommands import vssh
    called: list = []

    def _fake_execvpe(name, argv, env):
        called.append(argv)
        # execvpe 成功 = 进程替换、不会返回——用 SystemExit 模拟"替换成功"。
        raise SystemExit(0)

    monkeypatch.setattr(vssh, "_build_ssh_argv", lambda h, user="", port=22, key=None, cred=None: (["ssh", h], {}))
    monkeypatch.setattr(vssh, "_resolve_topology_credential", lambda h, allow_fallback=True: None)
    monkeypatch.setattr(vssh.os, "execvpe", _fake_execvpe)

    args = SimpleNamespace(hostspec="node1", user=None, port=22, key=None, no_credential=False)
    with pytest.raises(SystemExit):
        vssh.run(args)
    assert get_host_activity("node1", home=tmp_path) is not None
    assert called


def test_vssh_no_host_lists_without_marking(monkeypatch, tmp_path):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    from hermes_cli.subcommands import vssh
    called: list = []

    def _fake_execvpe(name, argv, env):
        called.append(argv)
        raise SystemExit(0)

    monkeypatch.setattr(vssh, "_build_ssh_argv", lambda h, user="", port=22, key=None, cred=None: (["ssh", h], {}))
    monkeypatch.setattr(vssh.os, "execvpe", _fake_execvpe)

    args = SimpleNamespace(hostspec="", user=None, port=22, key=None, no_credential=False)
    rc = vssh.run(args)  # 无 host → 列表路径，不打点
    assert rc == 0
    assert not called
    assert load_activity(tmp_path) == {}

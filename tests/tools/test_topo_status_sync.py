"""OPS-DELTA 批次二十四 — topo_status_sync 状态差异检测/同步验收测试。

覆盖：mock docker ps -a 输出 → 实体 running/实际 exited 差异检测、
confirm=False dry-run 不落盘、confirm=True 落盘 status=stopped、
实际状态不确定（docker 不可用/容器缺失）→ action=ask 不落盘、
无差异 → checked>0 changed=0、host 过滤、registry 注册与 schema 引导。
"""

from __future__ import annotations

import json

import pytest
import yaml

from tools.topo_discovery import ProbeResult
from tools.topo_tools import topo_status_sync

TOPO_YAML = """\
version: 3
updated_at: 2026-08-14
environments:
  - {name: local, isolation: relaxed, role: local}
  - {name: test, isolation: relaxed, role: test}
hosts:
  - {name: docker-host, env: test, runtime: docker, endpoint: "192.168.1.10", services_index: hosts/docker-host.yaml}
"""

HOST_INDEX = """\
host: docker-host
env: test
services:
  - {name: web, type: service, env: test, detail: entities/test__docker-host__web.yaml}
  - {name: db, type: db, env: test, detail: entities/test__docker-host__db.yaml}
  - {name: ghost, type: service, env: test, detail: entities/test__docker-host__ghost.yaml}
"""

WEB_YAML = """\
name: web
type: service
env: test
status: running
attrs:
  container: web-container
"""

DB_YAML = """\
name: db
type: db
env: test
status: running
attrs:
  container: db-container
"""

GHOST_YAML = """\
name: ghost
type: service
env: test
status: running
attrs:
  container: ghost-container
"""

# web-container 已停（exited）；db-container 正常运行；ghost-container 缺失。
DOCKER_PS_A = """\
{"Names":"/web-container","Image":"nginx:1.25","State":"exited","Labels":""}
{"Names":"db-container","Image":"postgres:16","State":"running","Labels":""}
"""


class FakeRunner:
    """可编程 mock runner：返回固定 docker ps -a 输出；记录调用。"""

    def __init__(self, output="", exit_code=0, stderr=""):
        self.output = output
        self.exit_code = exit_code
        self.stderr = stderr
        self.calls: list = []

    def __call__(self, cmd: str) -> ProbeResult:
        self.calls.append(cmd)
        return ProbeResult(self.output, self.exit_code, self.stderr)


@pytest.fixture
def sync_home(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    home.mkdir()
    files = {
        "topology.yaml": TOPO_YAML,
        "hosts/docker-host.yaml": HOST_INDEX,
        "entities/test__docker-host__web.yaml": WEB_YAML,
        "entities/test__docker-host__db.yaml": DB_YAML,
        "entities/test__docker-host__ghost.yaml": GHOST_YAML,
    }
    for rel, text in files.items():
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(home))
    import hermes_cli.config as _hc
    _hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        _hc._LOAD_CONFIG_CACHE.clear()


def _load(result: str) -> dict:
    return json.loads(result)


class TestTopoStatusSync:
    def test_dry_run_detects_difference_without_writing(self, sync_home):
        runner = FakeRunner(output=DOCKER_PS_A)
        result = _load(topo_status_sync(home=sync_home, runner=runner))
        assert result["checked"] == 3
        assert result["changed"] == 0
        assert result["confirm"] is False
        assert runner.calls == ["docker ps -a --format '{{json .}}'"]
        # web：拓扑 running / 实际 exited → update（写 stopped）。
        web = next(d for d in result["differences"] if d["entity"] == "web")
        assert web["expected"] == "running"
        assert web["actual"] == "exited"
        assert web["action"] == "update"
        assert web["write_status"] == "stopped"
        # db：running/running → 无差异。
        assert all(d["entity"] != "db" for d in result["differences"])
        # ghost：容器缺失 → ask，不自动写。
        ghost = next(n for n in result["needs_user"] if n["entity"] == "ghost")
        assert ghost["action"] == "ask"
        # dry-run 不落盘。
        saved = (sync_home / "entities" / "test__docker-host__web.yaml").read_text(encoding="utf-8")
        assert "status: stopped" not in saved

    def test_confirm_true_writes_stopped(self, sync_home):
        runner = FakeRunner(output=DOCKER_PS_A)
        result = _load(topo_status_sync(confirm=True, home=sync_home, runner=runner))
        assert result["changed"] == 1
        web = yaml.safe_load(
            (sync_home / "entities" / "test__docker-host__web.yaml").read_text(encoding="utf-8")
        )
        assert web["status"] == "stopped"
        assert web["source"] == "agent"
        # 一致实体与 ask 实体不动。
        db = yaml.safe_load(
            (sync_home / "entities" / "test__docker-host__db.yaml").read_text(encoding="utf-8")
        )
        assert db["status"] == "running"
        ghost = (sync_home / "entities" / "test__docker-host__ghost.yaml").read_text(encoding="utf-8")
        assert "status: stopped" not in ghost

    def test_docker_unavailable_asks_and_never_writes(self, sync_home):
        runner = FakeRunner(output="", exit_code=127, stderr="docker: command not found")
        result = _load(topo_status_sync(confirm=True, home=sync_home, runner=runner))
        assert result["checked"] == 3
        assert result["changed"] == 0
        assert len(result["needs_user"]) == 3
        assert all(n["action"] == "ask" for n in result["needs_user"])
        assert "docker 探测失败" in result["needs_user"][0]["reason"]
        web = (sync_home / "entities" / "test__docker-host__web.yaml").read_text(encoding="utf-8")
        assert "status: stopped" not in web

    def test_no_difference_when_all_consistent(self, sync_home):
        runner = FakeRunner(
            output=(
                '{"Names":"web-container","Image":"nginx","State":"running","Labels":""}\n'
                '{"Names":"db-container","Image":"postgres","State":"running","Labels":""}\n'
                '{"Names":"ghost-container","Image":"x","State":"running","Labels":""}\n'
            )
        )
        result = _load(topo_status_sync(home=sync_home, runner=runner))
        assert result["checked"] == 3
        assert result["changed"] == 0
        assert result["differences"] == []
        assert result["needs_user"] == []

    def test_host_param_filters(self, sync_home):
        runner = FakeRunner(output=DOCKER_PS_A)
        result = _load(topo_status_sync(host="docker-host", home=sync_home, runner=runner))
        assert result["checked"] == 3
        result = _load(topo_status_sync(host="nope", home=sync_home, runner=runner))
        assert result["checked"] == 0

    def test_entity_without_declared_status_asks_when_stopped(self, sync_home):
        (sync_home / "entities" / "test__docker-host__db.yaml").write_text(
            "name: db\ntype: db\nenv: test\nattrs:\n  container: db-container\n",
            encoding="utf-8",
        )
        runner = FakeRunner(
            output='{"Names":"db-container","Image":"postgres","State":"exited","Labels":""}\n'
        )
        result = _load(topo_status_sync(home=sync_home, runner=runner))
        db = next(n for n in result["needs_user"] if n["entity"] == "db")
        assert db["action"] == "ask"
        assert "未声明" in db["reason"]

    def test_handler_passes_args(self, sync_home, monkeypatch):
        import tools.topo_tools as tt

        captured = {}

        def fake_sync(**kw):
            captured.update(kw)
            return "{}"

        monkeypatch.setattr(tt, "topo_status_sync", fake_sync)
        tt._status_sync_handler({"host": "docker-host", "confirm": True})
        assert captured["host"] == "docker-host"
        assert captured["confirm"] is True

    def test_registered_in_topo_toolset_and_schema(self):
        from tools.registry import registry
        from tools.topo_tools import _DEFAULT_TOPO_UPDATE_SCHEMA, _TOPO_STATUS_SYNC_SCHEMA

        entry = registry.get_entry("topo_status_sync")
        assert entry is not None
        assert entry.toolset == "topo"
        assert "topo_status_sync" in registry.get_tool_names_for_toolset("topo")
        props = _TOPO_STATUS_SYNC_SCHEMA["parameters"]["properties"]
        assert "host" in props
        assert "confirm" in props
        # 任务 3：topo_update 描述引导走 topo_status_sync 正规链路。
        assert "topo_status_sync" in _DEFAULT_TOPO_UPDATE_SCHEMA["description"]
        assert "检测差异" in _DEFAULT_TOPO_UPDATE_SCHEMA["description"]

"""batch85（OPS-DELTA #101）—— vigil topo reset：清空拓扑回到未初始化。

覆盖：核心 topo_reset（topology.yaml + entities/services/hosts/hardware 全清、
保留非拓扑文件、空 home 幂等）；CLI run_reset（--yes 直清 / 交互 n 取消 /
审计记录 / 未初始化时无需 reset）；standalone main reset 入口。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import tools.topo_tools as tt
from hermes_cli.subcommands.topo_export import run_reset
from tools.topo_tools import topo_query, topo_reset

TOPOLOGY = """\
version: 4
updated_at: '2026-08-31'
environments:
  - {name: prod, isolation: strict, role: prod}
clusters:
  - {name: k3s-prod, env: prod, type: k3s, endpoint: "https://203.0.113.15:6443"}
hosts:
  - {name: node1, env: prod, cluster: k3s-prod, endpoint: "203.0.113.10", services_index: services/node1.yaml}
"""

NODE1_INDEX = """\
host: node1
services:
  - {name: harbor, type: registry, env: prod}
"""


@pytest.fixture
def topo_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "services").mkdir(parents=True)
    (home / "entities").mkdir()
    (home / "hardware").mkdir()
    (home / "hosts").mkdir()
    (home / "topology.yaml").write_text(TOPOLOGY, encoding="utf-8")
    (home / "services" / "node1.yaml").write_text(NODE1_INDEX, encoding="utf-8")
    (home / "entities" / "harbor.yaml").write_text(
        "name: harbor\ntype: registry\nenv: prod\n", encoding="utf-8"
    )
    (home / "hardware" / "node1.yaml").write_text("cpu: 8\n", encoding="utf-8")
    # 非拓扑文件必须保留
    (home / "matrix.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (home / "runbooks").mkdir()
    (home / "runbooks" / "x.yaml").write_text("name: x\n", encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(home))
    import hermes_cli.config as _hc
    _hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        _hc._LOAD_CONFIG_CACHE.clear()


class TestTopoResetCore:
    def test_reset_clears_all_topology_data(self, topo_home):
        result = topo_reset()
        assert result["ok"] is True
        assert result["hosts"] == 1
        assert result["clusters"] == 1
        assert result["entities"] == 2  # node1（host）+ harbor（服务实体）
        assert not (topo_home / "topology.yaml").exists()
        assert not (topo_home / "services").exists()
        assert not (topo_home / "entities").exists()
        assert not (topo_home / "hardware").exists()
        assert not (topo_home / "hosts").exists()

    def test_reset_keeps_non_topology_files(self, topo_home):
        topo_reset()
        assert (topo_home / "matrix.yaml").is_file()
        assert (topo_home / "runbooks" / "x.yaml").is_file()

    def test_reset_returns_uninitialized_for_query(self, topo_home):
        topo_reset()
        result = topo_query()
        payload = json.loads(result)
        assert "error" in payload
        assert "topology.yaml" in payload["error"]

    def test_reset_idempotent_on_empty_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        result = topo_reset()
        assert result["ok"] is True
        assert result["hosts"] == 0 and result["clusters"] == 0 and result["entities"] == 0


class TestTopoResetCli:
    def test_run_reset_yes_clears_and_audits(self, topo_home, monkeypatch):
        events = []
        monkeypatch.setattr(
            "agent.trajectory.record_event",
            lambda **kw: events.append(kw),
        )
        rc = run_reset(SimpleNamespace(yes=True))
        assert rc == 0
        assert not (topo_home / "topology.yaml").exists()
        assert events, "reset 必须写审计记录"
        ev = events[-1]
        assert ev["type"] == "topo_reset"
        assert ev["action"] == "reset"
        assert ev["meta"]["hosts"] == 1
        assert "topology.yaml" in ev["meta"]["removed"]

    def test_run_reset_interactive_no_cancels(self, topo_home, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        rc = run_reset(SimpleNamespace(yes=False))
        assert rc == 1
        assert (topo_home / "topology.yaml").is_file(), "取消后不得清空"
        assert "已取消" in capsys.readouterr().err

    def test_run_reset_interactive_yes_confirms(self, topo_home, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _prompt: "y")
        rc = run_reset(SimpleNamespace(yes=False))
        assert rc == 0
        assert not (topo_home / "topology.yaml").exists()

    def test_run_reset_uninitialized_noop(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        rc = run_reset(SimpleNamespace(yes=True))
        assert rc == 0
        assert "无需 reset" in capsys.readouterr().out

    def test_main_reset_standalone(self, topo_home, monkeypatch):
        from hermes_cli.subcommands.topo_export import main
        rc = main(["reset", "--yes"])
        assert rc == 0
        assert not (topo_home / "topology.yaml").exists()

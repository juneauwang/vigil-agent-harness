"""Vigil 值守分析层（OPS-DELTA #9 第二层）——watch_digest 数据契约测试。

覆盖：有未处理条目 → 摘要含全部告警 + 拓扑关联；无条目 → 明确"无待处理"；
mark_processed 后不再出现；instance 命中/未命中拓扑两态；check_fn 门控。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as hc
from tools.watch_tools import check_watch_requirements, watch_digest


@pytest.fixture
def watch_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "watch" / "inbox").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()

    def _write(ops: dict):
        (home / "config.yaml").write_text(
            yaml.safe_dump({"ops": ops}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        hc._LOAD_CONFIG_CACHE.clear()

    yield home, _write
    hc._LOAD_CONFIG_CACHE.clear()


def _write_inbox(home: Path, name: str, alerts: list, processed: bool = False) -> Path:
    path = home / "watch" / "inbox" / name
    path.write_text(
        json.dumps(
            {
                "collected_at": "2026-08-12T10:00:00Z",
                "alerts": alerts,
                "processed": processed,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _write_topo(home: Path) -> None:
    """最小 v0.2 拓扑：node1/prod + harbor/prod（服务）两个实体。"""
    topo = {
        "version": 2,
        "environments": [{"name": "prod"}],
        "hosts": [
            {"name": "node1", "env": "prod", "endpoint": "node1"},
            {"name": "node2", "env": "test", "endpoint": "node2"},
        ],
        "cross_host": [],
    }
    (home / "topology.yaml").write_text(
        yaml.safe_dump(topo, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _alerts():
    return [
        {"alertname": "HighCPU", "severity": "critical", "instance": "node1:9100",
         "startsAt": "2026-08-12T10:00:00Z", "state": "firing"},
        {"alertname": "HighMem", "severity": "warning", "instance": "unknown-host:9100",
         "startsAt": "2026-08-12T10:00:00Z", "state": "firing"},
    ]


class TestWatchDigest:
    def test_summary_with_topology_association(self, watch_home):
        home, _ = watch_home
        _write_topo(home)
        _write_inbox(home, "a.json", _alerts())
        result = watch_digest()
        assert "待处理告警: 2 条" in result
        assert "[critical] HighCPU" in result
        assert "instance=node1:9100" in result
        assert "拓扑: node1 (prod)" in result
        assert "[warning] HighMem" in result
        assert "拓扑: 不在拓扑" in result

    def test_no_unprocessed(self, watch_home):
        home, _ = watch_home
        result = watch_digest()
        assert result == "无待处理告警。"

    def test_processed_entries_hidden(self, watch_home):
        home, _ = watch_home
        _write_inbox(home, "old.json", _alerts(), processed=True)
        result = watch_digest()
        assert result == "无待处理告警。"

    def test_mark_processed_consumes(self, watch_home):
        home, _ = watch_home
        _write_topo(home)
        _write_inbox(home, "a.json", _alerts())
        first = watch_digest(mark_processed=True)
        assert "已按 mark_processed=True" in first
        # 标记后再调 → 无待处理
        second = watch_digest()
        assert second == "无待处理告警。"
        # 文件内容确实被改写
        data = json.loads((home / "watch" / "inbox" / "a.json").read_text(encoding="utf-8"))
        assert data["processed"] is True

    def test_no_topo_falls_back_to_not_in_topo(self, watch_home):
        home, _ = watch_home
        _write_inbox(home, "a.json", _alerts())  # 无 topology.yaml
        result = watch_digest()
        assert "拓扑: 不在拓扑" in result


class TestCheckWatchRequirements:
    def test_explicit_false_disables(self, watch_home):
        _, write = watch_home
        write({"watch": {"enabled": False}})
        assert check_watch_requirements() is False

    def test_absent_enables(self, watch_home):
        _, write = watch_home
        write({"watch": {"enabled": True}})
        assert check_watch_requirements() is True
        write({})
        assert check_watch_requirements() is True

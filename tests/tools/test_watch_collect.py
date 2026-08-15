"""Vigil 值守采集层（OPS-DELTA #9 第一层）——collect_once 数据契约测试。

覆盖：有告警写 inbox / 无告警不写 / 按 alertname+instance 去重 / 配置缺失
零行为 / 采集失败只写 errors.log 不抛 / last_collect 记录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as hc
from tools import watch_collect as wc


@pytest.fixture
def watch_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()

    def _write(prom: dict | None = None, watch: dict | None = None):
        ops = {}
        if prom is not None:
            ops["prometheus"] = prom
        if watch is not None:
            ops["watch"] = watch
        (home / "config.yaml").write_text(
            yaml.safe_dump({"ops": ops}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        hc._LOAD_CONFIG_CACHE.clear()

    yield home, _write
    hc._LOAD_CONFIG_CACHE.clear()


def _alert(alertname="HighCPU", instance="node1:9100", severity="critical", state="firing"):
    return {
        "labels": {"alertname": alertname, "severity": severity, "instance": instance},
        "status": {"state": state},
        "startsAt": "2026-08-12T10:00:00Z",
    }


def _inbox_files(home: Path):
    d = home / "watch" / "inbox"
    return sorted(d.glob("*.json")) if d.exists() else []


def _inbox_json(home: Path):
    return [json.loads(p.read_text(encoding="utf-8")) for p in _inbox_files(home)]


class TestCollectOnce:
    def test_alerts_write_inbox(self, watch_home, monkeypatch):
        home, write = watch_home
        write(prom={"alertmanager": "http://127.0.0.1:9093"})
        monkeypatch.setattr(wc, "_fetch_alerts", lambda am, cfg: [
            {"alertname": "HighCPU", "severity": "critical", "instance": "node1:9100",
             "startsAt": "2026-08-12T10:00:00Z", "state": "firing"},
        ])
        result = wc.collect_once()
        files = _inbox_files(home)
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["processed"] is False
        assert data["alerts"][0]["alertname"] == "HighCPU"
        assert data["alerts"][0]["instance"] == "node1:9100"
        assert data["collected_at"]
        assert "写入 1 条新告警" in result
        # last_collect 记录
        assert (home / "watch" / "last_collect").is_file()

    def test_no_alerts_no_file(self, watch_home, monkeypatch):
        home, write = watch_home
        write(prom={"alertmanager": "http://127.0.0.1:9093"})
        monkeypatch.setattr(wc, "_fetch_alerts", lambda am, cfg: [])
        result = wc.collect_once()
        assert "无活跃告警" in result
        assert _inbox_files(home) == []
        assert (home / "watch" / "last_collect").is_file()

    def test_dedup_same_alert_no_second_file(self, watch_home, monkeypatch):
        home, write = watch_home
        write(prom={"alertmanager": "http://127.0.0.1:9093"})
        alerts = [
            {"alertname": "HighCPU", "severity": "critical", "instance": "node1:9100",
             "startsAt": "2026-08-12T10:00:00Z", "state": "firing"},
            {"alertname": "HighMem", "severity": "warning", "instance": "node2:9100",
             "startsAt": "2026-08-12T10:00:00Z", "state": "firing"},
        ]
        monkeypatch.setattr(wc, "_fetch_alerts", lambda am, cfg: alerts)
        wc.collect_once()
        assert len(_inbox_files(home)) == 1
        # 同告警再来一次 → 去重，不追加
        result = wc.collect_once()
        assert "去重跳过" in result
        assert len(_inbox_files(home)) == 1
        # 新 instance 的告警 → 允许新条目
        alerts2 = list(alerts) + [
            {"alertname": "HighCPU", "severity": "critical", "instance": "node3:9100",
             "startsAt": "2026-08-12T10:05:00Z", "state": "firing"},
        ]
        monkeypatch.setattr(wc, "_fetch_alerts", lambda am, cfg: alerts2)
        wc.collect_once()
        assert len(_inbox_files(home)) == 2

    def test_processed_entry_allows_readd(self, watch_home, monkeypatch):
        home, write = watch_home
        write(prom={"alertmanager": "http://127.0.0.1:9093"})
        alerts = [
            {"alertname": "HighCPU", "severity": "critical", "instance": "node1:9100",
             "startsAt": "2026-08-12T10:00:00Z", "state": "firing"},
        ]
        monkeypatch.setattr(wc, "_fetch_alerts", lambda am, cfg: alerts)
        wc.collect_once()
        # 标记 processed 后，同键告警再次出现 → 允许新条目（恢复后再复发）
        for p in _inbox_files(home):
            data = json.loads(p.read_text(encoding="utf-8"))
            data["processed"] = True
            p.write_text(json.dumps(data), encoding="utf-8")
        wc.collect_once()
        assert len(_inbox_files(home)) == 2

    def test_watch_disabled_zero_behavior(self, watch_home, monkeypatch):
        home, write = watch_home
        write(prom={"alertmanager": "http://127.0.0.1:9093"}, watch={"enabled": False})
        called = []
        monkeypatch.setattr(wc, "_fetch_alerts", lambda am, cfg: called.append(1) or [])
        result = wc.collect_once()
        assert "已禁用" in result
        assert called == []
        assert _inbox_files(home) == []
        assert not (home / "watch").exists()

    def test_missing_alertmanager_writes_error_no_inbox(self, watch_home, monkeypatch):
        home, write = watch_home
        write(prom={"endpoint": "http://127.0.0.1:9090"})  # 无 alertmanager
        called = []
        monkeypatch.setattr(wc, "_fetch_alerts", lambda am, cfg: called.append(1) or [])
        result = wc.collect_once()
        assert "alertmanager 未配置" in result
        assert called == []
        assert _inbox_files(home) == []
        errors = (home / "watch" / "errors.log").read_text(encoding="utf-8")
        assert "alertmanager 未配置" in errors

    def test_fetch_failure_only_errors_log_no_raise(self, watch_home, monkeypatch):
        home, write = watch_home
        write(prom={"alertmanager": "http://127.0.0.1:9093"})

        def _boom(am, cfg):
            raise RuntimeError("HTTP 500: upstream timeout")

        monkeypatch.setattr(wc, "_fetch_alerts", _boom)
        result = wc.collect_once()
        assert "采集失败" in result
        assert "HTTP 500" in result
        assert _inbox_files(home) == []
        errors = (home / "watch" / "errors.log").read_text(encoding="utf-8")
        assert "HTTP 500" in errors

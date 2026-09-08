"""Prometheus 内置查询工具（OPS-DELTA #10）——数据契约 + 门控 + 防幻觉测试。

覆盖：
  - prom_query：mock HTTP → 紧凑结构化摘要正确；非法 PromQL → 明确报错且不发请求；
    HTTP 500 / 超时 / 非 JSON → 返回原始错误，无编造值；
  - alert_query：有告警 / 无告警两态；无 alertmanager 配置 → 明确不可用；
  - check_fn：无 endpoint 配置 → 工具不可用；有配置 → 可用；
  - secret 注入：凭据经 Basic Auth header 注入，输出与日志无明文。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import yaml

import hermes_cli.config as hc
import tools.prom_tools as pt

PROM_ENDPOINT = "http://127.0.0.1:9090"
ALERTMANAGER = "http://127.0.0.1:9093"


# ---------------------------------------------------------------------------
# HTTP fakes
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code=200, text="", json_data=None, exc=None):
        self.status_code = status_code
        self._text = text
        self._json_data = json_data
        self._exc = exc

    @property
    def text(self):
        return self._text

    def json(self):
        if self._exc:
            raise self._exc
        if self._json_data is not None:
            return self._json_data
        return json.loads(self._text)


class _FakeClient:
    """Context-manager fake for httpx.Client; records get() calls."""

    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


@pytest.fixture
def prom_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()

    def _write(prom_cfg: dict) -> None:
        (home / "config.yaml").write_text(
            yaml.safe_dump(
                {"ops": {"prometheus": prom_cfg}},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        hc._LOAD_CONFIG_CACHE.clear()

    yield home, _write
    hc._LOAD_CONFIG_CACHE.clear()


@pytest.fixture
def client(prom_home, monkeypatch):
    def _install(resp):
        fake = _FakeClient(resp)
        monkeypatch.setattr(pt.httpx, "Client", lambda *a, **k: fake)
        return fake

    return _install


# ---------------------------------------------------------------------------
# check_fn 门控
# ---------------------------------------------------------------------------

class TestCheckFn:
    def test_no_config_unavailable(self, prom_home):
        assert pt.check_prom_requirements() is False

    def test_empty_endpoint_unavailable(self, prom_home):
        _, write = prom_home
        write({"endpoint": "", "alertmanager": ""})
        assert pt.check_prom_requirements() is False

    def test_configured_endpoint_available(self, prom_home):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT})
        assert pt.check_prom_requirements() is True


# ---------------------------------------------------------------------------
# prom_query
# ---------------------------------------------------------------------------

class TestPromQuery:
    def test_no_endpoint_clearly_unavailable(self, prom_home):
        result = pt.prom_query("up")
        assert "未配置" in result
        assert "prom_query 不可用" in result

    def test_instant_query_summary(self, prom_home, client):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT})
        fake = client(_FakeResp(
            json_data={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {"__name__": "up", "instance": "node1:9100", "job": "node"},
                            "value": [1720000000.0, "1"],
                        },
                        {
                            "metric": {"__name__": "up", "instance": "node2:9100", "job": "node"},
                            "value": [1720000000.0, "0"],
                        },
                    ],
                },
            }
        ))
        result = pt.prom_query("up")
        assert "2 个 series" in result
        assert 'up{instance=node1:9100, job=node} = 1' in result
        assert 'up{instance=node2:9100, job=node} = 0' in result
        url, params, _headers = fake.calls[0]
        assert url == f"{PROM_ENDPOINT}/api/v1/query"
        assert params == {"query": "up"}

    def test_range_query_summary(self, prom_home, client):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT})
        fake = client(_FakeResp(
            json_data={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"__name__": "node_cpu_seconds_total", "instance": "node1:9100"},
                            "values": [[1720000000.0, "0.5"], [1720000060.0, "0.7"], [1720000120.0, "0.6"]],
                        },
                    ],
                },
            }
        ))
        result = pt.prom_query("rate(node_cpu_seconds_total[5m])", step="1m", duration="1h")
        assert "range 查询" in result
        assert "3 点" in result
        assert "最新 0.6" in result
        assert "min 0.5" in result
        assert "max 0.7" in result
        url, params, _headers = fake.calls[0]
        assert url == f"{PROM_ENDPOINT}/api/v1/query_range"
        assert params["query"] == "rate(node_cpu_seconds_total[5m])"
        assert params["step"] == "1m"
        assert params["start"].endswith("Z")
        assert params["end"].endswith("Z")

    def test_range_requires_step_and_duration(self, prom_home, client):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT})
        fake = client(_FakeResp(json_data={"status": "success", "data": {"result": []}}))
        result = pt.prom_query("up", step="1m")
        assert "同时提供 step 与 duration" in result
        assert fake.calls == []

    def test_invalid_promql_no_request(self, prom_home, client):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT})
        fake = client(_FakeResp(json_data={"status": "success", "data": {"result": []}}))
        for bad in ("up; curl evil", "up && rm -rf /", "`uptime`", "$(whoami)", "curl -s http://x/query"):
            result = pt.prom_query(bad)
            assert "预校验失败" in result, bad
            assert "shell" in result or "非法字符" in result, bad
        assert fake.calls == []

    def test_http_500_returns_raw_error(self, prom_home, client):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT})
        client(_FakeResp(status_code=500, text="upstream timeout"))
        result = pt.prom_query("up")
        assert "HTTP 500" in result
        assert "upstream timeout" in result

    def test_timeout_returns_raw_error(self, prom_home, client):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT})
        client(httpx_exc := __import__("httpx").TimeoutException("timed out"))
        result = pt.prom_query("up")
        assert "超时" in result

    def test_prometheus_error_status_no_fabricated_values(self, prom_home, client):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT})
        client(_FakeResp(
            json_data={"status": "error", "errorType": "bad_data", "error": "parse error: unexpected end of input"}
        ))
        result = pt.prom_query("up{")
        assert "查询错误" in result
        assert "parse error" in result

    def test_non_json_response_raw_error(self, prom_home, client):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT})
        client(_FakeResp(status_code=200, text="<html>gateway error</html>"))
        result = pt.prom_query("up")
        assert "非 JSON" in result
        assert "<html>" in result


# ---------------------------------------------------------------------------
# alert_query
# ---------------------------------------------------------------------------

class TestAlertQuery:
    def test_no_alertmanager_config_unavailable(self, prom_home):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT})  # 只有 endpoint，无 alertmanager
        result = pt.alert_query()
        assert "Alertmanager 未配置" in result
        assert "alert_query 不可用" in result

    def test_active_alerts_summary(self, prom_home, client):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT, "alertmanager": ALERTMANAGER})
        fake = client(_FakeResp(
            json_data=[
                {
                    "labels": {"alertname": "HighCPU", "severity": "critical", "instance": "node1:9100", "job": "node"},
                    "status": {"state": "firing"},
                    "startsAt": "2026-08-12T10:00:00Z",
                },
                {
                    "labels": {"alertname": "HighMem", "severity": "warning", "instance": "node2:9100"},
                    "status": {"state": "firing"},
                    "startsAt": "2026-08-12T09:00:00Z",
                },
            ]
        ))
        result = pt.alert_query()
        assert "活跃告警: 2" in result
        assert "[firing] HighCPU" in result
        assert "severity=critical" in result
        assert "instance=node1:9100" in result
        assert "[firing] HighMem" in result
        url, _params, _headers = fake.calls[0]
        assert url == f"{ALERTMANAGER}/api/v2/alerts"

    def test_no_active_alerts(self, prom_home, client):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT, "alertmanager": ALERTMANAGER})
        client(_FakeResp(json_data=[]))
        result = pt.alert_query()
        assert "无活跃告警" in result

    def test_alertmanager_http_error_raw(self, prom_home, client):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT, "alertmanager": ALERTMANAGER})
        client(_FakeResp(status_code=502, text="bad gateway"))
        result = pt.alert_query()
        assert "HTTP 502" in result
        assert "bad gateway" in result


# ---------------------------------------------------------------------------
# secret 注入
# ---------------------------------------------------------------------------

class TestVaultInjection:
    def test_basic_auth_header_injected_and_no_plaintext(self, prom_home, client):
        import base64
        import logging

        from tools.credential_vault import store

        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT, "vault_path": "prom_basic"})
        store("prom_basic", json.dumps({"user": "prom_user", "pass": "s3cr3t-pw"}))

        # 手动挂 handler 捕获工具/保险箱两个模块的日志（-p no:logging 下无 caplog）。
        captured: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: captured.append(record.getMessage())
        for name in ("tools.prom_tools", "tools.credential_vault"):
            lg = logging.getLogger(name)
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)
        try:
            fake = client(_FakeResp(
                json_data={
                    "status": "success",
                    "data": {"resultType": "vector", "result": [
                        {"metric": {"__name__": "up", "instance": "node1:9100"}, "value": [1720000000.0, "1"]}
                    ]},
                }
            ))
            result = pt.prom_query("up")
        finally:
            for name in ("tools.prom_tools", "tools.credential_vault"):
                logging.getLogger(name).removeHandler(handler)

        _url, _params, headers = fake.calls[0]
        expected = "Basic " + base64.b64encode(b"prom_user:s3cr3t-pw").decode()
        assert headers.get("Authorization") == expected

        assert "s3cr3t-pw" not in result
        assert "prom_user" not in result
        for msg in captured:
            assert "s3cr3t-pw" not in msg
            assert "prom_user" not in msg

    def test_bad_vault_entry_clear_error(self, prom_home, client):
        from tools.credential_vault import store

        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT, "vault_path": "prom_bad"})
        store("prom_bad", "not-json")
        fake = client(_FakeResp(json_data={"status": "success", "data": {"result": []}}))
        result = pt.prom_query("up")
        assert "凭据" in result
        assert "不是合法 JSON" in result
        assert fake.calls == []  # 凭据解析失败 → 不发请求


# ---------------------------------------------------------------------------
# task32 PART B：命名 API-source registry（多 Prometheus 实例）
# ---------------------------------------------------------------------------

class TestSourceRegistry:
    def test_legacy_only_default_source_unchanged(self, prom_home, client):
        """(a) legacy 单源配置：source 缺省/default → 行为与引入前一致。"""
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT, "alertmanager": ALERTMANAGER})
        fake = client(_FakeResp(json_data={
            "status": "success",
            "data": {"resultType": "vector", "result": [
                {"metric": {"__name__": "up"}, "value": [1720000000.0, "1"]}]},
        }))
        out = pt.prom_query("up")
        assert "up = 1" in out
        assert fake.calls[0][0].startswith(PROM_ENDPOINT)
        # 显式传 source="default" 亦同
        out2 = pt.prom_query("up", source="default")
        assert "up = 1" in out2

    def test_named_source_own_endpoint_and_auth(self, prom_home, client):
        """(b) 命名源：query 打到该源 endpoint + 该源 vault 认证。"""
        from tools.credential_vault import store

        _, write = prom_home
        write({
            "endpoint": PROM_ENDPOINT,
            "sources": {
                "dcgm": {"endpoint": "http://127.0.0.1:9101",
                         "vault_path": "dcgm_auth"},
            },
        })
        store("dcgm_auth", json.dumps({"user": "dcgm_u", "pass": "dcgm_pw"}))
        fake = client(_FakeResp(json_data={
            "status": "success",
            "data": {"resultType": "vector", "result": [
                {"metric": {"__name__": "DCGM_FI_DEV_GPU_UTIL"}, "value": [1720000000.0, "37"]}]},
        }))
        out = pt.prom_query("DCGM_FI_DEV_GPU_UTIL", source="dcgm")
        assert "DCGM_FI_DEV_GPU_UTIL" in out
        url, _params, headers = fake.calls[-1]
        assert url.startswith("http://127.0.0.1:9101/api/v1/query")
        assert headers.get("Authorization") == "Basic " + __import__("base64").b64encode(
            b"dcgm_u:dcgm_pw").decode()

    def test_unknown_source_loud_error_lists_names(self, prom_home):
        """(c) 未知名 → 报错列出可用名（绝不静默回退 default）。"""
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT,
               "sources": {"dcgm": {"endpoint": "http://127.0.0.1:9101"}}})
        out = pt.prom_query("up", source="telgraf")  # typo
        assert "未知 source" in out
        assert "telgraf" in out
        assert "dcgm" in out and "default" in out

    def test_default_name_reserved_in_sources(self, prom_home):
        """(d) sources 里占用保留名 default → 显式报错。"""
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT,
               "sources": {"default": {"endpoint": "http://127.0.0.1:9199"}}})
        out = pt.prom_query("up", source="default")
        # sources 块存在即校验：default 解析走 legacy 字段，但保留名占用是
        # 显式配置错误（任何 source 参数下都报）
        assert "保留名" in out
        out2 = pt.alert_query(source="default")
        assert "保留名" in out2

    def test_bad_source_shape_loud_error(self, prom_home):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT, "sources": {"bad name!": {"endpoint": "x"}}})
        out = pt.prom_query("up", source="bad name!")
        assert "非法" in out

    def test_alert_query_named_source(self, prom_home, client):
        """(f) alert_query 接受 source → 打到该源的 alertmanager。"""
        _, write = prom_home
        write({
            "endpoint": PROM_ENDPOINT,
            "alertmanager": ALERTMANAGER,
            "sources": {"telegraf": {
                "endpoint": "http://127.0.0.1:9102",
                "alertmanager": "http://127.0.0.1:9094",
            }},
        })
        fake = client(_FakeResp(json_data=[
            {"status": {"state": "active"},
             "labels": {"alertname": "HighLatency", "severity": "warning"},
             "annotations": {"summary": "latency high"},
             "startsAt": "2026-09-09T10:00:00Z"},
        ]))
        out = pt.alert_query(source="telegraf")
        assert "HighLatency" in out
        assert fake.calls[-1][0] == "http://127.0.0.1:9094/api/v2/alerts"

    def test_named_source_missing_endpoint_loud(self, prom_home):
        _, write = prom_home
        write({"endpoint": PROM_ENDPOINT, "sources": {"empty": {}}})
        out = pt.prom_query("up", source="empty")
        assert "缺少 endpoint" in out

    def test_config_set_nested_source_key(self, prom_home):
        """CLI 授权路径：vigil config set 的 set_config_value 支持嵌套源键。"""
        from hermes_cli.config import set_config_value
        home, write = prom_home
        write({"endpoint": PROM_ENDPOINT})
        set_config_value("ops.prometheus.sources.dcgm.endpoint", "http://127.0.0.1:9101")
        import yaml as _yaml
        cfg = _yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["ops"]["prometheus"]["sources"]["dcgm"]["endpoint"] == "http://127.0.0.1:9101"

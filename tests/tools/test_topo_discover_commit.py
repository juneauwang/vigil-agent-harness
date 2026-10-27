"""topo_discover commit 分支测试（D3）。

全部用注入的假 runner，不连真机、不写真实 VIGIL_HOME：
  - commit=false 保持旧行为（不落盘）
  - commit=true 走 write_discovery(merge=True)，草案全部 needs_review=true
  - 合并保留人工 endpoint/managed_by；host last_verified 按既有契约刷新
  - 同 host 重跑幂等（appended=0）
  - prod 审批拒绝不落盘、审批通过才落盘，rule_key 可审计
  - 落盘仍是 v0.4 结构
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
import yaml

from tools import topo_tools
from tools.topo_discovery import ProbeResult, discover_host as real_discover_host

HOST_LOCAL = "10.0.0.9"
HOST_PROD = "203.0.113.20"
SERVICE_NAMES = {"bookstack", "cache"}

DOCKER_PS = "\n".join([
    json.dumps({
        "Command": "/entrypoint.sh",
        "ID": "abc123",
        "Image": "linuxserver/bookstack:latest",
        "Labels": (
            "com.docker.compose.project=bookstack,"
            "com.docker.compose.service=bookstack"
        ),
        "Names": "bookstack",
        "Ports": "0.0.0.0:8080->80/tcp, :::8080->80/tcp",
        "State": "running",
    }),
    json.dumps({
        "Command": "redis-server",
        "ID": "def456",
        "Image": "redis:7",
        "Labels": "",
        "Names": "cache",
        "Ports": "0.0.0.0:6379->6379/tcp",
        "State": "running",
    }),
])

COMPOSE_LS = json.dumps([
    {"Name": "bookstack", "Status": "running(1)", "ConfigFiles": "/opt/bookstack/compose.yaml"},
])

SS_TLNP = """\
State Recv-Q Send-Q Local Address:Port Peer Address:Port Process
LISTEN 0      128    0.0.0.0:22      0.0.0.0:*    users:(("sshd",pid=1,fd=3))
LISTEN 0      128    0.0.0.0:8080    0.0.0.0:*    users:(("docker-proxy",pid=2,fd=4))
LISTEN 0      128    0.0.0.0:6379    0.0.0.0:*    users:(("redis-server",pid=3,fd=5))
"""


class FakeRunner:
    """按命令前缀返回探测结果；未命中统一 exit 127（等价于命令不存在）。"""

    def __init__(self, **probes):
        self.probes = probes
        self.calls: list[str] = []

    def __call__(self, cmd: str) -> ProbeResult:
        self.calls.append(cmd)
        matches = [(key, value) for key, value in self.probes.items() if key in cmd]
        if not matches:
            return ProbeResult("", 127, "command not found")
        _, value = max(matches, key=lambda kv: len(kv[0]))
        if isinstance(value, ProbeResult):
            return value
        if isinstance(value, tuple):
            return ProbeResult(value[0], value[1], value[2] if len(value) > 2 else "")
        return ProbeResult(str(value))


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """隔离 VIGIL_HOME，确保 handler 只写 tmp 目录。"""
    path = tmp_path / "vigil_home"
    path.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(path))
    return path


@pytest.fixture
def install_fake_discovery(monkeypatch):
    """把 topo_tools.discover_host 换成「真实发现逻辑 + 假 runner」。"""
    runner = FakeRunner(
        **{
            "docker ps": DOCKER_PS,
            "compose ls": COMPOSE_LS,
            "ss -tlnp": SS_TLNP,
            "kubectl": ProbeResult("", 127, "kubectl: command not found"),
            "systemctl": ProbeResult("", 127, "systemctl: command not found"),
            "nvidia-smi": ProbeResult("", 127, "nvidia-smi: command not found"),
        }
    )

    def wrapper(host, env, creds=None, **kwargs):
        kwargs.setdefault("runner", runner)
        return real_discover_host(host, env, creds, **kwargs)

    monkeypatch.setattr(topo_tools, "discover_host", wrapper)
    return runner


def _call(args: dict) -> dict:
    return json.loads(topo_tools._discover_handler(args))


def _commit(home: Path, host: str = HOST_LOCAL, env: str = "local", **extra) -> dict:
    args = {"host": host, "env": env, "commit": True, "reason": "登记测试服务"}
    args.update(extra)
    return _call(args)


def _index_path(home: Path, host: str = HOST_LOCAL) -> Path:
    return home / "services" / f"{host}.yaml"


def _load_index(home: Path, host: str = HOST_LOCAL) -> dict:
    return yaml.safe_load(_index_path(home, host).read_text(encoding="utf-8"))


def _load_topology(home: Path) -> dict:
    return yaml.safe_load((home / "topology.yaml").read_text(encoding="utf-8"))


def _service_rows(home: Path, host: str = HOST_LOCAL) -> dict:
    return {row["name"]: row for row in _load_index(home, host)["services"]}


# ---------------------------------------------------------------------------
# 1. commit=false 不落盘，返回体保持旧形状
# ---------------------------------------------------------------------------

def test_commit_false_does_not_write(home, install_fake_discovery):
    result = _call({"host": HOST_LOCAL, "env": "local", "commit": False})

    assert result["services"]
    assert {s["name"] for s in result["services"]} == SERVICE_NAMES
    assert "_guide" in result
    assert "status" not in result
    assert not (home / "topology.yaml").exists()
    assert not (home / "services").exists()
    assert not (home / "entities").exists()


# ---------------------------------------------------------------------------
# 2. commit=true（local）：落盘、全部草案 needs_review=true
# ---------------------------------------------------------------------------

def test_commit_true_writes_drafts_needing_review(home, install_fake_discovery):
    result = _commit(home)

    assert result["status"] == "committed"
    assert result["appended"] > 0
    assert result["kept"] == 0
    assert result["merged"] is False
    assert result["all_needs_review"] is True

    assert (home / "topology.yaml").is_file()
    assert _index_path(home).is_file()
    assert list((home / "entities").glob("*.yaml"))
    assert str(_index_path(home)) == result["index"]
    assert str(home / "topology.yaml") == result["topology"]

    # 新 host + 新服务都进 drafts。
    assert HOST_LOCAL in result["drafts"]
    assert SERVICE_NAMES.issubset(set(result["drafts"]))

    rows = _service_rows(home)
    for name in result["drafts"]:
        if name == HOST_LOCAL:
            host_row = _load_topology(home)["hosts"][0]
            assert host_row["name"] == HOST_LOCAL
            assert host_row["needs_review"] is True
        else:
            assert rows[name]["needs_review"] is True


# ---------------------------------------------------------------------------
# 3. 合并保留人工字段；host last_verified 刷新
# ---------------------------------------------------------------------------

def test_merge_keeps_manual_fields_and_refreshes_host_last_verified(home, install_fake_discovery):
    _commit(home)

    index_path = _index_path(home)
    index = _load_index(home)
    bookstack = next(row for row in index["services"] if row["name"] == "bookstack")
    bookstack["endpoint"] = "manual.example:9999"
    bookstack["managed_by"] = "manual"
    bookstack["needs_review"] = False
    bookstack["last_verified"] = "2020-01-01"
    index_path.write_text(yaml.safe_dump(index, allow_unicode=True, sort_keys=False), encoding="utf-8")

    topo_path = home / "topology.yaml"
    topo = _load_topology(home)
    host_row = topo["hosts"][0]
    host_row["owner"] = "ops-team"
    host_row["last_verified"] = "2020-01-01"
    topo_path.write_text(yaml.safe_dump(topo, allow_unicode=True, sort_keys=False), encoding="utf-8")

    result = _commit(home)

    assert result["appended"] == 0
    assert result["kept"] == len(index["services"])
    assert result["merged"] is True
    assert result["drafts"] == []

    rows = _service_rows(home)
    assert rows["bookstack"]["endpoint"] == "manual.example:9999"
    assert rows["bookstack"]["managed_by"] == "manual"
    assert rows["bookstack"]["needs_review"] is False
    # write_discovery 对同名服务行逐字保留（含旧 last_verified）；刷新的是
    # host 行治理契约里的 last_verified。
    assert rows["bookstack"]["last_verified"] == "2020-01-01"

    merged_host = _load_topology(home)["hosts"][0]
    assert merged_host["owner"] == "ops-team"
    assert merged_host["last_verified"] == datetime.date.today().isoformat()


# ---------------------------------------------------------------------------
# 4. 幂等：同 host 连 commit 两次
# ---------------------------------------------------------------------------

def test_commit_twice_is_idempotent(home, install_fake_discovery):
    first = _commit(home)
    entity_count = len(list((home / "entities").glob("*.yaml")))
    service_count = len(_load_index(home)["services"])
    assert first["appended"] > 0
    assert first["drafts"]

    second = _commit(home)

    assert second["appended"] == 0
    assert second["kept"] == service_count
    assert second["drafts"] == []
    assert len(_load_index(home)["services"]) == service_count
    assert len(list((home / "entities").glob("*.yaml"))) == entity_count


# ---------------------------------------------------------------------------
# 5. prod 审批拒绝 → 不落盘
# ---------------------------------------------------------------------------

def test_prod_denied_does_not_write(home, install_fake_discovery, monkeypatch):
    calls = []

    def fake_approval(tool_name, reason, *, rule_key="", approval_callback=None):
        calls.append({"tool_name": tool_name, "reason": reason, "rule_key": rule_key})
        return {"approved": False, "message": "denied by test"}

    monkeypatch.setattr("tools.approval.request_tool_approval", fake_approval)

    result = _commit(home, host=HOST_PROD, env="prod")

    assert result.get("approved") is False
    assert "denied by test" in result["error"]
    assert not (home / "topology.yaml").exists()
    assert not (home / "services").exists()

    assert len(calls) == 1
    assert calls[0]["tool_name"] == "topo_discover"
    assert calls[0]["rule_key"] == f"topo_discover:commit:{HOST_PROD}:prod"
    assert HOST_PROD in calls[0]["reason"]
    assert "prod" in calls[0]["reason"]


# ---------------------------------------------------------------------------
# 6. prod 审批通过 → 落盘，rule_key 可审计
# ---------------------------------------------------------------------------

def test_prod_approved_writes(home, install_fake_discovery, monkeypatch):
    calls = []

    def fake_approval(tool_name, reason, *, rule_key="", approval_callback=None):
        calls.append({"tool_name": tool_name, "reason": reason, "rule_key": rule_key})
        return {"approved": True, "message": None}

    monkeypatch.setattr("tools.approval.request_tool_approval", fake_approval)

    result = _commit(home, host=HOST_PROD, env="prod", reason="登记 BookStack")

    assert result["status"] == "committed"
    assert result["appended"] > 0
    assert (home / "topology.yaml").is_file()
    assert _index_path(home, HOST_PROD).is_file()
    assert len(calls) == 1
    assert calls[0]["rule_key"] == f"topo_discover:commit:{HOST_PROD}:prod"
    assert "登记 BookStack" in calls[0]["reason"]


# ---------------------------------------------------------------------------
# 7. v0.4 结构不被降级
# ---------------------------------------------------------------------------

def test_commit_preserves_v4_structure(home, install_fake_discovery):
    _commit(home)
    topo = _load_topology(home)

    assert topo["version"] == 4
    assert isinstance(topo["hosts"], list) and topo["hosts"]
    assert "sources" not in topo
    assert "cross_host" not in topo
    assert "key_paths" not in topo
    assert "core_entities" not in topo

    host_row = topo["hosts"][0]
    assert isinstance(host_row["role"], list)
    assert isinstance(host_row["runtime"], list)
    assert "services_index" not in host_row

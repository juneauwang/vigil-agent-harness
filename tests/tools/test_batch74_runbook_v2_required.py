"""batch74（OPS-DELTA #89）任务 1：runbook_create 强制 v0.2 验收测试。

覆盖（任务书 §任务 1）：
  - 新建 v0.1（steps[].commands 裸命令）→ 拒绝，错误信息含"必须使用 v0.2"，
    且错误信息里的动作词表来自 schemas.yaml（含第 24 动作 runbook）；
  - 新建 v0.2（steps[].action）→ 通过（走现有分层校验 + 资产审批路径）；
  - 手工放 v0.1 存量文件 → overwrite=true 提交 v0.1 新内容 → 放行（v0.1 仅
    允许 overwrite 存量文件）；
  - 磁盘是 v0.2 文件，overwrite=true 提交 v0.1 → 拒绝（不能把 v0.2 降级成 v0.1）；
  - argocd 同款 v0.2（action query/scale/restart，target: argocd-server）→ 通过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as hc
from tools.runbook_tools import runbook_create, runbook_load

TOPOLOGY = {
    "version": 4,
    "environments": [
        {"name": "prod", "isolation": "strict", "role": "prod"},
        {"name": "test", "isolation": "relaxed", "role": "test"},
    ],
    "clusters": [
        {"name": "k3s-prod", "env": "prod", "type": "k3s", "host_groups": ["k3s-node"]},
    ],
    "hosts": [
        {"name": "node1", "env": "prod", "cluster": "k3s-prod", "endpoint": "10.0.0.1",
         "role": ["worker"], "runtime": ["k3s"]},
    ],
}
SERVICES = {
    "node1.yaml": """services:
  - {name: harbor, type: registry, managed_by: docker}
  - {name: argocd-server, type: app, managed_by: kubectl}
""",
}


@pytest.fixture
def v2_home(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    (home / "runbooks").mkdir(parents=True)
    (home / "services").mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"approvals": {"mode": "off"}, "ops": {"permissions": {"enabled": False}}}, allow_unicode=True),
        encoding="utf-8",
    )
    (home / "topology.yaml").write_text(
        yaml.safe_dump(TOPOLOGY, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    for fname, body in SERVICES.items():
        (home / "services" / fname).write_text(body, encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def _load(result: str) -> dict:
    return json.loads(result)


def _v1_runbook(**overrides):
    data = {
        "runbook": "legacy-ops",
        "title": "存量 v0.1",
        "kind": "incident",
        "steps": [
            {"id": "s1", "title": "裸命令", "commands": ["kubectl get pods -A"]},
        ],
    }
    data.update(overrides)
    return data


def _v2_runbook(**overrides):
    data = {
        "runbook": "argocd-health",
        "title": "探查 argocd 状态，未运行则恢复",
        "env": "prod",
        "kind": "incident",
        "clusters": ["k3s-prod"],
        "steps": [
            {"id": "q", "title": "探查", "action": "query",
             "params": {"target": "argocd-server", "pattern": "deployment"}},
            {"id": "scale", "title": "拉起副本", "action": "scale",
             "params": {"target": "argocd-server", "replicas": 2}},
            {"id": "restart", "title": "重启", "action": "restart",
             "params": {"target": "argocd-server"}},
        ],
    }
    data.update(overrides)
    return data


def _seed_v1(home: Path, name: str = "legacy-ops") -> Path:
    path = home / "runbooks" / f"{name}.yaml"
    path.write_text(
        yaml.safe_dump(_v1_runbook(runbook=name), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


class TestV2Required:
    def test_new_v1_rejected_with_v2_required(self, v2_home):
        out = _load(runbook_create(**_v1_runbook(), home=v2_home))
        assert "error" in out
        assert "必须使用 v0.2" in out["error"]
        assert "仅供 overwrite 存量文件" in out["error"]
        # 动作词表来自 schemas.yaml（含第 24 动作 runbook），不硬编码。
        assert "runbook" in out["error"]
        assert "restart" in out["error"]
        assert not (v2_home / "runbooks" / "legacy-ops.yaml").exists()

    def test_new_v1_rejected_even_with_overwrite_flag(self, v2_home):
        """同名不存在时 overwrite=true 也不能用 v0.1 新建（overwrite 只对存量文件生效）。"""
        out = _load(runbook_create(**_v1_runbook(), overwrite=True, home=v2_home))
        assert "error" in out
        assert "必须使用 v0.2" in out["error"]

    def test_new_v2_created(self, v2_home):
        out = _load(runbook_create(**_v2_runbook(), home=v2_home))
        assert out["status"] == "created", out
        path = v2_home / "runbooks" / "argocd-health.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["version"] == 2
        assert "commands" not in str(data["steps"])
        assert data["approved_at"] and data["approved_version"]
        loaded = _load(runbook_load(runbook="argocd-health", home=v2_home))
        assert loaded["name"] == "argocd-health"

    def test_overwrite_existing_v1_allowed(self, v2_home):
        seed = _seed_v1(v2_home)
        out = _load(runbook_create(
            **_v1_runbook(summary="更新后的 v0.1 内容"),
            overwrite=True, home=v2_home,
        ))
        assert out["status"] == "updated", out
        written = yaml.safe_load(seed.read_text(encoding="utf-8"))
        assert written["version"] == 1
        assert written["summary"] == "更新后的 v0.1 内容"

    def test_overwrite_v2_with_v1_rejected(self, v2_home):
        _load(runbook_create(**_v2_runbook(), home=v2_home))
        out = _load(runbook_create(
            **_v1_runbook(runbook="argocd-health"),
            overwrite=True, home=v2_home,
        ))
        assert "error" in out
        assert "v0.2" in out["error"] and "降级" in out["error"]
        # 磁盘文件保持 v0.2 原样。
        path = v2_home / "runbooks" / "argocd-health.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["version"] == 2

    def test_argocd_style_v2_passes(self, v2_home):
        """任务书实测样例：argocd 同款 v0.2（query/scale/restart, target: argocd-server）。"""
        out = _load(runbook_create(**{
            "runbook": "argocd-recover",
            "title": "探查 argocd 状态，未运行则恢复",
            "env": "prod",
            "kind": "incident",
            "steps": [
                {"id": "probe", "title": "探查", "action": "query",
                 "params": {"target": "argocd-server"}},
                {"id": "scale", "title": "拉起", "action": "scale",
                 "params": {"target": "argocd-server", "replicas": 2}},
                {"id": "restart", "title": "恢复", "action": "restart",
                 "params": {"target": "argocd-server"}},
            ],
        }, home=v2_home))
        assert out["status"] == "created", out

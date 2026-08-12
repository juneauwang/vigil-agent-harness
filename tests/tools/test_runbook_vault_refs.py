"""OPS-DELTA #21 输出侧 — runbook <vault:...> 凭据引用描述化。

验收点：runbook 含 <vault:path/field> 占位符 → runbook_load 返回内容不含
明文；占位符以描述形态返回（agent 永远拿不到明文），并带 vault_refs 提示。
"""

from __future__ import annotations

import json

import pytest
import yaml

import hermes_cli.config as hc
from tools.runbook_tools import runbook_load

RUNBOOK = {
    "name": "rotate-creds",
    "title": "凭据轮换",
    "version": 1,
    "env": "test",
    "kind": "incident",
    "steps": [
        {
            "id": "preflight",
            "title": "预检",
            "commands": [
                "kubectl -n prod get secret grafana-admin -o jsonpath='{.data.password}' | base64 -d",
            ],
        },
        {
            "id": "apply",
            "title": "更新 datasource",
            "commands": [
                "curl -u admin:<vault:grafana/password> -X PUT https://grafana/api/datasources/1",
                "kubectl patch cm grafana --from-literal=password=<vault:secrets/grafana>",
            ],
        },
    ],
}


@pytest.fixture
def rb_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "runbooks").mkdir(parents=True)
    (home / "runbooks" / "rotate-creds.yaml").write_text(
        yaml.safe_dump(RUNBOOK, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    yield home
    hc._LOAD_CONFIG_CACHE.clear()


def test_runbook_load_never_returns_plaintext_vault_refs(rb_home):
    payload = json.loads(runbook_load("rotate-creds"))
    dumped = json.dumps(payload, ensure_ascii=False)
    # 占位符描述化（不返回明文——占位符本身不含明文）
    assert "<vault:grafana/password>" not in dumped
    assert "<vault:secrets/grafana>" not in dumped
    assert "vault:grafana/password" in dumped
    assert payload["vault_refs"] == 2
    assert "凭据引用" in payload["note"]
    # 任何命令里都不出现 <vault: 原始形态
    for step in payload["steps"]:
        for cmd in step.get("commands") or []:
            assert "<vault:" not in cmd, cmd


def test_runbook_without_vault_refs_unchanged(rb_home):
    plain = dict(RUNBOOK)
    plain["name"] = "plain-rb"
    plain["steps"] = [{"id": "s1", "commands": ["ls -la"]}]
    (rb_home / "runbooks" / "plain-rb.yaml").write_text(
        yaml.safe_dump(plain, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    payload = json.loads(runbook_load("plain-rb"))
    assert payload.get("vault_refs") is None
    assert payload["steps"][0]["commands"] == ["ls -la"]

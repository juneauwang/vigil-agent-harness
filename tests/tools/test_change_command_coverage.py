"""YAPL P5（OPS-DELTA #75）— 变更命令覆盖契约（替代批次十七 _CHANGE_COMMAND_RE）。

prod 变更确认门依赖操作分类层识别"变更类命令"（服务重启/容器重建/配置下发）——
漏识别的变更命令会退回 unknown → 普通 approve（smart 可自动放行，不强制人工）。
本套件把变更命令族逐条钉住：每条都必须被 classifier 归到 prod 矩阵 required 档
的动作（restart/deploy/scale/decommission/remove/stop/upgrade 等）→ prod 判定
require_confirmation=True。识别不出的命令族（kubectl edit/drain/cordon）按设计
落到 unknown → 默认 approve（保守，非强制）——这是规则表边界，登记在案。
"""

from __future__ import annotations

import yaml

import pytest

import hermes_cli.config as hc
from tools.action_classifier import classify_command
from tools.ops_permissions import check_ops_command_permission

# 变更命令族（每一条都必须命中规则表 → prod 矩阵 required）。
_CHANGE_SAMPLES = [
    "ansible-playbook site.yml",
    "kubectl apply -f deploy.yaml",
    "kubectl delete pod web-1",
    "kubectl scale deploy web --replicas=5",
    "kubectl rollout restart deploy/web",
    "docker compose up -d",
    "docker compose restart web",
    "docker compose rm -f",
    "docker compose down",
    "docker restart web",
    "docker rm -f web",
    "docker stop web",
    "systemctl restart nginx",
    "systemctl stop postgresql",
    "helm upgrade release chart",
    "helm install release chart",
    "helm uninstall release",
]

# 查询类反向样本：classifier 归 query/fetch_log → prod execute（decision None）。
_NON_CHANGE_SAMPLES = [
    "kubectl get pods",
    "docker ps",
    "systemctl status nginx",
]

# 变更但规则表外 → unknown → 默认 approve（保守，非强制确认门）。
_UNKNOWN_SAMPLES = [
    "kubectl edit deployment nginx",
    "kubectl drain node1",
    "kubectl cordon node2",
]

MATRIX = {
    "prod": {
        "query": "execute", "fetch_log": "execute", "verify": "execute",
        "restart": {"approve": "required"}, "deploy": {"approve": "required"},
        "scale": {"approve": "required"}, "decommission": {"approve": "required"},
        "remove": {"approve": "required"}, "stop": {"approve": "required"},
        "upgrade": {"approve": "required"}, "rollback": {"approve": "required"},
    },
}


@pytest.fixture
def prod_env(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "ops:\n  permissions:\n    enabled: true\n    env: prod\n    role: operator\n",
        encoding="utf-8",
    )
    (tmp_path / "matrix.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "updated_at": "2026-08-23T00:00:00+08:00",
        "source": "test",
        "base_template": "template2",
        "matrix": MATRIX,
        "sources": {"prod": {act: "test" for act in MATRIX["prod"]}},
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    yield
    hc._LOAD_CONFIG_CACHE.clear()


def test_change_samples_all_land_in_prod_required(prod_env):
    """变更清单每一条：classifier 归动作 + prod 矩阵 required（强制人工确认门）。"""
    for cmd in _CHANGE_SAMPLES:
        classification = classify_command(cmd)
        assert classification["action"] != "unknown", cmd
        decision = check_ops_command_permission(cmd, target_env="prod")
        assert decision is not None, cmd
        assert decision["level"] == "required", cmd
        assert decision["require_confirmation"] is True, cmd
        assert decision["action_name"] == classification["action"], cmd


def test_non_change_samples_query_execute(prod_env):
    """查询类命令 → query/fetch_log → prod execute → decision None。"""
    for cmd in _NON_CHANGE_SAMPLES:
        classification = classify_command(cmd)
        assert classification["action"] in ("query", "fetch_log"), cmd
        assert check_ops_command_permission(cmd, target_env="prod") is None, cmd


def test_unknown_change_samples_default_approve(prod_env):
    """规则表外的变更命令（kubectl edit/drain/cordon）→ unknown → 默认 approve
    （保守走审批门，非 required；OPS-DELTA #75 规则表边界登记）。"""
    for cmd in _UNKNOWN_SAMPLES:
        assert classify_command(cmd)["action"] == "unknown", cmd
        decision = check_ops_command_permission(cmd, target_env="prod")
        assert decision is not None, cmd
        assert decision["action_name"] == "unknown", cmd
        assert decision["require_confirmation"] is False, cmd

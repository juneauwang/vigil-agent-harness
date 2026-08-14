"""批次十七 §AA 风险点 1 — _CHANGE_COMMAND_RE 变更类清单覆盖测试。

prod 变更确认门依赖 ``_CHANGE_COMMAND_RE`` 识别"变更类命令"（服务重启/容器重建/
配置下发）——**漏识别的变更命令会退回普通审批 → yolo 放行**（prod 变更无确认执行）。
本套件把变更清单逐条钉住：每条都必须命中正则 + 在 prod 档返回 require_confirmation=True；
非变更命令不命中、prod 查询档照常 execute。若某条不命中 = 变更清单漏项 = 真实风险。
"""

from __future__ import annotations

import pytest

import hermes_cli.config as hc
from tools.ops_permissions import (
    _CHANGE_COMMAND_RE,
    check_ops_command_permission,
    classify_command,
)

# 变更清单（宁可多列不漏列——漏列的代价是 prod 变更无确认执行）。
_CHANGE_SAMPLES = [
    "ansible-playbook site.yml",
    "kubectl apply -f deploy.yaml",
    "kubectl delete pod web-1",
    "kubectl edit deployment nginx",
    "kubectl scale deploy web --replicas=5",
    "kubectl rollout restart deploy/web",
    "kubectl drain node1",
    "kubectl cordon node2",
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

# 非变更反向样本：不得命中正则，prod 查询档照常 execute（decision None）。
_NON_CHANGE_SAMPLES = [
    "ls -la",
    "cat /etc/hosts",
    "kubectl get pods",
    "docker ps",
    "systemctl status nginx",
    "helm list",
]


@pytest.fixture
def prod_env(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "ops:\n  permissions:\n    enabled: true\n    env: prod\n    role: operator\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    yield
    hc._LOAD_CONFIG_CACHE.clear()


def test_change_samples_all_match_regex_and_require_confirmation(prod_env):
    """变更清单每一条：正则命中 + prod 档 require_confirmation=True。"""
    missing = []
    for cmd in _CHANGE_SAMPLES:
        if not _CHANGE_COMMAND_RE.search(cmd):
            missing.append(cmd)
            continue
        decision = check_ops_command_permission(cmd, target_env="prod")
        assert decision is not None, cmd
        assert decision["require_confirmation"] is True, cmd
    assert missing == [], f"变更清单漏项（未命中 _CHANGE_COMMAND_RE）: {missing}"


def test_change_samples_cover_positive_and_deny_classes(prod_env):
    """变更清单里既有 approve 类（L2）也有 deny 类（L3/L4）——都强制确认门。"""
    for cmd in _CHANGE_SAMPLES:
        decision = check_ops_command_permission(cmd, target_env="prod")
        assert decision is not None and decision["require_confirmation"] is True, cmd
        assert decision["action"] in ("approve", "deny"), cmd


def test_non_change_samples_not_matched_and_execute(prod_env):
    """非变更命令不命中正则；已分级查询（L1）prod 照常 execute（decision None）。

    ``helm list`` 是未分级查询（grade=None，不在 L1 模式内）——它不命中变更正则
    （不是变更类），但 B' 对 prod 未分级命令默认进确认门（预期行为，见报告
    "已知风险：B' 噪音面"——helm/k8s 等未分级查询命令在 prod 会开始要审批）。
    """
    for cmd in _NON_CHANGE_SAMPLES:
        assert not _CHANGE_COMMAND_RE.search(cmd), cmd
        if classify_command(cmd) == "L1":
            assert check_ops_command_permission(cmd, target_env="prod") is None, cmd
        else:
            # 未分级 + prod → B' 确认门（不是变更正则误伤，是 B' 预期）
            decision = check_ops_command_permission(cmd, target_env="prod")
            assert decision is not None and decision["require_confirmation"] is True, cmd


def test_change_regex_is_case_insensitive():
    assert _CHANGE_COMMAND_RE.search("KUBECTL APPLY -f x.yaml")
    assert _CHANGE_COMMAND_RE.search("Systemctl Restart nginx")
    assert _CHANGE_COMMAND_RE.search("Docker Compose Up -d")

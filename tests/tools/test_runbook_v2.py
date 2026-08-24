"""YAPL P2（批次五十三）：runbook schema v0.2 分层校验验收测试。

覆盖（任务书 §7 后端清单）：
  - 结构校验：动作词表 / 必填参数（target 缺失）/ 枚举（kind/expect 通道/
    on_failure 语义）/ 拒绝 permission / schedule cron+timezone / triggers 互斥；
  - 引用校验：target 存在性 / 类型兼容（主机族动作不接 service）/ 四维嵌套
    （host_group 不跨集群、host 集群在声明 clusters 内、env 一致）；
  - 关系校验：变量引用解析 / params 字段存在 / 自引用拒绝 / restore.from 必须
    引用 backup 步骤 dest / on_failure 场景引用存在；
  - 双 schema 兼容：v0.1 老 runbook 仍 load 通过、v0.2 校验拦截错误用例、
    checkpoint 对 v0.2 返回"执行器 P4"提示。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as hc
from tools.runbook_tools import (
    _validate_runbook,
    runbook_checkpoint,
    runbook_create,
    runbook_load,
)

TOPOLOGY = {
    "version": 4,
    "environments": [
        {"name": "prod", "isolation": "strict", "role": "prod"},
        {"name": "test", "isolation": "relaxed", "role": "test"},
    ],
    "clusters": [
        {"name": "k3s-prod", "env": "prod", "type": "k3s", "host_groups": ["k3s-node"]},
        {"name": "k3s-dev", "env": "test", "type": "k3s", "host_groups": ["k3s-dev-node"]},
    ],
    "hosts": [
        {"name": "node1", "env": "prod", "cluster": "k3s-prod", "endpoint": "10.0.0.1",
         "role": ["worker"], "runtime": ["k3s"]},
        {"name": "node2", "env": "prod", "cluster": "k3s-prod", "endpoint": "10.0.0.2",
         "role": ["worker"], "runtime": ["k3s"]},
        {"name": "dev-node", "env": "test", "cluster": "k3s-dev", "endpoint": "10.0.1.1"},
    ],
}
SERVICES = {
    "node1.yaml": """services:
  - {name: harbor, type: registry, managed_by: docker}
  - {name: nginx, type: app, managed_by: docker}
""",
    "node2.yaml": """services:
  - {name: gateway-svc, type: app, managed_by: kubectl}
""",
}


@pytest.fixture
def v2_home(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    (home / "runbooks").mkdir(parents=True)
    (home / "services").mkdir(parents=True)
    # YAPL P3 资产审批（§11.4）：v0.2 创建过审批门。本套件是 schema 校验测试，
    # 与审批语义无关——显式 approvals.mode=off 绕过资产审批门（资产审批路径
    # 由 test_matrix.py 专测），保持 41 例 schema 断言专注不串味。
    (home / "config.yaml").write_text(
        yaml.safe_dump({"approvals": {"mode": "off"}}, allow_unicode=True),
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


def _v2_runbook(**overrides):
    data = {
        "runbook": "nginx-config-update",
        "title": "Nginx 配置变更并生效",
        "env": "prod",
        "kind": "maintenance",
        "clusters": ["k3s-prod"],
        "triggers": [
            "harbor healthcheck failed",
            {"alertname": "HarborHealthcheckDown", "severity": "critical"},
        ],
        "on_failure": "stop",
        "rollback": [
            {"name": "rollback-main", "steps": [
                {"id": "rb-restore", "title": "恢复配置", "action": "restore",
                 "params": {"target": "nginx", "from": "{{ steps.backup.params.dest }}"}},
            ]},
        ],
        "steps": [
            {"id": "backup", "title": "变更前备份", "action": "backup",
             "params": {"target": "nginx", "dest": "/backup/nginx/config/latest"}},
            {"id": "apply", "title": "应用配置变更", "action": "apply_config",
             "params": {"target": "nginx",
                        "changes": [{"key": "http.server_tokens", "value": "off"}]},
             "on_failure": {"rollback": "rollback-main"}},
            {"id": "verify", "title": "验证生效", "action": "verify",
             "params": {"target": "nginx"},
             "expect": {"target": "http", "url": "http://127.0.0.1/healthz",
                        "http_status": 200}},
        ],
    }
    data.update(overrides)
    return data


def _err(rb_home, **overrides) -> str:
    result = _load(runbook_create(**_v2_runbook(**overrides), home=rb_home))
    assert result.get("status") is None or "error" in result
    return result.get("error", "")


# ---------------------------------------------------------------------------
# 结构层
# ---------------------------------------------------------------------------

class TestV2Structure:
    def test_create_roundtrip_loads(self, v2_home):
        out = _load(runbook_create(**_v2_runbook(), home=v2_home))
        assert out["status"] == "created"
        assert out["name"] == "nginx-config-update"
        path = v2_home / "runbooks" / "nginx-config-update.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["version"] == 2
        assert data["kind"] == "maintenance"
        assert "commands" not in str(data["steps"])
        loaded = _load(runbook_load(runbook="nginx-config-update", home=v2_home))
        assert loaded["name"] == "nginx-config-update"
        assert "v0.2 runbook" in loaded["note"]

    def test_action_not_in_wordlist_rejected(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "旧名", "action": "restart_service",
                                    "params": {"target": "nginx"}}])
        assert "restart_service" in err and "restart" in err  # 报错引导合法动作

    def test_required_target_missing_rejected(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "x", "action": "restart",
                                    "params": {}}])
        assert "target" in err

    def test_scale_requires_replicas(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "x", "action": "scale",
                                    "params": {"target": "gateway-svc"}}])
        assert "replicas" in err

    def test_scale_replicas_must_be_int(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "x", "action": "scale",
                                    "params": {"target": "gateway-svc", "replicas": "3"}}])
        assert "整数" in err

    def test_permission_field_rejected(self, v2_home):
        data = _v2_runbook()
        data["name"] = "nginx-config-update"
        data["version"] = 2
        data["permission"] = {"restart": "approve"}
        with pytest.raises(ValueError) as exc:
            _validate_runbook(data, "nginx-config-update", v2_home)
        assert "permission" in str(exc.value) and "操作矩阵" in str(exc.value)

    def test_on_failure_continue_blocked_on_mutating_action(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "x", "action": "restart",
                                    "params": {"target": "nginx"}, "on_failure": "continue"}])
        assert "continue" in err and "只读动作" in err

    def test_on_failure_continue_allowed_on_readonly(self, v2_home):
        out = _load(runbook_create(**_v2_runbook(steps=[
            {"id": "s1", "title": "查询", "action": "query", "params": {"pattern": "nginx"},
             "on_failure": "continue"},
        ], rollback=[]), home=v2_home))
        assert out["status"] == "created"

    def test_on_failure_rollback_scenario_missing_rejected(self, v2_home):
        err = _err(v2_home, on_failure={"rollback": "no-such-scenario"})
        assert "no-such-scenario" in err

    def test_on_failure_rollback_without_scenarios_rejected(self, v2_home):
        err = _err(v2_home, rollback=[], on_failure="rollback")
        assert "rollback" in err

    def test_expect_bad_channel_rejected(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "x", "action": "verify",
                                    "params": {"target": "nginx"},
                                    "expect": {"target": "curl", "http_status": 200}}])
        assert "检查通道" in err

    def test_expect_no_predicate_rejected(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "x", "action": "verify",
                                    "params": {"target": "nginx"},
                                    "expect": {"target": "http"}}])
        assert "谓词" in err

    def test_schedule_bad_cron_segments_rejected(self, v2_home):
        err = _err(v2_home, triggers=None,
                   schedule={"cron": "0 2 *", "timezone": "Asia/Shanghai"})
        assert "5/6 段" in err

    def test_schedule_bad_timezone_rejected(self, v2_home):
        err = _err(v2_home, triggers=None,
                   schedule={"cron": "0 2 * * 3", "timezone": "Shanghai/Nowhere"})
        assert "时区" in err

    def test_schedule_valid_roundtrip(self, v2_home):
        out = _load(runbook_create(**_v2_runbook(
            triggers=None,
            schedule={"cron": "0 2 * * 3", "timezone": "Asia/Shanghai"},
        ), home=v2_home))
        assert out["status"] == "created"

    def test_triggers_and_schedule_mutually_exclusive(self, v2_home):
        err = _err(v2_home, schedule={"cron": "0 2 * * 3", "timezone": "UTC"})
        assert "互斥" in err

    def test_kind_invalid_rejected(self, v2_home):
        err = _err(v2_home, kind="runbook")
        assert "kind" in err

    def test_maintenance_kind_allowed(self, v2_home):
        assert _v2_runbook()["kind"] == "maintenance"

    def test_mixed_commands_action_rejected(self, v2_home):
        err = _err(v2_home, steps=[
            {"id": "s1", "title": "x", "action": "restart", "params": {"target": "nginx"}},
            {"id": "s2", "title": "y", "commands": ["docker ps"]},
        ])
        assert "混用" in err

    def test_run_script_inline_rejected(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "x", "action": "run_script",
                                    "params": {"script": "#!/bin/bash\nrm -rf /"}}])
        assert "只引用资产" in err

    def test_apply_config_changes_missing_key_rejected(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "x", "action": "apply_config",
                                    "params": {"target": "nginx", "changes": [{"value": "off"}]}}])
        assert "key" in err

    def test_version_invalid_rejected(self, v2_home):
        data = _v2_runbook()
        data["name"] = "nginx-config-update"
        data["version"] = "2"
        with pytest.raises(ValueError) as exc:
            _validate_runbook(data, "nginx-config-update", v2_home)
        assert "version" in str(exc.value)


# ---------------------------------------------------------------------------
# 引用层（拓扑表）
# ---------------------------------------------------------------------------

class TestV2References:
    def test_target_not_in_topology_rejected(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "x", "action": "restart",
                                    "params": {"target": "ghost-service"}}])
        assert "不在拓扑表" in err

    def test_reboot_target_service_rejected(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "x", "action": "reboot",
                                    "params": {"target": "nginx"}}])
        assert "类型不兼容" in err

    def test_reboot_target_host_allowed(self, v2_home):
        out = _load(runbook_create(**_v2_runbook(steps=[
            {"id": "s1", "title": "重启 node1", "action": "reboot", "params": {"target": "node1"}},
        ], rollback=[]), home=v2_home))
        assert out["status"] == "created"

    def test_cluster_not_in_topology_rejected(self, v2_home):
        err = _err(v2_home, clusters=["k3s-staging"])
        assert "k3s-staging" in err and "拓扑表" in err

    def test_host_group_not_in_topology_rejected(self, v2_home):
        err = _err(v2_home, host_groups=["ghost-group"])
        assert "ghost-group" in err

    def test_host_group_cross_cluster_nesting_rejected(self, v2_home):
        # k3s-node 属于 k3s-prod；声明 clusters=[k3s-dev] → 严格嵌套失败
        err = _err(v2_home, clusters=["k3s-dev"], host_groups=["k3s-node"])
        assert "嵌套" in err or "不属于" in err

    def test_host_outside_declared_clusters_rejected(self, v2_home):
        err = _err(v2_home, clusters=["k3s-prod"], hosts=["dev-node"])
        assert "嵌套" in err or "集群" in err

    def test_env_mismatch_cluster_rejected(self, v2_home):
        err = _err(v2_home, env="test", clusters=["k3s-prod"])
        assert "env" in err

    def test_transfer_file_host_not_in_topology_rejected(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "x", "action": "transfer_file",
                                    "params": {"source": {"path": "/a"},
                                               "dest": {"host": "rogue-host", "path": "/b"}}}])
        assert "rogue-host" in err


# ---------------------------------------------------------------------------
# 关系层（变量引用 + 交叉引用）
# ---------------------------------------------------------------------------

class TestV2Relations:
    def test_var_ref_missing_step_rejected(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "x", "action": "restore",
                                    "params": {"target": "nginx",
                                               "from": "{{ steps.no-such.params.dest }}"}}])
        assert "no-such" in err

    def test_var_ref_missing_param_field_rejected(self, v2_home):
        err = _err(v2_home, steps=[
            {"id": "backup", "title": "备份", "action": "backup",
             "params": {"target": "nginx", "dest": "/backup/x"}},
            {"id": "s1", "title": "x", "action": "restore",
             "params": {"target": "nginx", "from": "{{ steps.backup.params.missing }}"}},
        ])
        assert "字段不存在" in err

    def test_self_reference_rejected(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "x", "action": "restore",
                                    "params": {"target": "nginx",
                                               "from": "{{ steps.s1.params.dest }}"}}])
        assert "自引用" in err

    def test_trigger_context_unknown_field_rejected(self, v2_home):
        err = _err(v2_home, steps=[{"id": "s1", "title": "x", "action": "restart",
                                    "params": {"target": "{{ trigger_context.hostname }}"}}])
        assert "触发上下文" in err

    def test_restore_from_must_reference_backup_step(self, v2_home):
        err = _err(v2_home, steps=[
            {"id": "backup", "title": "备份", "action": "backup",
             "params": {"target": "nginx", "dest": "/backup/x"}},
            {"id": "apply", "title": "应用", "action": "apply_config",
             "params": {"target": "nginx", "changes": [{"key": "a", "value": "b"}]}},
            {"id": "s1", "title": "x", "action": "restore",
             "params": {"target": "nginx", "from": "{{ steps.apply.params.target }}"}},
        ])
        assert "backup" in err

    def test_valid_var_ref_passes(self, v2_home):
        out = _load(runbook_create(**_v2_runbook(), home=v2_home))
        assert out["status"] == "created"

    def test_trigger_context_field_allowed(self, v2_home):
        out = _load(runbook_create(**_v2_runbook(steps=[
            {"id": "s1", "title": "重启", "action": "restart",
             "params": {"target": "{{ trigger_context.alertname }}"}},
        ], rollback=[]), home=v2_home))
        assert out["status"] == "created"


# ---------------------------------------------------------------------------
# 双 schema 兼容
# ---------------------------------------------------------------------------

class TestV2DualSchema:
    def _v1_runbook(self):
        return {
            "runbook": "deploy-check",
            "title": "部署 checklist",
            "env": "prod",
            "kind": "deploy",
            "steps": [
                {"id": "preflight", "title": "前置核对", "commands": ["echo ok"]},
                {"id": "verify", "title": "真实验证", "commands": ["curl -s http://x"],
                 "verify": "curl -s http://x", "expect": '"ok"'},
            ],
            "rollback": [{"title": "回滚", "commands": ["echo rb"]}],
        }

    def test_v1_create_and_load_unchanged(self, v2_home):
        # batch74：v0.1 仅允许 overwrite 存量文件——先手工放 v0.1 存量。
        path = v2_home / "runbooks" / "deploy-check.yaml"
        path.write_text(
            "name: deploy-check\ntitle: 存量\nversion: 1\nkind: deploy\n"
            "steps:\n  - id: preflight\n    title: x\n    commands: [echo ok]\n",
            encoding="utf-8",
        )
        out = _load(runbook_create(**self._v1_runbook(), overwrite=True, home=v2_home))
        assert out.get("status") in ("created", "updated"), out
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["version"] == 1
        loaded = _load(runbook_load(runbook="deploy-check", home=v2_home))
        assert "echo ok" in loaded["steps"][0]["commands"][0]

    def test_v2_checkpoint_points_to_executor(self, v2_home):
        runbook_create(**_v2_runbook(), home=v2_home)
        out = _load(runbook_checkpoint(runbook="nginx-config-update", step_id="backup",
                                       status="pass", home=v2_home))
        assert "runbook_execute" in out.get("error", "")

    def test_v1_checkpoint_still_works(self, v2_home):
        (v2_home / "runbooks" / "deploy-check.yaml").write_text(
            "name: deploy-check\ntitle: 存量\nversion: 1\nkind: deploy\n"
            "steps:\n  - id: preflight\n    title: x\n    commands: [echo ok]\n",
            encoding="utf-8",
        )
        runbook_create(**self._v1_runbook(), overwrite=True, home=v2_home)
        out = _load(runbook_checkpoint(runbook="deploy-check", step_id="preflight",
                                       status="pass", home=v2_home))
        assert out.get("recorded") == "preflight"

"""Ops harness runbook tools — data contract + L4 checklist tests.

Pins the program-layer contract (ops-agent-harness.md §1 / §3 L4):
  - runbook_load: by name / trigger-keyword match / list
  - schema validation (name consistency, steps, L4 checklist shape)
  - runbook_checkpoint: L4 phase ordering (前置核对 → 滚动发布 → 真实验证)
  - env-mismatch warning for cross-environment runbooks
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as hc
from tools.runbook_tools import (
    check_runbook_requirements,
    runbook_checkpoint,
    runbook_load,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_RUNBOOKS = PROJECT_ROOT / "hermes_cli" / "ops_samples" / "runbooks"


@pytest.fixture
def rb_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "runbooks").mkdir(parents=True)
    for src in sorted(SAMPLE_RUNBOOKS.glob("*.yaml")):
        shutil.copy2(src, home / "runbooks" / src.name)
    monkeypatch.setenv("HERMES_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def _load(result: str) -> dict:
    return json.loads(result)


def _write_runbook(home: Path, name: str, data: dict) -> None:
    (home / "runbooks" / name).write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _write_config(home: Path, data: dict) -> None:
    (home / "config.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    hc._LOAD_CONFIG_CACHE.clear()


def test_load_by_name(rb_home):
    result = _load(runbook_load(runbook="harbor-restart", home=rb_home))
    assert result["name"] == "harbor-restart"
    assert result["env"] == "prod"
    assert result["kind"] == "incident"
    step_ids = [s["id"] for s in result["steps"]]
    assert step_ids == ["diagnose", "restart", "verify"]
    assert any("docker restart harbor" in c for s in result["steps"] for c in s.get("commands", []))
    assert result["rollback"]
    assert "不执行任何命令" in result["note"]


def test_load_by_trigger_query(rb_home):
    result = _load(runbook_load(query="镜像拉取超时", home=rb_home))
    assert result["name"] == "harbor-restart"

    result = _load(runbook_load(query="发布 gateway-svc", home=rb_home))
    assert result["name"] == "deploy-gateway-svc"
    assert result["checklist"] is True


def test_load_list(rb_home):
    result = _load(runbook_load(home=rb_home))
    assert result["count"] == 3
    names = {r["name"] for r in result["runbooks"]}
    assert names == {"harbor-restart", "gateway-svc-restart", "deploy-gateway-svc"}


def test_load_unknown_and_empty_dir(tmp_path, monkeypatch):
    home = tmp_path / "empty"
    (home / "runbooks").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        result = runbook_load(runbook="nope", home=home)
        assert "不存在" in result
        result = runbook_load(home=home)
        assert "count" in result
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def test_validation_name_mismatch_and_missing_steps(rb_home):
    _write_runbook(rb_home, "bad.yaml", {"name": "other", "title": "x", "steps": []})
    result = runbook_load(runbook="bad", home=rb_home)
    assert "不一致" in result or "steps" in result

    _write_runbook(rb_home, "nosteps.yaml", {"name": "nosteps", "title": "x"})
    result = runbook_load(runbook="nosteps", home=rb_home)
    assert "steps" in result


def test_validation_checklist_requires_verify_and_rollback(rb_home):
    _write_runbook(
        rb_home, "deploy-no-rollback.yaml",
        {
            "name": "deploy-no-rollback", "title": "x", "env": "prod",
            "kind": "deploy", "checklist": True,
            "steps": [{"id": "preflight", "commands": ["ls"]}],
        },
    )
    result = runbook_load(runbook="deploy-no-rollback", home=rb_home)
    assert "verify" in result and "rollback" in result


def test_deploy_runbook_defaults_to_checklist(rb_home):
    """OPS-DELTA #32：kind=deploy 缺省 checklist=true（部署阶段门强制）。"""
    _write_runbook(
        rb_home, "deploy-implicit.yaml",
        {
            "name": "deploy-implicit", "title": "x", "env": "prod",
            "kind": "deploy",  # 无 checklist 键 → 默认 checklist=true
            "steps": [
                {"id": "preflight", "commands": ["ls"]},
                {"id": "verify", "commands": ["curl -sf http://x/healthz"],
                 "verify": "curl -sf http://x/healthz", "expect": "HTTP 200"},
            ],
            "rollback": [{"title": "回滚", "commands": ["kubectl rollout undo deploy/x"]}],
        },
    )
    loaded = _load(runbook_load(runbook="deploy-implicit", home=rb_home))
    # kind=deploy + 无 checklist 键 → 按 checklist 处理（阶段门状态随 load 返回）
    assert loaded["checklist_state"] is not None

    # 未过前置直接推进 → 阶段门拒绝（部署路径强制过阶段门）
    result = runbook_checkpoint(
        runbook="deploy-implicit", step_id="verify", status="pass", home=rb_home
    )
    assert "前置步骤未全部通过" in result


def test_deploy_runbook_explicit_checklist_false_stays_off(rb_home):
    """显式 checklist: false 仍可关闭（默认不覆盖用户声明）。"""
    _write_runbook(
        rb_home, "deploy-explicit-off.yaml",
        {
            "name": "deploy-explicit-off", "title": "x", "env": "test",
            "kind": "deploy", "checklist": False,
            "steps": [{"id": "s1", "commands": ["ls"]}],
        },
    )
    loaded = _load(runbook_load(runbook="deploy-explicit-off", home=rb_home))
    assert loaded["checklist"] is False
    assert loaded["checklist_state"] is None


def test_checkpoint_enforces_phase_order(rb_home):
    # 未过前置直接推进 → 阶段门拒绝
    result = runbook_checkpoint(
        runbook="deploy-gateway-svc", step_id="deploy", status="pass", home=rb_home
    )
    assert "前置步骤未全部通过" in result

    # 按序推进
    result = _load(runbook_checkpoint(
        runbook="deploy-gateway-svc", step_id="preflight", status="pass",
        evidence="replicas 正常", home=rb_home,
    ))
    assert result["checklist_state"]["preflight"]["status"] == "pass"

    result = _load(runbook_checkpoint(
        runbook="deploy-gateway-svc", step_id="deploy", status="pass",
        evidence="set image 完成", home=rb_home,
    ))
    assert result["recorded"] == "deploy"

    # 状态出现在 runbook_load 里
    loaded = _load(runbook_load(runbook="deploy-gateway-svc", home=rb_home))
    assert loaded["checklist_state"]["preflight"]["status"] == "pass"
    assert loaded["checklist_state"]["deploy"]["status"] == "pass"


def test_checkpoint_reset_starts_new_run(rb_home):
    runbook_checkpoint(
        runbook="deploy-gateway-svc", step_id="preflight", status="pass", home=rb_home
    )
    result = _load(runbook_checkpoint(
        runbook="deploy-gateway-svc", step_id="preflight", status="pass",
        reset=True, home=rb_home,
    ))
    assert list(result["checklist_state"]) == ["preflight"]


def test_checkpoint_rejects_non_checklist_and_bad_args(rb_home):
    result = runbook_checkpoint(
        runbook="harbor-restart", step_id="restart", status="pass", home=rb_home
    )
    assert "不是 checklist" in result

    result = runbook_checkpoint(
        runbook="deploy-gateway-svc", step_id="nope", status="pass", home=rb_home
    )
    assert "没有步骤" in result

    result = runbook_checkpoint(
        runbook="deploy-gateway-svc", step_id="preflight", status="maybe", home=rb_home
    )
    assert "pass/fail" in result


def test_env_mismatch_warning(rb_home):
    _write_config(rb_home, {"ops": {"permissions": {"enabled": True, "env": "test", "role": "test"}}})
    result = _load(runbook_load(runbook="harbor-restart", home=rb_home))
    assert result["session_env"] == "test"
    assert result["env_mismatch"] is True
    assert "跨环境操作默认拒绝" in result["env_warning"]


def test_check_runbook_requirements_data_existence_gating(rb_home):
    """OPS-DELTA #1：runbook 工具默认按数据存在性可用，enabled 降级为显式覆盖。"""
    import hermes_cli.config as hc

    # 数据就位 + 无 config（默认加载）→ 可用
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        assert check_runbook_requirements() is True
    finally:
        hc._LOAD_CONFIG_CACHE.clear()

    # 显式 enabled: false + 数据在 → 仍关闭（向后兼容）
    _write_config(rb_home, {"ops": {"runbooks": {"enabled": False}}})
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        assert check_runbook_requirements() is False
    finally:
        hc._LOAD_CONFIG_CACHE.clear()

    # 显式 enabled: true 但数据缺失 → 不可用（工具无数据只会报错）
    _write_config(rb_home, {"ops": {"runbooks": {"enabled": True}}})
    for p in rb_home.glob("runbooks/*.yaml"):
        p.unlink()
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        assert check_runbook_requirements() is False
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def test_check_runbook_requirements_no_data(tmp_path, monkeypatch):
    """无 runbooks/ 目录且无 config → 不可用（数据缺失，工具隐藏 + banner 引导）。"""
    import hermes_cli.config as hc

    home = tmp_path / "empty_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        assert check_runbook_requirements() is False
    finally:
        hc._LOAD_CONFIG_CACHE.clear()

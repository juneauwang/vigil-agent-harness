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
    _validate_runbook,
    check_runbook_requirements,
    runbook_checkpoint,
    runbook_load,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_RUNBOOKS = PROJECT_ROOT / "hermes_cli" / "ops_samples" / "runbooks"
SAMPLE_DIR_TOPOLOGY = PROJECT_ROOT / "hermes_cli" / "ops_samples" / "topology.yaml"
SAMPLE_SERVICES = PROJECT_ROOT / "hermes_cli" / "ops_samples" / "services"


@pytest.fixture
def rb_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "runbooks").mkdir(parents=True)
    for src in sorted(SAMPLE_RUNBOOKS.glob("*.yaml")):
        shutil.copy2(src, home / "runbooks" / src.name)
    monkeypatch.setenv("VIGIL_HOME", str(home))
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
    assert result["count"] == 4
    names = {r["name"] for r in result["runbooks"]}
    assert names == {
        "harbor-restart", "gateway-svc-restart", "deploy-gateway-svc",
        "argocd-server-check-restart",
    }


def test_v02_sample_validates_against_ops_topo(tmp_path, monkeypatch):
    """batch75：ops_samples 新增的 v0.2 样例必须能过分层校验 + load 回读。

    用 ops_samples 的 topology/services 铺 home（模拟新装种子环境），引用层
    （cluster k3s-prod + 服务 argocd 必须存在）一并验证。
    """
    home = tmp_path / "seed"
    (home / "runbooks").mkdir(parents=True)
    (home / "services").mkdir(parents=True)
    shutil.copy2(SAMPLE_DIR_TOPOLOGY, home / "topology.yaml")
    for p in sorted(SAMPLE_SERVICES.glob("*.yaml")):
        shutil.copy2(p, home / "services" / p.name)
    src = SAMPLE_RUNBOOKS / "argocd-server-check-restart.yaml"
    shutil.copy2(src, home / "runbooks" / src.name)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        data = yaml.safe_load(src.read_text(encoding="utf-8"))
        _validate_runbook(data, "argocd-server-check-restart", home)
        loaded = _load(runbook_load(runbook="argocd-server-check-restart", home=home))
        assert loaded["name"] == "argocd-server-check-restart"
        assert loaded["version"] == 2
        assert "v0.2 runbook" in loaded["note"]
        assert all("action" in s for s in loaded["steps"])
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def test_load_unknown_and_empty_dir(tmp_path, monkeypatch):
    home = tmp_path / "empty"
    (home / "runbooks").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
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
    assert "由命令级权限矩阵逐条判定" in result["env_warning"]


def test_load_legacy_env_uat_kept_and_mapped_for_mismatch(rb_home):
    """批二十七：老值 env=uat 的 runbook 可加载，data["env"] 保留原值 "uat"；
    env_mismatch 按映射后档位比较（uat→prod，会话 env=prod 时不误报）。"""
    _write_runbook(
        rb_home, "legacy-uat.yaml",
        {
            "name": "legacy-uat", "title": "x", "env": "uat", "kind": "incident",
            "steps": [{"id": "s1", "title": "x", "commands": ["echo ok"]}],
        },
    )
    _write_config(rb_home, {"ops": {"permissions": {"enabled": True, "env": "prod", "role": "operator"}}})

    loaded = _load(runbook_load(runbook="legacy-uat", home=rb_home))
    assert loaded["env"] == "uat"
    assert loaded["session_env"] == "prod"
    assert loaded.get("env_mismatch") is not True

    # 会话在 test 档时按映射后档位判断：prod 档 runbook 与 test 会话不匹配。
    _write_config(rb_home, {"ops": {"permissions": {"enabled": True, "env": "test", "role": "operator"}}})
    mismatched = _load(runbook_load(runbook="legacy-uat", home=rb_home))
    assert mismatched["env"] == "uat"
    assert mismatched["env_mismatch"] is True


def test_load_rejects_undefined_env_with_new_enum_message(rb_home):
    """批二十七：未声明名（sandbox）仍拒绝，错误消息含新四值枚举 + 老值映射提示。"""
    _write_runbook(
        rb_home, "bad-env.yaml",
        {
            "name": "bad-env", "title": "x", "env": "sandbox", "kind": "incident",
            "steps": [{"id": "s1", "title": "x", "commands": ["echo ok"]}],
        },
    )
    result = runbook_load(runbook="bad-env", home=rb_home)
    assert "local/test/dev/prod" in result
    assert "uat/staging" in result
    assert "test/uat/prod" not in result


def test_validate_runbook_env_accepts_four_values_and_legacy(rb_home):
    """批二十七：_validate_runbook 直调——四值 + uat/staging 合法，sandbox 抛错。"""
    from tools.runbook_tools import _validate_runbook

    base = {
        "name": "x", "title": "x", "kind": "incident",
        "steps": [{"id": "s1", "title": "x", "commands": ["echo ok"]}],
    }
    for env_val in ("local", "test", "dev", "prod", "uat", "staging"):
        data = dict(base)
        data["env"] = env_val
        _validate_runbook(data, "x")  # 不抛

    data = dict(base)
    data["env"] = "sandbox"
    with pytest.raises(ValueError) as exc:
        _validate_runbook(data, "x")
    assert "local/test/dev/prod" in str(exc.value)
    assert "test/uat/prod" not in str(exc.value)


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
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        assert check_runbook_requirements() is False
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def test_sibling_ops_profile_no_longer_fallback(tmp_path, monkeypatch):
    """OPS-DELTA #14 回退删除：default 无 runbooks 数据 + sibling ops profile
    有数据 → 不回退，工具不可用且提示"数据缺失"。"""
    import hermes_cli.config as hc

    root = tmp_path / "vigil_root"
    (root / "profiles" / "ops" / "runbooks").mkdir(parents=True)
    (root / "profiles" / "ops" / "runbooks" / "demo.yaml").write_text(
        "name: demo\ntitle: demo\nsteps:\n  - {id: s1, commands: [ls]}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGIL_HOME", str(root))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        assert check_runbook_requirements() is False
        result = _load(runbook_load("demo"))
        assert "runbooks 数据不存在" in result.get("error", "")
    finally:
        hc._LOAD_CONFIG_CACHE.clear()

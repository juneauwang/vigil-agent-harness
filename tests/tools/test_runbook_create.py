"""OPS-DELTA 批次二十五 — runbook_create 正规创建入口验收测试。

覆盖：正常创建 → 文件落盘 + runbook_load 回读、明文凭据拒绝 + vault 占位符
提示、同名未 overwrite 报错 / overwrite=True 更新、非法名称/空 steps 拒绝、
checklist(deploy) 规则复用、handler 透传、registry 注册。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as hc
from tools.runbook_tools import runbook_create, runbook_load

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_RUNBOOKS = PROJECT_ROOT / "hermes_cli" / "ops_samples" / "runbooks"


@pytest.fixture
def rb_home(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
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


def _incident_runbook(**overrides):
    data = {
        "runbook": "ansible-syntax-check",
        "title": "修改 ansible 后语法校验",
        "triggers": ["修改 ansible", "syntax check"],
        "summary": "改完 ansible playbook 后自动跑 syntax-check。",
        "env": "test",
        "kind": "incident",
        "steps": [
            {
                "id": "check",
                "title": "语法校验",
                "commands": [
                    "ansible-playbook --syntax-check -i inventory site.yml",
                ],
            }
        ],
    }
    data.update(overrides)
    return data


class TestRunbookCreate:
    def test_create_roundtrip_loadable(self, rb_home):
        result = _load(runbook_create(**_incident_runbook(), home=rb_home))
        assert result["status"] == "created"
        assert result["name"] == "ansible-syntax-check"
        assert result["steps"] == 1
        path = rb_home / "runbooks" / "ansible-syntax-check.yaml"
        assert path.is_file()

        loaded = _load(runbook_load(runbook="ansible-syntax-check", home=rb_home))
        assert loaded["name"] == "ansible-syntax-check"
        assert loaded["kind"] == "incident"
        assert "修改 ansible" in loaded["triggers"]
        assert "ansible-playbook --syntax-check" in loaded["steps"][0]["commands"][0]

    def test_fuzzy_match_via_triggers(self, rb_home):
        runbook_create(**_incident_runbook(), home=rb_home)
        result = _load(runbook_load(query="syntax check", home=rb_home))
        assert result["name"] == "ansible-syntax-check"

    def test_rejects_plaintext_password_in_steps(self, rb_home):
        data = _incident_runbook(
            steps=[{"id": "check", "title": "x",
                    "commands": ["curl -u admin:secret123 http://localhost/health"]}]
        )
        result = _load(runbook_create(**data, home=rb_home))
        assert "error" in result
        assert "明文凭据" in result["error"]
        assert "<vault:path/field>" in result["error"]
        # 拒绝时值不回显：错误消息不含 secret123。
        assert "secret123" not in result["error"]
        assert not (rb_home / "runbooks" / "ansible-syntax-check.yaml").exists()

    def test_rejects_plaintext_password_assignment_form(self, rb_home):
        data = _incident_runbook(
            steps=[{"id": "x", "title": "x", "commands": ["mysql -e 'SELECT 1' --password=abc"]}]
        )
        result = _load(runbook_create(**data, home=rb_home))
        assert "error" in result
        assert "password" in result["error"]

    def test_vault_placeholder_allowed(self, rb_home):
        data = _incident_runbook(
            steps=[{"id": "x", "title": "x",
                    "commands": ["curl -u admin:<vault:ansible/pass> http://localhost/health"]}]
        )
        result = _load(runbook_create(**data, home=rb_home))
        assert result["status"] == "created"

    def test_port_flag_not_treated_as_secret(self, rb_home):
        data = _incident_runbook(
            steps=[{"id": "x", "title": "x",
                    "commands": ["docker run -d -p 8080:80 nginx"]}]
        )
        result = _load(runbook_create(**data, home=rb_home))
        assert result["status"] == "created"

    @pytest.mark.parametrize("env_val", ["local", "test", "dev", "prod"])
    def test_create_env_four_values_roundtrip(self, rb_home, env_val):
        """批二十七：四值 local/test/dev/prod 均合法——create 落盘 + load 回读。"""
        data = _incident_runbook(env=env_val)
        result = _load(runbook_create(**data, home=rb_home))
        assert result["status"] == "created"

        path = rb_home / "runbooks" / "ansible-syntax-check.yaml"
        written = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert written["env"] == env_val

        loaded = _load(runbook_load(runbook="ansible-syntax-check", home=rb_home))
        assert loaded["env"] == env_val

    @pytest.mark.parametrize("legacy,expected", [("uat", "prod"), ("staging", "dev")])
    def test_create_legacy_env_maps_to_new_enum(self, rb_home, legacy, expected):
        """批二十七：create 传老值 uat/staging → 落盘映射后的新值（uat→prod、staging→dev）。"""
        data = _incident_runbook(env=legacy)
        result = _load(runbook_create(**data, home=rb_home))
        assert result["status"] == "created"

        path = rb_home / "runbooks" / "ansible-syntax-check.yaml"
        written = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert written["env"] == expected

        loaded = _load(runbook_load(runbook="ansible-syntax-check", home=rb_home))
        assert loaded["env"] == expected

    def test_create_rejects_undefined_env(self, rb_home):
        """批二十七：未声明名（sandbox）拒绝，错误消息含新枚举 + 老值映射提示。"""
        data = _incident_runbook(env="sandbox")
        result = _load(runbook_create(**data, home=rb_home))
        assert "error" in result
        assert "local/test/dev/prod" in result["error"]
        assert "uat/staging" in result["error"]
        # 文案不再出现旧三值枚举。
        assert "test/uat/prod" not in result["error"]
        assert not (rb_home / "runbooks" / "ansible-syntax-check.yaml").exists()

    def test_overwrite_requires_flag(self, rb_home):
        runbook_create(**_incident_runbook(), home=rb_home)
        result = _load(runbook_create(**_incident_runbook(), home=rb_home))
        assert "error" in result
        assert "已存在" in result["error"]

        result = _load(runbook_create(**_incident_runbook(
            summary="更新版摘要",
        ), overwrite=True, home=rb_home))
        assert result["status"] == "updated"
        loaded = yaml.safe_load(
            (rb_home / "runbooks" / "ansible-syntax-check.yaml").read_text(encoding="utf-8")
        )
        assert loaded["summary"] == "更新版摘要"

    def test_invalid_name_rejected(self, rb_home):
        for bad in ("My Runbook", "..", ".hidden", "with_underscore", "UPPER"):
            result = _load(runbook_create(**_incident_runbook(runbook=bad), home=rb_home))
            assert "error" in result, bad
            assert "kebab-case" in result["error"]

    def test_empty_steps_rejected(self, rb_home):
        result = _load(runbook_create(**_incident_runbook(steps=[]), home=rb_home))
        assert "error" in result
        assert "steps" in result["error"]

    def test_deploy_kind_requires_rollback_and_verify(self, rb_home):
        # kind=deploy 复用既有 checklist 校验：无 verify+expect / 无 rollback → 拒绝。
        data = _incident_runbook(kind="deploy")
        result = _load(runbook_create(**data, home=rb_home))
        assert "error" in result
        assert "checklist" in result["error"] or "rollback" in result["error"]
        # 补全后可通过。
        data = _incident_runbook(
            kind="deploy",
            steps=[
                {"id": "preflight", "title": "x", "commands": ["kubectl get deploy"],
                 "verify": "kubectl get deploy", "expect": "ready"},
            ],
            rollback=[{"title": "回滚", "commands": ["kubectl rollout undo deploy/x"]}],
        )
        result = _load(runbook_create(**data, home=rb_home))
        assert result["status"] == "created"

    def test_handler_passes_args(self, rb_home, monkeypatch):
        import tools.runbook_tools as rt

        captured = {}

        def fake_create(**kw):
            captured.update(kw)
            return "{}"

        monkeypatch.setattr(rt, "runbook_create", fake_create)
        rt._create_handler({
            "runbook": "x", "title": "t", "steps": [{"id": "a", "commands": ["echo hi"]}],
            "overwrite": True, "kind": "incident",
        })
        assert captured["runbook"] == "x"
        assert captured["overwrite"] is True
        assert captured["steps"] == [{"id": "a", "commands": ["echo hi"]}]

    def test_registered_in_runbook_toolset_and_schema(self):
        from tools.registry import registry
        from tools.runbook_tools import _DEFAULT_CREATE_SCHEMA

        entry = registry.get_entry("runbook_create")
        assert entry is not None
        assert entry.toolset == "runbook"
        assert "runbook_create" in registry.get_tool_names_for_toolset("runbook")
        required = set(_DEFAULT_CREATE_SCHEMA["parameters"]["required"])
        assert {"runbook", "title", "steps"} <= required
        assert "overwrite" in _DEFAULT_CREATE_SCHEMA["parameters"]["properties"]
        # 描述含专有名词绑定 + 凭据拒绝语义。
        assert "不是 Markdown 文档" in _DEFAULT_CREATE_SCHEMA["description"]
        assert "<vault:path/field>" in _DEFAULT_CREATE_SCHEMA["description"]

    def test_load_tail_offers_update(self, rb_home):
        """任务 3：runbook_load 返回尾部引导——执行有改进时提议更新 runbook。"""
        runbook_create(**_incident_runbook(), home=rb_home)
        result = _load(runbook_load(runbook="ansible-syntax-check", home=rb_home))
        assert "提议更新" in result["note"]
        assert "runbook_create overwrite=true" in result["note"]

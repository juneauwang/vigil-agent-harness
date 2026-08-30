"""YAPL P3（批次五十四）：操作矩阵层验收测试。

覆盖（任务书 §8 后端清单）：
  - 加载/校验：无 deny 拒绝（设计铁律）、动作枚举、漏配默认 approve+warning、
    {approve: required} 形态（其余 dict 形态拒绝）；
  - 热生效：改文件即生效不重启（任何读取路径实时读 matrix.yaml 不缓存）；
  - 模板 1/2/3 生成断言（逐格核对设计 11.3 严格度阶梯）+ 模板 4 级联；
  - set/edit/reset CLI（reset 回退模板、来源追踪 manual）；
  - matrix_query 只读 tool（无写接口、查档位、漏配默认 approve）；
  - 资产审批：runbook v0.2 创建走审批门 → 通过落盘带预审标记 / 拒绝不落盘；
    approvals.mode smart/manual/off 三态 + {approve: required} 强制人工覆盖
    mode；v0.1 不受影响；
  - 审计：CLI/UI 修改均产生 trajectory matrix_change 事件；
  - web 端点：GET/PUT /api/matrix + POST /api/matrix/init（_require_token
    保护、不进 PUBLIC_API_PATHS）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as hc
from hermes_cli.subcommands import matrix as matrix_cli
from tools import terminal_tool
from tools.approval import (
    reset_current_session_key,
    reset_hermes_interactive_context,
    set_current_session_key,
    set_hermes_interactive_context,
)
from tools import matrix_data as md
from tools.matrix_tools import matrix_query
from tools.runbook_tools import runbook_create, runbook_load

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mhome(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME；config approvals.mode=manual（资产审批人工门默认）。"""
    home = tmp_path / "vigil_home"
    home.mkdir(parents=True)
    (home / "runbooks").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    yield home
    hc._LOAD_CONFIG_CACHE.clear()


def _write_matrix(home: Path, matrix: dict, *, source: str = "template2",
                  base_template: str = "template2", sources: dict | None = None) -> Path:
    path = home / "matrix.yaml"
    path.write_text(yaml.safe_dump({
        "schema_version": 1,
        "updated_at": "2026-08-23T00:00:00+08:00",
        "source": source,
        "base_template": base_template,
        "matrix": matrix,
        "sources": sources or {
            env: {act: source for act in cells} for env, cells in matrix.items()
        },
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _cli(argv):
    """跑独立 CLI（hermes_cli/subcommands/matrix.main），返回退出码。"""
    return matrix_cli.main(argv)


def _run_in_thread(cb, fn):
    """交互上下文 + 审批回调在线程内绑定（照 chat worker），隔离阻塞等待。"""
    seen = {}
    def _runner():
        t1 = set_hermes_interactive_context(True)
        t2 = set_current_session_key("sess-" + str(time.time_ns()))
        terminal_tool.set_approval_callback(cb)
        try:
            seen["result"] = fn()
        finally:
            terminal_tool.set_approval_callback(None)
            reset_current_session_key(t2)
            reset_hermes_interactive_context(t1)
    t = threading.Thread(target=_runner)
    t.start()
    t.join(timeout=20)
    return seen.get("result")


def _approve_cb(seen: dict | None = None):
    def _cb(command, description, *, allow_permanent=True, allow_session=True,
            smart_denied=False):
        if seen is not None:
            seen["allow_permanent"] = allow_permanent
            seen["allow_session"] = allow_session
        return "once"
    return _cb


def _deny_cb(command, description, *, allow_permanent=True, allow_session=True,
             smart_denied=False):
    return "deny"


def _set_mode(home: Path, mode: str) -> None:
    (home / "config.yaml").write_text(
        yaml.safe_dump({"approvals": {"mode": mode}}), encoding="utf-8")
    hc._LOAD_CONFIG_CACHE.clear()


# ---------------------------------------------------------------------------
# 加载/校验
# ---------------------------------------------------------------------------

class TestMatrixLoad:
    def test_deny_rejected(self, mhome):
        """设计铁律：矩阵无 deny——字符串 deny/denied/block 一律报错。"""
        for bad in ("deny", "denied", "block", "blocked", "forbidden"):
            path = _write_matrix(mhome, {"prod": {"restart": bad}})
            with pytest.raises(md.MatrixValidationError) as exc:
                md.load_matrix(mhome)
            assert "矩阵无 deny" in exc.value.errors[0]
            assert path.is_file()

    def test_required_dict_form_accepted_other_dicts_rejected(self, mhome):
        _write_matrix(mhome, {"prod": {"restart": {"approve": "required"}}})
        data = md.load_matrix(mhome)
        assert data["matrix"]["prod"]["restart"] == "required"

        _write_matrix(mhome, {"prod": {"restart": {"approve": "always"}}})
        with pytest.raises(md.MatrixValidationError) as exc:
            md.load_matrix(mhome)
        assert "仅接受" in exc.value.errors[0]

        _write_matrix(mhome, {"prod": {"restart": {"required": True}}})
        with pytest.raises(md.MatrixValidationError):
            md.load_matrix(mhome)

    def test_unknown_action_rejected_with_legal_list(self, mhome):
        _write_matrix(mhome, {"prod": {"frobnicate": "execute"}})
        with pytest.raises(md.MatrixValidationError) as exc:
            md.load_matrix(mhome)
        assert "frobnicate" in exc.value.errors[0]
        assert "合法动作" in exc.value.errors[0]
        assert "restart" in exc.value.errors[0]

    def test_bad_level_rejected(self, mhome):
        _write_matrix(mhome, {"prod": {"restart": "always"}})
        with pytest.raises(md.MatrixValidationError) as exc:
            md.load_matrix(mhome)
        assert "execute / approve" in exc.value.errors[0]

    def test_missing_cells_default_approve_with_warning(self, mhome):
        _write_matrix(mhome, {"prod": {"query": "execute"}})
        data = md.load_matrix(mhome)
        assert data["matrix"]["prod"]["query"] == "execute"
        assert any("漏配" in w for w in data["warnings"])
        level = md.get_level(data, "prod", "restart")
        assert level["level"] == "approve" and level["configured"] is False

    def test_missing_file_empty_matrix_warning(self, mhome):
        data = md.load_matrix_or_empty(mhome)
        assert data["matrix"] == {}
        assert any("不存在" in w for w in data["warnings"])
        level = md.get_level(data, "prod", "restart")
        assert level["level"] == "approve"

    def test_source_tracking_roundtrip(self, mhome):
        _write_matrix(mhome, {"prod": {"query": "execute"}},
                      sources={"prod": {"query": "manual"}})
        data = md.load_matrix(mhome)
        assert data["sources"]["prod"]["query"] == "manual"


# ---------------------------------------------------------------------------
# 热生效
# ---------------------------------------------------------------------------

class TestHotReload:
    def test_file_change_takes_effect_without_restart(self, mhome):
        _write_matrix(mhome, {"prod": {"restart": "execute"}})
        assert md.get_level(md.load_matrix(mhome), "prod", "restart")["level"] == "execute"
        # 改文件（模拟人工编辑）→ 不重启直接读新值
        _write_matrix(mhome, {"prod": {"restart": {"approve": "required"}}})
        assert md.get_level(md.load_matrix(mhome), "prod", "restart")["level"] == "required"
        # 再改一次，确认无缓存残留
        _write_matrix(mhome, {"prod": {"restart": "approve"}})
        assert md.get_level(md.load_matrix(mhome), "prod", "restart")["level"] == "approve"


# ---------------------------------------------------------------------------
# 四模板（§11.3 逐格核对）
# ---------------------------------------------------------------------------

class TestTemplates:
    def test_template1_solo(self, mhome):
        t = md.template_matrix("template1")
        assert t["envs"] == ["local"]
        cells = t["matrix"]["local"]
        assert len(cells) == 23
        for act, level in cells.items():
            if act in ("reboot", "shutdown", "remove", "decommission"):
                assert level == "approve", act
            else:
                assert level == "execute", act

    def test_template2_small_team(self, mhome):
        t = md.template_matrix("template2")
        assert t["envs"] == ["local", "test", "dev", "prod"]
        local = t["matrix"]["local"]
        test = t["matrix"]["test"]
        dev = t["matrix"]["dev"]
        prod = t["matrix"]["prod"]

        # local = 模板 1
        for act, level in local.items():
            if act in ("reboot", "shutdown", "remove", "decommission"):
                assert level == "approve"
            else:
                assert level == "execute"

        # dev：approve = 高危 4 + restart/start/stop/reload/upgrade（9 个）
        dev_approve = {a for a, l in dev.items() if l == "approve"}
        assert dev_approve == {
            "reboot", "shutdown", "remove", "decommission",
            "restart", "start", "stop", "reload", "upgrade",
        }
        assert all(l == "execute" for a, l in dev.items() if a not in dev_approve)

        # test = dev 档（batch83 任务 4：模板补 test 段，档位参考 dev）
        assert test == dev

        # prod：execute 仅查询 3；approve = apply_config/backup/transfer_file/
        # enable/disable；其余 15 个强制人工
        prod_execute = {a for a, l in prod.items() if l == "execute"}
        assert prod_execute == {"query", "fetch_log", "verify"}
        prod_approve = {a for a, l in prod.items() if l == "approve"}
        assert prod_approve == {"apply_config", "backup", "transfer_file", "enable", "disable"}
        prod_required = {a for a, l in prod.items() if l == "required"}
        assert prod_required == {
            "restart", "start", "stop", "reload", "upgrade",
            "install", "scale", "run_script", "deploy", "rollback", "restore",
            "reboot", "shutdown", "remove", "decommission",
        }
        assert len(prod_execute) + len(prod_approve) + len(prod_required) == 23

    def test_template3_medium_team(self, mhome):
        t3 = md.template_matrix("template3")
        assert t3["envs"] == ["local", "test", "uat", "dev", "prod"]
        t2 = md.template_matrix("template2")
        assert t3["matrix"]["local"] == t2["matrix"]["local"]
        assert t3["matrix"]["test"] == t2["matrix"]["test"]
        assert t3["matrix"]["uat"] == t2["matrix"]["dev"]
        assert t3["matrix"]["dev"] == t2["matrix"]["prod"]
        prod = t3["matrix"]["prod"]
        assert {a for a, l in prod.items() if l == "execute"} == {"query", "fetch_log", "verify"}
        assert all(l == "required" for a, l in prod.items() if a not in {"query", "fetch_log", "verify"})

    def test_template4_cascade(self, mhome):
        t4 = md.template_matrix("template4", selections={
            "execute": ["query", "fetch_log", "verify"],
            "approve": ["backup", "restore"],
        })
        for env, cells in t4["matrix"].items():
            assert cells["query"] == "execute" and cells["backup"] == "approve"
            assert cells["reboot"] == "required"
            assert cells["restart"] == "required"

        # 级联语义：execute 与 approve 不得重叠
        with pytest.raises(ValueError, match="不得重叠"):
            md.template_matrix("template4", selections={
                "execute": ["query"], "approve": ["query"]})
        # 未知动作拒绝
        with pytest.raises(ValueError, match="不在词表"):
            md.template_matrix("template4", selections={
                "execute": ["frobnicate"], "approve": []})

    def test_init_marks_sources_template(self, mhome):
        data = md.init_matrix("template2", home=mhome)
        assert data["source"] == "template2" and data["base_template"] == "template2"
        assert data["sources"]["prod"]["restart"] == "template2"
        # 已存在防误覆盖
        with pytest.raises(FileExistsError):
            md.init_matrix("template2", home=mhome)


# ---------------------------------------------------------------------------
# CLI（show 之外三命令 + 审计）
# ---------------------------------------------------------------------------

class TestCLI:
    def test_init_show_set_reset_flow(self, mhome, capsys):
        assert _cli(["init", "--template", "2"]) == 0
        assert (mhome / "matrix.yaml").is_file()
        capsys.readouterr()  # 消费 init 输出，show 单独解析

        assert _cli(["show", "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["source"] == "template2"
        assert out["matrix"]["prod"]["query"] == "execute"
        assert out["matrix"]["prod"]["restart"] == "required"
        assert out["sources"]["prod"]["restart"] == "template2"

        # set 单格 → 该格来源 manual、顶层 source manual
        assert _cli(["set", "restart", "prod", "execute"]) == 0
        data = md.load_matrix(mhome)
        assert data["source"] == "manual"
        assert data["sources"]["prod"]["restart"] == "manual"
        assert data["matrix"]["prod"]["restart"] == "execute"

        # reset → 回退模板，来源全部恢复
        assert _cli(["reset"]) == 0
        data = md.load_matrix(mhome)
        assert data["source"] == "template2"
        assert data["sources"]["prod"]["restart"] == "template2"
        assert data["matrix"]["prod"]["restart"] == "required"

    def test_set_invalid_action_env_level(self, mhome):
        _cli(["init", "--template", "2"])
        assert _cli(["set", "frobnicate", "prod", "execute"]) == 1
        assert _cli(["set", "restart", "", "execute"]) == 1
        with pytest.raises(SystemExit):  # argparse choices 非法 → SystemExit(2)
            _cli(["set", "restart", "prod", "deny"])
        assert (mhome / "matrix.yaml").is_file()

    def test_init_force_and_exists_refusal(self, mhome):
        assert _cli(["init", "--template", "2"]) == 0
        assert _cli(["init", "--template", "3"]) == 1  # 已存在拒绝
        assert _cli(["init", "--template", "3", "--force"]) == 0
        data = md.load_matrix(mhome)
        assert data["base_template"] == "template3"
        assert data["matrix"]["uat"]["restart"] == "approve"

    def test_edit_bulk_with_validation(self, mhome):
        _cli(["init", "--template", "2"])
        # 合法批量编辑：prod.restart 改 execute（其余不变）→ 该格来源 manual
        edited = md.load_matrix(mhome)
        edited["matrix"]["prod"]["restart"] = "execute"
        edited["sources"]["prod"]["restart"] = "template2"
        result = md.edit_matrix(mhome, content=md.render_yaml(edited))
        assert result["errors"] == [] and result["changed_cells"] == 1
        data = md.load_matrix(mhome)
        assert data["source"] == "manual"
        assert data["sources"]["prod"]["restart"] == "manual"
        # 校验失败不落盘：deny 语义
        before = (mhome / "matrix.yaml").read_text()
        result = md.edit_matrix(mhome, content=yaml.safe_dump({
            "schema_version": 1, "matrix": {"prod": {"restart": "deny"}}}))
        assert result["errors"] and "矩阵无 deny" in result["errors"][0]
        assert (mhome / "matrix.yaml").read_text() == before

    def test_cli_audit_events(self, mhome):
        _cli(["init", "--template", "2"])
        _cli(["set", "restart", "prod", "execute"])
        _cli(["reset"])
        events = [json.loads(l) for l in
                  (mhome / "trajectory" / "matrix-cli.jsonl").read_text().splitlines()]
        assert len(events) == 3
        types = [e["type"] for e in events]
        assert types == ["matrix_change"] * 3
        assert events[1]["meta"]["env"] == "prod"
        assert events[1]["meta"]["action"] == "restart"
        assert events[1]["meta"]["source"] == "cli"
        assert events[1]["meta"]["old"] == "required"
        assert events[1]["meta"]["new"] == "execute"
        assert events[1]["meta"]["operator"]


# ---------------------------------------------------------------------------
# matrix_query 只读 tool
# ---------------------------------------------------------------------------

class TestMatrixQuery:
    def test_query_level_and_source(self, mhome):
        _write_matrix(mhome, {"prod": {"restart": {"approve": "required"}, "query": "execute"}},
                      sources={"prod": {"restart": "template2", "query": "template2"}})
        out = json.loads(matrix_query("restart", "prod", home=mhome))
        assert out["level"] == "required" and out["configured"] is True
        assert out["source"] == "template2"
        out = json.loads(matrix_query("query", "prod", home=mhome))
        assert out["level"] == "execute"

    def test_missing_default_approve_with_hint(self, mhome):
        _write_matrix(mhome, {"prod": {"query": "execute"}})
        out = json.loads(matrix_query("restart", "prod", home=mhome))
        assert out["level"] == "approve" and out["configured"] is False
        assert "默认 approve" in out["note"]
        assert "LLM 无 set 路径" in out["note"]

    def test_unknown_action_and_missing_env(self, mhome):
        out = json.loads(matrix_query("frobnicate", "prod", home=mhome))
        assert "error" in out and "合法动作" in out["error"]
        out = json.loads(matrix_query("restart", "", home=mhome))
        assert "error" in out and "env 必填" in out["error"]

    def test_readonly_no_file_change(self, mhome):
        _write_matrix(mhome, {"prod": {"restart": "execute"}})
        before = (mhome / "matrix.yaml").read_text()
        mtime = (mhome / "matrix.yaml").stat().st_mtime_ns
        matrix_query("restart", "prod", home=mhome)
        matrix_query("query", "dev", home=mhome)
        assert (mhome / "matrix.yaml").read_text() == before
        assert (mhome / "matrix.yaml").stat().st_mtime_ns == mtime

    def test_toolset_registered_readonly(self, mhome):
        from tools.registry import registry
        entry = registry.get_entry("matrix_query")
        assert entry is not None and entry.toolset == "matrix"
        assert entry.check_fn()
        # 只读声明：schema 描述明确 LLM 无 set 路径
        assert "无 set 路径" in entry.schema["description"]


# ---------------------------------------------------------------------------
# 资产审批（§11.4 双审批层次）
# ---------------------------------------------------------------------------

def _rb(runbook="test-rb", action="restart", env="prod", **kw):
    data = {
        "runbook": runbook,
        "title": "测试 runbook",
        "env": env,
        "kind": "maintenance",
        "steps": [{"id": "s1", "title": "x", "action": action,
                   "params": {"target": "nginx"}}],
    }
    data.update(kw)
    return data


def _matrix_with_required(home: Path):
    _write_matrix(home, {
        "prod": {"restart": {"approve": "required"}, "query": "execute"},
    }, sources={
        "prod": {"restart": "template2", "query": "template2"},
    })


class TestAssetApproval:
    def test_v2_approved_writes_markers(self, mhome):
        _matrix_with_required(mhome)
        result = _run_in_thread(_approve_cb(), lambda: runbook_create(**_rb(), home=mhome))
        out = json.loads(result)
        assert out["status"] == "created"
        written = yaml.safe_load((mhome / "runbooks" / "test-rb.yaml").read_text())
        assert written["approved_at"] and written["approved_by"] and written["approved_version"]
        # 内容哈希：16 位 hex
        assert len(written["approved_version"]) == 16
        # runbook_load 校验通过（预审标记字段不破坏 schema）
        loaded = json.loads(runbook_load(runbook="test-rb", home=mhome))
        assert loaded["name"] == "test-rb"

    def test_v2_denied_not_written(self, mhome):
        _matrix_with_required(mhome)
        result = _run_in_thread(_deny_cb, lambda: runbook_create(**_rb(), home=mhome))
        out = json.loads(result)
        assert out.get("status") is None
        assert "未落盘" in out["error"]
        assert not (mhome / "runbooks" / "test-rb.yaml").exists()

    def test_force_manual_hides_allowlists(self, mhome):
        """{approve: required} 高危动作 → 强制人工：不提供 session/永久选项。"""
        _matrix_with_required(mhome)
        seen: dict = {}
        result = _run_in_thread(_approve_cb(seen), lambda: runbook_create(**_rb(), home=mhome))
        assert json.loads(result)["status"] == "created"
        assert seen["allow_permanent"] is False
        assert seen["allow_session"] is False

    def test_smart_mode_all_execute_auto_approves(self, mhome):
        """approvals.mode=smart + 全部动作矩阵判 execute → 自动批准，无人工门。"""
        _write_matrix(mhome, {"prod": {"query": "execute"}},
                      sources={"prod": {"query": "template2"}})
        _set_mode(mhome, "smart")
        result = runbook_create(**_rb(action="query"), home=mhome)  # 无交互上下文
        out = json.loads(result)
        assert out["status"] == "created"
        written = yaml.safe_load((mhome / "runbooks" / "test-rb.yaml").read_text())
        assert written["approved_by"] == "smart(matrix=all-execute)"

    def test_smart_mode_with_required_still_human(self, mhome):
        """smart 模式遇 {approve: required} → 强制人工（高危不 smart）。"""
        _matrix_with_required(mhome)
        _set_mode(mhome, "smart")
        seen: dict = {}
        result = _run_in_thread(_approve_cb(seen), lambda: runbook_create(**_rb(), home=mhome))
        assert json.loads(result)["status"] == "created"
        assert seen["allow_permanent"] is False

    def test_mode_off_skips_unless_required(self, mhome):
        """approvals.mode=off：非强制人工跳过；{approve: required} 仍强制人工。"""
        _write_matrix(mhome, {"prod": {"query": "execute"}},
                      sources={"prod": {"query": "template2"}})
        _set_mode(mhome, "off")
        result = runbook_create(**_rb(action="query"), home=mhome)
        assert json.loads(result)["status"] == "created"

        _matrix_with_required(mhome)
        seen: dict = {}
        result = _run_in_thread(_approve_cb(seen), lambda: runbook_create(**_rb(runbook="test-rb-off"), home=mhome))
        assert json.loads(result)["status"] == "created"
        assert seen["allow_permanent"] is False

    def test_no_human_fail_closed(self, mhome):
        """无人在场（裸脚本/cron）→ fail-closed BLOCK，不落盘。"""
        _matrix_with_required(mhome)
        result = runbook_create(**_rb(), home=mhome)  # 无交互上下文
        out = json.loads(result)
        assert out.get("status") is None
        assert "未落盘" in out["error"]
        assert "BLOCKED" in out["error"]

    def test_v1_unaffected(self, mhome):
        """v0.1 runbook 走原路径（batch74 起仅 overwrite 存量）：无资产审批、无预审标记。"""
        _matrix_with_required(mhome)
        (mhome / "runbooks" / "v1-rb.yaml").write_text(
            "name: v1-rb\ntitle: 存量\nversion: 1\nkind: incident\n"
            "steps:\n  - id: s1\n    title: x\n    commands: [echo old]\n",
            encoding="utf-8",
        )
        v1 = {"runbook": "v1-rb", "title": "v1", "kind": "incident",
              "steps": [{"id": "s1", "title": "x", "commands": ["echo hi"]}]}
        out = json.loads(runbook_create(**v1, overwrite=True, home=mhome))
        assert out.get("status") in ("created", "updated")
        written = yaml.safe_load((mhome / "runbooks" / "v1-rb.yaml").read_text())
        assert "approved_at" not in written
        assert "approved_version" not in written


# ---------------------------------------------------------------------------
# web 端点（GET/PUT /api/matrix + POST /api/matrix/init）
# ---------------------------------------------------------------------------

class TestWebMatrixApi:
    def test_get_matrix_requires_token(self, mhome):
        pytest.importorskip("starlette.testclient")
        from starlette.testclient import TestClient
        from hermes_cli import web_server
        client = TestClient(web_server.app)
        resp = client.get("/api/matrix")
        assert resp.status_code == 401

    def test_matrix_not_in_public_paths(self):
        from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS
        assert "/api/matrix" not in PUBLIC_API_PATHS
        assert "/api/matrix/init" not in PUBLIC_API_PATHS

    def test_get_put_init_flow(self, mhome):
        pytest.importorskip("starlette.testclient")
        from starlette.testclient import TestClient
        from hermes_cli import web_server
        previous = getattr(web_server.app.state, "auth_required", None)
        web_server.app.state.auth_required = False
        client = TestClient(web_server.app)
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        try:
            # init（模板 2）
            resp = client.post("/api/matrix/init", json={"template": "template2"})
            assert resp.status_code == 200, resp.text
            data = resp.json()["data"]
            assert data["source"] == "template2"
            assert data["matrix"]["prod"]["restart"] == "required"
            assert data["actions"] == md.actions()

            # GET 全量
            resp = client.get("/api/matrix")
            assert resp.status_code == 200
            assert resp.json()["data"]["matrix"]["prod"]["query"] == "execute"

            # PUT 单格 → 落盘 + 来源 manual
            resp = client.put("/api/matrix", json={"env": "prod", "action": "restart", "level": "execute"})
            assert resp.status_code == 200, resp.text
            assert resp.json()["changed"] is True
            data = md.load_matrix(mhome)
            assert data["matrix"]["prod"]["restart"] == "execute"
            assert data["sources"]["prod"]["restart"] == "manual"
            assert data["source"] == "manual"

            # PUT 非法 level / 未知 action → 400
            assert client.put("/api/matrix", json={"env": "prod", "action": "restart", "level": "deny"}).status_code == 400
            assert client.put("/api/matrix", json={"env": "prod", "action": "frobnicate", "level": "execute"}).status_code == 400

            # UI 审计事件落 trajectory/matrix-ui.jsonl
            events = [json.loads(l) for l in
                      (mhome / "trajectory" / "matrix-ui.jsonl").read_text().splitlines()]
            assert any(e["type"] == "matrix_change" and e["meta"]["source"] == "ui"
                       and e["meta"]["action"] == "restart" for e in events)
        finally:
            if previous is None:
                try:
                    delattr(web_server.app.state, "auth_required")
                except AttributeError:
                    pass
            else:
                web_server.app.state.auth_required = previous

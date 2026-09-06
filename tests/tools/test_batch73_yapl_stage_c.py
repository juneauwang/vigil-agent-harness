"""YAPL 主框架阶段 C（batch73，OPS-DELTA #88）：runbook 第 24 动作 + 嵌套编排 + 3 补丁。

覆盖（任务书 §7 后端清单）：
  - 校验器：runbook 动作步骤（ref 必填/type 枚举/ref 存在性/环状拒绝含环路路径/
    变量不跨层/范围四字段可选兼容）；
  - 执行器：嵌套执行（子步骤顺序/范围继承 inherited|declared/每步查矩阵无豁免/
    子失败子的 on_failure 先生效/父回滚联动/type=tool 调工具 execute 豁免/
    无范围单独运行 → 待收集/运行时深度兜底）；
  - 补丁：凭据纪律（password 拒绝）、topo_update credentials（增改删/非法拒绝/
    审计）、CLI 命令索引注入。
不真调 LLM（编译契约工具用预置 registry + 编译代码 fixture）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as hc
from tools import terminal_tool
from tools.runbook_exec import (
    execute_runbook,
    ledger_path,
)
from tools.topo_tools import topo_update
from tools.runbook_tools import _validate_runbook

TOPOLOGY = {
    "version": 4,
    "environments": [
        {"name": "local", "isolation": "relaxed", "role": "local"},
        {"name": "test", "isolation": "relaxed", "role": "test"},
        {"name": "prod", "isolation": "strict", "role": "prod"},
    ],
    "clusters": [
        {"name": "local", "env": "local", "type": "docker",
         "host_groups": ["workers"]},
        {"name": "k3s-dev", "env": "test", "type": "k3s", "host_groups": []},
    ],
    "hosts": [
        {"name": "node-a", "env": "local", "cluster": "local",
         "endpoint": "10.0.0.10", "credentials": []},
        {"name": "node-b", "env": "local", "cluster": "local",
         "endpoint": "10.0.0.11", "credentials": []},
        {"name": "dev-node", "env": "test", "cluster": "k3s-dev",
         "endpoint": "198.51.100.1", "credentials": []},
    ],
}


@pytest.fixture
def stage_home(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME：拓扑 v0.4 + 矩阵 template1 + runbook 动作 execute。"""
    home = tmp_path / "vigil_home"
    (home / "runbooks").mkdir(parents=True)
    (home / "services").mkdir(parents=True)
    (home / "entities").mkdir(parents=True)
    (home / "contracts" / "compiled").mkdir(parents=True)
    (home / "services" / "node-a.yaml").write_text(
        "host: node-a\nservices:\n- {name: web-svc, type: app, managed_by: docker}\n",
        encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(home))
    (home / "topology.yaml").write_text(
        yaml.safe_dump(TOPOLOGY, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    from tools.matrix_data import load_matrix, set_level, template_matrix, write_matrix
    m = template_matrix("template1")
    # runbook 动作不在 setup 四模板（漏配默认 approve，保守）——测试基座显式
    # 配 execute 让嵌套执行不触发审批；档位门行为由专门用例覆盖。
    set_level(m, "local", "runbook", "execute")
    set_level(m, "test", "runbook", "execute")
    write_matrix(m, home)
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def _write_rb(home: Path, data: dict) -> Path:
    path = home / "runbooks" / f"{data['name']}.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return path


def _query_step(step_id: str, pattern: str = "x", **kw) -> dict:
    step = {"id": step_id, "title": step_id, "action": "query",
            "params": {"pattern": pattern}}
    step.update(kw)
    return step


def _ok_runner(calls=None):
    def runner(spec, target):
        if calls is not None:
            calls.append(spec)
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}
    return runner


def _fail_runner(exit_code: int = 1):
    def runner(spec, target):
        return {"exit_code": exit_code, "stdout": "", "stderr": "boom"}
    return runner


def _ledger_rows(home: Path) -> list:
    path = ledger_path(home)
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 校验器：runbook 第 24 动作
# ---------------------------------------------------------------------------

class TestValidatorRunbookAction:
    def test_ref_required(self, stage_home):
        data = {"name": "p", "title": "P", "version": 2, "kind": "maintenance",
                "env": "local",
                "steps": [_query_step("s1"), {"id": "r", "title": "r",
                                              "action": "runbook",
                                              "params": {"type": "runbook"}}]}
        with pytest.raises(ValueError, match="ref"):
            _validate_runbook(data, "p", stage_home)

    def test_type_enum_required(self, stage_home):
        data = {"name": "p", "title": "P", "version": 2, "kind": "maintenance",
                "env": "local",
                "steps": [_query_step("s1"), {"id": "r", "title": "r",
                                              "action": "runbook",
                                              "params": {"ref": "child",
                                                         "type": "maybe"}}]}
        with pytest.raises(ValueError, match="runbook\\|tool"):
            _validate_runbook(data, "p", stage_home)

    def test_type_tool_requires_explicit_type(self, stage_home):
        # type 必填不猜：只给 ref 不给 type → 结构层拒绝。
        data = {"name": "p", "title": "P", "version": 2, "kind": "maintenance",
                "env": "local",
                "steps": [_query_step("s1"), {"id": "r", "title": "r",
                                              "action": "runbook",
                                              "params": {"ref": "child"}}]}
        with pytest.raises(ValueError, match="type"):
            _validate_runbook(data, "p", stage_home)

    def test_sub_runbook_missing_ref_guides_create(self, stage_home):
        data = {"name": "p", "title": "P", "version": 2, "kind": "maintenance",
                "env": "local",
                "steps": [_query_step("s1"), {"id": "r", "title": "r",
                                              "action": "runbook",
                                              "params": {"ref": "ghost",
                                                         "type": "runbook"}}]}
        with pytest.raises(ValueError, match="不存在.*runbook_create"):
            _validate_runbook(data, "p", stage_home)

    def test_self_reference_rejected(self, stage_home):
        data = {"name": "selfy", "title": "S", "version": 2, "kind": "maintenance",
                "env": "local",
                "steps": [_query_step("s1"), {"id": "r", "title": "r",
                                              "action": "runbook",
                                              "params": {"ref": "selfy",
                                                         "type": "runbook"}}]}
        with pytest.raises(ValueError, match="引用自己"):
            _validate_runbook(data, "selfy", stage_home)

    def test_cycle_rejected_with_path(self, stage_home):
        _write_rb(stage_home, {"name": "a", "title": "A", "version": 2,
                               "kind": "maintenance", "env": "local",
                               "steps": [_query_step("s1"),
                                         {"id": "r", "title": "r",
                                          "action": "runbook",
                                          "params": {"ref": "b",
                                                     "type": "runbook"}}]})
        _write_rb(stage_home, {"name": "b", "title": "B", "version": 2,
                               "kind": "maintenance", "env": "local",
                               "steps": [_query_step("s1"),
                                         {"id": "r", "title": "r",
                                          "action": "runbook",
                                          "params": {"ref": "a",
                                                     "type": "runbook"}}]})
        with pytest.raises(ValueError, match="环路: a → b → a"):
            _validate_runbook(
                _load := yaml.safe_load((stage_home / "runbooks" / "a.yaml")
                                        .read_text(encoding="utf-8")),
                "a", stage_home)

    def test_tool_ref_unregistered_guides_compile(self, stage_home):
        data = {"name": "p", "title": "P", "version": 2, "kind": "maintenance",
                "env": "local",
                "steps": [_query_step("s1"), {"id": "r", "title": "r",
                                              "action": "runbook",
                                              "params": {"ref": "not-there",
                                                         "type": "tool"}}]}
        with pytest.raises(ValueError, match="contract compile"):
            _validate_runbook(data, "p", stage_home)

    def test_tool_ref_registered_passes(self, stage_home):
        _register_contract(stage_home, "verify-svc")
        data = {"name": "p", "title": "P", "version": 2, "kind": "maintenance",
                "env": "local",
                "steps": [_query_step("s1"), {"id": "r", "title": "r",
                                              "action": "runbook",
                                              "params": {"ref": "verify-svc",
                                                         "type": "tool"}}]}
        _validate_runbook(data, "p", stage_home)  # 不抛即通过

    def test_scope_fields_all_optional(self, stage_home):
        # 范围四字段（env/clusters/host_groups/hosts）全缺省合法——继承/收集语义。
        data = {"name": "noscope", "title": "N", "version": 2,
                "kind": "maintenance",
                "steps": [_query_step("s1"), _query_step("s2")]}
        _validate_runbook(data, "noscope", stage_home)  # 不抛即通过

    def test_variable_refs_file_local_only(self, stage_home):
        # 子 runbook 文件内 {{ steps... }} 只引用自身步骤（校验器不跨文件解析）。
        child = {"name": "child", "title": "C", "version": 2,
                 "kind": "maintenance", "env": "local",
                 "steps": [_query_step("inner"),
                           {"id": "use", "title": "use", "action": "query",
                            "params": {"pattern":
                                       "{{ steps.inner.params.pattern }}"}}]}
        _validate_runbook(child, "child", stage_home)  # 自引用步骤合法

        # 引用本文件不存在的步骤（如父层步骤）→ 拒绝（变量不跨层）。
        bad = {"name": "child2", "title": "C2", "version": 2,
               "kind": "maintenance", "env": "local",
               "steps": [_query_step("inner"),
                         {"id": "use", "title": "use", "action": "query",
                          "params": {"pattern":
                                     "{{ steps.parent_step.params.pattern }}"}}]}
        with pytest.raises(ValueError, match="不存在"):
            _validate_runbook(bad, "child2", stage_home)


def _register_contract(home: Path, name: str) -> Path:
    """预置编译契约：registry.yaml 记录 + compiled/<name>.py（不真调 LLM）。"""
    (home / "contracts" / "registry.yaml").write_text(
        yaml.safe_dump({name: {"name": name, "action": "verify",
                               "status": "registered", "compiled_at": "2026-08-24",
                               "approved_by": "test"}}, allow_unicode=True,
                       sort_keys=False),
        encoding="utf-8",
    )
    code = (
        "def call(params, *, home=None, context=None, runner=None):\n"
        "    return {'ok': True, 'target': params.get('target'), "
        "'context_env': (context or {}).get('env')}\n"
    )
    path = home / "contracts" / "compiled" / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 执行器：嵌套编排
# ---------------------------------------------------------------------------

class TestExecutorNested:
    def test_sub_runbook_runs_inherited_scope(self, stage_home):
        _write_rb(stage_home, {"name": "child", "title": "Child", "version": 2,
                               "kind": "maintenance",
                               # 无范围声明 → 完全继承父执行上下文
                               "steps": [_query_step("c1"), _query_step("c2")]})
        parent = {"name": "parent", "title": "Parent", "version": 2,
                  "kind": "maintenance", "env": "local",
                  "on_failure": "stop",
                  "steps": [_query_step("p1"),
                            {"id": "r", "title": "引用子", "action": "runbook",
                             "params": {"ref": "child", "type": "runbook"}},
                            _query_step("p2")]}
        res = execute_runbook(parent, home=stage_home, runner=_ok_runner())
        assert res["result"] == "ok"
        ids = [s["id"] for s in res["steps"]]
        assert ids == ["p1", "r", "p2"]
        rb_step = res["steps"][1]
        assert rb_step["status"] == "ok" and rb_step["sub_result"] == "ok"
        assert [s["id"] for s in rb_step["sub_steps"]] == ["c1", "c2"]
        assert rb_step["scope_source"] == "inherited"
        # 子执行记入 ledger（nested 标记 + 范围来源）
        rows = _ledger_rows(stage_home)
        sub_rows = [r for r in rows if r.get("runbook") == "child"]
        assert sub_rows and sub_rows[-1]["nested"] is True
        assert sub_rows[-1]["scope_source"] == "inherited"
        assert sub_rows[-1]["env"] == "local"

    def test_sub_declared_scope_wins_within_parent(self, stage_home):
        _write_rb(stage_home, {"name": "child", "title": "Child", "version": 2,
                               "kind": "maintenance",
                               "env": "local", "clusters": ["local"],
                               "hosts": ["node-a"],
                               "steps": [_query_step("c1")]})
        parent = {"name": "parent", "title": "Parent", "version": 2,
                  "kind": "maintenance", "env": "local", "hosts": ["node-a"],
                  "on_failure": "stop",
                  "steps": [{"id": "r", "title": "引用子", "action": "runbook",
                             "params": {"ref": "child", "type": "runbook"}}]}
        res = execute_runbook(parent, home=stage_home, runner=_ok_runner())
        assert res["result"] == "ok"
        assert res["steps"][0]["scope_source"] == "declared"

    def test_sub_scope_beyond_parent_rejected(self, stage_home):
        _write_rb(stage_home, {"name": "child", "title": "Child", "version": 2,
                               "kind": "maintenance", "env": "test",
                               "steps": [_query_step("c1")]})
        parent = {"name": "parent", "title": "Parent", "version": 2,
                  "kind": "maintenance", "env": "local", "on_failure": "stop",
                  "steps": [{"id": "r", "title": "引用子", "action": "runbook",
                             "params": {"ref": "child", "type": "runbook"}}]}
        res = execute_runbook(parent, home=stage_home, runner=_ok_runner())
        assert res["result"] == "failed"
        assert "超出父范围" in res["steps"][0]["error"]

    def test_sub_steps_check_matrix_no_exemption(self, stage_home, monkeypatch):
        # 子步骤照常查矩阵（无豁免）：把子步骤动作 query 改 required → 审批
        # 回调 deny → 子步骤 blocked（若子有豁免会直接执行）。
        from tools.matrix_data import load_matrix, set_level, write_matrix
        m = load_matrix(stage_home)
        set_level(m, "local", "query", "required")
        write_matrix(m, stage_home)
        _write_rb(stage_home, {"name": "child", "title": "Child", "version": 2,
                               "kind": "maintenance", "env": "local",
                               "steps": [_query_step("c1")]})
        parent = {"name": "parent", "title": "Parent", "version": 2,
                  "kind": "maintenance", "env": "local", "on_failure": "stop",
                  "steps": [{"id": "r", "title": "引用子", "action": "runbook",
                             "params": {"ref": "child", "type": "runbook"}}]}
        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        denied = []
        terminal_tool.set_approval_callback(
            lambda command, description, **k: denied.append(command) or "deny")
        try:
            res = execute_runbook(parent, home=stage_home, runner=_ok_runner())
        finally:
            terminal_tool.set_approval_callback(None)
        assert res["result"] == "failed"
        assert denied, "子步骤应触发矩阵审批门（无豁免）"
        sub_steps = res["steps"][0]["sub_steps"]
        assert sub_steps[0]["status"] == "blocked"

    def test_runbook_action_approve_gate_default(self, stage_home, monkeypatch):
        # 父引用步骤（action: runbook）查 runbook 动作档位：本基座显式 execute；
        # 这里把 runbook 动作改 required → 父引用步骤被审批门拦（不漏配 approve
        # 之外的语义）。
        from tools.matrix_data import load_matrix, set_level, write_matrix
        m = load_matrix(stage_home)
        set_level(m, "local", "runbook", "required")
        write_matrix(m, stage_home)
        _write_rb(stage_home, {"name": "child", "title": "Child", "version": 2,
                               "kind": "maintenance", "env": "local",
                               "steps": [_query_step("c1")]})
        parent = {"name": "parent", "title": "Parent", "version": 2,
                  "kind": "maintenance", "env": "local", "on_failure": "stop",
                  "steps": [{"id": "r", "title": "引用子", "action": "runbook",
                             "params": {"ref": "child", "type": "runbook"}}]}
        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        denied = []
        terminal_tool.set_approval_callback(
            lambda command, description, **k: denied.append(command) or "deny")
        try:
            res = execute_runbook(parent, home=stage_home, runner=_ok_runner())
        finally:
            terminal_tool.set_approval_callback(None)
        assert res["steps"][0]["status"] == "blocked"
        assert "BLOCKED" in res["steps"][0]["error"] or "denied" in res["steps"][0]["error"]
        assert denied

    def test_sub_failure_child_on_failure_first(self, stage_home, monkeypatch):
        # 子失败：子的 on_failure（rollback）先生效——子自己回滚；子最终失败 →
        # 父引用步骤视为失败 → 父 on_failure stop → 父 failed（父不回滚父步骤）。
        _write_rb(stage_home, {"name": "child", "title": "Child", "version": 2,
                               "kind": "maintenance", "env": "local",
                               "on_failure": {"rollback": "rb-child"},
                               "rollback": [{"name": "rb-child", "steps": [
                                   _query_step("rb1")]}],
                               "steps": [_query_step("c1"),
                                         {"id": "c2", "title": "c2",
                                          "action": "query",
                                          "params": {"pattern": "boom"},
                                          "on_failure": {"rollback": "rb-child"}}]})
        parent = {"name": "parent", "title": "Parent", "version": 2,
                  "kind": "maintenance", "env": "local", "on_failure": "stop",
                  "steps": [_query_step("p1"),
                            {"id": "r", "title": "引用子", "action": "runbook",
                             "params": {"ref": "child", "type": "runbook"}},
                            _query_step("p2")]}
        def _selective(spec, target):
            # 只让 c2（pattern=boom）失败；子 rollback 步骤照常成功。
            if "boom" in json.dumps(spec):
                return {"exit_code": 1, "stdout": "", "stderr": "boom"}
            return {"exit_code": 0, "stdout": "ok", "stderr": ""}
        # batch78（OPS-DELTA #93）：回滚步骤强制人工确认——测试走审批回调放行。
        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        terminal_tool.set_approval_callback(
            lambda command, description, **k: "once")
        try:
            res = execute_runbook(parent, home=stage_home, runner=_selective)
        finally:
            terminal_tool.set_approval_callback(None)
        assert res["result"] == "failed"
        rb_step = res["steps"][1]
        assert rb_step["status"] == "failed"
        assert rb_step["sub_result"] == "rolled_back"  # 子自己回滚了
        assert "已执行 rollback" in rb_step["error"]
        # 子 ledger 记录 result=rolled_back
        rows = _ledger_rows(stage_home)
        sub_rows = [r for r in rows if r.get("runbook") == "child"]
        assert sub_rows[-1]["result"] == "rolled_back"
        # 父 stop：p2 不执行
        assert "p2" not in [s["id"] for s in res["steps"]]

    def test_parent_rollback_linkage(self, stage_home, monkeypatch):
        # 父 on_failure: rollback → 子失败后父引用步骤失败 → 父自己的 rollback
        # 场景处理父已完成的其他步骤（子已完成步骤由子自己的回滚处理）。
        _write_rb(stage_home, {"name": "child", "title": "Child", "version": 2,
                               "kind": "maintenance", "env": "local",
                               "on_failure": "stop",
                               "steps": [_query_step("c1", pattern="boom")]})
        parent = {"name": "parent", "title": "Parent", "version": 2,
                  "kind": "maintenance", "env": "local",
                  "on_failure": {"rollback": "rb-parent"},
                  "rollback": [{"name": "rb-parent", "steps": [
                      _query_step("rb-p1")]}],
                  "steps": [_query_step("p1"),
                            {"id": "r", "title": "引用子", "action": "runbook",
                             "params": {"ref": "child", "type": "runbook"}}]}
        def _selective(spec, target):
            if "boom" in json.dumps(spec):
                return {"exit_code": 1, "stdout": "", "stderr": "boom"}
            return {"exit_code": 0, "stdout": "ok", "stderr": ""}
        # batch78（OPS-DELTA #93）：回滚步骤强制人工确认——测试走审批回调放行。
        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        terminal_tool.set_approval_callback(
            lambda command, description, **k: "once")
        try:
            res = execute_runbook(parent, home=stage_home, runner=_selective)
        finally:
            terminal_tool.set_approval_callback(None)
        assert res["result"] == "rolled_back"
        assert res["rolled_back"] is True
        last = res["steps"][-1]
        assert last["id"] == "__rollback__"
        assert last["steps"][0]["id"] == "rb-p1"  # 父回滚只处理父步骤
        rows = _ledger_rows(stage_home)
        sub_rows = [r for r in rows if r.get("runbook") == "child"]
        assert sub_rows[-1]["result"] == "failed"

    def test_type_tool_executes_with_exemption(self, stage_home):
        # type=tool = run_script 资产预审语义：工具已过资产审批——交互执行
        # execute（豁免逐次审批，不查矩阵）。runbook 动作档位改 required 也不
        # 拦工具调用（工具路径根本不查矩阵）。
        from tools.matrix_data import load_matrix, set_level, write_matrix
        m = load_matrix(stage_home)
        set_level(m, "local", "runbook", "required")
        write_matrix(m, stage_home)
        _register_contract(stage_home, "verify-svc")
        parent = {"name": "parent", "title": "Parent", "version": 2,
                  "kind": "maintenance", "env": "local", "on_failure": "stop",
                  "steps": [_query_step("p1"),
                            {"id": "t", "title": "调工具", "action": "runbook",
                             "params": {"ref": "verify-svc", "type": "tool",
                                        "target": "nginx"}},
                            _query_step("p2")]}
        res = execute_runbook(parent, home=stage_home, runner=_ok_runner())
        assert res["result"] == "ok"
        tool_step = res["steps"][1]
        assert tool_step["status"] == "ok" and tool_step["type"] == "tool"
        assert tool_step["output"]["ok"] is True
        assert tool_step["output"]["target"] == "nginx"
        # 上下文透传给工具（scope 即 resolve_topo_ref 的 context）
        assert tool_step["output"]["context_env"] == "local"

    def test_type_tool_unregistered_rejected_at_runtime(self, stage_home):
        parent = {"name": "parent", "title": "Parent", "version": 2,
                  "kind": "maintenance", "env": "local", "on_failure": "stop",
                  "steps": [{"id": "t", "title": "调工具", "action": "runbook",
                             "params": {"ref": "ghost-tool", "type": "tool"}}]}
        res = execute_runbook(parent, home=stage_home, runner=_ok_runner())
        # 校验层前置拦截（fail-closed，执行前拒绝）——runtime 未注册检查是
        # 纵深防御（校验被绕过时才触发）。
        assert res["result"] == "blocked"
        assert "contract compile" in res["error"]

    def test_variable_no_cross_layer_runtime(self, stage_home):
        # 子文件引用父层步骤变量 → 子校验失败 → 父引用步骤失败（变量不跨层：
        # 父向子传值走 params 显式传入，父执行时已解析）。
        _write_rb(stage_home, {"name": "child", "title": "Child", "version": 2,
                               "kind": "maintenance", "env": "local",
                               "steps": [
                                   {"id": "use", "title": "use",
                                    "action": "query",
                                    "params": {"pattern":
                                               "{{ steps.p1.params.pattern }}"}}]})
        parent = {"name": "parent", "title": "Parent", "version": 2,
                  "kind": "maintenance", "env": "local", "on_failure": "stop",
                  "steps": [_query_step("p1"),
                            {"id": "r", "title": "引用子", "action": "runbook",
                             "params": {"ref": "child", "type": "runbook"}}]}
        res = execute_runbook(parent, home=stage_home, runner=_ok_runner())
        assert res["result"] == "failed"
        assert "校验失败" in res["steps"][1]["error"]

    def test_parent_passes_explicit_params_to_sub(self, stage_home):
        # 跨层传值走 params 显式传入：父步骤 params 的 {{ }} 由父执行时解析后
        # 传给子（子步骤用自己的 step_values，父变量不可直接引用）。
        _write_rb(stage_home, {"name": "child", "title": "Child", "version": 2,
                               "kind": "maintenance", "env": "local",
                               "steps": [_query_step("c1")]})
        parent = {"name": "parent", "title": "Parent", "version": 2,
                  "kind": "maintenance", "env": "local", "on_failure": "stop",
                  "steps": [_query_step("p1", pattern="hello"),
                            {"id": "r", "title": "引用子", "action": "runbook",
                             "params": {"ref": "child", "type": "runbook",
                                        "note": "{{ steps.p1.params.pattern }}"}}]}
        res = execute_runbook(parent, home=stage_home, runner=_ok_runner())
        assert res["result"] == "ok"
        assert res["steps"][1]["params"]["note"] == "hello"

    def test_no_scope_standalone_needs_scope(self, stage_home):
        data = {"name": "noscope", "title": "N", "version": 2,
                "kind": "maintenance",
                "steps": [_query_step("s1")]}
        res = execute_runbook(data, home=stage_home, runner=_ok_runner(),
                              collect_scope=True)
        assert res["result"] == "needs_scope"
        assert res["status"] == "scope_collection"
        assert "未声明执行范围" in res["error"]
        assert res["steps"] == []

    def test_no_scope_with_explicit_env_runs(self, stage_home):
        # 显式 env 传入（clarify 收集后重跑形态）→ 正常执行。
        data = {"name": "noscope", "title": "N", "version": 2,
                "kind": "maintenance",
                "steps": [_query_step("s1")]}
        res = execute_runbook(data, env="local", home=stage_home,
                              runner=_ok_runner(), collect_scope=True)
        assert res["result"] == "ok"

    def test_depth_guard_breaks_chain(self, stage_home):
        # 环状防护运行时兜底：执行栈深度上限（_MAX_NESTING_DEPTH=10）防校验
        # 遗漏死循环。构造无环但超深的引用链 r1→r2→…→r12。
        chain = 12
        for i in range(1, chain + 1):
            name = f"r{i}"
            steps = [{"id": "r", "title": "引用", "action": "runbook",
                      "params": {"ref": f"r{i + 1}", "type": "runbook"}}] \
                if i < chain else [_query_step("leaf")]
            _write_rb(stage_home, {"name": name, "title": name, "version": 2,
                                   "kind": "maintenance", "env": "local",
                                   "on_failure": "stop", "steps": steps})
        data = yaml.safe_load((stage_home / "runbooks" / "r1.yaml")
                              .read_text(encoding="utf-8"))
        res = execute_runbook(data, home=stage_home, runner=_ok_runner())
        assert res["result"] == "failed"
        assert "嵌套深度超过上限" in res["error"]


# ---------------------------------------------------------------------------
# 补丁 1：凭据纪律校验器（契约 params 敏感参数拒绝）
# ---------------------------------------------------------------------------

class TestContractCredentialDiscipline:
    def _contract(self, name: str, params: dict) -> dict:
        return {"name": name,
                "description": f"{name} 测试契约",
                "action": "verify",
                "params": params,
                "returns": {"ok": "boolean"},
                "tests": [
                    {"name": "正常", "input": {"target": "svc"},
                     "expect": {"ok": True}},
                    {"name": "目标不存在", "input": {"target": "svc"},
                     "expect": {"error": "entity_not_found"}},
                ]}

    @pytest.mark.parametrize("pname", [
        "password", "api_token", "ssh_key", "db_password", "access_key",
        "private_key", "secret", "credential", "auth_key", "client_secret",
    ])
    def test_credential_param_names_rejected(self, stage_home, pname):
        from tools.contract_tools import validate_contract
        data = self._contract("c1", {pname: {"type": "string"}})
        with pytest.raises(ValueError, match="凭据"):
            validate_contract(data, "c1", stage_home)

    def test_normal_params_pass(self, stage_home):
        from tools.contract_tools import validate_contract
        data = self._contract("c2", {"target": {"type": "string"},
                                     "port": {"type": "integer"},
                                     "count": {"type": "integer"}})
        validate_contract(data, "c2", stage_home)  # 不抛即通过

    def test_credential_default_value_rejected(self, stage_home):
        from tools.contract_tools import validate_contract
        data = self._contract("c3", {"host": {"type": "string",
                                              "default": "password=sup3r"}})
        with pytest.raises(ValueError, match="疑似凭据明文"):
            validate_contract(data, "c3", stage_home)

    def test_compound_name_with_boundary_not_rejected(self, stage_home):
        # 词边界：monkey/keyboard 这类含 key 词的正常参数名不误杀。
        from tools.contract_tools import validate_contract
        data = self._contract("c4", {"monkey": {"type": "string"},
                                     "keyboard": {"type": "string"},
                                     "target": {"type": "string"}})
        validate_contract(data, "c4", stage_home)  # 不抛即通过


# ---------------------------------------------------------------------------
# 补丁 2：topo_update credentials 数组支持
# ---------------------------------------------------------------------------

class TestTopoUpdateCredentials:
    def _load(self, result: str) -> dict:
        return json.loads(result)

    def _topo(self, home: Path) -> dict:
        return yaml.safe_load((home / "topology.yaml")
                              .read_text(encoding="utf-8"))

    def test_credentials_array_add_modify_delete(self, stage_home):
        # 增：首次写入 credentials 数组（引用形态）
        out = self._load(topo_update(
            "node-a",
            {"credentials": [{"type": "ssh_key", "ref": "~/.ssh/id_rsa",
                              "user": "ops"}]},
            home=stage_home))
        assert out["status"] == "updated"
        assert out["credentials_updated"] is True
        assert out["credentials_refs"] == ["~/.ssh/id_rsa"]
        saved = self._topo(stage_home)
        row = next(h for h in saved["hosts"] if h["name"] == "node-a")
        assert row["credentials"] == [{"type": "ssh_key",
                                       "ref": "~/.ssh/id_rsa", "user": "ops"}]
        # 改：替换条目（vault 引用）
        out = self._load(topo_update(
            "node-a",
            {"credentials": [{"type": "secret", "ref": "vault:secret/db-pass",
                              "port": 22}]},
            home=stage_home))
        assert out["credentials_refs"] == ["vault:secret/db-pass"]
        saved = self._topo(stage_home)
        row = next(h for h in saved["hosts"] if h["name"] == "node-a")
        assert row["credentials"] == [{"type": "secret",
                                       "ref": "vault:secret/db-pass",
                                       "port": 22}]
        # 删：空数组 = 清空该主机凭据
        out = self._load(topo_update("node-a", {"credentials": []},
                                     home=stage_home))
        assert out["credentials_refs"] == []
        saved = self._topo(stage_home)
        row = next(h for h in saved["hosts"] if h["name"] == "node-a")
        assert row["credentials"] == []

    def test_credentials_non_host_rejected(self, stage_home):
        # credentials 只挂 host 行：service 实体（web-svc）→ 拒绝。
        out = self._load(topo_update("web-svc", {"credentials": [
            {"type": "ssh_key", "ref": "~/.ssh/id_rsa"}]}, home=stage_home))
        assert "不是 host" in out["error"]

    def test_credentials_invalid_shapes_rejected(self, stage_home):
        # 非法 type
        out = self._load(topo_update("node-a", {"credentials": [
            {"type": "plaintext", "ref": "x"}]}, home=stage_home))
        assert "ssh_key|secret" in out["error"]
        # 缺 ref
        out = self._load(topo_update("node-a", {"credentials": [
            {"type": "ssh_key"}]}, home=stage_home))
        assert "ref 必填" in out["error"]
        # 非数组
        out = self._load(topo_update("node-a", {"credentials": "key"},
                                     home=stage_home))
        assert "必须是数组" in out["error"]
        # 疑似明文 ref（含空白/=）拒绝——凭据纪律：ref 只收引用
        out = self._load(topo_update("node-a", {"credentials": [
            {"type": "secret", "ref": "BEGIN PRIVATE KEY-----"}]},
            home=stage_home))
        assert "不是合法引用" in out["error"]
        out = self._load(topo_update("node-a", {"credentials": [
            {"type": "secret", "ref": "pass= xyz"}]}, home=stage_home))
        assert "不是合法引用" in out["error"]
        # port 越界 / user 非字符串
        out = self._load(topo_update("node-a", {"credentials": [
            {"type": "ssh_key", "ref": "~/.ssh/id_rsa", "port": 99999}]},
            home=stage_home))
        assert "port" in out["error"]

    def test_credentials_rejected_writes_nothing(self, stage_home):
        before = self._topo(stage_home)
        self._load(topo_update("node-a", {"credentials": [
            {"type": "nope", "ref": "x"}]}, home=stage_home))
        after = self._topo(stage_home)
        assert before == after  # 校验失败不落盘


# ---------------------------------------------------------------------------
# 补丁 3：CLI 命令索引注入
# ---------------------------------------------------------------------------

class TestCliCommandIndex:
    def test_cli_index_injected_into_topo_block(self, stage_home):
        from plugins.memory.topo import render_topo_block
        block = render_topo_block(stage_home)
        assert "运维 CLI 命令索引" in block
        assert "vigil topo-discover" in block
        assert "vigil contract compile" in block
        assert "vigil contract list" in block
        assert "vigil matrix show" in block
        # topo 表单更新如实标注工具路径（不编辑 topology.yaml）
        assert "topo_update 工具" in block

    def test_topo_block_stays_silent_without_topology(self, tmp_path):
        from plugins.memory.topo import render_topo_block
        empty = tmp_path / "empty"
        empty.mkdir()
        assert render_topo_block(empty) == ""

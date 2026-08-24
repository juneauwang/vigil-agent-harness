"""YAPL 主框架阶段 A：契约加载器 + 分层校验器验收测试。

覆盖（任务书 §任务 1 + §任务 3）：
  - 加载器：目录缺失 = 空集 / 非 yaml 忽略 + 警告 / 文件名 ↔ name 不一致拒绝；
  - 结构校验：name kebab-case / description / action 枚举（当前 23 个）/
    拒绝 permission、steps / 统一命名空间冲突（内置工具 + runbooks/）；
  - params 类型系统：六类型合法 + 非法拒绝（未知类型 / topo_ref 无 kind /
    enum 空 / list 无 items / default 类型不符 / required+default 矛盾）；
  - returns：拒绝 topo_ref（含 list items 内）；
  - tests 命门：<2 拒绝 / input 未知键 / required 缺失 / expect 形态 /
    handler mock 形态。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import contract_tools
from tools.contract_tools import (
    load_contract,
    load_contracts,
    validate_contract,
)


@pytest.fixture
def chome(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME；内置工具名源置空（确定性 + 快速，冲突检查另有专测）。"""
    home = tmp_path / "vigil_home"
    home.mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    monkeypatch.setattr(contract_tools, "_registered_tool_names", lambda: set())
    return home


def _valid_contract(**over) -> dict:
    """设计 13.1 rolling-restart 示例（合法契约基线）。"""
    data = {
        "name": "rolling-restart",
        "description": "滚动重启服务——按批重启，批间等待，全部成功才算完成",
        "action": "restart",
        "params": {
            "target": {"type": "topo_ref", "kind": "service", "required": True},
            "batch_size": {"type": "integer", "required": False, "default": 1},
            "mode": {"type": "enum", "values": ["graceful", "quick"],
                     "required": True},
        },
        "returns": {"restarted": "integer", "failed": "integer"},
        "tests": [
            {"name": "正常滚动重启",
             "input": {"target": "service:harbor", "mode": "graceful"},
             "expect": {"restarted": 1, "failed": 0}},
            {"name": "目标不存在（参数层错误）",
             "input": {"target": "service:nonexistent", "mode": "graceful"},
             "expect": {"error": "entity_not_found"}},
            {"name": "执行失败传播（执行层错误）",
             "input": {"target": "service:harbor", "mode": "graceful"},
             "handler": {"status": "failed"},
             "expect": {"error": "execution_failed"}},
        ],
    }
    data.update(over)
    return data


def _write(home: Path, name: str, content: str) -> Path:
    cdir = home / "contracts"
    cdir.mkdir(parents=True, exist_ok=True)
    p = cdir / f"{name}.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _yaml(data: dict) -> str:
    import yaml
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------------
# 加载器
# ---------------------------------------------------------------------------

def test_load_contracts_missing_dir_empty(chome):
    assert load_contracts(chome) == {}


def test_load_contracts_non_yaml_ignored_with_warning(chome, caplog):
    (chome / "contracts").mkdir(parents=True)
    (chome / "contracts" / "README.md").write_text("docs", encoding="utf-8")
    (chome / "contracts" / "notes.txt").write_text("notes", encoding="utf-8")
    assert load_contracts(chome) == {}
    assert any("非 .yaml" in r.message and "README.md" in r.message
               for r in caplog.records)


def test_load_contracts_yml_suffix_ignored(chome):
    _write(chome, "good", _yaml(_valid_contract(name="good")))
    (chome / "contracts" / "legacy.yml").write_text(
        _yaml({"name": "legacy", "description": "x", "action": "restart"}),
        encoding="utf-8")
    loaded = load_contracts(chome)
    assert set(loaded) == {"good"}


def test_load_contracts_filename_mismatch_rejected(chome):
    _write(chome, "rolling-restart",
           _yaml(_valid_contract(name="other-name")))
    with pytest.raises(ValueError, match="不一致"):
        load_contracts(chome)


def test_load_contract_single(chome):
    _write(chome, "rolling-restart", _yaml(_valid_contract()))
    assert load_contract(chome, "rolling-restart")["name"] == "rolling-restart"
    assert load_contract(chome, "missing") is None
    assert load_contract(chome, "Bad Name") is None


# ---------------------------------------------------------------------------
# 结构层
# ---------------------------------------------------------------------------

def test_valid_contract_passes(chome):
    validate_contract(_valid_contract(), "rolling-restart", chome)


def test_name_mismatch_rejected(chome):
    with pytest.raises(ValueError, match="不一致"):
        validate_contract(_valid_contract(name="other"), "rolling-restart", chome)


@pytest.mark.parametrize("bad", ["RollingRestart", "rolling_restart",
                                 "rolling.restart", "-rolling",
                                 "rolling--restart"])
def test_name_not_kebab_rejected(chome, bad):
    with pytest.raises(ValueError, match="kebab-case"):
        validate_contract(_valid_contract(name=bad), bad, chome)


def test_description_required(chome):
    with pytest.raises(ValueError, match="description"):
        validate_contract(_valid_contract(description=""), "rolling-restart", chome)


def test_action_required(chome):
    data = _valid_contract()
    del data["action"]
    with pytest.raises(ValueError, match="缺少 action"):
        validate_contract(data, "rolling-restart", chome)


def test_action_not_in_vocab_lists_choices(chome):
    data = _valid_contract(action="teleport")
    with pytest.raises(ValueError) as ei:
        validate_contract(data, "rolling-restart", chome)
    msg = str(ei.value)
    assert "不在动作词表" in msg and "restart" in msg
    assert "可用: start, stop" in msg


@pytest.mark.parametrize("banned", ["permission", "steps"])
def test_permission_steps_rejected(chome, banned):
    data = _valid_contract(**{banned: [{"anything": True}]})
    with pytest.raises(ValueError, match=f"禁止 {banned}"):
        validate_contract(data, "rolling-restart", chome)


def test_namespace_conflict_with_registered_tool(chome, monkeypatch):
    monkeypatch.setattr(contract_tools, "_registered_tool_names",
                        lambda: {"rolling-restart"})
    with pytest.raises(ValueError, match="命名空间冲突"):
        validate_contract(_valid_contract(), "rolling-restart", chome)


def test_namespace_conflict_with_runbook_name(chome):
    (chome / "runbooks").mkdir(parents=True)
    (chome / "runbooks" / "rolling-restart.yaml").write_text(
        "name: rolling-restart\n", encoding="utf-8")
    with pytest.raises(ValueError, match="命名空间冲突"):
        validate_contract(_valid_contract(), "rolling-restart", chome)


# ---------------------------------------------------------------------------
# params 类型系统
# ---------------------------------------------------------------------------

def _params(params: dict) -> dict:
    return _valid_contract(params=params)


def test_params_six_types_legal(chome):
    data = _params({
        "s": {"type": "string", "required": False, "default": "all"},
        "i": {"type": "integer", "default": 2},
        "b": {"type": "boolean", "default": True},
        "f": {"type": "float", "default": 1.5},
        "t": {"type": "topo_ref", "kind": "host_group"},
        "e": {"type": "enum", "values": ["a", "b"]},
        "l": {"type": "list", "items": {"type": "string"}},
        "lt": {"type": "list",
               "items": {"type": "topo_ref", "kind": "service"}},
    })
    data["tests"] = [
        {"name": "正常", "input": {"s": "all", "i": 1, "b": True, "f": 1.0,
                                   "t": "h1", "e": "a", "l": ["x"],
                                   "lt": ["svc"]},
         "expect": {"ok": True}},
        {"name": "错误", "input": {"s": "all", "e": "a"},
         "expect": {"error": "invalid_params"}},
    ]
    validate_contract(data, "rolling-restart", chome)


def test_params_unknown_type_rejected(chome):
    with pytest.raises(ValueError, match="未知类型 'object'"):
        validate_contract(_params({"x": {"type": "object"}}),
                          "rolling-restart", chome)


def test_params_topo_ref_requires_kind(chome):
    with pytest.raises(ValueError, match="必须带 kind"):
        validate_contract(_params({"x": {"type": "topo_ref"}}),
                          "rolling-restart", chome)


def test_params_topo_ref_bad_kind(chome):
    with pytest.raises(ValueError, match="必须带 kind"):
        validate_contract(_params({"x": {"type": "topo_ref", "kind": "container"}}),
                          "rolling-restart", chome)


def test_params_enum_empty_values_rejected(chome):
    with pytest.raises(ValueError, match="非空 values"):
        validate_contract(_params({"x": {"type": "enum", "values": []}}),
                          "rolling-restart", chome)


def test_params_enum_nested_values_rejected(chome):
    with pytest.raises(ValueError, match="标量"):
        validate_contract(_params({"x": {"type": "enum",
                                         "values": [{"a": 1}]}}),
                          "rolling-restart", chome)


def test_params_list_requires_items(chome):
    with pytest.raises(ValueError, match="items"):
        validate_contract(_params({"x": {"type": "list"}}),
                          "rolling-restart", chome)


def test_params_list_nested_rejected(chome):
    with pytest.raises(ValueError, match="不支持嵌套"):
        validate_contract(_params({"x": {"type": "list", "items": {
            "type": "list", "items": {"type": "string"}}}}),
            "rolling-restart", chome)


@pytest.mark.parametrize("spec", [
    {"type": "integer", "default": "1"},
    {"type": "integer", "default": True},
    {"type": "boolean", "default": 1},
    {"type": "float", "default": "1.5"},
    {"type": "string", "default": 1},
    {"type": "topo_ref", "kind": "service", "default": ""},
])
def test_params_default_type_mismatch_rejected(chome, spec):
    with pytest.raises(ValueError, match="默认值"):
        validate_contract(_params({"x": spec}), "rolling-restart", chome)


def test_params_required_with_default_contradiction(chome):
    with pytest.raises(ValueError, match="矛盾"):
        validate_contract(_params({"x": {"type": "integer",
                                         "required": True, "default": 1}}),
                          "rolling-restart", chome)


def test_params_enum_default_out_of_values(chome):
    with pytest.raises(ValueError, match="enum values 内"):
        validate_contract(_params({"x": {"type": "enum",
                                         "values": ["a", "b"],
                                         "default": "slow"}}),
                          "rolling-restart", chome)


# ---------------------------------------------------------------------------
# returns
# ---------------------------------------------------------------------------

def test_returns_reject_topo_ref(chome):
    data = _valid_contract(returns={"x": {"type": "topo_ref",
                                          "kind": "service"}})
    with pytest.raises(ValueError, match="禁止 topo_ref"):
        validate_contract(data, "rolling-restart", chome)


def test_returns_reject_topo_ref_in_list_items(chome):
    data = _valid_contract(returns={"x": {"type": "list",
                                          "items": {"type": "topo_ref",
                                                    "kind": "service"}}})
    with pytest.raises(ValueError, match="禁止 topo_ref"):
        validate_contract(data, "rolling-restart", chome)


def test_returns_legal_types(chome):
    data = _valid_contract(returns={
        "count": "integer",
        "ok": "boolean",
        "names": {"type": "list", "items": {"type": "string"}},
        "mode": {"type": "enum", "values": ["a", "b"]},
    })
    validate_contract(data, "rolling-restart", chome)


# ---------------------------------------------------------------------------
# tests 命门
# ---------------------------------------------------------------------------

def test_tests_less_than_two_rejected(chome):
    data = _valid_contract(tests=[_valid_contract()["tests"][0]])
    with pytest.raises(ValueError, match="最少 2 个"):
        validate_contract(data, "rolling-restart", chome)


def test_tests_missing_rejected(chome):
    data = _valid_contract()
    del data["tests"]
    with pytest.raises(ValueError, match="最少 2 个"):
        validate_contract(data, "rolling-restart", chome)


def test_input_unknown_key_rejected(chome):
    tests = _valid_contract()["tests"]
    tests[0] = dict(tests[0], input={"extra": 1})
    with pytest.raises(ValueError, match="未知参数键 'extra'"):
        validate_contract(_valid_contract(tests=tests),
                          "rolling-restart", chome)


def test_input_required_missing_rejected(chome):
    tests = _valid_contract()["tests"]
    tests[0] = dict(tests[0], input={"target": "service:harbor"})
    with pytest.raises(ValueError, match="缺失必填参数 'mode'"):
        validate_contract(_valid_contract(tests=tests),
                          "rolling-restart", chome)


def test_input_value_type_mismatch_rejected(chome):
    tests = _valid_contract()["tests"]
    tests[0] = dict(tests[0], input={"target": 123, "mode": "graceful"})
    with pytest.raises(ValueError, match="裸实体名"):
        validate_contract(_valid_contract(tests=tests),
                          "rolling-restart", chome)


def test_expect_error_form_mixed_rejected(chome):
    tests = _valid_contract()["tests"]
    tests[1] = dict(tests[1], expect={"error": "entity_not_found",
                                      "restarted": 1})
    with pytest.raises(ValueError, match="不能混其他键"):
        validate_contract(_valid_contract(tests=tests),
                          "rolling-restart", chome)


def test_expect_error_code_empty_rejected(chome):
    tests = _valid_contract()["tests"]
    tests[1] = dict(tests[1], expect={"error": ""})
    with pytest.raises(ValueError, match="非空"):
        validate_contract(_valid_contract(tests=tests),
                          "rolling-restart", chome)


def test_handler_mock_shape_rejected(chome):
    tests = _valid_contract()["tests"]
    tests[2] = dict(tests[2], handler={"foo": 1})
    with pytest.raises(ValueError, match="handler mock 形态"):
        validate_contract(_valid_contract(tests=tests),
                          "rolling-restart", chome)


def test_handler_mock_status_enum_rejected(chome):
    tests = _valid_contract()["tests"]
    tests[2] = dict(tests[2], handler={"status": "boom"})
    with pytest.raises(ValueError, match="failed/success"):
        validate_contract(_valid_contract(tests=tests),
                          "rolling-restart", chome)

"""YAPL 主框架阶段 B：编译生成管线验收测试（编译状态机 + 沙箱自证 + 审批注册）。

覆盖（任务书 §任务 5）：
  - 编译状态机：contract_not_found / 契约校验失败 / generation_failed 重试超限 /
    语法错重试 / 越界拒绝重试 / 自证失败重试（带用例名引导）/ 审批拒绝不注册 /
    tirith block 拒 / register_conflict 重名拒绝；
  - 生成规范：mock llm_generate（不真调 LLM）返回合法/语法错/越界/自证不过代码
    → 对应处理；重试 note 注入下一轮提示；
  - 沙箱自证：handler 通道 mock（handler.status=failed → mock runner 失败 →
    错误传播验证）、超时防死循环；
  - 注册：registry.yaml 记录 / 编译代码 0600 落盘 / schema 生成 / 动态注册后
    get_tool_definitions 可见 / ensure 启动钩子幂等热加载。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import contract_compile as cc
from tools import contract_tools
from tools.contract_tools import load_contract


# ---------------------------------------------------------------------------
# 合法契约（verify-service，action=verify ∈ 23 词表）与合法薄包装
# ---------------------------------------------------------------------------

def _valid_contract(**over) -> dict:
    data = {
        "name": "verify-service",
        "description": "验证服务运行状态（薄包装生成规范验收用）",
        "action": "verify",
        "params": {
            "target": {"type": "topo_ref", "kind": "service", "required": True},
            "pattern": {"type": "string", "required": False, "default": ""},
        },
        "returns": {"status": "string"},
        "tests": [
            {"name": "正常验证",
             "input": {"target": "service:svc-one"},
             "expect": {"status": "ok"}},
            {"name": "目标不存在（参数层错误）",
             "input": {"target": "service:ghost-svc"},
             "expect": {"error": "entity_not_found"}},
            {"name": "执行失败传播（执行层错误）",
             "input": {"target": "service:svc-one"},
             "handler": {"status": "failed"},
             "expect": {"error": "execution_failed"}},
        ],
    }
    data.update(over)
    return data


VALID_WRAPPER = '''\
# verify-service 薄包装（YAPL 阶段 B 生成规范产物）


def call(params, *, home=None, context=None, runner=None):
    from tools.topo_tools import load_topology
    from tools.topo_ref import resolve_topo_ref
    from tools.runbook_exec import resolve_target
    from tools.runbook_handlers import generate_commands
    from tools.contract_runtime import run_specs, any_spec_failed

    def _ref(v):
        s = str(v or "").strip()
        return s.split(":", 1)[-1] if ":" in s else s

    p = dict(params or {})
    # ── 参数校验（必填 / 类型 / topo_ref 非空）──
    if "target" not in p or not str(p.get("target") or "").strip():
        return {"error": "invalid_params", "message": "target 必填（topo_ref）"}
    if not isinstance(p["target"], str):
        return {"error": "invalid_params", "message": "target 必须是字符串"}
    pattern = p.get("pattern")
    if pattern is None:
        pattern = ""
    p["pattern"] = pattern
    if not isinstance(pattern, str):
        return {"error": "invalid_params", "message": "pattern 必须是字符串"}
    # ── topo_ref 解析 ──
    try:
        topo = load_topology(home)
        if topo is None:
            return {"error": "execution_failed", "message": "拓扑表不存在"}
        resolve_topo_ref(topo, _ref(p["target"]), kind="service",
                         context=context, home=home)
        target = resolve_target(home, topo, _ref(p["target"]), context=context)
    except ValueError:
        return {"error": "entity_not_found",
                "message": "topo_ref 目标不存在或歧义——先 topo_query 确认实体名"}
    # ── 分派 + 执行 ──
    try:
        specs = generate_commands("verify", p, target)
        results = run_specs(specs, target, home=home, runner=runner)
    except Exception:
        return {"error": "execution_failed", "message": "handler 分派或执行失败"}
    if any_spec_failed(results):
        return {"error": "execution_failed", "message": "handler 命令执行失败"}
    # ── 结果包装 ──
    return {"status": "ok"}
'''


WRONG_WRAPPER = '''\
# 错误薄包装：不检查 any_spec_failed——handler 失败不会传播


def call(params, *, home=None, context=None, runner=None):
    from tools.topo_tools import load_topology
    from tools.topo_ref import resolve_topo_ref
    from tools.runbook_exec import resolve_target
    from tools.runbook_handlers import generate_commands
    from tools.contract_runtime import run_specs

    def _ref(v):
        s = str(v or "").strip()
        return s.split(":", 1)[-1] if ":" in s else s

    p = dict(params or {})
    if "target" not in p or not str(p.get("target") or "").strip():
        return {"error": "invalid_params", "message": "target 必填"}
    try:
        topo = load_topology(home)
        if topo is None:
            return {"error": "execution_failed", "message": "拓扑表不存在"}
        resolve_topo_ref(topo, _ref(p["target"]), kind="service",
                         context=context, home=home)
        target = resolve_target(home, topo, _ref(p["target"]), context=context)
    except ValueError:
        return {"error": "entity_not_found", "message": "目标不存在"}
    specs = generate_commands("verify", p, target)
    run_specs(specs, target, home=home, runner=runner)
    return {"status": "ok"}
'''


SYNTAX_BROKEN = "def call(params, *, home=None, context=None, runner=None):\n    if True\n"


OVERREACH_WRAPPER = VALID_WRAPPER + "\nimport subprocess  # 越界\n"


INFINITE_LOOP_WRAPPER = '''\
def call(params, *, home=None, context=None, runner=None):
    while True:
        pass
'''


class QueueGen:
    """按序返回生成物并记录 prompts（mock llm_generate，不真调 LLM）。"""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.prompts: list = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.outputs.pop(0)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def chome(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME；命名空间冲突源置空；清理全局注册表副作用。"""
    home = tmp_path / "vigil_home"
    home.mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    monkeypatch.setattr(contract_tools, "_registered_tool_names", lambda: set())
    monkeypatch.setattr(cc, "_last_llm_usage", {})
    yield home
    from tools.registry import registry
    for name in list(cc.registered_contracts(home)):
        try:
            registry.deregister(name)
        except Exception:
            pass
    try:
        from model_tools import _clear_tool_defs_cache
        _clear_tool_defs_cache()
    except Exception:
        pass


@pytest.fixture
def contract(chome):
    data = _valid_contract()
    cdir = chome / "contracts"
    cdir.mkdir(parents=True, exist_ok=True)
    import yaml
    (cdir / f"{data['name']}.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return data


def _write_contract(home: Path, data: dict) -> Path:
    cdir = home / "contracts"
    cdir.mkdir(parents=True, exist_ok=True)
    import yaml
    p = cdir / f"{data['name']}.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    return p


def _approve(choice: str = "once"):
    """注册 CLI 交互审批回调（照 script_asset 测试惯例）。"""
    from tools import terminal_tool
    token = None
    try:
        from tools.approval import set_hermes_interactive_context
        token = set_hermes_interactive_context("1")
    except Exception:
        pass
    terminal_tool.set_approval_callback(lambda *a, **k: choice)
    return token


def _reset_approve(token) -> None:
    from tools import terminal_tool
    terminal_tool.set_approval_callback(None)
    if token is not None:
        try:
            from tools.approval import reset_current_session_key
            reset_current_session_key(token)
        except Exception:
            pass


def _mock_runner(fail: bool = False):
    def runner(spec, target):
        return {"exit_code": 1 if fail else 0,
                "stdout": "" if not fail else "mock failure",
                "stderr": "" if not fail else "mock handler failed"}
    return runner


# ---------------------------------------------------------------------------
# 编译状态机
# ---------------------------------------------------------------------------

class TestCompileStateMachine:
    def test_contract_not_found(self, chome):
        out = cc.compile_contract(home=chome, name="no-such-contract",
                                  llm_generate=lambda p: VALID_WRAPPER)
        assert out["status"] == "failed"
        assert out["code"] == "contract_not_found"
        assert out["stage"] == "validated"

    def test_invalid_contract_rejected(self, chome):
        data = _valid_contract(tests=[{"name": "只有一个用例",
                                       "input": {"target": "service:svc-one"},
                                       "expect": {"status": "ok"}}])
        _write_contract(chome, data)
        out = cc.compile_contract(home=chome, name=data["name"],
                                  llm_generate=lambda p: VALID_WRAPPER)
        assert out["status"] == "failed"
        assert out["code"] == "contract_invalid"
        assert "最少 2 个用例" in out["error"]

    def test_generation_failed_after_retries(self, contract, chome):
        gen = QueueGen(SYNTAX_BROKEN, SYNTAX_BROKEN, SYNTAX_BROKEN)
        out = cc.compile_contract(home=chome, name=contract["name"],
                                  llm_generate=gen, max_retries=3)
        assert out["status"] == "failed"
        assert out["stage"] == "generating"
        assert out["code"] == "generation_failed"
        assert out["attempts"] == 3
        assert "语法错误" in out["error"]
        assert not (chome / "contracts" / "compiled").exists()

    def test_syntax_error_retry_with_guidance_then_success(self, contract, chome):
        gen = QueueGen(SYNTAX_BROKEN, VALID_WRAPPER)
        token = _approve("once")
        try:
            out = cc.compile_contract(home=chome, name=contract["name"],
                                      llm_generate=gen, max_retries=3)
        finally:
            _reset_approve(token)
        assert out["status"] == "registered"
        assert len(gen.prompts) == 2
        assert "上一轮生成物未通过沙箱自证" in gen.prompts[1]
        assert "语法错误" in gen.prompts[1]

    def test_overreach_rejected_retry_then_success(self, contract, chome):
        gen = QueueGen(OVERREACH_WRAPPER, VALID_WRAPPER)
        token = _approve("once")
        try:
            out = cc.compile_contract(home=chome, name=contract["name"],
                                      llm_generate=gen, max_retries=3)
        finally:
            _reset_approve(token)
        assert out["status"] == "registered"
        assert len(gen.prompts) == 2
        assert "越界模式 'subprocess'" in gen.prompts[1]

    def test_overreach_all_retries_failed(self, contract, chome):
        gen = QueueGen(OVERREACH_WRAPPER, OVERREACH_WRAPPER, OVERREACH_WRAPPER)
        out = cc.compile_contract(home=chome, name=contract["name"],
                                  llm_generate=gen, max_retries=2)
        assert out["status"] == "failed"
        assert out["code"] == "generation_failed"
        assert "越界" in out["error"]

    def test_selftest_failure_retry_guides_by_case_name(self, contract, chome):
        gen = QueueGen(WRONG_WRAPPER, VALID_WRAPPER)
        token = _approve("once")
        try:
            out = cc.compile_contract(home=chome, name=contract["name"],
                                      llm_generate=gen, max_retries=3)
        finally:
            _reset_approve(token)
        assert out["status"] == "registered"
        assert len(gen.prompts) == 2
        # 引导带用例名（错误传播没做 → 执行失败传播用例不过）
        assert "执行失败传播" in gen.prompts[1]

    def test_selftest_all_retries_failed(self, contract, chome):
        gen = QueueGen(WRONG_WRAPPER, WRONG_WRAPPER, WRONG_WRAPPER)
        out = cc.compile_contract(home=chome, name=contract["name"],
                                  llm_generate=gen, max_retries=3)
        assert out["status"] == "failed"
        assert out["code"] == "generation_failed"
        assert "执行失败传播" in out["error"]
        assert not (chome / "contracts" / "compiled").exists()

    def test_approval_denied_not_registered(self, contract, chome):
        gen = QueueGen(VALID_WRAPPER)
        token = _approve("deny")
        try:
            out = cc.compile_contract(home=chome, name=contract["name"],
                                      llm_generate=gen)
        finally:
            _reset_approve(token)
        assert out["status"] == "failed"
        assert out["code"] == "approval_rejected"
        assert "未注册" in out["error"]
        assert not (chome / "contracts" / "compiled").exists()
        from tools.registry import registry
        assert registry.get_entry(contract["name"]) is None

    def test_tirith_block_rejected(self, contract, chome, monkeypatch):
        def fake_scan(code):
            return {"action": "block", "findings": [{"id": "x"}],
                    "summary": "malicious pattern"}
        monkeypatch.setattr("tools.tirith_security.check_command_security", fake_scan)
        token = _approve("once")
        try:
            out = cc.compile_contract(home=chome, name=contract["name"],
                                      llm_generate=QueueGen(VALID_WRAPPER))
        finally:
            _reset_approve(token)
        assert out["status"] == "failed"
        assert out["code"] == "tirith_blocked"
        assert "tirith" in out["error"]
        assert not (chome / "contracts" / "compiled").exists()

    def test_tirith_warn_force_manual_still_requires_approval(self, contract, chome, monkeypatch):
        seen = []
        def fake_scan(code):
            return {"action": "warn", "findings": [{"id": "w"}], "summary": "有安全提示"}
        monkeypatch.setattr("tools.tirith_security.check_command_security", fake_scan)
        from tools import terminal_tool
        def cb(*a, **k):
            seen.append(k.get("allow_permanent"))
            return "once"
        token = None
        from tools.approval import set_hermes_interactive_context
        token = set_hermes_interactive_context("1")
        terminal_tool.set_approval_callback(cb)
        try:
            out = cc.compile_contract(home=chome, name=contract["name"],
                                      llm_generate=QueueGen(VALID_WRAPPER))
        finally:
            _reset_approve(token)
        assert out["status"] == "registered"
        # warn = 强制人工：不透出 permanent/session 选项
        assert seen and seen[0] is False


# ---------------------------------------------------------------------------
# 沙箱自证
# ---------------------------------------------------------------------------

class TestSandboxSelftest:
    def test_valid_wrapper_passes_all_cases(self, contract, chome):
        result = cc.sandbox_selftest(VALID_WRAPPER, contract, chome)
        assert result["ok"] is True
        assert result["error"] is None
        names = [c["name"] for c in result["cases"]]
        assert ("正常验证" in names and "目标不存在（参数层错误）" in names
                and "执行失败传播（执行层错误）" in names)
        by_name = {c["name"]: c for c in result["cases"]}
        # 执行失败传播用例 mock runner 失败 → 生成代码必须映射 execution_failed
        assert by_name["执行失败传播（执行层错误）"]["ok"] is True
        assert by_name["执行失败传播（执行层错误）"]["output"]["error"] == "execution_failed"
        assert by_name["目标不存在（参数层错误）"]["output"]["error"] == "entity_not_found"

    def test_wrong_wrapper_fails_with_case_detail(self, contract, chome):
        result = cc.sandbox_selftest(WRONG_WRAPPER, contract, chome)
        assert result["ok"] is False
        failed = [c for c in result["cases"] if not c["ok"]]
        assert failed and failed[0]["name"] == "执行失败传播（执行层错误）"
        assert "期望 error='execution_failed'" in failed[0]["detail"]

    def test_syntax_error_never_enters_sandbox(self, contract, chome, monkeypatch):
        calls = []
        real = cc.sandbox_selftest

        def spy(*a, **k):
            calls.append(1)
            return real(*a, **k)

        monkeypatch.setattr(cc, "sandbox_selftest", spy)
        out = cc.compile_contract(home=chome, name=contract["name"],
                                  llm_generate=QueueGen(SYNTAX_BROKEN,
                                                        SYNTAX_BROKEN,
                                                        SYNTAX_BROKEN),
                                  max_retries=3)
        assert out["status"] == "failed"
        assert out["code"] == "generation_failed"
        assert calls == []  # 语法错直接重试，不进沙箱

    def test_timeout_kills_hang(self, contract, chome):
        result = cc.sandbox_selftest(INFINITE_LOOP_WRAPPER, contract, chome, timeout=1)
        assert result["ok"] is False
        assert "超时" in result["error"]


# ---------------------------------------------------------------------------
# 注册 / schema / 动态注册可见性
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_registered_success_full_record(self, contract, chome):
        gen = QueueGen(VALID_WRAPPER)
        token = _approve("once")
        try:
            out = cc.compile_contract(home=chome, name=contract["name"],
                                      llm_generate=gen)
        finally:
            _reset_approve(token)
        assert out["status"] == "registered"
        assert out["state"] == "registered"
        assert out["action"] == "verify"
        assert out["approved_by"]
        assert all(c["ok"] for c in out["selftest_cases"])

        # registry.yaml 记录
        rec = cc.registered_contracts(chome)[contract["name"]]
        assert rec["action"] == "verify"
        assert rec["status"] == "registered"
        assert rec["approved_by"]
        assert rec["compiled_at"]
        assert rec["schema"]["name"] == contract["name"]

        # 编译代码落盘 + 0600 + 只读语义
        path = chome / "contracts" / "compiled" / f"{contract['name']}.py"
        assert path.is_file()
        assert (path.stat().st_mode & 0o777) == 0o600

        # 动态注册进工具表
        from tools.registry import registry
        entry = registry.get_entry(contract["name"])
        assert entry is not None
        assert entry.toolset == "contract"
        assert entry.schema["name"] == contract["name"]

    def test_registered_tool_visible_in_tool_definitions(self, contract, chome):
        gen = QueueGen(VALID_WRAPPER)
        token = _approve("once")
        try:
            cc.compile_contract(home=chome, name=contract["name"], llm_generate=gen)
        finally:
            _reset_approve(token)
        from model_tools import _clear_tool_defs_cache, get_tool_definitions
        _clear_tool_defs_cache()
        try:
            defs = get_tool_definitions(enabled_toolsets=["contract"], quiet_mode=True)
        finally:
            _clear_tool_defs_cache()
        names = [d["function"]["name"] for d in defs]
        assert contract["name"] in names
        func = next(d["function"] for d in defs
                    if d["function"]["name"] == contract["name"])
        assert func["parameters"]["required"] == ["target"]
        assert func["parameters"]["properties"]["target"]["type"] == "string"

    def test_compiled_call_direct_with_mock_runner(self, contract, chome):
        gen = QueueGen(VALID_WRAPPER)
        token = _approve("once")
        try:
            cc.compile_contract(home=chome, name=contract["name"], llm_generate=gen)
        finally:
            _reset_approve(token)
        # 直调走真实通道前需拓扑表（沙箱自证的合成拓扑只存在临时目录）——
        # 复用合成拓扑生成器把实体建进 chome，再走 mock runner（不碰真实通道）。
        cc._build_sandbox_topo(chome, contract)
        fn = cc.load_compiled_call(chome, contract["name"])
        assert fn({"target": "service:svc-one"},
                  home=chome, runner=_mock_runner(False)) == {"status": "ok"}
        out = fn({"target": "service:svc-one"},
                 home=chome, runner=_mock_runner(True))
        assert out["error"] == "execution_failed"
        out = fn({"target": "service:ghost-svc"},
                 home=chome, runner=_mock_runner(False))
        assert out["error"] == "entity_not_found"
        out = fn({"pattern": ""}, home=chome, runner=_mock_runner(False))
        assert out["error"] == "invalid_params"

    def test_registered_handler_call_invalid_params(self, contract, chome):
        gen = QueueGen(VALID_WRAPPER)
        token = _approve("once")
        try:
            cc.compile_contract(home=chome, name=contract["name"], llm_generate=gen)
        finally:
            _reset_approve(token)
        from tools.registry import registry
        entry = registry.get_entry(contract["name"])
        out = json.loads(entry.handler({"pattern": ""}))
        assert out["error"] == "invalid_params"
        assert "time_elapsed" in out
        cc._build_sandbox_topo(chome, contract)
        out = json.loads(entry.handler({"target": "service:svc-one"}))
        assert out["error"] == "execution_failed"  # 无 runner 注入 → 真实通道

    def test_register_conflict_rejected(self, contract, chome):
        # 预置 registry.yaml 条目 + 编译代码 + 动态注册 → 重名拒绝
        code = VALID_WRAPPER
        compiled_dir = chome / "contracts" / "compiled"
        compiled_dir.mkdir(parents=True, exist_ok=True)
        (compiled_dir / f"{contract['name']}.py").write_text(code, encoding="utf-8")
        from tools.registry import registry
        schema = cc.contract_to_tool_schema(contract)
        registry.register(name=contract["name"], toolset="contract", schema=schema,
                          handler=lambda args: {"ok": True}, emoji="📜")
        rec = {
            "name": contract["name"], "action": "verify", "status": "registered",
            "compiled_at": "2026-01-01T00:00:00+08:00", "approved_by": "test",
            "path": str(compiled_dir / f"{contract['name']}.py"),
            "toolset": "contract", "schema": schema,
        }
        cc._write_registry(chome, {contract["name"]: rec})
        token = _approve("once")
        try:
            out = cc.compile_contract(home=chome, name=contract["name"],
                                      llm_generate=QueueGen(VALID_WRAPPER))
        finally:
            _reset_approve(token)
        assert out["status"] == "failed"
        assert out["code"] == "register_conflict"

    def test_ensure_hook_idempotent_and_hot_reload(self, contract, chome):
        gen = QueueGen(VALID_WRAPPER)
        token = _approve("once")
        try:
            cc.compile_contract(home=chome, name=contract["name"], llm_generate=gen)
        finally:
            _reset_approve(token)
        # 已注册 → 幂等返回 0
        assert cc.ensure_compiled_tools_registered(chome) == 0
        # 进程内注册表丢失（模拟新进程）→ 从 registry.yaml 热加载
        from tools.registry import registry
        registry.deregister(contract["name"])
        assert registry.get_entry(contract["name"]) is None
        assert cc.ensure_compiled_tools_registered(chome) == 1
        entry = registry.get_entry(contract["name"])
        assert entry is not None and entry.toolset == "contract"
        # 再跑一次仍幂等
        assert cc.ensure_compiled_tools_registered(chome) == 0


# ---------------------------------------------------------------------------
# schema 生成
# ---------------------------------------------------------------------------

class TestSchemaGeneration:
    def test_param_to_json_schema_types(self):
        assert cc._param_to_json_schema({"type": "string"}, "a")["type"] == "string"
        assert cc._param_to_json_schema({"type": "integer"}, "a")["type"] == "integer"
        assert cc._param_to_json_schema({"type": "boolean"}, "a")["type"] == "boolean"
        assert cc._param_to_json_schema({"type": "float"}, "a")["type"] == "number"
        assert cc._param_to_json_schema({"type": "topo_ref", "kind": "service"}, "t")["type"] == "string"
        enum = cc._param_to_json_schema({"type": "enum", "values": ["a", "b"]}, "e")
        assert enum["type"] == "string" and enum["enum"] == ["a", "b"]
        lst = cc._param_to_json_schema(
            {"type": "list", "items": {"type": "string"}}, "l")
        assert lst["type"] == "array" and lst["items"]["type"] == "string"

    def test_contract_to_tool_schema(self):
        data = _valid_contract()
        schema = cc.contract_to_tool_schema(data)
        assert schema["name"] == "verify-service"
        assert schema["parameters"]["required"] == ["target"]
        assert "action=verify" in schema["description"]
        assert schema["parameters"]["properties"]["target"]["type"] == "string"
        assert "拓扑实体引用" in schema["parameters"]["properties"]["target"]["description"]

    def test_prompt_includes_spec_and_contract(self, contract):
        prompt = cc.build_generation_prompt(contract)
        assert "薄包装" in prompt
        assert "invalid_params" in prompt
        assert "verify-service" in prompt
        note = cc.build_generation_prompt(contract, retry_note="上一轮自证失败")
        assert "上一轮自证失败" in note

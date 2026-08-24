"""YAPL 主框架阶段 B：编译生成管线（契约 → 薄包装 → 沙箱自证 → 审批 → 注册）。

设计依据：yapl-design.md 第十三章 §13.4（2026-08-24 盘完）。管线：

    validated → generating → selftested → approved → registered / failed(原因)

- 生成：调用主模型（config model.default 路径）按固定生成规范产出 Python
  薄包装（参数校验 / topo_ref 解析 / 分派调 P4 handler / 结果包装）；
  ast.parse 语法检查先行，语法错直接重试不进沙箱；越界扫描（subprocess /
  os.system / socket / 动态执行）拒绝。
- 自证：独立子进程 + 临时目录跑契约 tests 命门（handler 通道 mock——
  ``handler: {status: failed}`` 用例 mock 返回失败，验证错误传播）；全过 =
  自证通过；失败带用例名/期望/实际 → LLM 重试（上限 3 次）；超时（60s）
  防死循环生成物。
- 审批：tirith 扫描（block 拒 / warn 强制人工）+ 内容审批
  （``request_asset_approval``，asset_type=contract）——拒绝不落注册。
- 注册：编译代码落 ``~/.vigil/contracts/compiled/<name>.py``（0600，只读
  语义）+ ``registry.yaml`` 状态记录 + 动态注册进工具表（toolset=contract，
  schema 由契约 params 自动生成，执行入口 = 薄包装 call，带 action 元数据）。
  启动钩子（``ensure_compiled_tools_registered``）把 registry.yaml 已注册
  契约热加载进运行时工具表（幂等）。

安全：生成代码不可信——tirith + 沙箱双保险；沙箱不落盘主目录（临时目录）；
注册代码只读。
"""

from __future__ import annotations

import ast
import getpass
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from tools.contract_tools import (
    load_contract,
    validate_contract,
)


logger = logging.getLogger(__name__)

_COMPILED_DIRNAME = "compiled"
_REGISTRY_FILENAME = "registry.yaml"
_SELFTEST_TIMEOUT_S = 60
_MAX_GENERATION_RETRIES = 3


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _compiled_dir(home: Path) -> Path:
    return Path(home) / "contracts" / _COMPILED_DIRNAME


def _compiled_path(home: Path, name: str) -> Path:
    return _compiled_dir(home) / f"{name}.py"


def _registry_path(home: Path) -> Path:
    return Path(home) / "contracts" / _REGISTRY_FILENAME


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 生成规范（固定模板，注入系统提示；LLM 不得越界）
# ---------------------------------------------------------------------------

_GENERATION_SPEC = """你是 Vigil 运维 agent 的契约编译器。输入是一份 YAPL 契约（YAML），
输出是一段 Python 薄包装源码。硬约束：
1. 只输出 Python 源码（不要 Markdown 围栏、不要解释、不要额外注释段落）。
2. 代码必须自包含、可直接 import。
3. 允许 import 的白名单：json、time、tools.topo_tools（load_topology）、
   tools.topo_ref（resolve_topo_ref）、tools.runbook_exec（resolve_target）、
   tools.runbook_handlers（generate_commands）、tools.contract_runtime
   （run_specs、any_spec_failed）。禁止引入其他模块/依赖。
4. 禁止直接执行逻辑：不得出现 subprocess / os.system / os.popen / socket /
   urllib / requests / shutil / paramiko / __import__ / eval( / exec( 等——
   执行一律走 run_specs（P4 handler 通道）；不修改 handler 内部。
5. 必须定义 call(params, *, home=None, context=None, runner=None) -> dict。
6. 参数校验：按契约 params 逐项——缺必填 → invalid_params；类型不符 →
   invalid_params；enum 值不在 values → invalid_params；list 逐元素校验；
   topo_ref 值必须是非空字符串，兼容 "kind:name" 与裸名（取冒号后段）。
   校验失败返回 {"error": "invalid_params", "message": <原因>}。
7. topo_ref 解析：调 resolve_topo_ref（kind/context/home 传入）做存在性 +
   歧义检查（ValueError → 返回 {"error": "entity_not_found"}）；再调
   resolve_target（home/context 传入）得富化执行目标。
8. 分派：调 generate_commands(<action>, params, target) 得命令规格；调
   run_specs(specs, target, home=home, runner=runner) 执行。
9. 结果包装：按契约 returns 结构返回；任一条命令非零退出
   （any_spec_failed）→ {"error": "execution_failed"}；分派/执行异常 →
   {"error": "execution_failed"}。
10. 错误码：invalid_params（参数错）/ entity_not_found（topo_ref 无匹配或
    歧义）/ execution_failed（handler 失败）。错误形态固定为
    {"error": <错误码>, "message": <原因>}。
参考骨架（按契约填空，不得改变调用约定）：
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
        # ── 参数校验（按契约声明逐项：必填/类型/enum/list/topo_ref 非空）──
        ...
        # ── topo_ref 解析（resolve_topo_ref 存在性/歧义 → resolve_target 富化）──
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
            specs = generate_commands("<ACTION>", p, target)
            results = run_specs(specs, target, home=home, runner=runner)
        except Exception:
            return {"error": "execution_failed", "message": "handler 分派或执行失败"}
        if any_spec_failed(results):
            return {"error": "execution_failed", "message": "handler 命令执行失败"}
        # ── 结果包装（按契约 returns 结构）──
        return {<returns 键值>}"""


_BANNED_PATTERNS = (
    "subprocess", "os.system", "os.popen", "os.exec", "os.spawn",
    "socket.", "urllib", "requests.", "http.client", "paramiko",
    "shutil.", "pty.", "winreg", "ctypes",
    "__import__", "eval(", "exec(", "compile(",
)


def build_generation_prompt(contract: Dict[str, Any],
                            retry_note: Optional[str] = None) -> str:
    """契约 YAML + 生成规范（+ 上一轮自证失败引导）→ LLM 提示。"""
    body = yaml.safe_dump(contract, allow_unicode=True, sort_keys=False)
    prompt = f"{_GENERATION_SPEC}\n\n契约（YAML）：\n```yaml\n{body}\n```"
    if retry_note:
        prompt += (
            "\n\n上一轮生成物未通过沙箱自证（请修正后重出）：\n"
            f"{retry_note}\n"
        )
    prompt += "\n\n请只输出 Python 源码。"
    return prompt


def _extract_python(raw: str) -> str:
    """剥 Markdown 围栏 / 前后缀，取首个 ```python ... ``` 或整体。"""
    text = str(raw or "").strip()
    fence = re.search(r"```(?:python)?\s*(.*?)```", text, re.S)
    if fence:
        return fence.group(1).strip()
    idx = text.find("def call(")
    return text[idx:] if idx >= 0 else text


def _overreach_scan(code: str) -> Optional[str]:
    """生成代码越界扫描：薄包装禁止直接执行/网络/动态执行。"""
    for pat in _BANNED_PATTERNS:
        if pat in code:
            return (
                f"生成代码含越界模式 {pat!r}——薄包装禁止直接执行逻辑"
                "（subprocess/os/socket/网络/动态执行）；执行一律走 "
                "run_specs（P4 handler 通道）"
            )
    return None


# ---------------------------------------------------------------------------
# 默认 LLM 生成器（config model.default 路径，复用现有 LLM 调用机制）
# ---------------------------------------------------------------------------

_last_llm_usage: Dict[str, Any] = {}


def _default_llm_generate(prompt: str) -> str:
    """调用主模型（config model.default 路径）生成薄包装源码。

    复用 chat_api._create_chat_agent 同款运行时解析（resolve_runtime_provider
    + fallback 链）；无工具集（生成不需要工具）；用量记录到模块级
    ``_last_llm_usage``（prompt/completion/total/api_calls/cost_usd），
    供编译结果返回（token 预算评估）。
    """
    global _last_llm_usage
    from hermes_cli.chat_api import _create_chat_agent

    agent = _create_chat_agent(
        chat_session_id=f"contract-compile-{int(time.time() * 1000)}",
        model=None,
        provider=None,
    )
    agent.enabled_toolsets = []
    try:
        code = agent.chat(prompt)
    finally:
        _last_llm_usage = {
            "prompt_tokens": getattr(agent, "session_prompt_tokens", 0),
            "completion_tokens": getattr(agent, "session_completion_tokens", 0),
            "total_tokens": getattr(agent, "session_total_tokens", 0),
            "api_calls": getattr(agent, "session_api_calls", 0),
            "estimated_cost_usd": getattr(agent, "session_estimated_cost_usd", 0.0),
        }
    return code


def _usage_snapshot() -> Optional[Dict[str, Any]]:
    if not _last_llm_usage:
        return None
    return dict(_last_llm_usage)


# ---------------------------------------------------------------------------
# 沙箱自证
# ---------------------------------------------------------------------------

_SANDBOX_RUNNER = r'''
import importlib.util
import json
import os
import sys
from pathlib import Path

payload = json.load(open(sys.argv[1], encoding="utf-8"))
sys.path.insert(0, payload["repo"])
os.environ["VIGIL_HOME"] = payload["sandbox_home"]

# handler 通道 mock（生成代码 import 的 handler 函数——import 前打补丁，
# 不真生成命令；执行走注入 runner，不碰真实通道）。
from tools import runbook_handlers as _rh

def _mock_generate_commands(action, params, target):
    return [{"cmd": "true", "desc": "sandbox mock handler", "argv": ["true"]}]

_rh.generate_commands = _mock_generate_commands

spec = importlib.util.spec_from_file_location(
    "compiled_" + payload["name"], payload["code_path"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def _mock_runner(fail):
    def runner(spec, target):
        return {"exit_code": 1 if fail else 0,
                "stdout": "" if not fail else "mock failure",
                "stderr": "" if not fail else "sandbox mock: handler failed"}
    return runner

def _match(out, expect):
    if "error" in (expect or {}):
        ok = isinstance(out, dict) and out.get("error") == expect["error"]
        return ok, f"期望 error={expect['error']!r}，实际 {out!r}"
    if not isinstance(out, dict):
        return False, f"期望结构 {expect!r}，实际 {out!r}"
    missing = [k for k in (expect or {}) if k not in out or out[k] != expect[k]]
    if missing:
        return False, f"期望 {expect!r}，实际 {out!r}（不匹配键: {missing}）"
    return True, "ok"

results = []
for case in payload["cases"]:
    fail = (case.get("handler") or {}).get("status") == "failed"
    try:
        out = mod.call(dict(case["input"]), home=Path(payload["sandbox_home"]),
                       context=None, runner=_mock_runner(fail))
        ok, detail = _match(out, case["expect"])
        results.append({"name": case.get("name"), "ok": ok,
                        "detail": detail, "output": out})
    except Exception as exc:
        results.append({"name": case.get("name"), "ok": False,
                        "detail": f"异常: {type(exc).__name__}: {exc}",
                        "output": None})

print(json.dumps(results, ensure_ascii=False, default=str))
'''


def _norm_ref(value: Any) -> str:
    s = str(value or "").strip()
    return s.split(":", 1)[-1] if ":" in s else s


def _collect_sandbox_refs(contract: Dict[str, Any]) -> List[str]:
    """自证沙箱拓扑的实体清单：正常用例的 topo_ref 值必须存在；
    ``{error: entity_not_found}`` 用例专属的引用值**不**建实体（验证拒绝路径）。"""
    params = contract.get("params") or {}
    topo_params = {
        k: v for k, v in params.items()
        if v.get("type") == "topo_ref"
        or (v.get("type") == "list" and (v.get("items") or {}).get("type") == "topo_ref")
    }
    present: set = set()
    for case in contract.get("tests") or []:
        inp = case.get("input") or {}
        is_enf = (case.get("expect") or {}).get("error") == "entity_not_found"
        for key, value in inp.items():
            if key not in topo_params:
                continue
            spec = topo_params[key]
            values = value if spec.get("type") == "list" else [value]
            for v in values:
                ref = _norm_ref(v)
                if ref and not is_enf:
                    # 只建正常用例引用的实体——entity_not_found 用例专属的
                    # 引用值不建实体（验证拒绝路径），同一实体既正常又不存在
                    # = 契约自相矛盾，沙箱按"存在"建，该用例自然失败。
                    present.add(ref)
    return sorted(present)


def _build_sandbox_topo(sandbox_home: Path, contract: Dict[str, Any]) -> None:
    """合成沙箱拓扑：hosts + services（测试用例引用到的实体）。"""
    (sandbox_home / "services").mkdir(parents=True, exist_ok=True)
    topology = {
        "version": 4,
        "environments": [{"name": "sandbox"}],
        "clusters": [{"name": "sandbox", "type": "docker",
                      "env": "sandbox", "host_groups": []}],
        "hosts": [{"name": "sandbox-host", "type": "host", "env": "sandbox",
                   "cluster": "sandbox", "endpoint": "127.0.0.1",
                   "os": "sandbox"}],
    }
    (sandbox_home / "topology.yaml").write_text(
        yaml.safe_dump(topology, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    services = [{"name": ref, "type": "service", "managed_by": "docker_compose",
                 "source": "manual"}
                for ref in _collect_sandbox_refs(contract)]
    (sandbox_home / "services" / "sandbox-host.yaml").write_text(
        yaml.safe_dump({"host": "sandbox-host", "services": services},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")


def _format_selftest_failure(result: Dict[str, Any]) -> str:
    lines = [f"沙箱自证未通过（{sum(1 for c in result.get('cases') or [] if not c.get('ok'))}"
             f"/{len(result.get('cases') or [])} 用例失败）"]
    for case in result.get("cases") or []:
        if not case.get("ok"):
            lines.append(
                f"- 用例 {case.get('name')!r}：{case.get('detail') or '未通过'}"
            )
    return "\n".join(lines)


def sandbox_selftest(code: str, contract: Dict[str, Any], home: Path,
                     timeout: int = _SELFTEST_TIMEOUT_S) -> Dict[str, Any]:
    """独立子进程 + 临时目录跑 tests 命门（handler 通道 mock）。全过 = 自证通过。"""
    with tempfile.TemporaryDirectory(prefix="vigil-contract-sandbox-") as td:
        td_path = Path(td)
        name = contract.get("name") or "contract"
        code_path = td_path / f"{name}.py"
        code_path.write_text(code, encoding="utf-8")
        sandbox_home = td_path / "sandbox-home"
        sandbox_home.mkdir(parents=True)
        _build_sandbox_topo(sandbox_home, contract)
        payload_path = td_path / "payload.json"
        payload_path.write_text(json.dumps({
            "repo": str(Path(__file__).resolve().parent.parent),
            "name": name,
            "code_path": str(code_path),
            "sandbox_home": str(sandbox_home),
            "cases": contract.get("tests") or [],
        }, ensure_ascii=False), encoding="utf-8")
        runner_path = td_path / "runner.py"
        runner_path.write_text(_SANDBOX_RUNNER, encoding="utf-8")
        env = dict(os.environ)
        env["VIGIL_HOME"] = str(sandbox_home)
        try:
            proc = subprocess.run(
                [sys.executable, str(runner_path), str(payload_path)],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace", env=env,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "cases": [],
                    "error": f"沙箱自证超时（{timeout}s）——生成物疑似死循环"}
        if proc.returncode != 0:
            return {"ok": False, "cases": [],
                    "error": f"沙箱 runner 异常退出（{proc.returncode}）: "
                             f"{(proc.stderr or '')[-2000:]}"}
        try:
            cases = json.loads(proc.stdout)
        except Exception as exc:
            return {"ok": False, "cases": [],
                    "error": f"沙箱输出解析失败: {exc}; 输出: {(proc.stdout or '')[-500:]}"}
        ok = all(c.get("ok") for c in cases)
        return {"ok": ok, "cases": cases,
                "error": None if ok else _format_selftest_failure({"cases": cases})}


# ---------------------------------------------------------------------------
# schema 生成 / 已注册契约装载
# ---------------------------------------------------------------------------

def _param_to_json_schema(pspec: Dict[str, Any], name: str) -> Dict[str, Any]:
    t = pspec.get("type")
    desc = f"参数 {name}"
    if t == "string":
        return {"type": "string", "description": desc}
    if t == "integer":
        return {"type": "integer", "description": desc}
    if t == "boolean":
        return {"type": "boolean", "description": desc}
    if t == "float":
        return {"type": "number", "description": desc}
    if t == "topo_ref":
        return {"type": "string",
                "description": f"{desc}——拓扑实体引用（kind={pspec.get('kind')}，"
                               "裸名或 kind:name 形态）"}
    if t == "enum":
        values = pspec.get("values") or []
        if values and all(isinstance(v, bool) for v in values):
            jtype = "boolean"
        elif values and all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            jtype = "integer"
        elif values and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            jtype = "number"
        else:
            jtype = "string"
        return {"type": jtype, "enum": values, "description": desc}
    if t == "list":
        return {"type": "array",
                "items": _param_to_json_schema(pspec.get("items") or {}, f"{name}[]"),
                "description": desc}
    return {"type": "string", "description": f"{desc}（{t}）"}


def contract_to_tool_schema(contract: Dict[str, Any]) -> Dict[str, Any]:
    """契约 params → 工具 JSON schema（OpenAI function 形态）。"""
    params = contract.get("params") or {}
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for pname, pspec in params.items():
        properties[pname] = _param_to_json_schema(pspec, pname)
        if pspec.get("required") is True:
            required.append(pname)
    return {
        "name": contract.get("name"),
        "description": (
            f"YAPL 编译契约工具（action={contract.get('action')}，已过沙箱自证 + "
            f"资产审批注册）。{contract.get('description') or ''}"
        ),
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def _load_registry(home: Path) -> Dict[str, Any]:
    path = _registry_path(home)
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("contract registry read failed: %s", exc)
        return {}


def _write_registry(home: Path, registry: Dict[str, Any]) -> None:
    path = _registry_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(registry, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    tmp.replace(path)


def registered_contracts(home: Optional[Path] = None) -> Dict[str, Any]:
    """registry.yaml 已注册契约记录（供 CLI/查询）。"""
    home = Path(home or _hermes_home()).resolve()
    return _load_registry(home)


def is_registered(home: Optional[Path] = None, name: str = "") -> bool:
    return name in registered_contracts(home)


def load_compiled_call(home: Path, name: str):
    """加载编译契约的 call()（每次现读——契约代码只读，人工不改）。"""
    path = _compiled_path(home, name)
    if not path.is_file():
        raise FileNotFoundError(f"编译契约代码缺失: {path}")
    spec = importlib.util.spec_from_file_location(f"vigil_contract_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "call", None)
    if not callable(fn):
        raise RuntimeError(f"编译契约 {name} 未定义 call()")
    return fn


def _tool_handler_for(name: str, home: Path) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """注册工具执行入口：薄包装 call() + 自动附加 time_elapsed。"""
    def handler(args: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        # registry.dispatch 会附带 task_id/session_id/tool_call_id 等上下文
        # kwargs——薄包装不消费（执行细节在 handler 通道内），忽略即可。
        t0 = time.time()
        try:
            fn = load_compiled_call(home, name)
            out = fn(dict(args or {}), home=home, context=None, runner=None)
        except Exception as exc:
            return {"error": "execution_failed",
                    "message": f"{type(exc).__name__}: {exc}"}
        if isinstance(out, dict):
            out = dict(out)
            out.setdefault("time_elapsed", round(time.time() - t0, 3))
        else:
            out = {"result": out, "time_elapsed": round(time.time() - t0, 3)}
        # registry 工具管线只接受字符串结果（或 multimodal envelope）——
        # 结构化输出以 JSON 串返回，调用层/模型照常消费。
        return json.dumps(out, ensure_ascii=False, default=str)
    return handler


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

def _register_compiled(home: Path, name: str, contract: Dict[str, Any],
                       code: str, approved_by: str) -> Dict[str, Any]:
    """注册：代码落盘（0600）+ 动态注册工具表 + registry.yaml 记录（原子）。"""
    registry = _load_registry(home)
    if name in registry:
        raise ValueError(
            f"契约 {name} 已注册（registry.yaml 已有条目）——重名拒绝；"
            "如需重新编译先注销"
        )
    from tools.registry import registry as tool_registry
    if tool_registry.get_entry(name) is not None:
        raise ValueError(f"契约 {name} 与已注册工具重名——统一命名空间拒绝")
    schema = contract_to_tool_schema(contract)
    handler = _tool_handler_for(name, home)
    path = _compiled_path(home, name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8")
        path.chmod(0o600)
        tool_registry.register(
            name=name,
            toolset="contract",
            schema=schema,
            handler=handler,
            description=schema.get("description", ""),
            emoji="📜",
        )
        record = {
            "name": name,
            "action": contract.get("action"),
            "status": "registered",
            "compiled_at": _now_iso(),
            "approved_by": approved_by,
            "path": str(path),
            "toolset": "contract",
            "schema": schema,
        }
        registry[name] = record
        _write_registry(home, registry)
        return record
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            tool_registry.deregister(name)
        except Exception:
            pass
        raise


def ensure_compiled_tools_registered(home: Optional[Path] = None) -> int:
    """启动钩子：registry.yaml 已注册契约 → 热加载进运行时工具表（幂等）。

    由 model_tools 模块级调用（工具发现机制兼容：动态注册走运行时注册表，
    注册后 ``get_tool_definitions`` 按 generation 增量可见）。返回本次
    新注册数。
    """
    try:
        home = Path(home or _hermes_home()).resolve()
        from tools.registry import registry
        loaded = 0
        for name, rec in (_load_registry(home) or {}).items():
            if registry.get_entry(name) is not None:
                continue
            path = Path(rec.get("path") or _compiled_path(home, name))
            if not path.is_file():
                logger.warning("contract %s compiled code missing: %s", name, path)
                continue
            schema = rec.get("schema") or {}
            if not schema:
                continue
            registry.register(
                name=name,
                toolset=rec.get("toolset") or "contract",
                schema=schema,
                handler=_tool_handler_for(name, home),
                description=schema.get("description", ""),
                emoji="📜",
            )
            loaded += 1
        if loaded:
            logger.info("compiled contracts re-registered: %d", loaded)
        return loaded
    except Exception:
        logger.warning("compiled contract re-registration skipped", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# 编译状态机
# ---------------------------------------------------------------------------

def _code_summary(code: str) -> str:
    lines = [ln for ln in code.splitlines() if ln.strip()]
    head = ""
    for ln in lines[:2]:
        if ln.lstrip().startswith("#"):
            head = ln.strip().lstrip("#").strip()
            break
    return f"{len(lines)} 行" + (f"，首行注释：{head[:60]}" if head else "")


def compile_contract(home: Optional[Path] = None, name: str = "",
                     llm_generate: Optional[Callable[[str], str]] = None,
                     approval_callback: Optional[Callable] = None,
                     timeout: int = _SELFTEST_TIMEOUT_S,
                     max_retries: int = _MAX_GENERATION_RETRIES,
                     runner: Optional[Callable] = None) -> Dict[str, Any]:
    """编译管线：validated → generating → selftested → approved → registered。

    Args:
        home: VIGIL_HOME（测试注入）。
        name: 契约名（contracts/<name>.yaml）。
        llm_generate: 生成器注入（测试 mock；缺省 = 主模型调用）。
        approval_callback: 资产审批回调（``(command, description, **kw) -> str``，
            'once'/'session'/'always'/'deny'）；None = 走标准交互门。
        timeout: 沙箱自证超时（秒）。
        max_retries: 生成-自证循环上限（语法错/越界/自证失败都算一次）。
        runner: 沙箱执行注入（None = 沙箱内 mock runner）。

    Returns:
        成功：``{"status": "registered", ...}``；失败：
        ``{"status": "failed", "stage": <阶段>, "code": <错误码>, "error": ...}``。
    """
    home = Path(home or _hermes_home()).resolve()
    stage = "validated"
    try:
        data = load_contract(home, name)
        if data is None:
            return {"status": "failed", "stage": stage,
                    "code": "contract_not_found",
                    "error": f"契约不存在: {name}（contracts/{name}.yaml）"}
        validate_contract(data, name, home)
        stage = "validated"

        gen = llm_generate or _default_llm_generate
        code: Optional[str] = None
        last_err = ""
        last_cases: List[Dict[str, Any]] = []
        usage: Optional[Dict[str, Any]] = None
        retry_note = ""
        for _attempt in range(1, max_retries + 1):
            prompt = build_generation_prompt(data, retry_note or None)
            raw = gen(prompt)
            usage = _usage_snapshot()
            candidate = _extract_python(raw)
            try:
                ast.parse(candidate)
            except SyntaxError as exc:
                last_err = f"生成物语法错误（第 {exc.lineno} 行）: {exc.msg}"
                retry_note = last_err
                continue
            overreach = _overreach_scan(candidate)
            if overreach:
                last_err = overreach
                retry_note = last_err
                continue
            selftest = sandbox_selftest(candidate, data, home, timeout=timeout)
            last_cases = selftest.get("cases") or []
            if selftest.get("ok"):
                code = candidate
                break
            last_err = selftest.get("error") or "沙箱自证未通过"
            retry_note = last_err
        if code is None:
            return {"status": "failed", "stage": "generating",
                    "code": "generation_failed",
                    "error": last_err or "生成失败（重试上限已到）",
                    "attempts": max_retries,
                    "token_usage": usage}
        stage = "selftested"

        # tirith 扫描（内容级安全，block 拒 / warn 强制人工——照 script_asset 流程）
        tirith_warn = False
        tirith_note = ""
        try:
            from tools.tirith_security import check_command_security
            scan = check_command_security(code)
            action = str(scan.get("action") or "allow")
            if action == "block":
                return {"status": "failed", "stage": stage,
                        "code": "tirith_blocked",
                        "error": f"生成代码被 tirith 拦截（block）："
                                 f"{scan.get('summary') or '安全扫描不通过'}"}
            if action == "warn":
                tirith_warn = True
                tirith_note = f"；tirith warn: {scan.get('summary') or '有安全提示'}"
        except Exception as exc:
            logger.warning("contract tirith 扫描失败（fail-open 继续）: %s", exc)

        from tools.approval import request_asset_approval
        approval = request_asset_approval(
            asset_type="contract",
            asset_name=name,
            description=(
                f"编译契约 {name} 注册审批——沙箱自证已过 + "
                f"{_code_summary(code)}{tirith_note}"
            ),
            env="",
            force_manual=tirith_warn,
            smart_low_risk=False,
            approval_callback=approval_callback,
        )
        if not approval.get("approved"):
            return {"status": "failed", "stage": "approved",
                    "code": "approval_rejected",
                    "error": f"资产审批未通过: {approval.get('message') or '拒绝'}"
                             "——未注册"}
        approved_by = str(approval.get("approved_by") or getpass.getuser())
        stage = "approved"

        try:
            record = _register_compiled(home, name, data, code, approved_by)
        except ValueError as exc:
            return {"status": "failed", "stage": "approved",
                    "code": "register_conflict", "error": str(exc)}
        stage = "registered"
        return {
            "status": "registered",
            "name": name,
            "state": stage,
            "action": data.get("action"),
            "path": str(_compiled_path(home, name)),
            "approved_by": approved_by,
            "schema": contract_to_tool_schema(data),
            "selftest_cases": last_cases,
            "token_usage": usage,
            "record": record,
        }
    except ValueError as exc:
        return {"status": "failed", "stage": stage,
                "code": "contract_invalid", "error": str(exc)}
    except Exception as exc:
        logger.exception("contract compile failed at stage %s", stage)
        return {"status": "failed", "stage": stage,
                "code": "compile_error",
                "error": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "build_generation_prompt",
    "compile_contract",
    "contract_to_tool_schema",
    "ensure_compiled_tools_registered",
    "is_registered",
    "load_compiled_call",
    "registered_contracts",
    "sandbox_selftest",
]

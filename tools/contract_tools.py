"""YAPL 主框架阶段 A：契约加载器 + 分层校验器（contract schema v0.1）。

设计依据：yapl-design.md 第十三章（2026-08-24 逐字段盘完）。契约 =
``~/.vigil/contracts/<name>.yaml``（与 runbooks/ 平级），五块——身份
（name/description/action）、输入（params 类型系统）、输出（returns）、
测试（tests 命门）；**无 permission/steps 字段**（权限 = 操作矩阵唯一裁决，
契约是原子层）。

本批只做基础设施（阶段 A）：加载 + 校验，不做 LLM 生成管线 / 沙箱自证 /
审批注册（阶段 B）。校验失败 = 明确 ValueError（带字段 / 枚举 / 示例引导，
LLM 一次改对），不落盘。

类型系统（克制版）：string / integer / boolean / float / topo_ref(+kind) /
enum(values) / list(items)；不加 json / tuple / array / time。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml


logger = logging.getLogger(__name__)

# 契约名 = kebab-case（小写字母/数字 + 连字符），与 runbook 创建名规则同源
# （tools/runbook_tools._CREATE_NAME_RE），保证 contracts/ 与 runbooks/
# 统一命名空间。
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_ALLOWED_TYPES = ("string", "integer", "boolean", "float",
                  "topo_ref", "enum", "list")
_TOPO_KINDS = ("service", "host", "cluster", "host_group")
_ERROR_CODES = ("entity_not_found", "execution_failed",
                "invalid_params", "unknown")

# 凭据纪律（设计 13.2，OPS-DELTA #88）：契约 params 不支持密码/密钥类参数——
# 参数名含 password/passwd/secret/token/key/api_key/private_key/access_key/
# credential/auth_key/pwd 等词（大小写不敏感；复合名如 ssh_key/api_token/
# db_password 也拒，`_` 视作词边界）；凭据走拓扑 credentials / secret 引用，
# 工具只引用实体，生成代码零凭据接触。
_CREDENTIAL_NAME_PATTERNS = (
    "password", "passwd", "secret", "token", "key", "api[_-]?key",
    "private[_-]?key", "access[_-]?key", "credential", "auth[_-]?key", "pwd",
)
_CREDENTIAL_PARAM_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(" + "|".join(_CREDENTIAL_NAME_PATTERNS) + r")(?:$|[^a-z0-9])"
)
# 值形态疑似凭据（default）：赋值式 password=…/token: … 或 PEM 私钥块。
_PLAINTEXT_CRED_VALUE_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|credential)"
    r"\s*[=:]\s*\S+|-----BEGIN [A-Z0-9 ]*PRIVATE KEY"
)

_warned_ignored_contracts: Set[str] = set()


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _contracts_dir(home: Path) -> Path:
    return Path(home) / "contracts"


# ---------------------------------------------------------------------------
# 加载器
# ---------------------------------------------------------------------------

def _warn_ignored_contract_files(home: Path) -> None:
    """contracts/ 下非 ``.yaml`` 文件的存在必须可见（同 runbooks/ 先例：
    静默失败 = 误导排查）。同一目录只警告一次（防刷屏）。"""
    try:
        cdir = _contracts_dir(home)
        if not cdir.is_dir():
            return
        ignored = sorted(
            p.name for p in cdir.iterdir()
            if p.is_file() and p.suffix.lower() != ".yaml"
            and not p.name.startswith(".")
        )
    except Exception:
        return
    if not ignored:
        return
    key = str(_contracts_dir(home))
    if key in _warned_ignored_contracts:
        return
    _warned_ignored_contracts.add(key)
    shown = ", ".join(ignored[:5])
    more = f" 等 {len(ignored)} 个" if len(ignored) > 5 else ""
    logger.warning(
        "contracts/ 发现 %d 个非 .yaml 文件被忽略（%s%s）——contract 仅支持 "
        ".yaml；其他格式不会被契约加载器加载。",
        len(ignored), shown, more,
    )


def load_contracts(home: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """加载 ``contracts/`` 全部契约：{name: data}。目录缺失 = 空集；
    非 ``.yaml`` 忽略 + 警告；文件名 ↔ 内部 name 不一致 = 硬校验拒绝。"""
    home = Path(home or _hermes_home()).resolve()
    _warn_ignored_contract_files(home)
    cdir = _contracts_dir(home)
    if not cdir.is_dir():
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for path in sorted(cdir.glob("*.yaml")):
        if path.name.startswith("."):
            continue
        stem = path.stem
        out[stem] = _load_one(path, stem)
    return out


def load_contract(home: Optional[Path] = None,
                  name: str = "") -> Optional[Dict[str, Any]]:
    """加载单个契约；名字非法 / 文件不存在 → None；解析/硬校验失败 → ValueError。"""
    home = Path(home or _hermes_home()).resolve()
    if not name or not isinstance(name, str) or not _NAME_RE.match(name):
        return None
    path = _contracts_dir(home) / f"{name}.yaml"
    if not path.is_file():
        return None
    return _load_one(path, name)


def _load_one(path: Path, stem: str) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"contract YAML 解析失败: {stem} ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"contract 顶层必须是对象: {stem}")
    if data.get("name") != stem:
        raise ValueError(
            f"contract 内 name({data.get('name')!r}) 与文件名({stem!r})不一致"
            "——文件名即契约名（<name>.yaml ↔ 内部 name 硬校验）"
        )
    return data


# ---------------------------------------------------------------------------
# 分层校验器（结构 / params / returns / tests）
# ---------------------------------------------------------------------------

def validate_contract(data: Dict[str, Any], name: str,
                      home: Optional[Path] = None) -> None:
    """分层校验契约（schema v0.1）。失败 = ValueError（字段 / 枚举 / 示例
    引导），不落盘。``name`` 为文件名 stem（加载器的硬校验输入）。"""
    home = Path(home or _hermes_home()).resolve()
    if not isinstance(data, dict):
        raise ValueError(f"contract {name}: 顶层必须是对象（YAML 映射）")

    # ── 结构层 ──
    cname = data.get("name")
    if cname != name:
        raise ValueError(
            f"contract 内 name({cname!r}) 与文件名({name!r})不一致"
            "——文件名即契约名（<name>.yaml ↔ 内部 name 硬校验）"
        )
    if not isinstance(cname, str) or not _NAME_RE.match(cname):
        raise ValueError(
            f"contract {name} 名称非法——必须 kebab-case（小写字母/数字 + "
            "连字符，同 runbook 名规则）"
        )
    if not isinstance(data.get("description"), str) or not data["description"].strip():
        raise ValueError(f"contract {name} 缺少 description（非空字符串，说明用途给 LLM）")
    conflicts = _namespace_conflicts(home)
    if name in conflicts:
        raise ValueError(
            f"contract {name} 与统一命名空间冲突（已注册工具 / runbook 名）"
            "——契约名不得与内置工具表、runbooks/ 重名；请改名"
        )
    action = data.get("action")
    if not isinstance(action, str) or not action.strip():
        raise ValueError(
            f"contract {name} 缺少 action——动作枚举元数据（调用层矩阵裁决用；"
            "例: restart {{target: harbor}}）"
        )
    actions = _actions(home)
    if actions is not None and action not in actions:
        raise ValueError(
            f"contract {name} action {action!r} 不在动作词表"
            f"（可用: {', '.join(actions)}；例: restart {{target: harbor}}）。"
            "动作未知无法执行，不兜底——改用词表内动作"
        )
    for banned in ("permission", "steps"):
        if banned in data:
            raise ValueError(
                f"contract {name} 禁止 {banned} 字段——权限 = 操作矩阵唯一裁决"
                "（调用层），契约是原子层；多步编排 = runbook（P2 已做）"
                f"——请删除 {banned} 字段"
            )

    # ── params 层 ──
    params = data.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError(f"contract {name} params 必须是对象（参数名 → 类型声明）")
    for pname, pspec in params.items():
        if not isinstance(pname, str) or not pname.strip() \
                or any(ch.isspace() for ch in pname):
            raise ValueError(
                f"contract {name} params 键必须是空白分隔的非空字符串参数名"
            )
        if _CREDENTIAL_PARAM_RE.search(pname):
            raise ValueError(
                f"contract {name} params.{pname} 是凭据类参数名"
                "（password/secret/token/key/api_key/private_key/credential "
                "命名或疑似凭据，含复合名如 ssh_key/api_token）——契约 params "
                "不支持凭据参数（设计 13.2 凭据纪律：凭据走拓扑 credentials / "
                "secret 引用，工具只引用实体，生成代码零凭据接触）；请删除该"
                "参数或改用 topo_ref 引用目标实体"
            )
        default = pspec.get("default")
        if isinstance(default, str) and _PLAINTEXT_CRED_VALUE_RE.search(default):
            raise ValueError(
                f"contract {name} params.{pname} 的默认值疑似凭据明文"
                "（password=…/token:…/PEM 私钥块）——凭据不落契约；凭据走拓扑 "
                "credentials / secret 引用（设计 13.2）"
            )
        _validate_type_spec(pspec, f"contract {name} params.{pname}")

    # ── returns 层 ──
    returns = data.get("returns") or {}
    if not isinstance(returns, dict):
        raise ValueError(f"contract {name} returns 必须是对象（返回键 → 类型声明）")
    for rname, rspec in returns.items():
        if isinstance(rspec, str):
            # 简写形态（设计 13.1：``restarted: integer``）→ 归一为完整声明
            rspec = {"type": rspec}
        _validate_type_spec(
            rspec, f"contract {name} returns.{rname}",
            allow_topo_ref=False, check_required=False, check_default=False,
        )

    # ── tests 层（命门）──
    tests = data.get("tests")
    if not isinstance(tests, list) or len(tests) < 2:
        raise ValueError(
            f"contract {name} tests 最少 2 个用例（1 正常 + ≥1 错误路径）"
            "——无用例 = 碰运气，校验/注册拒绝"
        )
    for i, tcase in enumerate(tests):
        _validate_test_case(tcase, i, name, params)


def _validate_type_spec(spec: Any, where: str, *,
                        allow_list: bool = True,
                        allow_topo_ref: bool = True,
                        check_required: bool = True,
                        check_default: bool = True) -> None:
    if not isinstance(spec, dict):
        raise ValueError(
            f"{where} 类型声明必须是对象（例: {{type: integer, default: 1}}）"
        )
    t = spec.get("type")
    if not isinstance(t, str) or t not in _ALLOWED_TYPES:
        raise ValueError(
            f"{where} 未知类型 {t!r}——类型系统: {', '.join(_ALLOWED_TYPES)}"
            "（不加 json/tuple/array/time；例: {{type: integer, default: 1}}）"
        )
    if t == "topo_ref":
        if not allow_topo_ref:
            raise ValueError(
                f"{where} 禁止 topo_ref——returns 是数据不是引用；"
                "返回键只声明标量/enum/list"
            )
        kind = spec.get("kind")
        if kind not in _TOPO_KINDS:
            raise ValueError(
                f"{where} topo_ref 必须带 kind（∈ {', '.join(_TOPO_KINDS)}；"
                "例: {{type: topo_ref, kind: service}}）"
            )
    if t == "enum":
        values = spec.get("values")
        if not isinstance(values, list) or not values:
            raise ValueError(
                f"{where} enum 必须带非空 values（标量词表；"
                "例: {{type: enum, values: [graceful, quick]}}）"
            )
        for v in values:
            if isinstance(v, (dict, list)):
                raise ValueError(
                    f"{where} enum values 必须是标量（str/int/float/bool），"
                    "不支持嵌套结构"
                )
    if t == "list":
        if not allow_list:
            raise ValueError(
                f"{where} list 不支持嵌套——items 里不能再声明 list"
                "（批量一层够；复杂结构拆契约）"
            )
        items = spec.get("items")
        if not isinstance(items, dict) or not items.get("type"):
            raise ValueError(
                f"{where} list 必须带 items（元素类型声明；"
                "例: {{type: list, items: {{type: topo_ref, kind: service}}}}）"
            )
        _validate_type_spec(
            items, f"{where}.items", allow_list=False,
            allow_topo_ref=allow_topo_ref,
            check_required=False, check_default=False,
        )
    if check_required:
        req = spec.get("required")
        if req is not None and not isinstance(req, bool):
            raise ValueError(f"{where} required 必须是布尔（true/false）")
        if req is True and "default" in spec:
            raise ValueError(
                f"{where} required=true 时不得给 default——矛盾"
                "（必填参数无默认；请删除 default 或改 required: false）"
            )
    if check_default and "default" in spec:
        _check_value_type(spec["default"], spec, f"{where} 默认值")


def _check_value_type(value: Any, spec: Dict[str, Any], where: str) -> None:
    """测试 input / 默认值按类型声明校验（类型系统六类）。"""
    t = spec.get("type")
    if t == "string":
        if not isinstance(value, str):
            raise ValueError(f"{where} 必须是字符串（string）")
    elif t == "integer":
        if not (isinstance(value, int) and not isinstance(value, bool)):
            raise ValueError(f"{where} 必须是整数（integer，bool 不算）")
    elif t == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{where} 必须是布尔（true/false）")
    elif t == "float":
        if not (isinstance(value, (int, float)) and not isinstance(value, bool)):
            raise ValueError(f"{where} 必须是数字（float，int 兼容）")
    elif t == "topo_ref":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{where} 必须是裸实体名字符串（topo_ref，不带集群）")
    elif t == "enum":
        values = spec.get("values") or []
        if value not in values:
            raise ValueError(
                f"{where} 不在 enum values 内（可用: {', '.join(map(str, values))}）"
            )
    elif t == "list":
        items = spec.get("items") or {}
        if not isinstance(value, list):
            raise ValueError(f"{where} 必须是列表（list）")
        for i, item in enumerate(value):
            _check_value_type(item, items, f"{where}[{i}]")
    else:
        raise ValueError(f"{where} 未知类型 {t!r}——类型声明先于取值校验")


def _validate_test_case(tcase: Any, i: int, name: str,
                        params: Dict[str, Any]) -> None:
    where = f"contract {name} tests[{i}]"
    if not isinstance(tcase, dict):
        raise ValueError(f"{where} 用例必须是对象")
    tname = tcase.get("name")
    if not isinstance(tname, str) or not tname.strip():
        raise ValueError(f"{where} 缺少非空 name（用例名）")
    inp = tcase.get("input")
    if not isinstance(inp, dict):
        raise ValueError(f"{where} input 必须是对象（参数名 → 值）")
    for key in inp:
        if key not in params:
            raise ValueError(
                f"{where} input 含未知参数键 {key!r}"
                f"（声明参数: {', '.join(str(p) for p in params) or '无'}）"
                "——契约是合同，未知键 = 拼写错/越界，拒绝"
            )
    for pname, pspec in params.items():
        if pspec.get("required") is True and pname not in inp:
            raise ValueError(
                f"{where} input 缺失必填参数 {pname!r}（required: true）"
            )
        if pname in inp:
            _check_value_type(inp[pname], pspec, f"{where} input.{pname}")
    expect = tcase.get("expect")
    if not isinstance(expect, dict) or not expect:
        raise ValueError(
            f"{where} expect 必须是对象——精确匹配结构（键值）或 "
            "{{error: <错误码>}} 形态"
        )
    if "error" in expect:
        if set(expect) != {"error"}:
            raise ValueError(
                f"{where} expect 的 {{error: ...}} 形态不能混其他键"
                "（精确匹配结构 或 error 形态二选一）"
            )
        code = expect["error"]
        if not isinstance(code, str) or not code.strip():
            raise ValueError(
                f"{where} expect.error 必须是非空字符串错误码"
                f"（{' / '.join(_ERROR_CODES)}；枚举后续随实现扩展）"
            )
    if "handler" in tcase:
        handler = tcase["handler"]
        if not isinstance(handler, dict) or "status" not in handler:
            raise ValueError(
                f"{where} handler mock 形态: {{status: failed}}"
                "（缺省 = 成功；本批只校验存在性/形态，语义阶段 B 用）"
            )
        status = handler.get("status")
        if status not in ("failed", "success"):
            raise ValueError(
                f"{where} handler.status 只能是 failed/success"
                "（缺省不写 handler = 成功）"
            )


# ---------------------------------------------------------------------------
# 词表 / 命名空间
# ---------------------------------------------------------------------------

def _actions(home: Path) -> Optional[List[str]]:
    """schemas.yaml actions 词表；读取失败/无 schemas → None（跳过枚举检查，
    同 runbook v0.2 校验器惯例）。"""
    try:
        from tools.topo_schemas import schema_list
        return schema_list("actions", home)
    except Exception:
        return None


def _namespace_conflicts(home: Path) -> Set[str]:
    """统一命名空间冲突集：已注册内置工具 + runbooks/ 文件名（不含契约自身
    ——契约名冲突检查只对这两个既存面，contracts/ 内部由文件名唯一性保证）。"""
    return _registered_tool_names() | _runbook_names(home)


def _registered_tool_names() -> Set[str]:
    """已注册工具名（懒触发内置工具发现；发现失败 = 空集，冲突检查降级跳过）。"""
    try:
        from tools.registry import discover_builtin_tools, registry
        discover_builtin_tools()
        return set(registry.get_all_tool_names())
    except Exception:
        return set()


def _runbook_names(home: Path) -> Set[str]:
    """runbooks/*.yaml 文件 stem 集（与 runbook 加载的命名面一致）。"""
    try:
        rdir = Path(home) / "runbooks"
        if not rdir.is_dir():
            return set()
        return {p.stem for p in rdir.glob("*.yaml")
                if not p.name.startswith(".")}
    except Exception:
        return set()


__all__ = [
    "_NAME_RE",
    "_ALLOWED_TYPES",
    "_TOPO_KINDS",
    "load_contract",
    "load_contracts",
    "validate_contract",
]

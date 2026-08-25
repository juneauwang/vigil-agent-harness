"""YAPL P4 runbook v0.2 执行引擎（yapl-design.md §10 + §11.1/11.4/11.5）。

命令由执行器生成（``tools/runbook_handlers``）——LLM 永不接触命令语法；
权限 = 操作矩阵唯一裁决（每步执行时查 matrix，runbook 无 permission 字段）；
``{approve: required}`` 强制人工（覆盖 approvals.mode，无 allowlist 绕过）；
定时执行走资产审批豁免（预审 runbook 跳过逐次审批）+ 事后审计（执行记录
JSONL + 通知）；逃生舱受控（run_script 只引用资产库脚本，不内联）。

步骤流程（§10.1/§10.2）：变量替换 → target 解析 → 审批门（action × env 查
矩阵）→ handler 生成命令 → 现有执行通道 → expect 检查 → 执行记录。

执行记录：``~/.vigil/runtime/runbook_executions.jsonl``（JSONL，追加式，改后
可被前端/审计查询）。stdout/stderr 截断 + 强制 redact（凭据明文永不落盘）。
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from tools.runbook_handlers import (
    UnsupportedCommand,
    evaluate_expect,
    generate_commands,
    generate_expect_check,
)
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_EXEC_TIMEOUT_S = 120
_LEDGER_DIRNAME = "runtime"
_LEDGER_FILENAME = "runbook_executions.jsonl"
_LEDGER_MAX_LINES = 500
_STDOUT_MAX_CHARS = 2000
_lock = threading.RLock()

_VAR_REF_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")

# 执行记录补充脱敏：赋值式凭据形态（password=… / token: …，含引号包裹）——
# agent.redact 对句内带引号形态可能漏网（实测），审计账本宁多掩不漏。
# 与 runbook 校验器的 _PLAINTEXT_SECRET_ASSIGN_RE 同族模式。
_SECRET_ASSIGN_RE = re.compile(
    r'(?i)(password|passwd|secret|token|api[_-]?key|credential|auth|pass)'
    r'\s*[=:]\s*[\'\"]?([^,\'\"\s}]+)'
)


def _hermes_home() -> Path:
    from tools.runbook_tools import _hermes_home as _rb_home
    return _rb_home()


def _redact(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text
        return str(redact_sensitive_text(text or ""))
    except Exception:
        return str(text or "")


def _clip(text: str, limit: int = _STDOUT_MAX_CHARS) -> str:
    text = _redact(text or "")
    text = _SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=***", text)
    if len(text) > limit:
        return text[:limit] + f"\n…(截断 {len(text) - limit} 字符)"
    return text


# ---------------------------------------------------------------------------
# 执行记录（事后审计数据模型）
# ---------------------------------------------------------------------------

def ledger_path(home: Optional[Path] = None) -> Path:
    home = Path(home or _hermes_home()).resolve()
    return home / _LEDGER_DIRNAME / _LEDGER_FILENAME


def record_execution(home: Optional[Path], entry: Dict[str, Any]) -> None:
    """追加一条执行记录（JSONL）。best-effort：失败只记日志，不阻断执行。"""
    home = Path(home or _hermes_home()).resolve()
    path = ledger_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with _lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > _LEDGER_MAX_LINES:
                path.write_text("\n".join(lines[-_LEDGER_MAX_LINES:]) + "\n",
                                encoding="utf-8")
    except Exception as exc:
        logger.warning("runbook 执行记录写入失败: %s", exc)


def recent_executions(home: Optional[Path] = None,
                      limit: int = 50) -> List[Dict[str, Any]]:
    """最近 N 条执行记录（新→旧）。"""
    path = ledger_path(home)
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as exc:
        logger.warning("runbook 执行记录读取失败: %s", exc)
        return []
    rows.reverse()
    return rows[: max(1, min(int(limit) if limit else 50, 200))]


# ---------------------------------------------------------------------------
# target 解析（§10.2 多态：service / host / host_group / cluster）
# ---------------------------------------------------------------------------

def _local_host_names() -> set:
    """本机身份集合：hostname + DNS 名 + 全部本地网卡 IPv4 地址。

    本地/远端判定用——endpoint 命中任一即 local（拓扑里本机服务常以网卡 IP
    作 endpoint，如 WSL eth0 172.18.x.x；只比对 hostname/DNS 名会误判远端
    走 SSH）。网卡枚举（SIOCGIFADDR）Linux/WSL 可用；其他平台回退 DNS 名 +
    默认路由出口 IP。
    """
    import socket
    names = {socket.gethostname()}
    try:
        names.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        pass
    try:
        import fcntl
        import struct
        for _idx, ifname in socket.if_nameindex():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    packed = fcntl.ioctl(
                        s.fileno(), 0x8915,  # SIOCGIFADDR
                        struct.pack("256s", ifname.encode()[:15]),
                    )
                    ip = socket.inet_ntoa(packed[20:24])
                    if ip and not ip.startswith("127."):
                        names.add(ip)
                finally:
                    s.close()
            except Exception:
                continue
    except Exception:
        pass
    try:  # 兜底：默认路由出口 IP（eth0 等）
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            names.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    return names


def _is_local_endpoint(endpoint: Any, host_name: str) -> bool:
    e = str(endpoint or "").strip()
    if not e or e in ("localhost", "127.0.0.1", "::1"):
        return True
    # 只比本机身份（hostname/DNS/网卡 IP）——不能和拓扑 host_name 比！
    # （2026-08-25 实测：阿里云主机 endpoint=39.106.217.32 == host_name，
    # 旧代码 `e == host_name → local` 误判本地，kubectl 命令在本机跑导致
    # "timed out waiting for the condition"，runbook 执行器全部走错机器。）
    if e in _local_host_names():
        return True
    # endpoint 带端口（host:port，如 LAPTOP-T2JA2ERE:5003）→ 剥端口再比对本机
    # 身份（OPS-DELTA #83 实测：本机服务行 endpoint 带端口被误判远端，SSH 回环
    # 报 Host key verification failed）。IPv6 方括号形态一并剥。
    host_part = e.rsplit(":", 1)[0] if e.rsplit(":", 1)[-1].isdigit() else e
    host_part = host_part.strip("[]")
    if host_part in ("localhost", "127.0.0.1", "::1"):
        return True
    if host_part in _local_host_names():
        return True
    return False


def resolve_target(home: Path, topo: Dict[str, Any],
                   name: str,
                   context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """target 多态解析：service / host / host_group / cluster → 执行目标。

    目标不存在 = 拒绝执行 + 提示重查拓扑（§六 topo_ref 执行端校验）。
    ``context``（``{env?, cluster?, host?}`` = runbook 执行范围）仅参与
    service 层重名收敛——host/cluster/host_group 匹配逻辑照旧；service 层
    改走 ``tools.topo_ref.resolve_topo_ref``（不再静默取第一个，重名报歧义）。

    Returns 执行目标 dict（handlers 消费）：
        name / type / env / cluster / managed_by / host（所属主机名）/
        endpoint / os / container / compose_project / compose_service /
        namespace / config_dir / data_dir / remote / host_row。
    """
    from tools.topo_tools import (
        _container_name_for_entity,
        _enrich_entity_detail,
    )

    name = str(name or "").strip()
    if not name:
        raise ValueError("target 必填（拓扑实体引用）")
    clusters = {
        str(c.get("name")): c
        for c in (topo.get("clusters") or [])
        if isinstance(c, dict) and c.get("name")
    }
    if name in clusters:
        c = clusters[name]
        endpoint = str(c.get("endpoint") or "").strip()
        cred = c.get("credential") or {}
        return {
            "name": name,
            "type": "cluster",
            "env": str(c.get("env") or ""),
            "cluster": name,
            "managed_by": "",
            "host": "",
            "endpoint": endpoint,
            "os": "",
            "container": "",
            "compose_project": "",
            "compose_service": "",
            "namespace": str((c.get("attrs") or {}).get("namespace") or ""),
            "config_dir": "",
            "data_dir": "",
            "remote": bool(endpoint and not _is_local_endpoint(endpoint, "")),
            "host_row": {},
        }
    for c in clusters.values():
        for hg in c.get("host_groups") or []:
            hg_name = hg.get("name") if isinstance(hg, dict) else hg
            if str(hg_name or "") == name:
                return {
                    "name": name,
                    "type": "host_group",
                    "env": str(c.get("env") or ""),
                    "cluster": str(c.get("name") or ""),
                    "managed_by": "",
                    "host": "",
                    "endpoint": str(c.get("endpoint") or "").strip(),
                    "os": "",
                    "container": "",
                    "compose_project": "",
                    "compose_service": "",
                    "namespace": str((c.get("attrs") or {}).get("namespace") or ""),
                    "config_dir": "",
                    "data_dir": "",
                    "remote": False,
                    "host_row": {},
                }

    hosts = {str(h.get("name")): h for h in (topo.get("hosts") or [])
             if isinstance(h, dict) and h.get("name")}
    entity = hosts.get(name)
    if entity is None:
        # v0.2/3 存量 cross_host 层并入 host 层（与旧 _all_core_entities
        # 列表序 next() 命中行为一致：第一层 host/cross_host 先于 service）。
        for ch in topo.get("cross_host") or []:
            if isinstance(ch, dict) and str(ch.get("name")) == name:
                entity = ch
                break
    if entity is None:
        from tools.topo_ref import resolve_topo_ref
        entity = resolve_topo_ref(topo, name, kind="service",
                                  context=context, home=home)
    entity = _enrich_entity_detail(home, entity)

    host_name = str(entity.get("_host") or entity.get("name") or "")
    host_row = hosts.get(host_name) or {}
    endpoint = str(entity.get("endpoint") or host_row.get("endpoint") or "").strip()
    snapshot = entity.get("snapshot") if isinstance(entity.get("snapshot"), dict) else {}
    by_runtime = snapshot.get("by_runtime") if isinstance(snapshot.get("by_runtime"), dict) else {}
    common = snapshot.get("common") if isinstance(snapshot.get("common"), dict) else {}
    compose_block = by_runtime.get("docker_compose") if isinstance(by_runtime.get("docker_compose"), dict) else {}
    k8s_block = by_runtime.get("kubectl") if isinstance(by_runtime.get("kubectl"), dict) else {}
    attrs = entity.get("attrs") or {}

    container = _container_name_for_entity(entity)
    compose_project = str(compose_block.get("project") or attrs.get("compose_project") or "").strip()
    services = compose_block.get("services") or compose_block.get("containers") or []
    compose_service = ""
    for s in services:
        if isinstance(s, dict) and s.get("name"):
            compose_service = str(s["name"])
            break
    if compose_service and compose_project and container:
        # docker_compose 的 services[].name 是容器名（docker-nginx-1），compose
        # 服务名（restart/pull 等 compose 命令用）取容器 label
        # com.docker.compose.service（nginx）。docker 不可用/查不到 → 回退存量名。
        try:
            import subprocess as _sp
            out = _sp.run(
                ["docker", "inspect", "-f",
                 "{{index .Config.Labels \"com.docker.compose.service\"}}",
                 container],
                capture_output=True, text=True, timeout=3,
            )
            label_svc = out.stdout.strip()
            if label_svc and label_svc != "<no value>":
                compose_service = label_svc
        except Exception:
            pass

    # managed_by 推断（batch76，OPS-DELTA #91）：实体档案/L2 服务行都可能缺
    # managed_by（v0.3 存量 k8s 实体只写 type=k8s-service/k8s-deploy，无
    # managed_by 字段；v0.4 发现写入也可能被同名合并跳过）。缺失时按运行时
    # 快照/type 推断，保证 kubectl/docker/systemd 通道不丢。推断优先级：
    # 显式 managed_by > snapshot.by_runtime 键（topo_discovery 写 by_runtime
    # = {managed_by: 块}，键即发现期 managed_by）> type 映射（与 topo_discovery
    # 写入对齐：k8s-*→kubectl、docker→docker、compose→docker_compose、
    # systemd→systemd）> 空（未知 type 不瞎猜）。只补缺失，不覆盖显式值。
    managed_by = str(entity.get("managed_by") or "").strip()
    if not managed_by:
        for runtime_key in ("kubectl", "docker_compose", "docker", "systemd"):
            if runtime_key in by_runtime:
                managed_by = runtime_key
                break
    if not managed_by:
        etype = str(entity.get("type") or "")
        if etype.startswith("k8s-") or etype in ("kubectl", "k8s"):
            managed_by = "kubectl"
        elif etype in ("docker", "container"):
            managed_by = "docker"
        elif etype in ("docker_compose", "compose"):
            managed_by = "docker_compose"
        elif etype in ("systemd", "service"):
            managed_by = "systemd"

    return {
        "name": name,
        "type": "service" if entity.get("_host") else "host",
        "env": str(entity.get("env") or host_row.get("env") or ""),
        "cluster": str(entity.get("cluster") or host_row.get("cluster") or "default"),
        "managed_by": managed_by,
        "host": host_name,
        "endpoint": endpoint,
        "os": str(host_row.get("os") or ""),
        "container": container,
        "compose_project": compose_project,
        "compose_service": compose_service,
        "namespace": str(attrs.get("namespace") or k8s_block.get("namespace") or ""),
        "config_dir": str(common.get("config_dir") or ""),
        "data_dir": str(common.get("data_dir") or ""),
        "remote": bool(endpoint and not _is_local_endpoint(endpoint, host_name)),
        "host_row": host_row,
    }


# ---------------------------------------------------------------------------
# 变量替换（§10.5）
# ---------------------------------------------------------------------------

def substitute_text(text: str, step_values: Dict[str, Dict[str, Any]],
                    trigger_ctx: Dict[str, Any], where: str) -> str:
    """``{{ steps.<id>.params.<key> }}`` / ``{{ steps.<id>.outputs.<key> }}`` /
    ``{{ trigger_context.<field> }}`` → 实际值。未执行/不存在/未注入 = 报错停。"""

    def _lookup(path: str) -> str:
        segs = path.split(".")
        if segs[0] == "trigger_context":
            if len(segs) != 2:
                raise ValueError(f"{where} 变量引用 {{{path}}} 不合法——"
                                 "trigger_context 引用形如 {{ trigger_context.alertname }}")
            key = segs[1]
            if key not in trigger_ctx:
                raise ValueError(
                    f"{where} 变量引用 {{{path}}} 的字段 {key!r} 未注入触发上下文"
                    f"（可用: {sorted(trigger_ctx)}）——触发原因由调度器/调用方注入"
                )
            return str(trigger_ctx[key])
        if segs[0] == "steps":
            if len(segs) < 4 or segs[2] not in ("params", "outputs"):
                raise ValueError(
                    f"{where} 变量引用 {{{path}}} 不合法——必须是 "
                    "{{ steps.<id>.params.<key> }} / {{ steps.<id>.outputs.<key> }}"
                )
            ref_id = segs[1]
            bucket = step_values.get(ref_id)
            if bucket is None:
                raise ValueError(
                    f"{where} 变量引用 {{{path}}} 指向步骤 {ref_id!r}——该步骤"
                    "尚未执行或不存在（步骤按顺序执行，只能引用已执行步骤）"
                )
            sub: Any = bucket
            for key in segs[2:]:
                if isinstance(sub, dict) and key in sub:
                    sub = sub[key]
                else:
                    raise ValueError(
                        f"{where} 变量引用 {{{path}}} 的 {key!r} 不存在于步骤 "
                        f"{ref_id!r} 的 {segs[2]}"
                    )
            if isinstance(sub, (dict, list)):
                raise ValueError(
                    f"{where} 变量引用 {{{path}}} 必须引用标量值，收到 "
                    f"{type(sub).__name__}"
                )
            return str(sub)
        raise ValueError(f"{where} 变量引用 {{{path}}} 不合法")

    return _VAR_REF_RE.sub(lambda m: _lookup(m.group(1).strip()), str(text))


def substitute_params(params: Any, step_values: Dict[str, Dict[str, Any]],
                      trigger_ctx: Dict[str, Any], where: str) -> Any:
    """深拷贝 params 并替换全部字符串引用。"""
    if isinstance(params, dict):
        return {k: substitute_params(v, step_values, trigger_ctx, where)
                for k, v in params.items()}
    if isinstance(params, list):
        return [substitute_params(v, step_values, trigger_ctx, where)
                for v in params]
    if isinstance(params, str):
        return substitute_text(params, step_values, trigger_ctx, where)
    return params


# ---------------------------------------------------------------------------
# 审批门（§11.1/11.5）
# ---------------------------------------------------------------------------

def _step_approval(home: Path, env: str, action: str, desc: str,
                   *, force_confirmation: bool = False) -> Optional[str]:
    """交互执行审批门：矩阵档位 execute → 放行；approve → 审批；required →
    强制人工（覆盖 approvals.mode，无 allowlist）。

    ``force_confirmation=True``（batch78，OPS-DELTA #93）：回滚步骤强制人工
    确认——即使矩阵档位是 execute（非 required）也要求确认，覆盖
    approvals.mode=smart 的自动批准；require_confirmation 语义 = 不提供
    session/永久 allowlist，每次都弹人工确认。返回 None = 放行。
    """
    from tools.approval import request_ops_approval
    from tools.matrix_data import get_level, load_matrix_or_empty
    level = get_level(load_matrix_or_empty(home), env, action)["level"]
    if level == "execute" and not force_confirmation:
        return None
    required = level == "required"
    confirmed = required or bool(force_confirmation)
    decision = {
        "action": "approve",
        "grade": "L4" if required else "L2",
        "env": env,
        "require_confirmation": confirmed,
        "description": (
            f"操作矩阵 {action}@{env} 档位"
            + ("=强制人工（{approve: required}，不 smart 不 allowlist）"
               if required else
               ("=execute（回滚步骤强制人工确认覆盖）"
                if force_confirmation else "=approve（交互审批）"))
        ),
    }
    res = request_ops_approval(desc or action, decision)
    if not res.get("approved"):
        return str(res.get("message")
                   or f"审批未通过（{action}@{env}）——fail-closed 不执行")
    return None


def _check_scheduled_exemption(data: Dict[str, Any]) -> Optional[str]:
    """定时执行豁免（§11.4）：预审标记存在 + 内容哈希未漂移 = 豁免逐次审批。

    返回 None = 可豁免；否则拒绝原因（未预审 / 内容被手改后豁免失效）。
    """
    approved_at = data.get("approved_at")
    approved_by = data.get("approved_by")
    approved_version = data.get("approved_version")
    if not approved_at or not approved_by:
        return (
            "runbook 未过资产审批（缺 approved_at/approved_by 预审标记）——"
            "定时执行豁免前提是创建时人工审过；请先 runbook_create 重新创建"
            "（过资产审批落盘预审标记）"
        )
    payload = {k: v for k, v in data.items()
               if k not in ("approved_at", "approved_by", "approved_version")}
    current = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False,
                   default=str).encode("utf-8")
    ).hexdigest()[:16]
    if approved_version and current != approved_version:
        return (
            f"runbook 内容自审批后已被修改（approved_version={approved_version}，"
            f"当前哈希={current}）——执行豁免失效；请 runbook_create 重新过资产"
            "审批后再定时执行"
        )
    return None


# ---------------------------------------------------------------------------
# 执行通道（§10.2：走现有通道，不新造）
# ---------------------------------------------------------------------------

def _exec_local(argv: List[str], timeout: int = _EXEC_TIMEOUT_S) -> Dict[str, Any]:
    try:
        proc = subprocess.run(argv, capture_output=True,
                              text=True, encoding='utf-8', errors='replace',
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"exit_code": 1, "stdout": "", "stderr": f"执行超时（{timeout}s）",
                "timed_out": True}
    except Exception as exc:
        return {"exit_code": 1, "stdout": "", "stderr": f"执行失败：{exc}"}
    return {"exit_code": proc.returncode,
            "stdout": _clip(proc.stdout or ""), "stderr": _clip(proc.stderr or "")}


def _remote_ssh_argv(target: Dict[str, Any]):
    from hermes_cli.subcommands.vssh import (
        _build_ssh_argv,
        _resolve_topology_credential,
    )
    host = str(target.get("host") or "")
    cred = _resolve_topology_credential(host, allow_fallback=False)
    user = str((cred or {}).get("user") or "root")
    port = int((cred or {}).get("port") or 22)
    argv, env = _build_ssh_argv(host, user=user, port=port, cred=cred)
    return argv, env, host


def _exec_remote(target: Dict[str, Any], argv: List[str],
                 timeout: int = _EXEC_TIMEOUT_S) -> Dict[str, Any]:
    from tools.sudo_tool import _ssh_run
    try:
        ssh_argv, ssh_env, host = _remote_ssh_argv(target)
    except Exception as exc:
        return {"exit_code": 1, "stdout": "", "stderr": f"远端凭据解析失败：{exc}"}
    remote_cmd = " ".join(shlex.quote(a) for a in argv)
    try:
        proc = _ssh_run(ssh_argv, ssh_env, remote_cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"exit_code": 1, "stdout": "", "stderr": f"远端执行超时（{timeout}s）",
                "timed_out": True}
    except Exception as exc:
        return {"exit_code": 1, "stdout": "", "stderr": f"远端执行失败：{exc}"}
    return {"exit_code": proc.returncode,
            "stdout": _clip(proc.stdout or ""), "stderr": _clip(proc.stderr or "")}


def _exec_sudo(home: Path, target: Dict[str, Any], cmd: str,
               timeout: int = _EXEC_TIMEOUT_S) -> Dict[str, Any]:
    from hermes_cli.subcommands.vssh import _resolve_topology_credential
    from tools.sudo_tool import _run_local_sudo, _run_remote_sudo
    host = str(target.get("host") or "")
    remote = bool(target.get("remote"))
    try:
        if remote:
            cred = _resolve_topology_credential(host, allow_fallback=False)
            if not cred:
                return {"exit_code": 1, "stdout": "", "stderr":
                        f"远端 sudo 需要拓扑表 {host} 的 credential（vault）引用",
                        "blocked": True}
            user = str(cred.get("user") or "root")
            port = int(cred.get("port") or 22)
            proc = _run_remote_sudo(host, user, port, cmd, cred)
        else:
            cred = _resolve_topology_credential(host, allow_fallback=False) or {}
            if not cred:
                return {"exit_code": 1, "stdout": "", "stderr":
                        "本地 sudo 需要拓扑表 credential（vault/askpass）引用——"
                        "提权命令请先补充 credential 声明或由用户手动执行",
                        "blocked": True}
            proc = _run_local_sudo(cmd, cred)
    except subprocess.TimeoutExpired:
        return {"exit_code": 1, "stdout": "", "stderr": f"sudo 执行超时（{timeout}s）",
                "timed_out": True}
    except Exception as exc:
        return {"exit_code": 1, "stdout": "", "stderr": f"sudo 执行失败：{exc}"}
    return {"exit_code": proc.returncode,
            "stdout": _clip(proc.stdout or ""), "stderr": _clip(proc.stderr or "")}


def _exec_transfer(home: Path, spec: Dict[str, Any],
                   timeout: int = _EXEC_TIMEOUT_S) -> Dict[str, Any]:
    """transfer_file 三形态：本→本 / 本→远 / 远→本 / 远→远（scp 直传）。"""
    source = spec.get("source") or {}
    dest = spec.get("dest") or {}
    src_host = str(source.get("host") or "").strip()
    dst_host = str(dest.get("host") or "").strip()
    src_path = str(source.get("path") or "").strip()
    dst_path = str(dest.get("path") or "").strip()
    if not src_path or not dst_path:
        return {"exit_code": 1, "stdout": "", "stderr":
                "transfer_file 需要 source.path 与 dest.path"}
    if not src_host and not dst_host:
        return _exec_local(["cp", "-a", src_path, dst_path], timeout)

    from hermes_cli.subcommands.vssh import _build_ssh_argv, _resolve_topology_credential
    from tools.sudo_tool import _scp_argv_from_ssh

    def _scp_argv_for(host: str, local: str, remote_path: str, upload: bool):
        cred = _resolve_topology_credential(host, allow_fallback=False)
        user = str((cred or {}).get("user") or "root")
        port = int((cred or {}).get("port") or 22)
        ssh_argv, ssh_env = _build_ssh_argv(host, user=user, port=port, cred=cred)
        if upload:
            argv = _scp_argv_from_ssh(ssh_argv, Path(local), remote_path)
        else:
            argv = _scp_argv_from_ssh(ssh_argv, Path(local), remote_path)
            argv[-2], argv[-1] = argv[-1], argv[-2]
        return argv, ssh_env

    try:
        if src_host and dst_host:
            # 远→远直传：A 端 key 认证发起（B 需信任 A，否则报错引导分两步）。
            cred_a = _resolve_topology_credential(src_host, allow_fallback=False)
            user_a = str((cred_a or {}).get("user") or "root")
            port_a = int((cred_a or {}).get("port") or 22)
            ssh_argv_a, ssh_env_a = _build_ssh_argv(src_host, user=user_a,
                                                    port=port_a, cred=cred_a)
            prefix = ["scp"]
            for piece in ssh_argv_a[1:-1]:
                if piece == "-p" and prefix[-1] == "scp":
                    pass
                prefix.append(piece)
            argv = prefix + [f"{ssh_argv_a[-1]}:{src_path}",
                             f"{src_host}@{_host_endpoint(src_host)}:{dst_path}"]
            env = ssh_env_a
        elif src_host:
            argv, env = _scp_argv_for(src_host, dst_path, src_path, upload=False)
        else:
            argv, env = _scp_argv_for(dst_host, src_path, dst_path, upload=True)
    except Exception as exc:
        return {"exit_code": 1, "stdout": "", "stderr": f"scp 参数构造失败：{exc}"}
    try:
        proc = subprocess.run(argv, capture_output=True,
                              text=True, encoding='utf-8', errors='replace',
                              timeout=timeout, env=env,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"exit_code": 1, "stdout": "", "stderr": f"scp 超时（{timeout}s）",
                "timed_out": True}
    except Exception as exc:
        return {"exit_code": 1, "stdout": "", "stderr": f"scp 执行失败：{exc}"}
    return {"exit_code": proc.returncode,
            "stdout": _clip(proc.stdout or ""), "stderr": _clip(proc.stderr or "")}


def _host_endpoint(host: str) -> str:
    from hermes_cli.subcommands.vssh import _resolve_topology_credential
    # endpoint 解析：拓扑 host 行 endpoint；本函数只被远→远直传的 dest 端使用。
    from tools.topo_tools import load_topology
    topo = load_topology()
    for h in (topo or {}).get("hosts") or []:
        if isinstance(h, dict) and str(h.get("name")) == host:
            return str(h.get("endpoint") or host)
    return host


def _scripts_dir(home: Path) -> Path:
    from tools.script_assets import scripts_dir as _sd
    return _sd(home)


def _exec_script_asset(home: Path, spec: Dict[str, Any],
                       timeout: int = _EXEC_TIMEOUT_S) -> Dict[str, Any]:
    """run_script：只引用资产库脚本（§10.9 逃生舱受控），不内联。

    脚本资产 = ``~/.vigil/scripts/<name>.sh``（或裸名），审批标记在
    ``~/.vigil/scripts/.meta/<name>.json``（script_asset_create 落盘）。
    引用不存在 / 未经资产审批 / 内容哈希漂移 → 报错引导，不执行。
    """
    name = str(spec.get("script_asset") or "").strip()
    from tools.script_assets import meta_dir, resolve_script_path, scripts_dir
    script_path = resolve_script_path(home, name)
    if script_path is None:
        return {"exit_code": 1, "stdout": "", "stderr":
                f"脚本资产 {name!r} 不存在（{scripts_dir(home)}）——用 "
                "script_asset_create 创建（内容过 tirith 扫描 + 资产审批后落盘"
                "预审标记）；run_script 只引用资产，不内联脚本"}
    meta_path = meta_dir(home) / f"{script_path.name}.json"
    if not meta_path.is_file():
        return {"exit_code": 1, "stdout": "", "stderr":
                f"脚本资产 {name!r} 无审批标记（{meta_path} 缺失）——资产需经 "
                "script_asset_create 过资产审批后才能被 run_script 引用"}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"exit_code": 1, "stdout": "", "stderr":
                f"脚本资产 {name!r} 审批标记损坏：{exc}"}
    if not meta.get("approved_at") or not meta.get("approved_by"):
        return {"exit_code": 1, "stdout": "", "stderr":
                f"脚本资产 {name!r} 缺预审标记（approved_at/approved_by）——"
                "需重新过资产审批"}
    content_hash = hashlib.sha256(script_path.read_bytes()).hexdigest()[:16]
    if meta.get("approved_version") and content_hash != meta.get("approved_version"):
        return {"exit_code": 1, "stdout": "", "stderr":
                f"脚本资产 {name!r} 内容自审批后已被修改（哈希漂移）——执行豁免"
                "失效，需重新过资产审批（script_asset_create --force）"}

    args = [str(a) for a in (spec.get("args") or [])]
    argv = ["bash", str(script_path)] + args
    try:
        proc = subprocess.run(argv, capture_output=True,
                              text=True, encoding='utf-8', errors='replace',
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"exit_code": 1, "stdout": "", "stderr": f"脚本执行超时（{timeout}s）",
                "timed_out": True}
    except Exception as exc:
        return {"exit_code": 1, "stdout": "", "stderr": f"脚本执行失败：{exc}"}
    return {"exit_code": proc.returncode,
            "stdout": _clip(proc.stdout or ""), "stderr": _clip(proc.stderr or "")}


def _run_spec(home: Path, target: Dict[str, Any],
              spec: Dict[str, Any]) -> Dict[str, Any]:
    if "transfer" in spec:
        return _exec_transfer(home, spec["transfer"])
    if "script_asset" in spec:
        return _exec_script_asset(home, spec)
    if spec.get("sudo"):
        cmd = spec["cmd"] if "cmd" in spec else " ".join(spec.get("argv") or [])
        return _exec_sudo(home, target, cmd)
    if "argv" in spec:
        argv = list(spec["argv"])
    else:
        cmd = spec["cmd"]
        argv = ["bash", "-c", cmd] if spec.get("shell") else shlex.split(cmd)
    if target.get("remote"):
        return _exec_remote(target, argv)
    return _exec_local(argv)


# ---------------------------------------------------------------------------
# 步骤执行
# ---------------------------------------------------------------------------

def _run_one_step(step: Dict[str, Any], *, env: str, home: Path,
                  step_values: Dict[str, Dict[str, Any]],
                  trigger_ctx: Dict[str, Any],
                  runner: Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]],
                  approve: Callable[[str, str, str], Optional[str]],
                  where: str,
                  scope: Optional[Dict[str, Any]] = None,
                  emit: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                  phase: str = "runbook",
                  scheduled: bool = False,
                  exec_id: Optional[str] = None,
                  depth: int = 0) -> Dict[str, Any]:
    """执行单个步骤并发出进度事件（OPS-DELTA #80）。

    ``emit(type, fields)`` 收到事件字段（step_id/title/action/target/status/
    detail 等，缺省字段由引擎补）；None = 不播报（LLM 工具路径）。``phase``
    标记 runbook / rollback，供 UI 区分回滚步骤。``scheduled``/``exec_id``
    透传给嵌套执行（子 runbook 引用 / 编译工具调用，YAPL 阶段 C）。``depth``
    嵌套深度（顶层 0；子 runbook 引用每层 +1，超过 _MAX_NESTING_DEPTH 拒绝）。
    """
    step_id = str(step.get("id") or "")
    if emit is not None:
        emit("step_start", {
            "step_id": step_id,
            "title": str(step.get("title") or step_id),
            "action": str(step.get("action") or ""),
            "target": _step_target_label(step),
            "status": "running",
            "phase": phase,
        })
    entry = _run_one_step_impl(step, env=env, home=home, step_values=step_values,
                               trigger_ctx=trigger_ctx, runner=runner,
                               approve=approve, where=where, scope=scope,
                               phase=phase, scheduled=scheduled,
                               exec_id=exec_id, depth=depth)
    if emit is not None:
        emit("step_done" if entry.get("ok") else "step_failed", {
            "step_id": step_id,
            "title": str(step.get("title") or step_id),
            "action": str(step.get("action") or ""),
            "target": _step_target_label(step, entry),
            "status": entry.get("status"),
            "detail": _clip(
                entry.get("error") or _step_output_summary(entry), 2000),
            "phase": phase,
        })
    return entry


def _runbook_targets(data: Dict[str, Any]) -> Set[str]:
    """执行锁用的 target 集合：runbook 步骤 + 回滚场景步骤的 params.target 原始名。"""
    targets: Set[str] = set()

    def _collect(steps: Any) -> None:
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            params = step.get("params")
            if isinstance(params, dict) and params.get("target"):
                targets.add(str(params["target"]))

    _collect(data.get("steps"))
    for rb in data.get("rollback") or []:
        if isinstance(rb, dict):
            _collect(rb.get("steps"))
    return targets


# ---------------------------------------------------------------------------
# expect 轮询策略（batch78，OPS-DELTA #93）
# ---------------------------------------------------------------------------

# 变更类动作：默认轮询等待就绪（batch79，OPS-DELTA #94：4 分钟窗口——
# 2026-08-26 实测 pod Ready 需要 2-4 分钟（镜像拉取 + 启动 + readiness
# probe），120s 偏短；expect.retry 显式声明仍可覆盖，含改短）。
_EXPECT_CHANGE_ACTIONS = frozenset({
    "start", "stop", "restart", "reload", "enable", "disable", "reboot",
    "shutdown", "deploy", "rollback", "scale", "decommission", "backup",
    "restore", "install", "upgrade", "remove",
})
# 只读类动作：默认单次（快查，失败即失败——fetch_log 拿到的日志不因等待而变）。
_EXPECT_READONLY_ACTIONS = frozenset({
    "query", "fetch_log", "verify", "transfer_file", "run_script",
    "apply_config", "runbook",
})
_EXPECT_CHANGE_RETRY = (24, 10)   # attempts, interval(s) = 4 分钟窗口
_EXPECT_SINGLE_RETRY = (1, 0)


def _expect_retry_policy(action: str, expect: Dict[str, Any]) -> tuple:
    """expect 轮询策略：显式 retry 覆盖 > 动作类别默认 > 单次。

    ``expect.retry: {attempts, interval}`` → 按声明；``expect.retry: false``
    → 单次（关闭轮询）；缺省 → 变更类 24×10s（4 分钟）、只读类单次。
    """
    retry = expect.get("retry")
    if retry is False:
        return _EXPECT_SINGLE_RETRY
    if isinstance(retry, dict):
        try:
            attempts = int(retry.get("attempts") or 0)
            interval = int(retry.get("interval") or 0)
        except (TypeError, ValueError):
            return _EXPECT_SINGLE_RETRY
        return max(1, attempts), max(0, interval)
    if action in _EXPECT_CHANGE_ACTIONS:
        return _EXPECT_CHANGE_RETRY
    return _EXPECT_SINGLE_RETRY


def _step_target_label(step: Dict[str, Any],
                       entry: Optional[Dict[str, Any]] = None) -> str:
    """步骤 target 展示：优先已解析实体名（name），否则原始 params.target。"""
    if entry and isinstance(entry.get("target"), dict) and entry["target"].get("name"):
        return str(entry["target"].get("name"))
    params = step.get("params")
    if isinstance(params, dict) and params.get("target"):
        return str(params.get("target"))
    return ""


def _step_output_summary(entry: Dict[str, Any]) -> str:
    """步骤输出摘要（截断防爆）：expect 详情 > 末条命令 stdout。"""
    expect = entry.get("expect")
    if isinstance(expect, dict) and expect.get("detail"):
        return str(expect["detail"])
    commands = entry.get("commands")
    if isinstance(commands, list) and commands:
        last = commands[-1]
        if isinstance(last, dict) and last.get("stdout"):
            return str(last["stdout"])
    return ""


def _run_one_step_impl(step: Dict[str, Any], *, env: str, home: Path,
                       step_values: Dict[str, Dict[str, Any]],
                       trigger_ctx: Dict[str, Any],
                       runner: Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]],
                       approve: Callable[..., Optional[str]],
                       where: str,
                       scope: Optional[Dict[str, Any]] = None,
                       phase: str = "runbook",
                       scheduled: bool = False,
                       exec_id: Optional[str] = None,
                       depth: int = 0) -> Dict[str, Any]:
    step_id = str(step.get("id") or "")
    action = str(step.get("action") or "")
    ctx = f"{where}步骤 {step_id!r}"
    entry: Dict[str, Any] = {"id": step_id, "action": action, "status": "pending"}
    try:
        params = substitute_params(step.get("params") or {}, step_values,
                                   trigger_ctx, ctx)
        entry["params"] = params
        if action == "runbook":
            # YAPL 主框架阶段 C（OPS-DELTA #88）：第 24 动作——嵌套引用。
            # 子 runbook / 编译工具调用走内联分派（不需要 target 解析 + 矩阵
            # 门差异：type=runbook 查 runbook 动作档位；type=tool = run_script
            # 资产预审语义 execute 豁免）。
            return _run_runbook_step(
                step, params, env=env, home=home, step_values=step_values,
                trigger_ctx=trigger_ctx, runner=runner, approve=approve,
                where=where, scope=scope, scheduled=scheduled, exec_id=exec_id,
                depth=depth,
            )
        target: Dict[str, Any] = {}
        if "target" in params:
            from tools.topo_tools import load_topology
            topo = load_topology(home)
            if topo is None:
                raise ValueError(f"{ctx} 拓扑表不存在——target 解析需要拓扑（topo_query 确认实体）")
            target = resolve_target(home, topo, str(params["target"]),
                                    context=scope)
            entry["target"] = {"name": target.get("name"), "type": target.get("type"),
                               "env": target.get("env"), "host": target.get("host"),
                               "managed_by": target.get("managed_by")}
        # 审批门（batch78，OPS-DELTA #93）：rollback 场景步骤强制人工确认——
        # 防用户对"恢复性"操作惯性批准（rollout undo 是 L3 高风险操作，实证
        # expect 误判触发回滚且被批准）。desc 加 ⚠ 回滚前缀 + force_confirmation
        # 覆盖 approvals.mode=smart 自动批准；scheduled 豁免语义不变（_approve
        # 在 scheduled 下直接放行，force 不生效）。
        approve_desc = f"{ctx} {action} {params.get('target', '')}"
        force_confirmation = False
        if phase == "rollback":
            scene = ""
            m = re.match(r"rollback\[([^\]]*)\]", where or "")
            if m:
                scene = m.group(1)
            approve_desc = f"⚠ 回滚（rollback 场景 {scene or '（默认）'}）: {approve_desc}"
            force_confirmation = True
        approve_err = approve(env, action, approve_desc,
                              force_confirmation=force_confirmation)
        if approve_err:
            entry.update({"status": "blocked", "ok": False, "error": approve_err})
            return entry
        specs = generate_commands(action, params, target)
        entry["commands"] = []
        for spec in specs:
            result = runner(spec, target)
            desc = str(spec.get("desc") or spec.get("cmd") or spec.get("argv") or action)
            entry["commands"].append({
                "desc": desc,
                "exit_code": result.get("exit_code"),
                "stdout": _clip(result.get("stdout") or "", 800),
                "stderr": _clip(result.get("stderr") or "", 800),
            })
            if result.get("exit_code") != 0:
                entry.update({
                    "status": "failed", "ok": False,
                    "error": _clip(f"命令失败（exit {result.get('exit_code')}）: {desc}\n"
                                   f"{result.get('stderr') or result.get('stdout') or ''}",
                                   4000),
                })
                return entry
        # expect（§10.3）：声明式检查 + 断言。batch78（OPS-DELTA #93）：默认
        # 轮询——变更类动作（scale/restart/…）等待就绪（默认 24×10s 窗口，
        # batch79：pod Ready 实测需 2-4 分钟），只读类动作单次快查；
        # expect.retry 显式覆盖（{attempts, interval} / false）。
        # 轮询只加等待不改命令内容；失败仍 fail-closed（最后一次结果入 error）。
        expect = step.get("expect")
        if expect:
            expect_specs = generate_expect_check(expect, target)
            attempts, interval = _expect_retry_policy(action, expect)
            check_results: List[Dict[str, Any]] = []
            ok, detail = False, ""
            for attempt in range(1, attempts + 1):
                check_results = [runner(s, target) for s in expect_specs]
                ok, detail = evaluate_expect(expect, check_results)
                if ok:
                    detail = f"第 {attempt}/{attempts} 次尝试通过: {detail}"
                    break
                if attempt < attempts:
                    time.sleep(interval)
            detail = _clip(detail or "", 2000)
            entry["expect"] = {
                "ok": ok,
                "detail": detail,
                "attempts": attempt,
                "retry": {"attempts": attempts, "interval": interval},
            }
            if not ok:
                entry.update({
                    "status": "failed", "ok": False,
                    "error": _clip(
                        f"expect 未通过（{attempts} 次尝试均失败，"
                        f"最后一次: {detail}）", 4000),
                })
                return entry
        entry["status"] = "ok"
        entry["ok"] = True
        outputs = {"exit_code": 0, "stdout": "", "stderr": ""}
        if entry.get("commands"):
            last = entry["commands"][-1]
            outputs = {"exit_code": last.get("exit_code"), "stdout": last.get("stdout"),
                       "stderr": last.get("stderr")}
        step_values[step_id] = {"params": params, "outputs": outputs}
        return entry
    except (ValueError, UnsupportedCommand) as exc:
        entry.update({"status": "failed", "ok": False, "error": str(exc)})
        return entry
    except Exception as exc:
        logger.exception("runbook 步骤执行异常 %s", ctx)
        entry.update({"status": "failed", "ok": False,
                      "error": f"执行异常：{exc}"})
        return entry


def _resolve_on_failure(value: Any, default: str) -> tuple:
    """on_failure 四形态：stop / continue / rollback / {rollback: 场景名}。"""
    if value is None:
        value = default
    if value == "stop":
        return "stop", None
    if value == "continue":
        return "continue", None
    if value == "rollback":
        return "rollback", None
    if isinstance(value, dict) and "rollback" in value:
        return "rollback", str(value.get("rollback") or "")
    raise ValueError(f"on_failure 非法: {value!r}")


def _run_rollback_scenario(data: Dict[str, Any], scenario_name: Optional[str],
                           *, env: str, home: Path,
                           step_values: Dict[str, Dict[str, Any]],
                           trigger_ctx: Dict[str, Any],
                           runner: Callable, approve: Callable,
                           scope: Optional[Dict[str, Any]] = None,
                           emit: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                           phase: str = "rollback",
                           depth: int = 0) -> Dict[str, Any]:
    scenarios = data.get("rollback") or []
    scenario = None
    if scenario_name:
        scenario = next((s for s in scenarios if isinstance(s, dict)
                         and str(s.get("name")) == scenario_name), None)
        if scenario is None:
            return {"ok": False, "error":
                    f"rollback 场景 {scenario_name!r} 不存在（可用: "
                    f"{', '.join(str(s.get('name')) for s in scenarios if isinstance(s, dict))}）"}
    elif scenarios:
        scenario = scenarios[0]
    if scenario is None or not isinstance(scenario, dict):
        return {"ok": False, "error": "runbook 未定义 rollback 场景，无法回滚"}
    rb_steps = scenario.get("steps") or []
    rb_results: List[Dict[str, Any]] = []
    for step in rb_steps:
        res = _run_one_step(step, env=env, home=home, step_values=step_values,
                            trigger_ctx=trigger_ctx, runner=runner, approve=approve,
                            where=f"rollback[{scenario.get('name')}]", scope=scope,
                            emit=emit, phase=phase, depth=depth)
        rb_results.append(res)
        if not res.get("ok"):
            return {"ok": False, "results": rb_results,
                    "error": f"rollback 步骤 {res.get('id')!r} 失败："
                             f"{res.get('error')}——rollback 失败 → 强制 stop，人工介入"}
    return {"ok": True, "results": rb_results}


# ---------------------------------------------------------------------------
# YAPL 主框架阶段 C：嵌套编排（runbook 第 24 动作）
# ---------------------------------------------------------------------------

# 环状防护运行时兜底（§13.5）：关系校验已做引用图 DFS 无环检测，此处再设
# 执行栈深度上限——防校验遗漏 / 文件被外部手改后成环时死循环（fail-closed，
# 超限拒绝执行）。
_MAX_NESTING_DEPTH = 10

def _merge_sub_scope(sub_data: Dict[str, Any],
                     parent_scope: Optional[Dict[str, Any]],
                     parent_env: str) -> Dict[str, Any]:
    """子 runbook 范围继承（yapl-design.md §13.5）：子声明缺省字段从父执行
    上下文补（env/cluster/host——同时是 resolve_topo_ref 的 context）；子声明了
    = 子声明优先，但受父范围约束（父已有该维度且子声明不同 → 拒绝，OPS-DELTA
    #88 注明合并规则）。"""
    scope = dict(parent_scope or {})
    declared_env = str(sub_data.get("env") or "").strip()
    if declared_env:
        if scope.get("env") and str(scope["env"]) != declared_env:
            raise ValueError(
                f"子 runbook {sub_data.get('name') or '?'} 声明 env={declared_env!r} "
                f"超出父范围（父 env={scope.get('env')!r}）——子范围受父约束，"
                "请删子 env 继承父，或改父范围"
            )
        scope["env"] = declared_env
    elif scope.get("env"):
        pass  # 缺省继承父 env
    for key, field in (("cluster", "clusters"), ("host", "hosts")):
        vals = sub_data.get(field)
        if isinstance(vals, list) and len(vals) == 1:
            val = str(vals[0])
            if scope.get(key) and str(scope[key]) != val:
                raise ValueError(
                    f"子 runbook {sub_data.get('name') or '?'} 声明 "
                    f"{field}={val!r} 超出父范围（父 {key}={scope.get(key)!r}）"
                    "——子范围受父约束，请继承父范围或改父范围"
                )
            scope[key] = val
    return scope


def _scope_source_label(sub_data: Dict[str, Any], merged: Dict[str, Any]) -> str:
    """ledger 范围来源标记：declared（子声明了任一范围字段）/ inherited（全继承）。"""
    declared = any(
        sub_data.get(f) for f in ("env", "clusters", "host_groups", "hosts")
    )
    return "declared" if declared else "inherited"


def _run_sub_runbook(ref: str, sub_data: Dict[str, Any], *, env: str, home: Path,
                     trigger_ctx: Dict[str, Any],
                     runner: Callable, approve: Callable,
                     parent_scope: Optional[Dict[str, Any]],
                     scheduled: bool, exec_id: Optional[str],
                     emit: Optional[Callable[[str, Dict[str, Any]], None]],
                     depth: int = 0) -> Dict[str, Any]:
    """执行子 runbook（type=runbook）：范围继承 + 每步照常走现有执行引擎
    （审批门查矩阵 / expect / on_failure——无豁免）+ 子失败子的 on_failure
    先生效（子自己回滚）→ 父引用步骤视为失败。子执行记入 ledger。"""
    from tools.runbook_tools import _validate_runbook
    try:
        _validate_runbook(sub_data, ref, home)
    except ValueError as exc:
        return {"ok": False, "error": f"子 runbook {ref} 校验失败，拒绝执行: {exc}"}
    try:
        sub_scope = _merge_sub_scope(sub_data, parent_scope, env)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    sub_env = str(sub_scope.get("env") or env or "local").strip() or "local"

    def _sub_emit(ev_type: str, fields: Optional[Dict[str, Any]] = None) -> None:
        if emit is None:
            return
        ev: Dict[str, Any] = {
            "type": ev_type,
            "exec_id": exec_id,
            "runbook": ref,
            "version": str(sub_data.get("version") or "2"),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        if fields:
            ev.update(fields)
        try:
            emit(ev)
        except Exception:
            logger.debug("sub-runbook progress callback failed", exc_info=True)

    # 变量不跨层（§13.5）：子步骤用自己的 step_values——父 {{ steps... }} 不
    # 能被子直接引用，跨层传值走 params 显式传入（父执行时已解析）。
    sub_step_values: Dict[str, Dict[str, Any]] = {}
    t0 = time.time()
    results, status, error, rolled_back = _run_steps_loop(
        sub_data, env=sub_env, home=home, step_values=sub_step_values,
        trigger_ctx=trigger_ctx, runner=runner, approve=approve,
        scope=sub_scope, emit=_sub_emit, scheduled=scheduled, exec_id=exec_id,
        where=f"子 runbook {ref} ",
        depth=depth,
    )
    entry: Dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "runbook": ref,
        "version": str(sub_data.get("version") or "2"),
        "env": sub_env,
        "source": trigger_ctx.get("source"),
        "trigger_context": {
            k: (v if isinstance(v, (dict, list)) else str(v))
            for k, v in trigger_ctx.items()
        },
        "result": status,
        "error": _clip(error or "", 4000),
        "rolled_back": rolled_back,
        "steps": results,
        "duration_s": round(time.time() - t0, 2),
        "operator": getpass.getuser(),
        "scope_source": _scope_source_label(sub_data, sub_scope),
        "nested": True,
    }
    if exec_id:
        entry["exec_id"] = exec_id
    record_execution(home, entry)
    if status != "ok":
        return {
            "ok": False,
            "error": (f"子 runbook {ref} 执行失败（result={status}）: "
                      f"{error or '子步骤失败'}"),
            "sub_result": status,
            "sub_steps": results,
            "scope_source": entry["scope_source"],
        }
    return {
        "ok": True,
        "sub_result": status,
        "sub_steps": results,
        "scope_source": entry["scope_source"],
        "sub_env": sub_env,
    }


def _run_runbook_step(step: Dict[str, Any], params: Dict[str, Any], *,
                      env: str, home: Path, step_values: Dict[str, Dict[str, Any]],
                      trigger_ctx: Dict[str, Any],
                      runner: Callable, approve: Callable,
                      where: str, scope: Optional[Dict[str, Any]],
                      scheduled: bool, exec_id: Optional[str],
                      depth: int = 0) -> Dict[str, Any]:
    """runbook 动作分派：type=tool（编译契约工具，run_script 资产预审语义
    execute 豁免）/ type=runbook（子 runbook 嵌套执行）。"""
    step_id = str(step.get("id") or "")
    ref = str(params.get("ref") or "").strip()
    rtype = str(params.get("type") or "").strip()
    if not ref or rtype not in ("runbook", "tool"):
        return {"id": step_id, "action": "runbook", "status": "failed",
                "ok": False, "error": f"{where}步骤 {step_id!r} 的 runbook 引用"
                "缺 ref/type——校验器应已拒绝（type ∈ runbook|tool 必填不猜）"}

    if rtype == "runbook" and depth >= _MAX_NESTING_DEPTH:
        # 运行时兜底（§13.5）：超过嵌套深度上限 → fail-closed 拒绝执行。正常
        # 路径永远到不了（关系校验已拒绝环）；此门只防校验遗漏/文件被外部
        # 手改后成环的死循环。
        return {"id": step_id, "action": "runbook", "status": "failed",
                "ok": False, "error": f"{where}步骤 {step_id!r} 的嵌套深度超过"
                f"上限 {_MAX_NESTING_DEPTH}——runbook 引用链异常（应已在校验层"
                "拒绝环状引用）；请检查 runbooks/ 引用关系"}

    if rtype == "tool":
        # type=tool = run_script 资产预审语义（§13.5/13.6）：工具注册时已过
        # 沙箱自证 + 内容审批，是已预审资产——交互执行 execute（豁免逐次
        # 审批，不查矩阵）；定时触发走父 runbook 资产审批豁免 + 事后审计。
        from tools.contract_compile import load_compiled_call, registered_contracts
        if ref not in (registered_contracts(home) or {}):
            return {"id": step_id, "action": "runbook", "status": "failed",
                    "ok": False, "error": f"编译契约工具 {ref!r} 未注册"
                    "（contracts/registry.yaml 无记录）——先 vigil contract "
                    f"compile {ref}"}
        tool_params = {k: v for k, v in params.items()
                       if k not in ("ref", "type")}
        try:
            fn = load_compiled_call(home, ref)
            out = fn(dict(tool_params), home=home, context=scope, runner=None)
        except Exception as exc:
            return {"id": step_id, "action": "runbook", "status": "failed",
                    "ok": False, "error": f"编译契约工具 {ref} 调用失败: "
                    f"{type(exc).__name__}: {exc}"}
        step_values[step_id] = {
            "params": params,
            "outputs": {"result": out},
        }
        if isinstance(out, dict) and out.get("error"):
            return {"id": step_id, "action": "runbook", "status": "failed",
                    "ok": False, "params": params, "tool": ref, "type": "tool",
                    "output": out, "error": out.get("error")}
        return {"id": step_id, "action": "runbook", "status": "ok", "ok": True,
                "params": params, "tool": ref, "type": "tool", "output": out}

    # type=runbook：父引用步骤查 runbook 动作档位（矩阵未配 = 漏配默认 approve，
    # 保守 §13.6）——与普通动作同一审批门，无豁免。
    approve_err = approve(env, "runbook", f"{where}步骤 {step_id!r} 引用子 runbook {ref}")
    if approve_err:
        return {"id": step_id, "action": "runbook", "status": "blocked",
                "ok": False, "error": approve_err}
    from tools.runbook_tools import _load_runbook
    try:
        sub_data = _load_runbook(home, ref)
    except ValueError as exc:
        return {"id": step_id, "action": "runbook", "status": "failed",
                "ok": False, "error": str(exc)}
    if sub_data is None:
        return {"id": step_id, "action": "runbook", "status": "failed",
                "ok": False, "error": f"子 runbook {ref} 不存在（runbooks/{ref}.yaml）"}
    sub_res = _run_sub_runbook(
        ref, sub_data, env=env, home=home, trigger_ctx=trigger_ctx,
        runner=runner, approve=approve, parent_scope=scope,
        scheduled=scheduled, exec_id=exec_id, emit=None, depth=depth + 1,
    )
    entry: Dict[str, Any] = {
        "id": step_id, "action": "runbook", "type": "runbook",
        "ref": ref, "params": params,
        "status": "ok" if sub_res.get("ok") else "failed",
        "ok": bool(sub_res.get("ok")),
        "sub_result": sub_res.get("sub_result"),
        "sub_steps": sub_res.get("sub_steps") or [],
        "scope_source": sub_res.get("scope_source"),
    }
    if not sub_res.get("ok"):
        entry["error"] = sub_res.get("error") or "子 runbook 执行失败"
    else:
        step_values[step_id] = {
            "params": params,
            "outputs": {"result": sub_res},
        }
    return entry


# ---------------------------------------------------------------------------
# 步骤循环（父/子共用：子 runbook 展开后每步照常走现有执行引擎）
# ---------------------------------------------------------------------------

def _run_steps_loop(data: Dict[str, Any], *, env: str, home: Path,
                    step_values: Dict[str, Dict[str, Any]],
                    trigger_ctx: Dict[str, Any],
                    runner: Callable, approve: Callable,
                    scope: Optional[Dict[str, Any]] = None,
                    emit: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                    scheduled: bool = False,
                    exec_id: Optional[str] = None,
                    depth: int = 0,
                    where: str = "runbook ") -> tuple:
    """执行 steps 循环（on_failure 四形态 + 回滚联动）。返回
    (results, status, error, rolled_back)。父 runbook 与嵌套子 runbook 共用
    ——子失败时子的 on_failure 先生效（stop/rollback 子自己的场景），子最终
    失败 → 父引用步骤视为失败 → 父步骤的 on_failure 生效（回滚联动：父
    on_failure: rollback 时，子已完成步骤的回滚由子自己的 rollback 场景处理
    ——子执行失败本身已触发子的回滚，父回滚只处理父已完成的其他步骤）。"""
    steps = data.get("steps") or []
    results: List[Dict[str, Any]] = []
    status = "ok"
    error: Optional[str] = None
    rolled_back = False
    # runbook 级 on_failure 三形态：stop/continue/rollback 字符串 或
    # {rollback: 场景名} 对象——不能 str() 化（dict 会被串成 "{'rollback': …}"
    # 字符串导致 _resolve_on_failure 拒收；阶段 C 父回滚联动依赖 dict 形态）。
    default_on_failure = data.get("on_failure") or "stop"
    for step in steps:
        if not isinstance(step, dict):
            continue
        res = _run_one_step(step, env=env, home=home, step_values=step_values,
                            trigger_ctx=trigger_ctx, runner=runner, approve=approve,
                            where=where, scope=scope, emit=emit, phase="runbook",
                            scheduled=scheduled, exec_id=exec_id, depth=depth)
        results.append(res)
        if res.get("ok"):
            continue
        error = res.get("error") or f"步骤 {res.get('id')} 失败"
        try:
            on_failure, scene = _resolve_on_failure(
                step.get("on_failure"), default_on_failure)
        except ValueError as exc:
            status = "failed"
            error = str(exc)
            break
        if on_failure == "continue":
            continue
        if on_failure == "rollback":
            if emit is not None:
                emit("rollback_start", {
                    "step_id": str(step.get("id") or ""),
                    "status": "running",
                    "detail": _clip(
                        f"步骤 {step.get('id')} 失败，触发回滚（场景 {scene or '默认'}）", 2000),
                })
            rb = _run_rollback_scenario(
                data, scene, env=env, home=home, step_values=step_values,
                trigger_ctx=trigger_ctx, runner=runner, approve=approve,
                scope=scope, emit=emit, depth=depth)
            status = "rolled_back" if rb.get("ok") else "failed"
            if not rb.get("ok"):
                error = f"{error}；{rb.get('error')}"
            else:
                error = (f"{error}（已执行 rollback 场景 "
                         f"{scene or '（默认）'} 后终止）")
            if emit is not None:
                emit("rollback_done", {
                    "step_id": str(step.get("id") or ""),
                    "status": "ok" if rb.get("ok") else "failed",
                    "detail": _clip(
                        rb.get("error")
                        or f"rollback 场景 {scene or '（默认）'} 执行完成", 2000),
                })
            results.append({"id": "__rollback__", "action": "rollback",
                            "status": status, "ok": rb.get("ok"),
                            "steps": rb.get("results", []),
                            "error": rb.get("error")})
            rolled_back = True
            break
        status = "failed"
        break
    return results, status, error, rolled_back


# ---------------------------------------------------------------------------
# 执行引擎
# ---------------------------------------------------------------------------

def execute_runbook(
    data: Dict[str, Any],
    *,
    env: str = "",
    trigger_context: Optional[Dict[str, Any]] = None,
    home: Optional[Path] = None,
    runner: Optional[Callable] = None,
    scheduled: bool = False,
    exec_id: Optional[str] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
    collect_scope: bool = False,
) -> Dict[str, Any]:
    """v0.2 runbook 执行（引擎核心）。

    Args:
        data: runbook 数据（_load_runbook 产物）。
        env: 矩阵环境（缺省取 runbook.env 或 local）。
        trigger_context: 触发上下文（§10.6；交互执行默认 source=user）。
        home: VIGIL_HOME（测试注入）。
        runner: 命令执行器（测试注入 mock；缺省 = 真实通道）。
        scheduled: 定时执行（走资产审批豁免 + 事后审计）。
        exec_id: 执行 id（进度流/历史关联；None = 无流，如 LLM 工具路径）。
        progress_callback: 进度事件回调（OPS-DELTA #80）——每步/回滚/终态
            事件实时回调，不落库（实时可见与事后审计分离）；回调异常只记
            日志不阻断执行；None = 不播报。
        collect_scope: 单独运行（standalone）时，runbook 无任何范围声明
            （env/clusters/host_groups/hosts 全缺省）→ 返回待收集状态
            （``needs_scope``），由调用方 clarify 用户收集 env/cluster/host
            后重跑（§13.5）；缺省 False 保持旧语义（env 缺省 local）。
    Returns:
        执行结果 dict（含 steps / result / error / ledger 已落盘）。
    """
    home = Path(home or _hermes_home()).resolve()
    name = str(data.get("name") or "")
    rb_env = str(env or data.get("env") or "local").strip() or "local"
    scheduled = bool(scheduled)
    version = str(data.get("version") or "v0.2")
    scope: Optional[Dict[str, Any]] = None
    if data.get("env"):
        scope = {"env": str(data.get("env"))}
    for key, field in (("cluster", "clusters"), ("host", "hosts")):
        vals = data.get(field)
        if isinstance(vals, list) and len(vals) == 1:
            scope = dict(scope or {})
            scope[key] = str(vals[0])

    # 单独运行无范围声明 → 待收集（§13.5）：执行器标记"范围缺失需收集"，不
    # 执行——由调用方 clarify 用户（目标 env/cluster/host，单台确认模式复用）
    # 后带范围重跑；用户无响应/拒绝 = 不执行（fail-closed）。
    if collect_scope:
        has_declared_scope = any(
            data.get(f) for f in ("env", "clusters", "host_groups", "hosts")
        ) or bool(env)
        if not has_declared_scope:
            return {
                "runbook": name,
                "result": "needs_scope",
                "status": "scope_collection",
                "error": (
                    f"runbook {name} 未声明执行范围（env/clusters/host_groups/"
                    "hosts 全缺省）——单独运行需要收集目标范围：env/cluster/host"
                    "（子 runbook 无范围声明时完全继承父上下文；单独运行靠 "
                    "clarify 收集，用户无响应/拒绝 = 不执行）"
                ),
                "steps": [],
                "duration_s": 0.0,
            }

    def _emit(ev_type: str, fields: Optional[Dict[str, Any]] = None) -> None:
        """构建并回调一个进度事件（带 base 字段；回调失败仅记日志）。"""
        if progress_callback is None:
            return
        ev: Dict[str, Any] = {
            "type": ev_type,
            "exec_id": exec_id,
            "runbook": name,
            "version": version,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        if fields:
            ev.update(fields)
        try:
            progress_callback(ev)
        except Exception:
            logger.debug("runbook progress callback failed", exc_info=True)

    # OPS-DELTA #81 执行级并发锁：同名 runbook / 同目标禁止并发下发。
    # 注册表 + 冲突判定 + 过期兜底在 tools/runbook_lock.py（web POST 预检
    # 409 之外，引擎入口是权威检查——web/定时/LLM 共用同一入口）。
    from tools.runbook_lock import release as _lock_release
    from tools.runbook_lock import try_acquire as _lock_try_acquire

    lock_id = exec_id or f"lock_{name}_{int(time.time() * 1000)}"
    conflict = _lock_try_acquire(
        runbook=name, version=version, exec_id=lock_id,
        targets=_runbook_targets(data), env=rb_env,
    )
    if conflict:
        _emit("runbook_done", {"status": "blocked", "error": conflict})
        return {
            "runbook": name, "env": rb_env, "result": "blocked",
            "error": conflict, "steps": [], "duration_s": 0.0,
            "ledger": str(ledger_path(home)),
        }

    def _execute_locked() -> Dict[str, Any]:
        from tools.runbook_tools import _is_v2_runbook, _validate_runbook
        try:
            if not _is_v2_runbook(data):
                _emit("runbook_done", {"status": "error",
                                       "error": f"runbook {name} 是 schema v0.1（commands 写死）——v0.1 走老执行路径"})
                return {"runbook": name, "result": "error",
                        "error": f"runbook {name} 是 schema v0.1（commands 写死）——"
                                 "v0.1 走老执行路径（runbook_load + terminal 执行），"
                                 "新执行器只处理 v0.2 声明式动作"}
            _validate_runbook(data, name, home)
        except ValueError as exc:
            _emit("runbook_done", {"status": "blocked",
                                   "error": f"runbook 校验失败，拒绝执行: {exc}"})
            return {"runbook": name, "result": "blocked",
                    "error": f"runbook 校验失败，拒绝执行: {exc}"}

        if scheduled:
            exempt_err = _check_scheduled_exemption(data)
            if exempt_err:
                _emit("runbook_done", {"status": "blocked", "error": exempt_err})
                return {"runbook": name, "result": "blocked", "error": exempt_err}

        trigger_ctx: Dict[str, Any] = {
            "source": "schedule" if scheduled else "user",
            "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        if isinstance(trigger_context, dict):
            for k, v in trigger_context.items():
                if k not in trigger_ctx:
                    trigger_ctx[str(k)] = v

        def _runner(spec: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
            if runner is not None:
                return runner(spec, target)
            return _run_spec(home, target, spec)

        def _approve(step_env: str, action: str, desc: str,
                     *, force_confirmation: bool = False) -> Optional[str]:
            if scheduled:
                return None  # 定时豁免：已预审 runbook 跳过逐次审批（§11.4）
            return _step_approval(home, step_env, action, desc,
                                  force_confirmation=force_confirmation)

        step_values: Dict[str, Dict[str, Any]] = {}
        t0 = time.time()
        results, status, error, rolled_back = _run_steps_loop(
            data, env=rb_env, home=home, step_values=step_values,
            trigger_ctx=trigger_ctx, runner=_runner, approve=_approve,
            scope=scope, emit=_emit, scheduled=scheduled, exec_id=exec_id,
        )

        _emit("runbook_done", {
            "status": status,
            "error": _clip(error or "", 2000),
            "rolled_back": rolled_back,
            "duration_s": round(time.time() - t0, 2),
            "step_count": len(results),
        })

        entry: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "runbook": name,
            "env": rb_env,
            "source": trigger_ctx.get("source"),
            "trigger_context": {
                k: (v if isinstance(v, (dict, list)) else str(v))
                for k, v in trigger_ctx.items()
            },
            "result": status,
            "error": _clip(error or "", 4000),
            "rolled_back": rolled_back,
            "steps": results,
            "duration_s": round(time.time() - t0, 2),
            "operator": getpass.getuser(),
        }
        if exec_id:
            entry["exec_id"] = exec_id
        record_execution(home, entry)
        return {
            "runbook": name,
            "env": rb_env,
            "result": status,
            "error": error,
            "rolled_back": rolled_back,
            "steps": results,
            "duration_s": entry["duration_s"],
            "ledger": str(ledger_path(home)),
        }

    try:
        return _execute_locked()
    finally:
        _lock_release(lock_id)
# ---------------------------------------------------------------------------
# 工具入口
# ---------------------------------------------------------------------------

def runbook_execute(
    runbook: str,
    env: str = "",
    trigger_context: Optional[Dict[str, Any]] = None,
    home: Optional[Path] = None,
    runner: Optional[Callable] = None,
    approval_callback: Optional[Callable] = None,
) -> str:
    """交互执行 runbook（v0.2 声明式动作，命令由执行器生成）。

    LLM 调用本工具执行 v0.2 runbook：每步变量替换 → target 解析 → 矩阵审批门
    （execute/approve/{approve: required} 强制人工）→ handler 生成命令 → 执行
    → expect 检查 → 执行记录。命令由执行器生成，LLM 永不接触命令语法。
    """
    home = Path(home or _hermes_home()).resolve()
    name = str(runbook or "").strip()
    if not name:
        return tool_error("runbook 必填（runbooks/<name>.yaml 的 name）")
    from tools.runbook_tools import _load_runbook, check_runbook_requirements
    if not check_runbook_requirements():
        return tool_error("runbooks/ 无数据——先创建 runbook（runbook_create）")
    data = _load_runbook(home, name)
    if data is None:
        return tool_error(f"runbook 不存在: {name}")
    result = execute_runbook(
        data, env=env, trigger_context=trigger_context, home=home,
        runner=runner, scheduled=False, collect_scope=True,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_DEFAULT_EXECUTE_SCHEMA = {
    "name": "runbook_execute",
    "description": (
        "执行 schema v0.2 runbook（声明式动作，命令由执行器生成——LLM 永不接触"
        "命令语法）。按步骤顺序执行：变量替换 → target 解析（拓扑表）→ 矩阵审批门"
        "（每步 action × env 查操作矩阵：execute 直接执行 / approve 交互审批 / "
        "{approve: required} 强制人工）→ 命令执行 → expect 检查 → 执行记录。"
        "on_failure：stop（默认）/ continue（只读动作）/ rollback（回滚后终止；"
        "rollback 失败强制 stop）。执行前确认 runbook 的 env 与目标实体在拓扑表"
        "（topo_query）。v0.1 runbook（commands 写死）不走本工具——那是老执行"
        "路径。修改矩阵 = 人工操作（vigil matrix CLI / UI），LLM 无 set 路径。"
        "长任务主动播报：runbook 通常耗时数分钟到数小时——执行中在关键节点"
        "（每步完成 / expect 检查通过 / 失败回滚）主动向用户播报进度，不等"
        "用户催促；大步骤完成后简要汇报当前进展与下一步。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "runbook": {
                "type": "string",
                "description": "runbook 名（如 nginx-config-update）。",
            },
            "env": {
                "type": "string",
                "description": "矩阵环境（local/test/dev/prod；缺省取 runbook.env）。",
            },
            "trigger_context": {
                "type": "object",
                "description": "触发上下文注入（§10.6）：如 {alertname: ...}；"
                               "交互执行默认 source=user, triggered_at=now。",
            },
        },
        "required": ["runbook"],
    },
}


def _execute_handler(args: Dict[str, Any], **kwargs) -> str:
    return runbook_execute(
        runbook=args.get("runbook", ""),
        env=args.get("env") or "",
        trigger_context=args.get("trigger_context"),
    )


# 顶层 registry.register（工具发现机制只认模块顶层调用——_register() 包装会被
# AST 扫描跳过，导致 CLI 运行时工具不加载；YAPL P1-3 曾踩此坑）。
from tools.runbook_tools import check_runbook_requirements
registry.register(
    name="runbook_execute",
    toolset="runbook",
    schema=_DEFAULT_EXECUTE_SCHEMA,
    handler=_execute_handler,
    check_fn=check_runbook_requirements,
    emoji="▶️",
    max_result_size_chars=30_000,
)

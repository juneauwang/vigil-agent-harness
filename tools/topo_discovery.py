"""拓扑自动发现引擎（OPS-DELTA #12，中间市场开箱即用地基）。

零侵入独立模块：SSH 进主机 → 扫 docker / k8s / 监听端口 / GPU → 自动生成
schema v0.3 片段（第一层 host 行 + 第二层 services 索引 + 第三层详情草案），
供 ``vigil topo-discover`` 展示、人工确认后落盘。

设计要点（与 ops-agent-harness.md / OPS-DELTA #12 对齐）：
- **只读**：本模块只采集，不写 topology.yaml——落盘由用户确认后显式调用
  ``write_discovery()``。
- **凭据不落明文**：SSH 由调用方注入 runner（默认 runner 只走 ssh-agent / key，
  密码经 SSH_ASKPASS 从保险箱文件读取，命令串/环境/日志里不出现密码）。
- **L3 命名（OPS-DELTA #42）**：实体详情文件 = ``entities/{cluster}__{host}__{name}.yaml``，
  跨 host 同名应用不互相覆盖；cluster 缺省显示 "default"，文件名按 env 兜底。
- **无半截数据**：SSH 级失败抛 ``DiscoveryError`` 直接中止；单个探针失败
  （docker/kubectl 未安装等）记为 skipped，不影响其余探针。
- **输出标记**：``source: discovered`` + ``last_verified: 今天`` +
  ``needs_review: true``——人工确认前不落盘为权威。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_SSH_TIMEOUT_S = 60
# SSH 认证失败熔断（OPS-DELTA 批次十六 §AD）：agent 连续尝试多种认证方式会耗尽
# OpenSSH MaxAuthTries（默认 6）→ "Too many authentication failures" → 接下来
# 几分钟 ssh 全部被拒（生产环境自伤）。会话级计数（进程内存 dict），host:user
# 独立；失败 3 次熔断，返回可操作错误，不再自动重试。
_SSH_AUTH_BREAKER_LIMIT = 3
_SSH_AUTH_FAILURES: Dict[str, Dict[str, Any]] = {}
_SSH_AUTH_LOCK = threading.Lock()
_SSH_AUTH_FAILURE_HINTS = (
    "permission denied",
    "too many authentication failures",
    "authentication failed",
)
# 容器名/服务名白名单（防路径穿越 + 防把不可打印字符写进文件名）。
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class DiscoveryError(Exception):
    """发现级失败：SSH 不通 / 数据冲突 / 落盘被拒。消息对用户可操作。"""


class ProbeResult:
    """一次远端命令探测的结果（stdout + exit_code + stderr）。"""

    __slots__ = ("stdout", "exit_code", "stderr")

    def __init__(self, stdout: str, exit_code: int = 0, stderr: str = ""):
        self.stdout = stdout or ""
        self.exit_code = exit_code
        self.stderr = stderr or ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


# ---------------------------------------------------------------------------
# Runner（SSH 执行）
# ---------------------------------------------------------------------------

def _sanitize_name(value: str) -> str:
    """名称净化：只保留白名单字符，防止路径穿越/脏名写入。"""
    value = str(value or "").strip().lower()
    value = re.sub(r"[^A-Za-z0-9_.-]", "-", value)
    return value or "unknown"


_ENV_TIERS = ("local", "test", "dev", "prod")


def _env_tier(env: str) -> str:
    """环境名 → 四值档位（写入端：老自定义名按档位映射，权限语义不放松）。

    uat → prod 档、staging → dev 档、其余按名字推导（含 prod → prod、
    尾缀 local/test/dev → 对应档、默认 dev）。读取端映射（带警告）在
    tools/ops_permissions.py。
    """
    lower = str(env or "").strip().lower()
    if lower in _ENV_TIERS:
        return lower
    if lower == "uat":
        return "prod"
    if lower == "staging":
        return "dev"
    if "prod" in lower:
        return "prod"
    if lower.endswith("local"):
        return "local"
    if lower.endswith("test"):
        return "test"
    if lower.endswith("dev"):
        return "dev"
    return "dev"


def _entity_filename(cluster: str, host: str, name: str, fallback_env: str = "") -> str:
    """L3 实体文件名：``entities/{cluster}__{host}__{name}.yaml``（OPS-DELTA #42）。

    三字段都过 :func:`_sanitize_name`（字符集 ``[A-Za-z0-9_.-]``，``__`` 分隔
    无歧义）；cluster 空用 env 兜底，再空用 "default"。同 host 不同应用 /
    同应用不同 host / 同应用同 host 不同 cluster 的文件名互不冲突。
    """
    cluster = cluster or fallback_env or "default"
    parts = [_sanitize_name(cluster), _sanitize_name(host), _sanitize_name(name)]
    return f"entities/{'__'.join(parts)}.yaml"


# ---------------------------------------------------------------------------
# SSH 认证失败熔断（OPS-DELTA 批次十六 §AD）
# ---------------------------------------------------------------------------

def _ssh_auth_key(host: str, user: str) -> str:
    return f"{user}@{host}"


def _ssh_auth_failures(host: str, user: str) -> int:
    with _SSH_AUTH_LOCK:
        return int(_SSH_AUTH_FAILURES.get(_ssh_auth_key(host, user), {}).get("count", 0))


def _record_ssh_auth_failure(host: str, user: str) -> None:
    with _SSH_AUTH_LOCK:
        entry = _SSH_AUTH_FAILURES.setdefault(_ssh_auth_key(host, user), {})
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["ts"] = _dt.datetime.now().isoformat()


def _reset_ssh_auth_failures(host: str, user: str) -> None:
    """认证成功后清零——凭据 OK，计数重新开始（§AD 细节 5）。"""
    with _SSH_AUTH_LOCK:
        _SSH_AUTH_FAILURES.pop(_ssh_auth_key(host, user), None)


def _ssh_auth_breaker_tripped(host: str, user: str) -> bool:
    return _ssh_auth_failures(host, user) >= _SSH_AUTH_BREAKER_LIMIT


def _is_ssh_auth_failure(proc: Any) -> bool:
    """认证失败判定：exit 255 + stderr 含 Permission denied / MaxAuthTries 等。"""
    if getattr(proc, "returncode", None) != 255:
        return False
    stderr = (getattr(proc, "stderr", None) or "").lower()
    return any(hint in stderr for hint in _SSH_AUTH_FAILURE_HINTS)


def _ssh_auth_breaker_error(host: str, user: str) -> str:
    n = _ssh_auth_failures(host, user)
    return (
        f"⚠️ SSH 认证失败 {n}/{_SSH_AUTH_BREAKER_LIMIT}——为避免 sshd 限流"
        f"（MaxAuthTries=6）把自己锁出主机（{user}@{host}），已停止自动重试。"
        "请：1) 手动 ssh 验证凭据 2) 或补充拓扑表 credential 声明（vssh 或 topo credential）"
    )


def _build_ssh_runner(host: str, user: str = "root", key_path: Optional[str] = None,
                      askpass_file: Optional[Path] = None,
                      key_passphrase_file: Optional[Path] = None,
                      sudo_password_file: Optional[Path] = None) -> Callable[[str], ProbeResult]:
    """默认 SSH runner：key/agent 认证（BatchMode）或 SSH_ASKPASS 密码文件。

    密码走 SSH_ASKPASS（``askpass_file`` 为 0700 脚本，从保险箱文件读取），
    任何密码明文都不进 argv / 环境变量。
    """
    if not host or not user:
        raise DiscoveryError("SSH 需要 host 与 user（--host / --user）")

    def run(cmd: str) -> ProbeResult:
        # 熔断检查在 runner 调用前（agent 工具/自动探测路径）——该 host:user 已
        # 连续认证失败达到上限，不再自动重试（§AD 细节 4：CLI 交互路径 vssh 不
        # 经过本 runner，不计数不受影响）。
        if _ssh_auth_breaker_tripped(host, user):
            raise DiscoveryError(_ssh_auth_breaker_error(host, user))
        env = dict(os.environ)
        use_password_askpass = askpass_file is not None and askpass_file.is_file()
        use_key_passphrase = key_passphrase_file is not None and key_passphrase_file.is_file()
        use_sudo = sudo_password_file is not None and sudo_password_file.is_file()
        remote_cmd = cmd
        sudo_stdin = None
        if use_sudo:
            # sudo -S 从本地 stdin 读取密码；密码经 askpass 脚本从保险箱读出后
            # 作为 stdin 注入，远程命令串/argv 中不出现明文。
            sudo_stdin = _askpass_output(sudo_password_file) + "\n"
            remote_cmd = f"sudo -S -p '' {cmd}"
        argv = ["ssh", "-o", "ConnectTimeout=10"]
        if not use_password_askpass and not use_key_passphrase:
            # key/agent 认证：BatchMode 确保不交互弹密码（密码路径走 askpass）。
            argv += ["-o", "BatchMode=yes"]
        elif use_key_passphrase:
            # 加密私钥需要 askpass 提供 passphrase；BatchMode 会同时禁掉
            # passphrase askpass，所以这里改用 publickey-only 并关闭 password
            # prompt，防止交互弹窗，同时保留 askpass 注入。
            argv += [
                "-o", "PreferredAuthentications=publickey",
                "-o", "NumberOfPasswordPrompts=0",
            ]
        if key_path:
            argv += ["-i", str(key_path)]
        argv += [f"{user}@{host}", remote_cmd]
        if use_password_askpass or use_key_passphrase:
            # 密码经 SSH_ASKPASS（0700 脚本读保险箱文件）注入，命令串/env 无明文。
            env["SSH_ASKPASS"] = str(key_passphrase_file if use_key_passphrase else askpass_file)
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env.setdefault("DISPLAY", ":0")
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=_SSH_TIMEOUT_S, env=env, input=sudo_stdin)
        except subprocess.TimeoutExpired as exc:
            raise DiscoveryError(f"SSH 连接 {user}@{host} 超时（{_SSH_TIMEOUT_S}s）") from exc
        except OSError as exc:
            raise DiscoveryError(f"无法执行 ssh：{exc}") from exc
        if proc.returncode == 255:
            if _is_ssh_auth_failure(proc):
                _record_ssh_auth_failure(host, user)
                if _ssh_auth_breaker_tripped(host, user):
                    raise DiscoveryError(_ssh_auth_breaker_error(host, user))
            # ssh 自身失败（连接拒绝/认证失败）→ 中止发现，无半截数据。
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise DiscoveryError(
                f"SSH 连接 {user}@{host} 失败（exit 255）："
                f"{detail[-1] if detail else '认证失败或主机不可达'}"
            )
        # 非 255 = ssh 连接成功（远端命令自身的退出码）——凭据 OK，计数清零。
        _reset_ssh_auth_failures(host, user)
        return ProbeResult(proc.stdout, proc.returncode, proc.stderr or "")

    return run


# 本机发现别名（批次十三 C4）：localhost/127.0.0.1/::1（或空 host）→ 直接本地执行
# 探测命令，不走 SSH、不需要凭据（WSL/单机无 sshd 场景）。
_LOCAL_HOST_ALIASES = ("localhost", "127.0.0.1", "::1", "")


def _build_local_runner(sudo_password_file: Optional[Path] = None) -> Callable[[str], ProbeResult]:
    """本机 runner（WSL/单机发现）：直接 subprocess 本地执行探测命令，不经 SSH。

    sudo 场景与 SSH 路径同机制：``sudo_password_file`` 存在 → ``sudo -S -p ''``
    从本地 stdin 注入密码（askpass 读保险箱），命令串/argv 无明文。
    """
    def run(cmd: str) -> ProbeResult:
        use_sudo = sudo_password_file is not None and sudo_password_file.is_file()
        run_cmd = cmd
        sudo_stdin = None
        if use_sudo:
            sudo_stdin = _askpass_output(sudo_password_file) + "\n"
            run_cmd = f"sudo -S -p '' {cmd}"
        try:
            proc = subprocess.run(run_cmd, capture_output=True, text=True,
                                  timeout=_SSH_TIMEOUT_S, shell=True, input=sudo_stdin)
        except subprocess.TimeoutExpired as exc:
            raise DiscoveryError(f"本地命令执行超时（{_SSH_TIMEOUT_S}s）：{cmd}") from exc
        except OSError as exc:
            raise DiscoveryError(f"无法执行本地命令：{exc}") from exc
        return ProbeResult(proc.stdout, proc.returncode, proc.stderr or "")

    return run


def _make_askpass_script(vault_file: Path) -> Path:
    """写 0700 askpass 脚本：输出保险箱文件内容（密码），不进 argv/env。"""
    script = Path(tempfile.mkstemp(prefix="vigil-askpass-", suffix=".sh")[1])
    script.write_text(f"#!/bin/sh\ncat {vault_file}\n", encoding="utf-8")
    os.chmod(script, 0o700)
    return script


def _askpass_output(askpass_file: Path) -> str:
    """执行 askpass 脚本并返回密码明文（仅注入 stdin，不进 argv/日志）。"""
    try:
        proc = subprocess.run(["/bin/sh", str(askpass_file)], capture_output=True,
                              text=True, timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise DiscoveryError("读取保险箱凭据超时（askpass）") from exc
    except OSError as exc:
        raise DiscoveryError(f"无法执行 askpass 脚本：{exc}") from exc
    if proc.returncode != 0:
        raise DiscoveryError("读取保险箱凭据失败（askpass 退出码非 0）")
    return proc.stdout.rstrip("\n")


def _is_likely_permission_denied(res: ProbeResult) -> bool:
    """区分“命令不存在”和“权限不足/daemon 拒绝”两类探测失败。"""
    combined = f"{res.stdout or ''}\n{res.stderr or ''}".lower()
    if "permission" in combined or "denied" in combined or "not allowed" in combined:
        return True
    # 非零退出且不是缺工具时，按用户实测场景保守提示 sudo 重试。
    return res.exit_code != 0 and "command not found" not in combined


# ---------------------------------------------------------------------------
# 探针解析
# ---------------------------------------------------------------------------

def _parse_docker_ps(output: str) -> List[Dict[str, Any]]:
    """docker ps --format '{{json .}}' → 容器列表（Name/Image/Ports/Labels）。"""
    containers: List[Dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        labels = row.get("Labels") or ""
        labels_map: Dict[str, str] = {}
        for part in str(labels).split(","):
            if "=" in part:
                k, _, v = part.partition("=")
                labels_map[k.strip()] = v.strip()
        containers.append({
            "name": str(row.get("Names") or "").strip(),
            "image": str(row.get("Image") or "").strip(),
            "ports": str(row.get("Ports") or "").strip(),
            "state": str(row.get("State") or "").strip(),
            "compose_project": labels_map.get("com.docker.compose.project", ""),
            "compose_service": labels_map.get("com.docker.compose.service", ""),
        })
    return [c for c in containers if c["name"]]


def _parse_published_ports(ports_str: str) -> List[int]:
    """'0.0.0.0:30443->5000/tcp, :::30443->5000/tcp' → [30443]（去重）。"""
    ports: List[int] = []
    for part in str(ports_str).split(","):
        part = part.strip()
        m = re.match(r".*?(\d+)->", part)
        if m:
            port = int(m.group(1))
        else:
            m = re.match(r".*?(\d+)/", part)
            if not m:
                continue
            port = int(m.group(1))
        if port not in ports:
            ports.append(port)
    return ports


def _parse_compose_ls(output: str) -> List[str]:
    """docker compose ls --format json → compose 项目名列表。"""
    out = output.strip()
    if not out:
        return []
    projects: List[str] = []
    if out.startswith("["):
        try:
            rows = json.loads(out)
        except json.JSONDecodeError:
            rows = []
        for row in rows or []:
            if isinstance(row, dict) and row.get("Name"):
                projects.append(str(row["Name"]))
        return projects
    # 非 json 兜底：第一列是项目名。
    for line in out.splitlines():
        line = line.strip()
        if line and not line.startswith("NAME"):
            projects.append(line.split()[0])
    return projects


def _parse_ss_tlnp(output: str) -> List[Dict[str, Any]]:
    """ss -tlnp → 非 loopback 监听端口（Local Address:Port）。"""
    listeners: List[Dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("State"):
            continue
        parts = line.split()
        if len(parts) < 5 or parts[0] != "LISTEN":
            continue
        addr_port = parts[3]
        host_part, _, port_part = addr_port.rpartition(":")
        host = host_part.strip("[]")
        if not port_part.isdigit():
            continue
        if host in ("127.0.0.1", "::1", "localhost", ""):
            continue
        listeners.append({"host": host, "port": int(port_part)})
    return listeners


def _ss_proc_key(value: str) -> str:
    """进程名/unit 名 → 匹配键（仅字母数字，容忍 node-exporter/node_exporter 差异）。"""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _parse_ss_proc_ports(output: str) -> Dict[str, List[int]]:
    """ss -tlnp 原始输出 → 进程名 → 监听端口列表（含 loopback，供 systemd 无端口过滤）。

    ``users:(("prometheus",pid=3,fd=5))`` 这类 Process 列解析出进程名；进程名按
    :func:`_ss_proc_key` 归一，与 systemd unit 名匹配（批次十八 B 配套：无监听端口的
    systemd 服务不自动入表，保留 pending_review 等确认）。
    """
    mapping: Dict[str, List[int]] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("State"):
            continue
        parts = line.split()
        if len(parts) < 5 or parts[0] != "LISTEN":
            continue
        _, _, port_part = parts[3].rpartition(":")
        if not port_part.isdigit():
            continue
        port = int(port_part)
        for name in re.findall(r'"([^"]+)"\s*,\s*pid=', line):
            mapping.setdefault(_ss_proc_key(name), []).append(port)
    return mapping


# systemctl 系统内部服务黑名单（批次十三 C1）：系统自身服务不进拓扑噪音，
# 用户部署的业务服务（node-exporter/prometheus/nginx/mysql/redis/postgres 等）
# 不得误伤。前缀在原始 unit 名上匹配（user@1000.service 前缀 user@）；精确名
# 按去 .service 后缀的单元名匹配。
_SYSTEMD_SYSTEM_SERVICE_PREFIXES = (
    "systemd-",          # systemd 自身（journald/logind/udevd/resolved 等）
    "user@",             # 用户会话
    "user-runtime-dir@", # 用户会话运行时目录
    "getty",             # 登录终端
    "serial-getty",      # 串口登录终端
)
_SYSTEMD_SYSTEM_SERVICES = frozenset({
    "dbus",                # 系统消息总线
    "polkit",              # 授权代理
    "sshd",                # 管理通道（与 ss 探测排除 22 端口一致）
    "ssh",                 # Debian/Ubuntu openssh 管理通道（unit 名为 ssh.service）
    "containerd",          # 已被 docker 探测覆盖
    "console-getty",       # 控制台登录终端（getty 族）
    "console-setup",       # 启动期控制台配置
    "keyboard-setup",      # 启动期键盘配置
    "kmod-static-nodes",   # 启动期设备节点
    "setvtrgb",            # 虚拟终端配色
    "snapd",               # snap 包管理器 daemon
    "snapd.seeded",        # snap 首次 seed
    "rsyslog",             # 系统日志
    "unattended-upgrades", # Ubuntu 自动安全更新
    "wsl-pro",             # WSL 系统代理
})


def _is_systemd_system_service(unit: str) -> bool:
    """原始 unit 名是否命中系统内部服务黑名单（前缀/精确名）。"""
    if unit.startswith(_SYSTEMD_SYSTEM_SERVICE_PREFIXES):
        return True
    return unit[: -len(".service")] in _SYSTEMD_SYSTEM_SERVICES


def _iter_systemctl_unit_rows(output: str):
    """逐行产出 systemctl 输出里的 active/running 服务行 ``(unit, active, sub)``。"""
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("UNIT"):
            continue
        parts = line.split()
        if len(parts) < 4 or not parts[0].endswith(".service"):
            continue
        unit = parts[0]
        active = parts[2].lower()
        sub = parts[3].lower()
        if active in ("active", "running") or sub == "running":
            yield unit, active, sub


def _parse_systemctl_units(output: str) -> List[Dict[str, Any]]:
    """systemctl list-units --type=service → 过滤系统内部服务后的运行中服务列表。

    过滤在解析层做（系统服务不进 services/details），统计由
    :func:`_count_systemd_filtered_units` 供 probes 文案使用。
    """
    services: List[Dict[str, Any]] = []
    for unit, active, sub in _iter_systemctl_unit_rows(output):
        if _is_systemd_system_service(unit):
            continue
        name = unit[: -len(".service")]
        services.append({
            "name": _sanitize_name(name),
            "unit": unit,
            "state": sub or active,
        })
    return services


def _count_systemd_filtered_units(output: str) -> int:
    """统计被系统服务黑名单过滤的 active 服务数（probes 文案用）。"""
    return sum(
        1 for unit, _active, _sub in _iter_systemctl_unit_rows(output)
        if _is_systemd_system_service(unit)
    )


def _parse_nvidia_smi(output: str) -> List[str]:
    """nvidia-smi csv → ['NVIDIA A100-SXM4-40GB, 40960 MiB', ...]。"""
    gpus = [l.strip() for l in output.splitlines() if l.strip()]
    return [g for g in gpus if not g.lower().startswith("name")]


def _parse_kubectl(output: str) -> List[Dict[str, Any]]:
    """kubectl get deploy,svc -A -o json → 服务/端口/镜像映射。"""
    try:
        data = json.loads(output or "{}")
    except json.JSONDecodeError:
        return []
    rows: List[Dict[str, Any]] = []
    for item in data.get("items") or []:
        kind = str(item.get("kind") or "").lower()
        meta = item.get("metadata") or {}
        name = str(meta.get("name") or "")
        namespace = str(meta.get("namespace") or "")
        if not name:
            continue
        if kind == "service":
            ports = []
            for p in (item.get("spec") or {}).get("ports") or []:
                if isinstance(p, dict):
                    port = p.get("nodePort") or p.get("port")
                    if port:
                        ports.append(int(port))
            rows.append({"kind": "k8s-service", "name": name, "namespace": namespace,
                         "ports": sorted(set(ports))})
        elif kind == "deployment":
            containers = (((item.get("spec") or {}).get("template") or {})
                          .get("spec") or {}).get("containers") or []
            image = containers[0].get("image", "") if containers else ""
            rows.append({"kind": "k8s-deploy", "name": name, "namespace": namespace,
                         "image": image})
    return rows


# ---------------------------------------------------------------------------
# 发现编排
# ---------------------------------------------------------------------------

def _probe(runner: Callable[[str], ProbeResult], cmd: str) -> ProbeResult:
    return runner(cmd)


def _service_from_container(c: Dict[str, Any], host: str, env: str,
                            details: Dict[str, Any], cluster: str = "") -> Dict[str, Any]:
    """docker 容器 → v0.3 服务行（第二层）+ 第三层详情草案（L3 命名）。"""
    ports = _parse_published_ports(c.get("ports") or "")
    service_name = c.get("compose_service") or c.get("name")
    project = c.get("compose_project") or ""
    name = _sanitize_name(service_name)
    if project and project != name and name in (details or {}):
        # 同名跨项目：前缀项目名保持唯一。
        name = _sanitize_name(f"{project}-{name}")
    endpoint = f"{host}:{ports[0]}" if ports else None
    detail_path = _entity_filename(cluster, host, name, env)
    svc = {
        "name": name,
        "type": "service",
        "env": env,
        "cluster": cluster or "default",
        "endpoint": endpoint,
        "source": "discovered",
        "last_verified": _dt.date.today().isoformat(),
        "needs_review": True,
        "detail": detail_path,
        "attrs": {
            "image": c.get("image") or "",
            "ports": ports,
            "container": c.get("name") or "",
            "state": c.get("state") or "",
        },
    }
    if project:
        svc["attrs"]["compose_project"] = project
        svc["attrs"]["compose_service"] = c.get("compose_service") or ""
    details[name] = {
        "name": name,
        "type": "service",
        "env": env,
        "cluster": cluster or "default",
        "detail": detail_path,
        "attrs": {k: v for k, v in svc["attrs"].items() if k not in ("ports",)},
        "source": "discovered",
        "last_verified": svc["last_verified"],
        "needs_review": True,
    }
    return svc


def discover_host(host: str, env: str, creds: Optional[Dict[str, Any]] = None,
                  *, cluster: str = "", runner: Optional[Callable[[str], ProbeResult]] = None,
                  skip_unidentified: bool = False) -> Dict[str, Any]:
    """发现一台主机的 v0.3 schema 片段。

    Args:
      host: 目标 IP/主机名（endpoint）。
      env: 目标环境（local/test/dev/prod 四值；老自定义名由调用方/读取端按档位映射）。
      creds: ``{"user", "key_path", "askpass_file"}`` 等认证信息（不含密码明文）。
      cluster: 集群名（可选）；缺省显示 "default"，L3 实体文件名按 env 兜底。
      runner: 可注入的执行器（测试用）；默认远程 ``_build_ssh_runner``，本机
        （localhost/127.0.0.1/::1/空）``_build_local_runner``。
      skip_unidentified: 为 True 时跳过 ss 端口扫描补出的 unidentified 服务。

    Returns:
      v0.3 片段 dict：``{version, source, last_verified, needs_review, host,
      services, details, pending_review, probes}``。host 行带 cluster +
      credential 引用（key_path → ``{type: ssh_key, ref, user, port}``，不落
      密码明文）。pending_review 为无监听端口的 systemd 服务（needs_review=true，
      不入 services/details，待用户确认后才入表）。
    """
    host = str(host or "").strip()
    is_local = host.lower() in _LOCAL_HOST_ALIASES
    if is_local:
        # 本机发现（批次十三 C4）：空 host/localhost/127.0.0.1/::1 → 本地执行，
        # 显示名/endpoint 归一为 localhost；user/key 凭据本地不需要，忽略。
        host = "localhost"
    if not env:
        raise DiscoveryError("discover_host 需要 env（--env <env>）")
    creds = creds or {}
    cluster = str(cluster or "").strip()
    cluster_display = cluster or "default"
    user = str(creds.get("user") or "root")
    if runner is None:
        if is_local:
            runner = _build_local_runner(
                sudo_password_file=Path(creds["sudo_password_file"]) if creds.get("sudo_password_file") else None,
            )
        else:
            runner = _build_ssh_runner(
                host, user=user,
                key_path=creds.get("key_path"),
                askpass_file=Path(creds["askpass_file"]) if creds.get("askpass_file") else None,
                key_passphrase_file=Path(creds["key_passphrase_file"]) if creds.get("key_passphrase_file") else None,
                sudo_password_file=Path(creds["sudo_password_file"]) if creds.get("sudo_password_file") else None,
            )

    probes: Dict[str, str] = {}
    services: List[Dict[str, Any]] = []
    details: Dict[str, Any] = {}
    seen_ports: set = set()
    runtime = "unknown"
    gpus: List[str] = []
    compose_projects: List[str] = []

    # 1) docker 枚举（docker ps + compose ls）。
    docker_res = _probe(runner, "docker ps --format '{{json .}}'")
    if docker_res.ok:
        runtime = "docker"
        probes["docker"] = "ok"
        for c in _parse_docker_ps(docker_res.stdout):
            svc = _service_from_container(c, host, env, details, cluster=cluster)
            if svc["name"] not in [s["name"] for s in services]:
                services.append(svc)
            for p in svc["attrs"].get("ports") or []:
                seen_ports.add(p)
        compose_res = _probe(runner, "docker compose ls --format json")
        if compose_res.ok:
            compose_projects = _parse_compose_ls(compose_res.stdout)
            probes["compose"] = f"ok({len(compose_projects)} projects)"
        else:
            probes["compose"] = compose_res.stdout.strip()[:80] or "exit!=0"
    else:
        if _is_likely_permission_denied(docker_res):
            probes["docker"] = "权限不足（可加 --sudo-password 重试）"
        else:
            reason = (docker_res.stdout or "").strip().splitlines()
            probes["docker"] = reason[-1][:120] if reason else "docker 不可用（exit!=0）"

    # 2) k8s 枚举（可选：kubectl 可用才扫）。
    k8s_res = _probe(runner, "kubectl get deploy,svc -A -o json 2>/dev/null")
    if k8s_res.ok:
        runtime = "k3s" if runtime == "unknown" else runtime
        probes["kubectl"] = "ok"
        for row in _parse_kubectl(k8s_res.stdout):
            if row["kind"] == "k8s-service":
                for p in row.get("ports") or []:
                    seen_ports.add(p)
            name = row["name"]
            if name not in [s["name"] for s in services]:
                node_port = row.get("ports")[0] if row.get("ports") else None
                detail_path = _entity_filename(cluster, host, name, env)
                svc = {
                    "name": name,
                    "type": row["kind"],
                    "env": env,
                    "cluster": cluster_display,
                    "endpoint": f"{host}:{node_port}" if node_port else None,
                    "source": "discovered",
                    "last_verified": _dt.date.today().isoformat(),
                    "needs_review": True,
                    "detail": detail_path,
                    "attrs": {"namespace": row.get("namespace", ""),
                              "image": row.get("image", ""),
                              "ports": row.get("ports") or []},
                }
                services.append(svc)
                details[name] = {
                    "name": name, "type": row["kind"], "env": env,
                    "cluster": cluster_display,
                    "detail": detail_path,
                    "attrs": {"namespace": row.get("namespace", ""),
                              "image": row.get("image", "")},
                    "source": "discovered",
                    "last_verified": svc["last_verified"],
                    "needs_review": True,
                }
    else:
        if _is_likely_permission_denied(k8s_res):
            probes["kubectl"] = "权限不足（可加 --sudo-password 重试）"
        else:
            probes["kubectl"] = "skipped（kubectl 不可用）"

    # 2.5) ss 监听端口探测（先于 systemd：供无端口系统服务过滤 + 未识别端口补条目）。
    ss_res = _probe(runner, "ss -tlnp 2>/dev/null")
    ss_proc_ports = _parse_ss_proc_ports(ss_res.stdout) if ss_res.ok else {}
    unmatched_listeners: List[Dict[str, Any]] = []
    if ss_res.ok:
        probes["ss"] = "ok"
        for listener in _parse_ss_tlnp(ss_res.stdout):
            port = listener["port"]
            if port in seen_ports or port == 22:
                # 22 = sshd 管理通道（主机自身，不是服务）；其余已映射端口跳过。
                continue
            unmatched_listeners.append(listener)
    else:
        probes["ss"] = "skipped（ss 不可用）"

    # 3) systemd 原生服务（排在 docker/k8s 后）。无监听端口的 systemd 服务不入
    #    services/details（批次十八 B 配套：替代 LLM 手动过滤），保留在
    #    pending_review 标 needs_review=true——神人写的 systemd 脚本服务不监听端口
    #    也真实业务，保留可见性不误杀；有监听端口（prometheus/node-exporter 等）
    #    正常入表。
    systemd_res = _probe(
        runner, "systemctl list-units --type=service --no-pager --no-legend 2>/dev/null"
    )
    pending_review: List[Dict[str, Any]] = []
    systemd_covered_ports: set = set()
    if systemd_res.ok:
        systemd_units = _parse_systemctl_units(systemd_res.stdout)
        systemd_filtered = _count_systemd_filtered_units(systemd_res.stdout)
        for unit in systemd_units:
            name = unit["name"]
            if name in [s["name"] for s in services]:
                # docker/k8s 已发现的服务优先，systemd 同名只补充不覆盖。
                continue
            detail_path = _entity_filename(cluster, host, name, env)
            svc = {
                "name": name,
                "type": "systemd-service",
                "env": env,
                "cluster": cluster_display,
                "endpoint": None,
                "source": "discovered",
                "last_verified": _dt.date.today().isoformat(),
                "needs_review": True,
                "detail": detail_path,
                "attrs": {
                    "unit": unit["unit"],
                    "state": unit["state"],
                    "source_probe": "systemctl",
                },
            }
            ports = ss_proc_ports.get(_ss_proc_key(name)) or []
            if not ports:
                # 无监听端口 → 不入正式拓扑，保留 pending_review 待用户确认。
                pending_review.append(svc)
                continue
            services.append(svc)
            details[name] = {
                "name": name,
                "type": "systemd-service",
                "env": env,
                "cluster": cluster_display,
                "detail": detail_path,
                "attrs": {
                    "unit": unit["unit"],
                    "state": unit["state"],
                    "source_probe": "systemctl",
                },
                "source": "discovered",
                "last_verified": svc["last_verified"],
                "needs_review": True,
            }
            systemd_covered_ports.update(ports)
        probes["systemctl"] = (
            f"ok({len(systemd_units)} 服务，过滤 {systemd_filtered} 系统服务，"
            f"另 {len(pending_review)} 个无端口系统服务未入表（可确认）)"
        )
    else:
        probes["systemctl"] = "skipped（systemctl 不可用）"

    # 4) 端口扫描补条目（非 loopback 监听端口，未映射到已知服务/无端口过滤）。
    for listener in unmatched_listeners:
        port = listener["port"]
        if port in systemd_covered_ports:
            # systemd 服务已覆盖该监听端口（正常入表），不重复补 unidentified。
            continue
        if skip_unidentified:
            continue
        name = f"unidentified-{port}"
        detail_path = _entity_filename(cluster, host, name, env)
        services.append({
            "name": name,
            "type": "service",
            "env": env,
            "cluster": cluster_display,
            "endpoint": f"{host}:{port}",
            "source": "discovered",
            "last_verified": _dt.date.today().isoformat(),
            "needs_review": True,
            "detail": detail_path,
            "attrs": {
                "listener": listener["host"],
                "ports": [port],
                "source_probe": "ss",
            },
        })
        details[name] = {
            "name": name, "type": "service", "env": env,
            "cluster": cluster_display,
            "detail": detail_path,
            "attrs": {"listener": listener["host"], "source_probe": "ss"},
            "source": "discovered",
            "last_verified": _dt.date.today().isoformat(),
            "needs_review": True,
        }

    # 5) GPU（可选）。
    gpu_res = _probe(runner, "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null")
    if gpu_res.ok:
        probes["gpu"] = "ok"
        gpus = _parse_nvidia_smi(gpu_res.stdout)
    else:
        probes["gpu"] = "skipped（nvidia-smi 不可用）"

    host_row = {
        "name": _sanitize_name(host),
        "env": env,
        "cluster": cluster_display,
        "endpoint": host,
        "runtime": runtime,
        "source": "discovered",
        "last_verified": _dt.date.today().isoformat(),
        "needs_review": True,
        "services_index": f"hosts/{_sanitize_name(host)}.yaml",
    }
    if not is_local and creds.get("key_path"):
        # 凭据引用（OPS-DELTA #42）：只落 type/ref/user/port，密码明文永不进拓扑。
        # 本机发现不走 SSH → 不写 ssh_key 凭据引用。
        host_row["credential"] = {
            "type": "ssh_key",
            "ref": str(creds["key_path"]),
            "user": user,
            "port": 22,
        }
    if gpus:
        host_row["attrs"] = {"gpu": gpus}
    elif compose_projects:
        host_row["attrs"] = {}
    if compose_projects:
        host_row["attrs"].setdefault("compose_projects", compose_projects)
    return {
        "version": 3,
        "source": "discovered",
        "last_verified": _dt.date.today().isoformat(),
        "needs_review": True,
        "host": host_row,
        "services": services,
        "details": details,
        "pending_review": pending_review,
        "probes": probes,
    }


# ---------------------------------------------------------------------------
# 落盘（用户确认后调用）
# ---------------------------------------------------------------------------

def _topo_layered(topo: Dict[str, Any]) -> bool:
    """分层 schema（v0.2/v0.3）判定：version 2/3，或 shape 检测（hosts/cross_host/clusters）。

    v0.1（扁平 ``core_entities``）不是分层结构，write_discovery 拒绝覆盖。
    """
    if topo.get("version") in (2, 3):
        return True
    if "version" in topo:
        return False
    return bool(topo.get("hosts") or topo.get("cross_host") or topo.get("clusters"))


def _read_host_index(home: Path, hostname: str) -> Dict[str, Any]:
    """读取 hosts/<hostname>.yaml 服务索引（合并语义用；缺失/解析失败 → {}）。"""
    path = Path(home) / "hosts" / f"{_sanitize_name(hostname)}.yaml"
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_discovery(home: Path, discovery: Dict[str, Any], force: bool = False,
                    merge: bool = True) -> Dict[str, Any]:
    """把发现结果落盘为 v0.3 结构（hosts/<host>.yaml + topology.yaml + entities/）。

    - 现有 topology.yaml 为 v0.1（扁平 core_entities）时拒绝写入（不自动改写用户数据）；
    - host 已存在且未 ``force`` → 合并语义（批次十八 B）：发现的新服务自动追加进
      hosts/<host>.yaml + entities/，已有同名服务/实体保留（含手动 endpoint/owner），
      host 行保留原内容只刷新 last_verified；``merge=False`` 显式关闭合并时维持旧
      的拒绝语义（需 ``--force`` 整体替换）；
    - 写路径：hosts/<hostname>.yaml（第二层服务索引）、
      entities/{cluster}__{host}__{name}.yaml（第三层详情草案，OPS-DELTA #42
      L3 命名）、topology.yaml 的 hosts 段追加；
    - 只产 v0.3：``version: 3``、hosts 行带 cluster、environments 只写四值档位
      （老自定义 env 按档位映射）。

    Returns:
      ``{"written", "topology", "index", "appended", "kept", "merged"}``——
      appended = 本次新增服务数，kept = 合并时保留的现有索引行数（新 host/force
      为 0），merged = 是否走了合并路径。
    """
    home = Path(home)
    host_row = dict(discovery.get("host") or {})
    hostname = _sanitize_name(host_row.get("name") or "")
    if not hostname:
        raise DiscoveryError("发现结果缺少 host.name，无法落盘。")
    host_row["name"] = hostname
    host_row.setdefault("cluster", "default")
    host_row["services_index"] = f"hosts/{hostname}.yaml"
    env = str(host_row.get("env") or "")
    env_tier = _env_tier(env)

    topo_path = home / "topology.yaml"
    topo: Dict[str, Any] = {}
    if topo_path.is_file():
        try:
            topo = yaml.safe_load(topo_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise DiscoveryError(f"topology.yaml 解析失败：{exc}") from exc
        if not isinstance(topo, dict) or not _topo_layered(topo):
            raise DiscoveryError(
                "现有 topology.yaml 是 schema v0.1（扁平 core_entities）。"
                "topo-discover 只写 v0.3；请先迁移（可运行 vigil ops-init --force "
                "重铺样例，或手动添加 version: 2/3 + hosts: 段）后再发现。"
            )
        hosts = [h for h in topo.get("hosts") or [] if isinstance(h, dict)]
        host_exists = any(h.get("name") == hostname for h in hosts)
        if host_exists and not force and not merge:
            raise DiscoveryError(
                f"host {hostname} 已存在于 topology.yaml（--force 覆盖）。"
            )
        if host_exists and not force:
            # 合并：保留手动维护的 host 行，只刷新校验时间（不冲掉手动编辑）。
            old_row = next(h for h in hosts if h.get("name") == hostname)
            merged_row = dict(old_row)
            merged_row["last_verified"] = _dt.date.today().isoformat()
            topo["hosts"] = [h for h in hosts if h.get("name") != hostname] + [merged_row]
        else:
            topo["hosts"] = [h for h in hosts if h.get("name") != hostname] + [host_row]
        envs = [e.get("name") for e in (topo.get("environments") or []) if isinstance(e, dict)]
        if env_tier and env_tier not in envs:
            topo.setdefault("environments", []).append(
                {"name": env_tier,
                 "isolation": "strict" if env_tier == "prod" else "relaxed",
                 "role": env_tier}
            )
        topo["version"] = 3
        topo["updated_at"] = _dt.date.today().isoformat()
    else:
        host_exists = False
        topo = {
            "version": 3,
            "updated_at": _dt.date.today().isoformat(),
            "sources": ["discovered"],
            "environments": [
                {"name": env_tier,
                 "isolation": "strict" if env_tier == "prod" else "relaxed",
                 "role": env_tier}
            ] if env_tier else [],
            "hosts": [host_row],
            "cross_host": [],
            "clusters": [],
            "key_paths": [],
        }

    # 第二层：hosts/<hostname>.yaml 服务索引（合并：同名跳过、新服务追加）。
    existing_index: Dict[str, Any] = {}
    if host_exists and not force:
        existing_index = _read_host_index(home, hostname)
    existing_rows = existing_index.get("services") or []
    if not isinstance(existing_rows, list):
        existing_rows = []
    existing_names = {
        str(s.get("name"))
        for s in existing_rows
        if isinstance(s, dict) and s.get("name")
    }

    merged_rows: List[Dict[str, Any]] = (
        list(existing_rows) if (host_exists and not force) else []
    )
    appended = 0
    for svc in discovery.get("services") or []:
        if not isinstance(svc, dict):
            continue
        row = dict(svc)
        row.pop("_host", None)
        if str(row.get("name")) in existing_names:
            # 同名跳过：保留现有行（含手动 endpoint/owner/类型），不覆盖。
            continue
        merged_rows.append(row)
        appended += 1
    kept = len(existing_rows) if (host_exists and not force) else 0

    index_data = {
        "host": hostname,
        "env": str(existing_index.get("env") or env) if (host_exists and not force) else env,
        "cluster": (str(existing_index.get("cluster") or host_row["cluster"])
                    if (host_exists and not force) else host_row["cluster"]),
        "services": merged_rows,
    }

    # 第三层：entities/{cluster}__{host}__{name}.yaml 详情草案（合并：已有同名
    # 实体保留不重写）。
    entities_dir = home / "entities"
    entity_paths = []
    for name, detail in (discovery.get("details") or {}).items():
        if not isinstance(detail, dict) or not _NAME_RE.fullmatch(str(name)):
            continue
        if name in existing_names and not force:
            # 合并：索引里已存在的实体（含手动维护）不重写详情文件。
            continue
        rel = str(detail.get("detail") or f"entities/{name}.yaml")
        fname = rel[len("entities/"):] if rel.startswith("entities/") else rel
        if "/" in fname or not _NAME_RE.fullmatch(fname):
            # 只接受 entities/ 下单层净化文件名（防路径穿越）。
            continue
        entities_dir.mkdir(parents=True, exist_ok=True)
        path = entities_dir / fname
        if path.exists() and not force:
            continue
        path.write_text(
            yaml.safe_dump(detail, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        entity_paths.append(str(path.relative_to(home)))

    hosts_dir = home / "hosts"
    hosts_dir.mkdir(parents=True, exist_ok=True)
    index_path = hosts_dir / f"{hostname}.yaml"
    index_path.write_text(
        yaml.safe_dump(index_data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    topo_path.parent.mkdir(parents=True, exist_ok=True)
    topo_path.write_text(
        yaml.safe_dump(topo, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    written = [str(index_path.relative_to(home)), str(topo_path.relative_to(home))]
    written.extend(entity_paths)
    return {
        "written": written,
        "topology": topo_path,
        "index": index_path,
        "appended": appended,
        "kept": kept,
        "merged": bool(host_exists and not force),
    }

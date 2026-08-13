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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_SSH_TIMEOUT_S = 60
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
            # ssh 自身失败（连接拒绝/认证失败）→ 中止发现，无半截数据。
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise DiscoveryError(
                f"SSH 连接 {user}@{host} 失败（exit 255）："
                f"{detail[-1] if detail else '认证失败或主机不可达'}"
            )
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
      runner: 可注入的 SSH 执行器（测试用）；默认 ``_build_ssh_runner``。
      skip_unidentified: 为 True 时跳过 ss 端口扫描补出的 unidentified 服务。

    Returns:
      v0.3 片段 dict：``{version, source, last_verified, needs_review, host,
      services, details, probes}``。host 行带 cluster + credential 引用
      （key_path → ``{type: ssh_key, ref, user, port}``，不落密码明文）。
    """
    host = str(host or "").strip()
    if not host:
        raise DiscoveryError("discover_host 需要 host（--host <ip>）")
    if not env:
        raise DiscoveryError("discover_host 需要 env（--env <env>）")
    creds = creds or {}
    cluster = str(cluster or "").strip()
    cluster_display = cluster or "default"
    user = str(creds.get("user") or "root")
    if runner is None:
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

    # 2.5) systemd 原生服务（排在 docker/k8s 后、ss 前）。
    systemd_res = _probe(
        runner, "systemctl list-units --type=service --no-pager --no-legend 2>/dev/null"
    )
    if systemd_res.ok:
        systemd_units = _parse_systemctl_units(systemd_res.stdout)
        systemd_filtered = _count_systemd_filtered_units(systemd_res.stdout)
        probes["systemctl"] = f"ok({len(systemd_units)} 服务，过滤 {systemd_filtered} 系统服务)"
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
    else:
        probes["systemctl"] = "skipped（systemctl 不可用）"

    # 3) 端口扫描补条目（非 loopback 监听端口，未映射到已知服务）。
    ss_res = _probe(runner, "ss -tlnp 2>/dev/null")
    if ss_res.ok:
        probes["ss"] = "ok"
        for listener in _parse_ss_tlnp(ss_res.stdout):
            port = listener["port"]
            if port in seen_ports or port == 22:
                # 22 = sshd 管理通道（主机自身，不是服务）；其余已映射端口跳过。
                continue
            name = f"unidentified-{port}"
            if skip_unidentified:
                seen_ports.add(port)
                continue
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
            seen_ports.add(port)
    else:
        probes["ss"] = "skipped（ss 不可用）"

    # 4) GPU（可选）。
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
    if creds.get("key_path"):
        # 凭据引用（OPS-DELTA #42）：只落 type/ref/user/port，密码明文永不进拓扑。
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


def write_discovery(home: Path, discovery: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
    """把发现结果落盘为 v0.3 结构（hosts/<host>.yaml + topology.yaml + entities/）。

    - 现有 topology.yaml 为 v0.1（扁平 core_entities）时拒绝写入（不自动改写用户数据）；
    - host 已存在且未 ``force`` → 拒绝覆盖；
    - 写路径：hosts/<hostname>.yaml（第二层服务索引）、
      entities/{cluster}__{host}__{name}.yaml（第三层详情草案，OPS-DELTA #42
      L3 命名）、topology.yaml 的 hosts 段追加；
    - 只产 v0.3：``version: 3``、hosts 行带 cluster、environments 只写四值档位
      （老自定义 env 按档位映射）。
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
        if any(h.get("name") == hostname for h in hosts) and not force:
            raise DiscoveryError(
                f"host {hostname} 已存在于 topology.yaml（--force 覆盖）。"
            )
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

    # 第二层：hosts/<hostname>.yaml 服务索引。
    index_rows = []
    for svc in discovery.get("services") or []:
        if not isinstance(svc, dict):
            continue
        row = dict(svc)
        row.pop("_host", None)
        index_rows.append(row)
    index_data = {
        "host": hostname,
        "env": env,
        "cluster": host_row["cluster"],
        "services": index_rows,
    }

    # 第三层：entities/{cluster}__{host}__{name}.yaml 详情草案（detail 字段是显式路径）。
    entities_dir = home / "entities"
    entity_paths = []
    for name, detail in (discovery.get("details") or {}).items():
        if not isinstance(detail, dict) or not _NAME_RE.fullmatch(str(name)):
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
    return {"written": written, "topology": topo_path, "index": index_path}

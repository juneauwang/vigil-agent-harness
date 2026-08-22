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
import socket as _socket
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tools.topo_schemas import validate_enum, validate_enum_list

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
    """名称净化：只保留白名单字符，防止路径穿越/脏名写入（小写归一）。"""
    value = str(value or "").strip().lower()
    value = re.sub(r"[^A-Za-z0-9_.-]", "-", value)
    return value or "unknown"


def _safe_filename(value: str) -> str:
    """文件名净化（保大小写）：主机行名/目录文件名用——v0.4 主机名可能是
    大写 hostname（如 LAPTOP-T2JA2ERE），不能小写归一（会写偏路径）。"""
    value = str(value or "").strip()
    value = re.sub(r"[^A-Za-z0-9_.-]", "-", value)
    return value or "unknown"


def _strip_entity_ext(value: str) -> str:
    """剥掉 name 里尾部多余的 ``.yaml``/``.yml`` 后缀（批三十九：杜绝 ``*.yaml.yaml`` 双后缀）。

    实体名可能带扩展名形态（如 LLM 传 ``dsl-review.yaml``），文件名只保留一层
    ``.yaml``。只剥 name 段；cluster/host 段值域不携带扩展名形态，不动。
    """
    while value.lower().endswith((".yaml", ".yml")):
        value = value[:-5] if value.lower().endswith(".yaml") else value[:-4]
    return value


def _dedupe_repeated_name(value: str) -> str:
    """名字重复校验（批三十九）：name 段已是 ``{base}-{base}`` 重复形态时去重。

    来源：compose 项目前缀与基础名同名时拼接 ``{project}-{name}`` 产生
    ``dsl-review-dsl-review`` 这类怪异实体名（§AV 实测）。文件名层再兜底剥一层
    重复段，避免把重复形态写进磁盘路径；实体名本身（L2 行 ``name`` / L3 文件
    ``name:`` 字段）保持不变。
    """
    m = re.fullmatch(r"(.+?)-\1", value)
    return m.group(1) if m else value


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
    无歧义）；name 段先剥 ``.yaml``/``.yml`` 再拼文件名（批三十九：杜绝
    ``*.yaml.yaml`` 双后缀），并对 ``{base}-{base}`` 重复形态去重；cluster 空用
    env 兜底，再空用 "default"。同 host 不同应用 / 同应用不同 host / 同应用同
    host 不同 cluster 的文件名互不冲突。
    """
    cluster = cluster or fallback_env or "default"
    name = _dedupe_repeated_name(_strip_entity_ext(_sanitize_name(name)))
    parts = [_sanitize_name(cluster), _sanitize_name(host), name]
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


def force_trip(host: str, user: str) -> None:
    """用户纠正信号 → 该 host:user 立即进入熔断态（等价已连续失败 3 次）。

    批次二十一 §AF 补丁 2 需求 2：用户纠正操作方向（新对话轮次明确否定当前
    尝试路径）＝ 停止信号。会话层检测到纠正信号后调用本函数，后续该 host:user
    的 runner/guard 入口直接熔断，不再自动重试（与自然触发 3 次失败同语义）。
    """
    with _SSH_AUTH_LOCK:
        entry = _SSH_AUTH_FAILURES.setdefault(_ssh_auth_key(host, user), {})
        entry["count"] = _SSH_AUTH_BREAKER_LIMIT
        entry["ts"] = _dt.datetime.now().isoformat()
        entry["forced"] = True


def _is_ssh_auth_failure(proc: Any) -> bool:
    """认证失败判定（批次十九扩展，不只 exit 255）。

    - ``too many authentication failures``：sshd 限流信号，stderr 出现即算——
      个别 ssh 包装/ansible/scp 返回码不一定是 255，但该提示出现意味着
      MaxAuthTries 已被刷爆，再试只会继续自伤；
    - ``permission denied`` 出现两次：ssh 多 key 逐个尝试的典型输出（agent
      自由拼的 ssh/scp 路径），单次尝试内两次拒绝即认证失败信号；
    - 其余保持批次十六语义：exit 255 + stderr 含认证提示才计数（连接拒绝等
      非认证 255 不计数）。
    """
    stderr = (getattr(proc, "stderr", None) or "").lower()
    if "too many authentication failures" in stderr:
        return True
    if stderr.count("permission denied") >= 2:
        return True
    if getattr(proc, "returncode", None) != 255:
        return False
    return any(hint in stderr for hint in _SSH_AUTH_FAILURE_HINTS)


def _ssh_auth_breaker_error(host: str, user: str) -> str:
    n = _ssh_auth_failures(host, user)
    return (
        f"⚠️ SSH 认证失败 {n}/{_SSH_AUTH_BREAKER_LIMIT}——为避免 sshd 限流"
        f"（MaxAuthTries=6）把自己锁出主机（{user}@{host}），已停止自动重试。"
        "分层定位：连接层错误（Too many authentication failures / Permission "
        "denied）→ 停止重试 + 检查 -o IdentitiesOnly=yes（ssh-agent 多 key 会"
        "遍历所有 key 刷爆 MaxAuthTries）+ 检查重试次数；执行层错误（转义/远端"
        "权限）→ 才换传递方式，不要用换姿势掩盖连接层真凶。请：1) 手动 ssh "
        "验证凭据 2) 或补充拓扑表 credential 声明（vssh 或 topo credential）"
        "。请勿换用户名/换 key/翻 ~/.ssh/ 继续尝试（§Q/§AD/§AF 教训——这些行为"
        "会触发 sshd 限流锁 15 分钟）。3) 或询问用户提供正确凭据"
    )


def _build_ssh_runner(host: str, user: str = "root", key_path: Optional[str] = None,
                      askpass_file: Optional[Path] = None,
                      key_passphrase_file: Optional[Path] = None,
                      sudo_password_file: Optional[Path] = None) -> Callable[[str], ProbeResult]:
    """默认 SSH runner：key/agent 认证（BatchMode）或 SSH_ASKPASS 密码文件。

    密码走 SSH_ASKPASS（``askpass_file`` 为 0700 脚本，从保险箱文件读取），
    任何密码明文都不进 argv / 环境变量。

    fail-closed（批次二十一 §Q 收口）：SSH 认证凭据（key_path / askpass_file /
    key_passphrase_file 任一）未提供时直接报错——不再回退到"尝试默认 key / 
    ssh-agent"自探测（§Q 实锤：agent 凭据缺失时翻 ~/.ssh/ 试密钥/猜 vault 字段，
    触发 sshd MaxAuthTries 限流）。CLI 交互路径（topo-discover 交互收集凭据 /
    vssh）在调用方提供凭据，不受影响；sudo_password_file 是 sudo 密码不是 SSH
    认证凭据，不构成放行条件。
    """
    if not host or not user:
        raise DiscoveryError("SSH 需要 host 与 user（--host / --user）")
    if not (key_path or askpass_file is not None or key_passphrase_file is not None):
        raise DiscoveryError(
            f"目标主机 {user}@{host} 未配置 SSH 凭据（拓扑表 credential 缺失或无 "
            "key/password）——禁止自行翻 ~/.ssh/ 找 key / 试多个用户名 / 猜 vault "
            "字段（§Q/§AD 教训，会触发 sshd MaxAuthTries 限流把主机锁 15 分钟）。"
            "请停止自动尝试：1) 手动 ssh 验证凭据 2) 或通过 topo_update 补充拓扑表 "
            "credential 声明 3) 或询问用户提供正确凭据"
        )

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
        argv = ["ssh", "-o", "ConnectTimeout=10", "-o", "IdentitiesOnly=yes"]
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
        # 认证失败判定（批次十九：不只 exit 255——Too many / Permission denied ×2
        # 即限流或多 key 遍历信号）→ 计数，达上限熔断；任一认证失败都中止发现。
        if _is_ssh_auth_failure(proc):
            _record_ssh_auth_failure(host, user)
            if _ssh_auth_breaker_tripped(host, user):
                raise DiscoveryError(_ssh_auth_breaker_error(host, user))
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise DiscoveryError(
                f"SSH 连接 {user}@{host} 失败（exit {proc.returncode}）："
                f"{detail[-1] if detail else '认证失败或主机不可达'}"
            )
        if proc.returncode == 255:
            # exit 255 但非认证失败（连接拒绝/握手失败）→ 中止发现，不计数。
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
    fd, path = tempfile.mkstemp(prefix="vigil-askpass-", suffix=".sh")
    os.close(fd)  # 不关 fd 则脚本保持写打开，ssh/exec 报 Text file busy
    script = Path(path)
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
            # docker --format '{{json .}}' 的 Names 是 JSON 数组（["/app"]）——
            # 取第一个元素并去前导 "/"（2026-08-14 验收实锤：str() 会把数组
            # 转成 "['/app']" 导致实体名匹配永远失败，topo_status_sync 全落 ask）。
            "name": _docker_first_name(row.get("Names")),
            "image": str(row.get("Image") or "").strip(),
            "ports": str(row.get("Ports") or "").strip(),
            "state": str(row.get("State") or "").strip(),
            "compose_project": labels_map.get("com.docker.compose.project", ""),
            "compose_service": labels_map.get("com.docker.compose.service", ""),
        })
    return [c for c in containers if c["name"]]


def _docker_first_name(names) -> str:
    """docker JSON 的 Names 字段 → 容器名（去前导 '/'）。

    docker --format '{{json .}}' 输出 Names 是 JSON 数组（["/app"]），
    但也可能因版本/格式差异是字符串（"/app"）或 None——统一归一。
    """
    if isinstance(names, list):
        raw = names[0] if names else ""
    elif isinstance(names, str):
        raw = names.strip().lstrip("[").rstrip("]").strip()
        raw = raw.strip("'\"").strip()
    else:
        raw = ""
    return str(raw).strip().lstrip("/")


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
# v0.4：服务类型判定 + 硬件/网络静态规格探针（YAPL P1）
# ---------------------------------------------------------------------------

# type 判定表（yapl-design.md §9.7）关键字启发式：主职存储/事务→db；索引/搜索
# →search；缓存→cache；消息→queue；日志/指标/可观测→monitor；流量入口→gateway；
# 镜像/制品→registry；对象存储→object_storage；其余业务→app。命中不了也诚实
# 给 app（不猜 db/cache）；name 优先级高于 image（harbor 镜像名可能不含 harbor）。
_TYPE_KEYWORDS: List[tuple] = [
    ("db", ("postgres", "postgis", "mysql", "mariadb", "mongodb", "mongo",
            "clickhouse", "influxdb", "cassandra", "elasticsearch", "opensearch",
            "tidb", "cockroachdb")),
    ("search", ("meilisearch", "typesense", "solr", "zincsearch", "vespa",
                "elasticsearch", "opensearch", "qdrant", "milvus", "weaviate",
                "chroma", "pgvector")),
    ("cache", ("redis", "valkey", "memcached", "keydb", "dragonfly")),
    ("queue", ("rabbitmq", "kafka", "nats", "pulsar", "rocketmq", "activemq",
               "redis-stream", "beanstalkd")),
    ("monitor", ("prometheus", "grafana", "alertmanager", "node-exporter",
                 "node_exporter", "loki", "tempo", "jaeger", "victoriametrics",
                 "thanos", "cadvisor", "zabbix", "nagios", "uptime-kuma")),
    ("registry", ("harbor", "nexus", "registry", "distribution", "quay", "gitea")),
    ("gateway", ("nginx", "openresty", "traefik", "haproxy", "istio", "envoy",
                 "caddy", "apisix", "kong", "fabio", "squid", "varnish")),
    ("object_storage", ("minio", "ceph", "rbd", "s3", "seaweedfs", "garage")),
]


def _classify_service_type(name: str, image: str = "") -> str:
    """服务名/镜像 → 受控 type 枚举（9.7 type 判定表关键字启发式，兜底 app）。"""
    blob = f"{name or ''} {image or ''}".lower()
    blob = re.sub(r"[^a-z0-9]+", "-", blob)
    for service_type, keywords in _TYPE_KEYWORDS:
        for kw in keywords:
            if kw in blob:
                return service_type
    return "app"


def _image_version(image: str) -> str:
    """镜像 → 版本标签（harbor:v2.11.0 → v2.11.0；无标签 → 空）。"""
    tag = str(image or "").rsplit(":", 1)[-1]
    if tag == image or re.fullmatch(r"[0-9.]+", tag or "") is None and ":" not in str(image or ""):
        return ""
    if ":" in str(image or "") and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag or ""):
        return tag
    return ""


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _parse_lscpu(output: str) -> Dict[str, Any]:
    model, cores = "", 0
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "model name" and not model:
            model = value
        elif key == "cpu(s)" and not cores:
            try:
                cores = int(value)
            except ValueError:
                pass
    return {"model": model, "cores": cores}


def _parse_meminfo_gb(output: str) -> int:
    for line in output.splitlines():
        if line.startswith("MemTotal:"):
            try:
                kb = int(line.split()[1])
            except (IndexError, ValueError):
                return 0
            return max(0, round(kb / 1024 / 1024))
    return 0


def _parse_df_disks(output: str) -> List[Dict[str, Any]]:
    """df --output=target,size,fstype,source → [{mount, size_gb, type}].

    列序 = ``Mount 1B-blocks FSType Source``（--output 顺序）；过滤伪文件系统；
    type 按设备名启发（nvme→ssd，sd*→hdd，其余→ssd，用户可人工修正）。
    """
    skip_fs = {"tmpfs", "devtmpfs", "overlay", "proc", "sysfs", "cgroup", "cgroup2",
               "devpts", "mqueue", "shm", "hugetlbfs", "none", "squashfs", "iso9660",
               "autofs", "debugfs", "tracefs", "securityfs", "pstore", "bpf", "fusectl"}
    disks: List[Dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] == "Filesystem":
            continue
        target, size_b, fstype, source = parts[0], parts[1], parts[2], parts[3]
        if fstype in skip_fs or target in ("/dev", "/proc", "/sys", "/run"):
            continue
        try:
            size_gb = max(1, round(int(size_b) / 1_000_000_000))
        except ValueError:
            continue
        dev = str(source).lower()
        disk_type = "ssd" if ("nvme" in dev or "loop" in dev) else ("hdd" if dev.startswith("/dev/sd") else "ssd")
        disks.append({"mount": target, "size_gb": size_gb, "type": disk_type})
    return disks


def _parse_ip_addr(output: str) -> List[Dict[str, Any]]:
    """ip -o addr show → [{name, ip, primary, role}]（跳过 loopback/link-local）。"""
    interfaces: List[Dict[str, Any]] = []
    seen: set = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] == "1:":
            continue
        iface = parts[1].rstrip(":")
        addr_family = parts[2]
        if addr_family not in ("inet", "inet6"):
            continue
        ip = parts[3].split("/")[0]
        scope = parts[4] if len(parts) > 4 else ""
        if iface in seen or iface == "lo" or scope in ("host",):
            continue
        if ip.startswith("169.254.") or ip.startswith("fe80:"):
            continue
        seen.add(iface)
        interfaces.append({
            "name": iface,
            "ip": ip,
            "primary": len(interfaces) == 0,  # 第一个全局地址视为主接口
            "role": "",
        })
    return interfaces


def _host_roles_for(runtime: str) -> List[str]:
    """runtime → host.role 数组启发（9.7 枚举；control-plane 无法从探测区分，默认 worker）。"""
    runtime = str(runtime or "").strip()
    if runtime in ("docker",):
        return ["docker-host"]
    if runtime in ("k8s", "k3s"):
        return ["worker"]
    return []


def _probe_hardware(runner: Callable[[str], ProbeResult], probes: Dict[str, str],
                    host_endpoint: str = "") -> Dict[str, Any]:
    """硬件/网络静态规格探测（v0.4 硬件层）。全部 best-effort，失败记 probes。

    只存静态规格 + controller 枚举（设计铁律：动态负载/健康/规则内容不落盘）。
    """
    out: Dict[str, Any] = {
        "hardware": {
            "source": "discovered",
            "cpu": {"model": "", "cores": 0},
            "memory_gb": 0,
            "storage": {
                "raid_controller": "",
                "raid_tool": "none",
                "raid_level": "none",
                "disks": [],
            },
            "gpu": {"controller": "none", "devices": []},
        },
        "network": {
            "source": "discovered",
            "interfaces": [],
            "firewall": {"controller": "none"},
        },
    }

    lscpu = _probe(runner, "lscpu 2>/dev/null")
    if lscpu.ok:
        out["hardware"]["cpu"] = _parse_lscpu(lscpu.stdout)
        probes["hardware.cpu"] = "ok"
    else:
        probes["hardware.cpu"] = "skipped（lscpu 不可用）"

    mem = _probe(runner, "cat /proc/meminfo 2>/dev/null")
    if mem.ok:
        out["hardware"]["memory_gb"] = _parse_meminfo_gb(mem.stdout)
        probes["hardware.mem"] = "ok"
    else:
        probes["hardware.mem"] = "skipped"

    disks = _probe(runner, "df -B1 --output=target,size,fstype,source 2>/dev/null")
    if disks.ok:
        out["hardware"]["storage"]["disks"] = _parse_df_disks(disks.stdout)
        probes["hardware.disks"] = f"ok({len(out['hardware']['storage']['disks'])} mounts)"
    else:
        probes["hardware.disks"] = "skipped"

    raid = _probe(runner, "command -v ssacli storcli megacli perccli mdadm 2>/dev/null")
    if raid.ok and raid.stdout.strip():
        tool = raid.stdout.strip().splitlines()[0].strip()
        if tool:
            out["hardware"]["storage"]["raid_tool"] = "mdadm" if "mdadm" in tool else tool.rsplit("/", 1)[-1]
            probes["hardware.raid_tool"] = out["hardware"]["storage"]["raid_tool"]
            scan = _probe(runner, "mdadm --detail --scan 2>/dev/null")
            if scan.ok and scan.stdout.strip():
                m = re.search(r"level=(\w+)", scan.stdout)
                out["hardware"]["storage"]["raid_level"] = m.group(1) if m else "raid1"
            ctrl = _probe(runner, "lspci 2>/dev/null | grep -i raid | head -1")
            if ctrl.ok and ctrl.stdout.strip():
                out["hardware"]["storage"]["raid_controller"] = re.sub(
                    r"^[0-9a-f:.]+\s+", "", ctrl.stdout.strip())
    else:
        probes["hardware.raid_tool"] = "none"

    gpu_bin = _probe(runner, "command -v nvidia-smi npu-smi cambricon-smi rocm-smi 2>/dev/null")
    gpu_controller = ""
    if gpu_bin.ok and gpu_bin.stdout.strip():
        gpu_controller = gpu_bin.stdout.strip().splitlines()[0].strip().rsplit("/", 1)[-1]
    if gpu_controller:
        out["hardware"]["gpu"]["controller"] = gpu_controller
        gpu_res = _probe(runner, f"{gpu_controller} --query-gpu=name --format=csv,noheader 2>/dev/null")
        if gpu_res.ok:
            counts: Dict[str, int] = {}
            for line in gpu_res.stdout.splitlines():
                model = line.strip()
                if model and not model.lower().startswith("name"):
                    counts[model] = counts.get(model, 0) + 1
            out["hardware"]["gpu"]["devices"] = [
                {"model": model, "count": count} for model, count in sorted(counts.items())
            ]
            probes["hardware.gpu"] = f"ok({sum(counts.values())} devices)"
        else:
            probes["hardware.gpu"] = "skipped（query 失败）"
    else:
        probes["hardware.gpu"] = "none"

    ip_out = _probe(runner, "ip -o addr show 2>/dev/null")
    if ip_out.ok:
        out["network"]["interfaces"] = _parse_ip_addr(ip_out.stdout)
        probes["network.ip"] = f"ok({len(out['network']['interfaces'])} ifaces)"
    else:
        probes["network.ip"] = "skipped（ip 不可用）"

    fw = _probe(runner, "systemctl is-active firewalld 2>/dev/null")
    if fw.ok and "active" in fw.stdout.split():
        out["network"]["firewall"]["controller"] = "firewalld"
    else:
        ufw = _probe(runner, "ufw status 2>/dev/null")
        if ufw.ok and "Status: active" in ufw.stdout:
            out["network"]["firewall"]["controller"] = "ufw"
        else:
            nft = _probe(runner, "nft list ruleset 2>/dev/null | head -5")
            if nft.ok and nft.stdout.strip():
                out["network"]["firewall"]["controller"] = "nftables"
            else:
                ipt = _probe(runner, "iptables -L -n 2>/dev/null | head -5")
                if ipt.ok and ipt.stdout.strip():
                    out["network"]["firewall"]["controller"] = "iptables"
    probes["network.firewall"] = out["network"]["firewall"]["controller"]

    if host_endpoint:
        for iface in out["network"]["interfaces"]:
            if iface["ip"] == host_endpoint:
                iface["primary"] = True
    return out


def _probe_os(runner: Callable[[str], ProbeResult]) -> str:
    """/etc/os-release → PRETTY_NAME（主机 os 事实；失败 → 空串）。"""
    res = _probe(runner, "cat /etc/os-release 2>/dev/null")
    if not res.ok:
        return ""
    for line in res.stdout.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def _probe(runner: Callable[[str], ProbeResult], cmd: str) -> ProbeResult:
    return runner(cmd)


def _service_from_container(c: Dict[str, Any], host: str, env: str,
                            details: Dict[str, Any], cluster: str = "") -> Dict[str, Any]:
    """docker 容器 → v0.4 服务行（第二层）+ 第三层档案草案（snapshot/checks）。

    v0.4 服务行无 env/cluster/owner（继承主机）；type 按 9.7 判定表归类；
    managed_by = docker/docker_compose（工具名）；attrs 移入 L3 snapshot。
    """
    ports = _parse_published_ports(c.get("ports") or "")
    service_name = c.get("compose_service") or c.get("name")
    project = c.get("compose_project") or ""
    name = _sanitize_name(service_name)
    if project and project != name and not name.startswith(f"{project}-") and name in (details or {}):
        # 同名跨项目：前缀项目名保持唯一。name 已带 ``{project}-`` 前缀（或
        # project 与 name 同名）时不再叠加，防止产生 ``dsl-review-dsl-review``
        # 这类重复形态的怪异实体名（批三十九 §AV）。
        name = _sanitize_name(f"{project}-{name}")
    endpoint = f"{host}:{ports[0]}" if ports else None
    detail_path = _entity_filename(cluster, host, name, env)
    image = c.get("image") or ""
    service_type = _classify_service_type(name, image)
    by_type: Dict[str, Any] = {}
    if service_type == "db":
        by_type = {"backup_dir": "", "role": "standalone"}
    elif service_type == "registry":
        by_type = {"replication_targets": [], "storage_backend": ""}
    elif service_type == "cache":
        by_type = {"persistence": "none"}
    elif service_type == "monitor":
        by_type = {"collection_mode": ""}
    if project:
        runtime_block: Dict[str, Any] = {
            "project": project,
            "workdir": "",
            "services": [{"name": c.get("name") or name, "state": c.get("state") or ""}],
        }
    else:
        runtime_block = {
            "containers": [{"name": c.get("name") or name, "state": c.get("state") or ""}],
            "port_mapping": {str(p): "" for p in ports},
            "image": image,
        }
    svc = {
        "name": name,
        "type": service_type,
        "managed_by": "docker_compose" if project else "docker",
        "endpoint": endpoint,
        "extra_ports": ports[1:] if len(ports) > 1 else [],
        "log_paths": [],
        "depends_on": [],
        "source": "discovered",
        "last_verified": _dt.date.today().isoformat(),
        "needs_review": True,
        "detail": detail_path,
    }
    details[name] = {
        "name": name,
        "detail": detail_path,
        "version": _image_version(image),
        "updated_at": _dt.date.today().isoformat(),
        "checks": [],
        "snapshot": {
            "captured_at": _now_iso(),
            "source": "discovered",
            "common": {
                "version": _image_version(image),
                "config_dir": "",
                "log_dir": "",
                "data_dir": "",
                "mode": "single",
            },
            "by_type": by_type,
            "by_runtime": {svc["managed_by"]: runtime_block},
        },
        "notes": "",
    }
    return svc


def discover_host(host: str, env: str, creds: Optional[Dict[str, Any]] = None,
                  *, cluster: str = "", runner: Optional[Callable[[str], ProbeResult]] = None,
                  skip_unidentified: bool = False) -> Dict[str, Any]:
    """发现一台主机的 v0.4 schema 片段。

    Args:
      host: 目标 IP/主机名（endpoint）；本机用 localhost/127.0.0.1/空。
      env: 目标环境（local/test/dev/prod 四值；老自定义名由调用方/读取端按档位映射）。
      creds: ``{"user", "key_path", "askpass_file"}`` 等认证信息（不含密码明文）。
      cluster: 集群名（可选）；缺省显示 "default"，L3 实体文件名按 env 兜底。
      runner: 可注入的执行器（测试用）；默认远程 ``_build_ssh_runner``，本机
        （localhost/127.0.0.1/::1/空）``_build_local_runner``。
      skip_unidentified: 为 True 时跳过 ss 端口扫描补出的 unidentified 服务。

    Returns:
      v0.4 片段 dict：``{version, source, last_verified, needs_review, host,
      services, details, hardware, pending_review, probes}``。host 行 v0.4
      （role/runtime 数组 + credentials 数组，无 services_index）；services 为
      v0.4 服务行（type/managed_by/extra_ports/depends_on）；details 为 v0.4
      档案（snapshot/checks）；hardware 为硬件层（hardware + network 段）。
      本机 host 行名 = 本机 hostname（与第一层手动登记行合并命中）。
      pending_review 为无监听端口的 systemd 服务 + unidentified 端口
      （needs_review=true，不入 services，待用户确认后才入表）。
    """
    host = str(host or "").strip()
    is_local = host.lower() in _LOCAL_HOST_ALIASES
    if is_local:
        # 本机发现（批次十三 C4）：空 host/localhost/127.0.0.1/::1 → 本地执行，
        # endpoint 归一为 localhost；user/key 凭据本地不需要，忽略。
        # v0.4：主机行名用本机 hostname（保大小写）——与第一层手动登记的
        # host 行（如 LAPTOP-T2JA2ERE）合并命中，不产生 localhost 重复行。
        host = "localhost"
        try:
            _hostname = _socket.gethostname().strip()
        except Exception:
            _hostname = ""
        if _hostname:
            host = _hostname
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
            ep_port: List[int] = []
            ep = svc.get("endpoint")
            if ep and ":" in str(ep):
                try:
                    ep_port.append(int(str(ep).rsplit(":", 1)[-1]))
                except ValueError:
                    pass
            for p in ep_port + (svc.get("extra_ports") or []):
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

    # 2) k8s 枚举（可选：kubectl 可用才扫）。v0.4：k8s = Service 一条（9.3），
    # Deployment 只喂实体档案 by_runtime.kubectl.deployments，不占服务行。
    k8s_res = _probe(runner, "kubectl get deploy,svc -A -o json 2>/dev/null")
    k8s_deploys: Dict[str, Dict[str, Any]] = {}
    if k8s_res.ok:
        runtime = "k3s" if runtime == "unknown" else runtime
        probes["kubectl"] = "ok"
        for row in _parse_kubectl(k8s_res.stdout):
            if row["kind"] == "k8s-deploy":
                k8s_deploys[f"{row.get('namespace')}/{row['name']}"] = row
                continue
            if row["kind"] != "k8s-service":
                continue
            for p in row.get("ports") or []:
                seen_ports.add(p)
            name = row["name"]
            if name not in [s["name"] for s in services]:
                node_port = row.get("ports")[0] if row.get("ports") else None
                detail_path = _entity_filename(cluster, host, name, env)
                image = ""
                dep = k8s_deploys.get(f"{row.get('namespace')}/{name}")
                if dep:
                    image = dep.get("image") or ""
                service_type = _classify_service_type(name, image)
                svc = {
                    "name": name,
                    "type": service_type,
                    "managed_by": "kubectl",
                    "endpoint": f"{host}:{node_port}" if node_port else None,
                    "extra_ports": row.get("ports")[1:] if len(row.get("ports") or []) > 1 else [],
                    "log_paths": [],
                    "depends_on": [],
                    "source": "discovered",
                    "last_verified": _dt.date.today().isoformat(),
                    "needs_review": True,
                    "detail": detail_path,
                }
                services.append(svc)
                deployments = []
                if dep:
                    deployments.append({"name": dep["name"], "replicas": 0, "ready": 0})
                details[name] = {
                    "name": name,
                    "detail": detail_path,
                    "version": _image_version(image),
                    "updated_at": _dt.date.today().isoformat(),
                    "checks": [],
                    "snapshot": {
                        "captured_at": _now_iso(),
                        "source": "discovered",
                        "common": {
                            "version": _image_version(image),
                            "config_dir": "",
                            "log_dir": "",
                            "data_dir": "",
                            "mode": "single",
                        },
                        "by_type": {},
                        "by_runtime": {
                            "kubectl": {
                                "namespace": row.get("namespace", ""),
                                "deployments": deployments,
                                "pvc": [],
                            },
                        },
                    },
                    "notes": "",
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
            service_type = _classify_service_type(name)
            svc = {
                "name": name,
                "type": service_type,
                "managed_by": "systemd",
                "endpoint": None,
                "extra_ports": [],
                "log_paths": [],
                "depends_on": [],
                "source": "discovered",
                "last_verified": _dt.date.today().isoformat(),
                "needs_review": True,
                "detail": detail_path,
            }
            ports = ss_proc_ports.get(_ss_proc_key(name)) or []
            if not ports:
                # 无监听端口 → 不入正式拓扑，保留 pending_review 待用户确认。
                pending_review.append(svc)
                continue
            services.append(svc)
            details[name] = {
                "name": name,
                "detail": detail_path,
                "version": "",
                "updated_at": _dt.date.today().isoformat(),
                "checks": [],
                "snapshot": {
                    "captured_at": _now_iso(),
                    "source": "discovered",
                    "common": {
                        "version": "",
                        "config_dir": "",
                        "log_dir": "",
                        "data_dir": "",
                        "mode": "single",
                    },
                    "by_type": {},
                    "by_runtime": {
                        "systemd": {
                            "unit": {
                                "load": "loaded",
                                "active": "active",
                                "sub": unit["state"],
                            },
                        },
                    },
                },
                "notes": "",
            }
            systemd_covered_ports.update(ports)
        probes["systemctl"] = (
            f"ok({len(systemd_units)} 服务，过滤 {systemd_filtered} 系统服务，"
            f"另 {len(pending_review)} 个无端口系统服务未入表（可确认）)"
        )
    else:
        probes["systemctl"] = "skipped（systemctl 不可用）"

    # 4) 端口扫描补条目：未映射到已知服务的监听端口 → 不入 services（9.3 排除
    # unidentified），进 pending_review 等确认（type=unknown 兜底过渡）。
    for listener in unmatched_listeners:
        port = listener["port"]
        if port in systemd_covered_ports:
            # systemd 服务已覆盖该监听端口（正常入表），不重复补 unidentified。
            continue
        if skip_unidentified:
            continue
        name = f"unidentified-{port}"
        pending_review.append({
            "name": name,
            "type": "unknown",
            "managed_by": "unknown",
            "endpoint": f"{host}:{port}",
            "source": "discovered",
            "last_verified": _dt.date.today().isoformat(),
            "needs_review": True,
        })

    # 5) 硬件/网络静态规格（v0.4 硬件层；GPU 探测并入其中，全部 best-effort）。
    host_endpoint = host
    if is_local:
        # 本机 endpoint：hostname -I 第一个地址（WSL 以太网 IP）；探测失败 → localhost。
        ip_res = _probe(runner, "hostname -I 2>/dev/null")
        if ip_res.ok and ip_res.stdout.strip():
            host_endpoint = ip_res.stdout.strip().split()[0]
        else:
            host_endpoint = "localhost"
    hardware = _probe_hardware(runner, probes, host_endpoint=host_endpoint)

    # 6) 主机 os 事实（/etc/os-release）。
    os_name = _probe_os(runner)
    if os_name:
        probes["os"] = os_name
    else:
        probes["os"] = "skipped"

    host_row = {
        "name": _safe_filename(host),
        "type": "host",
        "env": env,
        "cluster": cluster_display,
        "endpoint": host_endpoint,
        "role": _host_roles_for(runtime),
        "runtime": [runtime] if runtime and runtime != "unknown" else ["bare"],
        "os": os_name,
        "source": "discovered",
        "last_verified": _dt.date.today().isoformat(),
        "needs_review": True,
    }
    if not is_local and creds.get("key_path"):
        # 凭据数组（v0.4）：只落 type/ref/user/port，密码明文永不进拓扑。
        # 本机发现不走 SSH → 不写 ssh_key 凭据引用。
        host_row["credentials"] = [
            {"type": "ssh_key", "ref": str(creds["key_path"]), "user": user, "port": 22},
        ]
    return {
        "version": 4,
        "source": "discovered",
        "last_verified": _dt.date.today().isoformat(),
        "needs_review": True,
        "host": host_row,
        "services": services,
        "details": details,
        "hardware": hardware,
        "pending_review": pending_review,
        "probes": probes,
    }


# ---------------------------------------------------------------------------
# 落盘（用户确认后调用）
# ---------------------------------------------------------------------------

def _topo_layered(topo: Dict[str, Any]) -> bool:
    """分层 schema 判定：version 2/3/4，或 shape 检测（hosts/cross_host/clusters）。

    v0.1（扁平 ``core_entities``）不是分层结构，write_discovery 拒绝覆盖。
    """
    if topo.get("version") in (2, 3, 4):
        return True
    if "version" in topo:
        return False
    return bool(topo.get("hosts") or topo.get("cross_host") or topo.get("clusters"))


def _read_host_index(home: Path, hostname: str) -> Dict[str, Any]:
    """读取 services/<hostname>.yaml 服务索引（v0.4；老 hosts/ 目录兼容回退）。

    合并语义用；缺失/解析失败 → {}。文件名保大小写（大写 hostname 也命中）。
    """
    name = _safe_filename(hostname)
    lower = _sanitize_name(hostname)
    for rel in (f"services/{name}.yaml", f"hosts/{name}.yaml",
                f"services/{lower}.yaml", f"hosts/{lower}.yaml"):
        path = Path(home) / rel
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


# ---------------------------------------------------------------------------
# L3 写实体 → L2 hosts 索引同步（批三十九任务 1/4 统一入口）
# ---------------------------------------------------------------------------

# 写 L3 详情后需要同步到 L2 services 索引行的字段（审核/审计/状态生命周期字段）。
# 取值与 topo_update 写入 L3 的一致；其余顶层字段（endpoint/type 等）留在 L2
# 服务行——L2 是"服务目录"，L3 是"实体档案"，互不覆盖。
_L2_INDEX_SYNC_FIELDS = frozenset({"needs_review", "source", "last_verified"})


def sync_l2_index_row(
    home: Path,
    *,
    host_name: str,
    entity: Dict[str, Any],
    fields: Dict[str, Any],
    services_index: Optional[str] = None,
    cluster: str = "default",
) -> str:
    """写 L3 实体详情后，同步 L2 services 索引对应行的审核/状态字段（批三十九）。

    新老写入路径共用这一入口，杜绝第三处"只写 L3 不同步 L2"的漂移：
      - 索引文件 = ``services/<host>.yaml``（v0.4；老 hosts/ 由读取端兼容），
        或调用方传入的 ``services_index``（越界检查，与 topo_tools._safe_entity_path 同语义）；
      - name 匹配的行存在 → 用 ``fields`` 覆盖同步字段；
      - 行缺失 → 按 ``entity``（L2 行形态）补建一行再应用 fields——索引层缺行
        本身就是漂移，补建优先，不静默丢实体；
      - 写盘失败不吞掉：返回可读 warning 文案（调用方记日志 + 带进返回），
        成功返回空串。只改写入路径，不改调用方返回契约。
    """
    home = Path(home)
    host_name = str(host_name or "").strip()
    if not host_name:
        return "L2 索引同步跳过：host 名为空"
    name = str((entity or {}).get("name") or "").strip()
    if not name:
        return "L2 索引同步跳过：实体行缺少 name"
    index_rel = str(services_index or f"services/{_safe_filename(host_name)}.yaml")
    try:
        root = home.resolve()
        index_path = (home / index_rel).resolve()
        index_path.relative_to(root)
    except ValueError:
        return f"L2 索引同步跳过：索引路径越界（{index_rel}）"
    try:
        data: Dict[str, Any] = {}
        if index_path.is_file():
            loaded = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data = loaded
        rows = data.get("services")
        if not isinstance(rows, list):
            rows = []
        data["services"] = rows
        row = next(
            (r for r in rows if isinstance(r, dict) and str(r.get("name") or "") == name),
            None,
        )
        if row is None:
            row = {
                k: v for k, v in entity.items()
                if not str(k).startswith("_")
            }
            row["name"] = name
            row.setdefault("cluster", cluster)
            rows.append(row)
        for k, v in fields.items():
            row[k] = v
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return ""
    except Exception as exc:
        return f"L2 索引同步失败：{exc}"


def write_discovery(home: Path, discovery: Dict[str, Any], force: bool = False,
                    merge: bool = True) -> Dict[str, Any]:
    """把发现结果落盘为 v0.4 结构（services/<host>.yaml + topology.yaml +
    entities/ + hardware/<host>.yaml）。

    - 现有 topology.yaml 为 v0.1（扁平 core_entities）时拒绝写入（不自动改写用户数据）；
    - host 已存在且未 ``force`` → 合并语义（批次十八 B 延续，适配 v0.4）：发现的
      新服务自动追加进 services/<host>.yaml + entities/，已有同名服务/实体保留
      （含手动 endpoint/type/managed_by）；**第一层 host 行治理字段
      （owner/department/os/role/runtime/credentials）不覆盖**，只刷新
      last_verified；``merge=False`` 显式关闭合并时维持旧的拒绝语义（需
      ``--force`` 整体替换）；
    - 写路径：services/<hostname>.yaml（第二层服务目录）、
      entities/{cluster}__{host}__{name}.yaml（第三层档案草案，OPS-DELTA #42
      L3 命名）、hardware/<hostname>.yaml（硬件层）、topology.yaml 的 hosts 段；
    - 只产 v0.4：``version: 4``、hosts 行 v0.4（role/runtime 数组 + credentials
      数组，无 services_index）、删除 sources/cross_host/key_paths 顶层字段；
      environments 只写四值档位（老自定义 env 按档位映射）。

    Returns:
      ``{"written", "topology", "index", "appended", "kept", "merged"}``——
      appended = 本次新增服务数，kept = 合并时保留的现有索引行数（新 host/force
      为 0），merged = 是否走了合并路径。
    """
    home = Path(home)
    host_row = dict(discovery.get("host") or {})
    hostname = _safe_filename(host_row.get("name") or "")
    if not hostname:
        raise DiscoveryError("发现结果缺少 host.name，无法落盘。")
    host_row["name"] = hostname
    host_row.setdefault("cluster", "default")
    # v0.4：services_index 已删除——第二层路径约定 services/<host>.yaml。
    host_row.pop("services_index", None)
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
                "topo-discover 只写 v0.4；请先迁移（可运行 vigil ops-init --force "
                "重铺样例，或手动添加 version: 2/3/4 + hosts: 段）后再发现。"
            )
        hosts = [h for h in topo.get("hosts") or [] if isinstance(h, dict)]
        host_exists = any(h.get("name") == hostname for h in hosts)
        if host_exists and not force and not merge:
            raise DiscoveryError(
                f"host {hostname} 已存在于 topology.yaml（--force 覆盖）。"
            )
        if host_exists and not force:
            # 合并：保留手动维护的 host 行（owner/department/os/role/runtime/
            # credentials 等治理字段一律不覆盖），只刷新校验时间。
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
        topo["version"] = 4
        topo["updated_at"] = _dt.date.today().isoformat()
    else:
        host_exists = False
        topo = {
            "version": 4,
            "updated_at": _dt.date.today().isoformat(),
            "environments": [
                {"name": env_tier,
                 "isolation": "strict" if env_tier == "prod" else "relaxed",
                 "role": env_tier}
            ] if env_tier else [],
            "hosts": [host_row],
            "clusters": [],
        }

    # v0.4 删除的顶层字段（sources/services_index/cross_host/key_paths）写盘时
    # 一律剥离——发现结果只产 v0.4 结构，不留旧字段。
    for _deleted_key in ("sources", "services_index", "cross_host", "key_paths"):
        topo.pop(_deleted_key, None)

    # 第二层：services/<hostname>.yaml 服务目录（合并：同名跳过、新服务追加）。
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
            # 同名跳过：保留现有行（含手动 endpoint/type/managed_by），不覆盖。
            continue
        merged_rows.append(row)
        appended += 1
    kept = len(existing_rows) if (host_exists and not force) else 0

    index_data = {
        "host": hostname,
        "updated_at": _dt.date.today().isoformat(),
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
        rel = str(detail.get("detail") or f"entities/{_strip_entity_ext(_sanitize_name(name))}.yaml")
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

    services_dir = home / "services"
    services_dir.mkdir(parents=True, exist_ok=True)
    index_path = services_dir / f"{hostname}.yaml"
    index_path.write_text(
        yaml.safe_dump(index_data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    # 硬件层：hardware/<hostname>.yaml（v0.4 新增；发现片段带 hardware 才写）。
    hardware_written: List[str] = []
    hardware = discovery.get("hardware")
    if isinstance(hardware, dict):
        hardware_dir = home / "hardware"
        hardware_dir.mkdir(parents=True, exist_ok=True)
        hardware_path = hardware_dir / f"{hostname}.yaml"
        hardware_data = {
            "host": hostname,
            "updated_at": _dt.date.today().isoformat(),
            **{k: v for k, v in hardware.items() if k not in ("host", "updated_at")},
        }
        hardware_path.write_text(
            yaml.safe_dump(hardware_data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        hardware_written = [str(hardware_path.relative_to(home))]
    topo_path.parent.mkdir(parents=True, exist_ok=True)
    topo_path.write_text(
        yaml.safe_dump(topo, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    written = [str(index_path.relative_to(home)), str(topo_path.relative_to(home))]
    written.extend(entity_paths)
    written.extend(hardware_written)
    return {
        "written": written,
        "topology": topo_path,
        "index": index_path,
        "appended": appended,
        "kept": kept,
        "merged": bool(host_exists and not force),
    }

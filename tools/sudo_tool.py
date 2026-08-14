"""``sudo_exec`` —— agent 正规提权工具（OPS-DELTA 批次十六 §W，§V 根因对策）。

**行为约束（第一行）**：提权/需 sudo 的命令**一律走本工具**，禁止自行拼
``sudo -S <<< '密码'``、``SUDO_PASS=$(curl vault | jq)``、``echo 密码 | sudo -S``
等管道形态——密码由工具从凭据来源内部注入，命令串/argv/展示层永不出现密码。

凭据从拓扑表 host 行的 ``credential`` 引用解析（复用 vssh 的
``_resolve_topology_credential`` + ``_build_ssh_argv``：ssh_key/vault/askpass
三通道，port 缺省 22）；sudo 密码只经 ASKPASS 注入：

- 本地 sudo：``sudo -A <command>`` + env ``SUDO_ASKPASS=<0700 askpass 脚本>``
  （复用 :func:`tools.topo_discovery._make_askpass_script`，脚本只 ``cat`` 保险箱
  文件）——密码不进 argv、不进 stdin 管道、不进 ps（``echo pass | sudo -S`` 的
  管道在 ps 可见，sudo 文档明确反对）。
- 远端 sudo（ssh host）：``ssh <argv> "SUDO_ASKPASS=<远端脚本> sudo -A <command>"``——
  0700 askpass 脚本 + 保险箱文件 scp 到远端 /tmp（凭据文件本身不过 ssh 命令串），
  执行后立即删除。**待实测项**：RHEL requiretty 场景 ``ssh -tt`` 强制 pty + 监听
  ``[sudo] password`` 提示再注入的时序本批不落地（验收时若真实远端时序不稳，
  返回明确错误而不是挂起）。

**安全边界**：只接受单条只读诊断命令——拒绝嵌套 shell（``bash -c``）、重定向到
文件（``>``/``>>``）、后台（``&``）、多命令分隔（``;``/``||``）、命令替换
（``$(…)``/反引号）、内嵌 sudo（防 ``sudo -S <<<`` 形态死灰复燃）。

**权限矩阵联动**：执行前 ``sudo <command>`` 过 ops_permissions 判定——prod 变更类
→ 返回 require_confirmation（强制人工确认门）；deny → 拒绝；只读诊断（L1 查询档）
直接执行。凭据缺失/认证失败 → 停下来问用户（提供凭据或手动执行），禁止翻
~/.ssh/ 试密钥、禁止连续猜 vault 字段、禁止换用户名试登录（§Q/§AD 教训——
会触发 SSH 认证熔断，且违反 fail-closed）。

四层骨架（本批只落地 sudo 适配器，后续批次扩展）：
agent → credential resolver（§T 四档来源）→ injector adapter（sudo/ssh）→ executor。
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_cli.subcommands.vssh import _build_ssh_argv, _resolve_topology_credential
from tools.credential_vault import path_for
from tools.ops_permissions import check_ops_command_permission
from tools.registry import registry, tool_error
from tools.topo_discovery import _make_askpass_script
from tools.topo_tools import check_topo_requirements

logger = logging.getLogger(__name__)

_EXEC_TIMEOUT_S = 120
_PROVISION_TIMEOUT_S = 60
_LOCAL_HOST_ALIASES = ("localhost", "127.0.0.1", "::1", "")

# 命令校验黑名单（确定性拒绝，防注入；全部 fail-closed）
_FORBIDDEN_COMMAND_PATTERNS = (
    (re.compile(r"\b(bash|sh|zsh|dash|fish)\s+-c\b", re.IGNORECASE), "嵌套 shell（bash -c …）"),
    (re.compile(r"(^|[\s/])/?bin/(bash|sh|zsh|dash)(\s|$)", re.IGNORECASE), "嵌套 shell（/bin/bash …）"),
    (re.compile(r">"), "重定向（> / >> / 2>&1）"),
    (re.compile(r"&"), "后台 / 逻辑与（& / && / >&）"),
    (re.compile(r";"), "多命令分隔（;）"),
    (re.compile(r"\|\|"), "逻辑或（||）"),
    (re.compile(r"\$\("), "命令替换（$(…)）"),
    (re.compile(r"`"), "命令替换（反引号）"),
    (re.compile(r"\bsudo\b", re.IGNORECASE), "内嵌 sudo（本工具已提供提权）"),
    (re.compile(r"\n"), "多行命令"),
)


def _validate_command(command: str) -> Optional[str]:
    """单条只读命令校验：命中黑名单 → 返回拒绝原因；合法 → None。"""
    for pattern, reason in _FORBIDDEN_COMMAND_PATTERNS:
        if pattern.search(command):
            return f"sudo_exec 拒绝 command：{reason}（fail-closed，只读诊断请走单条命令）"
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return f"sudo_exec 拒绝 command：无法解析（{exc}）"
    if not tokens:
        return "sudo_exec 拒绝 command：空命令"
    return None


def _sudo_askpass_for_vault(ref: str) -> Path:
    """vault 凭据 → 0700 askpass 脚本（只 cat 保险箱文件，不进 argv/env）。"""
    return _make_askpass_script(path_for(str(ref)))


def _write_askpass_cat(vault_path: str) -> Path:
    """写 0700 askpass 脚本：``cat <vault_path>``（远端脚本用远端路径）。"""
    script = Path(tempfile.mkstemp(prefix="vigil-sudo-askpass-", suffix=".sh")[1])
    script.write_text(f"#!/bin/sh\ncat {vault_path}\n", encoding="utf-8")
    os.chmod(script, 0o700)
    return script


def _scp_argv_from_ssh(ssh_argv: List[str], local: Path, dest: str) -> List[str]:
    """ssh argv → scp argv：``-p <port>`` → ``-P <port>``，追加 <local> <user@host>:<dest>。"""
    out = ["scp", "-P", ssh_argv[2]]
    # ssh_argv = ["ssh", "-p", port, ("-i", key)?, "user@host"]——target 是末元素
    for piece in ssh_argv[3:-1]:
        out.append(piece)
    out += [str(local), f"{ssh_argv[-1]}:{dest}"]
    return out


def _run_local_sudo(command: str, cred: Dict[str, Any]) -> subprocess.CompletedProcess:
    """本地 sudo：``sudo -A <command>`` + SUDO_ASKPASS env（密码不进 argv/stdin）。"""
    cred_type = str(cred.get("type") or "")
    askpass: Optional[Path] = None
    try:
        if cred_type == "vault":
            askpass = _sudo_askpass_for_vault(str(cred["ref"]))
        elif cred_type == "askpass":
            askpass = Path(str(cred["ref"]))
        else:
            raise RuntimeError(
                "本地 sudo 需要 vault/askpass 类型凭据（ssh_key 无密码明文）——"
                "请补充拓扑表 credential 声明或手动执行"
            )
        if not askpass.is_file():
            raise FileNotFoundError(f"askpass 脚本不存在: {askpass}")
        argv = ["sudo", "-A"] + shlex.split(command)
        env = dict(os.environ)
        env["SUDO_ASKPASS"] = str(askpass)
        logger.debug("sudo_exec: local argv=%s (SUDO_ASKPASS=%s)", argv[0:2], askpass)
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=_EXEC_TIMEOUT_S, env=env)
    finally:
        if askpass is not None and cred_type == "vault":
            try:
                askpass.unlink()
            except OSError:
                pass


def _ssh_run(ssh_argv: List[str], ssh_env: Dict[str, str], remote_cmd: str,
             timeout: int = _EXEC_TIMEOUT_S) -> subprocess.CompletedProcess:
    """``ssh <argv> "<remote_cmd>"``（argv 已含 user@host 目标）。"""
    return subprocess.run(ssh_argv + [remote_cmd], capture_output=True, text=True,
                          timeout=timeout, env=ssh_env)


def _run_remote_sudo(host: str, user: str, port: int, command: str,
                     cred: Dict[str, Any]) -> subprocess.CompletedProcess:
    """远端 sudo：scp 0700 askpass + 保险箱文件 → 远端 /tmp → ``sudo -A`` → 清理。"""
    cred_type = str(cred.get("type") or "")
    if cred_type != "vault":
        raise RuntimeError(
            "远端 sudo 需要 vault 类型凭据（携带 sudo 密码）——ssh_key 无密码、"
            "askpass 类型暂不支持远端注入；请补充拓扑表 vault credential 声明或手动执行"
        )
    vault_file = path_for(str(cred["ref"]))
    if not vault_file.is_file():
        raise FileNotFoundError(f"保险箱中不存在凭据: {cred['ref']}")

    basename = f"vigil-sudo-{os.getpid()}-{secrets.token_hex(4)}"
    remote_script = f"/tmp/{basename}.sh"
    remote_vault = f"/tmp/{basename}.vault"
    askpass_local = _write_askpass_cat(remote_vault)  # 脚本 cat 远端 vault 路径
    ssh_argv, ssh_env = _build_ssh_argv(host, user=user, port=port, cred=cred)
    try:
        _scp(ssh_argv, ssh_env, askpass_local, f"{user}@{host}:{remote_script}")
        _scp(ssh_argv, ssh_env, vault_file, f"{user}@{host}:{remote_vault}")
        _ssh_run(ssh_argv, ssh_env,
                 f"chmod 700 {remote_script}; chmod 600 {remote_vault}",
                 timeout=_PROVISION_TIMEOUT_S)
        return _ssh_run(ssh_argv, ssh_env,
                        f"SUDO_ASKPASS={remote_script} sudo -A {command}",
                        timeout=_EXEC_TIMEOUT_S)
    finally:
        try:
            _ssh_run(ssh_argv, ssh_env, f"rm -f {remote_script} {remote_vault}",
                     timeout=_PROVISION_TIMEOUT_S)
        except Exception:
            logger.warning("sudo_exec: 远端临时凭据清理失败 %s@%s", user, host)
        finally:
            try:
                askpass_local.unlink()
            except OSError:
                pass


def _scp(ssh_argv: List[str], ssh_env: Dict[str, str], local: Path, dest: str) -> None:
    """scp 上传（复用 ssh 的 key/askpass 认证 env）。"""
    proc = subprocess.run(_scp_argv_from_ssh(ssh_argv, local, dest),
                          capture_output=True, text=True,
                          timeout=_PROVISION_TIMEOUT_S, env=ssh_env)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(f"scp 上传失败：{detail[-1] if detail else '未知错误'}")


def _sudo_exec_handler(args: Dict[str, Any], **kwargs) -> str:
    host = str(args.get("host") or "").strip()
    command = str(args.get("command") or "").strip()
    target_env = str(args.get("env") or "").strip() or None

    if not command:
        return tool_error("sudo_exec 需要 command（要在远端/本机以 sudo 执行的只读诊断命令）")
    err = _validate_command(command)
    if err:
        return tool_error(err)

    # 权限矩阵联动：``sudo <command>`` 过 ops_permissions（env 档位 + 变更类判定）。
    decision = check_ops_command_permission(f"sudo {command}", target_env=target_env)
    if decision:
        if decision.get("action") == "deny":
            return tool_error(f"权限矩阵拒绝执行：{decision.get('description') or 'deny'}")
        if decision.get("action") == "approve":
            return json.dumps({
                "status": "require_confirmation",
                "action": "approve",
                "grade": decision.get("grade"),
                "env": decision.get("env"),
                "require_confirmation": bool(decision.get("require_confirmation")),
                "description": decision.get("description") or "",
                "command": f"sudo {command}",
                "message": (
                    "该命令需要人工确认后才执行——向用户展示完整命令并取得确认后再执行；"
                    "或改跑只读诊断命令（L1 查询档无需确认）。"
                ),
            }, ensure_ascii=False)

    # 凭据解析：拓扑表 host 行 credential 引用（ssh_key/vault/askpass 三通道）。
    cred = _resolve_topology_credential(host)
    if not cred:
        return tool_error(
            f"sudo_exec 缺少 sudo 凭据：拓扑表中 host {host or '(未指定)'} 无 credential 引用。"
            "请停止自动重试：1) 手动执行该命令 2) 或在拓扑表补充 credential 声明"
            "（vault 类型，见 vssh / topo credential）"
        )
    user = str(cred.get("user") or "root")
    port = int(cred.get("port") or 22)

    is_local = host in _LOCAL_HOST_ALIASES
    try:
        if is_local:
            result = _run_local_sudo(command, cred)
        else:
            result = _run_remote_sudo(host, user, port, command, cred)
    except subprocess.TimeoutExpired:
        return tool_error(f"sudo_exec 执行超时（{_EXEC_TIMEOUT_S}s）：sudo {command}")
    except OSError as exc:
        return tool_error(f"sudo_exec 无法执行：{exc}")
    except Exception as exc:
        return tool_error(f"sudo_exec 失败：{exc}")

    return json.dumps({
        "status": "ok",
        "host": host or "local",
        "command": f"sudo -A {command}",
        "stdout": (result.stdout or ""),
        "stderr": (result.stderr or ""),
        "exit_code": result.returncode,
    }, ensure_ascii=False)


_SUDO_EXEC_SCHEMA = {
    "name": "sudo_exec",
    "description": (
        "提权执行：在目标主机以 sudo 运行只读诊断命令。"
        "**提权/需 sudo 的命令一律走本工具**——禁止自行拼 `sudo -S <<< '密码'`、"
        "`SUDO_PASS=$(curl vault | jq)`、`echo 密码 | sudo -S` 等管道形态：密码由本工具"
        "从凭据来源内部注入（ASKPASS），命令串/argv/展示层永不出现密码。"
        "凭据从拓扑表 host 行的 credential 引用自动读取（ssh_key/vault/askpass 三通道）。"
        "凭据缺失或认证失败时**停下来问用户**（提供凭据或手动执行），禁止翻 ~/.ssh/ 试密钥、"
        "禁止猜 vault 字段、禁止换用户名试登录。"
        "安全边界：只接受单条只读命令——拒绝 bash -c 嵌套 shell、重定向到文件（> / >>）、"
        "后台（&）、多命令分隔（;）、命令替换（$()/反引号）、内嵌 sudo。"
        "prod 环境变更类命令（重启/重建/配置下发）需人工确认；只读诊断（ps/ss/vmstat/cat）"
        "直接执行。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "host": {
                "type": "string",
                "description": "拓扑实体 host 名或 IP（从拓扑表取 credential 凭据引用）。",
            },
            "command": {
                "type": "string",
                "description": "要在远端/本机以 sudo 执行的命令（单条只读诊断命令，如 ss -tlnp / ps aux）。",
            },
            "env": {
                "type": "string",
                "description": "目标环境（local/test/dev/prod），用于权限矩阵判定。",
            },
        },
        "required": ["host", "command"],
    },
}


registry.register(
    name="sudo_exec",
    toolset="topo",
    schema=_SUDO_EXEC_SCHEMA,
    handler=_sudo_exec_handler,
    check_fn=check_topo_requirements,
    emoji="🔑",
    max_result_size_chars=60_000,
)

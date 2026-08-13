"""``vigil vssh`` —— 常用运维命令第一块（OPS-DELTA #26 产品化）。

vault 安全凭据的交互 SSH：密码/passphrase 经保险箱 → SSH_ASKPASS 注入
（复用 :mod:`tools.topo_discovery` 的 askpass 机制），命令串/argv/env 不出现
明文；host 在拓扑表带 ``credential`` 引用时自动读取（ssh_key → ``-i``；
vault/askpass → 保险箱注入），无凭据回退 ssh-agent / ssh 交互。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from tools.topo_discovery import _make_askpass_script


def _resolve_topology_credential(host: str) -> Optional[Dict]:
    """读取拓扑表 host 行的 credential 引用（ssh_key/vault/askpass）。

    无拓扑数据 / host 无凭据引用 / 读取异常 → None（回退 ssh-agent/交互）。
    """
    try:
        from hermes_constants import get_hermes_home
        from tools.topo_tools import load_topology
    except Exception:
        return None
    try:
        topo = load_topology(Path(get_hermes_home()))
        if not topo:
            return None
        want = str(host or "").strip().lower()
        for row in topo.get("hosts") or []:
            name = str(row.get("name") or "").strip().lower()
            endpoint = str(row.get("endpoint") or "").strip().lower()
            if name == want or endpoint == want:
                cred = row.get("credential")
                return dict(cred) if isinstance(cred, dict) else None
    except Exception:
        return None
    return None


def _build_ssh_argv(host: str, *, user: str, port: int = 22,
                    key: Optional[str] = None,
                    cred: Optional[Dict] = None) -> Tuple[List[str], Dict[str, str]]:
    """构造 ``ssh`` argv + env（凭据经 askpass 注入，明文不进 argv/env）。

    cred（拓扑 credential 引用，port 缺省 22）：
      - ``ssh_key``: ref = 私钥路径 → ``-i <ref>``；
      - ``vault``:   ref = 保险箱凭据名 → askpass 脚本读保险箱文件；
      - ``askpass``: ref = askpass 脚本路径 → 直接作为 SSH_ASKPASS。
    """
    argv: List[str] = ["ssh", "-p", str(port)]
    env = dict(os.environ)
    key_path = key
    askpass_ref: Optional[str] = None
    if cred:
        port = int(cred.get("port") or port)
        argv[2] = str(port)
        cred_type = str(cred.get("type") or "")
        if cred_type == "ssh_key":
            key_path = key_path or cred.get("ref")
        elif cred_type == "vault":
            try:
                from tools.credential_vault import path_for
                askpass_ref = str(_make_askpass_script(path_for(str(cred["ref"]))))
            except Exception:
                askpass_ref = None
        elif cred_type == "askpass":
            askpass_ref = str(cred.get("ref") or "")
    if key_path:
        argv += ["-i", str(key_path)]
    if askpass_ref:
        env["SSH_ASKPASS"] = askpass_ref
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", ":0")
    argv += [f"{user}@{host}"]
    return argv, env


def _split_hostspec(hostspec: str, user: Optional[str] = None) -> Tuple[str, str]:
    """``user@host`` / ``host`` → (user, host)。"""
    spec = str(hostspec or "").strip()
    if "@" in spec:
        u, h = spec.rsplit("@", 1)
        return (u or user or "root"), h
    return (user or "root"), spec


def run(args) -> int:
    """执行 vssh：解析凭据 → 构造 ssh argv/env → exec（密码不回显）。"""
    user, host = _split_hostspec(getattr(args, "hostspec", None),
                                 getattr(args, "user", None))
    if not host:
        print("✗ vssh 需要目标主机（<host> 或 <user>@<host>）", file=sys.stderr)
        return 2
    port = int(getattr(args, "port", None) or 22)
    cred = None
    if not getattr(args, "no_credential", False):
        cred = _resolve_topology_credential(host)
    argv, env = _build_ssh_argv(host, user=user, port=port,
                                key=getattr(args, "key", None), cred=cred)
    os.execvpe("ssh", argv, env)
    print(f"✗ 无法执行 ssh：{argv[0]}", file=sys.stderr)
    return 127


def build_vssh_parser(subparsers, *, cmd_vssh: Callable) -> None:
    """Attach the ``vssh`` subcommand to ``subparsers``."""
    vssh_parser = subparsers.add_parser(
        "vssh",
        help="常用运维命令：vault 安全凭据的交互 SSH（密码经保险箱注入，不回显）",
        description=(
            "常用运维命令第一块（OPS-DELTA #26 产品化）：vault 安全凭据的交互 SSH。"
            "密码/passphrase 经保险箱 → SSH_ASKPASS 注入，命令串/argv/env 不出现明文；"
            "host 在拓扑表带 credential 引用时自动读取（ssh_key → -i；vault/askpass → "
            "保险箱注入），无凭据回退 ssh-agent/交互。"
        ),
    )
    vssh_parser.add_argument("hostspec",
                             help="目标主机：<host> 或 <user>@<host>（IP/主机名）")
    vssh_parser.add_argument("-p", "--port", type=int, default=22,
                             help="SSH 端口（默认 22；拓扑 credential 引用有 port 时优先）")
    vssh_parser.add_argument("-i", "--key", default=None,
                             help="SSH 私钥路径（默认读拓扑 credential 引用 / ssh-agent）")
    vssh_parser.add_argument("-u", "--user", default=None,
                             help="SSH 用户（默认 root；hostspec 带 user@ 时优先）")
    vssh_parser.add_argument("--no-credential", action="store_true",
                             help="忽略拓扑凭据引用，走 ssh-agent/ssh 交互")
    vssh_parser.set_defaults(func=cmd_vssh)


def main(argv: Optional[List[str]] = None) -> int:
    """独立入口（测试/直接调用）：argv → Namespace → run。"""
    parser = argparse.ArgumentParser(
        prog="vigil vssh",
        description="vault 安全凭据的交互 SSH（常用运维命令；密码经保险箱安全注入，不回显）。",
    )
    parser.add_argument("hostspec", nargs="?",
                        help="目标主机：<host> 或 <user>@<host>（IP/主机名）")
    parser.add_argument("-p", "--port", type=int, default=22,
                        help="SSH 端口（默认 22；拓扑 credential 引用有 port 时优先）")
    parser.add_argument("-i", "--key", default=None,
                        help="SSH 私钥路径（默认读拓扑 credential 引用 / ssh-agent）")
    parser.add_argument("-u", "--user", default=None,
                        help="SSH 用户（默认 root；hostspec 带 user@ 时优先）")
    parser.add_argument("--no-credential", action="store_true",
                        help="忽略拓扑凭据引用，走 ssh-agent/ssh 交互")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

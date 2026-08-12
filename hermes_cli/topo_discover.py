"""``vigil topo-discover`` —— 引导式拓扑自动发现（OPS-DELTA #12）。

中间市场用户不会手写 topology.yaml：SSH 进主机 → 扫 docker/k8s/端口/GPU →
自动生成 schema v0.2 片段（第一层 host 行 + 第二层服务索引 + 第三层详情草案）
→ 展示 → 用户确认后落盘。凭据经保险箱（tools/credential_vault）存储，
密码走 SSH_ASKPASS 注入，命令串/日志不出现明文。

用法：
    vigil topo-discover --host <ip> --env <env> [--user <u>] [--key <path>]
                        [--dry-run] [--force] [--yes]
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.topo_discovery import (
    DiscoveryError,
    _make_askpass_script,
    _sanitize_name,
    discover_host,
    write_discovery,
)

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _active_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _store_password(host: str, password: str) -> Path:
    """密码入保险箱（user 来源），返回 askpass 读取用的保险箱文件路径。"""
    from tools.credential_vault import path_for, store
    name = f"discover-{_sanitize_name(host)}"
    store(name, password, source="user")
    return path_for(name)


def _prompt_credentials(host: str, user: str, key: Optional[str]) -> Dict[str, Any]:
    """交互收集 SSH 认证信息（不落明文）。

    - ``--key`` → key/agent 认证（BatchMode）；
    - 交互 tty → getpass 收密码 → 存保险箱 → askpass 注入；
    - 非 tty 且无 key → BatchMode（依赖 ssh-agent/key），打印提示。
    """
    creds: Dict[str, Any] = {"user": user}
    if key:
        creds["key_path"] = key
        return creds
    if sys.stdin.isatty():
        pw = getpass.getpass(f"SSH 密码（{user}@{host}，仅本次使用，安全存储不回显；直接回车则用 key/agent）：")
        if pw:
            vault_file = _store_password(host, pw)
            creds["askpass_file"] = str(_make_askpass_script(vault_file))
            return creds
    print("· 未提供密码：使用 ssh-agent / 默认 key（BatchMode）连接。")
    return creds


def _print_summary(discovery: Dict[str, Any]) -> None:
    host = discovery.get("host") or {}
    probes = discovery.get("probes") or {}
    services = discovery.get("services") or []
    print("\n===== 发现结果（schema v0.2，needs_review=true） =====")
    print(f"host: {host.get('name')}  env: {host.get('env')}  "
          f"runtime: {host.get('runtime')}  endpoint: {host.get('endpoint')}")
    if host.get("attrs", {}).get("gpu"):
        print(f"gpu: {', '.join(host['attrs']['gpu'])}")
    print(f"services: {len(services)}")
    for svc in services:
        ports = (svc.get("attrs") or {}).get("ports") or []
        img = (svc.get("attrs") or {}).get("image") or ""
        endpoint = svc.get("endpoint") or "-"
        print(f"  - {svc.get('name')}  type={svc.get('type')}  endpoint={endpoint}"
              + (f"  image={img}" if img else "")
              + (f"  ports={ports}" if ports else ""))
    print("probes: " + ", ".join(f"{k}={v}" for k, v in sorted(probes.items())))
    print("====================================================")


def _confirm(force: bool, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        answer = input("确认写入拓扑（hosts/ + entities/ + topology.yaml）? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vigil topo-discover",
        description="SSH 进主机自动发现 docker/k8s/端口/GPU，生成 schema v0.2 拓扑片段，确认后落盘。",
    )
    parser.add_argument("--host", required=True, help="目标主机 IP/主机名")
    parser.add_argument("--env", required=True, help="目标环境（test/uat/prod/自定义）")
    parser.add_argument("--user", default=None, help="SSH 用户（默认 root）")
    parser.add_argument("--key", default=None, help="SSH 私钥路径（默认走 ssh-agent）")
    parser.add_argument("--dry-run", action="store_true", help="只展示发现结果，不落盘")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 host 条目")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认（配合 --force 可非交互落盘）")
    args = parser.parse_args(argv)

    if not _HOST_RE.fullmatch(args.host):
        print(f"✗ 非法 --host {args.host!r}：仅支持字母/数字/._:-", file=sys.stderr)
        return 2

    home = _active_home()
    user = args.user or "root"
    creds = _prompt_credentials(args.host, user, args.key)
    try:
        discovery = discover_host(args.host, args.env, creds)
    except DiscoveryError as exc:
        print(f"✗ 发现失败：{exc}", file=sys.stderr)
        return 1

    _print_summary(discovery)

    if args.dry_run:
        print("\n· --dry-run：未写入任何文件。")
        return 0

    if not _confirm(args.force, args.yes):
        print("\n· 已取消，未写入任何文件（可用 --dry-run 预览，--yes 跳过确认）。")
        return 0

    try:
        result = write_discovery(home, discovery, force=args.force)
    except DiscoveryError as exc:
        print(f"✗ 落盘失败：{exc}", file=sys.stderr)
        return 1

    print("\n· 已写入：")
    for path in result["written"]:
        print(f"    {home / path}")
    print("\n· 提示：发现结果带 needs_review=true，请核对后再纳入权威拓扑"
          "（topo_update 或人工确认后置为 false）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

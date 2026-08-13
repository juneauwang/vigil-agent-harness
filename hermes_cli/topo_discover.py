"""``vigil topo-discover`` —— 引导式拓扑自动发现（OPS-DELTA #12）。

中间市场用户不会手写 topology.yaml：SSH 进主机 → 扫 docker/k8s/端口/GPU →
自动生成 schema v0.2 片段（第一层 host 行 + 第二层服务索引 + 第三层详情草案）
→ 展示 → 用户确认后落盘。凭据经保险箱（tools/credential_vault）存储，
密码走 SSH_ASKPASS 注入，命令串/日志不出现明文。

用法：
    vigil topo-discover --host <ip> --env <env> [--user <u>] [--key <path>]
                        [--dry-run] [--force] [--yes]
    vigil topo-discover --env local             # host 省略 = 本机发现（无需凭据）
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from tools.topo_discovery import (
    DiscoveryError,
    _make_askpass_script,
    _sanitize_name,
    discover_host,
    write_discovery,
)

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_HOST_RANGE_RE = re.compile(r"\[([0-9]+)-([0-9]+)\]")


def _active_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _store_password(host: str, password: str, suffix: str = "") -> Path:
    """密码入保险箱（user 来源），返回 askpass 读取用的保险箱文件路径。"""
    from tools.credential_vault import path_for, store
    name = f"discover-{_sanitize_name(host)}"
    if suffix:
        name = f"{name}-{_sanitize_name(suffix)}"
    store(name, password, source="user")
    return path_for(name)


def _expand_host_ranges(token: str) -> List[str]:
    """展开 ``10.123.66.23[3-8]`` 这类单/多区间主机表示。"""
    match = _HOST_RANGE_RE.search(token)
    if not match:
        return [token]
    start, end = int(match.group(1)), int(match.group(2))
    if start > end or end - start > 1023:
        raise ValueError(f"非法主机区间：{token!r}")
    width = max(len(match.group(1)), len(match.group(2)))
    prefix, suffix = token[: match.start()], token[match.end():]
    expanded: List[str] = []
    for value in range(start, end + 1):
        expanded.extend(_expand_host_ranges(prefix + str(value).zfill(width) + suffix))
    return expanded


def _expand_hosts(values: Sequence[str]) -> List[str]:
    """逗号列表 + ``[start-end]`` 区间展开，保持顺序并去重。"""
    hosts: List[str] = []
    seen = set()
    for value in values:
        for token in str(value or "").split(","):
            token = token.strip()
            if not token:
                continue
            for host in _expand_host_ranges(token):
                if not _HOST_RE.fullmatch(host):
                    raise ValueError(f"非法 --host {host!r}：仅支持字母/数字/._:-")
                if host in seen:
                    continue
                seen.add(host)
                hosts.append(host)
    return hosts


def _read_hosts_file(path: str) -> List[str]:
    """读取 ``--hosts`` 文件（每行一个 host，忽略空白行/注释）。"""
    host_file = Path(path)
    if not host_file.is_file():
        raise ValueError(f"--hosts 文件不存在：{path}")
    hosts = []
    for line in host_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            hosts.append(line)
    return hosts


_LOCAL_HOST_ALIASES = ("localhost", "127.0.0.1", "::1")


def _is_local_host(host: str) -> bool:
    """host 是否本机（localhost/127.0.0.1/::1）：本机发现不走 SSH、无需凭据。"""
    return str(host or "").strip().lower() in _LOCAL_HOST_ALIASES


def _prompt_credentials(host: str, user: str, key: Optional[str],
                        password: Optional[str] = None,
                        key_passphrase: Optional[str] = None,
                        sudo_password: Optional[str] = None) -> Dict[str, Any]:
    """交互收集 SSH 认证信息（不落明文）。

    - ``--key`` → key/agent 认证（BatchMode）；
    - ``--password``/``--password-stdin`` → 显式提供密码时跳过交互提示；
    - ``--key-passphrase`` → 加密私钥 passphrase；
    - ``--sudo-password`` → 探测命令 sudo 密码；
    - 交互 tty → getpass 收密码 → 存保险箱 → askpass 注入；
    - 非 tty 且无 key → BatchMode（依赖 ssh-agent/key），打印提示。
    """
    creds: Dict[str, Any] = {"user": user}
    if key:
        creds["key_path"] = key
        if key_passphrase:
            vault_file = _store_password(host, key_passphrase, "key-passphrase")
            creds["key_passphrase_file"] = str(_make_askpass_script(vault_file))
    if password:
        vault_file = _store_password(host, password)
        creds["askpass_file"] = str(_make_askpass_script(vault_file))
    if sudo_password:
        vault_file = _store_password(host, sudo_password, "sudo")
        creds["sudo_password_file"] = str(_make_askpass_script(vault_file))
    if key and (password or sudo_password):
        return creds
    if key:
        return creds
    if password:
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
    if any("--sudo-password" in str(v) or "权限不足" in str(v) for v in probes.values()):
        print("提示：部分探测因权限不足失败，可加 --sudo-password 重试。")
    print("====================================================")


def _print_batch_summary(successes: List[Dict[str, Any]],
                         failures: List[Dict[str, Any]]) -> None:
    print("\n===== 批量发现汇总 =====")
    print(f"成功 {len(successes)} / 失败 {len(failures)}")
    for item in failures:
        print(f"  - {item['host']}: {item['reason']}")
    print("==========================")


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
        description="SSH 进主机自动发现 docker/k8s/端口/GPU（本机 localhost/省略 host 直接本地执行），生成 schema v0.2 拓扑片段，确认后落盘。",
    )
    parser.add_argument("-H", "--host", default=None,
                        help="目标主机 IP/主机名；支持逗号列表与 [3-8] 区间展开。省略或 localhost = 本机发现，无需凭据")
    parser.add_argument("--hosts", default=None,
                        help="主机列表文件（每行一个 host，忽略空白行/注释）")
    parser.add_argument("-e", "--env", required=True, help="目标环境（test/uat/prod/自定义）")
    parser.add_argument("-u", "--user", default=None,
                        help="SSH 用户（默认 root；本机发现忽略）")
    parser.add_argument("-k", "--key", default=None, help="SSH 私钥路径（默认走 ssh-agent）")
    parser.add_argument("--password", action="store_true",
                        help="交互提示输入 SSH 密码（不落 argv；凭据走安全注入）")
    parser.add_argument("--password-stdin", action="store_true",
                        help="从 stdin 读取 SSH 密码（管道场景；不落 argv）")
    parser.add_argument("--key-passphrase", action="store_true",
                        help="交互提示输入加密私钥 passphrase（需配合 --key）")
    parser.add_argument("--sudo-password", action="store_true",
                        help="交互提示输入 sudo 密码，用于 docker/kubectl 等提权探测")
    parser.add_argument("--skip-unidentified", action="store_true",
                        help="跳过 ss 端口扫描生成的 unidentified 服务")
    parser.add_argument("--dry-run", action="store_true", help="只展示发现结果，不落盘")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 host 条目")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认（配合 --force 可非交互落盘）")
    args = parser.parse_args(argv)

    host_candidates: List[str] = []
    if args.host:
        host_candidates.append(args.host)
    if args.hosts:
        try:
            host_candidates.extend(_read_hosts_file(args.hosts))
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2
    try:
        hosts = _expand_hosts(host_candidates)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    if not hosts:
        if args.host or args.hosts:
            print("✗ 未解析到任何目标主机", file=sys.stderr)
            return 2
        # 批次十三 C4：host 省略 → 本机发现（WSL/单机无 sshd 也能跑）。
        hosts = ["localhost"]

    home = _active_home()
    is_local = _is_local_host(hosts[0])
    user = args.user or "root"

    password: Optional[str] = None
    if args.password and args.password_stdin:
        print("✗ --password 与 --password-stdin 不能同时使用", file=sys.stderr)
        return 2
    if is_local and (args.password or args.password_stdin):
        print("· 本机发现（localhost）不走 SSH：--password/--password-stdin 忽略，无需凭据。")
    elif args.password:
        password = getpass.getpass(
            f"SSH 密码（{user}@{hosts[0]}，仅本次使用，安全存储不回显）："
        )
    elif args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            print("✗ --password-stdin 未读到密码", file=sys.stderr)
            return 2

    key_passphrase: Optional[str] = None
    if args.key_passphrase:
        if not args.key:
            print("✗ --key-passphrase 需要配合 --key 使用", file=sys.stderr)
            return 2
        if is_local:
            print("· 本机发现（localhost）不走 SSH：--key/--key-passphrase 忽略。")
        else:
            key_passphrase = getpass.getpass("SSH 私钥 passphrase（安全存储不回显）：")

    sudo_password: Optional[str] = None
    if args.sudo_password:
        sudo_password = getpass.getpass("sudo 密码（探测命令提权，安全存储不回显）：")

    credential_kwargs: Dict[str, Any] = {}
    if password is not None:
        credential_kwargs["password"] = password
    if key_passphrase is not None:
        credential_kwargs["key_passphrase"] = key_passphrase
    if sudo_password is not None:
        credential_kwargs["sudo_password"] = sudo_password
    if is_local:
        # 本机发现无需 SSH 凭据；sudo 密码仍收集（本地 sudo -S 注入）。
        creds: Dict[str, Any] = {}
        if sudo_password is not None:
            vault_file = _store_password(hosts[0], sudo_password, "sudo")
            creds["sudo_password_file"] = str(_make_askpass_script(vault_file))
    else:
        creds = _prompt_credentials(hosts[0], user, args.key, **credential_kwargs)

    successes: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for host in hosts:
        try:
            discovery = discover_host(
                host, args.env, creds, skip_unidentified=args.skip_unidentified
            )
            successes.append({"host": host, "discovery": discovery})
            _print_summary(discovery)
        except DiscoveryError as exc:
            failures.append({"host": host, "reason": str(exc)})
            print(f"✗ {host} 发现失败：{exc}", file=sys.stderr)

    if len(hosts) > 1:
        _print_batch_summary(successes, failures)
    if not successes:
        return 1

    if args.dry_run:
        print("\n· --dry-run：未写入任何文件。")
        return 0

    if not _confirm(args.force, args.yes):
        print("\n· 已取消，未写入任何文件（可用 --dry-run 预览，--yes 跳过确认）。")
        return 0

    write_failures: List[Dict[str, Any]] = []
    written_paths: List[str] = []
    for item in successes:
        try:
            result = write_discovery(home, item["discovery"], force=args.force)
            for path in result["written"]:
                if path not in written_paths:
                    written_paths.append(path)
        except DiscoveryError as exc:
            write_failures.append({"host": item["host"], "reason": str(exc)})
            print(f"✗ {item['host']} 落盘失败：{exc}", file=sys.stderr)

    print("\n· 已写入：")
    for path in written_paths:
        print(f"    {home / path}")
    print("\n· 提示：发现结果只是草案（全部 needs_review=true），未经确认不参与权限判定。"
          "请按三步完成 review：")
    print("    1. 查看：vigil topo query（或会话内 topo_query）查看全部待审实体（needs_review=true）")
    print("    2. 确认：对每个实体用 topo_update 修正名称/类型/endpoint，确认无误后置 needs_review=false")
    print("    3. 效果：全部确认后实体进入权威拓扑，runbook 可按其绑定")
    return 1 if write_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

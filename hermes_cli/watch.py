"""``vigil watch`` —— 值守采集服务管理（OPS-DELTA #9 第一层）。

systemd --user 常驻服务（服务名固定 ``vigil-watch.service``，与 upstream
Vigil 的 ``hermes-gateway.service`` 无任何关联，硬约束 7）跑拉模式巡检
（每 5 分钟拉 alertmanager → 有告警写 ``~/.vigil/watch/inbox/``）。采集是
确定性代码；分析播报由 agent 会话消费 inbox（tools/watch_tools.py）。

子命令：
  install    — 生成 systemd --user unit 并 enable --now（开机自启）；
  uninstall  — stop + disable + 删 unit；
  status     — 真实服务状态（active/inactive）+ 上次采集时间 + inbox 未处理数，
               不报假健康（#12 缺陷 4 的核心教训）。

平台守卫：不支持 systemd --user（macOS/Windows/WSL 无 user session）时
install 返回明确错误并提示替代方案，不假装成功。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from tools.watch_collect import (
    _iter_inbox,
    inbox_dir,
    last_collect_path,
)

# 服务名显式固定（不依赖 get_service_name 的 hash 逻辑）。
SERVICE_NAME = "vigil-watch.service"
SERVICE_BASE = SERVICE_NAME[: -len(".service")]
_COLLECT_LOOP_MODULE = "hermes_cli.watch_collect_loop"


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME


def _supports_systemd_user() -> bool:
    try:
        from hermes_cli.gateway import supports_systemd_services
        return supports_systemd_services()
    except Exception:
        return False


def _systemctl(args: List[str]) -> subprocess.CompletedProcess:
    """Run ``systemctl --user <args>``; never raises (status paths must not)."""
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess(
            ["systemctl", "--user", *args], returncode=1, stdout="", stderr=str(exc)
        )


def _render_unit(home: Path, python: str) -> str:
    """Render the vigil-watch systemd --user unit. ``python`` = venv 解释器路径。"""
    return f"""[Unit]
Description=Vigil watch collector (alert polling)
After=network-online.target

[Service]
Type=simple
Environment=VIGIL_HOME={home}
ExecStart={python} -m {_COLLECT_LOOP_MODULE}
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""


def watch_install() -> int:
    """Install the vigil-watch systemd --user service (enable --now)."""
    if not _supports_systemd_user():
        print(
            "✗ 当前平台不支持 systemd --user（macOS/Windows/WSL 无 user session）。\n"
            "  WSL 可改用 cron（vigil cron）或常驻终端跑 `vigil watch`；"
            "不假装成功。",
            file=sys.stderr,
        )
        return 2

    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home()).resolve()
    python = sys.executable
    text = _render_unit(home, python)
    path = unit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
    except OSError as exc:
        print(f"✗ 写 unit 文件失败 {path}: {exc}", file=sys.stderr)
        return 1

    daemon = _systemctl(["daemon-reload"])
    enabled = _systemctl(["enable", "--now", SERVICE_NAME])
    if enabled.returncode != 0:
        print(
            f"✗ systemctl enable --now {SERVICE_NAME} 失败："
            f"{enabled.stderr.strip() or enabled.stdout.strip() or 'unknown'}",
            file=sys.stderr,
        )
        return 1

    print(f"✓ vigil-watch.service 已安装并启动（{path}）")
    print("  服务名固定为 vigil-watch.service，与 upstream hermes-gateway 无关联。")
    print(
        "  下一步：config.yaml 置 ops.watch.enabled: true 并配置 "
        "ops.prometheus.alertmanager，采集才会开始（当前零影响）。"
    )
    print(f"  查看状态：vigil watch status    查看日志：journalctl --user -u {SERVICE_NAME} -f")
    return 0


def watch_uninstall() -> int:
    """Stop, disable, and remove the vigil-watch systemd --user unit."""
    path = unit_path()
    _systemctl(["stop", SERVICE_NAME])
    _systemctl(["disable", SERVICE_NAME])
    _systemctl(["daemon-reload"])
    removed = False
    try:
        if path.exists():
            path.unlink()
            removed = True
    except OSError as exc:
        print(f"✗ 删除 unit 失败 {path}: {exc}", file=sys.stderr)
        return 1
    if not removed:
        print(f"vigil-watch.service 未安装（{path} 不存在）。")
        return 0
    print("✓ vigil-watch.service 已停止、禁用并删除。")
    print("  （inbox/errors.log 数据保留，重装后继续消费。）")
    return 0


def _service_is_active() -> Optional[bool]:
    """True=active，False=inactive/failed，None=未安装或无法判定（不含假健康）。"""
    if not unit_path().exists():
        return None
    if not _supports_systemd_user():
        return None
    result = _systemctl(["is-active", SERVICE_NAME])
    return result.returncode == 0 and result.stdout.strip() == "active"


def watch_service_active() -> Optional[bool]:
    """供 ``vigil cron status`` 判定采集服务是否在跑（best-effort，不抛）。"""
    try:
        return _service_is_active()
    except Exception:
        return None


def _last_collect_display() -> str:
    try:
        raw = last_collect_path().read_text(encoding="utf-8").strip()
        return raw if raw else "never"
    except OSError:
        return "never"


def _unprocessed_count() -> int:
    count = 0
    for path in _iter_inbox():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not data.get("processed"):
                count += 1
        except Exception:
            continue
    return count


def watch_status() -> int:
    """真实状态输出：systemd 服务状态 + 上次采集 + inbox 未处理数。"""
    path = unit_path()
    active = _service_is_active()
    if active is None:
        if not path.exists():
            print(f"vigil-watch.service 未安装（{path} 不存在）。")
            print("  安装：vigil watch install")
        else:
            print(f"vigil-watch.service 已安装但无法判定 systemd 状态（{path}）。")
    elif active:
        print(f"✓ vigil-watch.service 运行中（active）")
    else:
        print(f"⚠ vigil-watch.service 未运行（inactive/failed）——告警不会自动采集。")
        print("  启动：vigil watch install   或   systemctl --user start vigil-watch.service")
        print("  日志：journalctl --user -u vigil-watch.service")

    print(f"  上次采集: {_last_collect_display()}")
    print(f"  inbox 未处理: {_unprocessed_count()} 条")
    return 0


def watch_command(args) -> int:
    """Dispatch ``vigil watch <install|uninstall|status>``."""
    action = getattr(args, "watch_command", None) or "status"
    if action == "install":
        return watch_install()
    if action == "uninstall":
        return watch_uninstall()
    return watch_status()

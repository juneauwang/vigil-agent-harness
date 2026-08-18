"""``vigil dashboard install / uninstall / status`` —— systemd 常驻产品化（批三十六）。

常驻 dashboard 之前靠手写 systemd unit（2026-08-16 运维手动做）；本模块让
中间市场用户一条命令自动创建/卸载/查看，不再手写 unit：

  - ``install [--port N]``：检测 systemd user 会话可用（不可用 → 明确报错不
    安装）；解析当前解释器（``sys.executable`` 优先，回退 ``shutil.which("vigil")``）
    写 unit ExecStart（``-m hermes_cli.main dashboard --no-open --skip-build
    --port N``）；``Environment=HOME=<当前用户 home>`` 必写（防数据根错位）；
    daemon-reload → enable → start → 打印访问 URL。重复 install = 覆盖旧 unit
    （先停再写），``--port`` 变更即生效。
  - ``uninstall``：stop + disable + 删 unit 文件（幂等：不存在也成功）。
  - ``status``：unit 摘要（active/exited + 是否 enabled + URL/端口）。

unit 名与用户手写版一致（``vigil-dashboard.service``）：install 覆盖时行为
明确（先停旧服务再写新内容，绝不悄悄并存两个 dashboard 抢占端口）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

# Re-exported config helpers as module attributes so tests can monkeypatch
# the port-resolution source without importing hermes_cli.config directly.
def load_config_readonly():
    from hermes_cli.config import load_config_readonly as _lr
    return _lr()


def load_config():
    from hermes_cli.config import load_config as _lc
    return _lc()


def save_config(cfg):
    from hermes_cli.config import save_config as _sc
    return _sc(cfg)

UNIT_NAME = "vigil-dashboard.service"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_PATH = UNIT_DIR / UNIT_NAME

_DEFAULT_PORT = 9119


def _resolve_dashboard_port(
    args_port: Optional[int] = None,
    *,
    fallback_to_unit: bool = True,
) -> int:
    """单点解析 dashboard 端口：显式 --port > config dashboard.port > 默认。

    ``config dashboard.port`` 是行为配置的唯一事实来源（OPS-DELTA 批次四十三
    §BF）；systemd unit 只是执行载体。install/start/restart 全部走这里：
      - ``install`` 用解析值写 config dashboard.port + unit ExecStart --port
        （两处一致，restart 继承"上次用的端口"）；
      - ``restart`` 无显式 --port 时优先 config dashboard.port；config 也无时
        回退解析已写 unit 的 ExecStart 端口（_unit_port），保证继承已装端口。
    """
    port = int(args_port) if args_port else None
    if port is None:
        try:
            cfg = load_config_readonly()
            cfg_port = (cfg.get("dashboard") or {}).get("port")
            if cfg_port is not None:
                port = int(cfg_port)
        except Exception:
            port = None
    if port is None and fallback_to_unit:
        port = _unit_port()
    if port is None:
        port = _DEFAULT_PORT
    return int(port)


def _unit_content(port: int, python: str, home: str) -> str:
    """生成 unit 内容（对照已验证模板：真实 python 路径 + HOME + 端口参数化）。"""
    return (
        "[Unit]\n"
        "Description=Vigil Dashboard - Web UI\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"Environment=HOME={home}\n"
        f"ExecStart={python} -m hermes_cli.main dashboard --no-open --skip-build --port {int(port)}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _resolve_launcher() -> Tuple[str, bool]:
    """解析 unit ExecStart 的解释器路径。

    优先 ``sys.executable``（venv 绝对路径，systemd 精简环境也能跑）；
    不可用/为空 → 回退 ``shutil.which("vigil")``（binary 形态）。返回
    ``(launcher, is_binary)``：is_binary=True 时 ExecStart 直接用 binary
    子命令形态，不再 ``-m hermes_cli.main``。
    """
    exe = getattr(sys, "executable", None)
    if exe and str(exe).strip() and Path(str(exe)).is_file():
        return str(exe), False
    vig = shutil.which("vigil")
    if vig:
        return str(vig), True
    # 兜底：裸 python（PATH 靠 systemd 默认环境，能跑但依赖 PATH）。
    return "python", False


def _unit_exec_start(python: str, port: int, is_binary: bool) -> str:
    if is_binary:
        return f"{python} dashboard --no-open --skip-build --port {int(port)}"
    return f"{python} -m hermes_cli.main dashboard --no-open --skip-build --port {int(port)}"


def _systemctl(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    """``systemctl --user <args>``（可被测试 monkeypatch）。"""
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _systemd_user_available() -> Tuple[bool, str]:
    """探测 systemd user 会话：``systemctl --user show-environment`` 可执行。"""
    if sys.platform == "win32":
        return False, "当前环境无 systemd user 会话，无法常驻"
    try:
        r = _systemctl("show-environment", timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False, "当前环境无 systemd user 会话，无法常驻"
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        if detail:
            detail = f"（{detail.splitlines()[-1][:120]}）"
        return False, f"当前环境无 systemd user 会话，无法常驻{detail}"
    return True, ""


def _is_active() -> bool:
    try:
        r = _systemctl("is-active", UNIT_NAME)
        return (r.stdout or "").strip() == "active"
    except Exception:
        return False


def cmd_dashboard_install(args) -> int:
    """安装/覆盖常驻 dashboard unit（systemd user 会话）。"""
    port = _resolve_dashboard_port(getattr(args, "port", None))
    ok, why = _systemd_user_available()
    if not ok:
        print(why)
        return 1

    launcher, is_binary = _resolve_launcher()
    exec_start = _unit_exec_start(launcher, port, is_binary)
    home = str(Path.home())
    content = _unit_content(port, launcher, home)

    # 覆盖语义明确：先停旧服务（若在跑），再写新 unit，--port 变更即生效。
    if _is_active():
        try:
            _systemctl("stop", UNIT_NAME, timeout=60)
            print(f"    ✓ 已停止旧 {UNIT_NAME}（覆盖安装）")
        except Exception as exc:
            print(f"    ✗ 停止旧 {UNIT_NAME} 失败：{exc}")
            return 1

    try:
        UNIT_DIR.mkdir(parents=True, exist_ok=True)
        UNIT_PATH.write_text(content, encoding="utf-8")
    except OSError as exc:
        print(f"✗ 无法写入 unit 文件 {UNIT_PATH}：{exc}")
        return 1
    print(f"✓ 已写入 {UNIT_PATH}")

    for step, args in (
        ("daemon-reload", ("daemon-reload",)),
        ("enable", ("enable", UNIT_NAME)),
        ("start", ("start", UNIT_NAME)),
    ):
        try:
            r = _systemctl(*args, timeout=60)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            print(f"✗ {step} 失败：{exc}")
            return 1
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()
            print(f"✗ {step} 失败：{detail or 'unknown error'}")
            return 1
        print(f"    ✓ {step}")

    # batch 43 §BF: persist the resolved port into config.yaml as
    # dashboard.port so `vigil dashboard restart` (no --port) inherits the
    # exact port the systemd unit runs on — the config is the single source
    # of truth; the unit's ExecStart is just the execution carrier. Best-effort
    # and non-fatal: a read-only home must not block install from writing the
    # unit it was explicitly asked for.
    try:
        cfg = load_config()
        if not isinstance(cfg.get("dashboard"), dict):
            cfg["dashboard"] = {}
        cfg["dashboard"]["port"] = int(port)
        save_config(cfg)
    except Exception as exc:
        print(f"    ⚠ 未能写入 dashboard.port 到 config.yaml：{exc}")

    print(f"Dashboard 常驻服务已就绪：http://127.0.0.1:{port}")
    print(f"管理：vigil dashboard status | vigil dashboard uninstall")
    return 0


def cmd_dashboard_uninstall(args) -> int:
    """卸载常驻 dashboard unit（幂等：不存在也成功）。"""
    if not _systemd_user_available()[0]:
        # 无 systemd user 会话：文件级清理仍执行（幂等），不报错。
        print("当前环境无 systemd user 会话（跳过 systemctl，仅清理 unit 文件）")
    else:
        for step, args in (
            ("stop", ("stop", UNIT_NAME)),
            ("disable", ("disable", UNIT_NAME)),
        ):
            try:
                _systemctl(*args, timeout=60)
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass  # 幂等：不存在/已停都算成功
        try:
            _systemctl("daemon-reload", timeout=60)
        except Exception:
            pass

    existed = UNIT_PATH.exists()
    try:
        if existed:
            UNIT_PATH.unlink()
            print(f"✓ 已删除 {UNIT_PATH}")
        else:
            print(f"（{UNIT_PATH} 不存在，无需删除）")
    except OSError as exc:
        print(f"✗ 删除 {UNIT_PATH} 失败：{exc}")
        return 1
    print("Dashboard 常驻服务已卸载")
    return 0


def _unit_port() -> int:
    """从已写 unit 的 ExecStart 解析端口（缺省 _DEFAULT_PORT）。"""
    try:
        text = UNIT_PATH.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("ExecStart="):
                continue
            parts = line[len("ExecStart="):].split()
            for i, p in enumerate(parts):
                if p == "--port" and i + 1 < len(parts):
                    return int(parts[i + 1])
        return _DEFAULT_PORT
    except Exception:
        return _DEFAULT_PORT


def cmd_dashboard_status(args) -> int:
    """常驻 dashboard 状态摘要（active/exited + enabled + URL/端口）。"""
    if not UNIT_PATH.exists():
        print(f"未安装（无 {UNIT_NAME}；先运行 vigil dashboard install）")
        return 0
    active = "active" if _is_active() else "inactive"
    enabled = "?"
    try:
        r = _systemctl("is-enabled", UNIT_NAME)
        enabled = (r.stdout or "").strip() or "disabled"
    except Exception:
        pass
    port = _unit_port()
    print(f"unit:   {UNIT_NAME}")
    print(f"状态:   {active}（{'常驻运行中' if active == 'active' else '未运行'}）")
    print(f"开机自启: {enabled}")
    print(f"访问:   http://127.0.0.1:{port}")
    return 0

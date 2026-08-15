"""批次二十二 — /help 清理验收测试。

任务 1：用户可见 help 文案 ``~/.hermes`` → ``~/.vigil``（数据根语义不变，
        只改展示层文案；代码逻辑里的 .hermes 兼容引用保留）。
任务 2：``_EPILOGUE`` Examples 换运维场景（topo-discover/watch/vssh/config/setup），
        去掉上游模板（hermes-agent-dev / gateway install）。
任务 3：折叠命令可查性——``--help-all`` 同分组逐条展开 + 折叠行可操作提示 +
        ``_VIGIL_COMMANDS`` 覆盖 config/logs/status/doctor/sessions。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from hermes_cli._parser import _EPILOGUE, _VIGIL_COMMANDS

REPO_ROOT = Path(__file__).resolve().parents[2]

# 任务 1：每处用户可见 help/description 文案的旧串 → 断言旧串已消失、新串已就位。
# 只覆盖 help 文案；注释/运行时输出里的 .hermes 属"兼容语义该保留"，不在此列。
_TASK1_STRINGS: list[tuple[str, str, str]] = [
    # (相对路径, 旧串, 新串)
    ("hermes_cli/main.py",
     "Inspect / prune / clear ~/.hermes/checkpoints/",
     "Inspect / prune / clear ~/.vigil/checkpoints/"),
    ("hermes_cli/main.py",
     "instead of storing them in ~/.hermes/.env.",
     "instead of storing them in ~/.vigil/.env."),
    ("hermes_cli/_parser.py",
     "Ignore ~/.hermes/config.yaml and fall back to built-in defaults",
     "Ignore ~/.vigil/config.yaml and fall back to built-in defaults"),
    ("hermes_cli/subcommands/acp.py",
     "Install agent-browser + Playwright Chromium into ~/.hermes/node/",
     "Install agent-browser + Playwright Chromium into ~/.vigil/node/"),
    ("hermes_cli/subcommands/approvals.py",
     "default: ~/.hermes/state.db",
     "default: ~/.vigil/state.db"),
    ("hermes_cli/subcommands/claw.py",
     "zip snapshot of ~/.hermes/",
     "zip snapshot of ~/.vigil/"),
    ("hermes_cli/subcommands/claw.py",
     "written to ~/.hermes/backups/",
     "written to ~/.vigil/backups/"),
    ("hermes_cli/subcommands/webhook.py",
     "script under ~/.hermes/scripts/",
     "script under ~/.vigil/scripts/"),
    ("hermes_cli/subcommands/hooks.py",
     "declared in ~/.hermes/config.yaml",
     "declared in ~/.vigil/config.yaml"),
    ("hermes_cli/subcommands/hooks.py",
     "consent allowlist at ~/.hermes/shell-hooks-allowlist.json",
     "consent allowlist at ~/.vigil/shell-hooks-allowlist.json"),
    ("hermes_cli/subcommands/security.py",
     "~/.hermes/plugins/",
     "~/.vigil/plugins/"),
    ("hermes_cli/subcommands/skills.py",
     "sync manifest (~/.hermes/skills/.bundled_manifest)",
     "sync manifest (~/.vigil/skills/.bundled_manifest)"),
    ("hermes_cli/subcommands/cron.py",
     "Path to a script under ~/.hermes/scripts/",
     "Path to a script under ~/.vigil/scripts/"),
    ("hermes_cli/subcommands/dashboard.py",
     "HERMES_DASHBOARD_OAUTH_CLIENT_ID into ~/.hermes/.env",
     "HERMES_DASHBOARD_OAUTH_CLIENT_ID into ~/.vigil/.env"),
    ("hermes_cli/subcommands/gateway.py",
     "GATEWAY_RELAY_SECRET / GATEWAY_RELAY_DELIVERY_KEY into ~/.hermes/.env.",
     "GATEWAY_RELAY_SECRET / GATEWAY_RELAY_DELIVERY_KEY into ~/.vigil/.env."),
    ("hermes_cli/subcommands/gateway.py",
     "GATEWAY_RELAY_WAKE_URL in ~/.hermes/.env",
     "GATEWAY_RELAY_WAKE_URL in ~/.vigil/.env"),
    # curator：继承命令 help（--help-all 逐条展示），任务 1 全仓 grep 漏网收编。
    ("hermes_cli/curator.py",
     "snapshot of ~/.hermes/skills/",
     "snapshot of ~/.vigil/skills/"),
    ("hermes_cli/curator.py",
     "Restore ~/.hermes/skills/ from a curator snapshot",
     "Restore ~/.vigil/skills/ from a curator snapshot"),
]


def _run_cli(*argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("CI", None)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *argv],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=180,
    )


# ---------------------------------------------------------------------------
# 任务 1：~/.hermes → ~/.vigil 用户可见文案清理
# ---------------------------------------------------------------------------

def test_help_strings_no_longer_reference_hermes_home():
    """每个白名单文件的用户可见 help 文案旧串已消失、新串已就位。"""
    for rel, old, new in _TASK1_STRINGS:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert old not in text, f"{rel} 仍含旧串: {old!r}"
        assert new in text, f"{rel} 缺少新串: {new!r}"


def test_help_output_has_no_hermes_home():
    """行为探针：``vigil -h`` / ``vigil --help-all`` 输出都不含 ``~/.hermes``。"""
    for flag in ("-h", "--help-all"):
        proc = _run_cli(flag)
        assert proc.returncode == 0, proc.stderr
        assert "~/.hermes" not in proc.stdout, flag
        assert "~/.vigil" in proc.stdout, flag


# ---------------------------------------------------------------------------
# 任务 2：Examples 换运维场景
# ---------------------------------------------------------------------------

def test_epilogue_ops_examples():
    """_EPILOGUE 含运维场景，不含上游模板。"""
    assert "topo-discover -e prod -H 10.0.1.29" in _EPILOGUE
    assert "vigil vssh node1" in _EPILOGUE
    assert "vigil watch status" in _EPILOGUE
    assert "vigil config set model.default deepseek-v4-flash" in _EPILOGUE
    assert "vigil setup" in _EPILOGUE
    assert "hermes-agent-dev" not in _EPILOGUE
    assert "gateway install" not in _EPILOGUE


def test_help_output_examples_show_ops_scenarios():
    """行为探针：``vigil -h`` Examples 段含运维命令。"""
    proc = _run_cli("-h")
    assert proc.returncode == 0, proc.stderr
    assert "topo-discover -e prod -H 10.0.1.29" in proc.stdout
    assert "vigil vssh node1" in proc.stdout


# ---------------------------------------------------------------------------
# 任务 3：折叠命令可查性
# ---------------------------------------------------------------------------

def test_vigil_commands_cover_ops_frequent_inherited():
    """_VIGIL_COMMANDS 覆盖 config/logs/status（原有）与 doctor/sessions（新补）。"""
    for name in ("config", "logs", "status", "doctor", "sessions"):
        assert name in _VIGIL_COMMANDS, name


def test_collapsed_help_has_actionable_hint():
    """``-h`` 折叠行含 --help-all 提示 + 运维常用继承命令提示。"""
    proc = _run_cli("-h")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert re.search(r"继承命令（来自 hermes，\d+ 个）：", out)
    assert "完整列表与说明见：vigil --help-all" in out
    assert "运维常用继承命令：doctor / sessions / cron / skills" in out
    assert "Vigil 命令：" in out
    # 折叠行只列名字，不逐条展开继承命令说明。
    assert "Messaging gateway management" not in out


def test_help_all_grouped_and_expanded():
    """``--help-all`` 同分组逐条展开：Vigil 组在前，继承命令组标题 + 逐条行。"""
    proc = _run_cli("--help-all")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "Vigil 命令：" in out
    m = re.search(r"继承命令（来自 hermes，(\d+) 个）：", out)
    assert m, "缺少继承命令分组标题"
    assert int(m.group(1)) >= 50
    assert out.index("Vigil 命令：") < out.index("继承命令（来自 hermes")
    # 继承命令逐条展开（说明可见）。
    assert "Messaging gateway management" in out
    for name in ("gateway", "secrets", "egress", "cron"):
        assert re.search(rf"^\s+{re.escape(name)}\s", out, re.M), name
    # 别名/弃用命令在 --help-all 全量里也有说明（全量名副其实）。
    for name in ("gui", "learning", "memory-graph", "login"):
        assert re.search(rf"^\s+{re.escape(name)}\s", out, re.M), name

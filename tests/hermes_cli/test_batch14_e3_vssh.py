"""批次十四 E3 — ``vigil vssh`` 内置命令验收测试。

覆盖：凭据引用解析（拓扑 credential → ssh_key/secret/askpass）；密码/passphrase
经 SSH_ASKPASS 注入、明文不进 argv/env；port 缺省与 credential 覆盖；
无凭据回退 ssh-agent/交互；``run()`` execvpe 执行 ssh。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

from hermes_cli.subcommands import vssh as vssh_mod
from hermes_cli.subcommands.vssh import (
    _build_ssh_argv,
    _resolve_topology_credential,
    _split_hostspec,
    run,
)


# ---------------------------------------------------------------------------
# _build_ssh_argv
# ---------------------------------------------------------------------------

def test_build_argv_plain_no_credential():
    """无凭据：纯 ssh argv（key 显式给 → -i），不注入 SSH_ASKPASS。"""
    argv, env = _build_ssh_argv("h", user="root", key="/my/key")
    assert argv == ["ssh", "-o", "IdentitiesOnly=yes", "-p", "22", "-i", "/my/key", "root@h"]
    assert "SSH_ASKPASS" not in env


def test_build_argv_ssh_key_credential():
    """拓扑 ssh_key 引用 → -i 私钥路径（CLI key 优先于引用）。"""
    argv, _ = _build_ssh_argv("db1", user="root",
                              cred={"type": "ssh_key", "ref": "/keys/db.pem"})
    assert argv == ["ssh", "-o", "IdentitiesOnly=yes", "-p", "22", "-i", "/keys/db.pem", "root@db1"]
    argv, _ = _build_ssh_argv("db1", user="root", key="/cli/key",
                              cred={"type": "ssh_key", "ref": "/keys/db.pem"})
    assert argv == ["ssh", "-o", "IdentitiesOnly=yes", "-p", "22", "-i", "/cli/key", "root@db1"]


def test_build_argv_vault_credential_injects_askpass_no_plaintext():
    """secret 引用 → SSH_ASKPASS 脚本注入；凭据名/密码明文不进 argv/env。"""
    argv, env = _build_ssh_argv("db1", user="ops",
                                cred={"type": "secret", "ref": "db-pass"})
    assert argv == ["ssh", "-o", "IdentitiesOnly=yes", "-p", "22", "ops@db1"]
    assert env["SSH_ASKPASS_REQUIRE"] == "force"
    assert env["SSH_ASKPASS"]
    joined = " ".join(argv) + " " + " ".join(env.values())
    assert "db-pass" not in joined
    assert not any("db-pass" in v for v in env.values())


def test_build_argv_vault_failure_falls_back(monkeypatch):
    """secret 注入失败（askpass 构造异常）→ 回退无 SSH_ASKPASS 的普通 argv。"""
    def boom(_path):
        raise OSError("askpass write failed")
    monkeypatch.setattr(vssh_mod, "_make_askpass_script", boom)
    argv, env = _build_ssh_argv("db1", user="root",
                                cred={"type": "secret", "ref": "db-pass"})
    assert argv == ["ssh", "-o", "IdentitiesOnly=yes", "-p", "22", "root@db1"]
    assert "SSH_ASKPASS" not in env


def test_build_argv_askpass_credential():
    """askpass 引用 → 直接作为 SSH_ASKPASS 脚本路径。"""
    argv, env = _build_ssh_argv("h", user="root",
                                cred={"type": "askpass", "ref": "/tmp/ask.sh"})
    assert env["SSH_ASKPASS"] == "/tmp/ask.sh"
    assert env["SSH_ASKPASS_REQUIRE"] == "force"
    assert "root@h" in argv


def test_build_argv_credential_port_overrides_default():
    """credential 引用带 port → 覆盖 CLI 缺省 22。"""
    argv, _ = _build_ssh_argv("h", user="root",
                              cred={"type": "ssh_key", "ref": "/k", "port": 2222})
    assert argv[4] == "2222"


def test_build_argv_unknown_credential_type_ignored():
    """未知 credential type → 忽略（不注入、不报错）。"""
    argv, env = _build_ssh_argv("h", user="root",
                                cred={"type": "bogus", "ref": "x"})
    assert argv == ["ssh", "-o", "IdentitiesOnly=yes", "-p", "22", "root@h"]
    assert "SSH_ASKPASS" not in env


def test_build_argv_always_includes_identities_only():
    """批次十九 §AF：ssh argv 无条件带 ``-o IdentitiesOnly=yes``。

    多 key 环境 ``-i key`` 不等于"只用这个 key"——不显式声明会遍历 agent
    所有 key → MaxAuthTries 刷爆 → sshd 锁 15 分钟。所有形态（无凭据 / key /
    secret / askpass / credential port 覆盖）都必须带上。
    """
    argv, _ = _build_ssh_argv("h", user="root")
    assert argv[1:3] == ["-o", "IdentitiesOnly=yes"]
    argv, _ = _build_ssh_argv("h", user="root", key="/my/key")
    assert argv[1:3] == ["-o", "IdentitiesOnly=yes"]
    argv, _ = _build_ssh_argv("h", user="root",
                              cred={"type": "ssh_key", "ref": "/k", "port": 2222})
    assert argv[1:3] == ["-o", "IdentitiesOnly=yes"]
    assert argv[4] == "2222"
    argv, _ = _build_ssh_argv("db1", user="ops",
                              cred={"type": "secret", "ref": "db-pass"})
    assert argv[1:3] == ["-o", "IdentitiesOnly=yes"]


# ---------------------------------------------------------------------------
# _split_hostspec
# ---------------------------------------------------------------------------

def test_split_hostspec():
    assert _split_hostspec("alice@10.0.0.5") == ("alice", "10.0.0.5")
    assert _split_hostspec("10.0.0.5") == ("root", "10.0.0.5")
    # CLI -u 是缺省；hostspec 带 user@ 时优先。
    assert _split_hostspec("alice@10.0.0.5", user="bob") == ("alice", "10.0.0.5")
    assert _split_hostspec("10.0.0.5", user="bob") == ("bob", "10.0.0.5")
    assert _split_hostspec("@10.0.0.5", user="ops") == ("ops", "10.0.0.5")


# ---------------------------------------------------------------------------
# _resolve_topology_credential
# ---------------------------------------------------------------------------

@pytest.fixture
def topo_home(tmp_path, monkeypatch):
    (tmp_path / "topology.yaml").write_text(
        "version: 3\n"
        "hosts:\n"
        "  - name: db1\n"
        "    endpoint: 10.0.0.5\n"
        "    credential:\n"
        "      type: secret\n"
        "      ref: db-pass\n"
        "  - name: web1\n"
        "    endpoint: 10.0.0.6\n"
        "    credential:\n"
        "      type: ssh_key\n"
        "      ref: /keys/web.pem\n"
        "  - name: plain\n"
        "    endpoint: 10.0.0.7\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    return tmp_path


def test_resolve_topology_credential_by_name(topo_home):
    assert _resolve_topology_credential("db1") == {"type": "secret", "ref": "db-pass"}
    assert _resolve_topology_credential("web1") == {"type": "ssh_key", "ref": "/keys/web.pem"}


def test_resolve_topology_credential_by_endpoint(topo_home):
    assert _resolve_topology_credential("10.0.0.5") == {"type": "secret", "ref": "db-pass"}


def test_resolve_topology_credential_missing(topo_home):
    """无凭据引用 / 未知 host → None（回退 ssh-agent/交互）。"""
    assert _resolve_topology_credential("plain") is None
    assert _resolve_topology_credential("nope") is None


def test_resolve_topology_credential_plural_credentials(topo_home):
    """batch84（OPS-DELTA #100）：host 行只有复数 credentials（topo-discover
    v0.4 原生形态）→ 解析出 ssh_key 凭据（结构契约与单数一致）。"""
    (topo_home / "topology.yaml").write_text(
        "version: 4\n"
        "hosts:\n"
        "  - name: aliyun-1\n"
        "    endpoint: 39.106.217.32\n"
        "    credentials:\n"
        "      - type: ssh_key\n"
        "        ref: /home/wpwang/.ssh/aliyun_nopass.pem\n"
        "        user: root\n"
        "        port: 22\n",
        encoding="utf-8",
    )
    assert _resolve_topology_credential("aliyun-1") == {
        "type": "ssh_key",
        "ref": "/home/wpwang/.ssh/aliyun_nopass.pem",
        "user": "root",
        "port": 22,
    }
    # endpoint 匹配同样生效
    assert _resolve_topology_credential("39.106.217.32")["type"] == "ssh_key"


def test_resolve_topology_credential_plural_mixed_picks_ssh_key(topo_home):
    """batch84：复数数组 ssh_key/secret 混排 → ssh 场景取 type=ssh_key 那条。"""
    (topo_home / "topology.yaml").write_text(
        "version: 4\n"
        "hosts:\n"
        "  - name: mixed\n"
        "    endpoint: 10.0.0.9\n"
        "    credentials:\n"
        "      - type: secret\n"
        "        ref: db-pass\n"
        "      - type: ssh_key\n"
        "        ref: /keys/mixed.pem\n"
        "        user: root\n"
        "        port: 22\n",
        encoding="utf-8",
    )
    assert _resolve_topology_credential("mixed") == {
        "type": "ssh_key", "ref": "/keys/mixed.pem", "user": "root", "port": 22,
    }


def test_resolve_topology_credential_singular_still_wins(topo_home):
    """batch84 验收 b：单数 credential（老格式）优先——行为不变。"""
    (topo_home / "topology.yaml").write_text(
        "version: 4\n"
        "hosts:\n"
        "  - name: both\n"
        "    endpoint: 10.0.0.10\n"
        "    credential:\n"
        "      type: ssh_key\n"
        "      ref: /keys/single.pem\n"
        "      user: root\n"
        "    credentials:\n"
        "      - type: ssh_key\n"
        "        ref: /keys/plural.pem\n"
        "        user: root\n",
        encoding="utf-8",
    )
    assert _resolve_topology_credential("both") == {
        "type": "ssh_key", "ref": "/keys/single.pem", "user": "root",
    }


def test_resolve_topology_credential_plural_askpass_fallback(topo_home):
    """batch84：数组无 ssh_key（纯 askpass）→ 取第一条 dict 兜底（secret/askpass
    走同一消费契约）。"""
    (topo_home / "topology.yaml").write_text(
        "version: 4\n"
        "hosts:\n"
        "  - name: pw-host\n"
        "    endpoint: 10.0.0.11\n"
        "    credentials:\n"
        "      - type: askpass\n"
        "        ref: /keys/ask.sh\n",
        encoding="utf-8",
    )
    assert _resolve_topology_credential("pw-host") == {
        "type": "askpass", "ref": "/keys/ask.sh",
    }


def test_resolve_topology_credential_no_topology(tmp_path, monkeypatch):
    """无拓扑文件 / 坏 YAML → None（不抛错）。"""
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    assert _resolve_topology_credential("db1") is None
    (tmp_path / "topology.yaml").write_text("{{{{ bad", encoding="utf-8")
    assert _resolve_topology_credential("db1") is None


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------

def test_run_execs_ssh_with_credential(monkeypatch, topo_home):
    """带凭据 host：execvpe ssh，argv/env 无明文。"""
    captured = {}

    def fake_execvpe(name, argv, env):
        captured["name"] = name
        captured["argv"] = argv
        captured["env"] = env
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvpe", fake_execvpe)
    args = SimpleNamespace(hostspec="db1", user=None, port=None, key=None,
                           no_credential=False)
    with pytest.raises(SystemExit):
        run(args)
    assert captured["name"] == "ssh"
    assert captured["argv"][-1] == "root@db1"
    assert captured["env"]["SSH_ASKPASS_REQUIRE"] == "force"
    assert "db-pass" not in " ".join(captured["argv"])


def test_run_no_credential_falls_back_to_agent(monkeypatch, topo_home):
    """--no-credential：跳过拓扑读取，走 ssh-agent/交互（无 SSH_ASKPASS）。"""
    captured = {}

    def fake_execvpe(name, argv, env):
        captured["argv"] = argv
        captured["env"] = env
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvpe", fake_execvpe)
    args = SimpleNamespace(hostspec="db1", user=None, port=None, key=None,
                           no_credential=True)
    with pytest.raises(SystemExit):
        run(args)
    assert captured["argv"] == ["ssh", "-o", "IdentitiesOnly=yes", "-p", "22", "root@db1"]
    assert "SSH_ASKPASS" not in captured["env"]


def test_run_no_hostspec_lists_topology_hosts(monkeypatch, topo_home, capsys):
    """无参 → 列出拓扑表所有主机（name env 凭据状态）+ 用法提示，不执行 ssh。"""
    args = SimpleNamespace(hostspec=None, user=None, port=None, key=None,
                           no_credential=False)
    assert run(args) == 0
    out = capsys.readouterr().out
    # 三台主机都在，凭据状态只显示类型（不显示 ref/值）。
    assert "db1" in out and "✓secret" in out
    assert "web1" in out and "✓ssh_key" in out
    assert "plain" in out and "✗无凭据" in out
    assert "用法: vigil vssh <host> [user@host]" in out
    # fixture 无 env 字段 → 显示 "-"；凭据值（db-pass / 私钥路径）不进输出。
    assert "-" in out
    assert "db-pass" not in out
    assert "/keys/web.pem" not in out


def test_run_no_hostspec_empty_topology(tmp_path, monkeypatch, capsys):
    """无参 + 无拓扑数据 → 提示先 topo-discover，不执行 ssh。"""
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    args = SimpleNamespace(hostspec=None, user=None, port=None, key=None,
                           no_credential=False)
    assert run(args) == 0
    assert "拓扑表为空，先运行 vigil topo-discover 发现主机" in capsys.readouterr().out


def test_run_no_hostspec_never_execs_ssh(monkeypatch, topo_home):
    """无参列主机路径不碰 execvpe（不会被当成主机名执行 ssh）。"""
    def boom(*_a, **_kw):
        raise AssertionError("no-hostspec path must not exec ssh")
    monkeypatch.setattr(os, "execvpe", boom)
    args = SimpleNamespace(hostspec=None, user=None, port=None, key=None,
                           no_credential=False)
    assert run(args) == 0


def test_run_with_hostspec_behavior_unchanged(monkeypatch, topo_home):
    """有参（node1）→ 原行为：execvpe ssh，不列主机。"""
    captured = {}

    def fake_execvpe(name, argv, env):
        captured["argv"] = argv
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvpe", fake_execvpe)
    args = SimpleNamespace(hostspec="web1", user=None, port=None, key=None,
                           no_credential=False)
    with pytest.raises(SystemExit):
        run(args)
    assert captured["argv"][-1] == "root@web1"
    assert captured["argv"][:3] == ["ssh", "-o", "IdentitiesOnly=yes"]


def test_vssh_subcommand_registered():
    """``vigil vssh --help`` 可用：vssh 注册进顶层子命令，参数齐全。"""
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "vssh", "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert "常用运维命令第一块" in proc.stdout
    assert "secret" in proc.stdout and "SSH_ASKPASS" in proc.stdout
    assert "凭据引用" in proc.stdout and "无凭据回退 ssh-agent/交互" in proc.stdout
    for flag in ("-p", "--port", "-i", "--key", "-u", "--user", "--no-credential"):
        assert flag in proc.stdout


# ---------------------------------------------------------------------------
# 批次二十一 — vssh 凭据解析 fail-closed 对齐（任务 4：allow_fallback 双路径）
# ---------------------------------------------------------------------------

def test_resolve_topology_credential_allow_fallback_false_no_cred(topo_home):
    """allow_fallback=False 且无凭据 → None（调用方须 fail-closed，不 fallback）。"""
    assert _resolve_topology_credential("plain", allow_fallback=False) is None
    assert _resolve_topology_credential("nope", allow_fallback=False) is None
    # 默认（CLI 交互路径）语义不变。
    assert _resolve_topology_credential("plain") is None


def test_resolve_topology_credential_allow_fallback_false_with_cred(topo_home):
    """allow_fallback=False 且拓扑表有凭据引用 → 正常返回（凭据优先于 fallback）。"""
    assert _resolve_topology_credential("db1", allow_fallback=False) == {
        "type": "secret", "ref": "db-pass"}


def test_sudo_tool_resolves_with_allow_fallback_false(monkeypatch):
    """agent 工具路径（sudo_exec）解析凭据时传 allow_fallback=False。"""
    import tools.sudo_tool as sudo_tool

    seen = {}
    monkeypatch.setattr(sudo_tool, "check_ops_command_permission", lambda *a, **k: None)

    def fake_resolve(host, **kw):
        seen["kwargs"] = kw
        return None

    monkeypatch.setattr(sudo_tool, "_resolve_topology_credential", fake_resolve)
    # 实际调用：凭据缺失 → fail-closed 报错，且 kwargs 带 allow_fallback=False。
    from tools.sudo_tool import _sudo_exec_handler as handler
    result = handler({"host": "prod1", "command": "ss -tlnp", "env": "prod"})
    assert seen["kwargs"] == {"allow_fallback": False}
    assert "缺少 sudo 凭据" in result
    assert "禁止翻 ~/.ssh/" in result

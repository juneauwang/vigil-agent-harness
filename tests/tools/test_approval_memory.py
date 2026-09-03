"""batch86 任务 1+2 —— 命令级自进化白名单存储层 + 归一化 + 信号（OPS-DELTA #102）。

覆盖验收：a 归一化同模板（lscpu/lscpu -e/sudo lscpu；cat /etc/hosts →
cat）；b 动态参数拒绝沉淀（kubectl apply -f / curl URL）；c 黑名单形态永不
沉淀（rm -rf / 重定向）；d 存储读写落盘一致 + 损坏 fail-safe；e 批准计数
3 → active；f 拒绝一次 → banned 永不自动沉淀；g user_opt_in → 立即沉淀。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_cli.config as hc
from tools.approval_memory import (
    forget,
    get_entry,
    is_active,
    list_rows,
    memory_path,
    normalize_template,
    note_user_decision,
    record_denial,
    record_success,
    seed_templates,
)


@pytest.fixture
def mem_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    from tools import approval_memory as _m
    _m._read_cache.clear()
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()


# ---------------------------------------------------------------------------
# a — 归一化：同模板 / 种子只读操作数 / 布尔 flag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,expected", [
    ("lscpu", "lscpu"),
    ("lscpu -e", "lscpu"),
    ("sudo lscpu", "lscpu"),
    ("sudo -i lscpu", "lscpu"),
    ("sudo -u root lscpu", "lscpu"),
    ("free -h", "free"),
    ("cat /etc/hosts", "cat"),
    ("cat /etc/passwd", "cat"),
    ("cat -n /etc/hosts", None),  # flag+操作数 = 保守不命中（v0.1 边界，仍走判定）
    ("grep -r foo /etc", None),   # 同上（布尔 flag 后带操作数 → 不命中）
    ("ps aux", "ps"),
    ("ls -la", "ls"),
    ("df /", "df"),
])
def test_normalize_same_template(cmd, expected):
    assert normalize_template(cmd) == expected


# ---------------------------------------------------------------------------
# b — 带动态参数 → 拒绝沉淀（永远走判定）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "kubectl apply -f deploy.yaml",
    "kubectl delete ns foo",
    "curl https://example.com/x",
    "curl -s http://example.com/x",
    "ssh root@1.2.3.4 'df -h'",
    "tar czf backup.tgz /etc",
    "git push origin main",
    "python /tmp/script.py",
    "ls --color=auto /etc",
    "FOO=bar env ls",
])
def test_dynamic_args_never_normalize(cmd):
    assert normalize_template(cmd) is None


# ---------------------------------------------------------------------------
# c — 黑名单形态永不沉淀（即使成功 N 次）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "rm -rf /tmp/x",
    "rm file.txt",
    "dd if=/dev/zero of=/dev/sda",
    "mkfs.ext4 /dev/sdb1",
    "echo x > /etc/y",
    "echo x >> /etc/y",
    "tee /etc/x",
    "shred -u /etc/x",
    "find / -delete",
    "ls | grep foo",
    "a && b",
    "a ; b",
    "cat /etc/hosts | ssh root@1.2.3.4",
])
def test_blacklist_shapes_never_normalize(cmd):
    assert normalize_template(cmd) is None


# ---------------------------------------------------------------------------
# d — 存储读写落盘一致 + 损坏 fail-safe
# ---------------------------------------------------------------------------

def test_storage_roundtrip_and_audit_fields(mem_home):
    assert not memory_path().is_file()  # 默认不存在 = 空白名单
    record_success("lsblk", source_task="sess-1")
    record_success("lsblk", source_task="sess-1")
    record_success("lsblk", source_task="sess-1")
    path = memory_path()
    assert path.is_file()
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    entry = next(e for e in data["entries"] if e["template"] == "lsblk")
    assert entry["success_count"] == 3
    assert entry["status"] == "active"
    assert entry["source_task"] == "sess-1"
    assert entry["approved_at"]  # 沉淀时间（红线 3）
    assert entry["never_denied"] is True
    # 读回一致
    loaded = get_entry("lsblk")
    assert loaded["success_count"] == 3 and loaded["status"] == "active"
    # 撤销 → 落盘 inactive、计数清零
    assert forget("lsblk") is True
    assert get_entry("lsblk")["status"] == "inactive"
    assert get_entry("lsblk")["success_count"] == 0


def test_storage_corrupt_failsafe(mem_home):
    """文件损坏 → fail-safe（空白名单不崩；种子仍 code-level active）。"""
    memory_path().write_text("{ broken yaml [", encoding="utf-8")
    from tools.approval_memory import command_template_approved
    assert command_template_approved("sudo lsblk") is False
    assert is_active("lscpu") is True  # 种子不受文件损坏影响
    # 修复后继续可用
    record_success("lsblk")
    assert get_entry("lsblk")["success_count"] == 1


def test_forget_unknown_returns_false(mem_home):
    assert forget("no-such-template") is False


# ---------------------------------------------------------------------------
# e — 批准 3 次 → active（第 3 次起跳过审批）
# ---------------------------------------------------------------------------

def test_three_approvals_activate(mem_home):
    assert not is_active("lsblk")
    for i in range(1, 4):
        entry = record_success("lsblk", source_task=f"s{i}")
        if i < 3:
            assert entry["status"] == "pending" and not is_active("lsblk")
    assert is_active("lsblk")
    assert get_entry("lsblk")["success_count"] == 3
    assert get_entry("lsblk")["approved_at"]


# ---------------------------------------------------------------------------
# f — 拒绝一次 → banned，永不自动沉淀
# ---------------------------------------------------------------------------

def test_denied_once_banned_forever(mem_home):
    record_success("newtool")
    record_success("newtool")
    entry = record_denial("newtool")
    assert entry["status"] == "banned" and entry["never_denied"] is False
    # 之后成功 N 次也不沉淀（record_success 对 banned 是 no-op）
    for _ in range(5):
        record_success("newtool")
    assert get_entry("newtool")["status"] == "banned"
    assert not is_active("newtool")
    # 解禁只允许 forget 显式操作
    assert forget("newtool") is True
    assert get_entry("newtool")["status"] == "inactive"
    assert not is_active("newtool")
    # forget 后重新积累可再沉淀（不是自动复活）
    for _ in range(3):
        record_success("newtool")
    assert is_active("newtool")


# ---------------------------------------------------------------------------
# g — user_opt_in=true → 立即沉淀（1 次即可，不等 3 次）
# ---------------------------------------------------------------------------

def test_user_opt_in_immediate(mem_home):
    entry = record_success("nvidia-smi", user_opt_in=True, source_task="opt")
    assert entry["status"] == "active"
    assert entry["user_opt_in"] is True
    assert entry["success_count"] == 1
    assert is_active("nvidia-smi")


def test_user_opt_in_does_not_unban(mem_home):
    """opt-in 不强于拒绝（红线 1：被拒过的命令只有 forget 能解除）。"""
    record_denial("curl-x")
    record_success("curl-x", user_opt_in=True)
    assert get_entry("curl-x")["status"] == "banned"


# ---------------------------------------------------------------------------
# 种子表：默认 active、不走计数；note 拒绝可覆盖种子；forget 可撤销种子
# ---------------------------------------------------------------------------

def test_seed_defaults_active(mem_home):
    for seed in ("lscpu", "free", "cat", "ls", "df", "ps", "grep", "wc"):
        assert is_active(seed)
    assert len(seed_templates()) == 18
    rows = list_rows()
    seed_rows = [r for r in rows if r["source_task"] == "seed"]
    assert len(seed_rows) == 18
    assert all(r["status"] == "active" for r in seed_rows)


def test_seed_denial_override_and_forget(mem_home):
    assert is_active("cat")
    note_user_decision("sudo cat /etc/hosts", "denied")
    assert not is_active("cat")  # 拒绝覆盖种子默认 active
    assert get_entry("cat")["status"] == "banned"
    assert forget("cat") is True
    assert not is_active("cat")  # forget 后种子回到弹审批
    for _ in range(3):
        record_success("cat")
    assert is_active("cat")  # 重新批准 3 次可再沉淀


def test_ineligible_shapes_record_nothing(mem_home):
    """不可沉淀形态：批准/拒绝都不记信号（这类命令本来就不进白名单）。"""
    note_user_decision("kubectl delete ns foo", "denied")
    note_user_decision("rm -rf /tmp/x", "approved")
    note_user_decision("curl https://example.com/x", "approved")
    note_user_decision("a | b", "denied")
    assert memory_path().is_file() is False  # 没有任何条目落盘

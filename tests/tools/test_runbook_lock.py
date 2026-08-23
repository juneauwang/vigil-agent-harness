"""批八十一：runbook 执行级并发锁——注册/释放/同名冲突/目标重叠/过期兜底。"""

from __future__ import annotations

import pytest

from tools import runbook_lock as rl


@pytest.fixture(autouse=True)
def _clean_registry():
    """进程级锁注册表隔离：每例前后清空，避免跨文件残留。"""
    with rl._lock:
        rl._registry.clear()
    yield
    with rl._lock:
        rl._registry.clear()


def _reset():
    with rl._lock:
        rl._registry.clear()


def test_acquire_release():
    _reset()
    assert rl.try_acquire(runbook="a", exec_id="e1", targets={"t1"}, env="local") is None
    locks = rl.list_locks()
    assert len(locks) == 1
    assert locks[0]["runbook"] == "a"
    assert locks[0]["exec_id"] == "e1"
    assert locks[0]["targets"] == ["t1"]
    rl.release("e1")
    assert rl.list_locks() == []


def test_same_runbook_conflict():
    _reset()
    assert rl.try_acquire(runbook="a", exec_id="e1", targets={"t1"}) is None
    err = rl.try_acquire(runbook="a", exec_id="e2", targets={"t9"})
    assert err is not None
    assert "同名 runbook 已在执行" in err
    assert "exec_id=e1" in err
    assert "锁定中" in err


def test_target_overlap_conflict():
    _reset()
    assert rl.try_acquire(runbook="a", exec_id="e1", targets={"t1", "t2"}) is None
    err = rl.try_acquire(runbook="b", exec_id="e2", targets={"t2"})
    assert err is not None
    assert "目标与进行中执行重叠" in err
    assert "t2" in err
    # 不重叠 target → 放行
    assert rl.try_acquire(runbook="b", exec_id="e3", targets={"t3"}) is None


def test_self_reentry_same_exec_id():
    _reset()
    assert rl.try_acquire(runbook="a", exec_id="e1", targets={"t1"}) is None
    # 同一 exec_id 再注册（web 预检后引擎正式注册）→ 视为自身不判冲突
    assert rl.try_acquire(runbook="a", exec_id="e1", targets={"t1"}) is None


def test_peek_conflict_readonly():
    _reset()
    assert rl.try_acquire(runbook="a", exec_id="e1", targets={"t1"}) is None
    assert rl.peek_conflict(runbook="a") is not None
    assert rl.peek_conflict(runbook="b", targets={"t1"}) is not None
    assert rl.peek_conflict(runbook="b", targets={"t2"}) is None
    # peek 不注册
    assert len(rl.list_locks()) == 1


def test_ttl_expiry_releases_stale_lock():
    _reset()
    assert rl.try_acquire(runbook="a", exec_id="e1", targets={"t1"}) is None
    with rl._lock:
        rl._registry["e1"]["started_at"] = rl._now() - rl._LOCK_TTL_S - 1
    # 过期锁不再阻塞；惰性 prune 已清 e1，只剩新注册 e2
    assert rl.try_acquire(runbook="a", exec_id="e2", targets={"t1"}) is None
    locks = rl.list_locks()
    assert [l["exec_id"] for l in locks] == ["e2"]

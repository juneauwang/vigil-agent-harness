"""批二十八：统一 pending 审批注册表（tools/approval.py 新增段，OPS-DELTA #47）。

只测新增注册表原语：注册/查询/列表/批准/拒绝/等待/超时回收/scope 校验。审批
侧效应（allowlist 持久化 + trajectory）由既有审批流原生完成，不在此重复测试。
"""

from __future__ import annotations

import time

import pytest

from tools.approval import (
    approve_web_approval,
    clear_web_approvals,
    deny_web_approval,
    get_web_approval,
    list_web_approvals,
    reclaim_timed_out_web_approvals,
    register_web_approval,
    wait_web_approval,
)

_CONTRACT_KEYS = {
    "id", "command", "description", "env", "grade", "action", "session_key", "source",
    "status", "scope", "created_at", "timeout_at",
    "allow_session", "allow_permanent",
}
_INTERNAL_KEYS = {
    "_event", "_choice", "_timeout_epoch", "primary_key", "pattern_keys",
    "exec_id", "smart_denied", "reason",
}


@pytest.fixture(autouse=True)
def _clean():
    clear_web_approvals()
    yield
    clear_web_approvals()


def _register(**kw) -> str:
    base = dict(
        command="kubectl delete pod x",
        description="prod 变更确认门（B'）",
        env="prod",
        grade="L3",
        session_key="sess_1",
        source="web",
    )
    base.update(kw)
    return register_web_approval(**base)


def test_register_view_exposes_only_contract_fields():
    aid = _register(action="restart")
    view = get_web_approval(aid)
    assert view is not None
    assert set(view) == _CONTRACT_KEYS
    assert view["id"] == aid
    assert view["command"] == "kubectl delete pod x"
    assert view["description"] == "prod 变更确认门（B'）"
    assert view["env"] == "prod" and view["grade"] == "L3"
    # OPS-DELTA #75：审批键从 grade 迁移到动作枚举，action 随 view 暴露。
    assert view["action"] == "restart"
    assert view["session_key"] == "sess_1" and view["source"] == "web"
    assert view["status"] == "pending" and view["scope"] is None
    assert view["created_at"].endswith("Z") and view["timeout_at"].endswith("Z")
    assert view["allow_session"] is True and view["allow_permanent"] is True


def test_get_missing_returns_none():
    assert get_web_approval("apv_nope") is None
    assert approve_web_approval("apv_nope")["code"] == "not_found"
    assert deny_web_approval("apv_nope")["code"] == "not_found"


def test_approve_once_resolves_and_wakes_waiter():
    aid = _register()
    result = approve_web_approval(aid, scope="once")
    assert result == {"status": "approved", "scope": "once"}
    assert get_web_approval(aid)["status"] == "approved"
    assert get_web_approval(aid)["scope"] == "once"
    assert wait_web_approval(aid, timeout=0.5) == "once"


def test_approve_scope_permissions():
    aid = _register(allow_session=False, allow_permanent=False)
    assert approve_web_approval(aid, scope="session")["code"] == "invalid_request"
    assert approve_web_approval(aid, scope="permanent")["code"] == "invalid_request"
    assert approve_web_approval(aid, scope="bogus")["code"] == "invalid_request"
    # 仍可 once（prod 变更确认门只允许 once）。
    assert approve_web_approval(aid, scope="once") == {"status": "approved", "scope": "once"}
    # 批四十一 §2 幂等化：已裁决条目重复批准返回当前终态（不报错、不改状态）。
    repeat = approve_web_approval(aid, scope="once")
    assert repeat["status"] == "approved"
    assert repeat["already_resolved"] is True
    assert get_web_approval(aid)["scope"] == "once"


def test_smart_denied_forces_once_scope():
    aid = _register(smart_denied=True, allow_session=True, allow_permanent=True)
    assert approve_web_approval(aid, scope="session") == {"status": "approved", "scope": "once"}
    assert get_web_approval(aid)["scope"] == "once"


def test_deny_resolves_and_wakes_waiter():
    aid = _register()
    assert deny_web_approval(aid, reason="人工拒绝") == {"status": "denied"}
    view = get_web_approval(aid)
    assert view["status"] == "denied"
    assert wait_web_approval(aid, timeout=0.5) == "deny"
    # 批四十一 §2 幂等化：重复拒绝返回当前终态（不报错）。
    repeat = deny_web_approval(aid)
    assert repeat["status"] == "denied"
    assert repeat["already_resolved"] is True
    assert get_web_approval(aid)["status"] == "denied"


def test_timeout_fail_closed_no_auto_approve_or_deny():
    aid = _register(timeout_seconds=1)
    time.sleep(1.1)
    # wait 策略：不自动批准也不自动拒绝，显式 approve/deny → 409 timeout。
    assert approve_web_approval(aid, scope="once")["code"] == "timeout"
    assert deny_web_approval(aid)["code"] == "timeout"
    view = get_web_approval(aid)
    assert view["status"] == "timeout"
    # 等待方收不到裁决（None），命令保持 pending 语义。
    assert wait_web_approval(aid, timeout=0.1) is None


def test_reclaim_timed_out_marks_timeout():
    aid = _register(timeout_seconds=1)
    assert reclaim_timed_out_web_approvals() == 0
    time.sleep(1.1)
    assert reclaim_timed_out_web_approvals() == 1
    assert get_web_approval(aid)["status"] == "timeout"
    assert reclaim_timed_out_web_approvals() == 0


def test_wait_returns_none_without_decision():
    aid = _register()
    assert wait_web_approval(aid, timeout=0.05) is None
    assert get_web_approval(aid)["status"] == "pending"


def test_list_pending_first_prod_priority_pagination():
    _register(env="test", command="cmd test", timeout_seconds=3600)
    a_dev = _register(env="dev", command="cmd dev")
    a_prod = _register(env="prod", command="cmd prod")
    _register(env="prod", command="cmd prod resolved")
    # 先解决一条 prod，验证 pending 排前。
    approve_web_approval(a_dev, scope="once")
    approve_web_approval(a_prod, scope="once")

    views, total = list_web_approvals(limit=50, offset=0)
    assert total == 4
    statuses = [v["status"] for v in views]
    assert statuses[0] == "pending" and statuses[1] == "pending"
    assert all(s in ("pending", "approved", "denied", "timeout") for s in statuses)
    pending = [v for v in views if v["status"] == "pending"]
    assert [v["env"] for v in pending] == ["prod", "test"]  # prod 优先

    prod_views, prod_total = list_web_approvals(env="prod", limit=50, offset=0)
    assert prod_total == 2 and all(v["env"] == "prod" for v in prod_views)

    resolved_views, resolved_total = list_web_approvals(status="approved", limit=50, offset=0)
    assert resolved_total == 2 and all(v["status"] == "approved" for v in resolved_views)

    page, page_total = list_web_approvals(limit=2, offset=0)
    assert len(page) == 2 and page_total == 4
    page2, _ = list_web_approvals(limit=2, offset=2)
    assert len(page2) == 2 and {v["id"] for v in page} & {v["id"] for v in page2} == set()

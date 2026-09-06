"""批次四十七 §BT — sudo_exec 审批门链路修复（OPS-DELTA #64）。

根因：sudo_exec 的 approve 决策此前直接返回 ``require_confirmation`` JSON 交回
LLM 转述，web 端永远没有审批记录落库 → 全局审批弹窗不触发、批准不生效。修复：
approve 决策改走既有审批门（``request_ops_approval`` → ``_run_approval_gate``），
web/chat 经每线程回调落 /api/approvals 注册表 → 弹窗出现 → 批准 → wait 返回 →
命令执行。

覆盖：产生→落库→批准→放行 全链路（mock）；拒绝 fail-closed；prod 变更确认门
不提供 session/永久 allowlist；无人在场 fail-closed；``_sudo_exec_handler``
端到端批准后确实执行。
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from tools import sudo_tool, terminal_tool
from tools.approval import (
    approve_web_approval,
    clear_web_approvals,
    deny_web_approval,
    list_web_approvals,
    register_web_approval,
    reset_current_session_key,
    reset_hermes_interactive_context,
    set_current_session_key,
    set_hermes_interactive_context,
    wait_web_approval,
)

_CONFIRM_DECISION = {
    "action": "approve",
    "grade": "L2",
    "env": "prod",
    "env_tier": "prod",
    "role": "prod",
    "require_confirmation": True,
    "description": "⚠ prod 变更确认门：命令分级 L2 在 prod 环境需审批",
}
_PLAIN_DECISION = dict(_CONFIRM_DECISION, require_confirmation=False,
                       description="命令分级 L2 在 prod 环境需要审批")


@pytest.fixture(autouse=True)
def _clean():
    clear_web_approvals()
    yield
    clear_web_approvals()
    terminal_tool.set_approval_callback(None)


def _web_callback(seen: dict):
    """terminal_tool 审批回调：登记 web 审批 + 阻塞等待（照 chat_api 工厂）。"""
    def _cb(command: str, description: str, *,
            allow_permanent: bool = True, allow_session: bool = True,
            smart_denied: bool = False) -> str:
        seen["command"] = command
        seen["description"] = description
        seen["allow_permanent"] = allow_permanent
        seen["allow_session"] = allow_session
        aid = register_web_approval(
            command=command,
            description=description,
            env="prod",
            grade="L2",
            session_key="any",
            source="web",
            allow_session=allow_session,
            allow_permanent=allow_permanent,
        )
        seen["approval_id"] = aid
        return wait_web_approval(aid, timeout=10) or "timeout"
    return _cb


def _run_gate(decision: dict, seen: dict):
    """独立线程跑审批门：交互上下文 + 审批回调在线程内绑定（照 chat  worker），
    隔离等待阻塞；结果写回 seen。"""
    from tools.approval import request_ops_approval

    def _runner():
        token_i = set_hermes_interactive_context(True)
        token_s = set_current_session_key("chat-sess-" + str(time.time_ns()))
        terminal_tool.set_approval_callback(_web_callback(seen))
        try:
            seen["result"] = request_ops_approval("sudo systemctl restart nginx", decision)
        finally:
            terminal_tool.set_approval_callback(None)
            reset_current_session_key(token_s)
            reset_hermes_interactive_context(token_i)

    t = threading.Thread(target=_runner)
    t.start()
    for _ in range(100):
        if "approval_id" in seen or "result" in seen:
            break
        time.sleep(0.02)
    return t


class TestSudoApprovalWebChain:
    def test_record_lands_approve_releases(self):
        """§BT 全链路：审批产生 → 记录落库（/api/approvals pending）→ 批准 → 放行。"""
        seen: dict = {}
        t = _run_gate(_CONFIRM_DECISION, seen)
        assert seen.get("approval_id"), "审批记录未落库 —— §BT 根因"

        views, total = list_web_approvals(status="pending", limit=200)
        assert total == 1
        assert views[0]["command"] == "sudo systemctl restart nginx"
        assert views[0]["status"] == "pending"

        approve_web_approval(seen["approval_id"], scope="once")
        t.join(timeout=10)
        result = seen["result"]
        assert result["approved"] is True
        assert result.get("user_consent", True) is not False

    def test_record_deny_blocks_fail_closed(self):
        """批准被拒 → fail-closed BLOCK，命令不执行。"""
        seen: dict = {}
        t = _run_gate(_CONFIRM_DECISION, seen)
        assert seen.get("approval_id")
        deny_web_approval(seen["approval_id"], reason="用户拒绝测试")
        t.join(timeout=10)
        result = seen["result"]
        assert result["approved"] is False
        assert result.get("user_consent") is False
        assert "BLOCKED" in (result.get("message") or "")

    def test_confirmation_gate_hides_session_permanent(self):
        """prod 变更确认门（require_confirmation）：弹窗不提供 session/永久选项。"""
        seen: dict = {}
        t = _run_gate(_CONFIRM_DECISION, seen)
        assert seen.get("approval_id")
        assert seen["allow_session"] is False
        assert seen["allow_permanent"] is False
        approve_web_approval(seen["approval_id"], scope="once")
        t.join(timeout=10)
        assert seen["result"]["approved"] is True

    def test_plain_approve_keeps_session_permanent(self):
        """非确认门 approve（require_confirmation=False）：session/永久选项保留。"""
        seen: dict = {}
        t = _run_gate(_PLAIN_DECISION, seen)
        assert seen.get("approval_id")
        assert seen["allow_session"] is True
        assert seen["allow_permanent"] is True
        approve_web_approval(seen["approval_id"], scope="session")
        t.join(timeout=10)
        assert seen["result"]["approved"] is True

    def test_no_human_fails_closed(self, monkeypatch):
        """无交互用户/gateway/callback → fail-closed BLOCK（不静默放行）。"""
        from tools.approval import request_ops_approval

        result = request_ops_approval("sudo systemctl restart nginx", _CONFIRM_DECISION)
        assert result["approved"] is False
        assert "BLOCKED" in (result.get("message") or "")


class TestSudoExecHandlerApprovalChain:
    """§BT 端到端：sudo_exec 触发审批 → 弹窗数据 → 批准 → 命令确实执行。"""

    def _handler_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            sudo_tool, "check_ops_command_permission",
            lambda command, target_env=None: dict(_CONFIRM_DECISION),
        )
        monkeypatch.setattr(
            sudo_tool, "_resolve_topology_credential",
            lambda host, **kw: {"type": "secret", "ref": "srv-pass",
                                "user": "ops", "port": 22},
        )

    def _run_handler_thread(self, seen: dict) -> threading.Thread:
        """照 chat_api._run_chat_turn：审批上下文 + 回调在线程内绑定。"""
        def _run_handler():
            token_i = set_hermes_interactive_context(True)
            token_s = set_current_session_key("chat-sess-" + str(time.time_ns()))
            terminal_tool.set_approval_callback(_web_callback(seen))
            try:
                seen["raw"] = sudo_tool._sudo_exec_handler(
                    {"host": "prod1", "command": "systemctl restart nginx", "env": "prod"})
            finally:
                terminal_tool.set_approval_callback(None)
                reset_current_session_key(token_s)
                reset_hermes_interactive_context(token_i)

        t = threading.Thread(target=_run_handler)
        t.start()
        return t

    def test_approved_then_executes(self, monkeypatch, tmp_path):
        """mock 触发 sudo_exec 审批 → 记录落库 → 批准 → 命令执行。"""
        self._handler_env(monkeypatch, tmp_path)
        executed: list = []
        monkeypatch.setattr(
            sudo_tool, "_run_remote_sudo",
            lambda *a, **k: executed.append(a) or SimpleNamespace(
                returncode=0, stdout="restarted", stderr=""),
        )
        seen: dict = {}
        t = self._run_handler_thread(seen)
        for _ in range(100):
            if seen.get("approval_id"):
                break
            time.sleep(0.02)
        assert seen.get("approval_id"), "审批记录未落库"
        assert executed == [], "批准前不得执行"
        approve_web_approval(seen["approval_id"], scope="once")
        # 批准 → wait 返回 → 命令放行执行
        t.join(timeout=10)
        assert executed, "批准后命令确实执行"
        out = json.loads(seen["raw"])
        assert out["status"] == "ok"
        assert out["stdout"] == "restarted"

    def test_denied_never_executes(self, monkeypatch, tmp_path):
        """mock 触发 sudo_exec 审批 → 拒绝 → 命令不执行（fail-closed）。"""
        self._handler_env(monkeypatch, tmp_path)
        executed: list = []
        monkeypatch.setattr(
            sudo_tool, "_run_remote_sudo",
            lambda *a, **k: executed.append(a) or SimpleNamespace(
                returncode=0, stdout="", stderr=""),
        )
        seen: dict = {}
        t = self._run_handler_thread(seen)
        for _ in range(100):
            if seen.get("approval_id"):
                break
            time.sleep(0.02)
        assert seen.get("approval_id")
        assert executed == [], "拒绝前不得执行"
        deny_web_approval(seen["approval_id"], reason="no")
        t.join(timeout=10)
        assert executed == [], "拒绝后命令不得执行"
        assert "BLOCKED" in seen["raw"]


# ---------------------------------------------------------------------------
# M2（任务十安全审查）—— yolo 不可跳过 {approve: required}/force_manual 强制人工门
# ---------------------------------------------------------------------------

@pytest.fixture
def _yolo(monkeypatch):
    """会话 yolo 打开（进程 _YOLO_MODE_FROZEN 钉死 False，走可 monkeypatch 的缝）。"""
    import tools.approval as approval_module
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_module, "is_current_session_yolo_enabled", lambda: True)
    return approval_module


class TestYoloVsRequiredConfirmation:
    """矩阵 {approve: required} / force_manual 在 yolo 下必须仍走人工确认——
    与 terminal 通道 _ops_confirmation_required 前置守卫同一不变量。
    修复前：sudo/asset 通道经 _run_approval_gate 的 yolo 短路直接自动批准。"""

    def test_yolo_required_still_prompts_web_chain(self, _yolo):
        """yolo 开 + required：审批仍产生并落库（修复前：无审批直接放行）。"""
        seen: dict = {}
        t = _run_gate(_CONFIRM_DECISION, seen)
        assert seen.get("approval_id"), "yolo 下强制人工门被跳过（M2 回归）"
        approve_web_approval(seen["approval_id"], scope="once")
        t.join(timeout=10)
        assert seen["result"]["approved"] is True

    def test_yolo_required_no_human_fails_closed(self, _yolo):
        """yolo 开 + required + 无人在场 → fail-closed BLOCK（不自动批准）。"""
        from tools.approval import request_ops_approval

        result = request_ops_approval("sudo systemctl restart nginx", _CONFIRM_DECISION)
        assert result["approved"] is False
        assert "BLOCKED" in (result.get("message") or "")

    def test_yolo_plain_approve_still_auto_skipped(self, _yolo):
        """yolo 开 + 普通 approve 档：仍然自动放行（yolo 对可恢复审批照常生效）。"""
        from tools.approval import request_ops_approval

        result = request_ops_approval("sudo systemctl restart nginx", _PLAIN_DECISION)
        assert result["approved"] is True

    def test_yolo_force_manual_asset_fails_closed(self, _yolo):
        """yolo 开 + 资产 force_manual → fail-closed（修复前：自动批准）。"""
        from tools.approval import request_asset_approval

        result = request_asset_approval(
            asset_type="runbook", asset_name="prod-rotate",
            description="高危资产创建", env="prod", force_manual=True)
        assert result["approved"] is False
        assert "BLOCKED" in (result.get("message") or "")

    def test_yolo_plain_asset_auto_approved(self, _yolo):
        """yolo 开 + 非强制资产 → 自动批准（可恢复审批，现状不变）。"""
        from tools.approval import request_asset_approval

        result = request_asset_approval(
            asset_type="runbook", asset_name="dev-lint",
            description="低风险资产", env="dev", force_manual=False)
        assert result["approved"] is True

    def test_cron_force_manual_blocked_even_in_approve_mode(self, _yolo, monkeypatch):
        """cron + 强制人工：cron_mode: approve 也不放行（无人在场；terminal 通道
        对 ops 审批本就无 cron_mode 逃生门，sudo 通道对齐）。"""
        import tools.approval as approval_module
        monkeypatch.setattr(approval_module, "_is_cron_approval_context", lambda: True)
        monkeypatch.setattr(approval_module, "_get_cron_approval_mode", lambda: "approve")
        from tools.approval import request_ops_approval
        result = request_ops_approval("sudo systemctl restart nginx", _CONFIRM_DECISION)
        assert result["approved"] is False
        assert "BLOCKED" in (result.get("message") or "")

    def test_cron_plain_approve_mode_still_auto_approves(self, monkeypatch):
        """cron + 普通 approve 档 + cron_mode: approve：自动放行（现状不变）。"""
        import tools.approval as approval_module
        monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
        monkeypatch.setattr(approval_module, "is_current_session_yolo_enabled",
                            lambda: False)
        monkeypatch.setattr(approval_module, "_is_cron_approval_context", lambda: True)
        monkeypatch.setattr(approval_module, "_get_cron_approval_mode", lambda: "approve")
        from tools.approval import request_ops_approval
        result = request_ops_approval("sudo systemctl restart nginx", _PLAIN_DECISION)
        assert result["approved"] is True

"""runbook 执行级并发锁（OPS-DELTA #81）——同名 runbook / 同目标禁止并发下发。

执行注册表（内存 + started_at）：执行开始注册 ``{runbook, version, exec_id,
targets, started_at, env}``，执行结束（含失败/回滚）释放。冲突判定：新请求
runbook 名相同 **或** target 集合与进行中执行重叠 → 拒绝 + 明确错误。崩溃
兜底：超过 ``_LOCK_TTL_S``（6h）的注册视为过期自动释放（惰性清理，访问即
prune）。锁覆盖同一进程内全部执行入口（web / 定时 schedule / LLM 工具路径——
三者都经 ``runbook_exec.execute_runbook`` 引擎入口）；多进程部署的跨进程锁
不在本批范围（Vigil 单进程运行）。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Set

# 崩溃兜底阈值：执行进程异常退出后锁滞留，超过该时长视为过期自动释放
# （OPS-DELTA #81 注明）。
_LOCK_TTL_S = 6 * 3600
_LOCK_TTL_LABEL = "6h"

_lock = threading.RLock()
_registry: Dict[str, Dict[str, Any]] = {}  # exec_id -> entry


def _now() -> float:
    return time.time()


def _expired(entry: Dict[str, Any]) -> bool:
    return (_now() - float(entry.get("started_at") or 0)) > _LOCK_TTL_S


def _prune() -> None:
    expired = [eid for eid, entry in _registry.items() if _expired(entry)]
    for eid in expired:
        _registry.pop(eid, None)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def _conflict_message(entry: Dict[str, Any], reason: str) -> str:
    return (
        f"runbook {entry.get('runbook')} 正在执行中"
        f"（exec_id={entry.get('exec_id')}，自 {_iso(float(entry.get('started_at') or 0))}）"
        f"——{reason}，锁定中：禁止并发下发"
        f"（锁 TTL {_LOCK_TTL_LABEL}，进程异常退出后自动释放）"
    )


def peek_conflict(*, runbook: str, targets: Optional[Set[str]] = None
                  ) -> Optional[str]:
    """只读冲突探测（web POST 快速拒绝；不注册）。冲突 → 错误文案；无 → None。"""
    with _lock:
        _prune()
        for entry in _registry.values():
            if entry.get("runbook") == runbook:
                return _conflict_message(entry, "同名 runbook 已在执行")
            entry_targets = set(entry.get("targets") or [])
            if targets and entry_targets & targets:
                overlap = sorted(entry_targets & targets)
                return _conflict_message(
                    entry, f"目标与进行中执行重叠（{', '.join(overlap)}）")
        return None


def try_acquire(*, runbook: str, version: str = "", exec_id: str,
                targets: Optional[Set[str]] = None,
                env: str = "") -> Optional[str]:
    """注册执行锁。冲突 → 错误文案（调用方拒绝执行）；成功 → None。

    同一 exec_id 重复注册（web 预检后引擎正式注册用同一 id）→ 视为自身，跳过。
    """
    targets = set(targets or [])
    with _lock:
        _prune()
        for entry in _registry.values():
            if entry.get("exec_id") == exec_id:
                continue  # 自身重入（预检后正式注册），不判冲突
            if entry.get("runbook") == runbook:
                return _conflict_message(entry, "同名 runbook 已在执行")
            if targets and set(entry.get("targets") or []) & targets:
                overlap = sorted(set(entry.get("targets") or []) & targets)
                return _conflict_message(
                    entry, f"目标与进行中执行重叠（{', '.join(overlap)}）")
        _registry[exec_id] = {
            "runbook": runbook,
            "version": version,
            "exec_id": exec_id,
            "targets": sorted(targets),
            "started_at": _now(),
            "env": env,
        }
        return None


def release(exec_id: str) -> None:
    with _lock:
        _registry.pop(exec_id, None)


def list_locks() -> List[Dict[str, Any]]:
    """进行中锁快照（供执行记录/端点查询锁状态）。"""
    with _lock:
        _prune()
        return [
            {
                "exec_id": e.get("exec_id"),
                "runbook": e.get("runbook"),
                "version": e.get("version"),
                "env": e.get("env"),
                "targets": list(e.get("targets") or []),
                "started_at": _iso(float(e.get("started_at") or 0)),
            }
            for e in sorted(_registry.values(),
                            key=lambda x: float(x.get("started_at") or 0))
        ]

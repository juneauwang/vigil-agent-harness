"""凭据保险箱（OPS-DELTA #16 输入侧 + #25 sudo 分级放行的登记源）。

零侵入独立文件：把"用户对话里给出的明文凭据"落盘为 600 权限文件（本机
保险箱），同时维护会话内"已登记凭据来源"标记（user/vault + 时间戳），供
``tools/approval.py`` 的 sudo -S 守卫查询——密码有来源（用户给了 / vault 读
过）即视为授权放行，无来源（agent 猜测/试探）仍拦截（防暴力）。

SOP（agent 侧行为引导，不改核心）：对话输入疑似凭据（长度 ≥ 8 的密码类串）
时，凭据类输入由系统存入保险箱，会话记录不保留明文；agent 应调用本模块
``store()`` 存入后只引用名字，不再在命令串/回复里内插明文。命令串里的
明文会过 ``agent/redact.py`` 打码（#21 输出侧）。

存储：<VIGIL_HOME>/secrets/<name>，权限 600，owner 校验（非 owner 拒绝
读取）。name 仅允许 [A-Za-z0-9._-]+（防路径穿越）。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_SECRETS_DIRNAME = "secrets"
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_VALID_SOURCES = {"user", "secret"}

# 会话内已登记凭据来源标记：(session_key, name) -> {"source", "ts"}。
# session_key 复用 tools/approval.get_current_session_key() 的上下文解析
# （approval session contextvar → gateway session_context → env 兜底），
# 与 agent/secret_scope.py 的进程内隔离语义一致：本会话登记的来源只解除
# 本会话的 sudo -S 防暴力守卫，不跨会话泄漏（任务11审查 A1）。
# 单会话（CLI）场景所有读写解析到同一个 key，行为与旧实现完全一致。
_REGISTERED: Dict[tuple, Dict[str, Any]] = {}
_REGISTERED_LOCK = threading.Lock()
# 长驻 multiplex gateway 里死会话的登记条目按 session 键驱逐（最老先出），
# 防止注册表随会话数无界增长（与 approval._denial_tally 同款封顶策略）。
_REGISTERED_MAX_SESSIONS = 256


def _current_session_key() -> str:
    """当前审批会话键（懒导入避免与 tools.approval 循环依赖）。"""
    try:
        from tools.approval import get_current_session_key
        return get_current_session_key(default="default") or "default"
    except Exception:
        return "default"


def _prune_locked() -> None:
    """按 session 键驱逐最老条目（须持 _REGISTERED_LOCK 调用）。"""
    sessions = {sess for sess, _name in _REGISTERED}
    while len(sessions) > _REGISTERED_MAX_SESSIONS:
        oldest = min(sessions, key=lambda s: min(
            (info.get("ts") or 0)
            for (sess, _n), info in _REGISTERED.items() if sess == s
        ))
        for key in [k for k in _REGISTERED if k[0] == oldest]:
            _REGISTERED.pop(key, None)
        sessions.discard(oldest)


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _secrets_dir() -> Path:
    return _hermes_home() / _SECRETS_DIRNAME


def path_for(name: str) -> Path:
    """保险箱文件路径（校验 name；不校验存在性）。供 askpass 等读取方使用。"""
    _validate_name(name)
    return _secrets_dir() / name


def _validate_name(name: str) -> None:
    if not name or not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"凭据名 {name!r} 非法：仅允许 [A-Za-z0-9._-]+（防路径穿越）"
        )


def _check_owner(path: Path) -> None:
    """Owner 校验：文件属主必须是当前用户（防读取他人凭据）。"""
    if not hasattr(os, "getuid"):
        return  # 非 POSIX（Windows）无 uid 语义，跳过
    try:
        st = path.stat()
    except OSError as exc:
        raise PermissionError(f"无法读取凭据文件 {path}: {exc}")
    if st.st_uid != os.getuid():
        raise PermissionError(
            f"凭据文件 {path} 属主不是当前用户（uid {st.st_uid} != {os.getuid()}），拒绝读取"
        )


def register_source(name: str, source: str) -> None:
    """登记**当前会话**的凭据来源标记（user/secret + 时间戳），供 sudo guard 查询。"""
    _validate_name(name)
    if source not in _VALID_SOURCES:
        raise ValueError(f"凭据来源必须是 user/secret，收到 {source!r}")
    with _REGISTERED_LOCK:
        _REGISTERED[(_current_session_key(), name)] = {
            "source": source, "ts": time.time(),
        }
        _prune_locked()
    logger.debug("credential_vault: registered source=%s for %r", source, name)


def mark_user_authorized() -> None:
    """用户本轮确认过命令审批 → 登记 user 来源（sudo guard 放行依据之一）。

    OPS-DELTA #25：approval 审批确认时登记——用户即凭据持有者，给了即授权。
    只登记标记，不写文件（无明文可落盘）。
    """
    register_source("__user_authorized__", "user")


def store(name: str, value: str, source: str = "user") -> str:
    """把凭据写入本机保险箱（600 权限，owner 校验）。

    写入即登记来源标记（默认 user——用户本轮明确提供）。返回凭据名引用，
    agent 之后只引用名字，不再持有/内插明文。
    """
    _validate_name(name)
    if not isinstance(value, str) or not value:
        raise ValueError("凭据值必须是非空字符串")
    if source not in _VALID_SOURCES:
        raise ValueError(f"凭据来源必须是 user/secret，收到 {source!r}")

    secrets = _secrets_dir()
    secrets.mkdir(parents=True, exist_ok=True)
    path = secrets / name
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
    except Exception:
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _check_owner(path)
    register_source(name, source)
    # OPS-DELTA 批次三十二：写入的凭据值自动登记进全局 redact 登记表——
    # 值一旦入 vault，任何输出通道（终端/review diff/SSE/trajectory/落库）
    # 精确匹配即打码，不依赖熵检测。注册逻辑自带资格过滤（过短/纯数字跳过）。
    try:
        from agent.redact import register_credential_value
        register_credential_value(value)
    except Exception:
        logger.debug("credential value registration failed", exc_info=True)
    logger.info("credential_vault: stored %r (source=%s)", name, source)
    return name


def retrieve(name: str) -> str:
    """读取保险箱凭据；读取即登记 vault 来源（供 sudo guard 判定）。

    非 owner / 文件缺失 / 名非法 → 抛异常。返回明文给调用方（仅执行层使用；
    agent 输出层见 agent/redact.py #21 打码）。
    """
    _validate_name(name)
    path = _secrets_dir() / name
    if not path.is_file():
        raise FileNotFoundError(f"保险箱中不存在凭据: {name}")
    _check_owner(path)
    value = path.read_text(encoding="utf-8")
    register_source(name, "secret")
    return value


def expire(name: str) -> bool:
    """标记过期并删除保险箱文件（生命周期结束清理）。

    来源标记跨会话存在（同一凭据名可能被多个会话登记过）→ 全部清除。
    """
    _validate_name(name)
    with _REGISTERED_LOCK:
        for key in [k for k in _REGISTERED if k[1] == name]:
            _REGISTERED.pop(key, None)
    path = _secrets_dir() / name
    try:
        if path.is_file():
            path.unlink()
            logger.info("credential_vault: expired %r", name)
            return True
    except OSError as exc:
        logger.warning("credential_vault: expire %r failed: %s", name, exc)
    return False


def has_credential_source(sources=("user", "secret")) -> bool:
    """当前会话是否已登记指定来源的凭据（sudo guard 三态判定的第 1/2 态）。

    只查**当前会话**的登记切片——其他会话登记的来源不解除本会话的
    sudo -S 防暴力守卫（任务11审查 A1：旧实现是进程全局扫描，多会话
    gateway 里任意会话的登记会解除所有会话的拦截）。
    """
    if isinstance(sources, str):
        sources = (sources,)
    allowed = set(sources)
    session = _current_session_key()
    with _REGISTERED_LOCK:
        return any(
            info.get("source") in allowed
            for (sess, _name), info in _REGISTERED.items()
            if sess == session
        )


def registered_summary() -> Dict[str, Any]:
    """已登记凭据概览（审计/调试用，不含明文）。键为 ``session:name``。"""
    with _REGISTERED_LOCK:
        return {
            f"{sess}:{name}": {
                "session": sess,
                "name": name,
                "source": info.get("source"),
                "ts": info.get("ts"),
            }
            for (sess, name), info in sorted(_REGISTERED.items())
        }

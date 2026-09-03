"""命令级自进化白名单 v0.1——存储 + 模板归一化 + 信号沉淀（batch86，OPS-DELTA #102）。

08-31 实测：``sudo lscpu`` / ``sudo free -h`` / ``sudo cat /etc/hosts`` 在 prod 弹审批，
根因是分类器规则表只覆盖命令族（docker/kubectl/systemctl/包管理器），裸只读命令漏配 →
unknown → 矩阵默认 approve。命令空间开放，"枚举规则"补不完——本模块沉淀**用户批准的
只读命令模板**，下次同模板直接跳过审批（裁决接入在 tools/ops_permissions.py）。

三条红线（2026-09-01 设计定案，焊死）：

- **红线 1（用户侧信号）**：沉淀必须来自"用户批准"（审批门 approved/denied 事件），
  不能只看执行成功——``record_success`` 只由审批事件的用户批准触发；被用户拒绝过
  一次 → status=banned，永不自动沉淀（解禁只允许 ``forget`` 显式操作）。
- **红线 2（模板归一化）**：沉淀的是归一化模板（``lscpu -e``/``sudo lscpu`` →
  ``lscpu``），不是原始串。带动态参数（URL/IP/值 flag/操作数）+ 管道/链式 +
  破坏性形态（rm/dd/mkfs/重定向/tee/shred/-rf/-delete）→ ``normalize_template``
  返回 None = 永不进白名单、永远走原判定。
- **红线 3（可查看/撤销/审计）**：``vigil approvals list/forget/export`` +
  trajectory 审计（沉淀/撤销/导出都记）。

数据文件 ``<hermes home>/approval_memory.yaml``（默认不存在 = 空白名单 + 内置种子表
兜底）。内置只读种子表（lscpu/free/cat/...，source_task="seed"）默认 active，冷启动
起点；文件条目可覆盖种子（banned/inactive → 种子回到弹审批）。种子命令的只读操作数
（``cat /etc/hosts`` → 模板 ``cat``）与无值布尔 flag（``free -h`` → ``free``）可命中，
带值参数形态永远走判定。
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import threading
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_FILENAME = "approval_memory.yaml"
_SCHEMA_VERSION = 1

# 沉淀阈值：同一命令被用户批准 N 次（且从未被拒）→ active。
_ACTIVE_THRESHOLD = 3

# 内置只读种子表（冷启动起点，默认 active，source_task="seed"；自进化负责补漏项）。
# 种子命令的只读操作数/无值布尔 flag 形态可归一命中（模板 = 种子名本身）。
_SEED_READ_TEMPLATES: frozenset = frozenset({
    "lscpu", "free", "df", "du", "uptime", "uname", "hostname", "whoami",
    "id", "date", "ps", "stat", "ls", "cat", "grep", "head", "tail", "wc",
})

# 模板形态黑名单：任何含这些词的命令永不沉淀（即使成功 N 次——只读语义是沉淀前提）。
_BLACKLIST_WORDS = frozenset({"rm", "dd", "mkfs", "tee", "shred"})
_BLACKLIST_TOKEN_SUBSTR = ("-rf", "-rF", "-delete", "--delete", "shred")

# 链式/管道/命令替换/重定向——复合命令形态不可预测，永不沉淀、裁决查询也不命中。
_CHAIN_RE = re.compile(r"[|;&`\n]|\$\(")
_REDIRECT_RE = re.compile(r"[<>]")

# 主命令名（第一个 token）形态：简单命令名，不带路径/前缀 '-'/内嵌 '='。
_MAIN_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.+-]*$")

# 文件锁：进程内写-改-写串行化（跨进程并发留 v0.1 文档边界）。
_lock = threading.Lock()


def seed_templates() -> frozenset:
    """内置只读种子模板集合（冷启动默认 active）。"""
    return _SEED_READ_TEMPLATES


def memory_path(home: Optional[os.PathLike] = None) -> "object":
    """approval_memory.yaml 路径（<hermes home>，随 VIGIL_HOME/profile 语义）。"""
    from hermes_constants import get_hermes_home
    base = home if home is not None else get_hermes_home()
    return Path(base).expanduser() / _FILENAME



# ---------------------------------------------------------------------------
# 归一化（红线 2 核心）
# ---------------------------------------------------------------------------

def _strip_command_prefix(command: str) -> str:
    """剥 sudo/doas 前缀（复用 action_classifier 同款逻辑，避免双实现漂移）。"""
    from tools.action_classifier import _strip_sudo_prefix
    return _strip_sudo_prefix(command)


def normalize_template(command: Any) -> Optional[str]:
    """命令串 → 归一化模板；形态不沉淀 → None。

    规则（红线 2）：
    - 剥 sudo/doas 前缀（``sudo lscpu`` → ``lscpu``）。
    - 管道/链式（| && || ; 换行 ` $(）、重定向（>/<）→ None（永不沉淀）。
    - 黑名单形态（rm/dd/mkfs/tee/shred 词、-rf/-delete 串）→ None。
    - 主命令 = 第一个 token；无值布尔 flag（-h/-e/-a/-l 等）剥掉归一到主命令。
    - 值 flag / ``--x=y`` / 任何含 ``=`` 的 token → None（带动态值永远走判定）。
    - 操作数（位置参数）：
        * 主命令 ∈ 种子只读表（cat/grep/head/tail/wc/ls/stat/df/du/...）→ 只读目标，
          剥掉（``cat /etc/hosts`` → 模板 ``cat``——纯查询形态可归一）；
        * 其他命令带操作数（kubectl delete xxx / curl URL / apply -f 文件）→ None
          （动态参数不沉淀，永远走原判定）。
    - 主命令名非法（路径/前缀 '-'/含 '='）→ None。
    """
    if not isinstance(command, str) or not command.strip():
        return None
    cmd = _strip_command_prefix(command.strip())
    if not cmd:
        return None
    # 链式/重定向/命令替换：复合形态永不沉淀、裁决查询不命中。
    if _CHAIN_RE.search(cmd) or _REDIRECT_RE.search(cmd):
        return None
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    if not tokens:
        return None
    main = tokens[0].strip()
    if not _MAIN_NAME_RE.fullmatch(main):
        return None  # 路径/前缀 '-'/含 '=' 等非常规主命令形态
    lower_main = main.lower()
    for tok in tokens:
        if not tok:
            continue
        # 黑名单词（含 mkfs.ext4 这类前缀形态）与 -rf/-delete 串 → 永不沉淀。
        word = tok.lower()
        if word in _BLACKLIST_WORDS or any(word.startswith(w) for w in _BLACKLIST_WORDS):
            return None
        if any(sub in tok for sub in _BLACKLIST_TOKEN_SUBSTR):
            return None
        if "=" in tok:
            return None  # 任何带值 token（--x=y / FOO=bar / -f=文件）→ 动态值形态
    positional = False
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            positional = positional or (i + 1 < len(tokens))
            break
        if tok.startswith("-") and tok != "-":
            # 布尔 flag 簇：后跟非 flag token → 可能取值（-f file）→ 动态形态。
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                return None
            i += 1
            continue
        positional = True
        i += 1
    if lower_main in _SEED_READ_TEMPLATES:
        return lower_main  # 只读命令：操作数是读目标，剥掉归一到种子模板
    if positional:
        return None  # 非只读族命令带操作数 → 动态参数 → 永不沉淀
    return lower_main


# ---------------------------------------------------------------------------
# 存储层（红线 3：可查看/撤销；fail-safe）
# ---------------------------------------------------------------------------

def _load_raw(home: Optional[os.PathLike] = None) -> Dict[str, Any]:
    """读文件（损坏 → 空白名单 + warning，不崩——白名单查询失败只少一次跳过）。"""
    path = memory_path(home)
    try:
        if not path.is_file():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = _yaml_load(f.read()) or {}
        if not isinstance(data, dict):
            return {}
        raw_entries = data.get("entries")
        if not isinstance(raw_entries, list):
            return {}
        return {"schema_version": int(data.get("schema_version") or _SCHEMA_VERSION),
                "entries": raw_entries}
    except Exception as exc:
        logger.warning("approval_memory: 读取 %s 失败（按空白名单处理）：%s", path, exc)
        return {}


def _yaml_load(text: str) -> Any:
    import yaml
    return yaml.safe_load(text)


def _yaml_dump(obj: Any) -> str:
    import yaml
    return yaml.safe_dump(obj, allow_unicode=True, sort_keys=False, default_flow_style=False)


# 读缓存（mtime 键）：裁决查询热路径不逐命令 YAML 解析；写入 os.replace 换 mtime 自动失效。
_read_cache: Dict[Any, Dict[str, Dict[str, Any]]] = {}


def _entries_by_template(home: Optional[os.PathLike] = None) -> Dict[str, Dict[str, Any]]:
    """模板 → 条目 dict（文件条目；缺失 = 无覆盖）。按文件 mtime 缓存。"""
    path = Path(memory_path(home))
    try:
        mtime = path.stat().st_mtime_ns if path.is_file() else 0
    except OSError:
        mtime = 0
    key = (str(path), mtime)
    cached = _read_cache.get(key)
    if cached is not None:
        return cached
    data = _load_raw(home)
    out: Dict[str, Dict[str, Any]] = {}
    for item in data.get("entries") or []:
        if not isinstance(item, dict):
            continue
        template = str(item.get("template") or "").strip()
        if not template:
            continue
        out[template] = _sanitize_entry(template, item)
    _read_cache.clear()  # 单槽缓存，防陈旧膨胀
    _read_cache[key] = out
    return out


def _sanitize_entry(template: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """条目字段规范化（老/坏字段按默认兜底，绝不因字段异常崩查询）。"""
    status = str(item.get("status") or "pending").strip().lower()
    if status not in ("pending", "active", "banned", "inactive"):
        status = "pending"
    count = item.get("success_count")
    try:
        count = max(0, int(count))
    except (TypeError, ValueError):
        count = 0
    return {
        "template": template,
        "status": status,
        "success_count": count,
        "never_denied": bool(item.get("never_denied", True)),
        "user_opt_in": bool(item.get("user_opt_in", False)),
        "approved_at": str(item.get("approved_at") or "") or None,
        "source_task": str(item.get("source_task") or "") or None,
    }


def _save_entries(entries: Dict[str, Dict[str, Any]],
                  home: Optional[os.PathLike] = None) -> None:
    """原子落盘（tmp + replace）；目录不存在时创建。"""
    path = Path(memory_path(home))
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(
        (dict(v) for v in entries.values()),
        key=lambda e: e["template"],
    )
    payload = {"schema_version": _SCHEMA_VERSION, "entries": rows}
    fd, tmp = tempfile.mkstemp(prefix=".approval_memory-", dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_yaml_dump(payload))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _audit(template: str, action: str, *, home: Optional[os.PathLike] = None,
           source: str = "approval-memory", **extra) -> None:
    """沉淀/撤销/导出审计（复用 trajectory 通道；best-effort 绝不抛）。"""
    try:
        from agent.trajectory import record_event
        meta = {"template": template, "action": action}
        meta.update({k: v for k, v in extra.items() if v is not None})
        record_event(type="approval_memory", session_id=source,
                     action=template, meta=meta)
    except Exception:
        logger.debug("approval_memory audit failed", exc_info=True)


def _current_source_task() -> str:
    """来源任务/会话 id（审批上下文里的 session key；无 → 'approval'）。"""
    try:
        from tools.approval import get_current_session_key
        key = (get_current_session_key() or "").strip()
        return key or "approval"
    except Exception:
        return "approval"


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def get_entry(template: str, home: Optional[os.PathLike] = None) -> Optional[Dict[str, Any]]:
    """文件条目（无覆盖 → None；种子语义见 :func:`is_active`）。"""
    t = str(template or "").strip().lower()
    if not t:
        return None
    return _entries_by_template(home).get(t)


def is_active(template: str, home: Optional[os.PathLike] = None) -> bool:
    """模板是否命中免审批白名单：文件 active > 种子默认 active > 否。

    文件条目可覆盖种子：banned/inactive（forget 后）→ 种子回到弹审批。
    """
    t = str(template or "").strip().lower()
    if not t:
        return False
    entry = _entries_by_template(home).get(t)
    if entry is not None:
        return entry.get("status") == "active"
    return t in _SEED_READ_TEMPLATES


def command_template_approved(command: Any, home: Optional[os.PathLike] = None) -> bool:
    """裁决查询（ops_permissions 用）：命令归一化命中 active 模板 → 跳过审批。

    链式/动态参数/黑名单形态 normalize 返回 None → 永不命中（永远走原判定）。
    """
    template = normalize_template(command)
    if template is None:
        return False
    return is_active(template, home=home)


# ---------------------------------------------------------------------------
# 信号沉淀（红线 1：用户侧信号）
# ---------------------------------------------------------------------------

def record_success(template: str, *, user_opt_in: bool = False,
                   source_task: Optional[str] = None,
                   home: Optional[os.PathLike] = None) -> Optional[Dict[str, Any]]:
    """用户批准信号：success_count+1；>=3 且从未被拒 → active（下次跳过审批）。

    - banned 条目永不自动沉淀（record_success 是 no-op——被拒一次，哪怕后面成功
      100 次也不解除；解除只允许 forget 显式操作）。
    - user_opt_in=True（用户明说"以后不用问"）且从未被拒 → 立即 active（不等 3 次）。
    - 种子条目默认 active 不走计数；种子被 forget（inactive 覆盖）后重新积累。
    """
    t = str(template or "").strip().lower()
    if not t:
        return None
    with _lock:
        entries = _entries_by_template(home)
        entry = entries.get(t)
        if entry is not None and entry["status"] == "banned":
            return entry  # 永不自动复活（红线 1）
        base = entry or _default_entry(t)
        count = base["success_count"] + 1
        never_denied = base.get("never_denied", True)
        opt_in = bool(user_opt_in) and never_denied
        if opt_in or (count >= _ACTIVE_THRESHOLD and never_denied):
            status = "active"
            approved_at = base.get("approved_at") or _now_iso()
            source_task = source_task or base.get("source_task") or _current_source_task()
        else:
            status = "pending"
            approved_at = base.get("approved_at")
            source_task = base.get("source_task") or source_task or _current_source_task()
        updated = {
            "template": t,
            "status": status,
            "success_count": count,
            "never_denied": never_denied,
            "user_opt_in": bool(base.get("user_opt_in") or opt_in),
            "approved_at": approved_at,
            "source_task": source_task,
        }
        entries[t] = updated
        _save_entries(entries, home=home)
    if updated["status"] == "active":
        _audit(t, "sedimented" if not opt_in else "opt_in",
               source=source_task or "approval", count=count)
    return updated


def record_denial(template: str, *, source_task: Optional[str] = None,
                  home: Optional[os.PathLike] = None) -> Dict[str, Any]:
    """用户拒绝信号：status=banned、never_denied=False——永不自动沉淀。

    种子条目同样被覆盖（用户拒绝 = 该模板不再自动放行；forget 可解禁）。
    """
    t = str(template or "").strip().lower()
    if not t:
        return _default_entry("")
    with _lock:
        entries = _entries_by_template(home)
        base = entries.get(t) or _default_entry(t)
        updated = {
            "template": t,
            "status": "banned",
            "success_count": base.get("success_count", 0),
            "never_denied": False,
            "user_opt_in": False,
            "approved_at": base.get("approved_at"),
            "source_task": base.get("source_task") or source_task or _current_source_task(),
        }
        entries[t] = updated
        _save_entries(entries, home=home)
    _audit(t, "banned", source=source_task or "approval")
    return updated


def _default_entry(template: str) -> Dict[str, Any]:
    return {
        "template": template,
        "status": "pending",
        "success_count": 0,
        "never_denied": True,
        "user_opt_in": False,
        "approved_at": None,
        "source_task": None,
    }


def forget(template: str, *, home: Optional[os.PathLike] = None) -> bool:
    """撤销单条（红线 3）：条目置 inactive、计数清零——回到弹审批状态。

    种子可撤销（inactive 覆盖种子默认 active）；banned 条目仅本命令可解除
    （forget 后重新批准 3 次可再沉淀——不自动复活）。无条目且非种子 → False。
    """
    t = str(template or "").strip().lower()
    if not t:
        return False
    with _lock:
        entries = _entries_by_template(home)
        if t not in entries and t not in _SEED_READ_TEMPLATES:
            return False
        updated = _default_entry(t)
        updated["status"] = "inactive"
        entries[t] = updated
        _save_entries(entries, home=home)
    _audit(t, "forget", source="approvals-cli")
    return True


def list_rows(home: Optional[os.PathLike] = None) -> List[Dict[str, Any]]:
    """可查看全量：文件条目 + 虚拟种子条目（无文件覆盖时补出，source_task=seed）。"""
    entries = _entries_by_template(home)
    rows = [dict(v) for v in entries.values()]
    seen = set(entries)
    for seed in sorted(_SEED_READ_TEMPLATES):
        if seed in seen:
            continue
        rows.append(_default_entry(seed))
        rows[-1].update(status="active", source_task="seed", approved_at=None)
    return sorted(rows, key=lambda e: e["template"])


def note_user_decision(command: Any, outcome: str, *, scope: Optional[str] = None,
                       source_task: Optional[str] = None,
                       home: Optional[os.PathLike] = None) -> Optional[Dict[str, Any]]:
    """审批事件 → 沉淀信号（approval.py trajectory 挂载点调用；best-effort）。

    outcome ∈ approved / denied。scope="learn" = 用户选了"以后不用问"（CLI opt-in）
    → user_opt_in=True（立即沉淀）。命令不可沉淀（模板形态黑名单/动态参数/链式）
    → 不记录任何信号（拒绝也不 ban——这类命令本来就不进白名单）。
    """
    template = normalize_template(command)
    if template is None:
        return None
    if outcome == "denied":
        return record_denial(template, source_task=source_task, home=home)
    if outcome == "approved":
        return record_success(template, user_opt_in=(scope == "learn"),
                              source_task=source_task, home=home)
    return None


def _now_iso() -> str:
    try:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    except Exception:
        return datetime.now().isoformat(timespec="seconds")

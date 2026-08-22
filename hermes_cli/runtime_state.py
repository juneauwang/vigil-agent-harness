"""运行时瞬态状态存储（批三十五 lazy last_seen）。

与静态事实层分离：topology.yaml / services/*.yaml 是权威拓扑（v0.4 四层模型，
永远不含运行时瞬态）；本模块维护 ``<VIGIL_HOME>/runtime_state.json``（0600，原子写），
记录 agent 与各 host 最近一次真实交互成功的时间戳（epoch 秒）。

活性语义：不主动探测（无定时探活/prom 对接），agent 交互即活性——
vssh/SSH 会话发起、topo_status_sync 探测成功、sudo_exec 远端执行成功时
打点；UI 侧按阈值（默认 10 分钟）显示"在线 · X 分钟前活跃"或"离线/无活动"。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional

_RUNTIME_FILENAME = "runtime_state.json"
_lock = threading.Lock()


def _state_path(home: Optional[Path] = None) -> Path:
    from hermes_constants import get_hermes_home
    return Path(home or get_hermes_home()) / _RUNTIME_FILENAME


def _load(home: Optional[Path] = None) -> Dict[str, Dict]:
    path = _state_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save(data: Dict[str, Dict], home: Optional[Path] = None) -> None:
    path = _state_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def mark_host_activity(host: str, *, home: Optional[Path] = None,
                       now: Optional[float] = None) -> bool:
    """记录 host 最近一次交互成功时间戳。host 为空/非字符串 → 不打点。

    返回 True 表示已记录（调用方可用于测试断言）。线程安全：进程内锁 + 原子
    写（tmp + os.replace），多线程打点不丢条目。
    """
    if not host or not isinstance(host, str):
        return False
    host = host.strip()
    if not host:
        return False
    ts = float(now) if now is not None else time.time()
    with _lock:
        data = _load(home)
        data[host] = {"last_seen": ts}
        _save(data, home)
    return True


def get_host_activity(host: str, *, home: Optional[Path] = None) -> Optional[int]:
    """读取 host 的 last_seen epoch；无记录/异常 → None。"""
    if not host:
        return None
    with _lock:
        data = _load(home)
    entry = data.get(host)
    if not isinstance(entry, dict):
        return None
    try:
        ts = float(entry.get("last_seen") or 0)
    except (TypeError, ValueError):
        return None
    return int(ts) if ts > 0 else None


def load_activity(home: Optional[Path] = None) -> Dict[str, int]:
    """整表读取：{host: last_seen epoch}（build_view 合并用，避免逐 host 读盘）。"""
    with _lock:
        data = _load(home)
    out: Dict[str, int] = {}
    for host, entry in data.items():
        if not isinstance(entry, dict):
            continue
        try:
            ts = float(entry.get("last_seen") or 0)
        except (TypeError, ValueError):
            continue
        if ts > 0:
            out[str(host)] = int(ts)
    return out

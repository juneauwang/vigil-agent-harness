"""``vigil watch`` 采集常驻循环（systemd --user 服务入口）。

systemd unit 的 ``ExecStart=<venv-python> -m hermes_cli.watch_collect_loop``
指向本模块。每 300s（5 分钟）调一次 ``tools.watch_collect.collect_once()``，
SIGTERM/SIGINT 优雅退出（不残留半写 inbox 文件——inbox 写入本身是原子
tmp+rename）。

采集是确定性代码，不需要 LLM；分析播报由 agent 会话消费 inbox（第二层，
见 tools/watch_tools.py）。本循环不碰 agent / 会话 / prompt 缓存。
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from tools.watch_collect import collect_once

logger = logging.getLogger(__name__)

COLLECT_INTERVAL_SECONDS = 300


def main() -> int:
    stop = threading.Event()

    def _handle_signal(signum, frame):  # noqa: ARG001 - 信号处理签名固定
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while not stop.is_set():
        try:
            result = collect_once()
            if result:
                print(result, flush=True)
        except Exception as exc:  # 兜底：collect_once 约定不抛，防御性再包一层
            print(f"watch loop error: {exc}", flush=True)
        stop.wait(COLLECT_INTERVAL_SECONDS)

    print("vigil watch: 收到停止信号，退出。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""生成 weekly_change_digest 的测试 fixture（state.db schema 子集）。

固定数据，不要依赖当前时间：

* 3 个窗口内会话（2026-10-21 / 10-23 / 10-25，Asia/Shanghai）
* 1 个窗口外会话（2026-10-10），用于证明窗口过滤生效
* 覆盖：write_file、写类 terminal 命令、只读工具、失败 tool 结果、
  unknown terminal 命令（unclassified）、异常 end_reason

运行：``python tests/fixtures/weekly_digest/build_fixture.py``
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SH = ZoneInfo("Asia/Shanghai")
OUT = Path(__file__).resolve().parent / "state.db"

SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    session_key TEXT,
    chat_id TEXT,
    chat_type TEXT,
    display_name TEXT,
    model TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    title TEXT,
    last_activity_at REAL
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    effect_disposition TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
"""


def ts(text: str) -> float:
    """Asia/Shanghai 时间字符串 -> unixepoch。"""
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH).timestamp()


def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def main() -> None:
    if OUT.exists():
        OUT.unlink()
    con = sqlite3.connect(OUT)
    con.executescript(SCHEMA)

    sessions = [
        {
            "id": "20261021_100000_aaaaaa",
            "source": "web",
            "display_name": "nginx 配置更新",
            "started_at": ts("2026-10-21 10:00:00"),
            "ended_at": ts("2026-10-21 10:42:00"),
            "end_reason": "turn_complete",
            "message_count": 7,
            "tool_call_count": 3,
            "input_tokens": 12000,
            "output_tokens": 3400,
        },
        {
            "id": "20261023_140000_bbbbbb",
            "source": "cli",
            "display_name": "harbor 升级检查",
            "started_at": ts("2026-10-23 14:00:00"),
            "ended_at": ts("2026-10-23 14:30:00"),
            "end_reason": "interrupted",
            "message_count": 5,
            "tool_call_count": 2,
            "input_tokens": 8000,
            "output_tokens": 1500,
        },
        {
            "id": "20261025_090000_cccccc",
            "source": "web",
            "display_name": "拓扑查询",
            "started_at": ts("2026-10-25 09:00:00"),
            "ended_at": ts("2026-10-25 09:20:00"),
            "end_reason": "cli_close",
            "message_count": 5,
            "tool_call_count": 2,
            "input_tokens": 5000,
            "output_tokens": 900,
        },
        {
            # 窗口外：用于证明不会混入 facts。
            "id": "20261010_120000_dddddd",
            "source": "web",
            "display_name": "窗口外会话",
            "started_at": ts("2026-10-10 12:00:00"),
            "ended_at": ts("2026-10-10 12:10:00"),
            "end_reason": "turn_complete",
            "message_count": 4,
            "tool_call_count": 1,
            "input_tokens": 3000,
            "output_tokens": 400,
        },
    ]
    for s in sessions:
        con.execute(
            """
            INSERT INTO sessions (id, source, display_name, model, started_at, ended_at,
                                  end_reason, status, message_count, tool_call_count,
                                  input_tokens, output_tokens, last_activity_at)
            VALUES (?, ?, ?, 'fixture-model', ?, ?, ?, 'ended', ?, ?, ?, ?, ?)
            """,
            (
                s["id"], s["source"], s["display_name"], s["started_at"], s["ended_at"],
                s["end_reason"], s["message_count"], s["tool_call_count"],
                s["input_tokens"], s["output_tokens"], s["ended_at"],
            ),
        )

    def msg(session_id: str, role: str, at: str, content=None, tool_calls=None, tool_name=None, tool_call_id=None):
        con.execute(
            """
            INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls,
                                  tool_name, effect_disposition, timestamp, active)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 1)
            """,
            (
                session_id, role, content,
                tool_call_id,
                json.dumps(tool_calls, ensure_ascii=False) if tool_calls is not None else None,
                tool_name, ts(at),
            ),
        )

    # 会话 A：write_file + systemctl restart + read_file
    msg("20261021_100000_aaaaaa", "user", "2026-10-21 10:00:10",
        "帮我把 nginx 配置更新一下并重启\n顺便确认配置语法")
    msg("20261021_100000_aaaaaa", "assistant", "2026-10-21 10:00:12", "好的",
        tool_calls=[
            tool_call("call_a1", "terminal", {"command": "systemctl restart nginx"}),
            tool_call("call_a2", "write_file", {"path": "/etc/nginx/conf.d/vigil.conf", "content": "server {}"}),
        ])
    msg("20261021_100000_aaaaaa", "tool", "2026-10-21 10:00:13",
        '{"output": "restarted", "exit_code": 0, "error": null}',
        tool_name="terminal", tool_call_id="call_a1")
    msg("20261021_100000_aaaaaa", "tool", "2026-10-21 10:00:14",
        '{"success": true}', tool_name="write_file", tool_call_id="call_a2")
    msg("20261021_100000_aaaaaa", "assistant", "2026-10-21 10:01:00", "",
        tool_calls=[tool_call("call_a3", "read_file", {"path": "/etc/nginx/nginx.conf"})])
    msg("20261021_100000_aaaaaa", "tool", "2026-10-21 10:01:01",
        '{"content": "worker_processes auto;"}', tool_name="read_file", tool_call_id="call_a3")
    msg("20261021_100000_aaaaaa", "assistant", "2026-10-21 10:02:00", "已完成")

    # 会话 B：query + unknown（unclassified）+ 失败 tool 结果 + interrupted
    msg("20261023_140000_bbbbbb", "user", "2026-10-23 14:00:10",
        "查一下 harbor 的容器状态，然后跑个升级")
    msg("20261023_140000_bbbbbb", "assistant", "2026-10-23 14:00:12", "",
        tool_calls=[
            tool_call("call_b1", "terminal", {"command": "docker ps | grep harbor"}),
            tool_call("call_b2", "terminal", {"command": "frobnicate --upgrade-now"}),
        ])
    msg("20261023_140000_bbbbbb", "tool", "2026-10-23 14:00:13",
        '{"output": "harbor running", "exit_code": 0}', tool_name="terminal", tool_call_id="call_b1")
    msg("20261023_140000_bbbbbb", "tool", "2026-10-23 14:00:14",
        '{"error": "command not found: frobnicate"}', tool_name="terminal", tool_call_id="call_b2")
    msg("20261023_140000_bbbbbb", "assistant", "2026-10-23 14:01:00", "升级命令不存在")

    # 会话 C：只读（topo_query + read_file），正常关闭
    msg("20261025_090000_cccccc", "user", "2026-10-25 09:00:10", "看看拓扑里 harbor 的状态")
    msg("20261025_090000_cccccc", "assistant", "2026-10-25 09:00:12", "",
        tool_calls=[
            tool_call("call_c1", "topo_query", {"name": "harbor"}),
            tool_call("call_c2", "read_file", {"path": "/tmp/harbor.txt"}),
        ])
    msg("20261025_090000_cccccc", "tool", "2026-10-25 09:00:13",
        '{"success": true, "data": [{"name": "harbor", "env": "prod"}]}',
        tool_name="topo_query", tool_call_id="call_c1")
    msg("20261025_090000_cccccc", "tool", "2026-10-25 09:00:14",
        '{"content": "harbor"}', tool_name="read_file", tool_call_id="call_c2")
    msg("20261025_090000_cccccc", "assistant", "2026-10-25 09:01:00", "harbor 正常")

    # 会话 D（窗口外）：包含一次写操作，必须不出现在窗口 facts 里
    msg("20261010_120000_dddddd", "user", "2026-10-10 12:00:10", "清理测试命名空间")
    msg("20261010_120000_dddddd", "assistant", "2026-10-10 12:00:12", "",
        tool_calls=[tool_call("call_d1", "terminal", {"command": "kubectl delete namespace harbor"})])
    msg("20261010_120000_dddddd", "tool", "2026-10-10 12:00:13",
        '{"output": "deleted", "exit_code": 0}', tool_name="terminal", tool_call_id="call_d1")
    msg("20261010_120000_dddddd", "assistant", "2026-10-10 12:01:00", "已删除")

    con.commit()
    con.close()
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()

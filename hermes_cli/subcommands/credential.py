"""``vigil credential`` —— 本机凭据保险箱的最小管理入口。

只做 store/list/expire，不提供读取/打印明文的命令。值可通过隐藏输入
（默认）或 ``--stdin``（管道/自动化）写入，落在 ``<VIGIL_HOME>/secrets/``
（0600 + owner 校验，见 tools/credential_vault.py）。
"""

from __future__ import annotations

from typing import Callable


def build_credential_parser(subparsers, *, cmd_credential: Callable) -> None:
    parser = subparsers.add_parser(
        "credential",
        help="本机凭据保险箱（store/list/expire，不打印明文）",
        description=(
            "管理 credential_vault 里的凭据引用。digest 等内置功能只读引用名，"
            "不把 token 写进 config.yaml / jobs.json / 日志。"
        ),
    )
    sub = parser.add_subparsers(dest="credential_command", metavar="<subcommand>")

    store = sub.add_parser("store", help="写入/更新一个凭据（隐藏输入）")
    store.add_argument("name", help="凭据名（[A-Za-z0-9._-]+）")
    store.add_argument(
        "--stdin", action="store_true",
        help="从 stdin 读取值（管道/自动化；仍不打印）",
    )
    store.set_defaults(func=cmd_credential)

    list_parser = sub.add_parser("list", help="列出凭据名（不读值）")
    list_parser.set_defaults(func=cmd_credential)

    expire = sub.add_parser("expire", help="删除一个凭据并清来源标记")
    expire.add_argument("name", help="凭据名")
    expire.set_defaults(func=cmd_credential)

    parser.set_defaults(func=cmd_credential)

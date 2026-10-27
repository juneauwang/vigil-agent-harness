"""``vigil credential`` handler（不打印明文）。"""

from __future__ import annotations

import getpass
import sys


def cmd_credential(args) -> int:
    sub = getattr(args, "credential_command", None)
    from tools.credential_vault import expire, list_names, store

    if sub == "store":
        name = str(getattr(args, "name", "") or "").strip()
        if not name:
            print("credential: 需要凭据名", file=sys.stderr)
            return 2
        if getattr(args, "stdin", False):
            value = sys.stdin.read().strip()
        else:
            value = getpass.getpass("Credential value: ").strip()
        if not value:
            print("credential: 值为空，未写入", file=sys.stderr)
            return 2
        try:
            store(name, value)
        except Exception as exc:
            print(f"credential: 写入失败：{exc}", file=sys.stderr)
            return 1
        print(f"stored: {name}")
        return 0

    if sub == "list":
        names = list_names()
        if not names:
            print("vault is empty")
        for name in names:
            print(name)
        return 0

    if sub == "expire":
        name = str(getattr(args, "name", "") or "").strip()
        if not name:
            print("credential: 需要凭据名", file=sys.stderr)
            return 2
        try:
            removed = expire(name)
        except Exception as exc:
            print(f"credential: 删除失败：{exc}", file=sys.stderr)
            return 1
        print(f"expired: {name}" if removed else f"not found: {name}")
        return 0

    print("usage: vigil credential {store <name>|list|expire <name>}", file=sys.stderr)
    return 2

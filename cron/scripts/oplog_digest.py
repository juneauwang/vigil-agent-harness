#!/usr/bin/env python3
"""兼容 shim：旧 cron job 的 ``--script oplog_digest.py`` 仍指向这里。

实现已搬到 ``ops/digest.py``（随 wheel 分发）；这里只保留入口，不复制逻辑。
"""

from __future__ import annotations

import sys

from ops.digest import main


if __name__ == "__main__":
    sys.exit(main())

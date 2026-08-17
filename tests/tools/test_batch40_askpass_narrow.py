"""批次四十（§AS B1）：askpass 正则收窄——ops 诊断文档不再误伤。

覆盖：
  - 原误伤用例（echo "$(cat $t/comm) $(grep '^State' $t/status)"）放行；
  - 真 askpass 形态（shebang + echo 裸值 / printf 裸值 / 无 shebang 裸值）
    在受保护目录仍拦截（安全底线不回退）；
  - 短值、非受保护目录、含空白/命令拼接的值不误伤。
"""

from __future__ import annotations

import os

import pytest

import hermes_cli.config as hc
from tools.file_tools import write_file_tool


@pytest.fixture(autouse=True)
def _home_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    yield


class TestNarrowedDetection:
    def test_dogfood_diagnostic_echo_passes(self):
        """§AS B1 原误伤：runbook 诊断命令含 $(cat ...) $(grep ...) → 放行。"""
        target = os.path.join(os.environ["VIGIL_HOME"], "runbooks", "diag.yaml")
        content = "echo \"$(cat $t/comm) $(grep '^State' $t/status)\"\n"
        res = write_file_tool(target, content, task_id="t1")
        assert "拒绝" not in res

    def test_diagnostic_echo_with_pipe_passes(self):
        target = os.path.join(os.environ["VIGIL_HOME"], "notes", "diag.txt")
        content = "echo 'cpu 8 cores' | tee /tmp/cpu.txt\n"
        res = write_file_tool(target, content, task_id="t1")
        assert "拒绝" not in res

    def test_echo_with_spaces_passes(self):
        target = os.path.join(os.environ["VIGIL_HOME"], "notes", "note.txt")
        content = "echo 'this is a normal sentence'\n"
        res = write_file_tool(target, content, task_id="t1")
        assert "拒绝" not in res


class TestTrueAskpassStillBlocked:
    @pytest.mark.parametrize(
        "path,content",
        [
            (os.path.expanduser("~/.vigil/tmp-askpass-sudo"),
             "#!/bin/sh\necho 'wwplove815'\n"),
            (os.path.expanduser("~/.vigil/ask.sh"), "echo \"wwplove815\"\n"),
            (os.path.expanduser("~/credential/sudo_credential"),
             "echo 'wwplove815'"),
            (os.path.expanduser("~/credential/askpass"),
             "#!/bin/sh\nprintf '%s' 'wwplove815'\n"),
            (os.path.expanduser("~/.vigil/secrets/sudo-pw"),
             "echo 'wwplove815'\n"),
        ],
    )
    def test_bare_credential_echo_still_blocked(self, path, content):
        res = write_file_tool(path, content, task_id="t1")
        assert "拒绝" in res
        assert "credential_vault" in res and "sudo_exec" in res

    def test_short_value_not_blocked(self):
        res = write_file_tool(os.path.expanduser("~/.vigil/x"), "echo 'hi'\n", task_id="t1")
        assert "拒绝" not in res

    def test_non_protected_path_not_blocked(self):
        import tempfile

        outside = tempfile.mkdtemp(prefix="outside-vigil-")
        target = os.path.join(outside, "script.sh")
        res = write_file_tool(target, "echo 'wwplove815'\n", task_id="t1")
        assert "拒绝" not in res

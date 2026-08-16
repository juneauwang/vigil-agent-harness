"""OPS-DELTA 批次三十二 — 裸 askpass 脚本/裸凭据落盘拦截。

覆盖：
  - write_file：内容含 ``echo '<8-32 字符>'`` 且路径在 ~/.vigil 或 ~/credential/
    下 → 拦截并引导受控通道（redact 之前的原始内容检测）；
  - terminal：echo/printf 裸值写进 ~/.vigil 或 ~/credential/ → hardline 硬拒
    （批十九 sudoers.d 同款拦截风格）；
  - 良性路径（普通文件、短值、非受保护目录）不受影响。
"""

from __future__ import annotations

import os

import pytest

from tools import file_tools
from tools.approval import check_dangerous_command
from tools.file_tools import write_file_tool


@pytest.fixture(autouse=True)
def _home_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    import hermes_cli.config as hc
    hc._LOAD_CONFIG_CACHE.clear()
    yield


class TestWriteFileInterception:
    @pytest.mark.parametrize(
        "path,content",
        [
            (os.path.expanduser("~/.vigil/tmp-askpass-sudo"), "#!/bin/sh\necho 'wwplove815'\n"),
            (os.path.expanduser("~/.vigil/ask.sh"), "echo \"wwplove815\"\n"),
            (os.path.expanduser("~/credential/sudo_credential"), "echo 'wwplove815'"),
            (os.path.expanduser("~/credential/askpass"), "#!/bin/sh\nprintf '%s' 'wwplove815'\n"),
        ],
    )
    def test_askpass_write_blocked(self, path, content):
        res = write_file_tool(path, content, task_id="t1")
        assert "拒绝" in res
        assert "credential_vault" in res and "sudo_exec" in res

    def test_short_value_not_blocked(self, tmp_path):
        res = write_file_tool(os.path.expanduser("~/.vigil/x"), "echo 'hi'\n", task_id="t1")
        assert "拒绝" not in res

    def test_non_protected_path_not_blocked(self, tmp_path):
        import tempfile
        outside = tempfile.mkdtemp(prefix="outside-vigil-")
        target = os.path.join(outside, "script.sh")
        res = write_file_tool(target, "echo 'wwplove815'\n", task_id="t1")
        assert "拒绝" not in res

    def test_vault_secrets_dir_still_protected(self):
        """~/.vigil/secrets 下 echo 裸值同样拦截（绕过 vault 程序化写入的形态）。"""
        res = write_file_tool(
            os.path.expanduser("~/.vigil/secrets/sudo-pw"),
            "echo 'wwplove815'\n",
            task_id="t1",
        )
        assert "拒绝" in res


class TestTerminalHardline:
    @pytest.mark.parametrize(
        "cmd",
        [
            "echo 'wwplove815' > ~/.vigil/tmp-askpass-sudo",
            'echo "wwplove815" >> ~/credential/sudo_credential',
            "printf '%s\\n' 'wwplove815' > ~/.vigil/askpass",
            "echo 'wwplove815' > $HOME/.vigil/x",
        ],
    )
    def test_askpass_write_hardline_blocked(self, cmd):
        r = check_dangerous_command(cmd, "local")
        assert r["approved"] is False
        assert r.get("hardline") is True
        assert "sudo_exec" in r["message"]

    @pytest.mark.parametrize(
        "cmd",
        [
            "echo 'hello' > ~/.vigil/note.txt",   # 短值（非 8-32 密码形态）
            "cat ~/.vigil/config.yaml",           # 读操作合法
            "echo 'wwplove815' > /tmp/x",          # 非受保护目录
            "echo 'wwplove815' > ~/credential_backup/x",  # 目录名带前缀不算
            "echo 'wwplove815' | tee ~/.vigil/x",  # 管道形态不在本批硬拒范围
        ],
    )
    def test_benign_commands_pass(self, cmd):
        r = check_dangerous_command(cmd, "local")
        assert r["approved"] is True, cmd

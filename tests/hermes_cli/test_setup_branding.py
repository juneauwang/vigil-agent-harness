"""OPS-DELTA #3 — setup 引导品牌化 + 工具推荐裁剪（批次六）。

1. 向导欢迎框是 Vigil 运维定位（Welcome to Vigil + topology/runbooks/gates），
   不再是无定位的 Hermes 通用安装文案；
2. 默认预选只含运维/基础工具集，浏览器/图像/桌面自动化等非运维工具默认关闭
   （用户仍可在 `vigil tools` 手动启用——显式配置路径不受影响）。
"""

import pytest

from hermes_cli.setup import _wizard_welcome_box_lines
from hermes_cli.tools_config import _DEFAULT_OFF_TOOLSETS, _get_platform_tools


class TestWizardWelcomeBox:
    def test_vigil_branding_present(self):
        box = "\n".join(_wizard_welcome_box_lines())
        assert "Vigil Agent Setup Wizard" in box
        assert "Welcome to Vigil" in box

    def test_ops_positioning_tagline_present(self):
        box = "\n".join(_wizard_welcome_box_lines())
        # 与 banner/皮肤欢迎语一致（skin_engine welcome 文本）
        assert "topology loaded, runbooks ready" in box
        assert "permission gates armed" in box

    def test_no_hermes_command_residue(self):
        box = "\n".join(_wizard_welcome_box_lines())
        assert "hermes setup" not in box.lower()
        assert "hermes " not in box.lower()


_OPS_CORE_TOOLSETS = {
    "terminal", "file", "code_execution", "skills", "memory", "todo",
    "web", "topo", "runbook",
}
_NON_OPS_TOOLSETS = {
    "browser", "image_gen", "computer_use", "video", "video_gen",
    "homeassistant", "spotify",
}


class TestDefaultToolPreselection:
    def test_default_cli_preselects_ops_tools(self):
        enabled = _get_platform_tools({}, "cli")
        assert _OPS_CORE_TOOLSETS <= enabled

    def test_default_cli_excludes_non_ops_tools(self):
        enabled = _get_platform_tools({}, "cli")
        assert not (_NON_OPS_TOOLSETS & enabled)

    def test_non_ops_tools_are_in_default_off_set(self):
        # 机制层面：非运维工具必须登记在默认关闭集合里（浏览器/图像/桌面/娱乐）
        assert _NON_OPS_TOOLSETS <= _DEFAULT_OFF_TOOLSETS


class TestManualEnablePath:
    @pytest.mark.parametrize("ts", ["browser", "image_gen", "computer_use"])
    def test_explicit_config_still_enables_non_ops_tool(self, ts):
        # 用户显式保存工具集列表 → 默认关闭不生效（手动启用路径不受影响）
        enabled = _get_platform_tools({"platform_toolsets": {"cli": [ts]}}, "cli")
        assert ts in enabled

"""Static dashboard tests for the Profiles navigation copy."""
from pathlib import Path

import pytest


_EN_I18N = Path(__file__).resolve().parents[2] / "web" / "src" / "i18n" / "en.ts"


# 本 fork 的 web 前端没有上游 hermes 的 i18n 层（dashboard 页面为
# Approvals/Audit/Incidents/Matrix/Monitoring/Overview/Runbooks/Topology，
# 无 Profiles 导航、无 src/i18n/ 目录）——该文件不存在时测试无对象可断言。
# 保留文件存在时的断言，缺失时显式 skip 并说明原因。
pytestmark = pytest.mark.skipif(
    not _EN_I18N.is_file(),
    reason="web/src/i18n/en.ts 不存在：本 fork 的 dashboard 前端无 i18n 层/Profiles 导航",
)


def test_profiles_nav_label_uses_short_copy():
    content = _EN_I18N.read_text(encoding="utf-8")

    # Nav label should be the clean short form, not the old verbose string
    assert 'profiles: "Profiles"' in content
    assert "profiles : multi agents" not in content

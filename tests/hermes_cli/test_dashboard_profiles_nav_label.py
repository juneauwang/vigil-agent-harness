"""Static dashboard tests for the Profiles navigation copy."""
import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_EN_I18N = _REPO_ROOT / "web" / "src" / "i18n" / "en.ts"
_APP_TSX = _REPO_ROOT / "web" / "src" / "App.tsx"

# 断言的主体是「Profiles 导航项的文案」，而导航项声明在 web/src/App.tsx 的
# NAV_ITEMS 表里（label 形如 "common.nav.<page>"，路由形如 path="/<page>"）。
# 上游那条守卫却只看 i18n 文件在不在：本 fork 后来加了自己的 i18n 层
# （web/src/i18n/{en,zh}.ts 存在），但导航表里**没有** Profiles 页、
# catalog 里也没有该键 ⇒ 守卫前提失效，测试断言一个不存在的对象而必失败。
# 正确的判据是「本 fork 有没有这个导航页」——判在导航表/路由上，而不是判在
# 文件存在性上，这样才有意义：将来若真加了 Profiles 页却漏了/写错了文案，
# 导航项存在 ⇒ 守卫放行 ⇒ 下面两条断言会真失败（真失败不会被 skip 吞掉）。
_PROFILES_NAV_RE = re.compile(
    r"""(?:common\.nav\.profiles|path\s*[:=]\s*["']/profiles["'])"""
)


def _fork_ships_profiles_nav() -> bool:
    """True only when this fork's dashboard actually has a Profiles page."""
    if not _APP_TSX.is_file():
        return False
    return _PROFILES_NAV_RE.search(_APP_TSX.read_text(encoding="utf-8")) is not None


pytestmark = pytest.mark.skipif(
    not _fork_ships_profiles_nav(),
    reason=(
        "本 fork 的 dashboard 导航表（web/src/App.tsx NAV_ITEMS）没有 Profiles 页，"
        "无断言对象"
    ),
)


def test_profiles_nav_label_uses_short_copy():
    assert _EN_I18N.is_file(), (
        "导航表声明了 Profiles 页但 i18n catalog 缺失 —— 这是真失败，不是无对象可断言"
    )

    content = _EN_I18N.read_text(encoding="utf-8")

    # Nav label should be the clean short form, not the old verbose string
    assert 'profiles: "Profiles"' in content
    assert "profiles : multi agents" not in content

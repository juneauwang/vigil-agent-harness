"""Skin asset paths must be bound to the Vigil-native data root (#40).

A machine that previously ran upstream Hermes can leave ``~/.hermes`` behind.
Skin 是用户可见的品牌表面：读错目录会让 Vigil 展示 Hermes 的皮肤残留。
These tests pin the skin directory to the Vigil-native root unless an explicit
``VIGIL_HOME`` is set.
"""

import pytest

import hermes_constants as hc
from hermes_cli import skin_cmd, skin_engine


@pytest.fixture(autouse=True)
def _clean_data_root_env(tmp_path, monkeypatch):
    monkeypatch.delenv("VIGIL_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    hc._legacy_skin_warned = False
    hc._legacy_fallback_warned = False
    yield


def test_skin_dir_ignores_hermes_home_leftover(tmp_path, monkeypatch):
    fake_hermes = tmp_path / "hermes_legacy"
    fake_skins = fake_hermes / "skins"
    fake_skins.mkdir(parents=True)
    (fake_skins / "leftover.yaml").write_text("name: leftover\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(fake_hermes))

    assert skin_cmd._skins_dir() == tmp_path / "home" / ".vigil" / "skins"

    names = [s["name"] for s in skin_engine.list_skins()]
    assert "leftover" not in names


def test_skin_dir_follows_explicit_vigil_home(tmp_path, monkeypatch):
    custom = tmp_path / "vigil_custom"
    monkeypatch.setenv("VIGIL_HOME", str(custom))

    assert skin_cmd._skins_dir() == custom / "skins"
    assert skin_engine._skins_dir() == custom / "skins"


def test_legacy_data_root_warns_once_and_still_reads_native(tmp_path, capsys):
    legacy = tmp_path / "home" / ".hermes"
    (legacy / "profiles" / "ops").mkdir(parents=True)
    (legacy / "skins").mkdir()
    (legacy / "skins" / "old.yaml").write_text("name: old\n", encoding="utf-8")

    assert skin_cmd._skins_dir() == tmp_path / "home" / ".vigil" / "skins"
    assert skin_cmd._skins_dir() == tmp_path / "home" / ".vigil" / "skins"

    err = capsys.readouterr().err
    assert "检测到旧 Hermes 数据根" in err
    assert err.count("检测到旧 Hermes 数据根") == 1

    names = [s["name"] for s in skin_engine.list_skins()]
    assert "old" not in names

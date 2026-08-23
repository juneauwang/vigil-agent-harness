"""Skin asset paths must be bound to the Vigil-native data root (#40).

VIGIL_HOME 兼容已删除（OPS-DELTA #37）：skin 目录只认显式 ``VIGIL_HOME``，
否则固定 ``~/.vigil``。旧 ``~/.vigil`` 残留（含 profiles/ops 特征）不再参与，
也不再有任何 legacy 告警。
"""

import pytest

from hermes_cli import skin_cmd, skin_engine


@pytest.fixture(autouse=True)
def _clean_data_root_env(tmp_path, monkeypatch):
    monkeypatch.delenv("VIGIL_HOME", raising=False)
    monkeypatch.delenv("VIGIL_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    yield


def test_skin_dir_ignores_hermes_home_leftover(tmp_path, monkeypatch):
    """旧 ~/.hermes 残留（上游旧数据根名）不影响 skin：目录固定 ~/.vigil/skins。"""
    fake_hermes = tmp_path / "home" / ".hermes"
    fake_skins = fake_hermes / "skins"
    fake_skins.mkdir(parents=True)
    (fake_skins / "leftover.yaml").write_text("name: leftover\n", encoding="utf-8")

    assert skin_cmd._skins_dir() == tmp_path / "home" / ".vigil" / "skins"

    names = [s["name"] for s in skin_engine.list_skins()]
    assert "leftover" not in names


def test_skin_dir_follows_explicit_vigil_home(tmp_path, monkeypatch):
    custom = tmp_path / "vigil_custom"
    monkeypatch.setenv("VIGIL_HOME", str(custom))

    assert skin_cmd._skins_dir() == custom / "skins"
    assert skin_engine._skins_dir() == custom / "skins"


def test_legacy_data_root_no_warning_no_impact(tmp_path, monkeypatch, capsys):
    """旧 ~/.hermes（含 profiles/ops 特征）→ 无告警、无影响，skin 仍读 ~/.vigil。"""
    legacy = tmp_path / "home" / ".hermes"
    (legacy / "profiles" / "ops").mkdir(parents=True)
    (legacy / "skins").mkdir()
    (legacy / "skins" / "old.yaml").write_text("name: old\n", encoding="utf-8")

    assert skin_cmd._skins_dir() == tmp_path / "home" / ".vigil" / "skins"
    assert skin_cmd._skins_dir() == tmp_path / "home" / ".vigil" / "skins"

    err = capsys.readouterr().err
    assert "检测到旧 Vigil 数据根" not in err
    assert "检测到旧 Hermes 数据根" not in err

    names = [s["name"] for s in skin_engine.list_skins()]
    assert "old" not in names

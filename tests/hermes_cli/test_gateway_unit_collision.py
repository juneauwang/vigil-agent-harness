"""unit 名撞名的回归测试（2026-10-27 飞书网关静默 39 小时事故）。

背景：Vigil 把 ``~/.vigil`` 映射成 unit 名 ``hermes-gateway``，与上游 Hermes 的
``~/.hermes`` 算出同一个文件。谁后装谁改写，对方下一次 restart 起来的是另一个
产品，且全程不报错。修复分两层：

1. 名字按产品分：Vigil → ``vigil-gateway``，上游仍 ``hermes-gateway``；
2. 写 unit 之前先看归属：不是本 home 的 unit 一律拒绝改写，旧名字且同 home 的
   才做一次性迁移（备份后移除）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_cli.gateway as gateway_cli


def _unit_text(home: str, *, exec_start: str = "/opt/x/bin/python -m hermes_cli.main gateway run") -> str:
    return (
        "[Unit]\n"
        "Description=Gateway\n"
        "\n"
        "[Service]\n"
        f'Environment="VIGIL_HOME={home}"\n'
        f"ExecStart={exec_start}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


@pytest.fixture
def fake_unit_path(tmp_path, monkeypatch):
    """把 unit 路径指到 tmp，避免碰开发者真实 systemd 目录。"""
    path = tmp_path / "systemd" / "hermes-gateway.service"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: path)
    return path


class TestServiceNameIsProductScoped:
    def test_vigil_home_uses_vigil_base(self, tmp_path, monkeypatch):
        home = tmp_path / ".vigil"
        home.mkdir()
        monkeypatch.setenv("VIGIL_HOME", str(home))
        assert gateway_cli.get_service_name() == "vigil-gateway"

    def test_upstream_home_keeps_hermes_base(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("VIGIL_HOME", str(home))
        assert gateway_cli.get_service_name() == "hermes-gateway"

    def test_vigil_profile_appends_profile_suffix(self, tmp_path, monkeypatch):
        profile = tmp_path / ".vigil" / "profiles" / "coder"
        profile.mkdir(parents=True)
        monkeypatch.setenv("VIGIL_HOME", str(profile))
        assert gateway_cli.get_service_name() == "vigil-gateway-coder"


class TestUnitOwnership:
    def test_owner_home_parsed_from_environment(self, tmp_path):
        unit = tmp_path / "u.service"
        unit.write_text(_unit_text("/srv/other"), encoding="utf-8")
        assert gateway_cli._unit_env_home(unit.read_text(encoding="utf-8")) == "/srv/other"

    def test_foreign_unit_is_not_ours(self, tmp_path, monkeypatch):
        unit = tmp_path / "u.service"
        unit.write_text(_unit_text("/srv/other"), encoding="utf-8")
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: Path("/home/me/.vigil"))
        assert gateway_cli._unit_belongs_to_current_home(unit) is False

    def test_unit_without_home_is_not_ours(self, tmp_path, monkeypatch):
        unit = tmp_path / "u.service"
        unit.write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: Path("/home/me/.vigil"))
        assert gateway_cli._unit_belongs_to_current_home(unit) is False

    def test_same_home_unit_is_ours(self, tmp_path, monkeypatch):
        unit = tmp_path / "u.service"
        unit.write_text(_unit_text("/home/me/.vigil"), encoding="utf-8")
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: Path("/home/me/.vigil"))
        assert gateway_cli._unit_belongs_to_current_home(unit) is True


class TestRefreshRefusesForeignKey:
    def test_refresh_leaves_foreign_unit_untouched(self, fake_unit_path, monkeypatch, capsys):
        original = _unit_text("/home/me/.hermes")
        fake_unit_path.write_text(original, encoding="utf-8")
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: Path("/home/me/.vigil"))
        monkeypatch.setattr(
            gateway_cli,
            "generate_systemd_unit",
            lambda **_: pytest.fail("不得为别人的 unit 生成内容"),
        )

        assert gateway_cli.refresh_systemd_unit_if_needed() is False
        assert fake_unit_path.read_text(encoding="utf-8") == original
        assert "属于另一个 home" in capsys.readouterr().out


class TestAdoptLegacyUnit:
    def test_adoptable_detected_for_same_home(self, tmp_path, monkeypatch):
        home = tmp_path / ".vigil"
        home.mkdir()
        monkeypatch.setenv("VIGIL_HOME", str(home))
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: home)
        # 当前名 vigil-gateway，旧名 hermes-gateway
        current = tmp_path / "systemd" / "vigil-gateway.service"
        legacy = tmp_path / "systemd" / "hermes-gateway.service"
        current.parent.mkdir(parents=True)
        legacy.write_text(_unit_text(str(home)), encoding="utf-8")
        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: current)
        monkeypatch.setattr(
            gateway_cli,
            "_legacy_unit_search_paths",
            lambda: [(False, current.parent)],
        )
        found = gateway_cli._adoptable_legacy_unit()
        assert found is not None
        assert found[0] == "hermes-gateway"

    def test_foreign_legacy_unit_is_not_adopted(self, tmp_path, monkeypatch):
        home = tmp_path / ".vigil"
        home.mkdir()
        monkeypatch.setenv("VIGIL_HOME", str(home))
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: home)
        current = tmp_path / "systemd" / "vigil-gateway.service"
        legacy = tmp_path / "systemd" / "hermes-gateway.service"
        current.parent.mkdir(parents=True)
        legacy.write_text(_unit_text("/home/me/.hermes"), encoding="utf-8")
        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: current)
        monkeypatch.setattr(
            gateway_cli,
            "_legacy_unit_search_paths",
            lambda: [(False, current.parent)],
        )
        assert gateway_cli._adoptable_legacy_unit() is None


class TestInstallRefusesForeignUnit:
    def test_install_does_not_overwrite_other_products_unit(
        self, fake_unit_path, monkeypatch, capsys
    ):
        original = _unit_text("/home/me/.hermes")
        fake_unit_path.write_text(original, encoding="utf-8")
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: Path("/home/me/.vigil"))
        monkeypatch.setattr(gateway_cli, "_adoptable_legacy_unit", lambda system=False: None)
        monkeypatch.setattr(gateway_cli, "_run_systemctl", lambda *a, **k: None)

        gateway_cli.systemd_install(non_interactive=True)

        assert fake_unit_path.read_text(encoding="utf-8") == original
        assert "拒绝覆盖" in capsys.readouterr().out

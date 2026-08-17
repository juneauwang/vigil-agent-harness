"""批次四十（§AS C1）：``config set`` list/dict 值修复——禁止静默写坏配置。

覆盖：
  - ``platform_toolsets.cli`` 传 YAML list 字符串 → 落盘为真实 list；
  - 目标 key 默认是 list/dict 而传入标量 → 拒绝并引导 vigil config edit
    （不再 str 落库后被下游当字符列表遍历）；
  - bool/int/float 既有强转行为回归（str-typed key 不受 YAML 解析影响）。
"""

from __future__ import annotations

import os

import pytest
import yaml

import hermes_cli.config as hc


@pytest.fixture(autouse=True)
def _home_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    cfg_path = os.path.join(str(tmp_path), "config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump({"_config_version": hc.DEFAULT_CONFIG["_config_version"]}, f)
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()


def _raw(tmp_path) -> dict:
    with open(os.path.join(str(tmp_path), "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class TestListValueSet:
    def test_yaml_list_saved_as_list(self, tmp_path):
        hc.set_config_value("platform_toolsets.cli", '["clarify", "terminal"]')
        raw = _raw(tmp_path)
        cli = raw["platform_toolsets"]["cli"]
        assert isinstance(cli, list)
        assert cli == ["clarify", "terminal"]

    def test_yaml_dict_saved_as_dict(self, tmp_path):
        """list-default key 传入 YAML dict → 按解析结构落盘（dict 分支）。"""
        hc.set_config_value("platform_toolsets.cli", '{"custom": ["a"]}')
        raw = _raw(tmp_path)
        cli = raw["platform_toolsets"]["cli"]
        assert isinstance(cli, dict)
        assert cli == {"custom": ["a"]}

    def test_scalar_for_list_key_rejected(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            hc.set_config_value("platform_toolsets.cli", "hermes-cli")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "expects a list or mapping" in err
        assert "vigil config edit" in err
        # 未被静默写坏
        assert "platform_toolsets" not in _raw(tmp_path)

    def test_scalar_list_for_list_key_rejected(self, tmp_path):
        """即使带引号也是标量形态 → 拒绝。"""
        with pytest.raises(SystemExit):
            hc.set_config_value("platform_toolsets.cli", '["broken"')

    def test_bool_int_float_regression(self, tmp_path):
        hc.set_config_value("display.cli_refresh_interval", "0.5")
        assert _raw(tmp_path)["display"]["cli_refresh_interval"] == 0.5
        hc.set_config_value("agent.max_iterations", "42")
        assert _raw(tmp_path)["agent"]["max_iterations"] == 42
        hc.set_config_value("approvals.mode", "off")
        # str-typed key 不受 YAML 解析影响（"off" 保持字符串）
        assert _raw(tmp_path)["approvals"]["mode"] == "off"

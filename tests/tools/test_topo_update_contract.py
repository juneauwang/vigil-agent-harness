"""OPS-DELTA #33 — topo_update 参数契约校验测试。

覆盖：
  - updates 含 "attrs" 键 → 显式展开合并进顶层 attrs，不产生 attrs.attrs 嵌套；
  - 顶层字段（endpoint 等）与普通标量（custom）原逻辑不回归；
  - dict/list 复杂值（非白名单）→ 明确报错，不静默写入；
  - 返回消息含 "不需要包 attrs 层" 提示。
"""

from __future__ import annotations

import json

import pytest

from tools.topo_tools import topo_update

TOPO_YAML = """\
version: 1
environments:
  - name: prod
    isolation: strict
    role: prod
  - name: test
    isolation: relaxed
    role: test
core_entities:
  - name: web-test
    type: svc
    env: test
    endpoint: 1.2.3.4
    source: manual
"""


@pytest.fixture
def topo_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "entities").mkdir(parents=True)
    (home / "topology.yaml").write_text(TOPO_YAML, encoding="utf-8")
    (home / "entities" / "web-test.yaml").write_text(
        "name: web-test\nenv: test\nattrs:\n  version: v1\n", encoding="utf-8"
    )
    monkeypatch.setenv("VIGIL_HOME", str(home))
    import hermes_cli.config as _hc
    _hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        _hc._LOAD_CONFIG_CACHE.clear()


def _load(result: str) -> dict:
    return json.loads(result)


class TestAttrsExpansion:
    def test_attrs_key_expands_into_top_level(self, topo_home):
        result = _load(topo_update("web-test", {"attrs": {"version": "v2", "owner": "x"}}))
        assert result["status"] == "updated"
        assert "不需要包 attrs 层" in result.get("note", "")
        saved = (topo_home / "entities" / "web-test.yaml").read_text(encoding="utf-8")
        assert "version: v2" in saved
        assert "owner: x" in saved
        # 不产生 attrs.attrs 嵌套
        assert "attrs:\n  attrs:" not in saved

    def test_attrs_key_merges_existing_attrs(self, topo_home):
        topo_update("web-test", {"attrs": {"version": "v2"}})
        topo_update("web-test", {"attrs": {"status": "degraded"}})
        saved = (topo_home / "entities" / "web-test.yaml").read_text(encoding="utf-8")
        assert "version: v2" in saved
        assert "status: degraded" in saved
        assert saved.count("attrs:") == 1  # 仍只有一层 attrs

    def test_attrs_key_non_dict_rejected(self, topo_home):
        result = _load(topo_update("web-test", {"attrs": "oops"}))
        assert "error" in result
        assert "不需要包 attrs 层" in result["error"]
        saved = (topo_home / "entities" / "web-test.yaml").read_text(encoding="utf-8")
        assert "oops" not in saved


class TestTopLevelAndScalars:
    def test_top_level_field_unchanged(self, topo_home):
        result = _load(topo_update("web-test", {"endpoint": "5.6.7.8"}))
        assert result["status"] == "updated"
        saved = (topo_home / "entities" / "web-test.yaml").read_text(encoding="utf-8")
        assert "endpoint: 5.6.7.8" in saved

    def test_custom_scalar_goes_to_attrs(self, topo_home):
        topo_update("web-test", {"custom": "v"})
        saved = (topo_home / "entities" / "web-test.yaml").read_text(encoding="utf-8")
        assert "custom: v" in saved
        assert saved.count("attrs:") == 1


class TestComplexValueDefense:
    def test_nested_dict_rejected(self, topo_home):
        result = _load(topo_update("web-test", {"weird": {"nested": 1}}))
        assert "error" in result
        assert "复杂结构" in result["error"]
        saved = (topo_home / "entities" / "web-test.yaml").read_text(encoding="utf-8")
        assert "nested" not in saved

    def test_nested_list_rejected(self, topo_home):
        result = _load(topo_update("web-test", {"tags": ["a", "b"]}))
        assert "error" in result
        assert "复杂结构" in result["error"]

    def test_depends_on_list_still_allowed(self, topo_home):
        # depends_on 是已定义的顶层列表字段，允许
        result = _load(topo_update("web-test", {"depends_on": ["db"]}))
        assert result["status"] == "updated"
        saved = (topo_home / "entities" / "web-test.yaml").read_text(encoding="utf-8")
        assert "depends_on:" in saved
        assert "db" in saved

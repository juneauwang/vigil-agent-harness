"""Tests for hermes_cli.i18n — ops-console message catalog (zh default, en switch)."""

from __future__ import annotations

import pytest

from hermes_cli import i18n


ZD = "sudo_exec 执行超时（{n}s）：sudo {command}"  # zh default travelling with the call site


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("VIGIL_LANG", raising=False)
    i18n.reset_language_cache()
    yield
    i18n.reset_language_cache()


def test_default_lang_is_zh():
    """默认语言 zh：无 env / config 覆盖时返回调用点原文（含插值）。"""
    assert i18n.get_lang() == "zh"
    assert i18n.t("sudo.timeout", ZD, n=30, command="ls") == "sudo_exec 执行超时（30s）：sudo ls"


def test_en_switch_renders_catalog_entry(monkeypatch):
    """VIGIL_LANG=en → en 条目生效（含插值）。"""
    monkeypatch.setenv("VIGIL_LANG", "en")
    assert i18n.get_lang() == "en"
    assert i18n.t("sudo.timeout", ZD, n=30, command="ls") == "sudo_exec timed out (30s): sudo ls"


def test_missing_en_key_falls_back_to_zh(monkeypatch):
    """en 目录缺 key → 回到调用点中文原文（安全迁移，不漏 key 路径）。"""
    monkeypatch.setenv("VIGIL_LANG", "en")
    assert i18n.t("no.such.key", "原始中文 {n}", n=1) == "原始中文 1"


def test_lang_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("VIGIL_LANG", "en")
    assert i18n.t("sudo.timeout", ZD, lang="zh", n=5, command="id") == "sudo_exec 执行超时（5s）：sudo id"
    assert i18n.get_lang("en") == "en"


def test_unknown_lang_falls_safe_to_zh(monkeypatch):
    monkeypatch.setenv("VIGIL_LANG", "klingon")
    assert i18n.get_lang() == "zh"
    assert i18n.t("sudo.timeout", ZD, n=1, command="id") == "sudo_exec 执行超时（1s）：sudo id"


def test_monitoring_matcher_keys_present_in_en():
    """前端匹配器依赖的两条消息必须有 en 条目（双语言文本 OR 匹配）。"""
    assert "Prometheus not configured" in i18n._EN["monitoring.prom_unavailable"]
    assert "Alertmanager not configured" in i18n._EN["monitoring.alertmanager_unavailable"]


def test_slash_help_descriptions_bilingual(monkeypatch):
    """/env、/topo 描述：zh 默认 = 原中文；en = 英文翻译（commands.py 模块级
    t() 冻结语言——直接验证目录条目 + 显式 lang 渲染）。"""
    from hermes_cli.i18n import _EN

    # zh 走调用点原文（zh default 渲染回原中文）
    assert i18n.t("slash.env_help", "查看/切换会话操作环境", lang="zh") == "查看/切换会话操作环境"
    assert "In-session topology discovery" in _EN["slash.topo_help"]
    assert "--sudo-password" in _EN["slash.topo_help"]
    # en 渲染包含凭据纪律提示
    en_topo = i18n.t("slash.topo_help", "x", lang="en")
    assert "topo-discover --sudo-password" in en_topo


def test_boot_hints_and_usage_panel_keys():
    """boot.* 与 usage.*：zh 走调用点原文，en 条目存在且数字占位符一致。"""
    assert i18n.t("boot.loadingPlugins", "· 加载插件与工具…", lang="en") == "· Loading plugins and tools…"
    assert i18n.t("boot.ready", "· 启动就绪", lang="en") == "· Startup ready"
    # 数字格式在调用点 f-string 预格式化（{:,}），插值后逐字节保留
    zh_line = i18n.t(
        "usage.sessionSummary",
        "Tokens:         📊 本次会话: 输入 {inp} · 输出 {out} · 缓存 {cache} · reasoning {reasoning} · 总计 {total} tokens",
        inp="1,234", out="567", cache="0", reasoning="0", total="1,801",
    )
    assert zh_line == "Tokens:         📊 本次会话: 输入 1,234 · 输出 567 · 缓存 0 · reasoning 0 · 总计 1,801 tokens"
    en_line = i18n.t(
        "usage.sessionSummary", "x", lang="en",
        inp="1,234", out="567", cache="0", reasoning="0", total="1,801",
    )
    assert en_line == "Tokens:         📊 This session: input 1,234 · output 567 · cache 0 · reasoning 0 · total 1,801 tokens"
    assert i18n.t("usage.cacheHitRate", " · 缓存命中率 {rate}%", rate="37.5") == " · 缓存命中率 37.5%"
    assert i18n.t("usage.cacheHitRate", "x", lang="en", rate="37.5") == " · cache hit rate 37.5%"


def test_brace_doubled_literals_collapse_without_kwargs():
    """无参数消息中的 {{...}} 字面量也要折叠成单括号。"""
    assert i18n.t(
        "runbook_exec.matrix_level_required",
        "=强制人工（{{approve: required}}，不 smart 不 allowlist）",
    ) == "=强制人工（{approve: required}，不 smart 不 allowlist）"

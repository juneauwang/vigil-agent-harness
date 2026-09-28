"""Tests for the cronjob tool schema shape.

Guards the description text that flags ``schedule`` (and ``prompt``) as
REQUIRED for ``action=create`` — the load-bearing fix for description-driven
models (e.g. Grok) that omit schedule when the schema only lists ``action``
in ``required[]``. See issue #32427 / PR #32448.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    yield tmp_path


def test_cronjob_schema_action_description_flags_create_requirements():
    """`action` description must state schedule + prompt are required for create."""
    from tools.cronjob_tools import CRONJOB_SCHEMA

    action_desc = CRONJOB_SCHEMA["parameters"]["properties"]["action"]["description"]
    assert "action=create" in action_desc
    assert "schedule" in action_desc
    assert "REQUIRED" in action_desc


def test_tool_entry_forwards_attach_to_session(temp_home):
    """task40：经工具入口（registry handler）传 attach_to_session 必须落盘。

    同型漏：``CRONJOB_SCHEMA`` 声明了 ``attach_to_session``（面向 agent 的正常
    说明），``cronjob()`` 形参 + create/update 路径都处理它，但注册 handler 的
    lambda 曾漏读 → 经工具路径根本设不了"可续会话"任务。本用例只走工具入口。
    """
    import tools.cronjob_tools  # noqa: F401  (导入即向 registry 注册 cronjob)
    from tools.registry import registry
    from cron.jobs import get_job

    entry = registry.get_entry("cronjob")
    assert entry is not None
    out = json.loads(entry.handler({
        "action": "create",
        "prompt": "daily briefing",
        "schedule": "every 5m",
        "name": "attachable",
        "attach_to_session": True,
    }))
    assert out.get("success") is True, out

    stored = get_job(out["job_id"])
    assert stored is not None
    assert stored.get("attach_to_session") is True

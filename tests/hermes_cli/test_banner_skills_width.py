"""Tests for the unified console header — capability summary instead of skill inventory."""

import os
from unittest.mock import patch

from rich.console import Console

import hermes_cli.banner as banner
import model_tools
import tools.mcp_tool


def _build_banner_with_skills(skills_by_category, term_width=160):
    """Build the unified console header and return captured output."""
    state = {
        "ops_enabled": False, "env": "", "matrix_enabled": False,
        "topology_enabled": False, "runbook_enabled": False,
        "entity_count": 0, "runbook_count": 0,
        "profile": "default", "home": "/tmp",
    }
    with (
        patch.object(model_tools, "check_tool_availability", return_value=([], [])),
        patch.object(banner, "_load_banner_state", return_value=state),
        patch.object(banner, "get_available_skills", return_value=skills_by_category),
        patch.object(banner, "get_update_result", return_value=None),
        patch.object(banner, "get_latest_release_tag", return_value=None),
        patch.object(banner, "format_banner_version_label", return_value="Vigil v0.1.0 (test)"),
        patch.object(tools.mcp_tool, "get_mcp_status", return_value=[]),
        patch("shutil.get_terminal_size", return_value=os.terminal_size((term_width, 50))),
    ):
        console = Console(record=True, force_terminal=False, color_system=None, width=term_width)
        banner.build_welcome_banner(
            console=console,
            model="anthropic/test-model",
            cwd="/tmp/project",
            tools=[],
        )
        return console.export_text()


def test_skill_inventory_not_rendered_in_console_header():
    """The unified header drops the assistant-style skill list — skill names are gone."""
    skills = {"research": [f"skill-{i:02d}" for i in range(15)]}
    text = _build_banner_with_skills(skills, term_width=200)

    assert "skill-08" not in text
    assert "Available Skills" not in text


def test_capability_summary_counts_all_skills():
    """The non-ops capability line reports the total skill count."""
    skills = {"research": [f"skill-{i:02d}" for i in range(15)]}
    text = _build_banner_with_skills(skills, term_width=200)

    assert "15 skills" in text


def test_small_category_skills_counted_not_listed():
    """Small categories contribute their count without listing every name."""
    skills = {"security": ["auth", "vault"]}
    text = _build_banner_with_skills(skills, term_width=80)

    assert "auth" not in text
    assert "vault" not in text
    assert "2 skills" in text


def test_skill_count_aggregates_categories():
    """Skill counts aggregate across categories regardless of label width."""
    skills = {
        "very-long-category-name": [f"skill-{i:02d}" for i in range(10)],
        "sec": ["auth"],
    }
    text = _build_banner_with_skills(skills, term_width=120)

    assert "11 skills" in text

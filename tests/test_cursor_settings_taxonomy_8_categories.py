"""
Unit and integration tests for the 8 authentic Cursor settings categories:
- General (startup, notification, privacy, telemetry, logging)
- Appearance (color theme, agent conversation style, colors, typography, font, high contrast, motion)
- Agent (auto run, model, context length, composer, sandbox)
- Git & PRs (integration, stage, branch protection, PR review, commit generator)
- Worktrees (multi-worktree isolation, auto cleanup, workspace root, branch sync)
- Browser & Network (proxy routing, port forwarding, ssl verification, web search)
- Tab (cpp completion, suggestion delay, multiline, trigger chars)
- Code Intelligence (indexing engine, semantic search, symbol graph, embedding provider, exclude patterns)
"""
import os
import sys
import json
import pytest

# Add src to sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cursor_settings import CursorSettingsManager, DEFAULT_PROFILES
from server import app


@pytest.fixture
def temp_settings_mgr(tmp_path):
    """Create an isolated CursorSettingsManager instance with temporary files."""
    mgr = CursorSettingsManager()
    mgr.settings_file = str(tmp_path / "settings.json")
    mgr.profiles_file = str(tmp_path / "profiles.json")
    mgr.auto_switch_config_file = str(tmp_path / "auto_switch.json")
    return mgr


class TestCursorSettingsTaxonomy8Categories:
    """Validate 8 authentic Cursor settings categories and bidirectional sync."""

    EXPECTED_CATEGORIES = [
        "general",
        "appearance",
        "agent",
        "git_prs",
        "worktrees",
        "browser_network",
        "tab",
        "code_intelligence"
    ]

    def test_default_profiles_contains_all_8_categories(self):
        """Ensure DEFAULT_PROFILES includes all 8 authentic categories."""
        default_prof = DEFAULT_PROFILES.get("default", {})
        sections = default_prof.get("sections", {})
        for cat in self.EXPECTED_CATEGORIES:
            assert cat in sections, f"Category '{cat}' missing from default profile sections"

    def test_get_settings_returns_all_8_categories_in_sections(self, temp_settings_mgr):
        """Verify get_settings returns sections dictionary with all 8 categories."""
        settings = temp_settings_mgr.get_settings()
        assert "sections" in settings
        sections = settings["sections"]

        for cat in self.EXPECTED_CATEGORIES:
            assert cat in sections, f"Category '{cat}' missing from get_settings sections"

        # General category checks
        gen = sections["general"]
        assert "startup_behavior" in gen
        assert "notifications_enabled" in gen
        assert "privacy_mode" in gen
        assert "telemetry_enabled" in gen

        # Appearance category checks
        app_sec = sections["appearance"]
        assert "color_theme" in app_sec
        assert "agent_conversation_style" in app_sec
        assert "typography_font_family" in app_sec
        assert "typography_font_size" in app_sec
        assert "high_contrast" in app_sec
        assert "motion_reduced" in app_sec

        # Agent category checks
        agent = sections["agent"]
        assert "auto_run_commands" in agent
        assert "default_model" in agent
        assert "composer_mode" in agent

        # Git & PRs category checks
        git = sections["git_prs"]
        assert "git_integration" in git
        assert "pr_review_assistant" in git

        # Worktrees category checks
        wt = sections["worktrees"]
        assert "multi_worktree_isolation" in wt
        assert "auto_cleanup" in wt

        # Browser & Network category checks
        net = sections["browser_network"]
        assert "local_proxy_routed" in net
        assert "web_search_integration" in net

        # Tab category checks
        tab = sections["tab"]
        assert "cpp_enabled" in tab
        assert "suggestion_delay_ms" in tab

        # Code Intelligence category checks
        ci = sections["code_intelligence"]
        assert "indexing_engine" in ci
        assert "semantic_search_enabled" in ci
        assert "symbol_graph_enabled" in ci

    def test_update_settings_persists_across_all_8_categories(self, temp_settings_mgr):
        """Update fields across the 8 categories and ensure they persist."""
        update_payload = {
            "sections": {
                "general": {"startup_behavior": "restore_windows", "privacy_mode": True},
                "appearance": {"color_theme": "Cursor Dark", "typography_font_size": 16, "high_contrast": True},
                "agent": {"default_model": "claude-3-5-sonnet", "max_context_length": 128000},
                "git_prs": {"pr_review_assistant": True, "commit_message_generator": True},
                "worktrees": {"multi_worktree_isolation": True},
                "browser_network": {"web_search_integration": True},
                "tab": {"suggestion_delay_ms": 150},
                "code_intelligence": {"indexing_engine": "turbo_graph", "symbol_graph_enabled": True}
            }
        }
        res = temp_settings_mgr.update_settings(update_payload)
        assert res.get("success") is True

        refreshed = temp_settings_mgr.get_settings()
        sec = refreshed["sections"]

        assert sec["general"]["startup_behavior"] == "restore_windows"
        assert sec["general"]["privacy_mode"] is True
        assert sec["appearance"]["color_theme"] == "Cursor Dark"
        assert sec["appearance"]["typography_font_size"] == 16
        assert sec["appearance"]["high_contrast"] is True
        assert sec["agent"]["default_model"] == "claude-3-5-sonnet"
        assert sec["agent"]["max_context_length"] == 128000
        assert sec["git_prs"]["pr_review_assistant"] is True
        assert sec["worktrees"]["multi_worktree_isolation"] is True
        assert sec["browser_network"]["web_search_integration"] is True
        assert sec["tab"]["suggestion_delay_ms"] == 150
        assert sec["code_intelligence"]["indexing_engine"] == "turbo_graph"

    def test_api_cursor_settings_endpoint_returns_8_categories(self):
        """Verify GET /api/cursor/settings returns 200 with 8 categories."""
        client = app.test_client()
        resp = client.get("/api/cursor/settings")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "sections" in data
        for cat in self.EXPECTED_CATEGORIES:
            assert cat in data["sections"]

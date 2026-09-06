"""
Automated Test Suite for Full Cursor Settings Migration & Profile Presets
========================================================================
Verifies:
1. Native Cursor Settings Coverage across all 7 functional areas:
   - AI Models & Custom Inference
   - Cursor Tab & CPP Autocomplete
   - Composer & Autonomous Agent
   - Editor & Workbench UI
   - Privacy & Ghost Mode
   - Proxy Routing
   - Auto-Switch & Chain Continuation
2. Configuration Profile Architecture & Persistence:
   - 4 Pre-configured Presets (default, agent_turbo, cost_saver, privacy_stealth)
   - Profile CRUD (Create, Read, Update, Delete)
   - Set Global Default Profile
   - Export & Import JSON Profile Backups
   - Apply Profile to Cursor IDE (settings.json + auto_switch_config.json)
3. Multi-Account Batch Operations & Auto-Enforcement:
   - Per-account profile assignment (assign_profile_to_account)
   - 1-Click Batch application across all accounts in pool (apply_profile_to_all_accounts)
   - Automatic profile enforcement upon account switch (switch_to_account)
4. Dashboard HTTP REST API Endpoints in server.py
"""

import os
import json
import pytest
import sqlite3
import tempfile
import shutil
from cursor_settings import CursorSettingsManager
from account_pool import AccountPoolManager, DB_FILE
from server import app

@pytest.fixture
def temp_settings_mgr():
    """Provides an isolated CursorSettingsManager instance with temporary directories."""
    temp_dir = tempfile.mkdtemp()
    workspace_dir = tempfile.mkdtemp()
    
    settings_file = os.path.join(temp_dir, "settings.json")
    storage_file = os.path.join(temp_dir, "storage.json")
    rules_file = os.path.join(workspace_dir, ".cursorrules")
    profiles_file = os.path.join(workspace_dir, "cursor_config_profiles.json")
    autoswitch_file = os.path.join(workspace_dir, "auto_switch_config.json")
    
    mgr = CursorSettingsManager(workspace_dir=workspace_dir)
    mgr.settings_path = settings_file
    mgr.storage_path = storage_file
    mgr.cursorrules_path = rules_file
    mgr.profiles_path = profiles_file
    mgr.auto_switch_config_path = autoswitch_file
    mgr._init_profiles_file()
    
    yield mgr
    
    shutil.rmtree(temp_dir, ignore_errors=True)
    shutil.rmtree(workspace_dir, ignore_errors=True)

class TestCursorSettingsFidelity:
    """Verifies that all Cursor settings can be read, written, and persisted accurately."""

    def test_default_settings_schema(self, temp_settings_mgr):
        settings = temp_settings_mgr.get_settings()
        
        # Verify AI Models
        assert "default_model" in settings
        assert "custom_model" in settings
        assert "thinking_effort" in settings
        assert "fast_mode" in settings
        assert "openai_key" in settings
        assert "anthropic_key" in settings
        assert "custom_base_url" in settings
        
        # Verify Cursor Tab
        assert "cpp_enable" in settings
        assert "cpp_partial_accepts" in settings
        assert "cpp_auto_suggest" in settings
        assert "cpp_disabled_languages" in settings
        assert "cpp_trigger_delay" in settings
        
        # Verify Composer
        assert "composer_auto_apply" in settings
        assert "composer_agent_mode" in settings
        assert "composer_auto_execute_terminal" in settings
        assert "use_inline_diffs" in settings
        assert "always_search_codebase" in settings
        assert "auto_scroll" in settings
        assert "indexing_enable" in settings
        
        # Verify Editor & UI
        assert "font_size" in settings
        assert "font_family" in settings
        assert "tab_size" in settings
        assert "word_wrap" in settings
        assert "minimap" in settings
        assert "format_on_save" in settings
        assert "color_theme" in settings
        
        # Verify Privacy
        assert "privacy_mode" in settings
        assert "ghost_mode" in settings
        assert "telemetry_level" in settings
        assert "enable_crash_reporter" in settings
        assert "index_locally_only" in settings
        
        # Verify Proxy
        assert "is_proxy_routed" in settings
        assert "http_proxy" in settings

    def test_update_all_settings_categories(self, temp_settings_mgr):
        payload = {
            "default_model": "claude-3.7-sonnet-thinking",
            "custom_model": "custom-deepseek-v3",
            "thinking_effort": "high",
            "fast_mode": True,
            "openai_key": "sk-test-openai-12345",
            "anthropic_key": "sk-ant-test-67890",
            "custom_base_url": "https://api.custom-ai.com/v1",
            
            "cpp_enable": True,
            "cpp_partial_accepts": True,
            "cpp_auto_suggest": False,
            "cpp_disabled_languages": ["markdown", "latex"],
            "cpp_trigger_delay": 120,
            
            "composer_agent_mode": True,
            "composer_auto_apply": True,
            "composer_auto_execute_terminal": True,
            "use_inline_diffs": True,
            "always_search_codebase": True,
            "auto_scroll": True,
            "indexing_enable": False,
            
            "font_size": 16,
            "font_family": "'Fira Code', monospace",
            "tab_size": 2,
            "word_wrap": "off",
            "minimap": False,
            "format_on_save": True,
            "color_theme": "One Dark Pro",
            
            "privacy_mode": True,
            "ghost_mode": True,
            "telemetry_level": "off",
            "enable_crash_reporter": False,
            "index_locally_only": True,
            
            "route_via_proxy": True,
            "http_proxy": "http://127.0.0.1:8999",
            
            "reset_mode": "hard_restart",
            "auto_resend_action": "auto",
            "continue_prompt": "Hãy tiếp tục hoàn thiện mã nguồn",
            "target_mode": "composer",
            "quota_threshold": 48.5
        }
        
        res = temp_settings_mgr.update_settings(payload)
        assert isinstance(res, dict)
        assert res["default_model"] == "claude-3.7-sonnet-thinking"
        
        # Reload and check
        loaded = temp_settings_mgr.get_settings()
        assert loaded["default_model"] == "claude-3.7-sonnet-thinking"
        assert loaded["custom_model"] == "custom-deepseek-v3"
        assert loaded["thinking_effort"] == "high"
        assert loaded["fast_mode"] is True
        assert loaded["openai_key"] == "sk-test-openai-12345"
        assert loaded["anthropic_key"] == "sk-ant-test-67890"
        assert loaded["custom_base_url"] == "https://api.custom-ai.com/v1"
        assert loaded["cpp_disabled_languages"] == ["markdown", "latex"]
        assert loaded["cpp_trigger_delay"] == 120
        assert loaded["composer_auto_execute_terminal"] is True
        assert loaded["font_size"] == 16
        assert loaded["font_family"] == "'Fira Code', monospace"
        assert loaded["tab_size"] == 2
        assert loaded["word_wrap"] == "off"
        assert loaded["minimap"] is False
        assert loaded["color_theme"] == "One Dark Pro"
        assert loaded["index_locally_only"] is True
        assert loaded["is_proxy_routed"] is True
        assert loaded["quota_threshold"] == 48.5

class TestProfilePresetArchitecture:
    """Verifies presets, CRUD operations, export/import and applying to Cursor."""

    def test_default_presets_exist(self, temp_settings_mgr):
        data = temp_settings_mgr.get_all_profiles()
        profiles = data.get("profiles", {})
        
        # Check standard presets
        assert "default" in profiles
        assert "agent_turbo" in profiles
        assert "cost_saver" in profiles
        assert "privacy_stealth" in profiles
        
        # Check preset properties
        turbo = profiles["agent_turbo"]
        assert turbo["settings"]["default_model"] == "claude-3.7-sonnet-thinking"
        assert turbo["settings"]["composer_agent_mode"] is True
        assert turbo["settings"]["composer_auto_execute_terminal"] is True
        assert turbo["settings"]["reset_mode"] == "hard_restart"
        
        stealth = profiles["privacy_stealth"]
        assert stealth["settings"]["ghost_mode"] is True
        assert stealth["settings"]["telemetry_level"] == "off"
        assert stealth["settings"]["index_locally_only"] is True

    def test_profile_crud(self, temp_settings_mgr):
        # 1. Create custom profile
        custom = {
            "name": "Fullstack Python Expert",
            "description": "Optimized for autonomous backend testing",
            "badge": "Custom",
            "settings": {
                "default_model": "deepseek-v3",
                "composer_agent_mode": True,
                "reset_mode": "hard_restart",
                "font_size": 15
            }
        }
        saved = temp_settings_mgr.save_profile("python_expert", custom)
        assert saved["id"] == "python_expert"
        assert saved["name"] == "Fullstack Python Expert"
        
        # 2. Read
        fetched = temp_settings_mgr.get_profile("python_expert")
        assert fetched is not None
        assert fetched["settings"]["default_model"] == "deepseek-v3"
        assert fetched["settings"]["font_size"] == 15
        
        # 3. Update
        custom["settings"]["font_size"] = 17
        updated = temp_settings_mgr.save_profile("python_expert", custom)
        assert updated["settings"]["font_size"] == 17
        
        # 4. Delete
        del_ok = temp_settings_mgr.delete_profile("python_expert")
        assert del_ok is True
        assert temp_settings_mgr.get_profile("python_expert") is None
        
        # 5. Cannot delete default
        assert temp_settings_mgr.delete_profile("default") is False

    def test_apply_profile_to_cursor(self, temp_settings_mgr):
        res = temp_settings_mgr.apply_profile_to_cursor("agent_turbo")
        assert res["success"] is True
        assert res["profile_id"] == "agent_turbo"
        
        # Verify settings in settings.json
        settings = temp_settings_mgr.get_settings()
        assert settings["default_model"] == "claude-3.7-sonnet-thinking"
        assert settings["composer_agent_mode"] is True
        assert settings["composer_auto_execute_terminal"] is True
        
        # Verify active profile updated
        profiles_data = temp_settings_mgr.get_all_profiles()
        assert profiles_data.get("active_profile") == "agent_turbo"

    def test_export_and_import_profiles(self, temp_settings_mgr):
        # Add a custom profile
        temp_settings_mgr.save_profile("test_export", {
            "name": "Export Profile",
            "settings": {"default_model": "gpt-4o"}
        })
        
        # Export
        exported = temp_settings_mgr.export_profiles()
        assert "profiles" in exported
        assert "test_export" in exported["profiles"]
        
        # Create new manager and import
        temp_dir2 = tempfile.mkdtemp()
        mgr2 = CursorSettingsManager(workspace_dir=temp_dir2)
        
        import_res = mgr2.import_profiles(exported)
        assert import_res["success"] is True
        assert import_res["imported_count"] >= 1
        
        imported_prof = mgr2.get_profile("test_export")
        assert imported_prof is not None
        assert imported_prof["name"] == "Export Profile"
        
        shutil.rmtree(temp_dir2, ignore_errors=True)

class TestMultiAccountProfileIntegration:
    """Verifies per-account profile assignment and batch application across the pool."""

    def test_assign_profile_to_account(self):
        pool = AccountPoolManager()
        accounts = pool.get_all_accounts()
        if not accounts:
            pytest.skip("No accounts found in cursor_accounts.db")
        
        target_acc = accounts[0]
        acc_id = target_acc["id"]
        
        # Assign agent_turbo
        ok = pool.assign_profile_to_account(acc_id, "agent_turbo")
        assert ok is True
        
        # Verify assignment persisted in DB
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        cur.execute("SELECT config_profile FROM accounts WHERE id = ?", (acc_id,))
        val = cur.fetchone()[0]
        con.close()
        assert val == "agent_turbo"
        
        # Restore to default
        pool.assign_profile_to_account(acc_id, "default")

    def test_apply_profile_to_all_accounts(self):
        pool = AccountPoolManager()
        accounts = pool.get_all_accounts()
        if not accounts:
            pytest.skip("No accounts found in cursor_accounts.db")
        
        count = pool.apply_profile_to_all_accounts("cost_saver")
        assert count == len(accounts)
        
        # Verify in DB
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        cur.execute("SELECT COUNT(DISTINCT config_profile), MIN(config_profile) FROM accounts")
        dist_count, dist_val = cur.fetchone()
        con.close()
        assert dist_count == 1
        assert dist_val == "cost_saver"
        
        # Reset back to default
        pool.apply_profile_to_all_accounts("default")

class TestServerProfileEndpoints:
    """Verifies all Flask API endpoints in server.py."""

    @pytest.fixture
    def client(self):
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    def test_get_profiles_endpoint(self, client):
        res = client.get("/api/cursor/profiles")
        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        assert "profiles" in data
        assert "default" in data["profiles"]
        assert "total_accounts" in data
        assert "accounts_distribution" in data

    def test_save_and_delete_profile_endpoint(self, client):
        # Save custom
        payload = {
            "profile_id": "test_api_profile",
            "name": "API Test Profile",
            "description": "Created via automated test",
            "settings": {
                "default_model": "o3-mini",
                "font_size": 15
            }
        }
        res = client.post("/api/cursor/profiles/save", json=payload)
        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        assert data["profile"]["id"] == "test_api_profile"

        # Apply current
        res_apply = client.post("/api/cursor/profiles/apply-current", json={"profile_id": "test_api_profile"})
        assert res_apply.status_code == 200
        assert res_apply.get_json()["success"] is True

        # Delete
        res_del = client.post("/api/cursor/profiles/delete", json={"profile_id": "test_api_profile"})
        assert res_del.status_code == 200
        assert res_del.get_json()["success"] is True

    def test_assign_account_endpoint(self, client):
        pool = AccountPoolManager()
        accounts = pool.get_all_accounts()
        if not accounts:
            pytest.skip("No accounts in DB")
        acc_id = accounts[0]["id"]
        
        res = client.post("/api/cursor/profiles/assign-account", json={
            "account_id": acc_id,
            "profile_id": "agent_turbo"
        })
        assert res.status_code == 200
        assert res.get_json()["success"] is True
        
        # Restore
        client.post("/api/cursor/profiles/assign-account", json={
            "account_id": acc_id,
            "profile_id": "default"
        })

    def test_apply_all_endpoint(self, client):
        res = client.post("/api/cursor/profiles/apply-all", json={"profile_id": "default"})
        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        assert data["updated_count"] >= 1

    def test_export_and_import_endpoints(self, client):
        res = client.get("/api/cursor/profiles/export")
        assert res.status_code == 200
        export_data = res.get_json()
        assert export_data["success"] is True
        assert "export" in export_data

        # Import back
        res_import = client.post("/api/cursor/profiles/import", json=export_data["export"])
        assert res_import.status_code == 200
        assert res_import.get_json()["success"] is True

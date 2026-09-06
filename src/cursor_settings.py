"""
Cursor Settings & Hardware Fingerprint Management Module
========================================================
Supports:
1. Complete Native Cursor Settings (settings.json):
   - AI Models & Custom Inference (Default model, custom model, thinking effort, fast mode, API keys)
   - Cursor Tab & Autocomplete (cursor.cpp.enable, partial accepts, auto suggest, disabled languages, trigger delay)
   - Composer & Autonomous Agent (agentMode, autoApply, autoExecuteTerminal, inline diffs, codebase search, indexing)
   - Editor & Workbench UI (fontSize, fontFamily, tabSize, wordWrap, minimap, formatOnSave, colorTheme)
   - Privacy & Telemetry (Ghost mode, telemetry level, crash reporter, local indexing only)
   - Proxy Routing & Strict SSL
   - Auto-Switch & Continue Action (reset_mode, quota_threshold, continue_prompt, target_mode)
2. Hardware ID / Fingerprint Spoofer (storage.json: machineId, macMachineId, devDeviceId, sqmId)
3. .cursorrules Direct Editor (workspace root)
4. Config Profiles & Multi-Account Preset Management:
   - Save / load / update / delete named configuration profiles
   - 1-Click apply to current Cursor IDE
   - 1-Click batch apply to ALL accounts in cursor_accounts.db
   - Per-account profile assignment and auto-enforcement during account rotation
   - Export and import JSON configuration profiles
"""

import os
import sys
import json
import uuid
import secrets
import shutil
import time
import copy
from typing import Dict, Any, Optional, List

class CursorSettingsManager:
    DEFAULT_MODELS = [
        {"id": "grok-4.6", "name": "Grok 4.6 (Effort: Medium)", "badge": "Free Tier Verified", "thinking_effort": "medium"},
        {"id": "cursor-grok-4.5", "name": "Cursor Grok 4.5", "badge": "Fast", "thinking_effort": "low"},
        {"id": "claude-3.7-sonnet", "name": "Claude 3.7 Sonnet", "badge": "New"},
        {"id": "claude-3.7-sonnet-thinking", "name": "Claude 3.7 Sonnet (Thinking)", "badge": "Reasoning"},
        {"id": "claude-3.5-sonnet", "name": "Claude 3.5 Sonnet", "badge": "Popular"},
        {"id": "gpt-4o", "name": "GPT-4o", "badge": "OpenAI"},
        {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "badge": "Fast"},
        {"id": "o1", "name": "OpenAI o1", "badge": "Reasoning"},
        {"id": "o3-mini", "name": "OpenAI o3-mini", "badge": "Reasoning"},
        {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash", "badge": "Google"},
        {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro", "badge": "Google"},
        {"id": "deepseek-r1", "name": "DeepSeek R1", "badge": "Reasoning"},
        {"id": "deepseek-v3", "name": "DeepSeek V3", "badge": "Coding"},
        {"id": "composer-2.5", "name": "Composer 2.5", "badge": "Agentic"},
        {"id": "default", "name": "Cursor Default (Auto)", "badge": "Standard", "thinking_effort": "medium"}
    ]

    DEFAULT_AUTO_SWITCH_CONFIG = {
        "auto_rotate_enabled": True,
        "quota_threshold": 100.0,
        "reset_mode": "hard_restart",  # "hard_restart" | "soft_reload"
        "auto_resend_action": "manual",  # "auto" | "always" | "manual"
        "continue_prompt": "Tiếp tục",
        "target_mode": "composer",     # "composer" | "chat"
        "antidetect_mode": "account_locked"  # "account_locked" | "stealth_randomize" | "native_standard"
    }

    # =========================================================================
    # PRE-CONFIGURED PRESET PROFILES FOR MULTI-ACCOUNT APPLICATION
    # =========================================================================
    DEFAULT_PROFILES = {
        "default": {
            "id": "default",
            "name": "Tiêu Chuẩn (Standard - Grok 4.6 Free)",
            "description": "Cấu hình chuẩn Cursor Free: Grok 4.6 (Effort: Medium), Cursor Tab (CPP), Ghost Mode bật và Hard Reset khi hết quota.",
            "badge": "Standard",
            "is_default": True,
            "sections": {
                "general": {
                    "startup_behavior": "none",
                    "startup_editor": "none",
                    "notifications_enabled": True,
                    "privacy_mode": True,
                    "telemetry_enabled": False,
                    "ghost_mode": True,
                    "index_locally_only": True
                },
                "appearance": {
                    "color_theme": "Default Dark Modern",
                    "agent_conversation_style": "compact",
                    "typography_font_family": "'JetBrains Mono', 'Fira Code', Consolas, monospace",
                    "typography_font_size": 14,
                    "high_contrast": False,
                    "motion_reduced": False
                },
                "agent": {
                    "auto_run_commands": False,
                    "default_model": "grok-4.6",
                    "composer_mode": True,
                    "max_context_length": 200000
                },
                "git_prs": {
                    "git_integration": True,
                    "pr_review_assistant": True,
                    "commit_message_generator": True
                },
                "worktrees": {
                    "multi_worktree_isolation": True,
                    "auto_cleanup": True
                },
                "browser_network": {
                    "local_proxy_routed": False,
                    "web_search_integration": True
                },
                "tab": {
                    "cpp_enabled": True,
                    "suggestion_delay_ms": 50
                },
                "code_intelligence": {
                    "indexing_engine": "turbo_graph",
                    "semantic_search_enabled": True,
                    "symbol_graph_enabled": True
                }
            },
            "settings": {
                "default_model": "grok-4.6",
                "custom_model": "",
                "thinking_effort": "medium",
                "fast_mode": False,
                "openai_key": "",
                "anthropic_key": "",
                "custom_base_url": "",
                "cpp_enable": True,
                "cpp_partial_accepts": True,
                "cpp_auto_suggest": True,
                "cpp_disabled_languages": ["plaintext", "markdown"],
                "cpp_trigger_delay": 50,
                "composer_auto_apply": True,
                "composer_agent_mode": True,
                "composer_auto_execute_terminal": False,
                "use_inline_diffs": True,
                "always_search_codebase": False,
                "auto_scroll": True,
                "indexing_enable": True,
                "font_size": 14,
                "font_family": "'JetBrains Mono', 'Fira Code', Consolas, monospace",
                "tab_size": 4,
                "word_wrap": "on",
                "minimap": True,
                "format_on_save": True,
                "color_theme": "Default Dark Modern",
                "privacy_mode": True,
                "ghost_mode": True,
                "telemetry_level": "off",
                "enable_crash_reporter": False,
                "index_locally_only": True,
                "route_via_proxy": False,
                "proxy_port": 8999,
                "http_proxy": "",
                "proxy_strict_ssl": False,
                "proxy_support": "override",
                "reset_mode": "hard_restart",
                "auto_rotate_enabled": True,
                "quota_threshold": 100.0,
                "auto_resend_action": "manual",
                "continue_prompt": "Tiếp tục",
                "target_mode": "composer",
                "startup_editor": "none",
                "restore_windows": "all",
                "dnd_mode": False,
                "silent_notifications": False,
                "preferred_dark_theme": "Default Dark Modern",
                "agent_conversation_style": "compact",
                "icon_theme": "vs-seti",
                "line_height": 22,
                "font_weight": "normal",
                "high_contrast": False,
                "motion": "off",
                "import_third_party_keys": True,
                "composer_context_limit": 200000,
                "composer_use_tools": True,
                "terminal_execution_policy": "ask",
                "enable_pull_requests": True,
                "auto_fetch": True,
                "default_branch": "main",
                "prune_on_fetch": True,
                "co_authored_by_agent": True,
                "include_attribution": True,
                "worktrees_enabled": True,
                "worktrees_directory": ".worktrees",
                "auto_create_branch": True,
                "prune_merged": True,
                "proxy_bypass": "localhost, 127.0.0.1",
                "network_timeout": 30000,
                "cpp_model": "default",
                "indexing_memory_mb": 4096,
                "semantic_search": True,
                "embeddings_provider": "local",
                "precise_symbols": True,
                "symbol_hover": True
            }
        },
        "agent_turbo": {
            "id": "agent_turbo",
            "name": "Siêu Tốc - Claude 3.7 Thinking",
            "description": "Tối ưu hóa lập trình Agentic tự động: Claude 3.7 Reasoning, Composer Auto-Apply, Auto Execute Terminal và Fast Mode.",
            "badge": "Agentic",
            "is_default": False,
            "settings": {
                "default_model": "claude-3.7-sonnet-thinking",
                "custom_model": "",
                "thinking_effort": "high",
                "fast_mode": True,
                "openai_key": "",
                "anthropic_key": "",
                "custom_base_url": "",
                "cpp_enable": True,
                "cpp_partial_accepts": True,
                "cpp_auto_suggest": True,
                "cpp_disabled_languages": [],
                "cpp_trigger_delay": 20,
                "composer_auto_apply": True,
                "composer_agent_mode": True,
                "composer_auto_execute_terminal": True,
                "use_inline_diffs": True,
                "always_search_codebase": True,
                "auto_scroll": True,
                "indexing_enable": True,
                "font_size": 14,
                "font_family": "'JetBrains Mono', 'Fira Code', Consolas, monospace",
                "tab_size": 4,
                "word_wrap": "on",
                "minimap": False,
                "format_on_save": True,
                "color_theme": "Default Dark Modern",
                "privacy_mode": True,
                "ghost_mode": True,
                "telemetry_level": "off",
                "enable_crash_reporter": False,
                "index_locally_only": True,
                "route_via_proxy": True,
                "proxy_port": 8999,
                "http_proxy": "http://127.0.0.1:8999",
                "proxy_strict_ssl": False,
                "proxy_support": "override",
                "reset_mode": "hard_restart",
                "auto_rotate_enabled": True,
                "quota_threshold": 100.0,
                "auto_resend_action": "manual",
                "continue_prompt": "Tiếp tục giải quyết task này hoàn chỉnh",
                "target_mode": "composer"
            }
        },
        "cost_saver": {
            "id": "cost_saver",
            "name": "Tiết Kiệm Quota - Fast Coding",
            "description": "Sử dụng model nhẹ (Grok / GPT-4o-mini), tắt indexing nền, giới hạn quota 45% để kéo dài thời gian làm việc.",
            "badge": "Saver",
            "is_default": False,
            "settings": {
                "default_model": "cursor-grok-4.5",
                "custom_model": "",
                "thinking_effort": "low",
                "fast_mode": True,
                "openai_key": "",
                "anthropic_key": "",
                "custom_base_url": "",
                "cpp_enable": True,
                "cpp_partial_accepts": True,
                "cpp_auto_suggest": True,
                "cpp_disabled_languages": ["plaintext", "markdown"],
                "cpp_trigger_delay": 100,
                "composer_auto_apply": False,
                "composer_agent_mode": False,
                "composer_auto_execute_terminal": False,
                "use_inline_diffs": True,
                "always_search_codebase": False,
                "auto_scroll": True,
                "indexing_enable": False,
                "font_size": 14,
                "font_family": "'JetBrains Mono', Consolas, monospace",
                "tab_size": 4,
                "word_wrap": "on",
                "minimap": True,
                "format_on_save": False,
                "color_theme": "Default Dark Modern",
                "privacy_mode": True,
                "ghost_mode": True,
                "telemetry_level": "off",
                "enable_crash_reporter": False,
                "index_locally_only": True,
                "route_via_proxy": False,
                "proxy_port": 8999,
                "http_proxy": "",
                "proxy_strict_ssl": False,
                "proxy_support": "override",
                "reset_mode": "hard_restart",
                "auto_rotate_enabled": True,
                "quota_threshold": 45.0,
                "auto_resend_action": "manual",
                "continue_prompt": "Tiếp tục",
                "target_mode": "composer"
            }
        },
        "privacy_stealth": {
            "id": "privacy_stealth",
            "name": "Ẩn Danh & Chống Ban Tối Đa",
            "description": "Ghost Mode tuyệt đối, ngắt telemetry và crash report, cấm gửi dữ liệu ra ngoài và chỉ index cục bộ.",
            "badge": "Stealth",
            "is_default": False,
            "settings": {
                "default_model": "claude-3.5-sonnet",
                "custom_model": "",
                "thinking_effort": "default",
                "fast_mode": False,
                "openai_key": "",
                "anthropic_key": "",
                "custom_base_url": "",
                "cpp_enable": False,
                "cpp_partial_accepts": False,
                "cpp_auto_suggest": False,
                "cpp_disabled_languages": [],
                "cpp_trigger_delay": 200,
                "composer_auto_apply": True,
                "composer_agent_mode": True,
                "composer_auto_execute_terminal": False,
                "use_inline_diffs": True,
                "always_search_codebase": False,
                "auto_scroll": True,
                "indexing_enable": False,
                "font_size": 14,
                "font_family": "'JetBrains Mono', Consolas, monospace",
                "tab_size": 4,
                "word_wrap": "on",
                "minimap": True,
                "format_on_save": True,
                "color_theme": "Default Dark Modern",
                "privacy_mode": True,
                "ghost_mode": True,
                "telemetry_level": "off",
                "enable_crash_reporter": False,
                "index_locally_only": True,
                "route_via_proxy": True,
                "proxy_port": 8999,
                "http_proxy": "http://127.0.0.1:8999",
                "proxy_strict_ssl": False,
                "proxy_support": "override",
                "reset_mode": "hard_restart",
                "auto_rotate_enabled": True,
                "quota_threshold": 48.0,
                "auto_resend_action": "manual",
                "continue_prompt": "Tiếp tục",
                "target_mode": "composer"
            }
        }
    }

    def __init__(self, workspace_dir: Optional[str] = None, cursor_user_dir: Optional[str] = None):
        self.appdata = os.getenv("APPDATA") or ""
        default_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if workspace_dir:
            self.workspace_dir = os.path.abspath(workspace_dir)
        elif os.path.exists(os.path.join(os.getcwd(), "auto_switch_config.json")):
            self.workspace_dir = os.path.abspath(os.getcwd())
        else:
            self.workspace_dir = default_root
        
        if cursor_user_dir:
            self.cursor_user_dir = os.path.abspath(cursor_user_dir)
        else:
            self.cursor_user_dir = os.path.join(self.appdata, "Cursor", "User")
        self.settings_path = os.path.join(self.cursor_user_dir, "settings.json")
        self.storage_path = os.path.join(self.cursor_user_dir, "globalStorage", "storage.json")
        self.storage_bak_path = os.path.join(self.cursor_user_dir, "globalStorage", "storage.json.bak")
        self.cursorrules_path = os.path.join(self.workspace_dir, ".cursorrules")
        self.auto_switch_config_path = os.path.join(self.workspace_dir, "auto_switch_config.json")
        self.profiles_path = os.path.join(self.workspace_dir, "cursor_config_profiles.json")
        self._init_profiles_file()

    @property
    def settings_file(self) -> str:
        return self.settings_path

    @settings_file.setter
    def settings_file(self, value: str):
        self.settings_path = str(value)

    @property
    def profiles_file(self) -> str:
        return self.profiles_path

    @profiles_file.setter
    def profiles_file(self, value: str):
        self.profiles_path = str(value)

    @property
    def auto_switch_config_file(self) -> str:
        return self.auto_switch_config_path

    @auto_switch_config_file.setter
    def auto_switch_config_file(self, value: str):
        self.auto_switch_config_path = str(value)

    # =========================================================================
    # 1. Cursor Settings (settings.json) - 100% Comprehensive Native Fidelity
    # =========================================================================
    def get_settings(self) -> Dict[str, Any]:
        """Doc toan bo cau hinh Cursor tu settings.json va auto_switch_config.json."""
        raw_settings = {}
        if os.path.exists(self.settings_path):
            try:
                with open(self.settings_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        raw_settings = json.loads(content)
            except Exception as e:
                print(f"[-] Loi doc settings.json: {e}")

        # 1. General: Startup, Notifications, Privacy & Telemetry
        startup_editor = raw_settings.get("workbench.startupEditor", "none")
        restore_windows = raw_settings.get("window.restoreWindows", "all")
        auto_scroll = raw_settings.get("cursor.general.autoScroll", True)
        custom_base_url = raw_settings.get("cursor.general.customBaseUrl", "")
        dnd_mode = raw_settings.get("notifications.doNotDisturbMode", False)
        silent_notifications = raw_settings.get("notifications.silent", False)
        telemetry_level = raw_settings.get("telemetry.telemetryLevel", "off")
        crash_reporter = raw_settings.get("telemetry.enableCrashReporter", False)
        ghost_mode = raw_settings.get("cursor.ghostMode", True)
        privacy_mode = bool(telemetry_level == "off" and not crash_reporter and ghost_mode)
        index_locally_only = raw_settings.get("cursor.privacy.indexCodebaseLocally", True)

        # 2. Appearance: Theme, Agent Conversation, Colors, Typography, High Contrast, Motion, Font
        color_theme = raw_settings.get("workbench.colorTheme", "Default Dark Modern")
        preferred_dark_theme = raw_settings.get("workbench.preferredDarkColorTheme", "Default Dark Modern")
        agent_conversation_style = raw_settings.get("cursor.chat.agentConversationStyle", "compact")
        icon_theme = raw_settings.get("workbench.iconTheme", "vs-seti")
        font_size = raw_settings.get("editor.fontSize", 14)
        font_family = raw_settings.get("editor.fontFamily", "'JetBrains Mono', 'Fira Code', Consolas, monospace")
        line_height = raw_settings.get("editor.lineHeight", 22)
        font_weight = raw_settings.get("editor.fontWeight", "normal")
        high_contrast = raw_settings.get("workbench.highContrast", False)
        motion = raw_settings.get("workbench.reduceMotion", "off")

        # 3. Agent: Conversation settings, Third party import, Context & tool, Execution & approval, Terminal & editing
        default_model = (
            raw_settings.get("cursor.chat.defaultModel")
            or raw_settings.get("cursor.general.model")
            or raw_settings.get("cursor.composer.defaultModel")
            or "default"
        )
        custom_model = raw_settings.get("cursor.chat.customModel", "")
        thinking_effort = raw_settings.get("cursor.chat.thinkingEffort", "default")
        fast_mode = raw_settings.get("cursor.chat.fastMode", False)
        always_search_codebase = raw_settings.get("cursor.chat.alwaysSearchCodebase", False)
        import_third_party_keys = raw_settings.get("cursor.agent.importThirdPartyKeys", True)
        openai_key = raw_settings.get("cursor.general.openAIKey", "")
        anthropic_key = raw_settings.get("cursor.general.anthropicKey", "")
        composer_context_limit = raw_settings.get("cursor.composer.contextLimit", 200000)
        composer_use_tools = raw_settings.get("cursor.composer.useTools", True)
        composer_auto_apply = raw_settings.get("cursor.composer.autoApply", True)
        composer_agent_mode = raw_settings.get("cursor.composer.agentMode", True)
        composer_auto_execute_terminal = raw_settings.get("cursor.composer.autoExecuteTerminal", False)
        use_inline_diffs = raw_settings.get("cursor.diffs.useInlineDiffs", True)
        terminal_execution_policy = raw_settings.get("cursor.terminal.executionPolicy", "ask")
        format_on_save = raw_settings.get("editor.formatOnSave", True)
        tab_size = raw_settings.get("editor.tabSize", 4)
        word_wrap = raw_settings.get("editor.wordWrap", "on")
        minimap = raw_settings.get("editor.minimap.enabled", True)

        # 4. Git & PRs: Pull requests, Branch, Attribution
        enable_pull_requests = raw_settings.get("cursor.git.enablePullRequests", True)
        auto_fetch = raw_settings.get("cursor.git.autoFetch", True)
        default_branch = raw_settings.get("cursor.git.defaultBranch", "main")
        prune_on_fetch = raw_settings.get("cursor.git.pruneOnFetch", True)
        co_authored_by_agent = raw_settings.get("cursor.git.coAuthoredByAgent", True)
        include_attribution = raw_settings.get("cursor.git.includeAttribution", True)

        # 5. Worktrees: Worktrees enabled, default directory, branch settings
        worktrees_enabled = raw_settings.get("cursor.worktrees.enabled", True)
        worktrees_directory = raw_settings.get("cursor.worktrees.defaultDirectory", ".worktrees")
        auto_create_branch = raw_settings.get("cursor.worktrees.autoCreateBranch", True)
        prune_merged = raw_settings.get("cursor.worktrees.pruneMerged", True)

        # 6. Browser & Network: Proxy, bypass lists, SSL verification, timeout
        http_proxy = raw_settings.get("http.proxy", "")
        proxy_strict_ssl = raw_settings.get("http.proxyStrictSSL", False)
        proxy_support = raw_settings.get("http.proxySupport", "override")
        proxy_bypass = raw_settings.get("http.proxyBypass", "localhost, 127.0.0.1")
        network_timeout = raw_settings.get("http.timeout", 30000)
        is_proxy_routed = bool(http_proxy and ":8999" in http_proxy)

        # 7. Tab: Cursor Tab (CPP) toggle, model, auto-complete behavior, partial accept
        cpp_enable = raw_settings.get("cursor.cpp.enable", True)
        cpp_model = raw_settings.get("cursor.cpp.model", "default")
        cpp_partial_accepts = raw_settings.get("cursor.cpp.enablePartialAccepts", True)
        cpp_auto_suggest = raw_settings.get("cursor.cpp.autoSuggest", True)
        cpp_disabled_languages = raw_settings.get("cursor.cpp.disabledLanguages", ["plaintext", "markdown"])
        cpp_trigger_delay = raw_settings.get("cursor.cpp.triggerDelay", 50)

        # 8. Code Intelligence: Indexing, semantic search, symbol navigation
        indexing_enable = raw_settings.get("cursor.indexing.enable", True)
        indexing_memory_mb = raw_settings.get("cursor.indexing.maxMemoryMB", 4096)
        semantic_search = raw_settings.get("cursor.codeIntelligence.semanticSearch", True)
        embeddings_provider = raw_settings.get("cursor.codeIntelligence.embeddingsProvider", "local")
        precise_symbols = raw_settings.get("cursor.codeIntelligence.preciseSymbols", True)
        symbol_hover = raw_settings.get("cursor.codeIntelligence.symbolHover", True)

        # Auto-Switch & Continue Config
        autoswitch = self.get_auto_switch_config()

        return {
            "exists": os.path.exists(self.settings_path),
            "path": self.settings_path,
            # Flat attributes for backward compatibility
            "default_model": default_model,
            "custom_model": custom_model,
            "thinking_effort": thinking_effort,
            "fast_mode": fast_mode,
            "openai_key": openai_key,
            "anthropic_key": anthropic_key,
            "custom_base_url": custom_base_url,
            "telemetry_level": telemetry_level,
            "enable_crash_reporter": crash_reporter,
            "ghost_mode": ghost_mode,
            "privacy_mode": privacy_mode,
            "index_locally_only": index_locally_only,
            "cpp_enable": cpp_enable,
            "cpp_model": cpp_model,
            "cpp_partial_accepts": cpp_partial_accepts,
            "cpp_auto_suggest": cpp_auto_suggest,
            "cpp_disabled_languages": cpp_disabled_languages,
            "cpp_trigger_delay": cpp_trigger_delay,
            "composer_auto_apply": composer_auto_apply,
            "composer_agent_mode": composer_agent_mode,
            "composer_auto_execute_terminal": composer_auto_execute_terminal,
            "use_inline_diffs": use_inline_diffs,
            "always_search_codebase": always_search_codebase,
            "auto_scroll": auto_scroll,
            "indexing_enable": indexing_enable,
            "font_size": font_size,
            "font_family": font_family,
            "tab_size": tab_size,
            "word_wrap": word_wrap,
            "minimap": minimap,
            "format_on_save": format_on_save,
            "color_theme": color_theme,
            "http_proxy": http_proxy,
            "proxy_strict_ssl": proxy_strict_ssl,
            "proxy_support": proxy_support,
            "is_proxy_routed": is_proxy_routed,
            "route_via_proxy": is_proxy_routed,
            # New flat attributes for 8 categories
            "startup_editor": startup_editor,
            "restore_windows": restore_windows,
            "dnd_mode": dnd_mode,
            "silent_notifications": silent_notifications,
            "preferred_dark_theme": preferred_dark_theme,
            "agent_conversation_style": agent_conversation_style,
            "icon_theme": icon_theme,
            "line_height": line_height,
            "font_weight": font_weight,
            "high_contrast": high_contrast,
            "motion": motion,
            "import_third_party_keys": import_third_party_keys,
            "composer_context_limit": composer_context_limit,
            "composer_use_tools": composer_use_tools,
            "terminal_execution_policy": terminal_execution_policy,
            "enable_pull_requests": enable_pull_requests,
            "auto_fetch": auto_fetch,
            "default_branch": default_branch,
            "prune_on_fetch": prune_on_fetch,
            "co_authored_by_agent": co_authored_by_agent,
            "include_attribution": include_attribution,
            "worktrees_enabled": worktrees_enabled,
            "worktrees_directory": worktrees_directory,
            "auto_create_branch": auto_create_branch,
            "prune_merged": prune_merged,
            "proxy_bypass": proxy_bypass,
            "network_timeout": network_timeout,
            "indexing_memory_mb": indexing_memory_mb,
            "semantic_search": semantic_search,
            "embeddings_provider": embeddings_provider,
            "precise_symbols": precise_symbols,
            "symbol_hover": symbol_hover,
            # Auto-switch flat attributes
            "reset_mode": autoswitch.get("reset_mode", "hard_restart"),
            "auto_rotate_enabled": autoswitch.get("auto_rotate_enabled", True),
            "quota_threshold": autoswitch.get("quota_threshold", 100.0),
            "auto_resend_action": autoswitch.get("auto_resend_action", "manual"),
            "continue_prompt": autoswitch.get("continue_prompt", "Tiếp tục"),
            "target_mode": autoswitch.get("target_mode", "composer"),
            # Structured 8 authentic Cursor categories
            "sections": {
                "general": {
                    "startup_behavior": raw_settings.get("startup_behavior", startup_editor),
                    "startup_editor": startup_editor,
                    "restore_windows": restore_windows,
                    "auto_scroll": auto_scroll,
                    "custom_base_url": custom_base_url,
                    "dnd_mode": dnd_mode,
                    "silent_notifications": silent_notifications,
                    "notifications_enabled": raw_settings.get("notifications_enabled", (not silent_notifications and not dnd_mode)),
                    "privacy_mode": raw_settings.get("privacy_mode", privacy_mode),
                    "ghost_mode": ghost_mode,
                    "telemetry_level": telemetry_level,
                    "telemetry_enabled": raw_settings.get("telemetry_enabled", (telemetry_level != "off")),
                    "enable_crash_reporter": crash_reporter,
                    "index_locally_only": index_locally_only
                },
                "appearance": {
                    "color_theme": raw_settings.get("color_theme", color_theme),
                    "preferred_dark_theme": preferred_dark_theme,
                    "agent_conversation_style": raw_settings.get("agent_conversation_style", agent_conversation_style),
                    "icon_theme": icon_theme,
                    "font_size": font_size,
                    "typography_font_size": raw_settings.get("typography_font_size", font_size),
                    "font_family": font_family,
                    "typography_font_family": raw_settings.get("typography_font_family", font_family),
                    "font_weight": font_weight,
                    "line_height": line_height,
                    "high_contrast": raw_settings.get("high_contrast", high_contrast),
                    "motion": motion,
                    "motion_reduced": raw_settings.get("motion_reduced", (motion != "off")),
                    "privacy_mode": privacy_mode
                },
                "agent": {
                    "default_model": raw_settings.get("default_model", default_model),
                    "custom_model": custom_model,
                    "thinking_effort": thinking_effort,
                    "fast_mode": fast_mode,
                    "max_context_length": raw_settings.get("max_context_length", composer_context_limit),
                    "composer_context_limit": composer_context_limit,
                    "composer_mode": raw_settings.get("composer_mode", composer_agent_mode),
                    "composer_agent_mode": composer_agent_mode,
                    "auto_run_commands": raw_settings.get("auto_run_commands", composer_auto_execute_terminal),
                    "composer_auto_execute_terminal": composer_auto_execute_terminal,
                    "composer_auto_apply": composer_auto_apply,
                    "composer_use_tools": composer_use_tools,
                    "use_inline_diffs": use_inline_diffs,
                    "always_search_codebase": always_search_codebase,
                    "import_third_party_keys": import_third_party_keys,
                    "openai_key": openai_key,
                    "anthropic_key": anthropic_key,
                    "terminal_execution_policy": terminal_execution_policy,
                    "format_on_save": format_on_save,
                    "tab_size": tab_size,
                    "word_wrap": word_wrap,
                    "minimap": minimap
                },
                "git_prs": {
                    "enable_pull_requests": enable_pull_requests,
                    "git_integration": raw_settings.get("git_integration", enable_pull_requests),
                    "pr_review_assistant": raw_settings.get("pr_review_assistant", enable_pull_requests),
                    "commit_message_generator": raw_settings.get("commit_message_generator", True),
                    "auto_fetch": auto_fetch,
                    "default_branch": default_branch,
                    "prune_on_fetch": prune_on_fetch,
                    "co_authored_by_agent": co_authored_by_agent,
                    "include_attribution": include_attribution
                },
                "worktrees": {
                    "worktrees_enabled": worktrees_enabled,
                    "multi_worktree_isolation": raw_settings.get("multi_worktree_isolation", worktrees_enabled),
                    "auto_cleanup": raw_settings.get("auto_cleanup", prune_merged),
                    "worktrees_directory": worktrees_directory,
                    "auto_create_branch": auto_create_branch,
                    "prune_merged": prune_merged
                },
                "browser_network": {
                    "route_via_proxy": is_proxy_routed,
                    "local_proxy_routed": raw_settings.get("local_proxy_routed", is_proxy_routed),
                    "web_search_integration": raw_settings.get("web_search_integration", True),
                    "http_proxy": http_proxy,
                    "proxy_port": 8999,
                    "proxy_support": proxy_support,
                    "proxy_strict_ssl": proxy_strict_ssl,
                    "proxy_bypass": proxy_bypass,
                    "network_timeout": network_timeout
                },
                "tab": {
                    "cpp_enable": cpp_enable,
                    "cpp_enabled": raw_settings.get("cpp_enabled", cpp_enable),
                    "suggestion_delay_ms": raw_settings.get("suggestion_delay_ms", cpp_trigger_delay),
                    "cpp_model": cpp_model,
                    "cpp_partial_accepts": cpp_partial_accepts,
                    "cpp_auto_suggest": cpp_auto_suggest,
                    "cpp_disabled_languages": cpp_disabled_languages,
                    "cpp_trigger_delay": cpp_trigger_delay
                },
                "code_intelligence": {
                    "indexing_enable": indexing_enable,
                    "indexing_engine": raw_settings.get("indexing_engine", "turbo_graph" if indexing_enable else "off"),
                    "semantic_search_enabled": raw_settings.get("semantic_search_enabled", semantic_search),
                    "symbol_graph_enabled": raw_settings.get("symbol_graph_enabled", precise_symbols),
                    "indexing_memory_mb": indexing_memory_mb,
                    "semantic_search": semantic_search,
                    "embeddings_provider": embeddings_provider,
                    "precise_symbols": precise_symbols,
                    "symbol_hover": symbol_hover
                },
                # Legacy section mappings for backward compatibility
                "model": {
                    "default_model": default_model,
                    "custom_model": custom_model,
                    "thinking_effort": thinking_effort,
                    "fast_mode": fast_mode,
                    "openai_key": openai_key,
                    "anthropic_key": anthropic_key,
                    "custom_base_url": custom_base_url
                },
                "features": {
                    "cpp_enable": cpp_enable,
                    "cpp_partial_accepts": cpp_partial_accepts,
                    "cpp_auto_suggest": cpp_auto_suggest,
                    "cpp_disabled_languages": cpp_disabled_languages,
                    "cpp_trigger_delay": cpp_trigger_delay,
                    "composer_auto_apply": composer_auto_apply,
                    "composer_agent_mode": composer_agent_mode,
                    "composer_auto_execute_terminal": composer_auto_execute_terminal,
                    "use_inline_diffs": use_inline_diffs,
                    "always_search_codebase": always_search_codebase,
                    "auto_scroll": auto_scroll,
                    "indexing_enable": indexing_enable
                },
                "editor": {
                    "font_size": font_size,
                    "font_family": font_family,
                    "tab_size": tab_size,
                    "word_wrap": word_wrap,
                    "minimap": minimap,
                    "format_on_save": format_on_save,
                    "color_theme": color_theme
                },
                "privacy": {
                    "privacy_mode": privacy_mode,
                    "ghost_mode": ghost_mode,
                    "telemetry_level": telemetry_level,
                    "enable_crash_reporter": crash_reporter,
                    "index_locally_only": index_locally_only
                },
                "network": {
                    "route_via_proxy": is_proxy_routed,
                    "http_proxy": http_proxy,
                    "proxy_strict_ssl": proxy_strict_ssl,
                    "proxy_support": proxy_support
                },
                "autoswitch": autoswitch
            },
            "available_models": self.DEFAULT_MODELS,
            "raw": raw_settings
        }

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Cap nhat settings.json an toan, bao ton cac key ngoai le khac."""
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        
        raw_settings = {}
        if os.path.exists(self.settings_path):
            try:
                with open(self.settings_path, "r", encoding="utf-8") as f:
                    c = f.read().strip()
                    if c:
                        raw_settings = json.loads(c)
            except Exception:
                raw_settings = {}

        if "sections" in updates and isinstance(updates["sections"], dict):
            for sec_name, sec_dict in updates["sections"].items():
                if isinstance(sec_dict, dict):
                    for k, v in sec_dict.items():
                        updates[k] = v
                        raw_settings[k] = v
                        if k == "startup_behavior":
                            raw_settings["workbench.startupEditor"] = v
                        elif k == "typography_font_size":
                            raw_settings["editor.fontSize"] = int(v) if isinstance(v, (int, str)) and str(v).isdigit() else v
                        elif k == "typography_font_family":
                            raw_settings["editor.fontFamily"] = str(v)
                        elif k == "high_contrast":
                            raw_settings["workbench.highContrast"] = bool(v)
                        elif k == "motion_reduced":
                            raw_settings["workbench.reduceMotion"] = "on" if v else "off"
                        elif k == "max_context_length":
                            raw_settings["cursor.composer.contextLimit"] = int(v)
                        elif k == "auto_run_commands":
                            raw_settings["cursor.composer.autoExecuteTerminal"] = bool(v)
                        elif k == "pr_review_assistant":
                            raw_settings["cursor.git.enablePullRequests"] = bool(v)
                        elif k == "multi_worktree_isolation":
                            raw_settings["cursor.worktrees.enabled"] = bool(v)
                        elif k == "suggestion_delay_ms":
                            raw_settings["cursor.cpp.triggerDelay"] = int(v)
                        elif k == "indexing_engine":
                            raw_settings["cursor.indexing.enable"] = (v != "off")
                        elif k == "symbol_graph_enabled":
                            raw_settings["cursor.codeIntelligence.preciseSymbols"] = bool(v)
                        elif k == "semantic_search_enabled":
                            raw_settings["cursor.codeIntelligence.semanticSearch"] = bool(v)

        # 1. AI Models & Inference
        if "default_model" in updates:
            model_id = str(updates["default_model"]).strip()
            raw_settings["cursor.chat.defaultModel"] = model_id
            raw_settings["cursor.general.model"] = model_id
            raw_settings["cursor.composer.defaultModel"] = model_id
        if "custom_model" in updates:
            raw_settings["cursor.chat.customModel"] = str(updates["custom_model"]).strip()
        if "thinking_effort" in updates:
            raw_settings["cursor.chat.thinkingEffort"] = str(updates["thinking_effort"]).strip()
        if "fast_mode" in updates:
            raw_settings["cursor.chat.fastMode"] = bool(updates["fast_mode"])
        if "openai_key" in updates:
            raw_settings["cursor.general.openAIKey"] = str(updates["openai_key"]).strip()
        if "anthropic_key" in updates:
            raw_settings["cursor.general.anthropicKey"] = str(updates["anthropic_key"]).strip()
        if "custom_base_url" in updates:
            raw_settings["cursor.general.customBaseUrl"] = str(updates["custom_base_url"]).strip()

        # 2. Privacy & Ghost Mode
        if "privacy_mode" in updates:
            is_private = bool(updates["privacy_mode"])
            if is_private:
                raw_settings["telemetry.telemetryLevel"] = "off"
                raw_settings["telemetry.enableCrashReporter"] = False
                raw_settings["cursor.ghostMode"] = True
                raw_settings["cursor.privacy.indexCodebaseLocally"] = True
            else:
                raw_settings["telemetry.telemetryLevel"] = "all"
                raw_settings["telemetry.enableCrashReporter"] = True
                raw_settings["cursor.ghostMode"] = False
        if "ghost_mode" in updates:
            raw_settings["cursor.ghostMode"] = bool(updates["ghost_mode"])
        if "telemetry_level" in updates:
            raw_settings["telemetry.telemetryLevel"] = updates["telemetry_level"]
        if "enable_crash_reporter" in updates:
            raw_settings["telemetry.enableCrashReporter"] = bool(updates["enable_crash_reporter"])
        if "index_locally_only" in updates:
            raw_settings["cursor.privacy.indexCodebaseLocally"] = bool(updates["index_locally_only"])

        # 3. Cursor Tab / CPP Features
        if "cpp_enable" in updates:
            raw_settings["cursor.cpp.enable"] = bool(updates["cpp_enable"])
        if "cpp_partial_accepts" in updates:
            raw_settings["cursor.cpp.enablePartialAccepts"] = bool(updates["cpp_partial_accepts"])
        if "cpp_auto_suggest" in updates:
            raw_settings["cursor.cpp.autoSuggest"] = bool(updates["cpp_auto_suggest"])
        if "cpp_disabled_languages" in updates and isinstance(updates["cpp_disabled_languages"], list):
            raw_settings["cursor.cpp.disabledLanguages"] = updates["cpp_disabled_languages"]
        if "cpp_trigger_delay" in updates:
            raw_settings["cursor.cpp.triggerDelay"] = int(updates["cpp_trigger_delay"])

        # 4. Composer & Agent Features
        if "composer_auto_apply" in updates:
            raw_settings["cursor.composer.autoApply"] = bool(updates["composer_auto_apply"])
        if "composer_agent_mode" in updates:
            raw_settings["cursor.composer.agentMode"] = bool(updates["composer_agent_mode"])
        if "composer_auto_execute_terminal" in updates:
            raw_settings["cursor.composer.autoExecuteTerminal"] = bool(updates["composer_auto_execute_terminal"])
        if "use_inline_diffs" in updates:
            raw_settings["cursor.diffs.useInlineDiffs"] = bool(updates["use_inline_diffs"])
        if "always_search_codebase" in updates:
            raw_settings["cursor.chat.alwaysSearchCodebase"] = bool(updates["always_search_codebase"])
        if "auto_scroll" in updates:
            raw_settings["cursor.general.autoScroll"] = bool(updates["auto_scroll"])
        if "indexing_enable" in updates:
            raw_settings["cursor.indexing.enable"] = bool(updates["indexing_enable"])

        # 5. Editor & UI Preferences
        if "font_size" in updates:
            raw_settings["editor.fontSize"] = int(updates["font_size"])
        if "font_family" in updates:
            raw_settings["editor.fontFamily"] = str(updates["font_family"])
        if "tab_size" in updates:
            raw_settings["editor.tabSize"] = int(updates["tab_size"])
        if "word_wrap" in updates:
            raw_settings["editor.wordWrap"] = str(updates["word_wrap"])
        if "minimap" in updates:
            raw_settings["editor.minimap.enabled"] = bool(updates["minimap"])
        if "format_on_save" in updates:
            raw_settings["editor.formatOnSave"] = bool(updates["format_on_save"])
        if "color_theme" in updates:
            raw_settings["workbench.colorTheme"] = str(updates["color_theme"])

        # 6. Proxy Configuration
        if "route_via_proxy" in updates:
            if bool(updates["route_via_proxy"]):
                port = int(updates.get("proxy_port", 8999))
                raw_settings["http.proxy"] = f"http://127.0.0.1:{port}"
                raw_settings["http.proxyStrictSSL"] = False
                raw_settings["http.proxySupport"] = "override"
            else:
                raw_settings.pop("http.proxy", None)
                raw_settings.pop("http.proxyStrictSSL", None)
                raw_settings.pop("http.proxySupport", None)
        elif "http_proxy" in updates:
            val = str(updates["http_proxy"]).strip()
            if val:
                raw_settings["http.proxy"] = val
                raw_settings["http.proxyStrictSSL"] = False
                raw_settings["http.proxySupport"] = "override"
            else:
                raw_settings.pop("http.proxy", None)

        if "proxy_strict_ssl" in updates:
            raw_settings["http.proxyStrictSSL"] = bool(updates["proxy_strict_ssl"])
        if "proxy_support" in updates:
            raw_settings["http.proxySupport"] = str(updates["proxy_support"])

        # 7. Additional settings for 8 authentic Cursor categories
        # General & Startup / Notification
        if "startup_editor" in updates:
            raw_settings["workbench.startupEditor"] = str(updates["startup_editor"])
        if "restore_windows" in updates:
            raw_settings["window.restoreWindows"] = str(updates["restore_windows"])
        if "dnd_mode" in updates:
            raw_settings["notifications.doNotDisturbMode"] = bool(updates["dnd_mode"])
        if "silent_notifications" in updates:
            raw_settings["notifications.silent"] = bool(updates["silent_notifications"])
            
        # Appearance extras
        if "preferred_dark_theme" in updates:
            raw_settings["workbench.preferredDarkColorTheme"] = str(updates["preferred_dark_theme"])
        if "agent_conversation_style" in updates:
            raw_settings["cursor.chat.agentConversationStyle"] = str(updates["agent_conversation_style"])
        if "icon_theme" in updates:
            raw_settings["workbench.iconTheme"] = str(updates["icon_theme"])
        if "line_height" in updates:
            raw_settings["editor.lineHeight"] = int(updates["line_height"])
        if "font_weight" in updates:
            raw_settings["editor.fontWeight"] = str(updates["font_weight"])
        if "high_contrast" in updates:
            raw_settings["workbench.highContrast"] = bool(updates["high_contrast"])
        if "motion" in updates:
            raw_settings["workbench.reduceMotion"] = str(updates["motion"])

        # Agent extras
        if "import_third_party_keys" in updates:
            raw_settings["cursor.agent.importThirdPartyKeys"] = bool(updates["import_third_party_keys"])
        if "composer_context_limit" in updates:
            raw_settings["cursor.composer.contextLimit"] = int(updates["composer_context_limit"])
        if "composer_use_tools" in updates:
            raw_settings["cursor.composer.useTools"] = bool(updates["composer_use_tools"])
        if "terminal_execution_policy" in updates:
            raw_settings["cursor.terminal.executionPolicy"] = str(updates["terminal_execution_policy"])

        # Git & PRs
        if "enable_pull_requests" in updates:
            raw_settings["cursor.git.enablePullRequests"] = bool(updates["enable_pull_requests"])
        if "auto_fetch" in updates:
            raw_settings["cursor.git.autoFetch"] = bool(updates["auto_fetch"])
        if "default_branch" in updates:
            raw_settings["cursor.git.defaultBranch"] = str(updates["default_branch"])
        if "prune_on_fetch" in updates:
            raw_settings["cursor.git.pruneOnFetch"] = bool(updates["prune_on_fetch"])
        if "co_authored_by_agent" in updates:
            raw_settings["cursor.git.coAuthoredByAgent"] = bool(updates["co_authored_by_agent"])
        if "include_attribution" in updates:
            raw_settings["cursor.git.includeAttribution"] = bool(updates["include_attribution"])

        # Worktrees
        if "worktrees_enabled" in updates:
            raw_settings["cursor.worktrees.enabled"] = bool(updates["worktrees_enabled"])
        if "worktrees_directory" in updates:
            raw_settings["cursor.worktrees.defaultDirectory"] = str(updates["worktrees_directory"])
        if "auto_create_branch" in updates:
            raw_settings["cursor.worktrees.autoCreateBranch"] = bool(updates["auto_create_branch"])
        if "prune_merged" in updates:
            raw_settings["cursor.worktrees.pruneMerged"] = bool(updates["prune_merged"])

        # Browser & Network extras
        if "proxy_bypass" in updates:
            raw_settings["http.proxyBypass"] = str(updates["proxy_bypass"])
        if "network_timeout" in updates:
            raw_settings["http.timeout"] = int(updates["network_timeout"])

        # Tab extras
        if "cpp_model" in updates:
            raw_settings["cursor.cpp.model"] = str(updates["cpp_model"])

        # Code Intelligence extras
        if "indexing_memory_mb" in updates:
            raw_settings["cursor.indexing.maxMemoryMB"] = int(updates["indexing_memory_mb"])
        if "semantic_search" in updates:
            raw_settings["cursor.codeIntelligence.semanticSearch"] = bool(updates["semantic_search"])
        if "embeddings_provider" in updates:
            raw_settings["cursor.codeIntelligence.embeddingsProvider"] = str(updates["embeddings_provider"])
        if "precise_symbols" in updates:
            raw_settings["cursor.codeIntelligence.preciseSymbols"] = bool(updates["precise_symbols"])
        if "symbol_hover" in updates:
            raw_settings["cursor.codeIntelligence.symbolHover"] = bool(updates["symbol_hover"])

        # 8. Custom extra fields
        if "custom" in updates and isinstance(updates["custom"], dict):
            for k, v in updates["custom"].items():
                raw_settings[k] = v

        # 8. Ghi vao settings.json
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump(raw_settings, f, indent=4, ensure_ascii=False)

        # 9. Cap nhat dong thoi auto_switch_config.json neu co cac truong lien quan
        autoswitch_keys = {"reset_mode", "auto_resend_action", "continue_prompt", "auto_rotate_enabled", "quota_threshold", "target_mode"}
        autoswitch_updates = {k: v for k, v in updates.items() if k in autoswitch_keys}
        if autoswitch_updates:
            self.update_auto_switch_config(autoswitch_updates)

        res = self.get_settings()
        res["success"] = True
        return res

    # =========================================================================
    # Auto-Switch & Continue Action Configuration
    # =========================================================================
    def get_auto_switch_config(self) -> Dict[str, Any]:
        """Doc cau hinh tu dong doi account va tiep tuc gui tin nhan."""
        cfg = dict(self.DEFAULT_AUTO_SWITCH_CONFIG)
        if os.path.exists(self.auto_switch_config_path):
            try:
                with open(self.auto_switch_config_path, "r", encoding="utf-8") as f:
                    data = json.loads(f.read().strip())
                    if isinstance(data, dict):
                        cfg.update(data)
            except Exception as e:
                print(f"[-] Loi doc auto_switch_config.json: {e}")
        return cfg

    def update_auto_switch_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Luu cau hinh tu dong doi account va tiep tuc gui tin nhan."""
        current = self.get_auto_switch_config()
        for k, v in updates.items():
            if k in current or k in ("reset_mode", "auto_resend_action", "continue_prompt", "auto_rotate_enabled", "quota_threshold", "target_mode", "antidetect_mode"):
                current[k] = v

        try:
            with open(self.auto_switch_config_path, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[-] Loi ghi auto_switch_config.json: {e}")

        return current

    # =========================================================================
    # 2. Hardware ID / Fingerprint Spoofer (storage.json)
    # =========================================================================
    def get_storage_ids(self) -> Dict[str, Any]:
        """Doc cac thong so fingerprint / Machine ID trong storage.json."""
        data = {}
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    c = f.read().strip()
                    if c:
                        data = json.loads(c)
            except Exception as e:
                print(f"[-] Loi doc storage.json: {e}")

        has_backup = os.path.exists(self.storage_bak_path)
        backup_mtime = None
        if has_backup:
            try:
                backup_mtime = int(os.path.getmtime(self.storage_bak_path))
            except Exception:
                pass

        return {
            "exists": os.path.exists(self.storage_path),
            "path": self.storage_path,
            "macMachineId": data.get("telemetry.macMachineId", ""),
            "machineId": data.get("telemetry.machineId", ""),
            "devDeviceId": data.get("telemetry.devDeviceId", ""),
            "sqmId": data.get("telemetry.sqmId", ""),
            "has_backup": has_backup,
            "backup_mtime": backup_mtime
        }

    def generate_fingerprint(self) -> Dict[str, Any]:
        """Sinh mot bo fingerprint phan cung moi (Anti-Detect Fingerprint Profile)."""
        new_mac = secrets.token_hex(32)
        new_mach = secrets.token_hex(32)
        new_dev = str(uuid.uuid4())
        new_sqm = "{" + str(uuid.uuid4()).upper() + "}"
        return {
            "fingerprint_id": new_mach[:8],
            "macMachineId": new_mac,
            "machineId": new_mach,
            "devDeviceId": new_dev,
            "sqmId": new_sqm,
            "created_at": int(time.time())
        }

    def apply_fingerprint(self, fp: Dict[str, Any]) -> bool:
        """Ap dung mot bo fingerprint phan cung co dinh vao storage.json cua Cursor."""
        if not fp or not isinstance(fp, dict):
            return False

        if not os.path.exists(self.storage_path):
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            data = {}
        else:
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    data = json.loads(f.read().strip() or "{}")
            except Exception:
                data = {}

        if os.path.exists(self.storage_path) and not os.path.exists(self.storage_bak_path):
            try:
                shutil.copy2(self.storage_path, self.storage_bak_path)
            except Exception:
                pass

        if "macMachineId" in fp:
            data["telemetry.macMachineId"] = fp["macMachineId"]
        if "machineId" in fp:
            data["telemetry.machineId"] = fp["machineId"]
        if "devDeviceId" in fp:
            data["telemetry.devDeviceId"] = fp["devDeviceId"]
        if "sqmId" in fp:
            data["telemetry.sqmId"] = fp["sqmId"]

        try:
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)

            try:
                import cursor_checksum
                cursor_checksum.reset_cached_machine_ids()
            except Exception:
                pass

            print(f"[+] Da ap dung Anti-Detect Fingerprint thanh cong: ID={fp.get('fingerprint_id', fp.get('machineId', '')[:8])}")
            return True
        except Exception as e:
            print(f"[-] Loi ghi storage.json apply_fingerprint: {e}")
            return False

    def spoof_storage_ids(self) -> Dict[str, Any]:
        """
        Randomize / Fake toan bo Hardware Machine IDs trong storage.json:
        - telemetry.macMachineId (64 hex characters)
        - telemetry.machineId (64 hex characters)
        - telemetry.devDeviceId (UUIDv4)
        - telemetry.sqmId ({UUIDv4})
        Tu dong tao backup truoc khi ghi de.
        """
        if not os.path.exists(self.storage_path):
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            data = {}
        else:
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    data = json.loads(f.read().strip() or "{}")
            except Exception:
                data = {}

        # Luon tao backup trang thai hien tai truoc khi ghi de de ho tro restore chinh xac
        if os.path.exists(self.storage_path):
            try:
                shutil.copy2(self.storage_path, self.storage_bak_path)
            except Exception as e:
                print(f"[-] Loi tao backup storage.json: {e}")

        # Sinh bo fingerprint moi ngau nhien chuan cryptographic
        new_mac_machine_id = secrets.token_hex(32)
        new_machine_id = secrets.token_hex(32)
        new_dev_device_id = str(uuid.uuid4())
        new_sqm_id = "{" + str(uuid.uuid4()).upper() + "}"

        data["telemetry.macMachineId"] = new_mac_machine_id
        data["telemetry.machineId"] = new_machine_id
        data["telemetry.devDeviceId"] = new_dev_device_id
        data["telemetry.sqmId"] = new_sqm_id

        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

        try:
            import cursor_checksum
            cursor_checksum.reset_cached_machine_ids()
        except Exception:
            pass

        print(f"[+] Da spoof Hardware Fingerprint thanh cong: MachineId={new_machine_id[:16]}...")
        return self.get_storage_ids()

    def restore_storage_ids(self) -> bool:
        """Khoi phuc storage.json tu file backup storage.json.bak."""
        if not os.path.exists(self.storage_bak_path):
            return False
        try:
            shutil.copy2(self.storage_bak_path, self.storage_path)
            try:
                import cursor_checksum
                cursor_checksum.reset_cached_machine_ids()
            except Exception:
                pass
            print("[+] Da khoi phuc Hardware IDs ban goc tu backup!")
            return True
        except Exception as e:
            print(f"[-] Loi khoi phuc storage.json: {e}")
            return False

    # =========================================================================
    # 3. .cursorrules Editor
    # =========================================================================
    DEFAULT_CURSORRULES_TEMPLATE = """# Cursor Rules & Guidelines
# Mode: Production-Grade Pair Programming

## Code Quality & Architecture
- Write clean, maintainable, modular code following SOLID principles.
- Use explicit type annotations and handle errors at system boundaries.
- Never write broken placeholder implementations or truncate responses.

## Performance & Optimization
- Prevent memory leaks and unbounded resource allocation.
- Prefer asynchronous non-blocking patterns where appropriate.

## Security
- Validate all user inputs and sanitize sensitive data before logging.
- Protect secrets, tokens, and credentials from being written to disk in plain text.
"""

    def get_cursorrules(self, custom_path: Optional[str] = None) -> Dict[str, Any]:
        """Doc noi dung file .cursorrules."""
        target = os.path.abspath(custom_path or self.cursorrules_path)
        exists = os.path.exists(target)
        content = ""
        if exists:
            try:
                with open(target, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception as e:
                print(f"[-] Loi doc .cursorrules: {e}")
        else:
            content = self.DEFAULT_CURSORRULES_TEMPLATE

        return {
            "exists": exists,
            "path": target,
            "content": content,
            "template": self.DEFAULT_CURSORRULES_TEMPLATE
        }

    def save_cursorrules(self, content: str, custom_path: Optional[str] = None) -> Dict[str, Any]:
        """Luu noi dung file .cursorrules."""
        target = os.path.abspath(custom_path or self.cursorrules_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            return {
                "success": True,
                "path": target,
                "bytes_written": len(content.encode("utf-8")),
                "message": "Đã lưu .cursorrules thành công"
            }
        except Exception as e:
            return {
                "success": False,
                "path": target,
                "error": str(e)
            }

    # =========================================================================
    # 4. CONFIG PROFILES & MULTI-ACCOUNT PRESET MANAGEMENT
    # =========================================================================
    def _init_profiles_file(self):
        """Khoi tao file cursor_config_profiles.json voi 4 profiles mac dinh neu chua co."""
        if not os.path.exists(self.profiles_path):
            data = {
                "active_profile": "default",
                "global_default_profile": "default",
                "profiles": copy.deepcopy(self.DEFAULT_PROFILES)
            }
            try:
                with open(self.profiles_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
            except Exception as e:
                print(f"[-] Loi khoi tao cursor_config_profiles.json: {e}")

    def get_all_profiles(self) -> Dict[str, Any]:
        """Lay toan bo danh sach Config Profiles, profile dang active va global default."""
        self._init_profiles_file()
        try:
            with open(self.profiles_path, "r", encoding="utf-8") as f:
                data = json.loads(f.read().strip() or "{}")
        except Exception as e:
            print(f"[-] Loi doc profiles: {e}")
            data = {
                "active_profile": "default",
                "global_default_profile": "default",
                "profiles": copy.deepcopy(self.DEFAULT_PROFILES)
            }

        # Dam bao luon ton tai it nhat 4 default profiles
        profiles = data.get("profiles", {})
        for k, v in self.DEFAULT_PROFILES.items():
            if k not in profiles:
                profiles[k] = copy.deepcopy(v)
        data["profiles"] = profiles

        if "active_profile" not in data:
            data["active_profile"] = "default"
        if "global_default_profile" not in data:
            data["global_default_profile"] = "default"

        return data

    def get_profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
        """Lay thong tin chi tiet cua mot profile."""
        all_data = self.get_all_profiles()
        return all_data.get("profiles", {}).get(profile_id)

    def save_profile(self, profile_id: str, profile_data: Dict[str, Any]) -> Dict[str, Any]:
        """Tao moi hoac cap nhat mot Config Profile."""
        all_data = self.get_all_profiles()
        profiles = all_data.get("profiles", {})

        pid = str(profile_id).strip().lower().replace(" ", "_")
        if not pid:
            pid = f"profile_{int(time.time())}"

        name = profile_data.get("name") or pid.title()
        description = profile_data.get("description") or "Custom configuration profile"
        badge = profile_data.get("badge") or "Custom"
        is_default = bool(profile_data.get("is_default", False))
        settings = profile_data.get("settings", {})

        # Neu settings truyen vao rong, lay tu settings hien tai cua Cursor
        if not settings:
            current_cursor = self.get_settings()
            settings = {
                k: current_cursor[k] for k in self.DEFAULT_PROFILES["default"]["settings"].keys()
                if k in current_cursor
            }

        profiles[pid] = {
            "id": pid,
            "name": name,
            "description": description,
            "badge": badge,
            "is_default": is_default,
            "settings": settings,
            "updated_at": int(time.time())
        }

        all_data["profiles"] = profiles
        if is_default:
            all_data["global_default_profile"] = pid

        with open(self.profiles_path, "w", encoding="utf-8") as f:
            json.dump(all_data, f, indent=4, ensure_ascii=False)

        return profiles[pid]

    def delete_profile(self, profile_id: str) -> bool:
        """Xoa mot custom profile (khong cho phep xoa profile 'default' mac dinh)."""
        if profile_id == "default":
            return False
        all_data = self.get_all_profiles()
        profiles = all_data.get("profiles", {})
        if profile_id in profiles:
            del profiles[profile_id]
            if all_data.get("active_profile") == profile_id:
                all_data["active_profile"] = "default"
            if all_data.get("global_default_profile") == profile_id:
                all_data["global_default_profile"] = "default"
            with open(self.profiles_path, "w", encoding="utf-8") as f:
                json.dump(all_data, f, indent=4, ensure_ascii=False)
            return True
        return False

    def set_global_default_profile(self, profile_id: str) -> bool:
        """Dat mot profile lam Global Default Profile (tu dong ap dung cho moi tai khoan)."""
        all_data = self.get_all_profiles()
        if profile_id in all_data.get("profiles", {}):
            all_data["global_default_profile"] = profile_id
            with open(self.profiles_path, "w", encoding="utf-8") as f:
                json.dump(all_data, f, indent=4, ensure_ascii=False)
            return True
        return False

    def apply_profile_to_cursor(self, profile_id_or_data: Any) -> Dict[str, Any]:
        """
        Ap dung toan bo thiet lap cua mot Profile vao settings.json va auto_switch_config.json cua Cursor.
        profile_id_or_data co the la profile_id (str) hoac dict chua settings.
        """
        if isinstance(profile_id_or_data, str):
            profile_id = profile_id_or_data
            prof = self.get_profile(profile_id)
            if not prof:
                # Fallback to default
                prof = self.get_profile("default") or self.DEFAULT_PROFILES["default"]
            settings = prof.get("settings", {})
        elif isinstance(profile_id_or_data, dict):
            profile_id = profile_id_or_data.get("id", "custom")
            settings = profile_id_or_data.get("settings", profile_id_or_data)
        else:
            return {"success": False, "error": "Invalid profile input"}

        # 1. Cap nhat vao settings.json va auto_switch_config.json
        updated = self.update_settings(settings)

        # 2. Ghi nhan active profile
        all_data = self.get_all_profiles()
        all_data["active_profile"] = profile_id
        try:
            with open(self.profiles_path, "w", encoding="utf-8") as f:
                json.dump(all_data, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

        return {
            "success": True,
            "profile_id": profile_id,
            "applied_settings": updated,
            "message": f"Đã áp dụng thành công hồ sơ cấu hình '{profile_id}' vào Cursor"
        }

    def export_profiles(self) -> Dict[str, Any]:
        """Xuat khau toan bo ho so cau hinh ra dictionary json de tai ve hoac sao luu."""
        data = self.get_all_profiles()
        return {
            "version": "2.0",
            "exported_at": int(time.time()),
            "active_profile": data.get("active_profile", "default"),
            "global_default_profile": data.get("global_default_profile", "default"),
            "profiles": data.get("profiles", {})
        }

    def import_profiles(self, import_data: Dict[str, Any]) -> Dict[str, Any]:
        """Nhap khau va merge danh sach ho so cau hinh tu file JSON."""
        if not isinstance(import_data, dict):
            return {"success": False, "error": "Dữ liệu JSON không hợp lệ"}

        incoming_profiles = import_data.get("profiles", {})
        if not isinstance(incoming_profiles, dict) or not incoming_profiles:
            # Thu xem import_data co truc tiep chua profiles khong
            if any(k in import_data for k in ("default", "settings", "name")):
                incoming_profiles = {"imported_profile": import_data}
            else:
                return {"success": False, "error": "Không tìm thấy hồ sơ cấu hình hợp lệ trong file"}

        all_data = self.get_all_profiles()
        profiles = all_data.get("profiles", {})

        count = 0
        for pid, pdata in incoming_profiles.items():
            if isinstance(pdata, dict) and "settings" in pdata:
                profiles[pid] = pdata
                count += 1

        all_data["profiles"] = profiles
        if "global_default_profile" in import_data and import_data["global_default_profile"] in profiles:
            all_data["global_default_profile"] = import_data["global_default_profile"]

        with open(self.profiles_path, "w", encoding="utf-8") as f:
            json.dump(all_data, f, indent=4, ensure_ascii=False)

        return {
            "success": True,
            "imported_count": count,
            "total_profiles": len(profiles),
            "message": f"Đã nhập khẩu thành công {count} hồ sơ cấu hình"
        }


# Module-level exports for convenient import
DEFAULT_PROFILES = CursorSettingsManager.DEFAULT_PROFILES


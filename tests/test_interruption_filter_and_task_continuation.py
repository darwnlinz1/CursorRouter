"""
Unit Test Suite: Interruption Filter, Task Continuation, & Workspace Organization
===================================================================================

Verifies:
1. Mid-flight interruption handling (e.g. task interrupted at 75% quota exhaustion)
   triggers the continuation prompt correctly across accounts.
2. Natural task completion (e.g. task completed naturally at 50% quota)
   strictly suppresses prompt-resend to avoid unnecessary re-testing.
3. Multi-account cascading continuation halts immediately once a task succeeds.
4. Workspace organization: START_HOST.bat launcher, scripts/ directory, and tests/.
"""

import os
import sys
import time
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure root directory is in sys.path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from smart_task_filter import (
    SmartTaskCompletionFilter,
    TaskStateTracker,
    mark_task_interrupted,
    mark_task_completed,
    consume_task_interruption,
    should_trigger_auto_continue
)
from account_pool import AccountPoolManager, DB_FILE, QUOTA_EXHAUSTION_THRESHOLD
from cursor_settings import CursorSettingsManager


class TestInterruptionFilterAndTaskContinuation(unittest.TestCase):
    def setUp(self):
        TaskStateTracker().reset()
        self.filter = SmartTaskCompletionFilter()

    def tearDown(self):
        TaskStateTracker().reset()

    # -------------------------------------------------------------------------
    # 1. Kịch bản tác vụ đang code dở (75%) bị ngắt do hết Quota
    # -------------------------------------------------------------------------
    def test_01_interrupted_mid_task_triggers_continuation(self):
        """
        Khi agent dang code task (vi du 75%) ma bi ngat boi quota / chat lock:
        Bo loc bat buoc phai cho phep gui lenh tiep tuc (continue prompt).
        """
        TaskStateTracker().reset()
        # Mo phong su kien ngat giua chung khi dang code task
        mark_task_interrupted(
            reason="Layer 1 (API Quota): totalSpend >= 100.0 (Agent interrupted at 75% progress)",
            request_id="task-code-75-pct",
            source="proxy_connect_rpc"
        )

        should_cont, reason = self.filter.should_auto_continue(
            auto_continue=None,
            auto_resend_cfg="auto",
            consume=True
        )

        self.assertTrue(should_cont, "Task bi ngat giua chung BAT BUOC phai cho phep tiep tuc!")
        self.assertIn("interrupted mid-flight", reason.lower())
        self.assertIn("75%", reason)

        # Kiem tra co che Idempotency (Consume): khong duoc gui lap lai
        should_cont_retry, reason_retry = self.filter.should_auto_continue(
            auto_continue=None,
            auto_resend_cfg="auto",
            consume=True
        )
        self.assertFalse(should_cont_retry, "Sau khi da tieu thu (consume), khong duoc phep gui tiep prompt!")
        self.assertIn("suppressed", reason_retry.lower())

    # -------------------------------------------------------------------------
    # 2. Kịch bản tác vụ đã hoàn thành tự nhiên (dù quota đang ở mức 50%)
    # -------------------------------------------------------------------------
    def test_02_natural_completion_strictly_suppresses_continue(self):
        """
        Khi du an da lam xong (outcome == success, natural completion):
        Du quota dang o 50%, bo loc van phai CHAN gui lenh tiep tuc de tranh test thua.
        """
        TaskStateTracker().reset()

        # Mo phong co canh bao quota nhung task da kip hoan thanh xong
        mark_task_interrupted("Quota warning 50%", request_id="task-done-1")
        time.sleep(0.01)
        # Agent tu ket thuc tu nhien voi thanh cong
        mark_task_completed(request_id="task-done-1", source="agent_success")

        should_cont, reason = self.filter.should_auto_continue(
            auto_continue=None,
            auto_resend_cfg="auto",
            consume=True
        )

        self.assertFalse(should_cont, "Task da xong tu nhien TUYET DOI KHONG duoc gui them prompt test!")
        self.assertIn("completed or ended naturally", reason.lower())

    # -------------------------------------------------------------------------
    # 3. Kịch bản chuỗi chuyển đổi nhiều tài khoản (Cascading Handoff)
    # -------------------------------------------------------------------------
    def test_03_cascading_handoff_stops_cleanly_when_task_completes(self):
        """
        Acc 1 bi ngat (75%) -> Acc 2 nhan va tiep tuc
        Acc 2 bi ngat tiep -> Acc 3 nhan va tiep tuc
        Acc 3 hoan thanh xong -> Chuoi dung lai hoan toan.
        """
        tracker = TaskStateTracker()
        tracker.reset()

        # Buoc 1: Acc 1 ngat tai 75%
        tracker.mark_interrupted("Acc 1 exhausted", request_id="hop-1")
        cont_hop1, _ = self.filter.should_auto_continue(consume=True)
        self.assertTrue(cont_hop1, "Hop 1 phai cho phep tiep tuc sang Acc 2")

        # Buoc 2: Acc 2 dang chay tiep thi lai bi ngat (sau mot khoang thoi gian sinh code)
        time.sleep(0.02)
        tracker.mark_interrupted("Acc 2 exhausted", request_id="hop-2")
        cont_hop2, _ = self.filter.should_auto_continue(consume=True)
        self.assertTrue(cont_hop2, "Hop 2 phai cho phep tiep tuc sang Acc 3")

        # Buoc 3: Acc 3 hoan thanh task thanh cong
        time.sleep(0.02)
        tracker.mark_completed(request_id="hop-3")
        cont_hop3, reason_hop3 = self.filter.should_auto_continue(consume=True)
        self.assertFalse(cont_hop3, "Hop 3 da xong phai dung han, khong duoc gui lenh tiep tuc!")
        self.assertIn("naturally", reason_hop3.lower())

    # -------------------------------------------------------------------------
    # 4. Kịch bản cấu hình Continue Prompt tùy biến ("Tiếp tục unit test")
    # -------------------------------------------------------------------------
    def test_04_custom_continue_prompt_configuration(self):
        """
        Kiem tra cau hinh continue_prompt tuy bien ("Tiếp tục unit test" hoac "Tiếp tục").
        """
        mgr = CursorSettingsManager()
        current_cfg = mgr.get_auto_switch_config()
        self.assertIn("continue_prompt", current_cfg)
        self.assertIn("reset_mode", current_cfg)
        self.assertIn("auto_resend_action", current_cfg)
        self.assertEqual(current_cfg.get("reset_mode"), "hard_restart")

        # Thu thay doi prompt thanh "Tiếp tục unit test"
        mgr.update_auto_switch_config({"continue_prompt": "Tiếp tục unit test"})
        updated_cfg = mgr.get_auto_switch_config()
        self.assertEqual(updated_cfg.get("continue_prompt"), "Tiếp tục unit test")

        # Khoi phuc lai ve mac dinh "Tiếp tục"
        mgr.update_auto_switch_config({"continue_prompt": "Tiếp tục"})


class TestWorkspaceStructureAndLauncher(unittest.TestCase):
    """Kiem tra tinh toan ven cua file START_HOST.bat va cau truc thu muc scripts/ va tests/."""

    def test_01_main_unified_launcher_exists_and_valid(self):
        """main.py phai ton tai o root, co ma hoa UTF-8 va ho tro day du cac mode khoi chay."""
        main_path = os.path.join(ROOT_DIR, "main.py")
        self.assertTrue(os.path.exists(main_path), "File main.py phai ton tai o root!")

        with open(main_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        self.assertIn("utf-8", content.lower(), "main.py phai cau hinh ma hoa UTF-8!")
        self.assertTrue("server" in content or "app" in content, "main.py phai goi server hoac native app!")
        self.assertTrue(any(arg in content for arg in ("--native", "--app")), "main.py phai ho tro routing native app mode!")

        # Kiem tra main.py va native_app.py ton tai o root va src/server.py ton tai o src/
        self.assertTrue(os.path.exists(os.path.join(ROOT_DIR, "main.py")), "main.py phai ton tai o root!")
        self.assertTrue(os.path.exists(os.path.join(ROOT_DIR, "native_app.py")), "native_app.py phai ton tai o root!")
        self.assertTrue(os.path.exists(os.path.join(ROOT_DIR, "src", "server.py")), "server.py phai ton tai trong src/!")

    def test_02_scripts_folder_integrity(self):
        """Thu muc scripts/ phai chua cac script phu tro va deu duoc cau hinh ROOT_DIR."""
        scripts_dir = os.path.join(ROOT_DIR, "scripts")
        self.assertTrue(os.path.isdir(scripts_dir), "Thu muc scripts/ phai ton tai!")

        expected_scripts = [
            "check_current_state.py",
            "check_quota_accounts.py",
            "scan_all_sand.py",
            "send_prompt_to_cursor.py",
            "inspect_composer.py",
            "inspect_logs.py",
            "README.md"
        ]

        for s in expected_scripts:
            sp = os.path.join(scripts_dir, s)
            self.assertTrue(os.path.exists(sp), f"Script {s} phai ton tai trong scripts/!")

        # Kiem tra ROOT_DIR duoc chen vao check_current_state.py
        with open(os.path.join(scripts_dir, "check_current_state.py"), "r", encoding="utf-8") as f:
            c = f.read()
        self.assertIn("ROOT_DIR", c, "check_current_state.py phai duoc cau hinh ROOT_DIR!")

    def test_03_pytest_ini_and_conftest_configured(self):
        """pytest.ini va tests/conftest.py phai duoc thiet lap dung de test chay doc lap."""
        ini_path = os.path.join(ROOT_DIR, "pytest.ini")
        self.assertTrue(os.path.exists(ini_path), "pytest.ini phai ton tai o root!")

        with open(ini_path, "r", encoding="utf-8") as f:
            ini_content = f.read()
        self.assertIn("norecursedirs", ini_content)

        conftest_path = os.path.join(ROOT_DIR, "tests", "conftest.py")
        self.assertTrue(os.path.exists(conftest_path), "tests/conftest.py phai ton tai!")

        with open(conftest_path, "r", encoding="utf-8") as f:
            conftest_content = f.read()
        self.assertIn("ROOT_DIR", conftest_content)

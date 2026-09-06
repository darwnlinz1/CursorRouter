import os
import sys
import json
import pytest
import sqlite3
import tempfile
import shutil

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cursor_settings import CursorSettingsManager
from account_pool import AccountPoolManager
import server

@pytest.fixture
def temp_workspace(monkeypatch):
    temp_dir = tempfile.mkdtemp(prefix="cursor_antidetect_test_")
    cursor_user_dir = os.path.join(temp_dir, "CursorUser")
    global_storage_dir = os.path.join(cursor_user_dir, "globalStorage")
    os.makedirs(global_storage_dir, exist_ok=True)

    storage_path = os.path.join(global_storage_dir, "storage.json")
    with open(storage_path, "w", encoding="utf-8") as f:
        json.dump({
            "telemetry.macMachineId": "initial_mac_id",
            "telemetry.machineId": "initial_machine_id",
            "telemetry.devDeviceId": "initial_dev_id",
            "telemetry.sqmId": "{INITIAL-SQM-ID}"
        }, f)

    db_path = os.path.join(temp_dir, "test_accounts.db")
    config_path = os.path.join(temp_dir, "auto_switch_config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({
            "auto_rotate_enabled": True,
            "quota_threshold": 100.0,
            "reset_mode": "hard_restart",
            "auto_resend_action": "auto",
            "continue_prompt": "Tiếp tục",
            "target_mode": "composer",
            "antidetect_mode": "account_locked"
        }, f)

    monkeypatch.setenv("CURSOR_ACCOUNTS_DB", db_path)
    monkeypatch.setattr(server, "DB_FILE", db_path)
    
    yield {
        "root": temp_dir,
        "cursor_user": cursor_user_dir,
        "storage_path": storage_path,
        "db_path": db_path,
        "config_path": config_path
    }

    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        pass


def test_generate_fingerprint():
    csm = CursorSettingsManager()
    fp = csm.generate_fingerprint()

    assert isinstance(fp, dict)
    assert "macMachineId" in fp
    assert "machineId" in fp
    assert "devDeviceId" in fp
    assert "sqmId" in fp
    assert "fingerprint_id" in fp
    assert len(fp["macMachineId"]) == 64
    assert len(fp["machineId"]) == 64
    assert fp["fingerprint_id"] == fp["machineId"][:8]
    assert fp["sqmId"].startswith("{") and fp["sqmId"].endswith("}")


def test_apply_fingerprint(temp_workspace):
    csm = CursorSettingsManager(cursor_user_dir=temp_workspace["cursor_user"])
    csm.storage_path = temp_workspace["storage_path"]
    csm.storage_bak_path = os.path.join(temp_workspace["cursor_user"], "globalStorage", "storage.json.bak")

    fp = csm.generate_fingerprint()
    success = csm.apply_fingerprint(fp)
    assert success is True

    with open(temp_workspace["storage_path"], "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["telemetry.machineId"] == fp["machineId"]
    assert data["telemetry.macMachineId"] == fp["macMachineId"]
    assert data["telemetry.devDeviceId"] == fp["devDeviceId"]
    assert data["telemetry.sqmId"] == fp["sqmId"]
    assert os.path.exists(csm.storage_bak_path)


def test_account_pool_fingerprint_persistence(temp_workspace, monkeypatch):
    cookies_dir = os.path.join(temp_workspace["root"], "Cookies")
    os.makedirs(cookies_dir, exist_ok=True)
    monkeypatch.setattr("account_pool.DB_FILE", temp_workspace["db_path"])

    pool = AccountPoolManager(cookies_dir=cookies_dir)

    con = sqlite3.connect(temp_workspace["db_path"])
    cur = con.cursor()
    cur.execute("""
        INSERT INTO accounts (email, display_name, access_token, usage_percent, status)
        VALUES ('test_user@example.com', 'Test User', 'token123', 25.0, 'READY')
    """)
    acc_id = cur.lastrowid
    con.commit()
    con.close()

    fp1 = pool.get_account_fingerprint(acc_id)
    assert fp1 is not None
    assert "machineId" in fp1
    fpid1 = fp1.get("fingerprint_id")

    fp2 = pool.get_account_fingerprint(acc_id)
    assert fp2["machineId"] == fp1["machineId"]
    assert fp2["fingerprint_id"] == fpid1

    new_fp = pool.regenerate_account_fingerprint(acc_id)
    assert new_fp["machineId"] != fp1["machineId"]
    assert new_fp["fingerprint_id"] != fpid1

    all_accs = pool.get_all_accounts()
    matching = [a for a in all_accs if a["id"] == acc_id]
    assert len(matching) == 1
    assert matching[0]["fingerprint_id"] == new_fp["fingerprint_id"]


def test_server_antidetect_endpoints(temp_workspace, monkeypatch):
    cookies_dir = os.path.join(temp_workspace["root"], "Cookies")
    os.makedirs(cookies_dir, exist_ok=True)
    monkeypatch.setattr("account_pool.DB_FILE", temp_workspace["db_path"])
    monkeypatch.setattr("server.DB_FILE", temp_workspace["db_path"])

    csm = CursorSettingsManager(
        workspace_dir=temp_workspace["root"],
        cursor_user_dir=temp_workspace["cursor_user"]
    )
    csm.storage_path = temp_workspace["storage_path"]
    csm.auto_switch_config_path = temp_workspace["config_path"]
    monkeypatch.setattr(server, "settings_manager", csm)

    pool = AccountPoolManager(cookies_dir=cookies_dir)
    monkeypatch.setattr(server, "pool", pool)

    con = sqlite3.connect(temp_workspace["db_path"])
    cur = con.cursor()
    cur.execute("""
        INSERT INTO accounts (email, display_name, access_token, usage_percent, status)
        VALUES ('antidetect_api@example.com', 'API User', 'tok999', 10.0, 'READY')
    """)
    acc_id = cur.lastrowid
    con.commit()
    con.close()

    client = server.app.test_client()

    resp = client.get("/api/antidetect/config")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["antidetect_mode"] == "account_locked"
    assert "allowed_modes" in data

    resp = client.post("/api/antidetect/config", json={"antidetect_mode": "stealth_randomize"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["antidetect_mode"] == "stealth_randomize"

    resp = client.post("/api/antidetect/config", json={"antidetect_mode": "invalid_mode_xyz"})
    assert resp.status_code == 400

    resp = client.get(f"/api/antidetect/account/{acc_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "fingerprint" in data
    orig_fpid = data["fingerprint"]["fingerprint_id"]

    resp = client.post(f"/api/antidetect/regenerate/{acc_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    new_fpid = data["fingerprint"]["fingerprint_id"]
    assert new_fpid != orig_fpid

    resp = client.post(f"/api/antidetect/apply/{acc_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True

    with open(temp_workspace["storage_path"], "r", encoding="utf-8") as f:
        stored = json.load(f)
    assert stored["telemetry.machineId"] == data["fingerprint"]["machineId"]

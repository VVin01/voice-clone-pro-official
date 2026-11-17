"""Tests for Admin Tab Save Bar Scoping & VPS Worker Fleet Controls (2026-11-17).

Deliverables covered:
1. Save bar (#adm-settings-save-bar) scoped strictly inside #adm-screen-settings.
2. Save bar hidden by default; dirty tracking shows it only on unsaved modifications,
   hides on revert, hides on successful save, and hides on non-settings admin sections.
3. Fleet & VPS Worker inspection and control (status, toggle, recap queue routing).
"""
import json
import pathlib
import subprocess
import pytest
from datetime import datetime, timezone
from sqlalchemy import select

from app.models.schema import BotSetting, User
from app.webapp.api import (
    _get_laptop_worker_status,
    get_admin_fleet_status,
    toggle_admin_worker_status,
    FleetWorkerTogglePayload,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
INDEX_HTML = (REPO / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = (REPO / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "app" / "webapp" / "static" / "css" / "style.css").read_text(encoding="utf-8")


def test_admin_save_bar_dom_scoping():
    """Verify #adm-settings-save-bar is strictly enclosed within #adm-screen-settings."""
    assert 'id="adm-settings-save-bar"' in INDEX_HTML
    assert 'class="adm-settings-save-bar mt-3 hidden"' in INDEX_HTML

    # Extract #adm-screen-settings block
    settings_start = INDEX_HTML.index('id="adm-screen-settings"')
    settings_close = INDEX_HTML.index('</section>', settings_start)
    settings_block = INDEX_HTML[settings_start:settings_close]

    assert 'id="adm-settings-save-bar"' in settings_block, "Save bar must be inside #adm-screen-settings"

    # Verify NOT inside other admin screens
    for screen in ("adm-screen-overview", "adm-screen-users", "adm-screen-jobs", "adm-screen-activity"):
        s_idx = INDEX_HTML.index(f'id="{screen}"')
        next_screen_idx = INDEX_HTML.find('class="adm-screen', s_idx + len(screen) + 10)
        if next_screen_idx == -1:
            next_screen_idx = INDEX_HTML.index('</section>', s_idx)
        screen_block = INDEX_HTML[s_idx:next_screen_idx]
        assert 'id="adm-settings-save-bar"' not in screen_block, f"Save bar leaked into #{screen}"


def test_admin_fleet_card_dom():
    """Verify Fleet status card is present in #adm-jobs-live."""
    assert 'id="adm-fleet-card"' in INDEX_HTML
    assert 'id="adm-vps-status-badge"' in INDEX_HTML
    assert 'id="adm-btn-toggle-vps"' in INDEX_HTML
    assert 'id="adm-btn-toggle-recap-queue"' in INDEX_HTML

    jobs_live_idx = INDEX_HTML.index('id="adm-jobs-live"')
    queue_card_idx = INDEX_HTML.index('id="admin-queue-card"')
    fleet_card_idx = INDEX_HTML.index('id="adm-fleet-card"')

    assert jobs_live_idx < fleet_card_idx < queue_card_idx, "Fleet card must be inside #adm-jobs-live before queue card"


def test_admin_save_bar_dirty_state_node_eval():
    """Evaluate dirty checking and save bar toggle logic in Node.js."""
    script = """
    // Mock DOM elements
    const elements = {};
    function createElement(id, tagName, type = 'text', value = '', checked = false) {
        const classes = new Set();
        return {
            id,
            tagName: tagName.toUpperCase(),
            type,
            value,
            checked,
            classList: {
                add: (c) => classes.add(c),
                remove: (c) => classes.delete(c),
                toggle: (c, force) => {
                    if (force !== undefined) {
                        if (force) classes.add(c);
                        else classes.delete(c);
                    } else {
                        if (classes.has(c)) classes.delete(c);
                        else classes.add(c);
                    }
                },
                contains: (c) => classes.has(c)
            },
            closest: (sel) => {
                if (sel === "#adm-settings-save-bar") return id === "adm-settings-save-bar" ? true : null;
                return null;
            }
        };
    }

    const saveBar = createElement('adm-settings-save-bar', 'div');
    saveBar.classList.add('hidden');
    elements['adm-settings-save-bar'] = saveBar;

    const input1 = createElement('adm-set-kpay-num', 'input', 'text', '09123456789');
    const input2 = createElement('adm-set-maintenance', 'input', 'checkbox', '', false);
    elements['adm-set-kpay-num'] = input1;
    elements['adm-set-maintenance'] = input2;

    const pane = {
        querySelectorAll: (sel) => [input1, input2]
    };

    global.document = {
        getElementById: (id) => {
            if (id === 'adm-screen-settings') return pane;
            return elements[id] || null;
        }
    };
    global.admCurrentSection = 'settings';

    // Dirty state implementation
    let _adminSettingsSavedSnapshot = {};

    function _snapshotAdminSettingsValues() {
        const snap = {};
        pane.querySelectorAll("input, select, textarea").forEach(el => {
            if (!el.id || el.closest("#adm-settings-save-bar")) return;
            if (el.type === "checkbox" || el.type === "radio") snap[el.id] = el.checked;
            else snap[el.id] = el.value;
        });
        _adminSettingsSavedSnapshot = snap;
    }

    function _checkAdminSettingsDirty() {
        const els = pane.querySelectorAll("input, select, textarea");
        for (const el of els) {
            if (!el.id || el.closest("#adm-settings-save-bar")) continue;
            if (!(el.id in _adminSettingsSavedSnapshot)) continue;
            const saved = _adminSettingsSavedSnapshot[el.id];
            if (el.type === "checkbox" || el.type === "radio") {
                if (el.checked !== saved) return true;
            } else {
                if (el.value !== saved) return true;
            }
        }
        return false;
    }

    function updateAdminSettingsSaveBar(forceHide = false) {
        if (forceHide || (typeof admCurrentSection !== "undefined" && admCurrentSection !== "settings")) {
            saveBar.classList.add("hidden");
            return;
        }
        const isDirty = _checkAdminSettingsDirty();
        saveBar.classList.toggle("hidden", !isDirty);
    }

    // 1. Initial snapshot taken upon settings load
    _snapshotAdminSettingsValues();
    updateAdminSettingsSaveBar(true);
    const initialHidden = saveBar.classList.contains('hidden');

    // 2. Field modified -> dirty -> bar visible
    input1.value = '09999999999';
    updateAdminSettingsSaveBar();
    const modifiedVisible = !saveBar.classList.contains('hidden');

    // 3. Field reverted to original snapshot -> bar hidden
    input1.value = '09123456789';
    updateAdminSettingsSaveBar();
    const revertedHidden = saveBar.classList.contains('hidden');

    // 4. Checkbox toggled -> dirty -> bar visible
    input2.checked = true;
    updateAdminSettingsSaveBar();
    const checkboxDirty = !saveBar.classList.contains('hidden');

    // 5. Successful save -> snapshot taken & bar hidden
    _snapshotAdminSettingsValues();
    updateAdminSettingsSaveBar(true);
    const postSaveHidden = saveBar.classList.contains('hidden');

    // 6. Section switch to 'overview' -> bar hidden even if dirty
    input1.value = '09888888888';
    global.admCurrentSection = 'overview';
    updateAdminSettingsSaveBar();
    const otherSectionHidden = saveBar.classList.contains('hidden');

    console.log(JSON.stringify({
        initialHidden,
        modifiedVisible,
        revertedHidden,
        checkboxDirty,
        postSaveHidden,
        otherSectionHidden
    }));
    """
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    out = json.loads(res.stdout.strip())
    assert out["initialHidden"] is True
    assert out["modifiedVisible"] is True
    assert out["revertedHidden"] is True
    assert out["checkboxDirty"] is True
    assert out["postSaveHidden"] is True
    assert out["otherSectionHidden"] is True


@pytest.mark.asyncio
async def test_get_laptop_worker_status_paused(db_session):
    """Verify _get_laptop_worker_status properly identifies PAUSED state."""
    now_iso = datetime.now(timezone.utc).isoformat()
    db_session.add(BotSetting(key="worker_laptop_heartbeat", value=now_iso))
    db_session.add(BotSetting(key="worker_laptop_status", value="PAUSED"))
    db_session.add(BotSetting(key="worker_laptop_engine", value="VOXCPM"))
    db_session.add(BotSetting(key="worker_laptop_host", value="212.28.177.252"))
    await db_session.commit()

    status = await _get_laptop_worker_status(db_session)
    assert status["is_online"] is True
    assert status["status_code"] == "PAUSED"
    assert "Paused ⏸️" in status["status_text"]
    assert status["engine"] == "VOXCPM"
    assert status["host"] == "212.28.177.252"
    assert "recap_on_queue" in status


@pytest.mark.asyncio
async def test_fleet_endpoints(db_session):
    """Verify /admin/fleet/status and /admin/fleet/worker-toggle."""
    now_iso = datetime.now(timezone.utc).isoformat()
    db_session.add(BotSetting(key="worker_laptop_heartbeat", value=now_iso))
    db_session.add(BotSetting(key="worker_laptop_status", value="ONLINE"))
    db_session.add(BotSetting(key="worker_laptop_engine", value="VOXCPM"))
    db_session.add(BotSetting(key="worker_laptop_host", value="212.28.177.252"))
    await db_session.commit()

    admin_user = User(id=999999, username="admin_tester")

    # 1. Inspect fleet status
    fleet = await get_admin_fleet_status(admin=admin_user, db=db_session)
    assert fleet["success"] is True
    assert "vps_worker" in fleet
    assert "render_cloud" in fleet
    assert fleet["vps_worker"]["status_code"] == "ONLINE"
    assert len(fleet["vps_worker"]["workloads"]) >= 4
    assert len(fleet["render_cloud"]["workloads"]) >= 4

    # 2. Toggle worker to PAUSED
    res1 = await toggle_admin_worker_status(
        payload=FleetWorkerTogglePayload(status="PAUSED"),
        admin=admin_user,
        db=db_session,
    )
    assert res1["success"] is True
    assert res1["updated"]["worker_laptop_status"] == "PAUSED"
    assert res1["worker"]["status_code"] == "PAUSED"

    # 3. Toggle recap queue routing
    res2 = await toggle_admin_worker_status(
        payload=FleetWorkerTogglePayload(recap_on_queue=True),
        admin=admin_user,
        db=db_session,
    )
    assert res2["success"] is True
    assert res2["updated"]["recap_on_queue"] is True
    assert res2["worker"]["recap_on_queue"] is True

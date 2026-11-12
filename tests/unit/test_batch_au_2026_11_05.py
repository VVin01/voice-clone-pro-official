"""Batch AU — render quality split by tier (owner order 2026-11-05).

The Recap quality selector changes encoder effort only. FREE users are
clamped to Standard; PURCHASED users and admins keep Standard, High or
Max. The clamp is deliberately placed at both ends of the request's
life:

- ``create_recap_job`` is the only user-reachable endpoint that accepts
  a fresh ``RecapConfigRequest`` (the user retry endpoint delegates to
  it), and it clamps before the config is persisted or dispatched;
- ``_execute_recap_background_job`` is the choke point for both
  downstream ``render_quality=req.render_quality`` usages — the main
  recap render and the optional post-render split — so a config
  restored from a persisted job or a script-review snapshot is clamped
  before either encoder call can see it.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import settings
from app.models.enums import AccountCategory
from app.models.schema import Job
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod


STATIC_DIR = Path(api_mod.__file__).resolve().parent / "static"
APP_JS = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (STATIC_DIR / "css" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
API_PY = Path(api_mod.__file__).read_text(encoding="utf-8")


@pytest.fixture
def recap_runner(monkeypatch):
    """Capture background-job dispatches instead of running renders."""
    calls = []

    async def _fake_runner(job_id, req, user_id, chat_id, **kwargs):
        calls.append(
            {"job_id": job_id, "req": req, "user_id": user_id, "kwargs": kwargs}
        )

    monkeypatch.setattr(api_mod, "_run_recap_background_job", _fake_runner)
    yield calls
    api_mod.RECAP_JOBS_REGISTRY.clear()


async def _make_user(db_session, telegram_id, **fields):
    user, _ = await get_or_create_user(db_session, telegram_id=telegram_id)
    for key, value in fields.items():
        setattr(user, key, value)
    await db_session.commit()
    return user


def _req(quality):
    return api_mod.RecapConfigRequest(
        url="",
        title="Test Movie",
        voice_slug="komin",
        duration_seconds=60,
        render_quality=quality,
    )


async def _submit(db_session, recap_runner, user, quality):
    req = _req(quality)
    resp = await api_mod.create_recap_job(
        req, user=user, db=db_session, _gate=user
    )
    # create_recap_job dispatches with asyncio.create_task.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert resp["success"] is True
    assert len(recap_runner) == 1
    return resp, req


def _clamp_logs(caplog):
    return [
        record
        for record in caplog.records
        if "Recap render quality clamped" in record.getMessage()
    ]


# ── AU-1: submit-path clamp ─────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("quality", ["high", "max"])
async def test_au_free_submit_clamps_to_standard(
    db_session, recap_runner, caplog, quality
):
    user = await _make_user(
        db_session,
        890101,
        account_category=AccountCategory.FREE.value,
        recap_credits=2,
    )
    with caplog.at_level(logging.INFO, logger="app.webapp.api"):
        resp, req = await _submit(db_session, recap_runner, user, quality)

    assert req.render_quality == "standard"
    assert recap_runner[0]["req"].render_quality == "standard"

    job = await db_session.get(Job, resp["recap_id"])
    state = json.loads(job.recap_config)
    assert state["config"]["render_quality"] == "standard"

    logs = _clamp_logs(caplog)
    assert len(logs) == 1
    message = logs[0].getMessage()
    assert str(user.id) in message
    assert f"requested={quality}" in message
    assert "applied=standard" in message


@pytest.mark.asyncio
async def test_au_free_submit_standard_is_untouched(
    db_session, recap_runner, caplog
):
    user = await _make_user(
        db_session,
        890102,
        account_category=AccountCategory.FREE.value,
        recap_credits=2,
    )
    with caplog.at_level(logging.INFO, logger="app.webapp.api"):
        resp, req = await _submit(db_session, recap_runner, user, "standard")

    assert req.render_quality == "standard"
    job = await db_session.get(Job, resp["recap_id"])
    state = json.loads(job.recap_config)
    assert state["config"]["render_quality"] == "standard"
    assert _clamp_logs(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("quality", ["high", "max"])
async def test_au_purchased_submit_passes_quality_through(
    db_session, recap_runner, caplog, quality
):
    user = await _make_user(
        db_session,
        890103,
        account_category=AccountCategory.PURCHASED.value,
        recap_credits=2,
    )
    with caplog.at_level(logging.INFO, logger="app.webapp.api"):
        resp, req = await _submit(db_session, recap_runner, user, quality)

    assert req.render_quality == quality
    assert recap_runner[0]["req"].render_quality == quality
    job = await db_session.get(Job, resp["recap_id"])
    state = json.loads(job.recap_config)
    assert state["config"]["render_quality"] == quality
    assert _clamp_logs(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("quality", ["high", "max"])
async def test_au_admin_submit_passes_quality_through(
    db_session, recap_runner, caplog, monkeypatch, quality
):
    admin = await _make_user(db_session, 890104, recap_credits=0)
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin.id))
    with caplog.at_level(logging.INFO, logger="app.webapp.api"):
        resp, req = await _submit(db_session, recap_runner, admin, quality)

    assert req.render_quality == quality
    assert recap_runner[0]["req"].render_quality == quality
    job = await db_session.get(Job, resp["recap_id"])
    state = json.loads(job.recap_config)
    assert state["config"]["render_quality"] == quality
    assert _clamp_logs(caplog) == []


def test_au_junk_render_quality_is_still_rejected():
    with pytest.raises(ValidationError) as exc:
        _req("ultra")
    assert "Unsupported render_quality" in str(exc.value)


# ── AU-1: execution choke point for both downstream usages ──────────

@pytest.mark.parametrize("quality", ["high", "max"])
def test_au_execution_tier_flag_clamps_restored_configs(caplog, quality):
    req = _req(quality)
    with caplog.at_level(logging.INFO, logger="app.webapp.api"):
        applied = api_mod._clamp_recap_render_quality(
            req, user_id=890201, is_vip=False
        )
    assert applied == "standard"
    assert req.render_quality == "standard"
    assert len(_clamp_logs(caplog)) == 1


@pytest.mark.parametrize("quality", ["high", "max"])
def test_au_execution_tier_flag_passes_paid_and_admin(caplog, quality):
    req = _req(quality)
    with caplog.at_level(logging.INFO, logger="app.webapp.api"):
        applied = api_mod._clamp_recap_render_quality(
            req, user_id=890202, is_vip=True
        )
    assert applied == quality
    assert req.render_quality == quality
    assert _clamp_logs(caplog) == []


def test_au_helper_reuses_purchased_or_admin_tier_test(monkeypatch):
    purchased = SimpleNamespace(
        id=890203, account_category=AccountCategory.PURCHASED.value
    )
    req = _req("max")
    assert (
        api_mod._clamp_recap_render_quality(req, user=purchased) == "max"
    )

    admin = SimpleNamespace(
        id=890204, account_category=AccountCategory.FREE.value
    )
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin.id))
    req = _req("high")
    assert api_mod._clamp_recap_render_quality(req, user=admin) == "high"


def test_au_clamp_is_before_persist_dispatch_and_both_render_usages():
    create_src = inspect.getsource(api_mod.create_recap_job)
    clamp_at = create_src.index("_clamp_recap_render_quality")
    assert clamp_at < create_src.index("_persisted_recap_state(")
    assert clamp_at < create_src.index(
        "asyncio.create_task(_run_recap_background_job("
    )

    execute_src = inspect.getsource(api_mod._execute_recap_background_job)
    assert execute_src.count("render_quality=req.render_quality") == 2
    assert execute_src.index(
        "_clamp_recap_render_quality"
    ) < execute_src.index("render_quality=req.render_quality")


# ── AU-2/AU-3: client lock, with no new i18n string ─────────────────

def test_au_client_quality_lock_uses_account_category_and_admin():
    assert "function canUseRecapRenderQuality()" in APP_JS
    block = APP_JS[
        APP_JS.index("function canUseRecapRenderQuality()") :
        APP_JS.index("window.refreshRecapQualityLock")
    ]
    assert 'u.account_category === "PURCHASED"' in block
    assert "u.is_admin" in block


def test_au_client_free_quality_lock_disables_and_clamps_restored_pref():
    block = APP_JS[
        APP_JS.index("function refreshRecapQualityLock()") :
        APP_JS.index("window.refreshRecapQualityLock")
    ]
    assert 'state.recapRenderQuality = "standard"' in block
    assert 'quality === "high" || quality === "max"' in block
    assert "btn.disabled = locked" in block
    assert 'btn.classList.toggle("quality-locked", locked)' in block

    restore_start = APP_JS.index("function restoreRecapSettings()")
    restore_src = APP_JS[
        restore_start :
        APP_JS.index("window.resyncRecapControlsAfterPrefs", restore_start)
    ]
    assert restore_src.index(
        "refreshRecapQualityLock();"
    ) < restore_src.index(
        'syncPillGroup("#recap-quality-picker .pill-btn"'
    )


def test_au_client_profile_load_and_picker_guard_refresh_lock():
    assert APP_JS.count("refreshRecapQualityLock();") >= 4
    handler = APP_JS[
        APP_JS.index("// 6d. Render quality pills") :
        APP_JS.index("// 6e. Narration engine pills")
    ]
    assert "if (btn.disabled) return;" in handler


def test_au_client_locked_pills_are_dimmed_with_lock_marker():
    assert (
        "#recap-quality-picker .pill-btn.quality-locked "
        "{ opacity: 0.45; }" in STYLE_CSS
    )
    assert (
        "#recap-quality-picker .pill-btn.quality-locked::after "
        '{ content: " 🔒"; }' in STYLE_CSS
    )


def test_au_no_new_i18n_key_for_quality_lock():
    # The visual lock is the existing labels plus a CSS 🔒 marker;
    # Batch AU introduces no new user-visible copy to translate.
    assert "recap_quality_locked" not in APP_JS
    assert "quality_locked" not in (STATIC_DIR / "js" / "i18n.js").read_text(
        encoding="utf-8"
    )


def test_au_static_cache_token_is_r79():
    assert INDEX_HTML.count("20261104_r140") == 4
    assert "20261104_r67" not in INDEX_HTML

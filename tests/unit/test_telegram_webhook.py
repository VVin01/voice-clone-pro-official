"""
test_telegram_webhook.py - Tests for Telegram Webhook Migration (Task 1).

Covers:
(a) Correct secret accepted and update reaches the dispatcher (mock it).
(b) Wrong/missing secret rejected (403) and dispatcher not called.
(c) Duplicate update_id skipped.
(d) BOT_MODE=polling path unchanged.
(e) /health remains independent of webhook state.
(f) Webhook registration idempotency and parameters (drop_pending_updates=False).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.webapp.server import webapp_app
from app.webapp.telegram_webhook import (
    DEFAULT_WEBHOOK_PATH,
    SECRET_HEADER,
    RecentUpdateIds,
    attach_bot,
    detach_bot,
    get_webhook_path,
    recent_update_ids,
    register_webhook,
)


@pytest.fixture(autouse=True)
def clean_webhook_state():
    """Ensure every test starts and ends with a clean webhook environment."""
    detach_bot()
    recent_update_ids.clear()
    orig_mode = settings.bot_mode
    orig_secret = settings.telegram_webhook_secret
    orig_path = settings.bot_webhook_path
    orig_base = settings.public_base_url
    yield
    detach_bot()
    recent_update_ids.clear()
    settings.bot_mode = orig_mode
    settings.telegram_webhook_secret = orig_secret
    settings.bot_webhook_path = orig_path
    settings.public_base_url = orig_base


@pytest.mark.asyncio
async def test_webhook_accepted_with_correct_secret():
    """(a) Correct secret accepted and update reaches the dispatcher."""
    settings.bot_mode = "webhook"
    settings.telegram_webhook_secret = "correct_secret_token_abc123"
    settings.bot_webhook_path = "/telegram/webhook"

    mock_bot = MagicMock()
    mock_bot.id = 123456
    mock_dispatcher = MagicMock()
    mock_dispatcher.feed_webhook_update = AsyncMock(return_value=None)
    mock_dispatcher.feed_update = AsyncMock(return_value=None)

    attach_bot(mock_bot, mock_dispatcher)

    sample_update = {
        "update_id": 10001,
        "message": {
            "message_id": 55,
            "date": 1730000000,
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345, "is_bot": False, "first_name": "TestUser"},
            "text": "Hello bot",
        },
    }

    transport = ASGITransport(app=webapp_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/telegram/webhook",
            json=sample_update,
            headers={SECRET_HEADER: "correct_secret_token_abc123"},
        )

    assert response.status_code == 200
    assert response.json().get("ok") is True

    # Dispatcher was invoked with the update
    assert mock_dispatcher.feed_webhook_update.called or mock_dispatcher.feed_update.called
    called_mock = (
        mock_dispatcher.feed_webhook_update
        if mock_dispatcher.feed_webhook_update.called
        else mock_dispatcher.feed_update
    )
    call_args = called_mock.call_args
    assert call_args is not None
    # Verify the update reached the dispatcher
    passed_update = call_args.kwargs.get("update") or call_args.args[1]
    assert getattr(passed_update, "update_id", None) == 10001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provided_secret,status_code",
    [
        ("wrong_secret_value", 403),
        ("", 403),
        (None, 403),
    ],
)
async def test_webhook_rejected_with_invalid_or_missing_secret(provided_secret, status_code):
    """(b) Wrong/missing secret rejected (403) and dispatcher not called."""
    settings.bot_mode = "webhook"
    settings.telegram_webhook_secret = "expected_super_secret_999"

    mock_bot = MagicMock()
    mock_dispatcher = MagicMock()
    mock_dispatcher.feed_webhook_update = AsyncMock()
    mock_dispatcher.feed_update = AsyncMock()

    attach_bot(mock_bot, mock_dispatcher)

    headers = {}
    if provided_secret is not None:
        headers[SECRET_HEADER] = provided_secret

    transport = ASGITransport(app=webapp_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/telegram/webhook",
            json={"update_id": 20002, "message": {"message_id": 1, "date": 123, "chat": {"id": 1, "type": "private"}}},
            headers=headers,
        )

    assert response.status_code == 403
    assert not mock_dispatcher.feed_webhook_update.called
    assert not mock_dispatcher.feed_update.called


@pytest.mark.asyncio
async def test_webhook_rejected_when_secret_not_configured():
    """Missing env TELEGRAM_WEBHOOK_SECRET must reject everything with 403."""
    settings.bot_mode = "webhook"
    settings.telegram_webhook_secret = ""  # Not configured

    mock_bot = MagicMock()
    mock_dispatcher = MagicMock()
    mock_dispatcher.feed_webhook_update = AsyncMock()
    attach_bot(mock_bot, mock_dispatcher)

    transport = ASGITransport(app=webapp_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/telegram/webhook",
            json={"update_id": 30003},
            headers={SECRET_HEADER: "any_token"},
        )

    assert response.status_code == 403
    assert not mock_dispatcher.feed_webhook_update.called


@pytest.mark.asyncio
async def test_duplicate_update_id_skipped():
    """(c) Duplicate update_id skipped."""
    settings.bot_mode = "webhook"
    settings.telegram_webhook_secret = "secret_for_dedup_test"

    mock_bot = MagicMock()
    mock_bot.id = 123
    mock_dispatcher = MagicMock()
    mock_dispatcher.feed_webhook_update = AsyncMock(return_value=None)
    mock_dispatcher.feed_update = AsyncMock(return_value=None)

    attach_bot(mock_bot, mock_dispatcher)

    update_payload = {
        "update_id": 40004,
        "message": {
            "message_id": 12,
            "date": 1730000000,
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345, "is_bot": False, "first_name": "Test"},
            "text": "First attempt",
        },
    }

    transport = ASGITransport(app=webapp_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # First delivery: accepted
        resp1 = await client.post(
            "/telegram/webhook",
            json=update_payload,
            headers={SECRET_HEADER: "secret_for_dedup_test"},
        )
        assert resp1.status_code == 200
        assert resp1.json().get("duplicate") is not True

        total_calls_after_first = (
            mock_dispatcher.feed_webhook_update.call_count + mock_dispatcher.feed_update.call_count
        )
        assert total_calls_after_first == 1

        # Second delivery (Telegram retry with identical update_id): skipped
        resp2 = await client.post(
            "/telegram/webhook",
            json=update_payload,
            headers={SECRET_HEADER: "secret_for_dedup_test"},
        )
        assert resp2.status_code == 200
        assert resp2.json().get("duplicate") is True

        total_calls_after_second = (
            mock_dispatcher.feed_webhook_update.call_count + mock_dispatcher.feed_update.call_count
        )
        # Dispatcher must NOT have been called a second time
        assert total_calls_after_second == 1


@pytest.mark.asyncio
async def test_bot_mode_polling_path_unchanged():
    """(d) BOT_MODE=polling path unchanged: starts polling, does not register webhook."""
    from main import run_bot_and_worker

    call_order = []

    async def mock_setup_commands(bot):
        call_order.append("setup_bot_commands")

    async def mock_start_polling(*args, **kwargs):
        call_order.append("start_polling")

    mock_register_webhook = AsyncMock()

    with patch("main.init_db", AsyncMock()), \
         patch("main.settings") as mock_settings, \
         patch("main.setup_bot_commands", side_effect=mock_setup_commands), \
         patch("aiogram.Dispatcher.start_polling", side_effect=mock_start_polling), \
         patch("app.webapp.telegram_webhook.register_webhook", mock_register_webhook), \
         patch("main.Bot") as mock_bot_cls:

        mock_settings.bot_token = "fake_test_token_123456"
        mock_settings.log_level = "INFO"
        mock_settings.bot_mode = "polling"
        mock_bot_instance = MagicMock()
        mock_bot_instance.session.close = AsyncMock()
        mock_bot_cls.return_value = mock_bot_instance

        await run_bot_and_worker(mode="bot")

    # Polling mode: commands set up, polling started, webhook never registered
    assert call_order == ["setup_bot_commands", "start_polling"]
    assert not mock_register_webhook.called


@pytest.mark.asyncio
async def test_bot_mode_webhook_skips_polling_and_registers_webhook():
    """Mode switch to webhook (valid config): does NOT start polling; registers webhook."""
    from main import run_bot_and_worker

    call_order = []

    async def mock_setup_commands(bot):
        call_order.append("setup_bot_commands")

    async def mock_start_polling(*args, **kwargs):
        call_order.append("start_polling")

    async def mock_reg_webhook(bot, dp, **kwargs):
        call_order.append("register_webhook")
        return True

    # Real wiring: main.py resolves the effective mode through
    # telegram_webhook.resolve_bot_mode(), which reads the real settings
    # object - so the valid webhook configuration is set there (the
    # fixtures restore every field afterwards). Webhook also requires the
    # web server in-process, hence mode="web_and_bot" with serve mocked.
    settings.bot_token = "fake_test_token_123456"
    settings.bot_mode = "webhook"
    settings.telegram_webhook_secret = "valid_secret_for_wiring_test"
    settings.public_base_url = "https://voice-clone-studio.onrender.com"

    with patch("main.init_db", AsyncMock()), \
         patch("main.setup_bot_commands", side_effect=mock_setup_commands), \
         patch("aiogram.Dispatcher.start_polling", side_effect=mock_start_polling), \
         patch("app.webapp.telegram_webhook.register_webhook", side_effect=mock_reg_webhook), \
         patch("uvicorn.Server.serve", new=AsyncMock(return_value=None)), \
         patch("main.Bot") as mock_bot_cls:

        mock_bot_instance = MagicMock()
        mock_bot_instance.session.close = AsyncMock()
        mock_bot_cls.return_value = mock_bot_instance

        await run_bot_and_worker(mode="web_and_bot")

    # In webhook mode: setup_commands, then register_webhook; start_polling NEVER called
    assert call_order == ["setup_bot_commands", "register_webhook"]
    assert "start_polling" not in call_order


@pytest.mark.asyncio
async def test_webhook_mode_with_missing_secret_falls_back_to_polling():
    """Wiring: BOT_MODE=webhook without TELEGRAM_WEBHOOK_SECRET falls back to
    polling via resolve_bot_mode() instead of leaving the bot silent."""
    from main import run_bot_and_worker

    call_order = []

    async def mock_setup_commands(bot):
        call_order.append("setup_bot_commands")

    async def mock_start_polling(*args, **kwargs):
        call_order.append("start_polling")

    mock_register_webhook = AsyncMock()

    settings.bot_token = "fake_test_token_123456"
    settings.bot_mode = "webhook"
    settings.telegram_webhook_secret = ""  # missing -> webhook unusable
    settings.public_base_url = "https://voice-clone-studio.onrender.com"

    with patch("main.init_db", AsyncMock()), \
         patch("main.setup_bot_commands", side_effect=mock_setup_commands), \
         patch("aiogram.Dispatcher.start_polling", side_effect=mock_start_polling), \
         patch("app.webapp.telegram_webhook.register_webhook", mock_register_webhook), \
         patch("uvicorn.Server.serve", new=AsyncMock(return_value=None)), \
         patch("main.Bot") as mock_bot_cls:

        mock_bot_instance = MagicMock()
        mock_bot_instance.session.close = AsyncMock()
        # No webhook registered: the polling rollback guard is a read-only no-op.
        mock_bot_instance.get_webhook_info = AsyncMock(return_value=MagicMock(url=""))
        mock_bot_instance.delete_webhook = AsyncMock()
        mock_bot_cls.return_value = mock_bot_instance

        await run_bot_and_worker(mode="web_and_bot")

    # Fell back to polling; webhook never registered, nothing deleted
    assert call_order == ["setup_bot_commands", "start_polling"]
    assert not mock_register_webhook.called
    assert not mock_bot_instance.delete_webhook.called


@pytest.mark.asyncio
async def test_webhook_mode_without_web_server_falls_back_to_polling():
    """Wiring: BOT_MODE=webhook with RUN_MODE=bot (nothing serves the webhook
    path in this process) falls back to polling via resolve_bot_mode()."""
    from main import run_bot_and_worker

    call_order = []

    async def mock_setup_commands(bot):
        call_order.append("setup_bot_commands")

    async def mock_start_polling(*args, **kwargs):
        call_order.append("start_polling")

    mock_register_webhook = AsyncMock()

    settings.bot_token = "fake_test_token_123456"
    settings.bot_mode = "webhook"
    settings.telegram_webhook_secret = "valid_secret_for_wiring_test"
    settings.public_base_url = "https://voice-clone-studio.onrender.com"

    with patch("main.init_db", AsyncMock()), \
         patch("main.setup_bot_commands", side_effect=mock_setup_commands), \
         patch("aiogram.Dispatcher.start_polling", side_effect=mock_start_polling), \
         patch("app.webapp.telegram_webhook.register_webhook", mock_register_webhook), \
         patch("main.Bot") as mock_bot_cls:

        mock_bot_instance = MagicMock()
        mock_bot_instance.session.close = AsyncMock()
        mock_bot_instance.get_webhook_info = AsyncMock(return_value=MagicMock(url=""))
        mock_bot_instance.delete_webhook = AsyncMock()
        mock_bot_cls.return_value = mock_bot_instance

        await run_bot_and_worker(mode="bot")

    assert call_order == ["setup_bot_commands", "start_polling"]
    assert not mock_register_webhook.called


@pytest.mark.asyncio
async def test_polling_mode_clears_stale_webhook_before_polling():
    """Wiring: in polling mode a leftover webhook registration is removed
    (delete_webhook with drop_pending_updates=False) BEFORE polling starts,
    via clear_webhook_for_polling()."""
    from main import run_bot_and_worker

    call_order = []

    async def mock_setup_commands(bot):
        call_order.append("setup_bot_commands")

    async def mock_start_polling(*args, **kwargs):
        call_order.append("start_polling")

    async def mock_delete_webhook(*args, **kwargs):
        call_order.append("delete_webhook")
        return True

    settings.bot_token = "fake_test_token_123456"
    settings.bot_mode = "polling"

    with patch("main.init_db", AsyncMock()), \
         patch("main.setup_bot_commands", side_effect=mock_setup_commands), \
         patch("aiogram.Dispatcher.start_polling", side_effect=mock_start_polling), \
         patch("main.Bot") as mock_bot_cls:

        mock_bot_instance = MagicMock()
        mock_bot_instance.session.close = AsyncMock()
        mock_bot_instance.get_webhook_info = AsyncMock(
            return_value=MagicMock(
                url="https://voice-clone-studio.onrender.com/telegram/webhook"
            )
        )
        mock_bot_instance.delete_webhook = AsyncMock(side_effect=mock_delete_webhook)
        mock_bot_cls.return_value = mock_bot_instance

        await run_bot_and_worker(mode="bot")

    # The stale webhook is deleted before polling starts, keeping pending updates
    assert call_order == ["setup_bot_commands", "delete_webhook", "start_polling"]
    delete_kwargs = mock_bot_instance.delete_webhook.call_args.kwargs
    assert delete_kwargs["drop_pending_updates"] is False


@pytest.mark.asyncio
async def test_register_webhook_idempotent_parameters():
    """Registration sets webhook with drop_pending_updates=False and secret_token."""
    settings.public_base_url = "https://voice-clone-studio.onrender.com"
    settings.bot_webhook_path = "/telegram/webhook"
    settings.telegram_webhook_secret = "secret_token_xyz_789"

    mock_bot = MagicMock()
    mock_bot.set_webhook = AsyncMock(return_value=True)
    mock_bot.get_webhook_info = AsyncMock(return_value=MagicMock(pending_update_count=0))

    mock_dp = MagicMock()
    mock_dp.resolve_used_update_types = MagicMock(return_value=["message", "callback_query"])

    success = await register_webhook(mock_bot, mock_dp)
    assert success is True
    assert mock_bot.set_webhook.called

    call_kwargs = mock_bot.set_webhook.call_args.kwargs
    assert call_kwargs["url"] == "https://voice-clone-studio.onrender.com/telegram/webhook"
    assert call_kwargs["secret_token"] == "secret_token_xyz_789"
    assert call_kwargs["drop_pending_updates"] is False
    assert call_kwargs["allowed_updates"] == ["message", "callback_query"]


@pytest.mark.asyncio
async def test_health_check_independent_of_webhook_state():
    """Requirement 5: /health must stay independent of webhook state."""
    transport = ASGITransport(app=webapp_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Before attaching any bot
        detach_bot()
        r1 = await client.get("/health")
        assert r1.status_code == 200
        assert r1.json().get("status") == "ok"

        # In webhook mode with attached bot
        mock_bot = MagicMock()
        mock_dp = MagicMock()
        attach_bot(mock_bot, mock_dp)
        r2 = await client.get("/health")
        assert r2.status_code == 200
        assert r2.json().get("status") == "ok"


@pytest.mark.asyncio
async def test_custom_webhook_path():
    """Path can be configured via BOT_WEBHOOK_PATH."""
    settings.bot_mode = "webhook"
    settings.telegram_webhook_secret = "secret_custom_path_test"
    settings.bot_webhook_path = "/custom/telegram/hook"

    mock_bot = MagicMock()
    mock_bot.id = 888
    mock_dispatcher = MagicMock()
    mock_dispatcher.feed_webhook_update = AsyncMock(return_value=None)
    attach_bot(mock_bot, mock_dispatcher)

    transport = ASGITransport(app=webapp_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/custom/telegram/hook",
            json={"update_id": 50005},
            headers={SECRET_HEADER: "secret_custom_path_test"},
        )

    assert response.status_code == 200
    assert response.json().get("ok") is True
    assert mock_dispatcher.feed_webhook_update.called


def test_recent_update_ids_bounded_capacity():
    """RecentUpdateIds evicts oldest entries once capacity is reached."""
    dedup = RecentUpdateIds(maxlen=5)
    for i in range(1, 6):
        assert dedup.seen_or_add(i) is False
    assert len(dedup) == 5

    # 1 is present
    assert dedup.seen_or_add(1) is True

    # Adding 6 evicts 1 (the oldest)
    assert dedup.seen_or_add(6) is False
    assert len(dedup) == 5
    # 1 is gone now
    assert dedup.seen_or_add(1) is False

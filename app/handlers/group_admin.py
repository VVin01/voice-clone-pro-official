"""Group AI admin handler — see app/services/group_ai_admin.py.

Registered LAST among the routers so command/FSM handlers always win.
The handler itself is deliberately thin: all decisions (pre-filter,
kill switch, sensitive-topic hard rule, rate limits, the AI call) live
in the service module where they are unit-tested. It never raises —
any failure degrades to silence, never to a broken update pipeline.

Batch H additions wired here: per-message BotSetting limits applied
to the shared limiter, the in-memory conversation-context buffer,
the FAQ cache check (admin-approved answers only, no AI call), the
escalation draft on the admin DM with [Post] / [Edit] / [Dismiss]
buttons, and the callback + FSM machinery behind them:

* First tap wins — approve/dismiss close the record, edit claims it
  (status EDITING) and prompts the claiming admin in DM for the
  corrected text; saving or cancelling the edit releases the claim
  and refreshes every admin's DM with a fresh keyboard.
* Every DM copy's coordinates live on the escalation record, so any
  state change rewrites ALL admin copies at once: a short status
  line is appended and the keyboard is cleared (terminal states) —
  a failed edit on one copy never fails the action itself.
"""

from __future__ import annotations

import html
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.services import group_ai_admin as gai
from app.services import private_ai as pai
from app.services.i18n import t as _bot_t
from app.services.payments import get_recent_payment_intent

logger = logging.getLogger(__name__)

group_admin_router = Router(name="group_admin")


class GroupAiEditStates(StatesGroup):
    """Admin is composing a corrected draft for a claimed escalation."""

    waiting_text = State()


@asynccontextmanager
async def _session_or_new(session: AsyncSession | None):
    """Use the middleware-supplied session, else open a short-lived one."""
    if session is not None:
        yield session
    else:
        async with AsyncSessionLocal() as s:
            yield s


async def _kill_switch_on(session: AsyncSession | None) -> bool:
    async with _session_or_new(session) as s:
        return await gai.is_enabled(s)


async def _refresh_limits(session: AsyncSession | None) -> None:
    """Apply the owner-tuned BotSetting limits to the shared limiter."""
    async with _session_or_new(session) as s:
        gai.rate_limiter.update_limits(await gai.get_limits(s))
        # Batch AJ (M12): one-shot restore of the persisted limiter
        # snapshot (budgets / cooldowns / answered-set) after a restart.
        await gai.ensure_limiter_restored(s)


async def _flush_limiter(session: AsyncSession | None) -> None:
    """Batch AJ (M12): debounced write-through of limiter state."""
    try:
        async with _session_or_new(session) as s:
            await gai.flush_limiter_state_if_dirty(s)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cross-admin DM sync (batch H, requirement B)
# ---------------------------------------------------------------------------


async def _sync_admin_copies(bot: Bot, record: dict, *, status_line: str | None) -> None:
    """Rewrite every stored admin DM copy after a state change.

    Appends the status line to the stored DM body and clears the
    keyboard. A copy that cannot be edited (too old, deleted, …)
    falls back to a markup-only clear, and a copy that fails even
    that is logged and skipped — syncing must never fail the action
    that triggered it. The tap-time status check in the callback
    handler remains the backstop for any copy left stale.
    """
    dm_text = record.get("dm_text")
    for chat_id, message_id in record.get("dm_copies") or []:
        try:
            if dm_text:
                new_text = dm_text + (f"\n\n{status_line}" if status_line else "")
                await bot.edit_message_text(
                    new_text,
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode="HTML",
                    reply_markup=None,
                )
            else:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id, reply_markup=None
                )
        except Exception as exc:
            logger.debug(
                "group_ai_admin: DM copy sync failed for %s/%s: %s", chat_id, message_id, exc
            )
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id, reply_markup=None
                )
            except Exception:
                pass


async def _resend_escalation_dms(bot: Bot, session: AsyncSession, record: dict) -> None:
    """Send FRESH escalation DMs (current draft + live keyboard) to all
    admins and store the new copies on the record.

    Used after an edit is saved or cancelled: the old copies have
    just been status-lined and de-keyboarded by _sync_admin_copies,
    so the actionable copy going forward is this new one.
    """
    dm_text = gai.build_escalation_dm(
        author_name=record.get("author_label") or "",
        author_username=record.get("author_username"),
        author_id=record["author_id"],
        text=record.get("question_text") or "",
        chat_id=record["chat_id"],
        message_id=record["message_id"],
        reason=record.get("reason") or "",
        draft_text=record.get("draft_text"),
    )
    keyboard = (
        gai.build_escalation_keyboard(record["id"]) if record.get("draft_text") else None
    )
    copies: list[tuple[int, int]] = []
    for admin_id in settings.admin_ids:
        try:
            sent = await bot.send_message(
                admin_id, dm_text, parse_mode="HTML", reply_markup=keyboard
            )
            mid = getattr(sent, "message_id", None)
            if isinstance(mid, int):
                copies.append((int(admin_id), mid))
        except Exception as exc:
            logger.warning("group_ai_admin: escalation re-DM to %s failed: %s", admin_id, exc)
    if copies:
        await gai.set_escalation_dm(session, record["id"], dm_text=dm_text, copies=copies)


async def _release_edit_and_refresh(
    *,
    bot: Bot,
    session: AsyncSession,
    escalation_id: int,
    admin_id: int,
    admin_name: str,
    new_draft: str | None,
) -> dict | None:
    """Release an edit claim and refresh every admin's DM.

    Returns the released record snapshot, or None when the claim was
    not this admin's to release. Old copies get the edit-saved /
    edit-cancelled status line; fresh copies carry the live draft.
    """
    released, snapshot = await gai.release_escalation_edit(
        session, escalation_id, admin_id=admin_id, new_draft=new_draft
    )
    if not released or snapshot is None:
        return None
    line = gai.STATUS_LINE_EDIT_SAVED if new_draft is not None else gai.STATUS_LINE_EDIT_CANCELLED
    await _sync_admin_copies(bot, snapshot, status_line=line.format(name=admin_name))
    await _resend_escalation_dms(bot, session, snapshot)
    return snapshot


# ---------------------------------------------------------------------------
# Escalation delivery
# ---------------------------------------------------------------------------


async def _escalate(
    message: Message,
    bot: Bot | None,
    *,
    reason: str,
    deferral_text: str,
    session: AsyncSession | None = None,
    limiter=None,
    dm_link: str | None = ...,
) -> None:
    """Post the polite deferral line + DM every admin (cooldown-gated).

    Batch H: when the AI-call budget allows, one extra Gemini call
    drafts a suggested answer; the escalation is persisted so the
    DM's [Post] / [Edit] / [Dismiss] buttons can act on it later
    (first tap wins), and every DM copy's coordinates are stored on
    the record for cross-admin keyboard sync. Without budget — or if
    drafting/persisting fails — the DM goes out exactly as before:
    no draft, no buttons.

    Batch BX: ``limiter`` and ``dm_link`` parameterise the two
    group-specific pieces for the private-chat path — the deferral
    pacing/budget can run on the private limiter, and the admin DM
    can drop the (invalid-for-private) group message link. Both
    default to the historical group behaviour, byte-identical.
    """
    limiter = limiter if limiter is not None else gai.rate_limiter
    user = message.from_user
    if user is None or not limiter.can_defer(user.id):
        return
    limiter.record_deferral(user.id)
    try:
        await message.reply(deferral_text)
    except Exception as exc:
        logger.debug("group_ai_admin: deferral post failed: %s", exc)

    draft_text = None
    if limiter.ai_budget_available():
        limiter.record_ai_call()
        draft_text = await gai.draft_escalation_answer(
            text=message.text or "",
            author_name=user.first_name or "",
            reason=reason,
        )

    escalation_id = None
    if draft_text:
        async with _session_or_new(session) as s:
            escalation_id = await gai.create_escalation(
                s,
                chat_id=message.chat.id,
                message_id=message.message_id,
                author_id=user.id,
                author_label=user.first_name or "",
                author_username=user.username,
                question_text=message.text or "",
                reason=reason,
                draft_text=draft_text,
            )

    dm_text = gai.build_escalation_dm(
        author_name=user.first_name or "",
        author_username=user.username,
        author_id=user.id,
        text=message.text or "",
        chat_id=message.chat.id,
        message_id=message.message_id,
        reason=reason,
        draft_text=draft_text,
        dm_link=dm_link,
    )
    keyboard = gai.build_escalation_keyboard(escalation_id) if escalation_id else None
    sender = bot or message.bot
    copies: list[tuple[int, int]] = []
    for admin_id in settings.admin_ids:
        try:
            sent = await sender.send_message(
                admin_id, dm_text, parse_mode="HTML", reply_markup=keyboard
            )
            mid = getattr(sent, "message_id", None)
            if escalation_id and isinstance(mid, int):
                copies.append((int(admin_id), mid))
        except Exception as exc:
            logger.warning("group_ai_admin: escalation DM to %s failed: %s", admin_id, exc)
    if escalation_id and copies:
        async with _session_or_new(session) as s:
            await gai.set_escalation_dm(s, escalation_id, dm_text=dm_text, copies=copies)


def _log_decision(message: Message, user_id: int | None, decision: str, reason: str = "") -> None:
    """Batch AQ: one INFO line per terminal Group-AI decision.

    Before this, decisions were logged NOWHERE — a member question
    could vanish into silence (AI timeout, NO_REPLY, budget) with no
    trace at all, and two unanswered questions on 2026-11-05 were
    undiagnosable for exactly that reason. Ids + the reason class
    only — NEVER the message text or the AI's raw output (group
    messages are not stored; the logs follow the same rule).
    """
    logger.info(
        "group_ai_admin: decision=%s reason=%s chat_id=%s message_id=%s user_id=%s",
        decision,
        reason or "-",
        message.chat.id,
        message.message_id,
        user_id,
    )


@group_admin_router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text,
)
async def handle_group_message(
    message: Message,
    state: FSMContext = None,
    session: AsyncSession | None = None,
    bot: Bot = None,
) -> None:
    try:
        # A user mid-flow in an FSM conversation owns their messages.
        if state is not None:
            try:
                if await state.get_state():
                    return
            except Exception:
                pass

        user = message.from_user
        chat_type = getattr(message.chat.type, "value", str(message.chat.type))
        reason = gai.prefilter_reason(
            chat_id=message.chat.id,
            chat_type=chat_type,
            text=message.text,
            from_user_id=user.id if user else None,
            from_user_is_bot=bool(user.is_bot) if user else True,
            sender_chat_id=message.sender_chat.id if message.sender_chat else None,
        )
        if reason is not None:
            return

        if not await _kill_switch_on(session):
            return

        # Owner-tuned anti-spam values (BotSettings, kill-switch style).
        await _refresh_limits(session)

        # Hard rule first: payment/refund/account topics never reach
        # the AI — defer in the group and hand the question to a human.
        if gai.detect_sensitive_topic(message.text):
            _log_decision(message, user.id if user else None, "escalate", "sensitive")
            await _escalate(
                message,
                bot,
                reason="sensitive",
                deferral_text=gai.DEFERRAL_TEXT_SENSITIVE,
                session=session,
            )
            return

        if not gai.rate_limiter.can_reply(user.id, message.message_id):
            return

        # Conversation context for the AI (in-memory only): snapshot
        # the recent window BEFORE adding this message, so the prompt
        # shows the conversation that led here; the current message
        # then joins the buffer for the next candidate.
        context_lines = gai.context_buffer.snapshot(message.chat.id)
        gai.context_buffer.push(message.chat.id, user.first_name or "", message.text or "")

        reply_to = message.reply_to_message
        replied_to_text = (getattr(reply_to, "text", None) or None) if reply_to else None

        # Social fast paths (owner revision 2026-11-02): a pure
        # greeting or a short "want to try" starter fragment gets a
        # warm deterministic welcome/pointer — no AI call needed, and
        # the tone stays exactly as approved. Join service notices
        # never arrive here (they carry no user text).
        #
        # Batch AQ: a third deterministic path — while maintenance
        # mode is on, a genuine service-availability question gets the
        # maintenance answer immediately. It sits AFTER the sensitive
        # check and the can_reply cooldown above, consumes no AI
        # budget, and never fires when maintenance is off.
        fast_path_text = None
        fast_path_kind = None
        if gai.is_greeting_only(message.text):
            fast_path_text = gai.WELCOME_TEXT
            fast_path_kind = "greeting"
        elif gai.is_starter_fragment(message.text):
            fast_path_text = gai.STARTER_POINTER_TEXT
            fast_path_kind = "starter"
        elif gai.maintenance_mode_on() and gai.is_service_availability_question(
            message.text,
            context_lines=context_lines,
            replied_to_text=replied_to_text,
        ):
            fast_path_text = gai.build_maintenance_reply()
            fast_path_kind = "maintenance"
        if fast_path_text is not None:
            await message.reply(fast_path_text)
            gai.rate_limiter.record_reply(user.id, message.message_id)
            _log_decision(message, user.id, f"fast_path:{fast_path_kind}")
            return

        is_reply_to_bot = bool(
            reply_to
            and reply_to.from_user
            and (reply_to.from_user.username or "").lower() == gai.BOT_USERNAME
        )
        bot_mentioned = f"@{gai.BOT_USERNAME}" in (message.text or "").lower()

        # FAQ cache (admin-approved answers only): a near-identical
        # repeat question is answered from the cache — no AI call, no
        # budget consumed. The per-user cooldown was already enforced
        # by the can_reply check above, so cached answers cannot be
        # used to spam either.
        async with _session_or_new(session) as s:
            faq_hit = await gai.find_faq_answer(s, message.text or "")
            if faq_hit is not None:
                faq_id, faq_answer = faq_hit
                answer_text = gai.strip_emoji(faq_answer)
                if answer_text:
                    await message.reply(answer_text)
                    gai.rate_limiter.record_reply(user.id, message.message_id)
                    await gai.record_faq_hit(s, faq_id)
                    _log_decision(message, user.id, "faq_hit")
                    return

        # AI answers are budget-gated: with the hourly/daily AI-call
        # budget exhausted the bot stays silent (deferrals and the
        # free paths above are unaffected — they need no AI call).
        if not gai.rate_limiter.ai_budget_available():
            _log_decision(message, user.id, "no_reply", "ai_budget_exhausted")
            return
        gai.rate_limiter.record_ai_call()

        decision, reply_text = await gai.ask_group_admin_ai(
            text=message.text or "",
            author_name=user.first_name or "",
            bot_mentioned=bot_mentioned,
            is_reply_to_bot=is_reply_to_bot,
            context_lines=context_lines,
            replied_to_text=replied_to_text,
        )

        if decision == gai.DECISION_AI_FAILED:
            # Batch AQ: the call failed (timeout / pool exhausted) —
            # silence for the member, but logged AS a failure, not
            # mistaken for the AI choosing not to answer.
            _log_decision(message, user.id, "ai_failed", "ai_call")
            return
        if decision == gai.DECISION_ESCALATE:
            _log_decision(message, user.id, "escalate", "ai_escalate")
            await _escalate(
                message,
                bot,
                reason="ai_escalate",
                deferral_text=gai.DEFERRAL_TEXT_UNSURE,
                session=session,
            )
            return
        if decision != gai.DECISION_REPLY or not reply_text:
            _log_decision(message, user.id, "no_reply", "ai_decision")
            return

        # Owner rule (2026-11-02): zero emoji in group replies. The
        # service sanitises parsed AI output already; strip again here
        # so the send path itself is guaranteed clean. A reply that is
        # only emoji becomes empty and is treated as silence.
        reply_text = gai.strip_emoji(reply_text)
        if not reply_text:
            _log_decision(message, user.id, "no_reply", "empty_reply")
            return

        # Re-check the cap at send time (another reply may have landed
        # while the AI was thinking), then answer as a threaded reply.
        if not gai.rate_limiter.can_reply(user.id, message.message_id):
            _log_decision(message, user.id, "no_reply", "cooldown_recheck")
            return
        await message.reply(reply_text)
        gai.rate_limiter.record_reply(user.id, message.message_id)
        _log_decision(message, user.id, "reply", "ai")
    except Exception as exc:
        logger.warning("group_ai_admin: handler failed (silence): %s", exc)
    finally:
        # Batch AJ (M12): persist any limiter changes from this
        # message (debounced + dirty-checked; never raises).
        await _flush_limiter(session)


# ---------------------------------------------------------------------------
# Bot private-chat AI replies (Batch BX) — same router, same "last
# word" position: commands and FSM flows have already had their
# chance by the time a plain-text private message lands here.
# ---------------------------------------------------------------------------


def _log_private_decision(message: Message, user_id: int | None, decision: str, reason: str = "") -> None:
    """One INFO line per terminal private-AI decision — ids + reason
    class only, NEVER the message text (the no-storage rule)."""
    logger.info(
        "private_ai: decision=%s reason=%s chat_id=%s message_id=%s user_id=%s",
        decision,
        reason or "-",
        message.chat.id,
        message.message_id,
        user_id,
    )


async def _record_private_exchange(
    session: AsyncSession | None,
    message: Message,
    *,
    reply_text: str,
    kind: str,
) -> None:
    """Batch CD: persist the exchange the private-AI chain just
    handled (user text + the reply actually sent) for the admin
    user-detail chat log. Failure-isolated twice over — the service
    swallows its own errors and this wrapper swallows the rest — so
    recording can never break or delay the reply. PRIVATE chain
    only: the group handler never calls this (group messages are
    never stored — the standing ban)."""
    try:
        user = message.from_user
        if user is None:
            return
        async with _session_or_new(session) as s:
            await pai.record_private_exchange(
                s,
                user_id=user.id,
                user_text=message.text or "",
                reply_text=reply_text,
                kind=kind,
            )
    except Exception as exc:
        logger.warning("private_ai: transcript record failed (dropped): %s", exc)


async def _user_language(session: AsyncSession | None, user_id: int) -> str:
    """The user's stored language for the localised deferral lines."""
    try:
        async with _session_or_new(session) as s:
            from app.services.ledger import get_or_create_user

            user, _ = await get_or_create_user(s, user_id)
            return user.language_code or "my"
    except Exception:
        return "my"


@group_admin_router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def handle_private_message(
    message: Message,
    state: FSMContext = None,
    session: AsyncSession | None = None,
    bot: Bot = None,
) -> None:
    """Plain-text private chat: greetings + service questions get an
    AI answer in Burmese; payment/refund/account topics are deferred
    to the admins and NEVER answered by the AI; any failure degrades
    to silence. Mirrors handle_group_message step for step, on the
    private limiter/kill-switch (app/services/private_ai.py)."""
    try:
        # A user mid-flow in an FSM conversation owns their messages
        # (clone text input, payment slips, admin edit prompts…).
        if state is not None:
            try:
                if await state.get_state():
                    return
            except Exception:
                pass

        user = message.from_user

        # Batch BY: payment-intent hint, folded in from the old
        # payment.py catch-all (deleted there). That handler sat on
        # payment_router — included BEFORE this router — with a
        # bare (no-state, private, text) filter, so it consumed EVERY
        # plain private text even when it returned silently, and this
        # handler never fired in production (BX eyes-on FAIL 1: zero
        # private_ai log lines while the toggle was ON). This handler
        # is now the single terminal owner of plain private text, so
        # the hint check lives here, right after the FSM-state return
        # and BEFORE the AI prefilter: a user mid-purchase (recent
        # <24h persisted intent, no live FSM state) gets exactly the
        # polite screenshot request the old handler sent, in their
        # language, and the AI never also answers. The check runs
        # before the private-AI kill switch on purpose — the hint
        # predates the AI feature and must not depend on its toggle.
        if user is not None:
            intent = None
            hint_lang = "my"
            async with _session_or_new(session) as s:
                intent = await get_recent_payment_intent(s, user.id)
                if intent is not None:
                    from app.services.ledger import get_or_create_user

                    hint_user, _ = await get_or_create_user(s, user.id)
                    hint_lang = hint_user.language_code or "my"
            if intent is not None:
                await message.answer(
                    _bot_t("pay_receipt_wrong_content", hint_lang),
                    parse_mode="HTML",
                )
                _log_private_decision(message, user.id, "hint", "payment_intent")
                return

        chat_type = getattr(message.chat.type, "value", str(message.chat.type))
        reason = pai.prefilter_private_reason(
            chat_type=chat_type,
            text=message.text,
            from_user_id=user.id if user else None,
            from_user_is_bot=bool(user.is_bot) if user else True,
            sender_chat_id=message.sender_chat.id if message.sender_chat else None,
        )
        if reason is not None:
            return

        async with _session_or_new(session) as s:
            if not await pai.is_private_ai_enabled(s):
                return
            limits = await pai.get_private_limits(s)
        pai.private_rate_limiter.update_limits(
            gai.GroupAiLimits(
                user_cooldown_seconds=limits.user_cooldown_seconds,
                hourly_budget=limits.hourly_budget,
                daily_budget=limits.daily_budget,
            )
        )
        lang = await _user_language(session, user.id)

        # Hard rule first: payment/refund/account topics never reach
        # the AI — defer to the user and hand the question to a human.
        if gai.detect_sensitive_topic(message.text):
            _log_private_decision(message, user.id, "escalate", "sensitive")
            deferral_text = _bot_t("private_ai_deferral_sensitive", lang)
            # _escalate posts the deferral only when the limiter's
            # deferral cooldown allows it — record the exchange only
            # when the deferral will actually be posted.
            will_defer = pai.private_rate_limiter.can_defer(user.id)
            await _escalate(
                message,
                bot,
                reason="sensitive",
                deferral_text=deferral_text,
                session=session,
                limiter=pai.private_rate_limiter,
                dm_link=None,
            )
            if will_defer:
                await _record_private_exchange(
                    session, message,
                    reply_text=deferral_text, kind=pai.KIND_ESCALATION,
                )
            return

        if not pai.private_rate_limiter.can_reply(user.id, message.message_id):
            return

        # Deterministic fast paths (no AI budget): greeting, starter
        # fragment, maintenance availability — same placement as the
        # group handler (after the sensitive check + cooldown).
        fast_path_text = None
        fast_path_kind = None
        if gai.is_greeting_only(message.text):
            fast_path_text = pai.PRIVATE_WELCOME_TEXT
            fast_path_kind = "greeting"
        elif gai.is_starter_fragment(message.text):
            fast_path_text = gai.STARTER_POINTER_TEXT
            fast_path_kind = "starter"
        elif gai.maintenance_mode_on() and gai.is_service_availability_question(message.text):
            fast_path_text = gai.build_maintenance_reply()
            fast_path_kind = "maintenance"
        if fast_path_text is not None:
            await message.reply(fast_path_text)
            pai.private_rate_limiter.record_reply(user.id, message.message_id)
            _log_private_decision(message, user.id, f"fast_path:{fast_path_kind}")
            await _record_private_exchange(
                session, message,
                reply_text=fast_path_text, kind=pai.KIND_FAST_PATH,
            )
            return

        # AI answers are budget-gated; fast paths and deferrals above
        # are unaffected — they need no AI call.
        if not pai.private_rate_limiter.ai_budget_available():
            _log_private_decision(message, user.id, "no_reply", "ai_budget_exhausted")
            return
        pai.private_rate_limiter.record_ai_call()

        decision, reply_text = await pai.ask_private_ai(
            text=message.text or "",
            author_name=user.first_name or "",
        )

        if decision == gai.DECISION_AI_FAILED:
            # The call failed — silence for the user, logged AS a
            # failure (Batch AQ standard), never a canned fallback.
            _log_private_decision(message, user.id, "ai_failed", "ai_call")
            return
        if decision == gai.DECISION_ESCALATE:
            _log_private_decision(message, user.id, "escalate", "ai_escalate")
            deferral_text = _bot_t("private_ai_deferral_unsure", lang)
            will_defer = pai.private_rate_limiter.can_defer(user.id)
            await _escalate(
                message,
                bot,
                reason="ai_escalate",
                deferral_text=deferral_text,
                session=session,
                limiter=pai.private_rate_limiter,
                dm_link=None,
            )
            if will_defer:
                await _record_private_exchange(
                    session, message,
                    reply_text=deferral_text, kind=pai.KIND_ESCALATION,
                )
            return
        if decision != gai.DECISION_REPLY or not reply_text:
            _log_private_decision(message, user.id, "no_reply", "ai_decision")
            return

        reply_text = gai.strip_emoji(reply_text)
        if not reply_text:
            _log_private_decision(message, user.id, "no_reply", "empty_reply")
            return

        # Re-check the cap at send time, then answer.
        if not pai.private_rate_limiter.can_reply(user.id, message.message_id):
            _log_private_decision(message, user.id, "no_reply", "cooldown_recheck")
            return
        await message.reply(reply_text)
        pai.private_rate_limiter.record_reply(user.id, message.message_id)
        _log_private_decision(message, user.id, "reply", "ai")
        await _record_private_exchange(
            session, message,
            reply_text=reply_text, kind=pai.KIND_AI_REPLY,
        )
    except Exception as exc:
        logger.warning("private_ai: handler failed (silence): %s", exc)


# ---------------------------------------------------------------------------
# Escalation buttons: approve / dismiss / edit / cancel-edit
# ---------------------------------------------------------------------------


@group_admin_router.callback_query(F.data.startswith(f"{gai.ESCALATION_CALLBACK_PREFIX}:"))
async def handle_escalation_callback(
    callback: CallbackQuery,
    state: FSMContext = None,
    session: AsyncSession | None = None,
    bot: Bot = None,
) -> None:
    """Button taps on escalation DMs.

    Bot admins only. First tap wins (the service claims the pending
    record atomically); a later tap is told the item is handled, and
    every state change also rewrites all stored DM copies (status
    line + keyboard cleared) so stale buttons disappear everywhere.
    Approving posts the (possibly admin-edited) draft as a reply to
    the original group message and saves the pair into the FAQ cache.
    """
    try:
        parsed = gai.parse_escalation_callback(callback.data)
        if parsed is None:
            await callback.answer(gai.ADMIN_ALERT_BAD_BUTTON, show_alert=True)
            return
        action, escalation_id = parsed

        admin = callback.from_user
        if admin is None or not settings.is_admin(admin.id):
            await callback.answer(gai.ADMIN_ALERT_NOT_ADMIN, show_alert=True)
            return
        sender = bot or callback.bot
        admin_name = admin.first_name or "Admin"

        if action == "cancel":
            await _handle_edit_cancel(
                callback,
                sender,
                state=state,
                session=session,
                escalation_id=escalation_id,
                admin_id=admin.id,
                admin_name=admin_name,
            )
            return

        async with _session_or_new(session) as s:
            if action == "edit":
                await _handle_edit_claim(
                    callback,
                    sender,
                    state=state,
                    session=s,
                    escalation_id=escalation_id,
                    admin_id=admin.id,
                    admin_name=admin_name,
                )
                return

            outcome, record = await gai.claim_escalation(
                s, escalation_id, admin_id=admin.id, approve=(action == "approve")
            )
            if outcome == "not_found":
                await callback.answer(gai.ADMIN_ALERT_NOT_FOUND, show_alert=True)
                return
            if outcome == "expired":
                await _sync_admin_copies(sender, record, status_line=gai.STATUS_LINE_EXPIRED)
                await callback.answer(gai.ADMIN_ALERT_EXPIRED, show_alert=True)
                return
            if outcome != "claimed" or record is None:
                # Backstop for a copy whose keyboard could not be
                # cleared when the state changed: clear THIS copy now.
                await _clear_keyboard(callback)
                await callback.answer(gai.ADMIN_ALERT_ALREADY_HANDLED, show_alert=True)
                return

            if action == "dismiss":
                await _sync_admin_copies(
                    sender,
                    record,
                    status_line=gai.STATUS_LINE_DISMISSED.format(name=admin_name),
                )
                await callback.answer(gai.ADMIN_CONFIRM_DISMISSED)
                return

            draft_text = gai.strip_emoji(record.get("draft_text") or "")
            if not draft_text:
                await callback.answer(gai.ADMIN_ALERT_NO_DRAFT, show_alert=True)
                return

            try:
                await sender.send_message(
                    record["chat_id"],
                    draft_text,
                    reply_to_message_id=record["message_id"],
                )
            except Exception as exc:
                # The record stays APPROVED (this admin claimed it);
                # the draft is still in the DM for a manual repost.
                logger.warning("group_ai_admin: approved draft post failed: %s", exc)
                await callback.answer(gai.ADMIN_ALERT_POST_FAILED, show_alert=True)
                return

            await _sync_admin_copies(
                sender,
                record,
                status_line=gai.STATUS_LINE_POSTED.format(name=admin_name),
            )

            # Only a successfully posted, admin-approved answer may
            # enter the FAQ cache.
            await gai.save_faq_pair(
                s,
                question_text=record.get("question_text") or "",
                answer_text=draft_text,
            )

        await callback.answer(gai.ADMIN_CONFIRM_POSTED)
    except Exception as exc:
        logger.warning("group_ai_admin: escalation callback failed: %s", exc)
        try:
            await callback.answer(gai.ADMIN_ALERT_BAD_BUTTON, show_alert=True)
        except Exception:
            pass


async def _handle_edit_claim(
    callback: CallbackQuery,
    bot: Bot,
    *,
    state: FSMContext | None,
    session: AsyncSession,
    escalation_id: int,
    admin_id: int,
    admin_name: str,
) -> None:
    """✏️ tap: claim the record, prompt the claimer in DM via FSM."""
    # Abandoning a previous edit claim (same admin tapped edit on a
    # second escalation) releases it back to pending first — with the
    # usual copy sync + fresh DMs, so it never strands without a
    # live keyboard.
    if state is not None:
        try:
            data = await state.get_data()
            previous_id = data.get("escalation_id")
            if previous_id and int(previous_id) != int(escalation_id):
                released_prev, prev_snapshot = await gai.release_escalation_edit(
                    session, int(previous_id), admin_id=admin_id
                )
                await state.clear()
                if released_prev and prev_snapshot is not None:
                    await _sync_admin_copies(
                        bot,
                        prev_snapshot,
                        status_line=gai.STATUS_LINE_EDIT_CANCELLED.format(name=admin_name),
                    )
                    await _resend_escalation_dms(bot, session, prev_snapshot)
        except Exception:
            pass

    outcome, record = await gai.claim_escalation_for_edit(
        session, escalation_id, admin_id=admin_id
    )
    if outcome == "not_found":
        await callback.answer(gai.ADMIN_ALERT_NOT_FOUND, show_alert=True)
        return
    if outcome == "expired":
        await _sync_admin_copies(bot, record, status_line=gai.STATUS_LINE_EXPIRED)
        await callback.answer(gai.ADMIN_ALERT_EXPIRED, show_alert=True)
        return
    if outcome != "claimed" or record is None:
        await _clear_keyboard(callback)
        await callback.answer(gai.ADMIN_ALERT_ALREADY_HANDLED, show_alert=True)
        return

    # Every admin's copy loses its keyboard while the edit runs.
    await _sync_admin_copies(
        bot, record, status_line=gai.STATUS_LINE_EDITING.format(name=admin_name)
    )

    if state is not None:
        await state.set_state(GroupAiEditStates.waiting_text)
        await state.update_data(escalation_id=int(escalation_id))
    try:
        await bot.send_message(
            admin_id,
            gai.EDIT_PROMPT_TEXT.format(draft=html.escape(record.get("draft_text") or "")),
            parse_mode="HTML",
            reply_markup=gai.build_edit_cancel_keyboard(escalation_id),
        )
    except Exception as exc:
        logger.warning("group_ai_admin: edit prompt DM failed: %s", exc)
    await callback.answer(gai.ADMIN_CONFIRM_EDIT_CLAIMED)


async def _handle_edit_cancel(
    callback: CallbackQuery,
    bot: Bot,
    *,
    state: FSMContext | None,
    session: AsyncSession | None,
    escalation_id: int,
    admin_id: int,
    admin_name: str,
) -> None:
    """❌ tap on the edit prompt: release the claim (owner-only)."""
    async with _session_or_new(session) as s:
        snapshot = await _release_edit_and_refresh(
            bot=bot,
            session=s,
            escalation_id=escalation_id,
            admin_id=admin_id,
            admin_name=admin_name,
            new_draft=None,
        )
        if snapshot is None:
            # Not this admin's claim (or no claim at all). Distinguish
            # "someone else is editing" from "nothing to cancel".
            from app.models.schema import GroupAiEscalation

            row = await s.get(GroupAiEscalation, int(escalation_id))
            if row is not None and row.status == "EDITING":
                await callback.answer(gai.ADMIN_ALERT_NOT_EDITOR, show_alert=True)
            else:
                await callback.answer(gai.ADMIN_ALERT_EDIT_GONE, show_alert=True)
            return
    if state is not None:
        try:
            await state.clear()
        except Exception:
            pass
    await callback.answer(gai.ADMIN_CONFIRM_EDIT_CANCELLED)


@group_admin_router.message(GroupAiEditStates.waiting_text, F.chat.type == ChatType.PRIVATE)
async def handle_group_ai_edit_text(
    message: Message,
    state: FSMContext = None,
    session: AsyncSession | None = None,
    bot: Bot = None,
) -> None:
    """The claiming admin's corrected draft arrives as a private text.

    The text is sanitised through the same emoji-strip path as any
    group post, saved as the record's draft_text (the AI original
    stays in original_draft_text), the claim is released back to
    PENDING, and every admin gets a fresh DM with the updated draft
    and a live keyboard — anyone may then approve or dismiss it.
    """
    try:
        admin = message.from_user
        if admin is None or not settings.is_admin(admin.id):
            return
        if state is None:
            return
        data = await state.get_data()
        escalation_id = data.get("escalation_id")
        if not escalation_id:
            await state.clear()
            return
        sender = bot or message.bot

        raw = (message.text or "").strip()
        if raw == "/cancel":
            async with _session_or_new(session) as s:
                await _release_edit_and_refresh(
                    bot=sender,
                    session=s,
                    escalation_id=int(escalation_id),
                    admin_id=admin.id,
                    admin_name=admin.first_name or "Admin",
                    new_draft=None,
                )
            await state.clear()
            await message.answer(gai.ADMIN_CONFIRM_EDIT_CANCELLED)
            return

        cleaned = gai.strip_emoji(raw)
        if not cleaned:
            await message.answer(gai.ADMIN_EDIT_EMPTY)
            return

        async with _session_or_new(session) as s:
            snapshot = await _release_edit_and_refresh(
                bot=sender,
                session=s,
                escalation_id=int(escalation_id),
                admin_id=admin.id,
                admin_name=admin.first_name or "Admin",
                new_draft=cleaned,
            )
        await state.clear()
        if snapshot is None:
            await message.answer(gai.ADMIN_ALERT_EDIT_GONE)
            return
        await message.answer(gai.ADMIN_CONFIRM_EDIT_SAVED)
    except Exception as exc:
        logger.warning("group_ai_admin: edit text handling failed: %s", exc)


async def _clear_keyboard(callback: CallbackQuery) -> None:
    """Remove the buttons from a handled escalation DM (best-effort)."""
    try:
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

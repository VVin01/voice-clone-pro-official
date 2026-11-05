"""Batch AP (2026-11-05): Telegram recap delivery is video only, with
companion files available only on explicit per-file button presses.

Owner order (Ulfa, 2026-11-05 20:00, clarified 20:02): when a recap
is delivered to Telegram, do NOT send the SRT / narration audio /
script along with it — send only the video. The extras are sent
solely when the user presses that file's own button on the bot
completion message ("SRT, အသံ ဖိုင်, script တို့က user လိုချင်လို့
ခလုတ်နှိပ်မှ ပို့").

Pinned here:
- ``_send_recap_telegram_delivery`` (Mini App send-telegram path)
  delivers only the video / the parts-as-delivery / the oversize
  notice across single, segmented, oversize-split and oversize
  cases, and never calls send_document / send_audio for companions;
- the former duplicate-parts tail (full video AND parts for a
  non-segmented job whose render fits the 50 MB cap) is gone;
- the endpoint no longer resolves or passes companion paths;
- the bot completion keyboard carries the video button plus one
  button per extra with the agreed callback data;
- each ``send_recap_<extra>_`` bot handler sends exactly its own
  file, enforces the video handler's ownership rules (AJ M7
  fail-closed included), and answers honestly via the Mini App
  route when its file is absent;
- Mini App downloads still expose every deliverable.
"""
from __future__ import annotations

import inspect
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models.schema  # noqa: F401  (register models)
from app.config import settings
from app.handlers import clone as clone_mod
from app.services.i18n import t
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
CLONE_PY = (ROOT / "app/handlers/clone.py").read_text(encoding="utf-8")

OWNER_ID = 880201
OTHER_ID = 880299


# ── Delivery helper (Mini App send-telegram path) ───────────────────


class _FakeSession:
    async def close(self):
        pass


class _RecordingBot:
    """Records (method, paths) for every send the helper performs."""

    calls: list = []

    def __init__(self, token=None, **_kw):
        self.session = _FakeSession()
        _RecordingBot.calls = []

    async def _record(self, method, **kwargs):
        paths = [
            str(v.path)
            for v in kwargs.values()
            if getattr(v, "path", None) is not None
        ]
        _RecordingBot.calls.append((method, paths))

    async def send_video(self, **kwargs):
        await self._record("send_video", **kwargs)

    async def send_document(self, **kwargs):
        await self._record("send_document", **kwargs)

    async def send_audio(self, **kwargs):
        await self._record("send_audio", **kwargs)

    async def send_message(self, **kwargs):
        await self._record("send_message", **kwargs)


@pytest.fixture
def recording_bot(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123456:batch-ap-test")
    _RecordingBot.calls = []
    monkeypatch.setattr("aiogram.Bot", _RecordingBot)
    return _RecordingBot


def _small_file(path: Path, payload: bytes = b"x" * 2048) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _big_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(51 * 1024 * 1024)
    return path


async def _deliver(job_id="rc_ap_helper"):
    return await api_mod._send_recap_telegram_delivery(
        chat_id=OWNER_ID,
        video_path=_CURRENT_VIDEO[0],
        title="Some Title",
        style="Pro Dubbed",
        voice_name="Ko Min",
        duration_sec=60,
        job_id=job_id,
    )


_CURRENT_VIDEO: list = [None]


@pytest.mark.asyncio
async def test_ap_helper_single_video_only(recording_bot, tmp_path, monkeypatch):
    video = _small_file(tmp_path / "final.mp4")
    _CURRENT_VIDEO[0] = str(video)
    monkeypatch.setattr(api_mod, "resolve_recap_part_paths", lambda job_id: [])
    delivered, reason = await _deliver()
    assert delivered is True
    assert reason is None
    assert recording_bot.calls == [("send_video", [str(video)])]


@pytest.mark.asyncio
async def test_ap_helper_segmented_sends_parts_only(recording_bot, tmp_path, monkeypatch):
    video = _small_file(tmp_path / "part1-registered.mp4")
    _CURRENT_VIDEO[0] = str(video)
    part1 = _small_file(tmp_path / "parts" / "recap_part_01.mp4")
    part2 = _small_file(tmp_path / "parts" / "recap_part_02.mp4")
    parts = [
        {"n": 1, "label": "Part 1", "path": str(part1)},
        {"n": 2, "label": "Part 2", "path": str(part2)},
    ]
    monkeypatch.setattr(api_mod, "resolve_recap_part_paths", lambda job_id: parts)
    monkeypatch.setitem(
        api_mod.RECAP_JOBS_REGISTRY, "rc_ap_seg", {"segmented": True}
    )
    delivered, _ = await _deliver("rc_ap_seg")
    assert delivered is True
    assert recording_bot.calls == [
        ("send_video", [str(part1)]),
        ("send_video", [str(part2)]),
    ]


@pytest.mark.asyncio
async def test_ap_helper_oversize_with_parts_sends_parts_only(
    recording_bot, tmp_path, monkeypatch
):
    video = _big_file(tmp_path / "huge.mp4")
    _CURRENT_VIDEO[0] = str(video)
    part1 = _small_file(tmp_path / "parts" / "recap_part_01.mp4")
    parts = [{"n": 1, "label": "Part 1", "path": str(part1)}]
    monkeypatch.setattr(api_mod, "resolve_recap_part_paths", lambda job_id: parts)
    delivered, _ = await _deliver("rc_ap_oversize_parts")
    assert delivered is True
    assert recording_bot.calls == [("send_video", [str(part1)])]


@pytest.mark.asyncio
async def test_ap_helper_oversize_without_parts_sends_notice_only(
    recording_bot, tmp_path, monkeypatch
):
    video = _big_file(tmp_path / "huge.mp4")
    _CURRENT_VIDEO[0] = str(video)
    monkeypatch.setattr(api_mod, "resolve_recap_part_paths", lambda job_id: [])
    delivered, _ = await _deliver("rc_ap_oversize_notice")
    assert delivered is True
    assert [m for m, _ in recording_bot.calls] == ["send_message"]


@pytest.mark.asyncio
async def test_ap_helper_no_duplicate_parts_tail(recording_bot, tmp_path, monkeypatch):
    """A non-segmented job whose full render fits the cap used to get
    the video AND every part. Batch AP: the video only."""
    video = _small_file(tmp_path / "final.mp4")
    _CURRENT_VIDEO[0] = str(video)
    part1 = _small_file(tmp_path / "parts" / "recap_part_01.mp4")
    parts = [{"n": 1, "label": "Part 1", "path": str(part1)}]
    monkeypatch.setattr(api_mod, "resolve_recap_part_paths", lambda job_id: parts)
    delivered, _ = await _deliver("rc_ap_tail")
    assert delivered is True
    assert recording_bot.calls == [("send_video", [str(video)])]


def test_ap_helper_signature_has_no_companion_paths():
    params = inspect.signature(api_mod._send_recap_telegram_delivery).parameters
    assert "audio_path" not in params
    assert "srt_path" not in params
    assert "script_path" not in params


# ── Endpoint: resolves/passes the video only ────────────────────────


class _FakeDB:
    async def get(self, model, pk):
        return None


@pytest.mark.asyncio
async def test_ap_endpoint_passes_video_only(monkeypatch, tmp_path):
    job_id = "rc_ap_endpoint"
    video = _small_file(tmp_path / "final.mp4")
    monkeypatch.setitem(
        api_mod.RECAP_JOBS_REGISTRY, job_id, {"user_id": OWNER_ID, "title": "T"}
    )

    resolved_kinds = []

    def _resolve(jid, kind, result_path=None):
        resolved_kinds.append(kind)
        return str(video) if kind == "video" else str(tmp_path / f"extra.{kind}")

    monkeypatch.setattr(api_mod, "resolve_recap_deliverable_path", _resolve)

    captured = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return True, None

    monkeypatch.setattr(api_mod, "_send_recap_telegram_delivery", _capture)
    user = SimpleNamespace(id=OWNER_ID)
    out = await api_mod.send_recap_to_telegram(
        job_id=job_id, user=user, db=_FakeDB(), _gate=user
    )
    assert out["success"] is True
    assert resolved_kinds == ["video"]
    assert set(captured) == {
        "chat_id", "video_path", "title", "style",
        "voice_name", "duration_sec", "job_id",
    }


# ── Completion keyboard: four buttons, agreed callbacks ─────────────


def test_ap_completion_keyboard_has_all_four_buttons():
    start = API_PY.index(
        'clean_title = _clean_recap_display_title(entry.get("title"), req.url)'
    )
    body = API_PY[start:API_PY.index("notify_text = t(", start)]
    callbacks = re.findall(r'callback_data=f"([^"]+)"', body)
    assert callbacks == [
        "send_recap_{job_id}",
        "send_recap_srt_{job_id}",
        "send_recap_audio_{job_id}",
        "send_recap_script_{job_id}",
    ]
    # The three extra labels are real i18n keys in both locales.
    for key in (
        "clone_btn_send_srt",
        "clone_btn_send_recap_audio",
        "clone_btn_send_script",
        "clone_toast_sending_file",
    ):
        assert t(key, "my") != key
        assert t(key, "en") != key


def test_ap_extra_handlers_registered_before_broad_video_handler():
    """All four callbacks share the send_recap_ prefix; aiogram
    dispatches the first match, so the specific handlers must be
    registered before the broad video handler."""
    idx_srt = CLONE_PY.index('F.data.startswith("send_recap_srt_")')
    idx_audio = CLONE_PY.index('F.data.startswith("send_recap_audio_")')
    idx_script = CLONE_PY.index('F.data.startswith("send_recap_script_")')
    idx_video = CLONE_PY.index('F.data.startswith("send_recap_")')
    assert idx_srt < idx_video
    assert idx_audio < idx_video
    assert idx_script < idx_video


def test_ap_bot_video_handler_source_sends_no_srt():
    """The bot video handler itself no longer resolves or sends an
    SRT (the segmented branch used to send it first)."""
    start = CLONE_PY.index("async def handle_send_recap_callback")
    body = CLONE_PY[start:]
    assert "resolved_srt" not in body
    assert "clone_caption_subtitles" not in body


# ── Per-file bot handlers ───────────────────────────────────────────


def _mk_cb(data, uid=OWNER_ID):
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=uid, is_bot=False, first_name="QA")
    cb.data = data
    cb.bot = MagicMock()
    cb.bot.send_audio = AsyncMock()
    cb.bot.send_video = AsyncMock()
    cb.bot.send_document = AsyncMock()
    cb.bot.send_message = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _patch_clone_sessions(monkeypatch, db_session):
    maker = async_sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(clone_mod, "AsyncSessionLocal", maker)
    return clone_mod


async def _seed_recap_job(db_session, uid=OWNER_ID, job_id=None):
    from app.models.enums import JobStatus
    from app.models.schema import Job
    from app.services.ledger import get_or_create_user

    user, _ = await get_or_create_user(db_session, uid)
    job_id = job_id or f"rc_ap_{uuid.uuid4().hex[:10]}"
    job = Job(
        id=job_id,
        user_id=uid,
        status=JobStatus.SUCCESS.value,
        result_path=None,
        engine="recap",
        job_type="RECAP",
    )
    db_session.add(job)
    await db_session.commit()
    return job_id


def _write_extra(job_dir: Path, kind: str) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    if kind == "srt":
        p = job_dir / "recap_myanmar_synced.srt"
        p.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    elif kind == "audio":
        p = job_dir / "final_audio.mp3"
        p.write_bytes(b"a" * 2048)
    else:
        # The pipeline's canonical script name (<job_id>_script.txt);
        # the resolver also rebuilds exactly this file on demand from
        # the sync plan / SRT when it is absent.
        p = job_dir / f"{job_dir.name}_script.txt"
        p.write_text("script body", encoding="utf-8")
    return p


def _sent_file_path(call_kwargs):
    for key in ("document", "audio", "video"):
        v = call_kwargs.get(key)
        if v is not None:
            return str(getattr(v, "path", v))
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,send_method,caption_key,brand",
    [
        ("srt", "send_document", "clone_caption_subtitles", "Subtitles"),
        ("audio", "send_audio", "clone_caption_recap_audio", "RecapAudio"),
        ("script", "send_document", "clone_caption_recap_script", "RecapScript"),
    ],
)
async def test_ap_extra_handler_sends_exactly_its_file(
    db_session, tmp_path, monkeypatch, kind, send_method, caption_key, brand
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    job_id = await _seed_recap_job(db_session)
    job_dir = tmp_path / "recap_output" / job_id
    # All three extras exist on disk; the press must fetch only its own.
    paths = {k: _write_extra(job_dir, k) for k in ("srt", "audio", "script")}
    _patch_clone_sessions(monkeypatch, db_session)

    cb = _mk_cb(f"send_recap_{kind}_{job_id}")
    handler = {
        "srt": clone_mod.handle_send_recap_srt_callback,
        "audio": clone_mod.handle_send_recap_audio_callback,
        "script": clone_mod.handle_send_recap_script_callback,
    }[kind]
    await handler(cb)

    send = getattr(cb.bot, send_method)
    send.assert_awaited_once()
    kwargs = send.call_args.kwargs
    assert _sent_file_path(kwargs) == str(paths[kind])
    assert kwargs["caption"] == t(caption_key, "my")
    sent_name = getattr(
        kwargs.get("document") or kwargs.get("audio"), "filename", ""
    )
    assert sent_name.startswith(f"VoiceClonePro_{brand}_")
    # Nothing else was delivered: no other file sends, no messages.
    for other in ("send_document", "send_audio", "send_video"):
        if other != send_method:
            getattr(cb.bot, other).assert_not_called()
    cb.bot.send_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["srt", "audio", "script"])
async def test_ap_extra_handler_rejects_non_owner(
    db_session, tmp_path, monkeypatch, kind
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    job_id = await _seed_recap_job(db_session, uid=OWNER_ID)
    _write_extra(tmp_path / "recap_output" / job_id, kind)
    _patch_clone_sessions(monkeypatch, db_session)

    cb = _mk_cb(f"send_recap_{kind}_{job_id}", uid=OTHER_ID)
    handler = {
        "srt": clone_mod.handle_send_recap_srt_callback,
        "audio": clone_mod.handle_send_recap_audio_callback,
        "script": clone_mod.handle_send_recap_script_callback,
    }[kind]
    await handler(cb)
    cb.bot.send_document.assert_not_called()
    cb.bot.send_audio.assert_not_called()
    cb.bot.send_message.assert_awaited_once()
    assert cb.bot.send_message.call_args.kwargs["text"] == t(
        "clone_audio_not_found", "my"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["srt", "audio", "script"])
async def test_ap_extra_handler_missing_file_answers_honestly(
    db_session, tmp_path, monkeypatch, kind
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    job_id = await _seed_recap_job(db_session)
    _patch_clone_sessions(monkeypatch, db_session)

    cb = _mk_cb(f"send_recap_{kind}_{job_id}")
    handler = {
        "srt": clone_mod.handle_send_recap_srt_callback,
        "audio": clone_mod.handle_send_recap_audio_callback,
        "script": clone_mod.handle_send_recap_script_callback,
    }[kind]
    await handler(cb)
    cb.bot.send_document.assert_not_called()
    cb.bot.send_audio.assert_not_called()
    cb.bot.send_message.assert_awaited_once()
    kwargs = cb.bot.send_message.call_args.kwargs
    assert kwargs["text"] == t("clone_recap_file_missing_text", "my", job_id=job_id)
    kb = kwargs["reply_markup"]
    urls = [
        btn.web_app.url
        for row in kb.inline_keyboard
        for btn in row
        if btn.web_app
    ]
    assert urls and f"recap_job_id={job_id}" in urls[0]


@pytest.mark.asyncio
async def test_ap_extra_handler_registry_only_owner_and_stranger(
    db_session, tmp_path, monkeypatch
):
    """AJ M7 parity: with no Job row, the registry entry must prove
    ownership — the owner gets the file, a stranger is refused."""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    job_id = "rc_ap_registry_only"
    srt = _write_extra(tmp_path / "recap_output" / job_id, "srt")
    monkeypatch.setitem(
        api_mod.RECAP_JOBS_REGISTRY, job_id,
        {"user_id": OWNER_ID, "title": "Registry Job"},
    )
    _patch_clone_sessions(monkeypatch, db_session)

    owner_cb = _mk_cb(f"send_recap_srt_{job_id}", uid=OWNER_ID)
    await clone_mod.handle_send_recap_srt_callback(owner_cb)
    owner_cb.bot.send_document.assert_awaited_once()
    assert _sent_file_path(owner_cb.bot.send_document.call_args.kwargs) == str(srt)

    stranger_cb = _mk_cb(f"send_recap_srt_{job_id}", uid=OTHER_ID)
    await clone_mod.handle_send_recap_srt_callback(stranger_cb)
    stranger_cb.bot.send_document.assert_not_called()
    stranger_cb.bot.send_message.assert_awaited_once()


# ── Mini App downloads untouched ────────────────────────────────────


def test_ap_miniapp_deliverables_still_include_all_files():
    job = SimpleNamespace(
        id="rc_ap_dl", status="SUCCESS", job_type="RECAP", title="T"
    )
    _primary, out = api_mod._job_deliverables(job, True, True)
    for key in ("video", "audio", "srt", "script"):
        assert key in out

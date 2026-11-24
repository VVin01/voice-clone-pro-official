from __future__ import annotations

import logging
import os
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


def _parse_ids(raw_value: str | set[int] | list[int]) -> set[int]:
    """Parse comma/semicolon/newline separated Telegram IDs into a set."""
    if isinstance(raw_value, (set, list)):
        return {int(x) for x in raw_value}
    if not isinstance(raw_value, str):
        return set()
    values: set[int] = set()
    for token in raw_value.replace(";", ",").replace("\n", ",").split(","):
        token = token.strip().strip('"').strip("'")
        if token.isdigit():
            values.add(int(token))
    return values


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "development"
    app_name: str = "VoiceCloneProOfficial"
    log_level: str = "INFO"

    # Telegram Bot
    bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    # Internal worker token, separate from the Telegram bot token.
    # Env-only and rotatable. When empty, the internal worker-upload
    # endpoint fails closed (404 — it is effectively absent) and the
    # co-located worker skips cloud sync; there is NO fallback to the
    # bot token.
    worker_token: str = Field(default="", alias="WORKER_TOKEN")
    admin_ids_raw: str = Field(default="", alias="ADMIN_IDS")

    # Instance role & locality configuration (Multi-instance / VPS second worker)
    # INSTANCE_PRIMARY: True by default (Render primary instance). When False
    # (e.g. VPS second worker), singleton duties (orphan boot sweeps, park-and-ask
    # resumer, retention cleanup) are skipped to prevent duplicate sweeps/messages.
    instance_primary: bool = Field(default=True, alias="INSTANCE_PRIMARY")
    # WORKER_CLAIM_LOCAL_FILES: True by default (Render primary). When False / 0
    # (e.g. VPS second worker with isolated disk), the worker skips queued jobs
    # whose source_audio_path is a local path; the primary (which holds the file)
    # claims them instead, preventing missing-reference execution.
    worker_claim_local_files: bool = Field(default=True, alias="WORKER_CLAIM_LOCAL_FILES")

    # Recap on DB job queue (Second Worker Phase 2, 2026-11-16)
    # RECAP_ON_QUEUE: False by default (in-process rendering on primary).
    # When True, recaps move onto the DB job queue and are claimed by workers.
    recap_on_queue: bool = Field(default=False, alias="RECAP_ON_QUEUE")
    recap_stale_job_timeout_seconds: int = Field(default=2700, alias="RECAP_STALE_JOB_TIMEOUT_SECONDS")
    recap_worker_concurrency: int = Field(default=1, alias="RECAP_WORKER_CONCURRENCY")

    # Telegram update intake mode & webhook configuration
    # BOT_MODE: "polling" (default, existing behaviour) or "webhook".
    # In webhook mode, long polling is disabled and updates arrive via
    # POST {BOT_WEBHOOK_PATH} on the existing FastAPI Mini App service.
    bot_mode: str = Field(default="polling", alias="BOT_MODE")
    telegram_webhook_secret: str = Field(default="", alias="TELEGRAM_WEBHOOK_SECRET")
    bot_webhook_path: str = Field(default="/telegram/webhook", alias="BOT_WEBHOOK_PATH")
    public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")
    # Public URL of the Mini App itself, used for WebAppInfo buttons.
    # A real field (not a getattr fallback) so MINI_APP_URL in the
    # environment actually takes effect — Settings ignores unknown
    # env names, which silently pinned every button to the default.
    mini_app_url: str = Field(
        default="https://voice-clone-studio-c1er.onrender.com",
        alias="MINI_APP_URL",
    )

    # Local Telegram Bot API server (lifts official 50 MB upload cap -> 2,000 MB / 2 GB)
    # Example: TELEGRAM_BOT_API_URL=https://telegram-bot-api-kydu.onrender.com
    # Leave TELEGRAM_BOT_API_IS_LOCAL=false for remote HTTP Local Bot API servers
    # (so Aiogram uploads via multipart HTTP); set true ONLY when sharing disk on same host.
    telegram_bot_api_url: str = Field(default="", alias="TELEGRAM_BOT_API_URL")
    telegram_bot_api_is_local: bool = Field(default=False, alias="TELEGRAM_BOT_API_IS_LOCAL")
    # 0 = auto (2000 MB when Local Bot API is active, 50 MB on official api.telegram.org).
    telegram_max_upload_mb: int = Field(default=0, alias="TELEGRAM_MAX_UPLOAD_MB")
    telegram_bot_api_timeout_seconds: int = Field(default=300, alias="TELEGRAM_BOT_API_TIMEOUT_SECONDS")

    # Bandwidth & Anti-Flood Shield (DDoS / Static Flood Protection)
    # Optional Cloudflare Origin Secret: when non-empty, every request except
    # /health and the Telegram webhook path must present X-Origin-Secret matching
    # this secret (blocks direct attacks bypassing Cloudflare to *.onrender.com).
    cf_origin_secret: str = Field(default="", alias="CF_ORIGIN_SECRET")
    # Per-IP sliding-window flood cap on "/" and "/static/*" (GET/HEAD).
    static_flood_max_requests: int = Field(default=180, alias="STATIC_FLOOD_MAX_REQUESTS")
    static_flood_window_seconds: int = Field(default=60, alias="STATIC_FLOOD_WINDOW_SECONDS")

    # Membership Gate (Channel and Group)
    required_channel_id: str = Field(default="", alias="REQUIRED_CHANNEL_ID")
    required_group_id: str = Field(default="", alias="REQUIRED_GROUP_ID")
    channel_invite_link: str = Field(default="https://t.me/voiceclonepro", alias="CHANNEL_INVITE_LINK")
    group_invite_link: str = Field(default="https://t.me/voiceclonepro_chat", alias="GROUP_INVITE_LINK")
    # Bot-side periodic re-check of already-verified members (closes the
    # join-then-leave hole). A verified user's live membership is
    # re-probed at most once per TTL; on a Telegram API error the user
    # keeps access and the probe is retried after the shorter backoff.
    membership_recheck_ttl_seconds: int = Field(default=10800, alias="MEMBERSHIP_RECHECK_TTL_SECONDS")  # 3 hours
    membership_recheck_error_retry_seconds: int = Field(
        default=600, alias="MEMBERSHIP_RECHECK_ERROR_RETRY_SECONDS"
    )  # 10 minutes

    # Database: ISOLATED local SQLite by default, strictly separate from legacy/production
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/voice_clone_v2.db",
        alias="DATABASE_URL",
    )
    # Batch AJ L14: legacy_database_url removed — it had no reader
    # anywhere in the codebase (the isolated-v2 database_url above is
    # the only database setting the app honours). extra="ignore"
    # means a stale LEGACY_DATABASE_URL env var is harmless.
    expected_db_identity: str = Field(
        default="voice_clone_v2",
        alias="EXPECTED_DB_IDENTITY",
    )
    # Connection pool sizing (Round 15 Part D, 2026-11-23): Supabase Postgres
    # session-mode pooler caps at 15 clients across all processes. Safe repo
    # defaults (pool_size=5, max_overflow=2 -> max 7 per process, <=14 across
    # web + worker) prevent EMAXCONNSESSION even without env overrides; deploy
    # env overrides further split web (6+1=7) and worker (4+1=5) to 12 total,
    # leaving >=3 slots for sync-store / admin transient connections.
    db_pool_size: int = Field(default=5, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=2, alias="DB_MAX_OVERFLOW")
    db_pool_timeout: int = Field(default=30, alias="DB_POOL_TIMEOUT")
    db_pool_recycle: int = Field(default=1800, alias="DB_POOL_RECYCLE")

    # Project and storage paths
    root: Path = ROOT
    storage_root: Path = ROOT / "storage"
    locales_root: Path = ROOT / "locales"
    data_root: Path = ROOT / "data"

    # User Account Policies
    free_daily_allowance_seconds: int = Field(default=180, alias="FREE_DAILY_ALLOWANCE_SECONDS")  # 3 minutes/UTC day, non-accumulating
    free_monthly_allowance_seconds: int = Field(default=180, alias="FREE_MONTHLY_ALLOWANCE_SECONDS")  # Retired (no longer grants)
    welcome_bonus_seconds: int = Field(default=0, alias="WELCOME_BONUS_SECONDS")  # Retired in V2 (default 0)
    character_limit_free: int = Field(default=3000, alias="CHARACTER_LIMIT_FREE")  # Free tier: 3000 chars/generation
    character_limit_purchased: int = Field(default=15000, alias="CHARACTER_LIMIT_PURCHASED")  # VIP: 15000 chars/generation
    recap_free_max_duration_seconds: int = Field(default=120, alias="RECAP_FREE_MAX_DURATION_SECONDS")  # Free: 2 mins (120s)
    recap_max_duration_seconds: int = Field(default=600, alias="RECAP_MAX_DURATION_SECONDS")  # VIP: 10 mins (600s)
    # Batch BZ: default 0 — the FREE job-COUNT limit is retired as a
    # default (BX's guide already stopped naming it; the monthly
    # allowance + per-day minute caps are the real FREE governors).
    # The queue.py check stays and no-ops at 0, so an admin can still
    # re-enable a count cap from Admin Settings without a deploy.
    free_daily_job_limit: int = Field(default=0, alias="FREE_DAILY_JOB_LIMIT")
    purchased_daily_job_limit: int = Field(default=0, alias="PURCHASED_DAILY_JOB_LIMIT")  # 0 = unlimited
    # Batch AN: per-user DAILY voice-minutes cap, split by tier (owner
    # order 2026-11-05 — the scarce resource is HF caller quota, so one
    # user must not burn the pool in a day). Enforced in
    # create_voice_clone_job for FREE + PURCHASED; admins exempt;
    # 0 = unlimited for that tier.
    # Batch BZ: the FREE default is 0 (unlimited) — the old 5 min/day
    # cap could never bind because the FREE monthly wallet is only
    # 3 minutes, and quoting both numbers confused users (and the AI
    # fact sheet). The monthly 180 s allowance governs FREE; the cap
    # mechanism stays for admins to re-enable. PURCHASED stays 30.
    voice_daily_limit_minutes_free: int = Field(default=0, alias="VOICE_DAILY_LIMIT_MINUTES_FREE")  # 0 = unlimited
    voice_daily_limit_minutes_paid: int = Field(default=0, alias="VOICE_DAILY_LIMIT_MINUTES_PAID")  # 0 = unlimited (abolished 2026-11-23)
    # Batch AW: per-user DAILY TTS-tab minutes cap, split by tier like
    # Batch AN but on its own allowance — the TTS tab never deducts
    # voice minutes or recap credits; referral seconds are spent
    # before this cap is touched. Enforced in create_tts_job for
    # FREE + PURCHASED; admins exempt; 0 = unlimited for that tier.
    tts_daily_limit_minutes_free: int = Field(default=15, alias="TTS_DAILY_LIMIT_MINUTES_FREE")  # 0 = unlimited
    tts_daily_limit_minutes_paid: int = Field(default=60, alias="TTS_DAILY_LIMIT_MINUTES_PAID")  # 0 = unlimited
    # Batch CG / 2026-11-23: Gemini TTS pool "taste" — how much Gemini
    # audio per UTC day the SERVICE POOL funds before the route steps to
    # the user's own key (if saved, never limited) or Edge.
    # Free: 2 min/day (plus global free daily request budget below).
    # Paid: 10 min/day (600 delivered seconds). Admins exempt.
    free_gemini_taste_minutes: int = Field(default=2, alias="FREE_GEMINI_TASTE_MINUTES")
    paid_gemini_taste_minutes: int = Field(default=10, alias="PAID_GEMINI_TASTE_MINUTES")
    # Batch CG: the global FREE daily budget — total successful
    # pool Gemini TTS requests across ALL FREE users per UTC day.
    # Bounds the pool drain no matter how many FREE accounts show
    # up; when spent, FREE Gemini closes for the day (own keys and
    # Edge are unaffected, PURCHASED keep the pool).
    free_gemini_daily_budget_requests: int = Field(default=100, alias="FREE_GEMINI_DAILY_BUDGET_REQUESTS")
    # Batch AO: tiered recap pricing kill switch (owner decision
    # 2026-11-05 — credits = ceil(source duration / 180s), one credit
    # per started 3-minute band). Ships DARK: default False, and OFF
    # every recap charge/quote/label is exactly the legacy per-part
    # behaviour. Arming is a separate, later, announced step — the
    # owner flips this only at the announced effective date.
    tiered_recap_pricing_enabled: bool = Field(default=False, alias="TIERED_RECAP_PRICING_ENABLED")
    max_concurrent_jobs_per_user: int = Field(default=1, alias="MAX_CONCURRENT_JOBS_PER_USER")
    maintenance_mode: bool = Field(default=False, alias="MAINTENANCE_MODE")
    # Admin-written announcement the Group AI quotes verbatim from
    # its fact sheet while maintenance mode is on (e.g. the reopening
    # time); empty means no announced detail. BotSetting-backed like
    # the mode itself — see app/services/dynamic_settings.py (Batch Z).
    maintenance_note: str = Field(default="", alias="MAINTENANCE_NOTE")
    # AI group-admin auto-reply kill switch (BotSetting-backed; the
    # handler re-reads the DB row per message, so Admin toggles apply
    # instantly). Default OFF — see app/services/group_ai_admin.py.
    group_ai_admin_enabled: bool = Field(default=False, alias="GROUP_AI_ADMIN_ENABLED")
    # Group-AI anti-spam tuning (BotSetting-backed like the kill
    # switch: the handler re-reads the DB rows per message via
    # app/services/group_ai_admin.get_limits, so Admin edits apply
    # without a deploy). Only real AI calls consume the budgets;
    # deterministic fast paths, FAQ-cache hits and deferrals are free.
    group_ai_user_cooldown_seconds: int = Field(default=60, alias="GROUP_AI_USER_COOLDOWN_SECONDS")
    group_ai_hourly_budget: int = Field(default=40, alias="GROUP_AI_HOURLY_BUDGET")
    group_ai_daily_budget: int = Field(default=300, alias="GROUP_AI_DAILY_BUDGET")

    # Group-AI moderation ladder + voice + knowledge sheet (BotSetting-backed)
    group_ai_moderation_enabled: bool = Field(default=True, alias="GROUP_AI_MODERATION_ENABLED")
    group_ai_moderation_confidence_threshold: float = Field(default=0.8, alias="GROUP_AI_MODERATION_CONFIDENCE_THRESHOLD")
    group_ai_moderation_mute_duration_strike2_seconds: int = Field(default=3600, alias="GROUP_AI_MODERATION_MUTE_DURATION_STRIKE2_SECONDS")
    group_ai_moderation_mute_duration_strike3_seconds: int = Field(default=86400, alias="GROUP_AI_MODERATION_MUTE_DURATION_STRIKE3_SECONDS")
    group_ai_moderation_whitelist: str = Field(default="", alias="GROUP_AI_MODERATION_WHITELIST")
    group_ai_voice_enabled: bool = Field(default=True, alias="GROUP_AI_VOICE_ENABLED")
    group_ai_voice_daily_cap: int = Field(default=60, alias="GROUP_AI_VOICE_DAILY_CAP")
    group_ai_knowledge_sheet: str = Field(default="", alias="GROUP_AI_KNOWLEDGE_SHEET")
    group_ai_knowledge_sheet_saved_at: str = Field(default="", alias="GROUP_AI_KNOWLEDGE_SHEET_SAVED_AT")
    group_ai_knowledge_sheet_saved_by: str = Field(default="", alias="GROUP_AI_KNOWLEDGE_SHEET_SAVED_BY")
    # Batch BX: bot PRIVATE-chat AI auto-reply kill switch (owner
    # order 2026-11-07 — plain-text private messages must be answered,
    # not met with silence). BotSetting-backed like the group switch;
    # the handler re-reads the DB row per message. Default ON (the
    # owner's explicit order), unlike the group switch's OFF.
    private_ai_enabled: bool = Field(default=True, alias="PRIVATE_AI_ENABLED")
    # Private-AI anti-spam tuning (BotSetting-backed, read per message
    # via app/services/private_ai.get_private_limits). Only real AI
    # calls consume the budgets; fast paths and deferrals are free.
    private_ai_user_cooldown_seconds: int = Field(default=60, alias="PRIVATE_AI_USER_COOLDOWN_SECONDS")
    private_ai_hourly_budget: int = Field(default=40, alias="PRIVATE_AI_HOURLY_BUDGET")
    private_ai_daily_budget: int = Field(default=300, alias="PRIVATE_AI_DAILY_BUDGET")

    # Infrastructure Safety Limit (independent of user account tiers, protects Edge TTS & VoxCPM memory)
    infrastructure_safety_character_limit: int = Field(
        default=15000,
        alias="INFRASTRUCTURE_SAFETY_CHARACTER_LIMIT",
    )

    # Stale Job Crash Recovery (seconds)
    stale_job_timeout_seconds: int = Field(default=1800, alias="STALE_JOB_TIMEOUT_SECONDS")

    # Generated-media retention (days). Job outputs live on the persistent
    # disk (storage_root) and survive restarts/deploys; the periodic sweep
    # removes a job's files and record only once it is older than this
    # window. Batch X (owner's order 2026-11-04): the window is 24 HOURS
    # (1 day), not the old 7 days — the 10 GB disk filled with in-window
    # outputs. User-facing copy states this same number — keep in sync.
    media_retention_days: int = Field(default=1, alias="MEDIA_RETENTION_DAYS")

    # Tutorial Links & Controls (Voice Clone & Movie Recap)
    tutorial_video_link: str = Field(
        default="https://t.me/VoiceClonePro/28",
        alias="TUTORIAL_VIDEO_LINK",
    )
    tutorial_video_enabled: bool = Field(default=True, alias="TUTORIAL_VIDEO_ENABLED")

    tutorial_voice_title: str = Field(
        default="🎙️ Voice Clone ပြုလုပ်နည်း လမ်းညွှန်",
        alias="TUTORIAL_VOICE_TITLE",
    )
    tutorial_voice_link: str = Field(
        default="https://t.me/VoiceClonePro/29",
        alias="TUTORIAL_VOICE_LINK",
    )
    tutorial_voice_enabled: bool = Field(default=True, alias="TUTORIAL_VOICE_ENABLED")

    tutorial_recap_title: str = Field(
        default="🎬 Movie Recap ပြုလုပ်နည်း လမ်းညွှန်",
        alias="TUTORIAL_RECAP_TITLE",
    )
    tutorial_recap_link: str = Field(
        default="https://t.me/VoiceClonePro/30",
        alias="TUTORIAL_RECAP_LINK",
    )
    tutorial_recap_enabled: bool = Field(default=True, alias="TUTORIAL_RECAP_ENABLED")

    # Recap demo sample (batch L): a Telegram file_id or a URL for a
    # short before/after sample shown on the Recap tab. Empty (the
    # default) hides the slot — the owner supplies the asset later;
    # no placeholder media is bundled.
    recap_demo_asset: str = Field(
        default="",
        alias="RECAP_DEMO_ASSET",
    )

    # Remaining tutorial links (premium polish follow-up): every tutorial
    # in the @VoiceClonePro channel is admin-editable from Admin → Settings,
    # not just video/voice/recap above. Defaults match the frontend
    # TUTORIAL_LINKS registry; msg 38 is the index post linking all 10.
    tutorial_index_link: str = Field(
        default="https://t.me/VoiceClonePro/38",
        alias="TUTORIAL_INDEX_LINK",
    )
    tutorial_plans_link: str = Field(
        default="https://t.me/VoiceClonePro/31",
        alias="TUTORIAL_PLANS_LINK",
    )
    tutorial_customvoice_link: str = Field(
        default="https://t.me/VoiceClonePro/32",
        alias="TUTORIAL_CUSTOMVOICE_LINK",
    )
    tutorial_downloader_link: str = Field(
        default="https://t.me/VoiceClonePro/33",
        alias="TUTORIAL_DOWNLOADER_LINK",
    )
    tutorial_scriptedit_link: str = Field(
        default="https://t.me/VoiceClonePro/34",
        alias="TUTORIAL_SCRIPTEDIT_LINK",
    )
    tutorial_recapadvanced_link: str = Field(
        default="https://t.me/VoiceClonePro/35",
        alias="TUTORIAL_RECAPADVANCED_LINK",
    )
    tutorial_refer_link: str = Field(
        default="https://t.me/VoiceClonePro/36",
        alias="TUTORIAL_REFER_LINK",
    )
    tutorial_support_link: str = Field(
        default="https://t.me/VoiceClonePro/37",
        alias="TUTORIAL_SUPPORT_LINK",
    )

    # Edge TTS defaults
    default_tts_voice: str = "my-MM-ThihaNeural"

    # VoxCPM 2 Neural TTS & Voice Cloning
    voxcpm_space_url: str = Field(default="", alias="VOXCPM_SPACE_URL")
    voxcpm_spaces: str = Field(default="", alias="VOXCPM_SPACES")
    voxcpm_timeout: int = Field(default=60, alias="VOXCPM_TIMEOUT")
    groq_whisper_model: str = Field(default="whisper-large-v3-turbo", alias="GROQ_WHISPER_MODEL")
    hf_token: str = Field(default="", alias="HF_TOKEN")
    hf_tokens: str = Field(default="", alias="HF_TOKENS")

    # Recap Studio Policies
    # NOTE: RECAP_MAX_DURATION_SECONDS is defined once above (User Account Policies,
    # default 600). A duplicate definition previously lived here and was removed
    # in polish-premium-2026-10-28; do not re-add it, Pydantic silently kept the last one.

    # Gemini AI Dynamic Model Chain (Active 3.x models; 2.5/2.0/1.5 are retired on Google API)
    gemini_primary_model: str = Field(default="gemini-3.6-flash", alias="GEMINI_PRIMARY_MODEL")
    gemini_fallback_1: str = Field(default="gemini-3.5-flash", alias="GEMINI_FALLBACK_1")
    gemini_fallback_2: str = Field(default="gemini-3.5-flash-lite", alias="GEMINI_FALLBACK_2")
    gemini_fallback_3: str = Field(default="gemini-flash-lite-latest", alias="GEMINI_FALLBACK_3")

    # Gemini TTS Dynamic Model Chain (GA models only; Nov 17 retired previews removed)
    gemini_tts_primary_model: str = Field(default="gemini-3.8-flash-tts", alias="GEMINI_TTS_PRIMARY_MODEL")
    gemini_tts_fallback_1: str = Field(default="", alias="GEMINI_TTS_FALLBACK_1")
    gemini_tts_lite_model: str = Field(default="gemini-3.8-flash-lite-tts", alias="GEMINI_TTS_LITE_MODEL")
    gemini_tts_fallback_2: str = Field(default="", alias="GEMINI_TTS_FALLBACK_2")
    gemini_tts_leak_gate_mode: str = Field(default="shadow", alias="GEMINI_TTS_LEAK_GATE_MODE")

    # Versioned AI Prompt Registry profile ("v2" upgraded default, "v1" instant rollback)
    ai_prompt_profile: str = Field(default="v2", alias="AI_PROMPT_PROFILE")

    # Payment accounts
    payment_kpay_number: str = ""
    payment_kpay_name: str = ""
    payment_wave_number: str = ""
    payment_wave_name: str = ""

    # Worker Heartbeat (Local Laptop GPU)
    worker_laptop_heartbeat: str = ""
    worker_laptop_engine: str = "all"
    worker_laptop_status: str = "OFFLINE"
    worker_laptop_host: str = ""

    # Admin Watermark Controls for Recap
    watermark_free_enabled: bool = Field(default=True, alias="WATERMARK_FREE_ENABLED")
    watermark_paid_enabled: bool = Field(default=False, alias="WATERMARK_PAID_ENABLED")
    watermark_style: str = Field(default="tiled", alias="WATERMARK_STYLE")
    watermark_position: str = Field(default="bottom-right", alias="WATERMARK_POSITION")
    watermark_opacity: int = Field(default=20, alias="WATERMARK_OPACITY")
    watermark_size: int = Field(default=16, alias="WATERMARK_SIZE")


    # Monetization & Cost Monitoring Benchmarks (all optional; actual vs estimated shown in Admin Analytics)
    usd_to_mmk_rate: float = Field(default=4500.0, alias="USD_TO_MMK_RATE")
    gemini_input_cost_per_1m_usd: float = Field(default=0.15, alias="GEMINI_INPUT_COST_PER_1M_USD")
    gemini_output_cost_per_1m_usd: float = Field(default=0.60, alias="GEMINI_OUTPUT_COST_PER_1M_USD")
    gemini_tts_cost_per_min_usd: float = Field(default=0.015, alias="GEMINI_TTS_COST_PER_MIN_USD")
    gpu_provider_mode: str = Field(default="hf_spaces", alias="GPU_PROVIDER_MODE")  # hf_spaces | runpod | local_vps
    gpu_hourly_cost_usd: float = Field(default=0.0, alias="GPU_HOURLY_COST_USD")
    runpod_gpu_cost_per_hour_usd: float = Field(default=0.39, alias="RUNPOD_GPU_COST_PER_HOUR_USD")
    storage_cost_per_gb_month_usd: float = Field(default=0.15, alias="STORAGE_COST_PER_GB_MONTH_USD")
    bandwidth_cost_per_gb_usd: float = Field(default=0.09, alias="BANDWIDTH_COST_PER_GB_USD")

    @property
    def admin_ids(self) -> set[int]:
        return _parse_ids(self.admin_ids_raw)

    def is_admin(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return int(user_id) in self.admin_ids


settings = Settings()

GROQ_WHISPER_MODEL_DEFAULT = "whisper-large-v3-turbo"

WATERMARK_ALLOWED_STYLES = ("tiled", "logo", "text")
WATERMARK_ALLOWED_POSITIONS = ("bottom-right", "bottom-left", "top-right", "top-left", "centre")


def get_watermark_config(is_vip: bool = False) -> dict[str, str | int | bool]:
    """Return active server-side watermark settings for a user's tier (read at render/preview time)."""
    free_val = getattr(settings, "watermark_free_enabled", True)
    free_on = bool(free_val) if free_val is not None else True
    paid_val = getattr(settings, "watermark_paid_enabled", False)
    paid_on = bool(paid_val) if paid_val is not None else False
    enabled = paid_on if is_vip else free_on

    raw_style = str(getattr(settings, "watermark_style", "tiled") or "tiled").strip().lower()
    style = raw_style if raw_style in WATERMARK_ALLOWED_STYLES else "tiled"

    raw_pos = str(getattr(settings, "watermark_position", "bottom-right") or "bottom-right").strip().lower()
    if raw_pos == "center":
        raw_pos = "centre"
    pos = raw_pos if raw_pos in WATERMARK_ALLOWED_POSITIONS else "bottom-right"

    try:
        opacity_pct = max(20, min(100, int(getattr(settings, "watermark_opacity", 20) or 20)))
    except (TypeError, ValueError):
        opacity_pct = 20

    try:
        size_pct = max(5, min(40, int(getattr(settings, "watermark_size", 16) or 16)))
    except (TypeError, ValueError):
        size_pct = 16

    return {
        "watermark": enabled,
        "watermark_free_enabled": free_on,
        "watermark_paid_enabled": paid_on,
        "watermark_style": style,
        "watermark_position": pos,
        "watermark_opacity": opacity_pct,
        "watermark_size": size_pct,
    }


def get_groq_whisper_model() -> str:
    """Effective Groq Whisper model: dynamic setting > env > default.

    Batch BS: both transcription paths used to read the
    GROQ_WHISPER_MODEL env directly, so the registered dynamic
    setting of the same name — writable from the Admin Settings
    screen since BS — could never take effect. The in-memory
    ``settings.groq_whisper_model`` attribute already folds the
    first two layers in the right order: pydantic initialises it
    from the env (alias above), and the dynamic-settings overlay
    (load_dynamic_settings / set_dynamic_setting) replaces it
    whenever an admin-saved BotSetting row exists. A non-empty
    attribute is therefore the dynamic value when one is set and
    the env value otherwise; the raw env is re-checked only for
    callers whose Settings object never saw it, and the historical
    default closes the chain. When nothing is set anywhere the
    result is byte-identical to the pre-BS env-only reads.
    """
    configured = str(getattr(settings, "groq_whisper_model", "") or "").strip()
    if configured:
        return configured
    env_value = os.getenv("GROQ_WHISPER_MODEL", "").strip()
    if env_value:
        return env_value
    return GROQ_WHISPER_MODEL_DEFAULT

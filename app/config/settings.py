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
    # Connection pool sizing (audit fix 2026-11-03): SQLAlchemy's
    # defaults (pool_size=5, max_overflow=10) exhausted in production
    # under Mini App polling load — QueuePool timeouts surfaced as
    # 30-second hangs and 500s on status endpoints. Sized for a single
    # app instance; raise via env if the deployment shape changes.
    db_pool_size: int = Field(default=10, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=20, alias="DB_MAX_OVERFLOW")
    db_pool_timeout: int = Field(default=30, alias="DB_POOL_TIMEOUT")
    db_pool_recycle: int = Field(default=1800, alias="DB_POOL_RECYCLE")

    # Project and storage paths
    root: Path = ROOT
    storage_root: Path = ROOT / "storage"
    locales_root: Path = ROOT / "locales"
    data_root: Path = ROOT / "data"

    # User Account Policies
    free_monthly_allowance_seconds: int = Field(default=180, alias="FREE_MONTHLY_ALLOWANCE_SECONDS")  # 3 minutes
    welcome_bonus_seconds: int = Field(default=0, alias="WELCOME_BONUS_SECONDS")  # Retired in V2 (default 0)
    character_limit_free: int = Field(default=1000, alias="CHARACTER_LIMIT_FREE")  # Free tier: 1000 chars/generation
    character_limit_purchased: int = Field(default=5000, alias="CHARACTER_LIMIT_PURCHASED")  # VIP: 5000 chars/generation
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
    voice_daily_limit_minutes_paid: int = Field(default=30, alias="VOICE_DAILY_LIMIT_MINUTES_PAID")  # 0 = unlimited
    # Batch AW: per-user DAILY TTS-tab minutes cap, split by tier like
    # Batch AN but on its own allowance — the TTS tab never deducts
    # voice minutes or recap credits; referral seconds are spent
    # before this cap is touched. Enforced in create_tts_job for
    # FREE + PURCHASED; admins exempt; 0 = unlimited for that tier.
    tts_daily_limit_minutes_free: int = Field(default=15, alias="TTS_DAILY_LIMIT_MINUTES_FREE")  # 0 = unlimited
    tts_daily_limit_minutes_paid: int = Field(default=60, alias="TTS_DAILY_LIMIT_MINUTES_PAID")  # 0 = unlimited
    # Batch CG: the FREE Gemini TTS "taste" — how much Gemini audio
    # per day the SERVICE POOL funds for one FREE user before the
    # route steps to their own key (if saved) or Edge. Delivered
    # seconds, UTC day; 0 = no pool taste for FREE at all. PURCHASED
    # pool use is NOT taste-governed (their 60-min cap above rules);
    # admins exempt. A user's own key is never limited by this.
    free_gemini_taste_minutes: int = Field(default=2, alias="FREE_GEMINI_TASTE_MINUTES")
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
        default=5000,
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

    # Gemini TTS Dynamic Model Chain (effective order: primary -> fallback_1 -> lite -> fallback_2)
    gemini_tts_primary_model: str = Field(default="gemini-3.8-flash-tts", alias="GEMINI_TTS_PRIMARY_MODEL")
    gemini_tts_fallback_1: str = Field(default="gemini-3.1-flash-tts-preview", alias="GEMINI_TTS_FALLBACK_1")
    gemini_tts_lite_model: str = Field(default="gemini-3.8-flash-lite-tts", alias="GEMINI_TTS_LITE_MODEL")
    gemini_tts_fallback_2: str = Field(default="gemini-2.5-flash-preview-tts", alias="GEMINI_TTS_FALLBACK_2")

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


    @property
    def admin_ids(self) -> set[int]:
        return _parse_ids(self.admin_ids_raw)

    def is_admin(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return int(user_id) in self.admin_ids


settings = Settings()

GROQ_WHISPER_MODEL_DEFAULT = "whisper-large-v3-turbo"


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

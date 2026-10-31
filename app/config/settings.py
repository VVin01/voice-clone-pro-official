from __future__ import annotations

import logging
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
    # Env-only and rotatable. When empty, callers fall back to the bot
    # token with a warning so existing deployments do not break at once.
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
    legacy_database_url: str = Field(
        default="sqlite+aiosqlite:///./data/voice_clone_official.db",
        alias="LEGACY_DATABASE_URL",
    )
    expected_db_identity: str = Field(
        default="voice_clone_v2",
        alias="EXPECTED_DB_IDENTITY",
    )

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
    free_daily_job_limit: int = Field(default=5, alias="FREE_DAILY_JOB_LIMIT")
    purchased_daily_job_limit: int = Field(default=0, alias="PURCHASED_DAILY_JOB_LIMIT")  # 0 = unlimited
    max_concurrent_jobs_per_user: int = Field(default=1, alias="MAX_CONCURRENT_JOBS_PER_USER")
    maintenance_mode: bool = Field(default=False, alias="MAINTENANCE_MODE")

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
    # window. User-facing copy states this same number — keep them in sync.
    media_retention_days: int = Field(default=7, alias="MEDIA_RETENTION_DAYS")

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

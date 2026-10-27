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
    admin_ids_raw: str = Field(default="", alias="ADMIN_IDS")

    # Membership Gate (Channel and Group)
    required_channel_id: str = Field(default="", alias="REQUIRED_CHANNEL_ID")
    required_group_id: str = Field(default="", alias="REQUIRED_GROUP_ID")
    channel_invite_link: str = Field(default="https://t.me/voiceclonepro", alias="CHANNEL_INVITE_LINK")
    group_invite_link: str = Field(default="https://t.me/voiceclonepro_chat", alias="GROUP_INVITE_LINK")

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

    # Infrastructure Safety Limit (independent of user account tiers, protects Edge TTS & Applio memory)
    infrastructure_safety_character_limit: int = Field(
        default=5000,
        alias="INFRASTRUCTURE_SAFETY_CHARACTER_LIMIT",
    )

    # Applio Runtime Configuration
    applio_workdir: str = Field(default=r"C:\Applio", alias="APPLIO_WORKDIR")
    applio_python: str = Field(default="", alias="APPLIO_PYTHON")
    applio_runner: str = Field(default="", alias="APPLIO_RUNNER")
    applio_command: str = Field(default="", alias="APPLIO_COMMAND")
    applio_inference_timeout_seconds: int = Field(
        default=900,
        alias="APPLIO_INFERENCE_TIMEOUT_SECONDS",
    )

    # Stale Job Crash Recovery (seconds)
    stale_job_timeout_seconds: int = Field(default=1800, alias="STALE_JOB_TIMEOUT_SECONDS")

    # Tutorial Links & Controls (Voice Clone & Movie Recap)
    tutorial_video_link: str = Field(
        default="https://t.me/VoiceClonePro/16",
        alias="TUTORIAL_VIDEO_LINK",
    )
    tutorial_video_enabled: bool = Field(default=True, alias="TUTORIAL_VIDEO_ENABLED")

    tutorial_voice_title: str = Field(
        default="🎙️ Voice Clone ပြုလုပ်နည်း လမ်းညွှန်",
        alias="TUTORIAL_VOICE_TITLE",
    )
    tutorial_voice_link: str = Field(
        default="https://t.me/VoiceClonePro/16",
        alias="TUTORIAL_VOICE_LINK",
    )
    tutorial_voice_enabled: bool = Field(default=True, alias="TUTORIAL_VOICE_ENABLED")

    tutorial_recap_title: str = Field(
        default="🎬 Movie Recap ပြုလုပ်နည်း လမ်းညွှန်",
        alias="TUTORIAL_RECAP_TITLE",
    )
    tutorial_recap_link: str = Field(
        default="https://t.me/VoiceClonePro/16",
        alias="TUTORIAL_RECAP_LINK",
    )
    tutorial_recap_enabled: bool = Field(default=True, alias="TUTORIAL_RECAP_ENABLED")

    # Edge TTS defaults
    default_tts_voice: str = "my-MM-ThihaNeural"

    # VoxCPM 2 Neural TTS & Voice Cloning
    voxcpm_space_url: str = Field(default="", alias="VOXCPM_SPACE_URL")
    voxcpm_spaces: str = Field(default="", alias="VOXCPM_SPACES")
    hf_token: str = Field(default="", alias="HF_TOKEN")
    hf_tokens: str = Field(default="", alias="HF_TOKENS")

    # Recap Studio Policies
    recap_max_duration_seconds: int = Field(default=600, alias="RECAP_MAX_DURATION_SECONDS")  # Max 10 minutes per recap credit

    # Gemini AI Dynamic Model Chain (Active 3.x models; 2.5/2.0/1.5 are retired on Google API)
    gemini_primary_model: str = Field(default="gemini-3.6-flash", alias="GEMINI_PRIMARY_MODEL")
    gemini_fallback_1: str = Field(default="gemini-3.5-flash", alias="GEMINI_FALLBACK_1")
    gemini_fallback_2: str = Field(default="gemini-3.5-flash-lite", alias="GEMINI_FALLBACK_2")
    gemini_fallback_3: str = Field(default="gemini-flash-lite-latest", alias="GEMINI_FALLBACK_3")

    # Payment accounts
    payment_kpay_number: str = "09790760003"
    payment_kpay_name: str = "Win Kyaw Oo"
    payment_wave_number: str = "09790760003"
    payment_wave_name: str = "Win Kyaw Oo"

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

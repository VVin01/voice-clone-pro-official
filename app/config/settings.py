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
    character_limit_free: int = Field(default=500, alias="CHARACTER_LIMIT_FREE")
    character_limit_purchased: int = Field(default=2000, alias="CHARACTER_LIMIT_PURCHASED")
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

    # Tutorial Link
    tutorial_video_link: str = Field(
        default="https://t.me/voiceclonepro/tutorial",
        alias="TUTORIAL_VIDEO_LINK",
    )
    tutorial_video_enabled: bool = Field(default=True, alias="TUTORIAL_VIDEO_ENABLED")

    # Edge TTS defaults
    default_tts_voice: str = "my-MM-ThihaNeural"

    # Payment accounts
    payment_kpay_number: str = "09790760003"
    payment_kpay_name: str = "Win Kyaw Oo"
    payment_wave_number: str = "09790760003"
    payment_wave_name: str = "Win Kyaw Oo"


    @property
    def admin_ids(self) -> set[int]:
        return _parse_ids(self.admin_ids_raw)

    def is_admin(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return int(user_id) in self.admin_ids


settings = Settings()

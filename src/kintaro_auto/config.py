from __future__ import annotations

import os
import tomllib
from pathlib import Path
from pydantic import BaseModel, Field


class ApiSettings(BaseModel):
    base_url: str = "https://service.kintaroapi.com"
    api_key: str = ""
    timeout_seconds: float = 120.0
    max_retries: int = 3
    retry_backoff_seconds: float = 2.5
    sync_totp_window: bool = True
    totp_min_remaining_seconds: float = 24.0
    save_raw_responses: bool = True
    raw_logs_dir: str = "state/raw_logs"


class WarmupSettings(BaseModel):
    enabled: bool = True
    min_session_age_minutes_before_first_post: float = 30.0
    min_hours_between_posts_per_account: float = 3.0
    max_posts_per_account_per_day: int = 3
    feed_rounds_min: int = 2
    feed_rounds_max: int = 4
    views_per_round_min: int = 3
    views_per_round_max: int = 7
    playtime_ms_min: int = 4200
    playtime_ms_max: int = 19500
    like_probability: float = Field(default=0.12, ge=0.0, le=1.0)
    read_comments_probability: float = Field(default=0.18, ge=0.0, le=1.0)
    follow_probability: float = Field(default=0.03, ge=0.0, le=1.0)
    action_delay_seconds_min: float = 2.0
    action_delay_seconds_max: float = 6.5
    pre_upload_pause_seconds_min: float = 5.0
    pre_upload_pause_seconds_max: float = 15.0


class UniquifierSettings(BaseModel):
    enabled: bool = True
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    temp_dir: str = "state/tmp_media"
    # Imperceptible micro-transforms to break MD5/perceptual hash collisions across accounts
    crop_ratio_min: float = 0.985
    crop_ratio_max: float = 0.996
    brightness_shift_max: float = 0.018
    contrast_shift_max: float = 0.025
    saturation_shift_max: float = 0.03
    speed_jitter_max: float = 0.015
    video_crf_min: int = 19
    video_crf_max: int = 22
    photo_jpeg_quality_min: int = 90
    photo_jpeg_quality_max: int = 96


class ResponseMappingSettings(BaseModel):
    """
    OpenAPI spec `kintaro-api.json` declares all 200/403 schemas as untyped `object`.
    Keep candidate key paths configurable in one place instead of hardcoding assumptions.
    """

    device_keys: list[str] = Field(
        default_factory=lambda: ["device", "x_device", "x-device", "data.device"]
    )
    verify_keys: list[str] = Field(
        default_factory=lambda: ["verify", "data.verify", "challenge"]
    )
    feed_list_keys: list[str] = Field(
        default_factory=lambda: [
            "aweme_list",
            "data.aweme_list",
            "items",
            "data.items",
            "posts",
            "data.posts",
        ]
    )
    aweme_id_keys: list[str] = Field(
        default_factory=lambda: [
            "aweme_id",
            "data.aweme_id",
            "aweme.aweme_id",
            "data.aweme.aweme_id",
            "post.aweme_id",
            "item_id",
        ]
    )
    play_count_keys: list[str] = Field(
        default_factory=lambda: [
            "statistics.play_count",
            "stats.playCount",
            "play_count",
            "views",
        ]
    )


class AppConfig(BaseModel):
    state_dir: str = "state"
    max_parallel_accounts: int = 2
    api: ApiSettings = Field(default_factory=ApiSettings)
    warmup: WarmupSettings = Field(default_factory=WarmupSettings)
    uniquifier: UniquifierSettings = Field(default_factory=UniquifierSettings)
    response_mapping: ResponseMappingSettings = Field(
        default_factory=ResponseMappingSettings
    )

    def resolve_api_key(self) -> str:
        key = (os.getenv("KINTARO_API_KEY") or self.api.api_key).strip()
        if not key:
            raise RuntimeError(
                "Не задан API-ключ Kintaro (`x-api-key`). "
                "Укажи `api.api_key` в config.toml или переменную окружения KINTARO_API_KEY."
            )
        return key


def load_config(path: Path | None = None) -> AppConfig:
    if path is None:
        default_path = Path("config.toml")
        if default_path.exists():
            path = default_path
        else:
            return AppConfig()

    if not path.exists():
        raise FileNotFoundError(f"Конфиг не найден: {path}")

    with path.open("rb") as f:
        raw = tomllib.load(f)
    return AppConfig.model_validate(raw)

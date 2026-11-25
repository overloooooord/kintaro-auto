from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse
from pydantic import BaseModel, Field, field_validator

DEVICE_GEO_PROFILES: dict[str, dict[str, Any]] = {
    "US": {
        "locale": "en_US",
        "language": "en",
        "timezone": -5,
        "region": "US",
        "tz_name": "America/New_York",
        "tz_offset": -18000,
        "sim_region": "us",
        "mcc": "310",
        "mnc": "150",
        "mcc_region": "US",
        "carrier": "AT&T Wireless Inc.",
    },
    "DE": {
        "locale": "de_DE",
        "language": "de",
        "timezone": 1,
        "region": "DE",
        "tz_name": "Europe/Berlin",
        "tz_offset": 3600,
        "sim_region": "de",
        "mcc": "262",
        "mnc": "01",
        "mcc_region": "DE",
        "carrier": "Telekom.de",
    },
    "GB": {
        "locale": "en_GB",
        "language": "en",
        "timezone": 0,
        "region": "GB",
        "tz_name": "Europe/London",
        "tz_offset": 0,
        "sim_region": "gb",
        "mcc": "234",
        "mnc": "15",
        "mcc_region": "GB",
        "carrier": "Vodafone UK",
    },
    "VN": {
        "locale": "vi_VN",
        "language": "vi",
        "timezone": 7,
        "region": "VN",
        "tz_name": "Asia/Ho_Chi_Minh",
        "tz_offset": 25200,
        "sim_region": "vn",
        "mcc": "452",
        "mnc": "04",
        "mcc_region": "VN",
        "carrier": "Viettel Mobile",
    },
}


def apply_device_geo_profile(device: dict[str, Any] | str, target_region: str) -> dict[str, Any]:
    dev_dict: dict[str, Any] = (
        dict(json.loads(device)) if isinstance(device, str) else dict(device)
    )
    reg = target_region.strip().upper()
    if reg in DEVICE_GEO_PROFILES:
        dev_dict.update(DEVICE_GEO_PROFILES[reg])
    elif reg:
        dev_dict["region"] = reg
        dev_dict["sim_region"] = reg.lower()
        dev_dict["mcc_region"] = reg
    return dev_dict


def swap_proxy_country(proxy: str, target_country: str) -> str:
    cc = target_country.strip().upper()
    if not proxy or not cc:
        return proxy
    for pat, repl in (
        (r"([_-]country[_-])([A-Za-z]{2})\b", rf"\g<1>{cc}"),
        (r"(\bcountry[_-])([A-Za-z]{2})\b", rf"\g<1>{cc}"),
        (r"([_-]cc[_-])([A-Za-z]{2})\b", rf"\g<1>{cc}"),
    ):
        updated, count = re.subn(pat, repl, proxy, flags=re.IGNORECASE)
        if count > 0:
            return updated
    return proxy


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


AuthMethod = Literal["device_only", "username", "email", "mobile"]
MediaType = Literal["video", "photo"]


class UploadedPostRecord(BaseModel):
    aweme_id: str | None = None
    uploaded_at: str = Field(default_factory=utc_now_iso)
    media_type: MediaType
    source_files: list[str]
    description: str
    last_checked_at: str | None = None
    last_play_count: int | None = None
    suspected_shadowban: bool = False
    diagnostic_note: str = ""


class AccountRecord(BaseModel):
    account_id: str
    proxy: str
    region: str
    auth_region: str | None = None
    auth_method: AuthMethod = "username"
    username: str = ""
    email: str = ""
    mobile: str = ""
    password: str = ""
    totp_token: str = ""
    mail_login: str = ""
    mail_password: str = ""
    recovery_email: str = ""
    recovery_password: str = ""
    mail_totp: str = ""
    use_outlook_hub: bool = False
    device: dict[str, Any] | str | None = None

    authenticated: bool = False
    first_authenticated_at: str | None = None
    login_cooldown_until: str | None = None
    last_hosts_update_at: str | None = None
    last_token_beat_at: str | None = None
    last_warmup_at: str | None = None
    pending_verify: dict[str, Any] | None = None
    upload_history: list[UploadedPostRecord] = Field(default_factory=list)

    @field_validator("proxy")
    @classmethod
    def validate_proxy(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError(
                "Поле `proxy` не задано. Для защиты от теневого бана каждый аккаунт обязан иметь свой постоянный прокси `protocol://user:pass@host:port`."
            )
        parsed = urlparse(v)
        if not parsed.scheme or not parsed.hostname or not parsed.port:
            raise ValueError(
                f"Некорректный формат `x-proxy`: {v!r}. Ожидается `protocol://user:pass@host:port` (например `http://...` или `socks5://...`)."
            )
        return v

    @field_validator("region")
    @classmethod
    def validate_region(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 2 or not v.isalpha():
            raise ValueError(
                f"Некорректный `region`: {v!r}. Укажи двухбуквенный ISO-код страны прокси (например `US`, `DE`, `BR`)."
            )
        return v

    def proxy_for_country(self, country_code: str) -> str:
        # Keep the user's proxy string (e.g. clean `_country-VN` pool) untouched!
        # TikTok post region (`aweme.region = "US"`) is controlled by `apply_device_geo_profile` + `region`.
        _ = country_code
        return self.proxy

    def device_header_value(self) -> str:
        if self.device is None:
            raise RuntimeError(
                f"[{self.account_id}] Отсутствует `x-device`. Сначала выполни регистрацию устройства / вход (`kintaro-auto auth`)."
            )
        if isinstance(self.device, str):
            stripped = self.device.strip()
            if not stripped:
                raise RuntimeError(f"[{self.account_id}] Пустой `x-device`.")
            parsed = json.loads(stripped)
            return json.dumps(parsed, separators=(",", ":"), ensure_ascii=True)
        return json.dumps(self.device, separators=(",", ":"), ensure_ascii=True)


class UploadTask(BaseModel):
    task_id: str
    account_id: str | None = None
    media_type: MediaType = "video"
    video_path: str | None = None
    photo_paths: list[str] = Field(default_factory=list)
    description: str = ""
    mentions: list[str] = Field(default_factory=list)
    music_id: str = ""
    music_search_keyword: str | None = None
    is_private: bool = False
    disable_comments: bool = False
    disable_duet: bool = False
    disable_react: bool = False
    disable_stitch: bool = False
    is_story: bool = False
    completed: bool = False
    completed_at: str | None = None
    assigned_account_id: str | None = None
    published_aweme_id: str | None = None


class PostHealthReport(BaseModel):
    account_id: str
    aweme_id: str
    age_minutes: float
    play_count: int | None
    found_in_self_posts: bool
    found_in_public_get: bool
    suspected_shadowban: bool
    verdict: str

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse
from pydantic import BaseModel, Field, field_validator

GEO_LOCALE_DEFAULTS: dict[str, str] = {
    "US": "en_US",
    "GB": "en_GB",
    "CA": "en_CA",
    "AU": "en_AU",
    "IT": "it_IT",
    "DE": "de_DE",
    "FR": "fr_FR",
    "ES": "es_ES",
    "BR": "pt_BR",
    "PT": "pt_PT",
    "MX": "es_MX",
    "CL": "es_CL",
    "AR": "es_AR",
    "CO": "es_CO",
    "PE": "es_PE",
    "PL": "pl_PL",
    "KZ": "ru_KZ",
    "UA": "uk_UA",
    "PH": "en_PH",
    "ID": "id_ID",
    "VN": "vi_VN",
}

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
    "CA": {
        "locale": "en_CA",
        "language": "en",
        "timezone": -5,
        "region": "CA",
        "tz_name": "America/Toronto",
        "tz_offset": -18000,
        "sim_region": "ca",
        "mcc": "302",
        "mnc": "720",
        "mcc_region": "CA",
        "carrier": "Rogers Wireless",
    },
    "AU": {
        "locale": "en_AU",
        "language": "en",
        "timezone": 10,
        "region": "AU",
        "tz_name": "Australia/Sydney",
        "tz_offset": 36000,
        "sim_region": "au",
        "mcc": "505",
        "mnc": "01",
        "mcc_region": "AU",
        "carrier": "Telstra",
    },
    "IT": {
        "locale": "it_IT",
        "language": "it",
        "timezone": 1,
        "region": "IT",
        "tz_name": "Europe/Rome",
        "tz_offset": 3600,
        "sim_region": "it",
        "mcc": "222",
        "mnc": "01",
        "mcc_region": "IT",
        "carrier": "TIM",
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
    "FR": {
        "locale": "fr_FR",
        "language": "fr",
        "timezone": 1,
        "region": "FR",
        "tz_name": "Europe/Paris",
        "tz_offset": 3600,
        "sim_region": "fr",
        "mcc": "208",
        "mnc": "01",
        "mcc_region": "FR",
        "carrier": "Orange F",
    },
    "ES": {
        "locale": "es_ES",
        "language": "es",
        "timezone": 1,
        "region": "ES",
        "tz_name": "Europe/Madrid",
        "tz_offset": 3600,
        "sim_region": "es",
        "mcc": "214",
        "mnc": "07",
        "mcc_region": "ES",
        "carrier": "Movistar",
    },
    "BR": {
        "locale": "pt_BR",
        "language": "pt",
        "timezone": -3,
        "region": "BR",
        "tz_name": "America/Sao_Paulo",
        "tz_offset": -10800,
        "sim_region": "br",
        "mcc": "724",
        "mnc": "06",
        "mcc_region": "BR",
        "carrier": "Vivo",
    },
    "PT": {
        "locale": "pt_PT",
        "language": "pt",
        "timezone": 0,
        "region": "PT",
        "tz_name": "Europe/Lisbon",
        "tz_offset": 0,
        "sim_region": "pt",
        "mcc": "268",
        "mnc": "06",
        "mcc_region": "PT",
        "carrier": "MEO",
    },
    "MX": {
        "locale": "es_MX",
        "language": "es",
        "timezone": -6,
        "region": "MX",
        "tz_name": "America/Mexico_City",
        "tz_offset": -21600,
        "sim_region": "mx",
        "mcc": "334",
        "mnc": "020",
        "mcc_region": "MX",
        "carrier": "Telcel",
    },
    "CL": {
        "locale": "es_CL",
        "language": "es",
        "timezone": -4,
        "region": "CL",
        "tz_name": "America/Santiago",
        "tz_offset": -14400,
        "sim_region": "cl",
        "mcc": "730",
        "mnc": "01",
        "mcc_region": "CL",
        "carrier": "Entel PCS",
    },
    "AR": {
        "locale": "es_AR",
        "language": "es",
        "timezone": -3,
        "region": "AR",
        "tz_name": "America/Argentina/Buenos_Aires",
        "tz_offset": -10800,
        "sim_region": "ar",
        "mcc": "722",
        "mnc": "310",
        "mcc_region": "AR",
        "carrier": "Claro AR",
    },
    "CO": {
        "locale": "es_CO",
        "language": "es",
        "timezone": -5,
        "region": "CO",
        "tz_name": "America/Bogota",
        "tz_offset": -18000,
        "sim_region": "co",
        "mcc": "732",
        "mnc": "101",
        "mcc_region": "CO",
        "carrier": "Claro CO",
    },
    "PE": {
        "locale": "es_PE",
        "language": "es",
        "timezone": -5,
        "region": "PE",
        "tz_name": "America/Lima",
        "tz_offset": -18000,
        "sim_region": "pe",
        "mcc": "716",
        "mnc": "10",
        "mcc_region": "PE",
        "carrier": "Claro PE",
    },
    "PL": {
        "locale": "pl_PL",
        "language": "pl",
        "timezone": 1,
        "region": "PL",
        "tz_name": "Europe/Warsaw",
        "tz_offset": 3600,
        "sim_region": "pl",
        "mcc": "260",
        "mnc": "03",
        "mcc_region": "PL",
        "carrier": "Orange PL",
    },
    "KZ": {
        "locale": "ru_KZ",
        "language": "ru",
        "timezone": 5,
        "region": "KZ",
        "tz_name": "Asia/Almaty",
        "tz_offset": 18000,
        "sim_region": "kz",
        "mcc": "401",
        "mnc": "02",
        "mcc_region": "KZ",
        "carrier": "Kcell",
    },
    "UA": {
        "locale": "uk_UA",
        "language": "uk",
        "timezone": 2,
        "region": "UA",
        "tz_name": "Europe/Kyiv",
        "tz_offset": 7200,
        "sim_region": "ua",
        "mcc": "255",
        "mnc": "03",
        "mcc_region": "UA",
        "carrier": "Kyivstar",
    },
    "PH": {
        "locale": "en_PH",
        "language": "en",
        "timezone": 8,
        "region": "PH",
        "tz_name": "Asia/Manila",
        "tz_offset": 28800,
        "sim_region": "ph",
        "mcc": "515",
        "mnc": "02",
        "mcc_region": "PH",
        "carrier": "Globe",
    },
    "ID": {
        "locale": "id_ID",
        "language": "id",
        "timezone": 7,
        "region": "ID",
        "tz_name": "Asia/Jakarta",
        "tz_offset": 25200,
        "sim_region": "id",
        "mcc": "510",
        "mnc": "10",
        "mcc_region": "ID",
        "carrier": "Telkomsel",
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


def apply_device_geo_profile(
    device: dict[str, Any] | str,
    target_region: str = "",
    app_locale: str | None = None,
) -> dict[str, Any]:
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
    if app_locale and app_locale.strip():
        loc = app_locale.strip().replace("-", "_")
        lang = loc.split("_", 1)[0].lower()
        if "_" not in loc:
            loc = f"{lang}_{reg or 'US'}"
        else:
            p1, p2 = loc.split("_", 1)
            loc = f"{p1.lower()}_{p2.upper()}"
        dev_dict["language"] = lang
        dev_dict["locale"] = loc
    return dev_dict


def swap_proxy_country(proxy: str, target_country: str) -> str:
    cc = target_country.strip().upper()
    if not proxy or not cc:
        return proxy
    for pat, repl in (
        (r"([_-]country[_-])([A-Za-z]{2})(?=$|[^A-Za-z])", rf"\g<1>{cc}"),
        (r"(\bcountry[_-])([A-Za-z]{2})(?=$|[^A-Za-z])", rf"\g<1>{cc}"),
        (r"([_-]cc[_-])([A-Za-z]{2})(?=$|[^A-Za-z])", rf"\g<1>{cc}"),
    ):
        updated, count = re.subn(pat, repl, proxy, flags=re.IGNORECASE)
        if count > 0:
            return updated
    return proxy


def allocate_sticky_proxy(
    proxy: str,
    target_country: str = "",
    lifetime: str = "60m",
    preserve_existing_session: bool = True,
) -> str:
    """
    Pins an 8-char sticky residential session (`_session-xxxxxxxx_lifetime-60m`) on `bpproxy.at`
    so login, uploads, restarts, and 3-stage Auto-Appeal share one constant IP.
    Never changes `_country-XX` unless `target_country` is explicitly passed.
    """
    if not proxy or "bpproxy." not in proxy.lower():
        return swap_proxy_country(proxy, target_country) if target_country else proxy

    if preserve_existing_session and re.search(r"_session-[a-zA-Z0-9]+", proxy):
        m_cc = re.search(r"[_-]country[_-]([A-Za-z]{2})(?=$|[^A-Za-z])", proxy, flags=re.IGNORECASE)
        cur_cc = m_cc.group(1).upper() if m_cc else ""
        req_cc = target_country.strip().upper()
        if not req_cc or cur_cc == req_cc:
            return proxy

    px = swap_proxy_country(proxy, target_country) if target_country else proxy
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    sid = "".join(secrets.choice(alphabet) for _ in range(8))
    px_clean = re.sub(r"_session-[a-zA-Z0-9]+(?:_lifetime-[a-zA-Z0-9]+)?", "", px)
    if re.search(r"_country-[A-Za-z]{2}", px_clean):
        return re.sub(
            r"(_country-[A-Za-z]{2})",
            rf"\1_session-{sid}_lifetime-{lifetime}",
            px_clean,
            count=1,
        )
    return px_clean


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
    imap_host: str = ""
    app_locale: str = ""
    raw_line: str = ""
    device: dict[str, Any] | str | None = None

    authenticated: bool = False
    first_authenticated_at: str | None = None
    login_cooldown_until: str | None = None
    last_hosts_update_at: str | None = None
    last_token_beat_at: str | None = None
    last_warmup_at: str | None = None
    last_restarted_at: str | None = None
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
    randomize_tags: bool = False
    photo_mode: str | None = None
    region_override: str | None = None
    completed: bool = False
    completed_at: str | None = None
    assigned_account_id: str | None = None
    published_aweme_id: str | None = None


class CampaignConfig(BaseModel):
    """Configuration for a multi-threaded anti-shadowban campaign (`swegby` + `kintaro-auto` merged workflow)."""

    account_ids: list[str] = Field(default_factory=list)
    media_type: MediaType = "photo"
    media_paths: list[str] = Field(default_factory=list)
    description: str = ""
    mentions: list[str] = Field(default_factory=list)
    randomize_tags: bool = True
    copies_count: int = Field(default=6, ge=1, le=30)
    threads: int = Field(default=5, ge=1, le=20)
    delay_min_sec: float = Field(default=14.0, ge=0.0)
    delay_max_sec: float = Field(default=20.0, ge=0.0)
    restart_every_n: int = Field(default=3, ge=0, le=20)
    region: str = ""
    phone_region: str = ""
    app_locale: str = ""
    pin_sticky_proxy: bool = False
    override_proxy_country: str = ""
    verify_proxy_geo: bool = False
    delete_old_posts: bool = True
    warmup_before_upload: bool = False
    photo_mode: str = "minimal_snow"
    uniquifier_overrides: dict[str, Any] = Field(default_factory=dict)
    music_id: str = ""
    auto_music_query: str = "Fortnite"
    is_private: bool = False
    disable_comments: bool = False
    is_story: bool = False
    auto_appeal_2048: bool = True
    appeal_pre_upload: bool = True
    appeal_inter_post: bool = True
    appeal_post_upload: bool = True
    appeal_post_wait_sec: float = Field(default=8.0, ge=0.0, le=180.0)
    appeal_quiet_window_sec: float = Field(default=60.0, ge=0.0, le=300.0)
    appeal_max_passes: int = Field(default=8, ge=1, le=20)
    appeal_text: str = (
        "{Our content is original, authentic, and strictly follows all TikTok Community Guidelines.|"
        "Original content created and edited exclusively by our team.}"
    )
    sort_to_folders: bool = True
    max_upload_retries: int = Field(default=4, ge=1, le=6)


class CampaignAccountResult(BaseModel):
    account_id: str
    username: str = ""
    status: str = "pending"
    posts_published: int = 0
    target_posts: int = 6
    deleted_old_posts: int = 0
    appeals_sent: int = 0
    appeal_verdict: str = ""
    folder_key: str = ""
    initial_views: int = 0
    aweme_ids: list[str] = Field(default_factory=list)
    post_urls: list[str] = Field(default_factory=list)
    error: str = ""
    elapsed_sec: float = 0.0


class PostHealthReport(BaseModel):
    account_id: str
    aweme_id: str
    age_minutes: float
    play_count: int | None
    found_in_self_posts: bool
    found_in_public_get: bool
    suspected_shadowban: bool
    verdict: str

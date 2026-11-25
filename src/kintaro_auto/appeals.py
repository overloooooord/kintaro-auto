from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit

import httpx
from playwright.async_api import BrowserContext, Page, async_playwright

from kintaro_auto.models import AccountRecord

logger = logging.getLogger("kintaro_auto")

# ── Regional Mobile IDC Gateways & Moderation Enums (merged from 233) ────────

IDC_GATEWAY_MAP: dict[str, str] = {
    "no1a": "https://api16-normal-no1a.tiktokv.eu",
    "useast2b": "https://api16-normal-no1a.tiktokv.eu",
    "us-eastred": "https://api16-normal-no1a.tiktokv.eu",
    "useastred": "https://api16-normal-no1a.tiktokv.eu",
    "euttp": "https://api16-normal-no1a.tiktokv.eu",
    "useast1a": "https://api16-normal-c-useast1a.tiktokv.com",
    "useast2a": "https://api16-normal-c-useast2a.tiktokv.com",
    "useast5": "https://api16-normal-us.tiktokv.us",
    "useast8": "https://api16-normal-us.tiktokv.us",
    "maliva": "https://api16-normal-c-useast1a.tiktokv.com",
    "alisg": "https://api16-normal-c-alisg.tiktokv.com",
    "sg1": "https://api16-normal-c-alisg.tiktokv.com",
}

FALLBACK_GATEWAYS = (
    "https://api16-normal-no1a.tiktokv.eu",
    "https://api16-normal-c-useast1a.tiktokv.com",
    "https://api16-normal-c-useast2a.tiktokv.com",
    "https://api16-normal-c-alisg.tiktokv.com",
)

WWW_BASE = "https://www.tiktok.com"

CORE_SYSTEM_NOTICE_GROUPS = (47, 46, 84, 210, 214, 69, 13, 226, 227, 228, 229, 661, 702)

VIOLATION_V1_STATUS: dict[int, str] = {
    1: "Effecting (Active / Appealable)",
    2: "ViolationExpired",
    3: "ViolationRecovered",
    4: "Appealing (Under Review)",
    5: "AppealExpired",
    6: "AppealSuccess (Restored)",
    7: "AppealFailed (Rejected)",
    8: "AppealInterrupted",
}

TNS_APPEAL_STATUS: dict[int, str] = {
    0: "CAN_NOT_SUBMIT",
    1: "CAN_SUBMIT",
    2: "REVIEWING",
    3: "SUCCEEDED",
    4: "FAILED",
    5: "EXPIRED",
    6: "TIMEOUT",
    7: "PENDING",
    8: "PUNISH_FINISHED",
}

STUDIO_APPEAL_STATUS: dict[int, str] = {
    0: "CAN_SUBMIT",
    1: "CANNOT_SUBMIT",
    2: "NO_PENALTY",
    3: "REVIEWING",
    4: "SUCCEEDED",
    5: "FAILED",
    6: "TIMEOUT",
}

STUDIO_REASON_CODES: dict[int, str] = {
    0: "OTHER",
    1: "COMMUNITY_GUIDELINES",
    2: "TOBACCO",
    3: "VIOLENT",
    4: "DANGEROUS",
    5: "OVERLY_SEXUALIZED",
    6: "SPAM",
    7: "UNORIGINAL",
    8: "MINOR_SAFETY",
    9: "UNORIGINAL_NR",
    10: "REPRODUCED_ACCOUNT",
    11: "CLICKBAIT_NFF",
}

PUNISH_TYPE_NAMES: dict[int, str] = {
    1030: "VideoNotRecommend (FYP Ineligible - Video)",
    1039: "PhotoNotRecommend (FYP Ineligible - Photo/Carousel)",
    1052: "VideoUMNotRecommend (Unoriginal/Low-Quality FYP Ineligible)",
    2004: "AccountOriginal (Unoriginal Account Restriction)",
    2048: "AccountIneligibleForRecommendation (Account FYP Restriction)",
}

APPEAL_STAGGER_SECONDS: tuple[float, float] = (8.5, 17.5)
WATCH_POLL_SECONDS: tuple[float, float] = (95.0, 165.0)

MOBILE_BASE_PARAMS: dict[str, str] = {
    "aid": "1233",
    "app_name": "musical_ly",
    "version_code": "37.1.0",
    "build_number": "371014",
    "device_platform": "iphone",
    "os_version": "17.5.1",
    "app_language": "en",
    "language": "en",
}

CHROME_CANDIDATES = (
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

ANDROID_WEBVIEW_UA = (
    "Mozilla/5.0 (Linux; Android 16; SM-A546E Build/BP2A.250605.031.A3; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/131.0.0.0 Mobile Safari/537.36 "
    "musical_ly_2024102030 JsSdk/1.0 NetType/WIFI Channel/googleplay AppName/musical_ly "
    "app_version/41.2.3 ByteLocale/en ByteFullLocale/en_US Region/US AppId/1233 "
    "Spark/1.5.9.1-bugfix BytedanceWebview/d8a21c6"
)

DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ── Audit & Appeal Data Models ───────────────────────────────────────────────

@dataclass
class NoticeViolation:
    nid: str
    group: int
    create_time: int
    title: str
    content: str
    object_id: str
    object_type: str
    punish_id: str
    punish_type: int
    schema_url: str = ""
    web_schema_url: str = ""
    can_appeal: bool = False
    status_code: int = 0
    status_label: str = "Unknown"
    appeal_id: str = ""
    punish_source: str = ""
    punish_end_time: int = 0
    reason_title: str = ""


@dataclass
class PostPenalty:
    item_id: str
    create_time: int
    desc: str
    play_count: int
    appeal_status: int = 2
    appeal_status_label: str = "NO_PENALTY"
    penalties: list[dict[str, Any]] = field(default_factory=list)
    reason_labels: list[str] = field(default_factory=list)
    v2_can_appeal: bool = False
    v2_appeal_details: Any = None


@dataclass
class AccountAuditResult:
    account_id: str
    username: str
    uid: str = ""
    reg_country: str = ""
    current_region: str = ""
    idc_gateway: str = ""
    region_warning_present: bool = False
    recent_device_logins: list[str] = field(default_factory=list)
    notice_violations: list[NoticeViolation] = field(default_factory=list)
    post_penalties: list[PostPenalty] = field(default_factory=list)
    total_posts: int = 0


@dataclass
class RestrictionActionReport:
    account_id: str
    scope: str  # "account_or_inbox" | "post_studio" | "post_cleanup"
    target_id: str
    punish_key: str
    title: str
    previous_state: str
    action_taken: str
    appealed_now: bool
    detail: str = ""


@dataclass
class AccountAppealSummary:
    account_id: str
    items: list[RestrictionActionReport] = field(default_factory=list)
    audit: AccountAuditResult | None = None
    notice_appeals_verified: int = 0
    post_appeals_verified: int = 0
    posts_hidden: int = 0


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_playwright_http_proxy(raw_proxy: str) -> dict[str, str]:
    """
    Chromium `--proxy-server` does not support authenticated SOCKS5 directly.
    Convert `bpproxy.at:1002` (SOCKS5) -> `http://bpproxy.at:1000` (HTTP proxy port on bpproxy),
    or pass standard `http://` proxy credentials to Playwright while preserving GEO string verbatim.
    """
    parsed = urlparse(raw_proxy)
    host = parsed.hostname or ""
    port = parsed.port or 80
    scheme = (parsed.scheme or "http").lower()

    if scheme.startswith("socks") and "bpproxy.at" in host and port == 1002:
        scheme = "http"
        port = 1000
    elif scheme.startswith("socks"):
        raise RuntimeError(
            f"Для браузерного фоллбэка через Chromium нужен HTTP-порт прокси (получен {raw_proxy}). "
            "На bpproxy это порт 1000 (переключается автоматически), для другого провайдера укажи http:// прокси."
        )

    cfg: dict[str, str] = {"server": f"{scheme}://{host}:{port}"}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


def extract_cookie_dict(account: AccountRecord) -> dict[str, str]:
    if not account.device:
        raise RuntimeError(f"[{account.account_id}] Нет `x-device` для извлечения кук.")
    dev = json.loads(account.device) if isinstance(account.device, str) else account.device
    cookies_map = dev.get("cookies") if isinstance(dev, dict) else None
    if not isinstance(cookies_map, dict):
        cookies_map = {}
    out = {str(k): str(v) for k, v in cookies_map.items() if v is not None and str(v) != ""}
    if not (out.get("sessionid") or out.get("sid_tt")):
        raise RuntimeError(
            f"[{account.account_id}] В `x-device.cookies` отсутствует `sessionid`/`sid_tt` — сначала выполни вход (`auth`)."
        )
    return out


def _extract_cookies_for_browser(account: AccountRecord) -> list[dict[str, str]]:
    cookies_map = extract_cookie_dict(account)
    out: list[dict[str, str]] = []
    for name, val in cookies_map.items():
        for dom in (".tiktok.com", ".tiktokv.com", ".tiktokv.eu", ".tiktokw.eu"):
            out.append({"name": name, "value": val, "domain": dom, "path": "/"})
    return out


def build_tns_appeal_reason_json(
    *,
    appeal_reasons_options: list[dict] | None,
    description_text: str,
) -> str:
    """
    Exact payload format reverse-engineered from TikTok `3782.06cbbab3.js`:
    - If `appeal_reasons_options` present: `{"type":2,"options":[optionCode],"description":desc}`
    - Else if `description_text` non-empty: `{"file_uri":"","description":desc}`
    - Else: `""`
    """
    clean_desc = (description_text or "").strip()
    if appeal_reasons_options:
        first_opt = appeal_reasons_options[0]
        opt_val = (
            first_opt.get("id")
            or first_opt.get("code")
            or first_opt.get("reason_id")
            or first_opt.get("value")
        )
        if opt_val is not None:
            return json.dumps(
                {"type": 2, "options": [opt_val], "description": clean_desc},
                separators=(",", ":"),
                ensure_ascii=False,
            )
    if clean_desc:
        return json.dumps(
            {"file_uri": "", "description": clean_desc},
            separators=(",", ":"),
            ensure_ascii=False,
        )
    return ""


def _extract_query_from_schema(schema_url: str) -> tuple[str, dict[str, str]]:
    if not schema_url:
        return "", {}
    raw = schema_url.strip()
    if raw.startswith(("aweme://", "sslocal://")):
        outer_q = dict(parse_qsl(urlsplit(raw).query))
        inner = outer_q.get("url", "")
        if inner:
            return inner, dict(parse_qsl(urlsplit(inner).query))
        return raw, outer_q
    return raw, dict(parse_qsl(urlsplit(raw).query))


# ── 1. Pure-HTTP Mobile IDC & Studio Scanner (0 Kintaro cost, 0 Browser) ─────

async def _resolve_idc_gateway(cookies: dict[str, str], http: httpx.AsyncClient) -> str:
    for key in ("store-idc", "tt-target-idc"):
        val = (cookies.get(key) or "").lower().strip()
        if val in IDC_GATEWAY_MAP:
            return IDC_GATEWAY_MAP[val]
    for gw in FALLBACK_GATEWAYS:
        try:
            r = await http.get(
                f"{gw}/aweme/v1/notice/count/",
                params={**MOBILE_BASE_PARAMS, "source": "1"},
                timeout=8.0,
            )
            if r.status_code == 200 and r.json().get("groups_in_filters") is not None:
                return gw
        except Exception:
            continue
    return FALLBACK_GATEWAYS[0]


async def _enrich_violation_detail(
    http: httpx.AsyncClient, idc_gateway: str, vio: NoticeViolation, uid: str
) -> None:
    params = {
        **MOBILE_BASE_PARAMS,
        "ViolationId": vio.punish_id,
        "UserId": uid,
        "ObjectId": vio.object_id,
        "ViolationType": str(vio.punish_type),
        "enter_from": "1",
    }
    try:
        resp = await http.get(f"{idc_gateway}/api/violation/v1/get_violation_detail", params=params)
        if resp.status_code != 200:
            return
        data = resp.json()
        if (data.get("BaseResp") or {}).get("StatusCode", -1) != 0:
            return
        vio.can_appeal = bool(data.get("CanAppeal", False))
        vinfo = data.get("ViolationInfo") or {}
        status_int = int(vinfo.get("Status") or 0)
        vio.status_code = status_int
        vio.status_label = VIOLATION_V1_STATUS.get(status_int, f"Status({status_int})")
        vio.appeal_id = str(vinfo.get("AppealId") or "")
        vio.punish_source = str(vinfo.get("PunishFromSource") or "")
        vio.punish_end_time = int(vinfo.get("PunishEndTime") or 0)
        reasons = data.get("ViolationReasons") or []
        if reasons and isinstance(reasons[0], dict):
            vio.reason_title = str(reasons[0].get("Title") or "")
    except Exception:
        pass


async def _check_single_post_penalty_http(http: httpx.AsyncClient, pp: PostPenalty) -> None:
    try:
        r1 = await http.get(
            f"{WWW_BASE}/mod/v1/getPenaltyDetails/",
            params={"aid": "1988", "app_name": "tiktok_creator_center", "vid": pp.item_id},
        )
        if r1.status_code == 200:
            d1 = r1.json()
            st = int(d1.get("appeal_status", 2))
            pp.appeal_status = st
            pp.appeal_status_label = STUDIO_APPEAL_STATUS.get(st, f"UNKNOWN({st})")
            pp.penalties = d1.get("penalties") or []
            codes: list[str] = []
            for pen in pp.penalties:
                for rc in pen.get("reason_codes") or []:
                    codes.append(STUDIO_REASON_CODES.get(int(rc), f"CODE_{rc}"))
            pp.reason_labels = codes
    except Exception:
        pass

    try:
        r2 = await http.get(
            f"{WWW_BASE}/api/moderation/penalties/gettransparencypagecontent",
            params={"aid": "1988", "entityId": pp.item_id, "enterFrom": ""},
        )
        if r2.status_code == 200:
            d2 = r2.json()
            if d2.get("showAppealBtn"):
                pp.v2_can_appeal = True
                pp.v2_appeal_details = d2.get("appealDetails")
    except Exception:
        pass


async def audit_account_restrictions(
    account: AccountRecord,
    *,
    extra_post_ids: list[str] | None = None,
    max_posts: int = 50,
) -> AccountAuditResult:
    """
    Fast pure-HTTP audit over the account's sticky SOCKS5/HTTP proxy:
    - Verifies session & extracts `uid`, `RegistrationCountry`, `CurrentRegion`
    - Scans mobile IDC gateway `/aweme/v1/notice/multi/` (`group=47/661/...`) + `/api/violation/v1/get_violation_detail`
    - Scans TikTok Studio `/tiktok/creator/manage/item_list/v1/` + V1/V2 post penalty endpoints
    """
    cookies = extract_cookie_dict(account)
    headers = {
        "user-agent": DESKTOP_CHROME_UA,
        "accept": "application/json, text/plain, */*",
    }
    async with httpx.AsyncClient(
        proxy=account.proxy,
        cookies=cookies,
        headers=headers,
        timeout=22.0,
        follow_redirects=True,
    ) as http:
        uid = ""
        reg_country = ""
        current_region = ""
        try:
            r_user = await http.get(
                f"{WWW_BASE}/tiktokstudio/api/web/user",
                params={"needIsVerified": "true", "needProfileBio": "true"},
            )
            if r_user.status_code == 200:
                u_data = r_user.json()
                uid = str(u_data.get("userId") or "")
                reg_info = (
                    (u_data.get("userBaseInfo") or {})
                    .get("UserProfile", {})
                    .get("UserBase", {})
                    .get("Region", {})
                )
                reg_country = str(reg_info.get("RegistrationCountry") or "")
                current_region = str(reg_info.get("Region") or "")
        except Exception:
            pass

        idc_gw = await _resolve_idc_gateway(cookies, http)
        audit = AccountAuditResult(
            account_id=account.account_id,
            username=account.username or account.account_id,
            uid=uid,
            reg_country=reg_country,
            current_region=current_region,
            idc_gateway=idc_gw,
        )

        # Discover active system notice groups
        groups: set[int] = set(CORE_SYSTEM_NOTICE_GROUPS)
        try:
            r_cnt = await http.get(
                f"{idc_gw}/aweme/v1/notice/count/",
                params={**MOBILE_BASE_PARAMS, "source": "1"},
            )
            if r_cnt.status_code == 200:
                for flt in r_cnt.json().get("groups_in_filters") or []:
                    if flt.get("filter_type") in (33, 41, 6, 14):
                        for g in flt.get("groups") or []:
                            groups.add(int(g))
        except Exception:
            pass

        raw_notices: list[tuple[int, dict[str, Any]]] = []
        sorted_groups = sorted(groups)
        for i in range(0, len(sorted_groups), 20):
            chunk = sorted_groups[i : i + 20]
            group_list = json.dumps(
                [{"count": 25, "group": g, "is_mark_read": 0, "max_time": 0, "min_time": 0} for g in chunk],
                separators=(",", ":"),
            )
            try:
                r_multi = await http.get(
                    f"{idc_gw}/aweme/v1/notice/multi/",
                    params={**MOBILE_BASE_PARAMS, "group_list": group_list},
                )
                if r_multi.status_code == 200:
                    for nl in r_multi.json().get("notice_lists") or []:
                        g_id = int(nl.get("group") or 0)
                        for item in nl.get("notice_list") or []:
                            if isinstance(item, dict):
                                raw_notices.append((g_id, item))
            except Exception:
                continue

        # Sort newest-first by `create_time` so only the latest state per object is surfaced cleanly
        raw_notices.sort(
            key=lambda pair: int(
                pair[1].get("create_time")
                or (pair[1].get("template_notice") or {}).get("create_time")
                or 0
            ),
            reverse=True,
        )

        seen_punish_ids: set[str] = set()
        seen_objects: set[str] = set()

        for g_id, item in raw_notices:
            nid = str(item.get("nid_str") or item.get("nid") or "")
            ctime = int(
                item.get("create_time")
                or (item.get("template_notice") or {}).get("create_time")
                or 0
            )
            tn = item.get("template_notice") or {}
            txt = item.get("text") or {}
            ann = item.get("announcement") or {}
            notice_body = tn.get("notice") or {}

            title = str(
                (notice_body.get("title") or {}).get("title")
                or txt.get("title")
                or ann.get("title")
                or ""
            )
            content = str(
                notice_body.get("content")
                or txt.get("content")
                or ann.get("content")
                or ""
            )

            low_title = title.lower()
            if "update your account region" in low_title:
                audit.region_warning_present = True
            if "logged in from a new device" in low_title and content:
                if content not in audit.recent_device_logins and len(audit.recent_device_logins) < 5:
                    audit.recent_device_logins.append(content)

            raw_schema = str(
                tn.get("schema_url")
                or notice_body.get("right_schema_url")
                or txt.get("schema_url")
                or ann.get("schema_url")
                or ""
            )
            web_schema = ""
            msg_extra_raw = tn.get("message_extra") or ""
            if msg_extra_raw:
                try:
                    me = json.loads(msg_extra_raw)
                    be_raw = me.get("business_extra") or ""
                    if be_raw:
                        be = json.loads(be_raw) if isinstance(be_raw, str) else be_raw
                        web_schema = str(be.get("web_schema_url") or "")
                except Exception:
                    pass

            falcon_url, q_falcon = _extract_query_from_schema(raw_schema)
            _, q_web = _extract_query_from_schema(web_schema)
            merged_q = {**q_web, **q_falcon}

            punish_id = merged_q.get("punish_id") or merged_q.get("punish_key") or ""
            object_id = merged_q.get("object_id") or ""
            punish_type_str = merged_q.get("punish_type") or "0"
            object_type = merged_q.get("object_type") or ""

            if not punish_id or not object_id:
                continue
            if punish_id in seen_punish_ids:
                continue
            seen_punish_ids.add(punish_id)

            effective_obj_type = object_type or ("4" if object_id == uid else "1")
            obj_dedup = f"{effective_obj_type}:{object_id}"
            if obj_dedup in seen_objects:
                continue
            seen_objects.add(obj_dedup)

            try:
                punish_type = int(punish_type_str)
            except ValueError:
                punish_type = 0

            vio = NoticeViolation(
                nid=nid,
                group=g_id,
                create_time=ctime,
                title=title or "Moderation Notice",
                content=content,
                object_id=object_id,
                object_type=effective_obj_type,
                punish_id=punish_id,
                punish_type=punish_type,
                schema_url=falcon_url or raw_schema,
                web_schema_url=web_schema,
            )

            if "eligible" in low_title and "ineligible" not in low_title:
                vio.can_appeal = False
                vio.status_code = 6
                vio.status_label = VIOLATION_V1_STATUS[6]
            else:
                await _enrich_violation_detail(http, idc_gw, vio, uid)

            audit.notice_violations.append(vio)

        # Scan TikTok Studio posts via pure HTTP (`/tiktok/creator/manage/item_list/v1/`)
        post_map: dict[str, PostPenalty] = {}
        try:
            r_items = await http.post(
                f"{WWW_BASE}/tiktok/creator/manage/item_list/v1/",
                params={"aid": "1988", "app_name": "tiktok_creator_center", "device_platform": "web_pc"},
                json={
                    "cursor": 0,
                    "size": max_posts,
                    "query": {
                        "sort_orders": [{"field_name": "post_time", "order": 2}],
                        "conditions": [],
                        "is_recent_posts": False,
                    },
                },
            )
            if r_items.status_code == 200:
                for it in r_items.json().get("item_list") or []:
                    vid = str(it.get("item_id") or "")
                    if vid:
                        post_map[vid] = PostPenalty(
                            item_id=vid,
                            create_time=int(it.get("create_time") or 0),
                            desc=str(it.get("desc") or ""),
                            play_count=int(it.get("play_count") or 0),
                        )
        except Exception:
            pass

        for extra_vid in extra_post_ids or []:
            if extra_vid and extra_vid not in post_map:
                post_map[extra_vid] = PostPenalty(item_id=extra_vid, create_time=0, desc="", play_count=0)
        for rec in account.upload_history:
            if rec.aweme_id and rec.aweme_id not in post_map:
                post_map[rec.aweme_id] = PostPenalty(
                    item_id=rec.aweme_id,
                    create_time=0,
                    desc=rec.description,
                    play_count=rec.last_play_count or 0,
                )

        audit.total_posts = len(post_map)
        sem = asyncio.Semaphore(5)

        async def _check_one(p_obj: PostPenalty) -> None:
            async with sem:
                await _check_single_post_penalty_http(http, p_obj)

        await asyncio.gather(*(_check_one(pp) for pp in post_map.values()))
        audit.post_penalties = list(post_map.values())

    return audit


# ── 2. Browser `webmssdk` Fallbacks (Launched Lazily ONLY When Needed) ───────

async def _attach_fast_cdn_router(page: Page, direct_http: httpx.AsyncClient) -> None:
    async def _route_handler(route) -> None:
        req = route.request
        u = req.url
        if req.resource_type in ("image", "media", "font"):
            await route.abort()
            return
        if any(
            cdn in u
            for cdn in (
                "ttwstatic.com",
                "tiktokcdn.com",
                "tiktokcdn-eu.com",
                "ibytedtos.com",
            )
        ):
            try:
                r = await direct_http.get(u)
                await route.fulfill(status=r.status_code, headers=dict(r.headers), body=r.content)
                return
            except Exception:
                pass
        await route.continue_()

    await page.route("**/*", _route_handler)


def _find_chrome_executable(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for cand in CHROME_CANDIDATES:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    raise RuntimeError("Не найден установленный Google Chrome / Chromium (`/usr/bin/google-chrome`).")


async def _submit_notice_via_browser_webview(
    account: AccountRecord,
    vio: NoticeViolation,
    reason_options: list[dict[str, Any]] | None,
    appeal_text: str,
    chrome_executable: str | None = None,
) -> tuple[bool, str]:
    exe = _find_chrome_executable(chrome_executable)
    pw_proxy = _resolve_playwright_http_proxy(account.proxy)
    pw_cookies = _extract_cookies_for_browser(account)

    target_url = vio.web_schema_url or vio.schema_url
    if not target_url or not target_url.startswith("http"):
        target_url = (
            f"{WWW_BASE}/violation-appeal?enter_from=1&hide_nav_bar=1&is_new_vio_platform=1"
            f"&object_id={vio.object_id}&object_type={vio.object_type}"
            f"&punish_key={vio.punish_id}&punish_type={vio.punish_type}"
        )

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as direct_http:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=exe,
                headless=True,
                proxy=pw_proxy,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            try:
                ctx_webview: BrowserContext = await browser.new_context(
                    viewport={"width": 430, "height": 932},
                    user_agent=ANDROID_WEBVIEW_UA,
                )
                await ctx_webview.add_cookies(pw_cookies)
                wpage = await ctx_webview.new_page()
                await _attach_fast_cdn_router(wpage, direct_http)

                detail_common_qs = "aid=1988&app_language=en&app_name=tiktok_web&channel=tiktok_web&device_platform=web_mobile&os=android"
                ui_commit_resp: dict | None = None

                async def _capture(resp) -> None:
                    nonlocal detail_common_qs, ui_commit_resp
                    if "/tns-api/appeal/violation-detail" in resp.url:
                        try:
                            raw_qs = parse_qs(urlparse(resp.url).query, keep_blank_values=True)
                            filtered = {
                                k: v[0]
                                for k, v in raw_qs.items()
                                if k not in ("object_id", "object_type", "punish_key", "msToken", "X-Bogus", "X-Gnarly")
                            }
                            if filtered:
                                detail_common_qs = urlencode(filtered)
                        except Exception:
                            pass
                    elif "/tns-api/appeal/post-commit" in resp.url:
                        try:
                            ui_commit_resp = await resp.json()
                        except Exception:
                            pass

                wpage.on("response", lambda r: asyncio.create_task(_capture(r)))
                await wpage.goto(target_url, wait_until="domcontentloaded", timeout=35000)
                await wpage.wait_for_timeout(4500)

                low_txt = (await wpage.inner_text("body")).lower()
                if "appeal submitted" in low_txt or "request received" in low_txt or "appeal approved" in low_txt:
                    return True, "already_submitted_or_approved"

                # Try native UI click first
                appeal_btn = wpage.get_by_role("button", name="Appeal")
                if await appeal_btn.count() == 0:
                    appeal_btn = wpage.get_by_text("Appeal", exact=True)
                if await appeal_btn.count() > 0 and await appeal_btn.first.is_visible():
                    await appeal_btn.first.click()
                    await wpage.wait_for_timeout(2000)

                    radios = wpage.locator('input[type="radio"], [role="radio"]')
                    if await radios.count() > 0 and await radios.first.is_visible():
                        await radios.first.click()
                        await wpage.wait_for_timeout(400)

                    ta = wpage.locator("textarea")
                    if await ta.count() > 0 and await ta.first.is_visible() and appeal_text:
                        await ta.first.fill(appeal_text)
                        await wpage.wait_for_timeout(400)

                    sub_btn = wpage.get_by_role("button", name="Submit")
                    if await sub_btn.count() == 0:
                        sub_btn = wpage.get_by_text("Submit", exact=True)
                    if await sub_btn.count() > 0 and await sub_btn.first.is_visible():
                        for _ in range(3):
                            ui_commit_resp = None
                            await sub_btn.first.click()
                            await wpage.wait_for_timeout(2500)
                            if isinstance(ui_commit_resp, dict) and ui_commit_resp.get("status_code") == 0:
                                return True, "ui_post_commit(status_code=0)"
                            after_txt = (await wpage.inner_text("body")).lower()
                            if "appeal submitted" in after_txt or "request received" in after_txt:
                                return True, "ui_submitted"

                # Signed `/tns-api/appeal/post-commit` fallback (3 retries)
                opts = reason_options
                if not opts:
                    r_reasons = await wpage.evaluate(
                        """async ({appealType, objectId, qs}) => {
                            const r = await fetch(`/tns-api/appeal/reason?${qs}&appeal_type=${appealType}&object_id=${encodeURIComponent(objectId)}`, {credentials: 'include'});
                            return await r.json().catch(() => ({}));
                        }""",
                        {"appealType": vio.punish_type, "objectId": vio.object_id, "qs": detail_common_qs},
                    )
                    cand = (r_reasons or {}).get("appeal_reasons")
                    if isinstance(cand, list):
                        opts = cand

                reason_str = build_tns_appeal_reason_json(
                    appeal_reasons_options=opts,
                    description_text=appeal_text,
                )
                commit_payload = {
                    "object_id": vio.object_id,
                    "appeal_type": vio.punish_type,
                    "reason": reason_str,
                    "punish_key": vio.punish_id,
                    "punish_id": vio.punish_id,
                    "appeal_uri": "",
                    "extra": "",
                }
                last_resp: Any = {}
                for _ in range(3):
                    last_resp = await wpage.evaluate(
                        """async ({payload, qs}) => {
                            const r = await fetch(`/tns-api/appeal/post-commit?${qs}`, {
                                method: 'POST',
                                credentials: 'include',
                                headers: {'content-type': 'application/json'},
                                body: JSON.stringify(payload),
                            });
                            return await r.json().catch(() => ({status_code: -1}));
                        }""",
                        {"payload": commit_payload, "qs": detail_common_qs},
                    )
                    if isinstance(last_resp, dict) and last_resp.get("status_code") == 0:
                        return True, "tns_post_commit(status_code=0)"
                    await wpage.wait_for_timeout(1500)
                return False, f"tns_resp={last_resp}"
            finally:
                await browser.close()


async def _submit_post_via_browser_studio(
    account: AccountRecord,
    pp: PostPenalty,
    chrome_executable: str | None = None,
) -> tuple[bool, str]:
    exe = _find_chrome_executable(chrome_executable)
    pw_proxy = _resolve_playwright_http_proxy(account.proxy)
    pw_cookies = _extract_cookies_for_browser(account)

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as direct_http:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=exe,
                headless=True,
                proxy=pw_proxy,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            try:
                ctx_desktop = await browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    user_agent=DESKTOP_CHROME_UA,
                )
                await ctx_desktop.add_cookies(pw_cookies)
                page = await ctx_desktop.new_page()
                await _attach_fast_cdn_router(page, direct_http)
                await page.goto(
                    f"{WWW_BASE}/tiktokstudio/analytics/{pp.item_id}",
                    wait_until="domcontentloaded",
                    timeout=40000,
                )
                await page.wait_for_timeout(4500)

                if pp.v2_can_appeal and pp.v2_appeal_details is not None:
                    res_v2 = await page.evaluate(
                        """async ({appealDetails, itemId}) => {
                            const r = await fetch('/api/moderation/penalties/submitappeal', {
                                method: 'POST',
                                credentials: 'include',
                                headers: {'content-type': 'application/json'},
                                body: JSON.stringify({appealDetails, itemId}),
                            });
                            return await r.json().catch(() => ({}));
                        }""",
                        {"appealDetails": pp.v2_appeal_details, "itemId": pp.item_id},
                    )
                    if (res_v2.get("BaseResp") or {}).get("StatusCode") == 0:
                        return True, "browser_studio_v2(StatusCode=0)"

                details = [
                    {
                        "issuer": pen.get("issuer"),
                        "penalty_id": (pen.get("penalty_id") or {}).get("id")
                        if isinstance(pen.get("penalty_id"), dict)
                        else pen.get("penalty_id"),
                    }
                    for pen in pp.penalties
                ]
                qs_params = urlencode(
                    {
                        "aid": "1988",
                        "app_name": "tiktok_creator_center",
                        "vid": pp.item_id,
                        "appeal_details": json.dumps(details, separators=(",", ":")),
                    }
                )
                res_v1 = await page.evaluate(
                    """async (qs) => {
                        const r = await fetch(`/mod/v1/submitAppeals?${qs}`, {
                            method: 'POST',
                            credentials: 'include',
                        });
                        return await r.json().catch(() => ({}));
                    }""",
                    qs_params,
                )
                if res_v1.get("status_code") == 0:
                    return True, "browser_studio_v1(status_code=0)"
                return False, f"studio_resp={res_v1}"
            finally:
                await browser.close()


# ── 3. Unified 3-Channel Cascading Unbanner + Rejected Cleanup ───────────────

async def remove_account_restrictions(
    account: AccountRecord,
    *,
    post_ids: list[str] | None = None,
    appeal_text: str = "Our content is original, authentic, and strictly follows all TikTok Community Guidelines.",
    dry_run: bool = False,
    hide_rejected: bool = False,
    stagger_appeals: bool = True,
    chrome_executable: str | None = None,
) -> AccountAppealSummary:
    """
    Full multi-channel restriction scanner, appealer & rejected-post cleaner (unified 12 + 233):
    1. Pure-HTTP Mobile IDC & Studio audit (`audit_account_restrictions`).
    2. Notice/Account appeals: `commit_appeal` -> `aweme/v2/appeal/commit/` -> Headless Chrome `BytedanceWebview` `tns-api`,
       verified via `get_violation_detail`.
    3. Studio Post appeals: HTTP V2/V1 -> Headless Chrome Studio fallback, verified via `getPenaltyDetails`.
    4. Optional `--hide-rejected`: switches `FAILED` (`appeal_status == 5`) posts to Private (`visibility_type=2`)
       to prevent 30-day `punish_type=2048` account recommendation bans.
    """
    logger.info("[%s] Запуск HTTP-аудита системных уведомлений (IDC) и постов Studio...", account.account_id)
    audit = await audit_account_restrictions(account, extra_post_ids=post_ids)
    summary = AccountAppealSummary(account_id=account.account_id, audit=audit)

    cookies = extract_cookie_dict(account)
    headers = {
        "user-agent": DESKTOP_CHROME_UA,
        "accept": "application/json, text/plain, */*",
    }

    async with httpx.AsyncClient(
        proxy=account.proxy,
        cookies=cookies,
        headers=headers,
        timeout=22.0,
        follow_redirects=True,
    ) as http:
        appeals_sent_count = 0

        # A. Process Notice / Account Violations
        for vio in audit.notice_violations:
            is_appealable = vio.can_appeal or vio.status_code == 1
            if not is_appealable:
                summary.items.append(
                    RestrictionActionReport(
                        account_id=account.account_id,
                        scope="account_or_inbox",
                        target_id=vio.object_id,
                        punish_key=vio.punish_id,
                        title=vio.title,
                        previous_state=vio.status_label,
                        action_taken="none",
                        appealed_now=False,
                        detail=(
                            f"Статус: {vio.status_label}"
                            + (f" | Source={vio.punish_source}" if vio.punish_source else "")
                            + (f" | AppealId={vio.appeal_id}" if vio.appeal_id and vio.appeal_id != "0" else "")
                        ),
                    )
                )
                continue

            if dry_run:
                summary.items.append(
                    RestrictionActionReport(
                        account_id=account.account_id,
                        scope="account_or_inbox",
                        target_id=vio.object_id,
                        punish_key=vio.punish_id,
                        title=vio.title,
                        previous_state=vio.status_label,
                        action_taken="dry_run",
                        appealed_now=False,
                        detail=f"[DRY-RUN] Доступна апелляция (type={vio.punish_type}, source={vio.punish_source or '-'})",
                    )
                )
                continue

            if stagger_appeals and appeals_sent_count > 0:
                delay = random.uniform(*APPEAL_STAGGER_SECONDS)
                logger.info("[%s] Анти-спам пауза %.1fс перед следующей апелляцией...", account.account_id, delay)
                await asyncio.sleep(delay)

            appeals_sent_count += 1
            prev_label = vio.status_label

            # Fetch mobile appeal reason options
            reasons: list[dict[str, Any]] = []
            try:
                r_rsn = await http.get(
                    f"{audit.idc_gateway}/tiktok/v1/appeal/reason/",
                    params={
                        **MOBILE_BASE_PARAMS,
                        "object_id": vio.object_id,
                        "appeal_type": str(vio.punish_type),
                    },
                )
                if r_rsn.status_code == 200:
                    reasons = r_rsn.json().get("appeal_reasons") or []
            except Exception:
                pass

            selected_code = (
                reasons[0].get("code") or reasons[0].get("id") if reasons else None
            )
            reason_json = build_tns_appeal_reason_json(
                appeal_reasons_options=reasons or None,
                description_text=appeal_text,
            )

            body_v1: dict[str, Any] = {
                "ViolationId": vio.punish_id,
                "ViolationType": vio.punish_type,
                "UserId": audit.uid,
                "ObjectId": vio.object_id,
                "appeal_type": vio.punish_type,
                "object_id": vio.object_id,
                "punish_key": vio.punish_id,
                "appeal_uri": "",
                "extra": "",
            }
            if selected_code is not None:
                body_v1["AppealReasons"] = [str(selected_code)]
            if reason_json:
                body_v1["reason"] = reason_json
            if appeal_text:
                body_v1["AppealDescription"] = appeal_text

            ok_notice = False
            channel_used = ""
            err_note = ""

            # Channel 1: Pure-HTTP `/api/violation/v1/commit_appeal`
            try:
                r1 = await http.post(
                    f"{audit.idc_gateway}/api/violation/v1/commit_appeal",
                    params=MOBILE_BASE_PARAMS,
                    json=body_v1,
                )
                if r1.status_code == 200:
                    j1 = r1.json()
                    sc = j1.get("status_code", (j1.get("BaseResp") or {}).get("StatusCode", -1))
                    if sc == 0:
                        await asyncio.sleep(1.5)
                        await _enrich_violation_detail(http, audit.idc_gateway, vio, audit.uid)
                        if vio.status_code in (4, 6) or not vio.can_appeal:
                            ok_notice = True
                            channel_used = "HTTP /api/violation/v1/commit_appeal"
                    else:
                        err_note = f"commit_appeal={sc}"
            except Exception as exc:
                err_note = str(exc)

            # Channel 2: Pure-HTTP `/aweme/v2/appeal/commit/`
            if not ok_notice:
                try:
                    legacy_params = {
                        **MOBILE_BASE_PARAMS,
                        "appeal_type": str(vio.punish_type),
                        "object_id": vio.object_id,
                        "punish_key": vio.punish_id,
                        "appeal_uri": "",
                        "extra": "",
                    }
                    if reason_json:
                        legacy_params["reason"] = reason_json
                    r2 = await http.post(
                        f"{audit.idc_gateway}/aweme/v2/appeal/commit/",
                        params=legacy_params,
                    )
                    if r2.status_code == 200 and r2.json().get("status_code", -1) == 0:
                        await asyncio.sleep(1.5)
                        await _enrich_violation_detail(http, audit.idc_gateway, vio, audit.uid)
                        if vio.status_code in (4, 6) or not vio.can_appeal:
                            ok_notice = True
                            channel_used = "HTTP /aweme/v2/appeal/commit/"
                except Exception:
                    pass

            # Channel 3: Headless Chrome `BytedanceWebview` `webmssdk` fallback
            if not ok_notice:
                logger.info(
                    "[%s] HTTP commit требует WebView подписи — запускаю Chrome BytedanceWebview для punish_id=%s...",
                    account.account_id,
                    vio.punish_id,
                )
                ok_web, web_msg = await _submit_notice_via_browser_webview(
                    account, vio, reasons or None, appeal_text, chrome_executable
                )
                if ok_web:
                    await asyncio.sleep(1.5)
                    await _enrich_violation_detail(http, audit.idc_gateway, vio, audit.uid)
                    ok_notice = True
                    channel_used = f"WebView ({web_msg})"
                else:
                    err_note = f"{err_note} | {web_msg}".strip(" |")

            if ok_notice:
                summary.notice_appeals_verified += 1

            summary.items.append(
                RestrictionActionReport(
                    account_id=account.account_id,
                    scope="account_or_inbox",
                    target_id=vio.object_id,
                    punish_key=vio.punish_id,
                    title=vio.title,
                    previous_state=prev_label,
                    action_taken=channel_used or "failed",
                    appealed_now=ok_notice,
                    detail=(
                        f"Апелляция принята ({channel_used}) -> {vio.status_label}"
                        if ok_notice
                        else f"Не удалось подать апелляцию: {err_note}"
                    ),
                )
            )

        # B. Process TikTok Studio Per-Post FYP Restrictions
        for pp in audit.post_penalties:
            can_submit_post = pp.appeal_status == 0 or pp.v2_can_appeal
            if can_submit_post:
                reasons_str = ",".join(pp.reason_labels) or ("V2" if pp.v2_can_appeal else "FYP_INELIGIBLE")
                if dry_run:
                    summary.items.append(
                        RestrictionActionReport(
                            account_id=account.account_id,
                            scope="post_studio",
                            target_id=pp.item_id,
                            punish_key="studio",
                            title=f"Post FYP ({reasons_str})",
                            previous_state=pp.appeal_status_label,
                            action_taken="dry_run",
                            appealed_now=False,
                            detail=f"[DRY-RUN] Можно подать апелляцию на пост (views={pp.play_count})",
                        )
                    )
                    continue

                if stagger_appeals and appeals_sent_count > 0:
                    delay = random.uniform(*APPEAL_STAGGER_SECONDS)
                    logger.info(
                        "[%s] Анти-спам пауза %.1fс перед апелляцией поста %s...",
                        account.account_id,
                        delay,
                        pp.item_id,
                    )
                    await asyncio.sleep(delay)

                appeals_sent_count += 1
                prev_post_state = pp.appeal_status_label
                ok_post = False
                post_channel = ""

                # Channel 1A: Pure-HTTP Studio V2
                if pp.v2_can_appeal and pp.v2_appeal_details is not None:
                    try:
                        rv2 = await http.post(
                            f"{WWW_BASE}/api/moderation/penalties/submitappeal",
                            params={"aid": "1988"},
                            json={"appealDetails": pp.v2_appeal_details, "itemId": pp.item_id},
                        )
                        if rv2.status_code == 200 and (rv2.json().get("BaseResp") or {}).get("StatusCode") == 0:
                            ok_post = True
                            post_channel = "HTTP Studio V2"
                    except Exception:
                        pass

                # Channel 1B: Pure-HTTP Studio V1
                if not ok_post and pp.penalties:
                    details = [
                        {
                            "issuer": pen.get("issuer"),
                            "penalty_id": (pen.get("penalty_id") or {}).get("id")
                            if isinstance(pen.get("penalty_id"), dict)
                            else pen.get("penalty_id"),
                        }
                        for pen in pp.penalties
                        if pen.get("penalty_id")
                    ]
                    try:
                        rv1 = await http.post(
                            f"{WWW_BASE}/mod/v1/submitAppeals",
                            params={
                                "aid": "1988",
                                "app_name": "tiktok_creator_center",
                                "vid": pp.item_id,
                                "appeal_details": json.dumps(details, separators=(",", ":")),
                            },
                        )
                        if rv1.status_code == 200 and rv1.json().get("status_code") == 0:
                            await asyncio.sleep(1.2)
                            await _check_single_post_penalty_http(http, pp)
                            if pp.appeal_status != 0:
                                ok_post = True
                                post_channel = "HTTP Studio V1"
                    except Exception:
                        pass

                # Channel 2: Headless Chrome Studio `webmssdk` fallback
                if not ok_post:
                    ok_br, br_msg = await _submit_post_via_browser_studio(
                        account, pp, chrome_executable
                    )
                    if ok_br:
                        await asyncio.sleep(1.2)
                        await _check_single_post_penalty_http(http, pp)
                        ok_post = True
                        post_channel = br_msg
                    else:
                        post_channel = br_msg

                if ok_post:
                    summary.post_appeals_verified += 1

                summary.items.append(
                    RestrictionActionReport(
                        account_id=account.account_id,
                        scope="post_studio",
                        target_id=pp.item_id,
                        punish_key=reasons_str,
                        title=f"Ineligible for For You ({reasons_str})",
                        previous_state=prev_post_state,
                        action_taken=post_channel,
                        appealed_now=ok_post,
                        detail=(
                            f"Апелляция на пост отправлена ({post_channel}) -> {pp.appeal_status_label}"
                            if ok_post
                            else f"Ошибка апелляции поста: {post_channel}"
                        ),
                    )
                )
            elif pp.appeal_status not in (2,):
                # Already reviewing / succeeded / failed
                reasons_str = ",".join(pp.reason_labels) or "MODERATION"
                if hide_rejected and pp.appeal_status == 5:
                    if dry_run:
                        summary.items.append(
                            RestrictionActionReport(
                                account_id=account.account_id,
                                scope="post_cleanup",
                                target_id=pp.item_id,
                                punish_key=reasons_str,
                                title=f"Rejected Post ({reasons_str})",
                                previous_state="FAILED",
                                action_taken="dry_run_private",
                                appealed_now=False,
                                detail="[DRY-RUN] Перевёл бы отклонённый пост в Private (защита от бана 2048)",
                            )
                        )
                    else:
                        ok_priv = False
                        priv_note = ""
                        try:
                            rp = await http.post(
                                f"{WWW_BASE}/tiktokstudio/api/web/item/visibility",
                                params={"aid": "1988"},
                                json={"item_id": pp.item_id, "visibility_type": 2},
                            )
                            rj = rp.json()
                            if rp.status_code == 200 and rj.get("statusCode", rj.get("status_code", -1)) == 0:
                                ok_priv = True
                                summary.posts_hidden += 1
                                priv_note = "Отклонённый пост переведён в Private (visibility_type=2)"
                            else:
                                priv_note = f"Ответ visibility: {rp.text[:140]}"
                        except Exception as exc:
                            priv_note = f"Ошибка visibility: {exc}"

                        summary.items.append(
                            RestrictionActionReport(
                                account_id=account.account_id,
                                scope="post_cleanup",
                                target_id=pp.item_id,
                                punish_key=reasons_str,
                                title=f"Rejected Post ({reasons_str})",
                                previous_state="FAILED",
                                action_taken="POST /tiktokstudio/api/web/item/visibility",
                                appealed_now=ok_priv,
                                detail=priv_note,
                            )
                        )
                else:
                    summary.items.append(
                        RestrictionActionReport(
                            account_id=account.account_id,
                            scope="post_studio",
                            target_id=pp.item_id,
                            punish_key=reasons_str,
                            title=f"Post ({reasons_str})",
                            previous_state=pp.appeal_status_label,
                            action_taken="none",
                            appealed_now=False,
                            detail=f"Статус поста: {pp.appeal_status_label} (views={pp.play_count})",
                        )
                    )

    return summary

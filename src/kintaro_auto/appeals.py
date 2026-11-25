from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx
from playwright.async_api import BrowserContext, Page, async_playwright

from kintaro_auto.models import AccountRecord

logger = logging.getLogger("kintaro_auto")

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


@dataclass
class RestrictionActionReport:
    account_id: str
    scope: str  # "account_or_inbox" | "post_studio"
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


def _resolve_playwright_http_proxy(raw_proxy: str) -> dict[str, str]:
    """
    Chromium `--proxy-server` does not support authenticated SOCKS5 directly.
    Convert `bpproxy.at:1002` (SOCKS5) -> `http://bpproxy.at:1000` (HTTP proxy port on bpproxy),
    or pass standard `http://` proxy credentials to Playwright.
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
            f"Для браузерного снятия огранов через Chromium нужен HTTP-порт прокси (получен {raw_proxy}). "
            "На bpproxy это порт 1000 (переключается автоматически), для другого провайдера укажи http:// прокси."
        )

    cfg: dict[str, str] = {"server": f"{scheme}://{host}:{port}"}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


def _extract_cookies_for_browser(account: AccountRecord) -> list[dict[str, str]]:
    if not account.device:
        raise RuntimeError(f"[{account.account_id}] Нет `x-device` для извлечения кук.")
    dev = json.loads(account.device) if isinstance(account.device, str) else account.device
    cookies_map = dev.get("cookies") or {}
    if not cookies_map.get("sessionid"):
        raise RuntimeError(
            f"[{account.account_id}] В `x-device.cookies` отсутствует `sessionid` — сначала выполни вход (`auth`)."
        )
    out: list[dict[str, str]] = []
    for name, val in cookies_map.items():
        if val is None or str(val) == "":
            continue
        for dom in (".tiktok.com", ".tiktokv.com", ".tiktokv.eu", ".tiktokw.eu"):
            out.append({"name": str(name), "value": str(val), "domain": dom, "path": "/"})
    return out


def build_tns_appeal_reason_json(
    *,
    appeal_reasons_options: list[dict] | None,
    description_text: str,
) -> str:
    """
    Exact payload format reverse-engineered from TikTok `3782.06cbbab3.js` (`ta.ig`):
    - If `hasAppealReasons` and option exists: `{"type":2,"options":[optionCode],"description":desc}`
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
                await route.fulfill(
                    status=r.status_code, headers=dict(r.headers), body=r.content
                )
                return
            except Exception:
                pass
        await route.continue_()

    await page.route("**/*", _route_handler)


async def _scan_and_appeal_inbox_violations(
    ctx_desktop: BrowserContext,
    ctx_webview: BrowserContext,
    direct_http: httpx.AsyncClient,
    account: AccountRecord,
    appeal_text: str,
) -> list[RestrictionActionReport]:
    """
    1. Opens Desktop Web -> Activity -> System Notifications to collect signed `/api/inbox/notice_list/`
       (`group: 661` Account Updates, `46/47` System messages).
    2. Extracts all `https://www.tiktok.com/violation-appeal?...` `schema_url` items
       (covers `Account ineligible for recommendation` `object_type=4, punish_type=2048` and removed/restricted posts).
    3. Opens each `schema_url` in Android BytedanceWebview context, inspects `/tns-api/appeal/violation-detail`,
       fetches `/tns-api/appeal/reason`, and submits `/tns-api/appeal/post-commit` (with retries against TikTok's Network Error bug).
    """
    reports: list[RestrictionActionReport] = []
    page = await ctx_desktop.new_page()
    await _attach_fast_cdn_router(page, direct_http)

    captured_notices: list[dict] = []

    async def _on_resp(resp) -> None:
        if "notice_list" in resp.url and "json" in resp.headers.get("content-type", ""):
            try:
                data = await resp.json()
                for nl in data.get("notice_lists") or []:
                    for item in nl.get("notice_list") or []:
                        if isinstance(item, dict):
                            captured_notices.append(item)
            except Exception:
                pass

    page.on("response", lambda r: asyncio.create_task(_on_resp(r)))

    try:
        profile_url = (
            f"https://www.tiktok.com/@{account.username}"
            if account.username
            else "https://www.tiktok.com/"
        )
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(4500)

        act_btn = page.locator('[data-e2e="nav-activity"]')
        if await act_btn.count() > 0 and await act_btn.first.is_visible():
            await act_btn.first.click()
            await page.wait_for_timeout(4000)

            sys_loc = page.get_by_text("System Notifications", exact=False)
            if await sys_loc.count() > 0 and await sys_loc.first.is_visible():
                await sys_loc.first.click()
                await page.wait_for_timeout(3500)

        # Also directly fetch group 661 (`Account Updates`) & 46/47 (`System Notifications`) via signed window.fetch
        for grp_id in (661, 46, 47):
            extra_grp = await page.evaluate(
                """async (g) => {
                    const grp = encodeURIComponent(JSON.stringify([{count: 25, is_mark_read: 0, group: g, max_time: 0, min_time: 0}]));
                    const r = await fetch(`/api/inbox/notice_list/?aid=1988&app_language=en&app_name=tiktok_web&channel=tiktok_web&device_platform=web_pc&group_list=${grp}`, {credentials: 'include'});
                    return await r.json().catch(() => ({}));
                }""",
                grp_id,
            )
            for nl in (extra_grp or {}).get("notice_lists") or []:
                for item in nl.get("notice_list") or []:
                    if isinstance(item, dict):
                        captured_notices.append(item)
    finally:
        await page.close()

    resolved_punish_keys: set[str] = set()
    for item in captured_notices:
        tmpl_r = item.get("template_notice") or {}
        s_url_r = str(tmpl_r.get("schema_url") or "")
        if "violation-appeal" not in s_url_r:
            continue
        t_r = str(((tmpl_r.get("notice") or {}).get("title") or {}).get("title") or "").lower()
        pk_r = (parse_qs(urlparse(s_url_r).query).get("punish_key") or [""])[0]
        if pk_r and "eligible" in t_r and "ineligible" not in t_r:
            resolved_punish_keys.add(pk_r)

    def _notice_sort_key(n: dict) -> tuple[int, int]:
        tmpl_n = n.get("template_notice") or {}
        ct = int(n.get("create_time") or tmpl_n.get("create_time") or 0)
        s_url = str(tmpl_n.get("schema_url") or "")
        pk_str = (parse_qs(urlparse(s_url).query).get("punish_key") or ["0"])[0]
        pk = int(pk_str) if pk_str.isdigit() else 0
        # Sort by notification `create_time` descending FIRST so a fresh "Account eligible for recommendation" restoration wins
        return (ct, pk)

    captured_notices.sort(key=_notice_sort_key, reverse=True)

    seen_keys: set[str] = set()
    seen_objects: set[str] = set()
    violation_targets: list[tuple[str, str, dict[str, str]]] = []

    for item in captured_notices:
        tmpl = item.get("template_notice") or {}
        schema_url = str(tmpl.get("schema_url") or "")
        if "violation-appeal" not in schema_url:
            continue
        notice_obj = tmpl.get("notice") or {}
        title = str((notice_obj.get("title") or {}).get("title") or "Violation Notice")
        qs = parse_qs(urlparse(schema_url).query)
        punish_key = (qs.get("punish_key") or [""])[0]
        object_id = (qs.get("object_id") or [""])[0]
        object_type = (qs.get("object_type") or ["4"])[0]
        punish_id = (qs.get("punish_id") or [""])[0]
        dedup = punish_key or schema_url
        if dedup in seen_keys:
            continue
        seen_keys.add(dedup)

        obj_dedup = f"{object_type}:{object_id}" if object_id else dedup
        if obj_dedup in seen_objects:
            continue
        seen_objects.add(obj_dedup)

        low_title = title.lower()
        if punish_key in resolved_punish_keys or ("eligible" in low_title and "ineligible" not in low_title):
            reports.append(
                RestrictionActionReport(
                    account_id=account.account_id,
                    scope="account_or_inbox",
                    target_id=object_id,
                    punish_key=punish_key,
                    title=title,
                    previous_state="approved",
                    action_taken="none",
                    appealed_now=False,
                    detail="Огран уже снят (аккаунт/пост сейчас в статусе Eligible).",
                )
            )
            continue

        violation_targets.append(
            (
                title,
                schema_url,
                {
                    "object_id": object_id,
                    "object_type": object_type,
                    "punish_key": punish_key,
                    "punish_id": punish_id,
                },
            )
        )

    # Open each violation-appeal schema_url in Android BytedanceWebview context and appeal if eligible
    for title, schema_url, meta in violation_targets:
        wpage = await ctx_webview.new_page()
        await _attach_fast_cdn_router(wpage, direct_http)
        detail_payload: dict | None = None
        detail_common_qs = "aid=1988&app_language=en&app_name=tiktok_web&channel=tiktok_web&device_platform=web_mobile&os=android"
        ui_commit_resp: dict | None = None

        async def _capture_detail(resp) -> None:
            nonlocal detail_payload, detail_common_qs, ui_commit_resp
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
                    detail_payload = await resp.json()
                except Exception:
                    pass
            elif "/tns-api/appeal/post-commit" in resp.url:
                try:
                    ui_commit_resp = await resp.json()
                except Exception:
                    pass

        wpage.on("response", lambda r: asyncio.create_task(_capture_detail(r)))
        try:
            await wpage.goto(schema_url, wait_until="domcontentloaded", timeout=35000)
            await wpage.wait_for_timeout(4500)
            body_txt = (await wpage.inner_text("body")).strip()

            detail_obj = (detail_payload or {}).get("detail") or {}
            base_info = detail_obj.get("base_info") or {}
            vio_info = detail_obj.get("violation_info") or {}
            appeal_info = vio_info.get("appeal_info") or {}

            perception_stage = base_info.get("perception_stage")
            appeal_type = appeal_info.get("appeal_type") or int(
                parse_qs(urlparse(schema_url).query).get("punish_type", ["2048"])[0]
            )
            appeal_status = appeal_info.get("appeal_status")

            # Check if already submitted / approved / expired
            low_txt = body_txt.lower()
            if "appeal submitted" in low_txt or "request received" in low_txt or perception_stage == 2:
                reports.append(
                    RestrictionActionReport(
                        account_id=account.account_id,
                        scope="account_or_inbox",
                        target_id=meta["object_id"],
                        punish_key=meta["punish_key"],
                        title=title,
                        previous_state="on_appeal",
                        action_taken="none",
                        appealed_now=False,
                        detail="Апелляция уже подана и находится на рассмотрении TikTok (до 24ч).",
                    )
                )
                continue

            if "appeal approved" in low_txt or "restored" in low_txt:
                reports.append(
                    RestrictionActionReport(
                        account_id=account.account_id,
                        scope="account_or_inbox",
                        target_id=meta["object_id"],
                        punish_key=meta["punish_key"],
                        title=title,
                        previous_state="approved",
                        action_taken="none",
                        appealed_now=False,
                        detail="Огран уже снят (Appeal approved).",
                    )
                )
                continue

            if "deadline has expired" in low_txt or "appeal not approved" in low_txt:
                reports.append(
                    RestrictionActionReport(
                        account_id=account.account_id,
                        scope="account_or_inbox",
                        target_id=meta["object_id"],
                        punish_key=meta["punish_key"],
                        title=title,
                        previous_state="closed",
                        action_taken="none",
                        appealed_now=False,
                        detail="Апелляция отклонена или истёк срок подачи.",
                    )
                )
                continue

            commit_ok = False
            last_commit_resp: dict = {}

            # Step 1: Try native WebView UI Appeal -> Reason -> Submit (uses TikTok's internal secsdk + axios instance)
            appeal_btn = wpage.get_by_role("button", name="Appeal")
            if await appeal_btn.count() == 0:
                appeal_btn = wpage.get_by_text("Appeal", exact=True)
            if await appeal_btn.count() > 0 and await appeal_btn.first.is_visible():
                await appeal_btn.first.click()
                await wpage.wait_for_timeout(2000)

                radios = wpage.locator('input[type="radio"], [role="radio"]')
                if await radios.count() > 0 and await radios.first.is_visible():
                    await radios.first.click()
                    await wpage.wait_for_timeout(500)

                ta = wpage.locator("textarea")
                if await ta.count() > 0 and await ta.first.is_visible():
                    await ta.first.fill(appeal_text)
                    await wpage.wait_for_timeout(500)

                sub_btn = wpage.get_by_role("button", name="Submit")
                if await sub_btn.count() == 0:
                    sub_btn = wpage.get_by_text("Submit", exact=True)
                if await sub_btn.count() > 0 and await sub_btn.first.is_visible():
                    for _ in range(3):
                        ui_commit_resp = None
                        await sub_btn.first.click()
                        await wpage.wait_for_timeout(2500)
                        if isinstance(ui_commit_resp, dict) and ui_commit_resp.get("status_code") == 0:
                            commit_ok = True
                            last_commit_resp = ui_commit_resp
                            break
                        after_txt = (await wpage.inner_text("body")).lower()
                        if "appeal submitted" in after_txt or "request received" in after_txt:
                            commit_ok = True
                            last_commit_resp = ui_commit_resp or {"status_code": 0}
                            break

            # Step 2: Fallback direct signed `/tns-api/appeal/post-commit` with captured common_qs (up to 3 retries)
            if not commit_ok:
                reasons_resp = await wpage.evaluate(
                    """async ({appealType, objectId, qs}) => {
                        const r = await fetch(`/tns-api/appeal/reason?${qs}&appeal_type=${appealType}&object_id=${encodeURIComponent(objectId)}`, {credentials: 'include'});
                        return await r.json().catch(() => ({}));
                    }""",
                    {"appealType": appeal_type, "objectId": meta["object_id"], "qs": detail_common_qs},
                )
                reason_opts = (reasons_resp or {}).get("appeal_reasons") or None
                reason_str = build_tns_appeal_reason_json(
                    appeal_reasons_options=reason_opts if isinstance(reason_opts, list) else None,
                    description_text=appeal_text,
                )

                commit_payload = {
                    "object_id": meta["object_id"],
                    "appeal_type": appeal_type,
                    "reason": reason_str,
                    "punish_key": meta["punish_key"],
                    "punish_id": meta["punish_id"],
                    "appeal_uri": "",
                    "extra": "",
                }

                for _attempt in range(3):
                    last_commit_resp = await wpage.evaluate(
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
                    if isinstance(last_commit_resp, dict) and last_commit_resp.get("status_code") == 0:
                        commit_ok = True
                        break
                    await wpage.wait_for_timeout(1500)

            reports.append(
                RestrictionActionReport(
                    account_id=account.account_id,
                    scope="account_or_inbox",
                    target_id=meta["object_id"],
                    punish_key=meta["punish_key"],
                    title=title,
                    previous_state=f"status={appeal_status}/stage={perception_stage}",
                    action_taken="POST /tns-api/appeal/post-commit",
                    appealed_now=commit_ok,
                    detail=(
                        "Апелляция успешно отправлена (status_code=0)!"
                        if commit_ok
                        else f"Ответ /tns-api/appeal/post-commit: {last_commit_resp}"
                    ),
                )
            )
        finally:
            await wpage.close()

    return reports


async def _scan_and_appeal_studio_posts(
    ctx_desktop: BrowserContext,
    direct_http: httpx.AsyncClient,
    account: AccountRecord,
    post_ids: list[str],
) -> list[RestrictionActionReport]:
    """
    Checks each post `aweme_id` in TikTok Studio against BOTH decompiled moderation endpoints (`5590.945fdef2.js`):
      - Mod V1: `/mod/v1/getPenaltyDetails/` -> `POST /mod/v1/submitAppeals` (`appeal_status == 0` CAN_SUBMIT)
      - Mod V2: `/api/moderation/penalties/gettransparencypagecontent` -> `POST /api/moderation/penalties/submitappeal` (`showAppealBtn == true`)
    """
    if not post_ids:
        return []

    reports: list[RestrictionActionReport] = []
    page = await ctx_desktop.new_page()
    await _attach_fast_cdn_router(page, direct_http)

    try:
        await page.goto(
            "https://www.tiktok.com/tiktokstudio",
            wait_until="domcontentloaded",
            timeout=40000,
        )
        await page.wait_for_timeout(4500)

        for vid in post_ids:
            check = await page.evaluate(
                """async (vid) => {
                    const r1 = await fetch(`/mod/v1/getPenaltyDetails/?aid=1988&app_name=tiktok_creator_center&vid=${encodeURIComponent(vid)}`, {credentials: 'include'});
                    const v1 = await r1.json().catch(() => ({}));
                    const r2 = await fetch(`/api/moderation/penalties/gettransparencypagecontent?aid=1988&entityId=${encodeURIComponent(vid)}&enterFrom=`, {credentials: 'include'});
                    const v2 = await r2.json().catch(() => ({}));
                    return {v1, v2};
                }""",
                vid,
            )
            v1 = (check or {}).get("v1") or {}
            v2 = (check or {}).get("v2") or {}

            penalties = v1.get("penalties") or []
            appeal_status = v1.get("appeal_status")
            top_pen = v1.get("top_penalty_details") or {}
            pen_type = top_pen.get("penalty_type", 0)

            # Case A: Mod V2 (`showAppealBtn == true`)
            if v2.get("showAppealBtn") and v2.get("appealDetails"):
                res_v2 = {}
                ok_v2 = False
                for _ in range(3):
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
                        {"appealDetails": v2["appealDetails"], "itemId": vid},
                    )
                    if (res_v2.get("BaseResp") or {}).get("StatusCode") == 0:
                        ok_v2 = True
                        break
                    await page.wait_for_timeout(1200)

                reports.append(
                    RestrictionActionReport(
                        account_id=account.account_id,
                        scope="post_studio",
                        target_id=vid,
                        punish_key="SubmitAppealsV2",
                        title="Ineligible for For You feed (V2)",
                        previous_state="showAppealBtn=true",
                        action_taken="POST /api/moderation/penalties/submitappeal",
                        appealed_now=ok_v2,
                        detail="Апелляция на пост отправлена!" if ok_v2 else f"Ответ: {res_v2}",
                    )
                )
                continue

            # Case B: Mod V1 (`penalties` present & `appeal_status == 0` CAN_SUBMIT)
            if penalties and appeal_status == 0:
                appeal_details_list = [
                    {
                        "issuer": p.get("issuer"),
                        "penalty_id": (p.get("penalty_id") or {}).get("id")
                        if isinstance(p.get("penalty_id"), dict)
                        else p.get("penalty_id"),
                    }
                    for p in penalties
                ]
                qs_params = urlencode(
                    {
                        "aid": "1988",
                        "app_name": "tiktok_creator_center",
                        "vid": vid,
                        "appeal_details": json.dumps(appeal_details_list, separators=(",", ":")),
                    }
                )
                res_v1 = {}
                ok_v1 = False
                for _ in range(3):
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
                        ok_v1 = True
                        break
                    await page.wait_for_timeout(1200)

                reports.append(
                    RestrictionActionReport(
                        account_id=account.account_id,
                        scope="post_studio",
                        target_id=vid,
                        punish_key=str(appeal_details_list[0].get("penalty_id") or ""),
                        title="Ineligible for For You feed (V1)",
                        previous_state=f"appeal_status={appeal_status}",
                        action_taken="POST /mod/v1/submitAppeals",
                        appealed_now=ok_v1,
                        detail="Апелляция на пост отправлена!" if ok_v1 else f"Ответ: {res_v1}",
                    )
                )
            elif penalties or pen_type not in (0, None):
                state_map = {1: "На рассмотрении (REVIEWING)", 3: "Одобрена", 4: "Отклонена", 5: "Истёк срок"}
                reports.append(
                    RestrictionActionReport(
                        account_id=account.account_id,
                        scope="post_studio",
                        target_id=vid,
                        punish_key="",
                        title=f"Post penalty_type={pen_type}",
                        previous_state=state_map.get(appeal_status, f"appeal_status={appeal_status}"),
                        action_taken="none",
                        appealed_now=False,
                        detail="Повторная подача недоступна.",
                    )
                )
    finally:
        await page.close()

    return reports


async def remove_account_restrictions(
    account: AccountRecord,
    *,
    post_ids: list[str] | None = None,
    appeal_text: str = "Our content is original, authentic, and strictly follows all TikTok Community Guidelines.",
    chrome_executable: str | None = None,
) -> AccountAppealSummary:
    """
    Full automated restriction scanner & appeal submitter for a Kintaro session (`x-device`).
    Uses zero Kintaro paid AUTH credits — authenticates directly via `x-device.cookies` inside headless Chrome.
    """
    import os

    exe = chrome_executable
    if not exe:
        for cand in CHROME_CANDIDATES:
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                exe = cand
                break
    if not exe:
        raise RuntimeError(
            "Не найден установленный Google Chrome / Chromium (`/usr/bin/google-chrome`)."
        )

    pw_proxy = _resolve_playwright_http_proxy(account.proxy)
    pw_cookies = _extract_cookies_for_browser(account)
    summary = AccountAppealSummary(account_id=account.account_id)

    known_vids = list(post_ids or [])
    for rec in account.upload_history:
        if rec.aweme_id and rec.aweme_id not in known_vids:
            known_vids.append(rec.aweme_id)

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

                ctx_webview = await browser.new_context(
                    viewport={"width": 430, "height": 932},
                    user_agent=ANDROID_WEBVIEW_UA,
                )
                await ctx_webview.add_cookies(pw_cookies)

                logger.info(
                    "[%s] Проверяю System Notifications / Account Updates (group=661/46/47)...",
                    account.account_id,
                )
                inbox_reports = await _scan_and_appeal_inbox_violations(
                    ctx_desktop, ctx_webview, direct_http, account, appeal_text
                )
                summary.items.extend(inbox_reports)

                logger.info(
                    "[%s] Проверяю постовые ограны в TikTok Studio (постов: %d)...",
                    account.account_id,
                    len(known_vids),
                )
                studio_reports = await _scan_and_appeal_studio_posts(
                    ctx_desktop, direct_http, account, known_vids
                )
                summary.items.extend(studio_reports)
            finally:
                await browser.close()

    return summary
